#!/usr/bin/env python3
"""Synchronise Archipelago Digital Object Collections into ERIC.

This is deliberately the first, collection-only harvest.  It creates the
source-neutral ERIC Collection and its stable ARK, then attaches the current
Archipelago collection UUID and Drupal node ID as identifiers.  It does not
create image memberships; that is the responsibility of a later sync job.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin

import bootstrap

from app import (
    Collection,
    CollectionSourceRecord,
    Identifier,
    IdentifierType,
    Object,
    ObjectType,
    SyncState,
    app,
    db,
    mint_ark,
)
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    REQUEST_TIMEOUT,
    build_session,
    log,
)


DEFAULT_JSONAPI_ROOT = "http://lac-dams-live2.is.ed.ac.uk/jsonapi"
SOURCE_SYSTEM = "archipelago"
SYNC_JOB_NAME = "archipelago_collections"
SHELFMARK_KEYS = (
    "shelfmark",
    "work_shelfmark",
    "work shelfmark",
    "call_number",
    "call number",
    "reference",
    "reference_number",
    "archival_reference",
    "archival reference",
    "identifier",
    "signatur",
    "signature",
)


class IdentifierConflict(RuntimeError):
    """An externally supplied identifier is already owned by another Object."""


def utcnow():
    """Return a UTC value suitable for ERIC's naive MariaDB DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Synchronise Archipelago Digital Object Collections into ERIC."
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


def normalise_key(value):
    return " ".join(
        "".join(char if char.isalnum() else " " for char in (value or "").lower()).split()
    )


def normalise_shelfmark(value):
    """Normalise whitespace and case without removing meaningful punctuation."""
    return " ".join((value or "").upper().split())


def iter_scalar_strings(value):
    if isinstance(value, str):
        value = value.strip()
        if value:
            yield value
    elif isinstance(value, (int, float)):
        yield str(value)
    elif isinstance(value, list):
        for item in value:
            yield from iter_scalar_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_scalar_strings(item)


def parse_metadata(attributes):
    field = attributes.get("field_descriptive_metadata")
    raw_value = field.get("value") if isinstance(field, dict) else None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return {}
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def first_metadata_value(metadata, keys):
    wanted = {normalise_key(key) for key in keys}
    for key, value in metadata.items():
        if normalise_key(str(key)) not in wanted:
            continue
        for candidate in iter_scalar_strings(value):
            return candidate
    return ""


def extract_shelfmark(metadata):
    shelfmark = first_metadata_value(metadata, SHELFMARK_KEYS)
    if shelfmark:
        return shelfmark
    for key, value in metadata.items():
        normalised_key = normalise_key(str(key))
        if "shelfmark" not in normalised_key and normalised_key != "identifier":
            continue
        for candidate in iter_scalar_strings(value):
            return candidate
    return ""


def extract_title(attributes, metadata):
    return (
        first_metadata_value(metadata, ("label", "title", "name"))
        or str(attributes.get("title") or "").strip()
    )


