import argparse
import uuid
from collections import Counter
from datetime import datetime, timezone

import bootstrap
from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload

from app import app, db, Identifier, IdentifierType, Object, ObjectType, mint_ark
from project_paths import BACKFILL_CHECKPOINT_JSON
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    build_session,
    canonical_file_identifier,
    extract_cantaloupe_identifier_from_image,
    fetch_cantaloupe_data,
    fetch_luna_identifier,
    fetch_source_page,
    load_crawl_checkpoint,
    log,
    normalise_filename,
    save_checkpoint,
)


MANAGED_IDENTIFIER_SHORTCODES = ("file", "arch", "luna", "cantaloupe")


def normalise_identifier_value(value):
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def parse_source_created_at(value):
    value = normalise_identifier_value(value)
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def preferred_primary_id(row):
    return (
        normalise_identifier_value(row.get("luna"))
        or normalise_identifier_value(row.get("arch"))
        or normalise_identifier_value(row.get("file"))
        or normalise_identifier_value(row.get("cantaloupe"))
    )


def get_identifier(obj, shortcode):
    for ident in obj.identifiers:
        if ident.type.shortcode == shortcode:
            return ident
    return None


def get_identifiers(obj, shortcode):
    return [ident for ident in obj.identifiers if ident.type.shortcode == shortcode]


def choose_canonical_object(matches):
    if not matches:
        return None

    priority = {"file": 0, "arch": 1, "luna": 2, "cantaloupe": 3}
    matches = sorted(matches, key=lambda item: priority.get(item[0], 99))
    return matches[0][1]


def create_object_for_row(row, object_types, counters):
    obj_type = object_types.get(row["object_type"])
    if not obj_type:
        raise ValueError(f"ObjectType {row['object_type']!r} does not exist in DB.")

    obj = Object(
        uuid=uuid.uuid4(),
        type_id=obj_type.id,
        primary_id=preferred_primary_id(row),
        source_created_at=parse_source_created_at(row.get("source_created_at")),
    )
    db.session.add(obj)
    db.session.flush()
    counters["inserted_objects"] += 1
    return obj


def sync_object_metadata(obj, row, object_types, counters):
    obj_type = object_types.get(row["object_type"])
    if not obj_type:
        raise ValueError(f"ObjectType {row['object_type']!r} does not exist in DB.")

    if obj.type_id != obj_type.id:
        obj.type_id = obj_type.id
        counters["updated_objects"] += 1

    desired_primary_id = preferred_primary_id(row)
    if desired_primary_id and obj.primary_id != desired_primary_id:
        obj.primary_id = desired_primary_id
        counters["updated_objects"] += 1

    desired_source_created_at = parse_source_created_at(row.get("source_created_at"))
    if desired_source_created_at and obj.source_created_at != desired_source_created_at:
        obj.source_created_at = desired_source_created_at
        counters["updated_objects"] += 1


def merge_objects(target, source, counters):
    if target.id == source.id:
        return

    for ident in list(source.identifiers):
        target_same_value = next((i for i in target.identifiers if i.value == ident.value), None)
        if target_same_value:
            db.session.delete(ident)
            counters["deleted_identifiers"] += 1
            continue

        target_same_shortcode = next(
            (i for i in target.identifiers if i.type.shortcode == ident.type.shortcode),
            None,
        )
        if target_same_shortcode and ident.type.shortcode in MANAGED_IDENTIFIER_SHORTCODES + ("ark",):
            db.session.delete(ident)
            counters["deleted_identifiers"] += 1
            continue

        ident.object = target

    db.session.flush()
    db.session.delete(source)
    counters["merged_objects"] += 1
    counters["deleted_objects"] += 1


def describe_object_for_log(obj):
    file_ident = get_identifier(obj, "file")
    luna_ident = get_identifier(obj, "luna")
    arch_ident = get_identifier(obj, "arch")
    return (
        f"object_id={obj.id} "
        f"file={file_ident.value if file_ident else '-'} "
        f"luna={luna_ident.value if luna_ident else '-'} "
        f"arch={arch_ident.value if arch_ident else '-'}"
    )


