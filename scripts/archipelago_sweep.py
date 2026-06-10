import json
import os
import re
from html import unescape
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


JSON_API_BASE_URL = "http://lac-dams-live2.is.ed.ac.uk/jsonapi/node/digital_object"
ARCH_RECORD_BASE_URL = "https://digital.collections.ed.ac.uk/do"
LUNA_FETCH_URL = "https://images.is.ed.ac.uk/luna/servlet/as/fetchMediaSearch"
JSON_API_SORT = "created"

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
PAGE_LIMIT = 50
REQUEST_TIMEOUT = 30
DEFAULT_HTTP_RETRIES = 5
DEFAULT_RETRY_BACKOFF = 1.0

LUNA_IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z0-9]+~\d+~\d+~\d+~\d+\b")
IIIF_INFO_PATTERN = re.compile(r'data-iiif-infojson="([^"]*/iiif/2/([^"]+)/info\.json)"')
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


def build_session(http_retries=DEFAULT_HTTP_RETRIES, retry_backoff=DEFAULT_RETRY_BACKOFF):
    session = requests.Session()
    session.headers.update(API_HEADERS)
    retry = Retry(
        total=http_retries,
        connect=http_retries,
        read=http_retries,
        status=http_retries,
        backoff_factor=retry_backoff,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def log(message, verbose=True):
    if verbose:
        print(message, flush=True)


def load_checkpoint(path):
    if not path or not os.path.exists(path):
        return None

    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_checkpoint(path, payload):
    if not path:
        return

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def load_crawl_checkpoint(checkpoint_path, page_limit, resume, sort=None):
    checkpoint = load_checkpoint(checkpoint_path) if resume else None
    page_offset = 0
    page_number = 0

    if checkpoint:
        saved_page_limit = checkpoint.get("page_limit")
        saved_sort = checkpoint.get("sort")
        sort_matches = sort is None or saved_sort in {None, sort}
        if saved_page_limit == page_limit and sort_matches:
            page_offset = checkpoint.get("page_offset", 0)
            page_number = checkpoint.get("page_number", 0)

    return page_offset, page_number


def fetch_source_page(session, page_offset, page_limit, sort=JSON_API_SORT, verbose=True):
    log(
        f"Fetching JSON:API page at offset={page_offset}, limit={page_limit}, sort={sort}...",
        verbose,
    )
    url = (
        f"{JSON_API_BASE_URL}"
        f"?fields[node--digital_object]=field_descriptive_metadata,created"
        f"&sort={sort}"
        f"&page[limit]={page_limit}"
        f"&page[offset]={page_offset}"
    )

    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    objects = payload.get("data", [])
    page_rows = []

    for obj in objects:
        node_uuid = obj["id"]
        created = obj.get("attributes", {}).get("created")
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

        page_rows.append(
            {
                "arch": node_uuid,
                "metadata": metadata,
                "created": created,
            }
        )

    log(
        f"Fetched {len(objects)} object(s); {len(page_rows)} object(s) had usable metadata.",
        verbose,
    )
    return objects, page_rows


def normalise_filename(filename):
    return (filename or "").strip()


def filename_stem(filename):
    name = normalise_filename(filename)
    return name.rsplit(".", 1)[0] if "." in name else name


def filename_leaf(filename):
    name = normalise_filename(filename)
    return name.rsplit("/", 1)[-1]


def filename_marker(filename):
    stem = filename_stem(filename)
    marker_match = re.search(r"([cd])(?:-\d+)?$", stem, re.IGNORECASE)
    return marker_match.group(1).lower() if marker_match else stem[-1:].lower()


def canonical_file_identifier(filename):
    leaf_name = filename_leaf(filename)
    stem = filename_stem(leaf_name)
    extension = ""
    if "." in leaf_name:
        extension = leaf_name.rsplit(".", 1)[1].lower()

    tail = filename_marker(leaf_name)
    if extension in {"jpg", "jpeg"}:
        if tail == "c":
            extension = "tif"
        elif tail == "d":
            extension = "jpg"

    if extension:
        return f"{stem}.{extension}"
    return stem


def unique_nonempty(values):
    seen = set()
    ordered = []
    for value in values:
        value = (value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(value)
    return ordered


def build_luna_filename_variants(filename):
    full_name = normalise_filename(filename)
    if not full_name:
        return []

    variants = [full_name]
    leaf_name = filename_leaf(full_name)
    if leaf_name != full_name:
        variants.append(leaf_name)

    stem = filename_stem(full_name)
    extension = full_name.rsplit(".", 1)[1].lower() if "." in full_name else ""
    marker = filename_marker(full_name)

    alternate_extensions = []
    if extension in {"tif", "tiff"} and marker in {"c", "d"}:
        alternate_extensions.extend(["jpg", "jpeg"])
    elif extension in {"jpg", "jpeg"} and marker == "c":
        alternate_extensions.extend(["tif", "tiff"])

    for alternate_extension in alternate_extensions:
        alternate_name = f"{stem}.{alternate_extension}"
        variants.append(alternate_name)
        alternate_leaf = filename_leaf(alternate_name)
        if alternate_leaf != alternate_name:
            variants.append(alternate_leaf)

    return unique_nonempty(variants)


def build_luna_query_candidates(filename):
    query_candidates = []
    seen = set()

    for candidate_name in build_luna_filename_variants(filename):
        full_name = normalise_filename(candidate_name)
        leaf_name = filename_leaf(full_name)
        leaf_stem = filename_stem(leaf_name)
        full_stem = filename_stem(full_name)

        for field_name, candidate in (
            ("Repro_Record_ID", full_name),
            ("Repro_Record_ID", leaf_name),
            ("Repro_Link_ID", leaf_stem),
            ("Repro_Link_ID", full_stem),
        ):
            key = (field_name.lower(), candidate.lower())
            if not candidate or key in seen:
                continue
            seen.add(key)
            query_candidates.append((field_name, candidate))

    return query_candidates


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
    filename_variants = build_luna_filename_variants(filename)
    names_and_stems = set()
    for variant in filename_variants:
        full_name = normalise_filename(variant).lower()
        leaf_name = filename_leaf(variant).lower()
        full_stem = filename_stem(variant).lower()
        leaf_stem = filename_stem(leaf_name).lower()
        names_and_stems.update({full_name, leaf_name, full_stem, leaf_stem})

    candidates = {
        attrs.get("repro_link_id", "").lower(),
        attrs.get("repro_record_id", "").lower(),
        attrs.get("mediafileName", "").lower(),
        attrs.get("id", "").lower(),
    }

    media_filename = attrs.get("mediafileName", "")
    if media_filename:
        candidates.add(filename_stem(media_filename).lower())
        candidates.add(normalise_filename(media_filename).lower())

    repro_record_id = attrs.get("repro_record_id", "")
    if repro_record_id:
        candidates.add(filename_leaf(repro_record_id).lower())
        candidates.add(filename_stem(repro_record_id).lower())

    return any(candidate in candidates for candidate in names_and_stems if candidate)


def fetch_luna_identifier(session, filename, cache, verbose=True):
    cache_key = normalise_filename(filename).lower()
    if cache_key in cache:
        return cache[cache_key]

    query_candidates = build_luna_query_candidates(filename)
    attempted_queries = []
    all_results = []
    seen_result_ids = set()

    for field_name, candidate in query_candidates:
        attempted_queries.append(f"{field_name}={candidate}")
        log(f"  Querying LUNA for {filename} via {field_name}={candidate}...", verbose)
        params = {
            "fullData": "true",
            "bs": 10,
            "q": f"{field_name}={candidate}",
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

        for item in results:
            result_id = item.get("id") or item.get("identity") or json.dumps(item, sort_keys=True)
            if result_id in seen_result_ids:
                continue
            seen_result_ids.add(result_id)
            all_results.append(item)

        chosen = next((item for item in results if luna_item_matches_filename(item, filename)), None)
        if chosen is None and len(results) == 1:
            chosen = results[0]

        if chosen:
            luna_identifier = chosen.get("id") or chosen.get("identity")
            cache[cache_key] = (luna_identifier, attempted_queries)
            return cache[cache_key]

    chosen = next((item for item in all_results if luna_item_matches_filename(item, filename)), None)
    if chosen is None and len(all_results) == 1:
        chosen = all_results[0]

    luna_identifier = None
    if chosen:
        luna_identifier = chosen.get("id") or chosen.get("identity")

    cache[cache_key] = (luna_identifier, attempted_queries)
    return cache[cache_key]


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


def fetch_cantaloupe_data(session, arch_uuid, cache, verbose=True):
    if arch_uuid in cache:
        return cache[arch_uuid]

    log(f"Fetching Archipelago record {arch_uuid} for IIIF/Cantaloupe data...", verbose)
    record_url = f"{ARCH_RECORD_BASE_URL}/{arch_uuid}"
    response = session.get(record_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    mapping = {}
    for match in IIIF_INFO_PATTERN.finditer(response.text):
        info_url = unescape(match.group(1))
        cantaloupe_id = unescape(match.group(2))
        filename_match = IIIF_FILENAME_PATTERN.search(unquote(cantaloupe_id))
        if not filename_match:
            continue
        mapping[f"{filename_match.group(1)}.tif"] = {
            "cantaloupe": cantaloupe_id,
        }

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
