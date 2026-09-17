#!/usr/bin/env python3
"""Create missing ERIC image Objects and repair reloaded Archipelago mappings.

Unlike the legacy backfill this never queries LUNA and never fetches every
public Archipelago page. It uses the JSON:API image metadata, then only writes
records whose Archipelago UUID is absent or whose filename/Cantaloupe ID links
them to an existing ERIC Object.
"""

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin

import bootstrap

from app import Identifier, IdentifierType, Object, ObjectType, SyncState, app, db, mint_ark
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES, DEFAULT_RETRY_BACKOFF, REQUEST_TIMEOUT, build_session,
    canonical_file_identifier, extract_cantaloupe_identifier_from_image, log,
    normalise_filename,
)


DEFAULT_JSONAPI_ROOT = "http://lac-dams-live2.is.ed.ac.uk/jsonapi"
SYNC_JOB_NAME = "archipelago_object_reconciliation"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonapi-root", default=os.environ.get("ARCHIPELAGO_JSONAPI_ROOT", DEFAULT_JSONAPI_ROOT))
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--http-retries", type=int, default=DEFAULT_HTTP_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_metadata(attributes):
    field = attributes.get("field_descriptive_metadata")
    raw = field.get("value") if isinstance(field, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def iter_source_pages(session, root, page_limit, max_pages, timeout, verbose):
    endpoint = f"{root.rstrip('/')}/node/digital_object"
    query = urlencode({
        "fields[node--digital_object]": "drupal_internal__nid,created,field_descriptive_metadata",
        "sort": "drupal_internal__nid", "page[limit]": page_limit,
    })
    next_url = f"{endpoint}?{query}"
    page = 0
    while next_url:
        page += 1
        if max_pages is not None and page > max_pages:
            return
        log(f"Fetching Digital Object page {page}: {next_url}", verbose)
        response = session.get(next_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        records = [item for item in payload.get("data", []) if isinstance(item, dict)]
        yield page, records
        next_link = payload.get("links", {}).get("next", {})
        href = next_link.get("href") if isinstance(next_link, dict) else None
        next_url = urljoin(endpoint, href) if isinstance(href, str) and href else None


def source_row(record):
    attributes = record.get("attributes") or {}
    source_uuid = str(record.get("id") or "").strip()
    try:
        source_nid = int(attributes.get("drupal_internal__nid"))
    except (TypeError, ValueError):
        return None
    metadata = parse_metadata(attributes)
    images = metadata.get("as:image")
    entries = list(images.values()) if isinstance(images, dict) else []
    if len(entries) != 1 or not isinstance(entries[0], dict):
        return None
    image = entries[0]
    source_filename = normalise_filename(image.get("name"))
    if not source_uuid or not source_filename:
        return None
    filename = canonical_file_identifier(source_filename)
    cantaloupe = extract_cantaloupe_identifier_from_image(image, source_filename)
    return {
        "arch": source_uuid,
        "arch_nid": str(source_nid),
        "file": filename,
        "cantaloupe": str(cantaloupe).strip() if cantaloupe else None,
        "source_created_at": attributes.get("created"),
    }


def identifiers_by_values(identifier_type, values):
    values = {value for value in values if value}
    if not values:
        return {}
    return {
        value: obj for value, obj in db.session.query(Identifier.value, Object)
        .join(Object, Object.id == Identifier.object_id)
        .filter(Identifier.type_id == identifier_type.id)
        .filter(Identifier.value.in_(values)).all()
    }


def get_type(shortcode):
    identifier_type = IdentifierType.query.filter_by(shortcode=shortcode).first()
    if identifier_type is None:
        raise RuntimeError(f"Missing IdentifierType {shortcode!r}")
    return identifier_type


def set_identifier(obj, identifier_type, value, counters, replace=False):
    if not value:
        return None
    value = str(value).strip()
    current = [item for item in obj.identifiers if item.type_id == identifier_type.id]
    found = next((item for item in current if item.value == value), None)
    if found:
        if replace:
            for item in current:
                if item.id != found.id:
                    db.session.delete(item)
                    counters["removed_identifiers"] += 1
        return found
    owner = Identifier.query.filter_by(type_id=identifier_type.id, value=value).first()
    if owner and owner.object_id != obj.id:
        raise RuntimeError(
            f"{identifier_type.shortcode} value {value!r} belongs to object_id={owner.object_id}"
        )
    if owner:
        return owner
    if replace:
        for item in current:
            db.session.delete(item)
            counters["removed_identifiers"] += 1
    identifier = Identifier(value=value, object_id=obj.id, type_id=identifier_type.id)
    db.session.add(identifier)
    db.session.flush()
    counters["created_identifiers"] += 1
    return identifier


def choose_existing(row, by_file, by_cantaloupe):
    file_match = by_file.get(row["file"])
    cantaloupe_match = by_cantaloupe.get(row["cantaloupe"])
    if file_match and cantaloupe_match and file_match.id != cantaloupe_match.id:
        raise RuntimeError(
            f"filename maps to object_id={file_match.id}, but Cantaloupe ID maps to object_id={cantaloupe_match.id}"
        )
    return file_match or cantaloupe_match


def create_object(image_type, types, row, counters):
    obj = Object(type_id=image_type.id, uuid=uuid.uuid4(), primary_id=None)
    db.session.add(obj)
    db.session.flush()
    ark = set_identifier(obj, types["ark"], mint_ark(), counters)
    obj.primary_id = ark.value
    counters["created_objects"] += 1
    return obj


def sync_row(row, by_arch, by_file, by_cantaloupe, image_type, types, counters):
    obj = by_arch.get(row["arch"])
    if obj:
        set_identifier(obj, types["arch_nid"], row["arch_nid"], counters, replace=True)
        counters["already_current"] += 1
        return
    obj = choose_existing(row, by_file, by_cantaloupe)
    if obj is None:
        obj = create_object(image_type, types, row, counters)
    else:
        counters["relinked_objects"] += 1

    set_identifier(obj, types["arch"], row["arch"], counters, replace=True)
    set_identifier(obj, types["arch_nid"], row["arch_nid"], counters, replace=True)
    set_identifier(obj, types["file"], row["file"], counters, replace=False)
    if row["cantaloupe"]:
        set_identifier(obj, types["cantaloupe"], row["cantaloupe"], counters, replace=True)
    else:
        counters["missing_cantaloupe"] += 1
    by_arch[row["arch"]] = obj


def details(counters):
    return json.dumps(dict(sorted(counters.items())), sort_keys=True)


def main():
    args = parse_args()
    if args.page_limit < 1:
        raise SystemExit("--page-limit must be at least 1")
    verbose = not args.quiet
    session = build_session(args.http_retries, args.retry_backoff)

    with app.app_context():
        image_type = ObjectType.query.filter_by(name="Image").first()
        if image_type is None:
            raise SystemExit("Missing ObjectType 'Image'")
        types = {key: get_type(key) for key in ("ark", "arch", "arch_nid", "file", "cantaloupe")}
        state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
        if state is None:
            state = SyncState(job_name=SYNC_JOB_NAME)
            db.session.add(state)
        counters = Counter()
        if not args.dry_run:
            state.status = "running"
            state.details_json = None
            db.session.commit()
        try:
            for page, records in iter_source_pages(session, args.jsonapi_root, args.page_limit, args.max_pages, args.timeout, verbose):
                rows = [row for row in (source_row(record) for record in records) if row]
                counters["pages"] += 1
                counters["source_records"] += len(records)
                counters["usable_records"] += len(rows)
                by_arch = identifiers_by_values(types["arch"], {row["arch"] for row in rows})
                by_file = identifiers_by_values(types["file"], {row["file"] for row in rows if row["arch"] not in by_arch})
                by_cantaloupe = identifiers_by_values(types["cantaloupe"], {row["cantaloupe"] for row in rows if row["arch"] not in by_arch})
                for row in rows:
                    savepoint = db.session.begin_nested()
                    try:
                        sync_row(row, by_arch, by_file, by_cantaloupe, image_type, types, counters)
                        db.session.flush()
                        savepoint.commit()
                    except Exception as exc:
                        savepoint.rollback()
                        counters["errors"] += 1
                        print(f"Skipping Archipelago UUID {row['arch']}: {exc}", file=sys.stderr)
            if args.dry_run:
                db.session.rollback()
                print("Dry run only: rolled back all database changes.")
            else:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).one()
                state.status = "partial" if args.max_pages is not None else "complete"
                if args.max_pages is None:
                    state.last_completed_at = utcnow()
                state.details_json = details(counters)
                db.session.commit()
        except Exception as exc:
            db.session.rollback()
            if not args.dry_run:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
                if state is None:
                    state = SyncState(job_name=SYNC_JOB_NAME)
                    db.session.add(state)
                state.status = "failed"
                counters["fatal_errors"] += 1
                state.details_json = details(counters)
                db.session.commit()
            raise SystemExit(f"Object reconciliation failed: {exc}") from exc
        print(
            "Object reconciliation complete. "
            f"pages={counters['pages']} usable_records={counters['usable_records']} "
            f"created_objects={counters['created_objects']} relinked_objects={counters['relinked_objects']} "
            f"already_current={counters['already_current']} missing_cantaloupe={counters['missing_cantaloupe']} "
            f"errors={counters['errors']}"
        )


if __name__ == "__main__":
    main()
