import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_INPUT_CSV = Path("data/luna_0.csv")
OUTPUT_DIR = Path("data/output")
NORMALISE_REPRO_SUFFIX_PATTERN = re.compile(r"([cd])_\d+(?=\.[^.]+$)", re.IGNORECASE)
LUNA_FETCH_URL = "https://images.is.ed.ac.uk/luna/servlet/as/fetchMediaSearch"
LUNA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/javascript,*/*;q=0.1",
    "Referer": "https://images.is.ed.ac.uk/",
}
REQUEST_TIMEOUT = 30
SHELFMARK_KEYS = (
    "work_shelfmark",
    "shelfmark",
    "work shelfmark",
    "call_number",
    "call number",
    "reference",
    "reference_number",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Look up LUNA records for filenames in a CSV and export the shelfmark "
            "and LUNA identifier."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_CSV,
        help=f"Input CSV path (default: {DEFAULT_INPUT_CSV})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path (default: data/output/<input_stem>_lookup.csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process this many input rows.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-row progress logging.",
    )
    return parser.parse_args()


def log(message, verbose=True):
    if verbose:
        print(message, flush=True)


def default_output_path(input_path):
    return OUTPUT_DIR / f"{input_path.stem}_lookup.csv"


def normalise_lookup_filename(filename):
    return NORMALISE_REPRO_SUFFIX_PATTERN.sub(r"\1", (filename or "").strip())


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


def normalise_key(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def parse_luna_attributes(item):
    raw_attributes = item.get("attributes") if isinstance(item, dict) else None
    if isinstance(raw_attributes, dict):
        return raw_attributes
    if isinstance(raw_attributes, str) and raw_attributes:
        try:
            return json.loads(raw_attributes)
        except json.JSONDecodeError:
            return {}
    return {}


def extract_shelfmark(attributes):
    if not isinstance(attributes, dict):
        return ""

    normalised = {normalise_key(key): value for key, value in attributes.items()}
    for key in SHELFMARK_KEYS:
        value = normalised.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key, value in normalised.items():
        if "shelfmark" in key and isinstance(value, str) and value.strip():
            return value.strip()

    return ""


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


def fetch_query_results(field_name, candidate):
    query = urlencode(
        {
            "fullData": "true",
            "bs": 10,
            "q": f"{field_name}={candidate}",
        }
    )
    request = Request(f"{LUNA_FETCH_URL}?{query}", headers=LUNA_HEADERS)
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = response.read().decode("utf-8")
    results = json.loads(payload)
    if not isinstance(results, list):
        raise ValueError(f"Unexpected LUNA response type: {type(results)}")
    return results


def choose_luna_item(filename, cache):
    cache_key = filename.lower()
    if cache_key in cache:
        return cache[cache_key]

    attempted_queries = []
    all_results = []
    seen_result_ids = set()

    for field_name, candidate in build_luna_query_candidates(filename):
        attempted_queries.append(f"{field_name}={candidate}")
        results = fetch_query_results(field_name, candidate)

        for item in results:
            result_id = item.get("id") or item.get("identity") or json.dumps(item, sort_keys=True)
            if result_id in seen_result_ids:
                continue
            seen_result_ids.add(result_id)
            all_results.append(item)

        chosen = next((item for item in results if luna_item_matches_filename(item, filename)), None)
        if chosen is None and len(results) == 1:
            chosen = results[0]

        if chosen is not None:
            cache[cache_key] = (chosen, attempted_queries)
            return cache[cache_key]

    chosen = next((item for item in all_results if luna_item_matches_filename(item, filename)), None)
    if chosen is None and len(all_results) == 1:
        chosen = all_results[0]

    cache[cache_key] = (chosen, attempted_queries)
    return cache[cache_key]


def read_input_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        first_row = next(reader, None)
        if not first_row:
            return

        first_value = (first_row[0] if first_row else "").strip()
        has_header = first_value.lower() == "file"

        if not has_header and first_value:
            yield first_value

        for row in reader:
            value = (row[0] if row else "").strip()
            if value:
                yield value


def main():
    args = parse_args()
    verbose = not args.quiet
    output_path = args.output or default_output_path(args.input)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cache = {}
    processed = 0
    found = 0

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file",
                "lookup_file",
                "shelfmark",
                "luna_identifier",
                "attempted_queries",
            ],
        )
        writer.writeheader()

        for original_filename in read_input_rows(args.input):
            if args.limit is not None and processed >= args.limit:
                break

            processed += 1
            lookup_filename = normalise_lookup_filename(original_filename)
            log(
                f"[{processed}] Looking up {original_filename} as {lookup_filename}",
                verbose=verbose,
            )

            item, attempted_queries = choose_luna_item(lookup_filename, cache)
            attributes = parse_luna_attributes(item) if item else {}
            luna_identifier = item.get("id") or item.get("identity") if item else ""
            shelfmark = extract_shelfmark(attributes)
            if luna_identifier:
                found += 1

            writer.writerow(
                {
                    "file": original_filename,
                    "lookup_file": lookup_filename,
                    "shelfmark": shelfmark,
                    "luna_identifier": luna_identifier,
                    "attempted_queries": " | ".join(attempted_queries),
                }
            )

    print(
        f"Wrote {processed} row(s) to {output_path} with {found} LUNA match(es).",
        flush=True,
    )


if __name__ == "__main__":
    main()
