import argparse
import csv
from html import unescape
import json
from pathlib import Path
import re

import bootstrap

from scripts.archipelago_sweep import (
    JSON_API_BASE_URL,
    PAGE_LIMIT,
    REQUEST_TIMEOUT,
    build_session,
    canonical_file_identifier,
    fetch_source_page,
    normalise_filename,
)


DEFAULT_INPUT_CSV = Path("data/luna_0.csv")
OUTPUT_DIR = Path("data/output")
SOURCE_ID_KEYS = (
    "source_id",
    "source id",
)
ARCH_NODE_BASE_URL = "https://digital.collections.ed.ac.uk/node"
SOURCE_ID_PATTERNS = (
    re.compile(r'"source_id"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"source_id"\s*:\s*([0-9A-Za-z-]+)', re.IGNORECASE),
    re.compile(r"'source_id'\s*:\s*'([^']+)'", re.IGNORECASE),
    re.compile(r"'source_id'\s*:\s*([0-9A-Za-z-]+)", re.IGNORECASE),
    re.compile(r"\bsource_id\b\s*[:=]\s*[\"']?([0-9A-Za-z-]+)", re.IGNORECASE),
)
PARENT_REFERENCE_KEYS = (
    "ispartof",
    "is_part_of",
    "is part of",
    "part_of",
    "part of",
    "member_of",
    "member of",
    "parent",
    "parent_uuid",
    "parent uuid",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Read filenames from a CSV, find the matching Archipelago object, "
            "follow its ispartof chain, and export the parent source_id."
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
        help="Output CSV path (default: data/output/<input_stem>_source_id.csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process this many input rows.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=PAGE_LIMIT,
        help="JSON:API page size for the Archipelago crawl.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Only inspect this many Archipelago pages.",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Start the Archipelago crawl at this JSON:API page offset.",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="Start the Archipelago crawl at this 1-based page number instead of offset.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-page and per-row progress logging.",
    )
    return parser.parse_args()


def log(message, verbose=True):
    if verbose:
        print(message, flush=True)


def default_output_path(input_path):
    return OUTPUT_DIR / f"{input_path.stem}_source_id.csv"


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


def normalise_key(value):
    return "".join(char.lower() if char.isalnum() else " " for char in (value or "")).split()


def key_matches(key, expected):
    return " ".join(normalise_key(key)) == expected


def iter_values(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from iter_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_values(nested)
    else:
        yield value


def find_first_text_by_keys(payload, keys):
    if not isinstance(payload, dict):
        return ""

    normalised_targets = {" ".join(normalise_key(key)) for key in keys}

    for key, value in payload.items():
        if " ".join(normalise_key(key)) not in normalised_targets:
            continue

        for candidate in iter_values(value):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

    for value in payload.values():
        if isinstance(value, dict):
            found = find_first_text_by_keys(value, keys)
            if found:
                return found
        elif isinstance(value, list):
            for nested in value:
                if isinstance(nested, dict):
                    found = find_first_text_by_keys(nested, keys)
                    if found:
                        return found

    return ""


def extract_source_id_from_payload(payload):
    source_id = find_first_text_by_keys(payload, SOURCE_ID_KEYS)
    if source_id:
        return source_id

    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, str):
                text = value.strip()
                if text[:1] not in {"{", "["}:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                source_id = extract_source_id_from_payload(parsed)
                if source_id:
                    return source_id
            else:
                source_id = extract_source_id_from_payload(value)
                if source_id:
                    return source_id
    elif isinstance(payload, list):
        for value in payload:
            source_id = extract_source_id_from_payload(value)
            if source_id:
                return source_id

    return ""


def looks_like_uuid(value):
    text = (value or "").strip()
    if len(text) != 36:
        return False

    parts = text.split("-")
    return [len(part) for part in parts] == [8, 4, 4, 4, 12] and all(
        all(char in "0123456789abcdefABCDEF" for char in part)
        for part in parts
    )


def looks_like_node_id(value):
    text = str(value or "").strip()
    return text.isdigit()


def extract_parent_reference_candidates(metadata):
    candidates = []
    normalised_parent_keys = {" ".join(normalise_key(key)) for key in PARENT_REFERENCE_KEYS}

    def walk(value, parent_key=None):
        if isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, key)
        elif isinstance(value, list):
            for nested in value:
                walk(nested, parent_key)
        elif isinstance(value, (str, int)):
            text = str(value).strip()
            key_text = " ".join(normalise_key(parent_key or ""))
            if key_text in normalised_parent_keys and (looks_like_uuid(text) or looks_like_node_id(text)):
                candidates.append(text)
            elif (looks_like_uuid(text) or looks_like_node_id(text)) and any(
                token in key_text for token in ("part", "parent", "member")
            ):
                candidates.append(text)

    walk(metadata)

    seen = set()
    ordered = []
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(candidate)
    return ordered