def metadata_hash(metadata):
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def coerce_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def iter_collections(session, jsonapi_root, page_limit, max_pages, timeout, verbose):
    endpoint = f"{jsonapi_root.rstrip('/')}/node/digital_object_collection"
    resource_type = "node--digital_object_collection"
    query = urlencode(
        {
            f"fields[{resource_type}]": (
                "drupal_internal__nid,title,created,changed,field_descriptive_metadata"
            ),
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
        log(f"Fetching collection page {page_number}: {next_url}", verbose)
        response = session.get(next_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        for record in payload.get("data", []):
            if isinstance(record, dict):
                yield record

        next_link = payload.get("links", {}).get("next", {})
        href = next_link.get("href") if isinstance(next_link, dict) else None
        next_url = urljoin(endpoint, href) if isinstance(href, str) and href else None


def get_identifier_type(shortcode):
    result = IdentifierType.query.filter_by(shortcode=shortcode).first()
    if result is None:
        raise RuntimeError(
            f"Missing IdentifierType {shortcode!r}; apply the collection migration first."
        )
    return result


def ensure_identifier(obj, identifier_type, value, counters):
    value = str(value).strip()
    existing = Identifier.query.filter_by(value=value).first()
    if existing:
        if existing.object_id != obj.id:
            raise IdentifierConflict(
                f"{identifier_type.shortcode} value {value!r} already belongs to "
                f"object_id={existing.object_id}"
            )
        if existing.type_id != identifier_type.id:
            raise IdentifierConflict(
                f"Identifier value {value!r} is type {existing.type.shortcode!r}, "
                f"not {identifier_type.shortcode!r}"
            )
        return existing

    created = Identifier(value=value, object_id=obj.id, type_id=identifier_type.id)
    db.session.add(created)
    db.session.flush()
    counters["created_identifiers"] += 1
    return created


def find_by_source_uuid(source_uuid):
    return (
        CollectionSourceRecord.query
        .join(CollectionSourceRecord.primary_identifier)
        .filter(CollectionSourceRecord.source_system == SOURCE_SYSTEM)
        .filter(Identifier.value == source_uuid)
        .first()
    )


def find_unique_by_shelfmark(shelfmark_normalised):
    if not shelfmark_normalised:
        return None
    matches = Collection.query.filter_by(shelfmark_normalised=shelfmark_normalised).all()
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous shelfmark {shelfmark_normalised!r}: {len(matches)} collections match"
        )
    return matches[0] if matches else None


def create_collection(collection_type, identifier_types, counters):
    collection_object = Object(type_id=collection_type.id, primary_id=None)
    db.session.add(collection_object)
    db.session.flush()
    ark_identifier = ensure_identifier(
        collection_object, identifier_types["ark"], mint_ark(), counters
    )
    collection_object.primary_id = ark_identifier.value
    collection = Collection(object_id=collection_object.id)
    db.session.add(collection)
    db.session.flush()
    counters["created_collections"] += 1
    return collection


def sync_record(record, collection_type, identifier_types, counters):
    attributes = record.get("attributes") or {}
    source_uuid = str(record.get("id") or "").strip()
    source_nid = coerce_int(attributes.get("drupal_internal__nid"))
    if not source_uuid:
        raise RuntimeError("Collection record has no JSON:API UUID")
    if source_nid is None:
        raise RuntimeError(f"Collection {source_uuid} has no integer Drupal node ID")

    metadata = parse_metadata(attributes)
    shelfmark = extract_shelfmark(metadata)
    shelfmark_normalised = normalise_shelfmark(shelfmark)
    title = extract_title(attributes, metadata)

    source_record = find_by_source_uuid(source_uuid)
    if source_record:
        collection = source_record.collection
        counters["matched_source_uuid"] += 1
    else:
        collection = find_unique_by_shelfmark(shelfmark_normalised)
        if collection:
            counters["matched_shelfmark"] += 1
        else:
            collection = create_collection(collection_type, identifier_types, counters)

    collection_object = collection.object
    source_identifier = ensure_identifier(
        collection_object, identifier_types["arch"], source_uuid, counters
    )
    ensure_identifier(collection_object, identifier_types["arch_nid"], source_nid, counters)

    if source_record is None:
        source_record = CollectionSourceRecord(
            collection_id=collection.id,
            source_system=SOURCE_SYSTEM,
            primary_identifier_id=source_identifier.id,
        )
        db.session.add(source_record)
        counters["created_source_records"] += 1

    collection.shelfmark = shelfmark or None
    collection.shelfmark_normalised = shelfmark_normalised or None
    collection.title = title or None
    source_record.source_url = f"https://digital.collections.ed.ac.uk/node/{source_nid}"
    source_record.source_metadata_hash = metadata_hash(metadata)
    source_record.last_seen_at = utcnow()
    counters["processed"] += 1


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
        collection_type = ObjectType.query.filter_by(name="Digital Object Collection").first()
        if collection_type is None:
            raise SystemExit("Missing ObjectType 'Digital Object Collection'; apply migration first.")
        identifier_types = {
            shortcode: get_identifier_type(shortcode)
            for shortcode in ("ark", "arch", "arch_nid")
        }
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
            for record in iter_collections(
                session,
                args.jsonapi_root,
                args.page_limit,
                args.max_pages,
                args.timeout,
                verbose,
            ):
                savepoint = db.session.begin_nested()
                try:
                    sync_record(record, collection_type, identifier_types, counters)
                    db.session.flush()
                    savepoint.commit()
                except Exception as exc:
                    savepoint.rollback()
                    counters["errors"] += 1
                    print(f"Skipping collection {record.get('id')}: {exc}", file=sys.stderr)

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
                counters["errors"] += 1
                state.details_json = state_details(counters)
                db.session.commit()
            raise SystemExit(f"Collection sync failed: {exc}") from exc

        print(
            "Collection sync complete. "
            f"processed={counters['processed']} "
            f"created_collections={counters['created_collections']} "
            f"matched_source_uuid={counters['matched_source_uuid']} "
            f"matched_shelfmark={counters['matched_shelfmark']} "
            f"created_source_records={counters['created_source_records']} "
            f"created_identifiers={counters['created_identifiers']} "
            f"errors={counters['errors']}"
        )


if __name__ == "__main__":
    main()