def reconcile_identifier(
    obj,
    shortcode,
    desired_value,
    identifier_types,
    counters,
    arch_uuid_changes=None,
):
    if not desired_value:
        return

    id_type = identifier_types.get(shortcode)
    if not id_type:
        raise ValueError(f"IdentifierType {shortcode!r} does not exist in DB.")

    current_identifiers = get_identifiers(obj, shortcode)
    matching_identifier = next(
        (ident for ident in current_identifiers if ident.value == desired_value),
        None,
    )

    if matching_identifier:
        for ident in current_identifiers:
            if ident.id != matching_identifier.id:
                db.session.delete(ident)
                counters["deleted_identifiers"] += 1
        return

    conflict = Identifier.query.filter_by(value=desired_value).first()
    if conflict and conflict.object_id != obj.id:
        raise ValueError(
            f"Identifier {desired_value!r} already belongs to object_id={conflict.object_id}."
        )

    if shortcode == "arch" and current_identifiers and arch_uuid_changes is not None:
        previous_values = sorted(
            {
                ident.value
                for ident in current_identifiers
                if ident.value and ident.value != desired_value
            }
        )
        if previous_values:
            arch_uuid_changes.append(
                {
                    "object_id": obj.id,
                    "file": next(
                        (ident.value for ident in obj.identifiers if ident.type.shortcode == "file"),
                        None,
                    ),
                    "old_arch_values": previous_values,
                    "new_arch_value": desired_value,
                }
            )
            counters["changed_arch_uuids"] += 1
            print(
                "Arch UUID change detected: "
                f"{describe_object_for_log(obj)} -> new_arch={desired_value}"
            )

    for ident in current_identifiers:
        db.session.delete(ident)
        counters["deleted_identifiers"] += 1

    db.session.add(
        Identifier(
            value=desired_value,
            object_id=obj.id,
            type_id=id_type.id,
        )
    )
    counters["inserted_identifiers"] += 1


def ensure_ark_identifier(obj, identifier_types, counters):
    ark_type = identifier_types.get("ark")
    if not ark_type:
        raise ValueError("IdentifierType 'ark' does not exist in DB.")

    existing_ark = get_identifier(obj, "ark")
    if existing_ark:
        extra_arks = [ident for ident in get_identifiers(obj, "ark") if ident.id != existing_ark.id]
        for ident in extra_arks:
            db.session.delete(ident)
            counters["deleted_identifiers"] += 1
        return

    db.session.add(
        Identifier(
            value=mint_ark(),
            object_id=obj.id,
            type_id=ark_type.id,
        )
    )
    counters["inserted_arks"] += 1


def delete_object_and_identifiers(obj, counters):
    for ident in list(obj.identifiers):
        db.session.delete(ident)
        counters["deleted_identifiers"] += 1

    db.session.flush()
    db.session.delete(obj)
    counters["deleted_objects"] += 1


def prune_missing_files(seen_files, counters):
    objects = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .all()
    )

    for obj in objects:
        file_identifiers = [
            canonical_file_identifier(ident.value).lower()
            for ident in obj.identifiers
            if ident.type.shortcode == "file" and ident.value
        ]
        if not file_identifiers:
            continue

        if any(file_value in seen_files for file_value in file_identifiers):
            continue

        print(
            "Pruning object "
            f"object_id={obj.id} file={', '.join(sorted(set(file_identifiers)))} "
            "because it no longer appears in the live sweep."
        )
        delete_object_and_identifiers(obj, counters)
        counters["pruned_files"] += 1


