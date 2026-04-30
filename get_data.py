import argparse
import csv
import heapq
import json
import re
from collections import defaultdict
from html import unescape
from urllib.parse import unquote

import requests


JSON_API_BASE_URL = "http://lac-dams-live2.is.ed.ac.uk/jsonapi/node/digital_object"
ARCH_RECORD_BASE_URL = "https://digital.collections.ed.ac.uk/do"
LUNA_FETCH_URL = "https://images.is.ed.ac.uk/luna/servlet/as/fetchMediaSearch"

API_HEADERS = {"Accept": "application/vnd.api+json"}
LUNA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/javascript,*/*;q=0.1",
    "Referer": "https://images.is.ed.ac.uk/",
}

RECENT_ITEMS_CSV = "recent_items.csv"
RECENT_TINYURLS_CSV = "recent_item_tinyurls.csv"
TOP_N = 1000
PAGE_LIMIT = 50
REQUEST_TIMEOUT = 30

LUNA_IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z0-9]+~\d+~\d+~\d+~\d+\b")
IIIF_INFO_PATTERN = re.compile(r'data-iiif-infojson="[^"]*/iiif/2/([^"]+)/info\.json"')
IIIF_FILENAME_PATTERN = re.compile(
    r"image-([^.\/]+)-[0-9a-fA-F-]+\.tif$",
    re.IGNORECASE,
)
IIIF_ID_IN_URL_PATTERN = re.compile(r"/iiif/2/([^/]+)/")
CANTALOUPE_ID_PATTERN = re.compile(
    r"([0-9a-z]{3}%2Fimage-[^\"'&?\s]+\.tif)",
    re.IGNORECASE,
)
CANTALOUPE_DECODED_PATTERN = re.compile(
    r"([0-9a-z]{3})/((?:image)-[^\"'&?\s]+\.tif)",
    re.IGNORECASE,
)


def build_session():
    session = requests.Session()
    session.headers.update(API_HEADERS)
    return session


def log(message, verbose=True):
    if verbose:
        print(message, flush=True)


def fetch_recent_objects(session, top_n=TOP_N, page_limit=PAGE_LIMIT, max_pages=None, verbose=True):
    top_objects = []
    page_offset = 0
    page_number = 0

    while True:
        if max_pages is not None and page_number >= max_pages:
            log(f"Stopping early after {page_number} page(s) due to --max-pages.", verbose)
            break

        log(
            f"Fetching JSON:API page {page_number + 1} "
            f"(offset={page_offset}, limit={page_limit})...",
            verbose,
        )
        url = (
            f"{JSON_API_BASE_URL}"
            f"?fields[node--digital_object]=field_descriptive_metadata"
            f"&page[limit]={page_limit}"
            f"&page[offset]={page_offset}"
        )

        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()

        objects = payload.get("data", [])
        if not objects:
            break

        for obj in objects:
            node_uuid = obj["id"]
            field_value = (
                obj["attributes"]
                .get("field_descriptive_metadata", {})
                .get("value")
            )
            if not field_value:
                continue

            try:
                metadata = json.loads(field_value)
            except json.JSONDecodeError:
                print(f"Warning: Could not parse JSON for node {node_uuid}")
                continue

            image_count = metadata.get("images")
            if image_count is None:
                continue

            heapq.heappush(top_objects, (image_count, node_uuid, metadata))
            if len(top_objects) > top_n:
                heapq.heappop(top_objects)

        log(
            f"Fetched {len(objects)} object(s); heap currently holds {len(top_objects)} top record(s).",
            verbose,
        )

        page_offset += page_limit
        page_number += 1

    top_objects.sort(reverse=True)
    log(f"Collected {len(top_objects)} top object(s) for enrichment.", verbose)
    return top_objects


def normalise_filename(filename):
    return (filename or "").strip()


def filename_stem(filename):
    name = normalise_filename(filename)
    return name.rsplit(".", 1)[0] if "." in name else name


def parse_luna_attributes(item):
    raw_attributes = item.get("attributes")
    if isinstance(raw_attributes, dict):
        return raw_attributes
    if isinstance(raw_attributes, str) and raw_attributes:
        try:
            return json.loads(raw_attributes)
        except json.JSONDecodeError:
            return {}
    return {}


def luna_item_matches_filename(item, filename):
    attrs = parse_luna_attributes(item)
    stem = filename_stem(filename).lower()

    candidates = {
        stem,
        filename.lower(),
        attrs.get("repro_link_id", "").lower(),
        attrs.get("mediafileName", "").lower(),
        attrs.get("id", "").lower(),
    }

    media_filename = attrs.get("mediafileName", "")
    if media_filename:
        candidates.add(filename_stem(media_filename).lower())

    return stem in candidates or filename.lower() in candidates


def fetch_luna_identifier(session, filename, cache, verbose=True):
    if filename in cache:
        return cache[filename]

    log(f"  Querying LUNA for {filename}...", verbose)
    params = {
        "fullData": "true",
        "bs": 10,
        "q": f"Repro_Record_ID={filename}",
    }

    response = session.get(
        LUNA_FETCH_URL,
        params=params,
        headers=LUNA_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    results = response.json()
    if not isinstance(results, list):
        raise ValueError(f"Unexpected LUNA response for {filename!r}: {type(results)}")

    chosen = None
    for item in results:
        if luna_item_matches_filename(item, filename):
            chosen = item
            break

    if chosen is None and results:
        chosen = results[0]

    luna_identifier = None
    if chosen:
        luna_identifier = chosen.get("id") or chosen.get("identity")

    cache[filename] = luna_identifier
    return luna_identifier


def iter_string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from iter_string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_string_values(nested)


def normalise_cantaloupe_candidate(candidate):
    candidate = candidate.strip()
    encoded_match = CANTALOUPE_ID_PATTERN.search(candidate)
    if encoded_match:
        return encoded_match.group(1)

    decoded = unquote(candidate)
    decoded_match = CANTALOUPE_DECODED_PATTERN.search(decoded)
    if decoded_match:
        return f"{decoded_match.group(1)}%2F{decoded_match.group(2)}"

    iiif_match = IIIF_ID_IN_URL_PATTERN.search(candidate)
    if iiif_match:
        return unescape(iiif_match.group(1))

    return None


def extract_cantaloupe_identifier_from_image(image_data, filename):
    filename = normalise_filename(filename)
    candidates = []

    for key in ("s3_url", "url", "iiif_url", "iiif", "@id", "id"):
        value = image_data.get(key)
        if value:
            candidates.extend(iter_string_values(value))

    candidates.extend(iter_string_values(image_data))

    for candidate in candidates:
        cantaloupe_id = normalise_cantaloupe_candidate(candidate)
        if cantaloupe_id:
            return cantaloupe_id

        decoded = unquote(candidate)
        path_bits = [bit for bit in decoded.split("/") if bit]
        if path_bits and path_bits[-1] == filename and len(path_bits) >= 2:
            return f"{path_bits[-2]}%2F{path_bits[-1]}"

    return None


def fetch_cantaloupe_map(session, arch_uuid, cache, verbose=True):
    if arch_uuid in cache:
        return cache[arch_uuid]

    log(f"Fetching Archipelago record {arch_uuid} for IIIF/Cantaloupe data...", verbose)
    record_url = f"{ARCH_RECORD_BASE_URL}/{arch_uuid}"
    response = session.get(record_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    mapping = {}
    for match in IIIF_INFO_PATTERN.finditer(response.text):
        cantaloupe_id = unescape(match.group(1))
        filename_match = IIIF_FILENAME_PATTERN.search(unquote(cantaloupe_id))
        if not filename_match:
            continue
        mapping[f"{filename_match.group(1)}.tif"] = cantaloupe_id

    cache[arch_uuid] = mapping
    return mapping


def classify_tinyurl(target_url):
    if "/luna/servlet/widget/detail/" in target_url:
        return "widget_detail"
    if "/luna/servlet/detail/" in target_url:
        return "detail"
    if "/luna/servlet/workspace" in target_url:
        return "workspace"
    if "/luna/servlet/view/search" in target_url:
        return "search"
    if "/luna/servlet/view/all" in target_url or "/luna/servlet/widget/view/all" in target_url:
        return "browse"
    if "/luna/servlet/s/" in target_url:
        return "shortlink"
    if "/MediaManager/" in target_url:
        return "media_manager"
    if "/ll/thumbnailView.html" in target_url:
        return "thumbnail_view"
    if "/luna/servlet/" in target_url:
        return "luna_other"
    return "other"


def extract_luna_identifiers(target_url):
    decoded = unquote(target_url or "")
    seen = []
    for identifier in LUNA_IDENTIFIER_PATTERN.findall(decoded):
        if identifier not in seen:
            seen.append(identifier)
    return seen


def load_tinyurl_routes(path):
    routes_by_luna = defaultdict(list)

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        next(reader, None)

        for row in reader:
            if not row:
                continue

            token = row[0].strip()
            target_url = row[1].strip() if len(row) > 1 else ""
            if not token or not target_url:
                continue

            route = {
                "token": token,
                "route_type": classify_tinyurl(target_url),
                "target_url": target_url,
            }

            for luna_identifier in extract_luna_identifiers(target_url):
                routes_by_luna[luna_identifier].append(route)

    return routes_by_luna


def write_recent_items(rows):
    with open(RECENT_ITEMS_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["object_type", "luna", "arch", "file", "cantaloupe"],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_recent_tinyurls(rows):
    with open(RECENT_TINYURLS_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["file", "luna", "arch", "token", "route_type", "target_url"],
        )
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(description="Build ERIC ingest and routing CSVs.")
    parser.add_argument("--top-n", type=int, default=TOP_N, help="Number of top objects to enrich.")
    parser.add_argument("--page-limit", type=int, default=PAGE_LIMIT, help="JSON:API page size.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Stop after this many JSON:API pages for debugging.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress logging.",
    )
    parser.add_argument(
        "--debug-cantaloupe",
        action="store_true",
        help="Print one sample image payload when Cantaloupe extraction fails.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    verbose = not args.quiet
    cantaloupe_debug_dumped = False
    session = build_session()
    top_objects = fetch_recent_objects(
        session,
        top_n=args.top_n,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        verbose=verbose,
    )
    tinyurl_routes = load_tinyurl_routes("tinyurls.csv")
    log(f"Loaded TinyURL routes for {len(tinyurl_routes)} LUNA identifier(s).", verbose)

    luna_cache = {}
    cantaloupe_cache = {}
    recent_rows = []
    tinyurl_rows = []

    for index, (image_count, arch_uuid, metadata) in enumerate(top_objects, start=1):
        image_total = len(metadata.get("as:image", {})) if isinstance(metadata.get("as:image", {}), dict) else image_count
        log(
            f"Enriching object {index}/{len(top_objects)}: arch={arch_uuid}, images={image_total}",
            verbose,
        )
        images = metadata.get("as:image", {})
        if not isinstance(images, dict):
            continue

        cantaloupe_map = fetch_cantaloupe_map(
            session,
            arch_uuid,
            cantaloupe_cache,
            verbose=verbose,
        )

        for img in images.values():
            filename = normalise_filename(img.get("name"))
            if not filename:
                continue

            try:
                luna_identifier = fetch_luna_identifier(
                    session,
                    filename,
                    luna_cache,
                    verbose=verbose,
                )
            except Exception as exc:
                print(f"Warning: Could not fetch LUNA ID for {filename}: {exc}")
                luna_identifier = None

            cantaloupe_identifier = extract_cantaloupe_identifier_from_image(img, filename)
            if not cantaloupe_identifier:
                cantaloupe_identifier = cantaloupe_map.get(filename)
            if not cantaloupe_identifier:
                print(
                    f"Warning: Could not find Cantaloupe ID for {filename} "
                    f"on Archipelago record {arch_uuid}"
                )
                if args.debug_cantaloupe and not cantaloupe_debug_dumped:
                    print("Debug: sample as:image payload follows:", flush=True)
                    print(json.dumps(img, indent=2, sort_keys=True)[:4000], flush=True)
                    cantaloupe_debug_dumped = True

            recent_rows.append(
                {
                    "object_type": "Image",
                    "luna": luna_identifier or "",
                    "arch": arch_uuid,
                    "file": filename,
                    "cantaloupe": cantaloupe_identifier or "",
                }
            )

            if not luna_identifier:
                continue

            for route in tinyurl_routes.get(luna_identifier, []):
                tinyurl_rows.append(
                    {
                        "file": filename,
                        "luna": luna_identifier,
                        "arch": arch_uuid,
                        "token": route["token"],
                        "route_type": route["route_type"],
                        "target_url": route["target_url"],
                    }
                )

    write_recent_items(recent_rows)
    write_recent_tinyurls(tinyurl_rows)

    print(f"Done! Wrote {len(recent_rows)} rows to {RECENT_ITEMS_CSV}")
    print(f"Done! Wrote {len(tinyurl_rows)} rows to {RECENT_TINYURLS_CSV}")


if __name__ == "__main__":
    main()