def fetch_object_metadata(session, object_ref, cache):
    cache_key = str(object_ref or "").lower()
    if not cache_key:
        return None
    if cache_key in cache:
        return cache[cache_key]

    metadata = {}
    if looks_like_uuid(object_ref):
        response = session.get(
            f"{JSON_API_BASE_URL}/{object_ref}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        field_value = (
            data.get("attributes", {})
            .get("field_descriptive_metadata", {})
            .get("value")
        )
        if field_value:
            try:
                metadata = json.loads(field_value)
            except json.JSONDecodeError:
                metadata = {}
    elif looks_like_node_id(object_ref):
        json_response = session.get(
            f"{ARCH_NODE_BASE_URL}/{object_ref}?_format=json",
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if json_response.ok:
            try:
                payload = json_response.json()
            except json.JSONDecodeError:
                payload = None

            source_id = extract_source_id_from_payload(payload)
            if source_id:
                metadata = {"source_id": source_id}

        if not metadata:
            response = session.get(
                f"{ARCH_NODE_BASE_URL}/{object_ref}",
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            page_text = response.text
            for candidate_text in (page_text, unescape(page_text)):
                for pattern in SOURCE_ID_PATTERNS:
                    match = pattern.search(candidate_text)
                    if match:
                        metadata = {"source_id": match.group(1)}
                        break
                if metadata:
                    break

    cache[cache_key] = metadata
    return metadata


def extract_source_id_from_parent(session, metadata, parent_cache):
    parent_refs = extract_parent_reference_candidates(metadata)
    if not parent_refs:
        return "", ""

    for parent_ref in parent_refs:
        parent_metadata = fetch_object_metadata(session, parent_ref, parent_cache)
        source_id = find_first_text_by_keys(parent_metadata, SOURCE_ID_KEYS)
        if source_id:
            return source_id, parent_ref

    return "", parent_refs[0]


def crawl_archipelago_for_files(session, wanted_files, page_limit, max_pages, start_offset, verbose):
    matches = {}
    parent_cache = {}
    page_offset = start_offset
    page_number = start_offset // page_limit if page_limit else 0

    while wanted_files - matches.keys():
        if max_pages is not None and page_number >= max_pages:
            break

        raw_objects, source_rows = fetch_source_page(
            session,
            page_offset,
            page_limit,
            verbose=verbose,
        )
        if not raw_objects:
            break

        for source_row in source_rows:
            arch_uuid = source_row["arch"]
            metadata = source_row["metadata"]
            images = metadata.get("as:image", {})
            if not isinstance(images, dict):
                continue

            for image_data in images.values():
                source_filename = normalise_filename(image_data.get("name"))
                if not source_filename:
                    continue

                canonical_filename = canonical_file_identifier(source_filename)
                if canonical_filename not in wanted_files or canonical_filename in matches:
                    continue

                source_id, parent_ref = extract_source_id_from_parent(session, metadata, parent_cache)
                matches[canonical_filename] = {
                    "arch_uuid": arch_uuid,
                    "parent_ref": parent_ref,
                    "source_id": source_id,
                    "source_filename": source_filename,
                }
                log(
                    f"Matched {canonical_filename} -> arch={arch_uuid} parent={parent_ref or 'NONE'} source_id={source_id or 'NONE'}",
                    verbose=verbose,
                )

        page_offset += page_limit
        page_number += 1
        log(
            f"Scanned {page_number - (start_offset // page_limit) if page_limit else page_number} page(s) "
            f"from offset {start_offset}; found {len(matches)} of {len(wanted_files)} target file(s).",
            verbose=verbose,
        )

    return matches


def main():
    args = parse_args()
    verbose = not args.quiet
    output_path = args.output or default_output_path(args.input)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.start_page is not None and args.start_offset:
        raise SystemExit("Use either --start-offset or --start-page, not both.")
    if args.start_page is not None and args.start_page < 1:
        raise SystemExit("--start-page must be 1 or greater.")

    start_offset = args.start_offset
    if args.start_page is not None:
        start_offset = (args.start_page - 1) * args.page_limit

    original_files = []
    wanted_files = set()
    for index, original_filename in enumerate(read_input_rows(args.input), start=1):
        if args.limit is not None and index > args.limit:
            break
        original_files.append(original_filename)
        wanted_files.add(canonical_file_identifier(original_filename))

    session = build_session()
    matches = crawl_archipelago_for_files(
        session,
        wanted_files,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        start_offset=start_offset,
        verbose=verbose,
    )

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file",
                "canonical_file",
                "arch_uuid",
                "parent_ref",
                "source_id",
                "source_filename",
            ],
        )
        writer.writeheader()

        for original_filename in original_files:
            canonical_filename = canonical_file_identifier(original_filename)
            match = matches.get(canonical_filename, {})
            writer.writerow(
                {
                    "file": original_filename,
                    "canonical_file": canonical_filename,
                    "arch_uuid": match.get("arch_uuid", ""),
                    "parent_ref": match.get("parent_ref", ""),
                    "source_id": match.get("source_id", ""),
                    "source_filename": match.get("source_filename", ""),
                }
            )

    print(
        f"Wrote {len(original_files)} row(s) to {output_path}; found {len(matches)} Archipelago match(es).",
        flush=True,
    )


if __name__ == "__main__":
    main()