def build_object_index(rows, identifier_types, shortcodes):
    clauses = []

    for shortcode in shortcodes:
        values = {
            normalise_identifier_value(row.get(shortcode))
            for row in rows
            if row.get(shortcode)
        }
        if not values:
            continue

        id_type = identifier_types.get(shortcode)
        if not id_type:
            raise ValueError(f"IdentifierType {shortcode!r} does not exist in DB.")

        clauses.append(and_(Identifier.type_id == id_type.id, Identifier.value.in_(values)))

    if not clauses:
        return {}

    identifiers = (
        Identifier.query
        .options(
            joinedload(Identifier.type),
            joinedload(Identifier.object).joinedload(Object.identifiers).joinedload(Identifier.type),
        )
        .filter(or_(*clauses))
        .all()
    )

    return {
        (ident.type.shortcode, ident.value): ident.object
        for ident in identifiers
    }


def find_matching_objects_from_index(row, object_index, shortcodes):
    matches = []
    seen_object_ids = set()

    for shortcode in shortcodes:
        value = row.get(shortcode)
        if not value:
            continue

        obj = object_index.get((shortcode, value))
        if obj and obj.id not in seen_object_ids:
            matches.append((shortcode, obj))
            seen_object_ids.add(obj.id)

    return matches


def add_page_row(page_rows_by_file, row, counters, newest_first):
    file_key = row["file"].lower()
    existing = page_rows_by_file.get(file_key)
    if existing:
        counters["duplicate_rows"] += 1
        if newest_first:
            return

        if existing != row:
            print(
                f"Duplicate live sweep row for {row['file']}: "
                f"arch {existing.get('arch')} -> {row.get('arch')}. Keeping the later row."
            )

    page_rows_by_file[file_key] = row


def page_has_older_records(raw_objects, created_since):
    if created_since is None:
        return False

    created_values = []
    for obj in raw_objects:
        created_value = obj.get("attributes", {}).get("created")
        created_dt = parse_source_created_at(created_value)
        if created_dt is not None:
            created_values.append(created_dt)

    if not created_values:
        return False

    return min(created_values) < created_since


def collect_page_rows(
    source_rows,
    session,
    cantaloupe_cache,
    counters,
    verbose,
    newest_first,
    created_since,
    seen_files,
    processed_files,
):
    page_rows_by_file = {}

    for row_index, source_row in enumerate(source_rows, start=1):
        arch_uuid = source_row["arch"]
        metadata = source_row["metadata"]
        created = source_row.get("created") or "unknown"
        created_dt = parse_source_created_at(created)
        if created_since and created_dt and created_dt < created_since:
            counters["skipped_old_source_rows"] += 1
            continue

        images = metadata.get("as:image", {})
        image_total = len(images) if isinstance(images, dict) else metadata.get("images", 0)
        log(
            f"Sweeping row={row_index}/{len(source_rows)}: "
            f"arch={arch_uuid}, created={created}, images={image_total}",
            verbose,
        )
        if not isinstance(images, dict):
            continue

        cantaloupe_data = fetch_cantaloupe_data(
            session,
            arch_uuid,
            cantaloupe_cache,
            verbose=verbose,
        )

        for img in images.values():
            source_filename = normalise_filename(img.get("name"))
            if not source_filename:
                continue

            filename = canonical_file_identifier(source_filename)
            file_key = filename.lower()
            counters["swept_images"] += 1

            if seen_files is not None:
                seen_files.add(file_key)

            if newest_first and processed_files is not None and file_key in processed_files:
                counters["duplicate_rows"] += 1
                continue

            mapped_cantaloupe = cantaloupe_data.get(filename, {})
            cantaloupe_identifier = extract_cantaloupe_identifier_from_image(img, source_filename)
            if not cantaloupe_identifier and filename != source_filename:
                cantaloupe_identifier = extract_cantaloupe_identifier_from_image(img, filename)
            if not cantaloupe_identifier:
                cantaloupe_identifier = mapped_cantaloupe.get("cantaloupe")

            if not cantaloupe_identifier:
                counters["missing_cantaloupe_rows"] += 1
                print(
                    f"Warning: Could not find Cantaloupe ID for {filename} "
                    f"on Archipelago record {arch_uuid}"
                )

            add_page_row(
                page_rows_by_file,
                {
                    "object_type": "Image",
                    "source_filename": source_filename,
                    "luna": None,
                    "arch": normalise_identifier_value(arch_uuid),
                    "file": filename,
                    "cantaloupe": normalise_identifier_value(cantaloupe_identifier),
                    "source_created_at": normalise_identifier_value(created),
                },
                counters,
                newest_first=newest_first,
            )

    if newest_first and processed_files is not None:
        processed_files.update(page_rows_by_file)

    return list(page_rows_by_file.values())


