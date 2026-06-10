"""
Harvest image metadata from Archipelago and enrich it with LUNA/Cantaloupe IDs.

This script intentionally keeps two filename forms in play:

1. `source_filename`
   The original image name from Archipelago metadata. This may include a folder
   path and may reflect the source-system extension exactly as stored upstream.
   We use this form when querying LUNA, because LUNA may hold the value as a
   full Repro Record ID, a leaf filename, or a stem-only Repro Link ID.

2. `file`
   The canonical ERIC file identifier. This is always the leaf filename only,
   with the local rule applied that `...c.jpg` really means `...c.tif`, while
   `...d.jpg` remains `...d.jpg`.

In short: we match LUNA with the richest source filename we have, but we store
the normalized `file` value we want ERIC to use consistently.
"""

import argparse
import csv
import os
from collections import defaultdict

import bootstrap
from project_paths import (
    CHECKPOINT_JSON,
    MISSING_LUNA_CSV,
    RECENT_ITEMS_CSV,
    RECENT_TINYURLS_CSV,
    TINYURLS_CSV,
    ensure_project_dirs,
)
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    JSON_API_SORT,
    PAGE_LIMIT,
    LUNA_IDENTIFIER_PATTERN,
    build_session,
    canonical_file_identifier,
    classify_tinyurl,
    extract_cantaloupe_identifier_from_image,
    extract_luna_identifiers,
    fetch_cantaloupe_data,
    fetch_luna_identifier,
    fetch_source_page,
    load_checkpoint,
    load_crawl_checkpoint,
    log,
    normalise_filename,
    save_checkpoint,
    unique_nonempty,
)


def remove_file_if_exists(path, verbose=True):
    if path and os.path.exists(path):
        os.remove(path)
        log(f"Removed existing file: {path}", verbose)


def reset_output_files(checkpoint_path, verbose=True):
    for path in (
        RECENT_ITEMS_CSV,
        RECENT_TINYURLS_CSV,
        MISSING_LUNA_CSV,
        checkpoint_path,
    ):
        remove_file_if_exists(path, verbose=verbose)


def clear_output_csvs(verbose=True):
    for path in (
        RECENT_ITEMS_CSV,
        RECENT_TINYURLS_CSV,
        MISSING_LUNA_CSV,
    ):
        remove_file_if_exists(path, verbose=verbose)




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


def load_csv_keys(path, key_builder):
    if not os.path.exists(path):
        return set()

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return {
            key
            for row in reader
            for key in [key_builder(row)]
            if key is not None
        }


