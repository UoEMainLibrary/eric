#!/usr/bin/env python3
"""Populate ordered Collection-to-Digital-Object relationships in ERIC."""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin

import bootstrap

from app import (
    Collection,
    CollectionItem,
    Identifier,
    IdentifierType,
    Object,
    SyncState,
    app,
    db,
)
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    REQUEST_TIMEOUT,
    build_session,
    log,
)


DEFAULT_JSONAPI_ROOT = "http://lac-dams-live2.is.ed.ac.uk/jsonapi"
SYNC_JOB_NAME = "archipelago_collection_items"
PARENT_KEY_CANDIDATES = (
    "ispartof",
    "is_part_of",
    "is part of",
    "part_of",
    "part of",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Synchronise Archipelago Digital Object collection memberships into ERIC."
    )
    parser.add_argument(
        "--jsonapi-root",
        default=os.environ.get("ARCHIPELAGO_JSONAPI_ROOT", DEFAULT_JSONAPI_ROOT),
        help="Archipelago JSON:API root (default: %(default)s)",
    )
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--http-retries", type=int, default=DEFAULT_HTTP_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalise_key(value):
    return " ".join(
        "".join(char if char.isalnum() else " " for char in (value or "").lower()).split()
    )


def parse_metadata(attributes):
    field = attributes.get("field_descriptive_metadata")
    raw_value = field.get("value") if isinstance(field, dict) else None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return {}
    try:
        metadata = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def iter_membership_values(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_membership_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_membership_values(item)
    else:
        yield value


def extract_parent_nids(metadata):
    wanted = {normalise_key(key) for key in PARENT_KEY_CANDIDATES}
    result = []
    seen = set()
    for key, value in metadata.items():
        if normalise_key(str(key)) not in wanted:
            continue
        for candidate in iter_membership_values(value):
            if isinstance(candidate, int):
                nid = candidate
            elif isinstance(candidate, str) and candidate.strip().isdigit():
                nid = int(candidate.strip())
            else:
                continue
            if nid not in seen:
                seen.add(nid)
                result.append(nid)
    return result


def extract_sequence(metadata, fallback_nid):
    raw_value = metadata.get("sequence_id", "")
    if isinstance(raw_value, list) and raw_value:
        raw_value = raw_value[0]
    if isinstance(raw_value, (int, float)):
        return int(raw_value)
    if isinstance(raw_value, str):
        digits = re.findall(r"\d+", raw_value)
        if digits:
            return int(digits[0])
    return fallback_nid


def extract_label(attributes, metadata):
    for key in ("label", "title", "name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            first = next((item for item in value if isinstance(item, str) and item.strip()), None)
            if first:
                return first.strip()
    return str(attributes.get("title") or "").strip()


def iter_digital_objects(session, jsonapi_root, page_limit, max_pages, timeout, verbose):
    endpoint = f"{jsonapi_root.rstrip('/')}/node/digital_object"
    resource_type = "node--digital_object"
    query = urlencode(
        {
            f"fields[{resource_type}]": "drupal_internal__nid,title,field_descriptive_metadata",
            "sort": "drupal_internal__nid",
            "page[limit]": page_limit,
        }
    )
    next_url = f"{endpoint}?{query}"
    page_number = 0

    while next_url:
        page_number += 1
        if max_pages is not None and page_number > max_pages:
            return
        log(f"Fetching Digital Object page {page_number}: {next_url}", verbose)
        response = session.get(next_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        records = [item for item in payload.get("data", []) if isinstance(item, dict)]
        yield page_number, records

        next_link = payload.get("links", {}).get("next", {})
        href = next_link.get("href") if isinstance(next_link, dict) else None
        next_url = urljoin(endpoint, href) if isinstance(href, str) and href else None


def load_collections_by_arch_nid(arch_nid_type):
    rows = (
        db.session.query(Identifier.value, Collection)
        .join(Collection, Collection.object_id == Identifier.object_id)
        .filter(Identifier.type_id == arch_nid_type.id)
        .all()
    )
    result = {}
    for value, collection in rows:
        existing = result.get(value)
        if existing and existing.id != collection.id:
            raise RuntimeError(
                f"Archipelago NID {value!r} is attached to multiple Collections."
            )
        result[value] = collection
    return result


def load_arch_nid_owners(arch_nid_type):
    return {
        value: object_id
        for value, object_id in db.session.query(Identifier.value, Identifier.object_id)
        .filter(Identifier.type_id == arch_nid_type.id)
        .all()
    }


def load_objects_by_arch_uuid(source_uuids, arch_type):
    source_uuids = {value for value in source_uuids if value}
    if not source_uuids:
        return {}
    rows = (
        db.session.query(Identifier.value, Object)
        .join(Object, Object.id == Identifier.object_id)
        .filter(Identifier.type_id == arch_type.id)
        .filter(Identifier.value.in_(source_uuids))
        .all()
    )
    return {value: obj for value, obj in rows}


def ensure_arch_nid(obj, nid, arch_nid_type, arch_nid_owners, counters):
    value = str(nid)
    owner_id = arch_nid_owners.get(value)
    if owner_id is not None:
        if owner_id != obj.id:
            counters["arch_nid_conflicts"] += 1
            print(
                f"Archipelago NID {value} belongs to object_id={owner_id}, "
                f"not child object_id={obj.id}",
                file=sys.stderr,
            )
        return

    db.session.add(Identifier(value=value, object_id=obj.id, type_id=arch_nid_type.id))
    db.session.flush()
    arch_nid_owners[value] = obj.id
    counters["created_arch_nid_identifiers"] += 1


def upsert_collection_item(collection, obj, sequence, label, counters):
    item = CollectionItem.query.filter_by(collection_id=collection.id, object_id=obj.id).first()
    if item is None:
        item = CollectionItem(
            collection_id=collection.id,
            object_id=obj.id,
            sequence=sequence,
            label=label or None,
            first_seen_at=utcnow(),
        )
        db.session.add(item)
        counters["created_collection_items"] += 1
    else:
        item.sequence = sequence
        item.label = label or None
        counters["updated_collection_items"] += 1
    item.last_seen_at = utcnow()


def process_record(
    record,
    objects_by_arch_uuid,
    collections_by_nid,
    arch_nid_type,
    arch_nid_owners,
    counters,
):
    attributes = record.get("attributes") or {}
    source_uuid = str(record.get("id") or "").strip()
    raw_nid = attributes.get("drupal_internal__nid")
    try:
        source_nid = int(raw_nid)
    except (TypeError, ValueError):
        counters["missing_source_nid"] += 1
        return

    metadata = parse_metadata(attributes)
    parent_nids = extract_parent_nids(metadata)
    if not parent_nids:
        counters["without_collection_parent"] += 1
        return

    obj = objects_by_arch_uuid.get(source_uuid)
    if obj is None:
        counters["missing_eric_objects"] += 1
        print(
            f"No ERIC Object has Archipelago UUID {source_uuid} (NID {source_nid}).",
            file=sys.stderr,
        )
        return

    ensure_arch_nid(obj, source_nid, arch_nid_type, arch_nid_owners, counters)
    sequence = extract_sequence(metadata, source_nid)
    label = extract_label(attributes, metadata)
    matched_parent = False
    for parent_nid in parent_nids:
        collection = collections_by_nid.get(str(parent_nid))
        if collection is None:
            counters["missing_collections"] += 1
            print(
                f"No ERIC Collection has Archipelago NID {parent_nid} "
                f"(child {source_uuid}).",
                file=sys.stderr,
            )
            continue
        upsert_collection_item(collection, obj, sequence, label, counters)
        matched_parent = True

    if matched_parent:
        counters["processed_digital_objects"] += 1


def process_page(records, collections_by_nid, arch_type, arch_nid_type, arch_nid_owners, counters):
    source_uuids = {str(record.get("id") or "").strip() for record in records}
    objects_by_arch_uuid = load_objects_by_arch_uuid(source_uuids, arch_type)

    for record in records:
        savepoint = db.session.begin_nested()
        try:
            process_record(
                record,
                objects_by_arch_uuid,
                collections_by_nid,
                arch_nid_type,
                arch_nid_owners,
                counters,
            )
            db.session.flush()
            savepoint.commit()
        except Exception as exc:
            savepoint.rollback()
            counters["record_errors"] += 1
            print(f"Skipping Digital Object {record.get('id')}: {exc}", file=sys.stderr)


def state_details(counters):
    return json.dumps(dict(sorted(counters.items())), sort_keys=True)


def main():
    args = parse_args()
    if args.page_limit < 1:
        raise SystemExit("--page-limit must be at least 1")

    verbose = not args.quiet
    session = build_session(
        http_retries=args.http_retries,
        retry_backoff=args.retry_backoff,
    )

    with app.app_context():
        arch_type = IdentifierType.query.filter_by(shortcode="arch").first()
        arch_nid_type = IdentifierType.query.filter_by(shortcode="arch_nid").first()
        if arch_type is None or arch_nid_type is None:
            raise SystemExit("Missing 'arch' or 'arch_nid' IdentifierType; apply migration first.")

        collections_by_nid = load_collections_by_arch_nid(arch_nid_type)
        arch_nid_owners = load_arch_nid_owners(arch_nid_type)
        counters = Counter()
        state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
        if state is None:
            state = SyncState(job_name=SYNC_JOB_NAME)
            db.session.add(state)

        if not args.dry_run:
            state.status = "running"
            state.details_json = None
            db.session.commit()

        try:
            for page_number, records in iter_digital_objects(
                session,
                args.jsonapi_root,
                args.page_limit,
                args.max_pages,
                args.timeout,
                verbose,
            ):
                counters["pages"] += 1
                try:
                    process_page(
                        records,
                        collections_by_nid,
                        arch_type,
                        arch_nid_type,
                        arch_nid_owners,
                        counters,
                    )
                except Exception as exc:
                    counters["page_errors"] += 1
                    print(f"Skipping page {page_number}: {exc}", file=sys.stderr)

            if args.dry_run:
                db.session.rollback()
                print("Dry run only: rolled back all database changes.")
            else:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).one()
                state.status = "partial" if args.max_pages is not None else "complete"
                if args.max_pages is None:
                    state.last_completed_at = utcnow()
                state.details_json = state_details(counters)
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
                state.details_json = state_details(counters)
                db.session.commit()
            raise SystemExit(f"Collection-item sync failed: {exc}") from exc

        print(
            "Collection-item sync complete. "
            f"pages={counters['pages']} "
            f"processed_digital_objects={counters['processed_digital_objects']} "
            f"created_collection_items={counters['created_collection_items']} "
            f"updated_collection_items={counters['updated_collection_items']} "
            f"created_arch_nid_identifiers={counters['created_arch_nid_identifiers']} "
            f"missing_eric_objects={counters['missing_eric_objects']} "
            f"missing_collections={counters['missing_collections']} "
            f"arch_nid_conflicts={counters['arch_nid_conflicts']} "
            f"record_errors={counters['record_errors']} "
            f"page_errors={counters['page_errors']}"
        )


if __name__ == "__main__":
    main()