def populate_luna_identifiers(page_rows, object_index, session, luna_cache, counters, verbose):
    rows_needing_lookup = []

    for row in page_rows:
        matches = find_matching_objects_from_index(
            row,
            object_index,
            shortcodes=("file", "arch", "cantaloupe"),
        )
        obj = choose_canonical_object(matches)
        existing_luna = get_identifier(obj, "luna") if obj else None
        if existing_luna and existing_luna.value:
            row["luna"] = existing_luna.value
            counters["reused_luna"] += 1
            continue

        rows_needing_lookup.append(row)

    if not rows_needing_lookup:
        return

    for row in rows_needing_lookup:
        try:
            counters["queried_luna"] += 1
            luna_identifier, attempted_queries = fetch_luna_identifier(
                session,
                row["source_filename"],
                luna_cache,
                verbose=verbose,
            )
        except Exception as exc:
            print(f"Warning: Could not fetch LUNA ID for {row['source_filename']}: {exc}")
            luna_identifier = None
            attempted_queries = []

        row["luna"] = normalise_identifier_value(luna_identifier)
        if not row["luna"]:
            counters["missing_luna_rows"] += 1
            attempted_text = " | ".join(attempted_queries) or "no queries recorded"
            print(
                f"Warning: Could not resolve LUNA ID for {row['file']} "
                f"on Archipelago record {row['arch']}. Tried: {attempted_text}"
            )