def load_existing_recent_rows():
    if not os.path.exists(RECENT_ITEMS_CSV):
        return {}

    rows = {}
    with open(RECENT_ITEMS_CSV, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            arch = normalise_filename(row.get("arch"))
            file_name = canonical_file_identifier(row.get("file"))
            if not arch or not file_name:
                continue
            rows[(arch, file_name)] = row
    return rows


def load_missing_image_keys():
    return load_csv_keys(
        MISSING_LUNA_CSV,
        lambda row: (
            normalise_filename(row.get("arch")),
            canonical_file_identifier(row.get("file")),
        ) if row.get("arch") and row.get("file") else None,
    )


def load_existing_tinyurl_map():
    tinyurl_map = defaultdict(set)
    if not os.path.exists(RECENT_TINYURLS_CSV):
        return tinyurl_map

    with open(RECENT_TINYURLS_CSV, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            arch = normalise_filename(row.get("arch"))
            file_name = canonical_file_identifier(row.get("file"))
            luna = normalise_filename(row.get("luna"))
            token = normalise_filename(row.get("token"))
            if not arch or not file_name or not luna or not token:
                continue
            tinyurl_map[(arch, file_name, luna)].add(token)
    return tinyurl_map


def count_csv_rows(path):
    if not os.path.exists(path):
        return 0

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def open_csv_writer(path, fieldnames, resume=False):
    should_append = resume and os.path.exists(path) and os.path.getsize(path) > 0
    mode = "a" if should_append else "w"
    handle = open(path, mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    if not should_append:
        writer.writeheader()
        handle.flush()
    return handle, writer


def parse_args():
    parser = argparse.ArgumentParser(description="Build ERIC ingest and routing CSVs.")
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the checkpoint/output CSVs instead of starting from scratch.",
    )
    parser.add_argument(
        "--checkpoint",
        default=CHECKPOINT_JSON,
        help="Path to the JSON checkpoint file used for resumable runs.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete generated CSV outputs and the crawl checkpoint before starting.",
    )
    parser.add_argument(
        "--clear-csvs",
        action="store_true",
        help="Delete generated CSV outputs but keep the crawl checkpoint.",
    )
    parser.add_argument(
        "--http-retries",
        type=int,
        default=DEFAULT_HTTP_RETRIES,
        help="Retry count for transient HTTP/network failures on GET requests.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=DEFAULT_RETRY_BACKOFF,
        help="Backoff factor for HTTP retries; larger values wait longer between attempts.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    verbose = not args.quiet
    cantaloupe_debug_dumped = False

    # 1. Prepare the run state: make sure directories exist, decide whether
    #    to start fresh, and restore the oldest-first crawl checkpoint if we
    #    are resuming a previous run.
    ensure_project_dirs()
    if args.fresh and args.clear_csvs:
        raise SystemExit("Use either --fresh or --clear-csvs, not both.")
    if args.fresh:
        reset_output_files(args.checkpoint, verbose=verbose)
    elif args.clear_csvs:
        clear_output_csvs(verbose=verbose)
    session = build_session(
        http_retries=args.http_retries,
        retry_backoff=args.retry_backoff,
    )
    page_offset, page_number = load_crawl_checkpoint(
        args.checkpoint,
        args.page_limit,
        args.resume and not args.fresh,
    )
    if args.resume and (page_offset or page_number):
        log(
            f"Resuming oldest-first crawl from page {page_number + 1} "
            f"(offset={page_offset}).",
            verbose,
        )
    # 2. Load the lookup material that lets us skip work safely on resume:
    #    known tinyurl routes, recent CSV rows already written, and images
    #    previously recorded as missing a LUNA match.
    tinyurl_routes = load_tinyurl_routes(TINYURLS_CSV)
    log(f"Loaded TinyURL routes for {len(tinyurl_routes)} LUNA identifier(s).", verbose)

    luna_cache = {}
    cantaloupe_cache = {}
    existing_recent_rows = load_existing_recent_rows() if args.resume else {}
    missing_image_keys = load_missing_image_keys() if args.resume else set()
    existing_tinyurl_map = load_existing_tinyurl_map() if args.resume else defaultdict(set)
    recent_written = 0
    tinyurl_written = 0
    missing_luna_written = 0
    skipped_images = 0

    recent_handle, recent_writer = open_csv_writer(
        RECENT_ITEMS_CSV,
        ["object_type", "luna", "arch", "file", "cantaloupe", "source_created_at", "width", "height"],
        resume=args.resume,
    )
    tinyurl_handle, tinyurl_writer = open_csv_writer(
        RECENT_TINYURLS_CSV,
        ["file", "luna", "arch", "token", "route_type", "target_url"],
        resume=args.resume,
    )
    missing_handle, missing_writer = open_csv_writer(
        MISSING_LUNA_CSV,
        ["arch", "file", "attempted_queries"],
        resume=args.resume,
    )

    try:
        # 3. Crawl Archipelago JSON:API oldest-first, one source page at a
        #    time, and checkpoint after each completed page.
        while True:
            if args.max_pages is not None and page_number >= args.max_pages:
                log(f"Stopping early after {page_number} page(s) due to --max-pages.", verbose)
                break

            raw_objects, source_rows = fetch_source_page(
                session,
                page_offset,
                args.page_limit,
                verbose=verbose,
            )
            if not raw_objects:
                save_checkpoint(
                    args.checkpoint,
                    {
                        "stage": "complete",
                        "page_offset": page_offset,
                        "page_number": page_number,
                        "page_limit": args.page_limit,
                        "sort": JSON_API_SORT,
                    },
                )
                break

            if not source_rows:
                page_offset += args.page_limit
                page_number += 1
                save_checkpoint(
                    args.checkpoint,
                    {
                        "stage": "crawl",
                        "page_offset": page_offset,
                        "page_number": page_number,
                        "page_limit": args.page_limit,
                        "sort": JSON_API_SORT,
                    },
                )
                continue

            # 4. For each source object, read the descriptive metadata and
            #    fetch any Cantaloupe identifiers we can reuse for its images.
            for row_index, source_row in enumerate(source_rows, start=1):
                arch_uuid = source_row["arch"]
                metadata = source_row["metadata"]
                created = source_row.get("created") or "unknown"
                image_total = len(metadata.get("as:image", {})) if isinstance(metadata.get("as:image", {}), dict) else metadata.get("images", 0)
                log(
                    f"Enriching source object page={page_number + 1} row={row_index}/{len(source_rows)}: "
                    f"arch={arch_uuid}, created={created}, images={image_total}",
                    verbose,
                )
                images = metadata.get("as:image", {})
                if not isinstance(images, dict):
                    continue

                cantaloupe_data = fetch_cantaloupe_data(
                    session,
                    arch_uuid,
                    cantaloupe_cache,
                    verbose=verbose,
                )

                # 5. Walk each image on the object. We keep two filename forms:
                #    `source_filename` for LUNA matching, and canonical
                #    `filename` for the ERIC `file` identifier we store/export.
                for img in images.values():
                    source_filename = normalise_filename(img.get("name"))
                    if not source_filename:
                        continue
                    filename = canonical_file_identifier(source_filename)

                    image_key = (arch_uuid, filename)
                    existing_recent_row = existing_recent_rows.get(image_key)
                    existing_luna = normalise_filename(existing_recent_row.get("luna")) if existing_recent_row else ""
                    if existing_recent_row:
                        if not existing_luna and image_key in missing_image_keys:
                            skipped_images += 1
                            log(
                                f"  Skipping previously processed image {filename} on Archipelago record {arch_uuid}.",
                                verbose,
                            )
                            continue

                        if existing_luna:
                            expected_tokens = {
                                route["token"]
                                for route in tinyurl_routes.get(existing_luna, [])
                            }
                            existing_tokens = existing_tinyurl_map.get((arch_uuid, filename, existing_luna), set())
                            if expected_tokens.issubset(existing_tokens):
                                skipped_images += 1
                                log(
                                    f"  Skipping previously processed image {filename} on Archipelago record {arch_uuid}.",
                                    verbose,
                                )
                                continue

                    try:
                        # 6. Query LUNA using the original source filename, not
                        #    the canonicalized ERIC `file` value. This gives the
                        #    matcher the best chance of hitting Repro Record ID
                        #    or Repro Link ID in whatever shape LUNA stored it.
                        luna_identifier, attempted_queries = fetch_luna_identifier(
                            session,
                            source_filename,
                            luna_cache,
                            verbose=verbose,
                        )
                    except Exception as exc:
                        print(f"Warning: Could not fetch LUNA ID for {source_filename}: {exc}")
                        luna_identifier = None
                        attempted_queries = []

                    if not luna_identifier and existing_luna:
                        luna_identifier = existing_luna

                    mapped_cantaloupe = cantaloupe_data.get(filename, {})
                    cantaloupe_identifier = extract_cantaloupe_identifier_from_image(img, source_filename)
                    if not cantaloupe_identifier and filename != source_filename:
                        cantaloupe_identifier = extract_cantaloupe_identifier_from_image(img, filename)
                    if not cantaloupe_identifier:
                        cantaloupe_identifier = mapped_cantaloupe.get("cantaloupe")
                    if not cantaloupe_identifier:
                        print(
                            f"Warning: Could not find Cantaloupe ID for {filename} "
                            f"on Archipelago record {arch_uuid}"
                        )
                        if args.debug_cantaloupe and not cantaloupe_debug_dumped:
                            print("Debug: sample as:image payload follows:", flush=True)
                            print(json.dumps(img, indent=2, sort_keys=True)[:4000], flush=True)
                            cantaloupe_debug_dumped = True

                    if not luna_identifier:
                        attempted_query_text = " | ".join(unique_nonempty(attempted_queries))
                        print(
                            f"Warning: Could not resolve LUNA ID for {filename} "
                            f"on Archipelago record {arch_uuid}. Tried: {attempted_query_text or 'no queries recorded'}"
                        )
                        if not existing_recent_row:
                            recent_writer.writerow(
                                {
                                    "object_type": "Image",
                                    "luna": "",
                                    "arch": arch_uuid,
                                    "file": filename,
                                    "cantaloupe": cantaloupe_identifier or "",
                                    "source_created_at": created,
                                    "width": "",
                                    "height": "",
                                }
                            )
                            recent_handle.flush()
                            existing_recent_rows[image_key] = {
                                "object_type": "Image",
                                "luna": "",
                                "arch": arch_uuid,
                                "file": filename,
                                "cantaloupe": cantaloupe_identifier or "",
                                "source_created_at": created,
                                "width": "",
                                "height": "",
                            }
                            recent_written += 1
                        if image_key not in missing_image_keys:
                            missing_writer.writerow(
                                {
                                    "arch": arch_uuid,
                                    "file": filename,
                                    "attempted_queries": attempted_query_text,
                                }
                            )
                            missing_handle.flush()
                            missing_image_keys.add(image_key)
                            missing_luna_written += 1
                        continue

                    # 7. Write the resolved image row, then expand any known
                    #    tinyurl routes for that LUNA identifier into the
                    #    companion CSV used by route ingest.
                    if not existing_recent_row:
                        recent_writer.writerow(
                            {
                                "object_type": "Image",
                                "luna": luna_identifier,
                                "arch": arch_uuid,
                                "file": filename,
                                "cantaloupe": cantaloupe_identifier or "",
                                "source_created_at": created,
                                "width": "",
                                "height": "",
                            }
                        )
                        recent_handle.flush()
                        existing_recent_rows[image_key] = {
                            "object_type": "Image",
                            "luna": luna_identifier,
                            "arch": arch_uuid,
                            "file": filename,
                            "cantaloupe": cantaloupe_identifier or "",
                            "source_created_at": created,
                            "width": "",
                            "height": "",
                        }
                        recent_written += 1

                    for route in tinyurl_routes.get(luna_identifier, []):
                        existing_tokens = existing_tinyurl_map[(arch_uuid, filename, luna_identifier)]
                        if route["token"] in existing_tokens:
                            continue

                        tinyurl_writer.writerow(
                            {
                                "file": filename,
                                "luna": luna_identifier,
                                "arch": arch_uuid,
                                "token": route["token"],
                                "route_type": route["route_type"],
                                "target_url": route["target_url"],
                            }
                        )
                        tinyurl_handle.flush()
                        existing_tokens.add(route["token"])
                        tinyurl_written += 1

            # 8. Only advance the crawl checkpoint after the full source page
            #    has been processed, so resume restarts cleanly at page level.
            page_offset += args.page_limit
            page_number += 1
            save_checkpoint(
                args.checkpoint,
                {
                    "stage": "crawl",
                    "page_offset": page_offset,
                    "page_number": page_number,
                    "page_limit": args.page_limit,
                    "sort": JSON_API_SORT,
                },
            )
    finally:
        recent_handle.close()
        tinyurl_handle.close()
        missing_handle.close()

    print(
        f"Done! Wrote {recent_written} new rows to {RECENT_ITEMS_CSV} "
        f"(total rows now {count_csv_rows(RECENT_ITEMS_CSV)}; skipped existing {skipped_images})"
    )
    print(
        f"Done! Wrote {tinyurl_written} new rows to {RECENT_TINYURLS_CSV} "
        f"(total rows now {count_csv_rows(RECENT_TINYURLS_CSV)}; skipped existing {skipped_images})"
    )
    print(
        f"Done! Wrote {missing_luna_written} new rows to {MISSING_LUNA_CSV} "
        f"(total rows now {count_csv_rows(MISSING_LUNA_CSV)}; skipped existing {skipped_images})"
    )


if __name__ == "__main__":
    main()