def process_page_rows(
    page_rows,
    session,
    luna_cache,
    identifier_types,
    object_types,
    counters,
    verbose,
    dry_run=False,
    load_missing_only=False,
    arch_uuid_changes=None,
):
    if not page_rows:
        return

    initial_index = build_object_index(
        page_rows,
        identifier_types,
        shortcodes=("file", "arch", "cantaloupe"),
    )
    rows_to_consider = []
    if load_missing_only:
        for row in page_rows:
            matches = find_matching_objects_from_index(
                row,
                initial_index,
                shortcodes=("file", "arch", "cantaloupe"),
            )
            if matches:
                counters["skipped_existing_rows"] += 1
                continue
            rows_to_consider.append(row)
    else:
        rows_to_consider = list(page_rows)

    populate_luna_identifiers(
        rows_to_consider,
        initial_index,
        session,
        luna_cache,
        counters,
        verbose,
    )
    luna_index = build_object_index(
        rows_to_consider,
        identifier_types,
        shortcodes=("luna",),
    )

    for row in rows_to_consider:
        counters["processed_rows"] += 1
        savepoint = db.session.begin_nested()
        try:
            matches = find_matching_objects_from_index(
                row,
                initial_index,
                shortcodes=("file", "arch", "cantaloupe"),
            )
            matches.extend(
                find_matching_objects_from_index(
                    row,
                    luna_index,
                    shortcodes=("luna",),
                )
            )

            deduped_matches = []
            seen_object_ids = set()
            for shortcode, obj in matches:
                if obj.id in seen_object_ids:
                    continue
                deduped_matches.append((shortcode, obj))
                seen_object_ids.add(obj.id)

            obj = choose_canonical_object(deduped_matches)

            if load_missing_only and obj is not None:
                counters["skipped_existing_rows"] += 1
                savepoint.commit()
                continue

            if obj is None:
                obj = create_object_for_row(row, object_types, counters)
            else:
                duplicate_objects = [match_obj for _, match_obj in deduped_matches if match_obj.id != obj.id]
                for duplicate in duplicate_objects:
                    merge_objects(obj, duplicate, counters)

            sync_object_metadata(obj, row, object_types, counters)

            for shortcode in MANAGED_IDENTIFIER_SHORTCODES:
                reconcile_identifier(
                    obj,
                    shortcode,
                    row.get(shortcode),
                    identifier_types,
                    counters,
                    arch_uuid_changes=arch_uuid_changes,
                )

            ensure_ark_identifier(obj, identifier_types, counters)
            db.session.flush()
            savepoint.commit()
        except Exception as exc:
            savepoint.rollback()
            counters["errors"] += 1
            print(
                f"Error while syncing file={row.get('file')} arch={row.get('arch')}: {exc}"
            )

    if not dry_run:
        db.session.commit()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sweep Archipelago directly and reconcile the ERIC database."
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=50,
        help="JSON:API page size for the Archipelago sweep.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Stop after this many Archipelago pages for debugging or timing tests.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress logging.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the saved backfill checkpoint.",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Start the crawl from this JSON:API offset instead of zero.",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=None,
        help="Start the crawl from this 1-based page number instead of offset zero.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(BACKFILL_CHECKPOINT_JSON),
        help="Path to the JSON checkpoint file used for resumable runs.",
    )
    parser.add_argument(
        "--prune-missing-files",
        action="store_true",
        help=(
            "Delete objects whose file identifier no longer appears in the live sweep. "
            "Use this only with a known-complete run."
        ),
    )
    parser.add_argument(
        "--newest-first",
        action="store_true",
        help="Crawl newest-first instead of oldest-first.",
    )
    parser.add_argument(
        "--created-since",
        default=None,
        help=(
            "Only process Archipelago records created on or after this ISO date/time. "
            "Best used with newest-first recent sweeps."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate and print the changes, then roll them back instead of committing.",
    )
    parser.add_argument(
        "--load-missing-only",
        action="store_true",
        help=(
            "Only insert rows that are not already present in ERIC. "
            "Skip object updates, merges, and identifier reconciliation for existing matches."
        ),
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
    created_since = parse_source_created_at(args.created_since)
    if args.created_since and created_since is None:
        raise SystemExit("--created-since must be a valid ISO date or datetime.")

    if created_since and not args.newest_first:
        args.newest_first = True
        log("Enabling newest-first crawl because --created-since was provided.", verbose)

    if args.prune_missing_files and args.max_pages is not None:
        raise SystemExit("Do not combine --prune-missing-files with --max-pages.")
    if args.prune_missing_files and created_since is not None:
        raise SystemExit("Do not combine --prune-missing-files with --created-since.")
    if args.resume and created_since is not None:
        raise SystemExit("Do not combine --resume with --created-since.")
    if args.resume and args.start_page is not None:
        raise SystemExit("Do not combine --resume with --start-page.")
    if args.resume and args.start_offset:
        raise SystemExit("Do not combine --resume with --start-offset.")
    if args.start_page is not None and args.start_offset:
        raise SystemExit("Use only one of --start-page or --start-offset.")
    if args.start_page is not None and args.start_page < 1:
        raise SystemExit("--start-page must be 1 or greater.")
    if args.start_offset < 0:
        raise SystemExit("--start-offset must be 0 or greater.")

    sort = "-created" if args.newest_first else "created"
    session = build_session(
        http_retries=args.http_retries,
        retry_backoff=args.retry_backoff,
    )
    luna_cache = {}
    cantaloupe_cache = {}
    seen_files = set() if args.prune_missing_files else None
    processed_files = set() if args.newest_first else None
    page_offset, page_number = load_crawl_checkpoint(
        args.checkpoint,
        args.page_limit,
        args.resume,
        sort=sort,
    )
    if not args.resume:
        if args.start_page is not None:
            page_number = args.start_page - 1
            page_offset = (args.start_page - 1) * args.page_limit
        else:
            page_offset = args.start_offset
            page_number = page_offset // args.page_limit if args.page_limit else 0

    if args.resume and (page_offset or page_number):
        log(
            f"Resuming backfill crawl from page {page_number + 1} "
            f"(offset={page_offset}, sort={sort}).",
            verbose,
        )
    elif page_offset or page_number:
        log(
            f"Starting backfill crawl at page {page_number + 1} "
            f"(offset={page_offset}, sort={sort}).",
            verbose,
        )

    with app.app_context():
        counters = Counter()
        arch_uuid_changes = []
        pages_processed = 0
        identifier_types = {row.shortcode: row for row in IdentifierType.query.all()}
        object_types = {row.name: row for row in ObjectType.query.all()}

        while True:
            if args.max_pages is not None and pages_processed >= args.max_pages:
                log(f"Stopping early after {pages_processed} page(s) due to --max-pages.", verbose)
                break

            raw_objects, source_rows = fetch_source_page(
                session,
                page_offset,
                args.page_limit,
                sort=sort,
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
                        "sort": sort,
                    },
                )
                break

            page_rows = collect_page_rows(
                source_rows,
                session,
                cantaloupe_cache,
                counters,
                verbose,
                newest_first=args.newest_first,
                created_since=created_since,
                seen_files=seen_files,
                processed_files=processed_files,
            )
            process_page_rows(
                page_rows,
                session,
                luna_cache,
                identifier_types,
                object_types,
                counters,
                verbose,
                dry_run=args.dry_run,
                load_missing_only=args.load_missing_only,
                arch_uuid_changes=arch_uuid_changes,
            )

            page_offset += args.page_limit
            page_number += 1
            pages_processed += 1
            save_checkpoint(
                args.checkpoint,
                {
                    "stage": "crawl",
                    "page_offset": page_offset,
                    "page_number": page_number,
                    "page_limit": args.page_limit,
                    "sort": sort,
                },
            )

            if created_since is not None and page_has_older_records(raw_objects, created_since):
                log("Stopping because the crawl has moved past --created-since.", verbose)
                break

        if args.prune_missing_files:
            savepoint = db.session.begin_nested()
            try:
                prune_missing_files(seen_files or set(), counters)
                db.session.flush()
                savepoint.commit()
                if not args.dry_run:
                    db.session.commit()
            except Exception as exc:
                savepoint.rollback()
                counters["errors"] += 1
                print(f"Error while pruning files missing from the sweep: {exc}")

        if args.dry_run:
            db.session.rollback()
            print("Dry run only: rolled back all database changes.")

        if arch_uuid_changes:
            print(
                "Arch UUID changes detected during backfill:"
            )
            for change in arch_uuid_changes:
                old_values = ", ".join(change["old_arch_values"])
                print(
                    f"  object_id={change['object_id']} "
                    f"file={change.get('file') or '-'} "
                    f"old_arch={old_values} "
                    f"new_arch={change['new_arch_value']}"
                )

        print(
            "Backfill sync complete. "
            f"swept_images={counters['swept_images']} "
            f"processed_rows={counters['processed_rows']} "
            f"inserted_objects={counters['inserted_objects']} "
            f"updated_objects={counters['updated_objects']} "
            f"merged_objects={counters['merged_objects']} "
            f"inserted_identifiers={counters['inserted_identifiers']} "
            f"inserted_arks={counters['inserted_arks']} "
            f"deleted_identifiers={counters['deleted_identifiers']} "
            f"deleted_objects={counters['deleted_objects']} "
            f"pruned_files={counters['pruned_files']} "
            f"duplicate_rows={counters['duplicate_rows']} "
            f"reused_luna={counters['reused_luna']} "
            f"queried_luna={counters['queried_luna']} "
            f"missing_luna_rows={counters['missing_luna_rows']} "
            f"missing_cantaloupe_rows={counters['missing_cantaloupe_rows']} "
            f"skipped_old_source_rows={counters['skipped_old_source_rows']} "
            f"skipped_existing_rows={counters['skipped_existing_rows']} "
            f"changed_arch_uuids={counters['changed_arch_uuids']} "
            f"errors={counters['errors']}"
        )


if __name__ == "__main__":
    main()
