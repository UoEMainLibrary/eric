#!/usr/bin/env python3
"""Repair unclassified CompoundObjects from their public Archipelago node page.

This is a deliberately narrow fallback for records absent from the internal
JSON:API collection feed.  It only considers CompoundObjects whose `domain`
is NULL and never changes their ERIC ARK, Archipelago identifiers, or image
memberships.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import bootstrap

from app import (
    CompoundObject,
    CompoundObjectSourceRecord,
    Identifier,
    IdentifierType,
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


PUBLIC_NODE_ROOT = "https://digital.collections.ed.ac.uk/node"
SYNC_JOB_NAME = "archipelago_compound_object_public_page_repair"
INTERNAL_TYPE_DOMAINS = {
    "archivesspace": "archives",
    "alma": "rare_books",
    "vernon": "museums",
}
SOURCE_ID_KEYS = {"sourceid", "sourceidentifier"}
INTERNAL_TYPE_PATTERN = re.compile(
    r'"internal_type"\s*:\s*\{\s*"type"\s*:\s*"([^"]+)"', re.IGNORECASE
)
SOURCE_ID_PATTERN = re.compile(
    r'"source_id"\s*:\s*"?([^",}\s]+)', re.IGNORECASE
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--http-retries", type=int, default=DEFAULT_HTTP_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def normalise_key(value):
    return "".join(character for character in str(value).lower() if character.isalnum())


def metadata_from_value(value):
    """Find the nested descriptive-metadata object in a Drupal JSON response."""
    if isinstance(value, dict):
        normalised_keys = {normalise_key(key) for key in value}
        if "internaltype" in normalised_keys or normalised_keys & SOURCE_ID_KEYS:
            return value
        for nested in value.values():
            found = metadata_from_value(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = metadata_from_value(nested)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                return metadata_from_value(json.loads(text))
            except json.JSONDecodeError:
                pass
    return None


def first_value(metadata, wanted_key):
    if not isinstance(metadata, dict):
        return ""
    for key, value in metadata.items():
        if normalise_key(key) != wanted_key:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def internal_type(metadata):
    if not isinstance(metadata, dict):
        return ""
    for key, value in metadata.items():
        if normalise_key(key) != "internaltype":
            continue
        if isinstance(value, dict):
            return first_value(value, "type")
        if isinstance(value, str):
            return value.strip()
    return ""


def node_nid(source_url):
    match = re.search(r"/node/(\d+)(?:/|$)", urlparse(source_url or "").path)
    return match.group(1) if match else ""


def fetch_metadata(session, nid, timeout):
    json_response = session.get(
        f"{PUBLIC_NODE_ROOT}/{nid}?_format=json",
        headers={"Accept": "application/json"}, timeout=timeout,
    )
    if json_response.ok:
        try:
            metadata = metadata_from_value(json_response.json())
        except json.JSONDecodeError:
            metadata = None
        if metadata:
            return metadata

    page_response = session.get(f"{PUBLIC_NODE_ROOT}/{nid}", timeout=timeout)
    page_response.raise_for_status()
    internal_match = INTERNAL_TYPE_PATTERN.search(page_response.text)
    source_match = SOURCE_ID_PATTERN.search(page_response.text)
    if internal_match or source_match:
        return {
            "internal_type": {"type": internal_match.group(1) if internal_match else ""},
            "source_id": source_match.group(1) if source_match else "",
        }
    return None


def type_row(shortcode):
    result = IdentifierType.query.filter_by(shortcode=shortcode).first()
    if result is None:
        raise RuntimeError(f"Missing IdentifierType {shortcode!r}; apply migration 003 first.")
    return result


def ensure_identifier(obj, id_type, value, counters):
    existing = Identifier.query.filter_by(type_id=id_type.id, value=value).first()
    if existing is not None:
        if existing.object_id != obj.id:
            raise RuntimeError(
                f"{id_type.shortcode} ID {value!r} belongs to object_id={existing.object_id}"
            )
        return
    db.session.add(Identifier(object_id=obj.id, type_id=id_type.id, value=value))
    counters["created_catalogue_identifiers"] += 1


def process(source_record, session, catalogue_types, timeout, counters):
    compound_object = source_record.compound_object
    nid = node_nid(source_record.source_url)
    if not nid:
        counters["invalid_source_urls"] += 1
        print(f"No node NID in source URL for compound_object_id={compound_object.id}", file=sys.stderr)
        return
    metadata = fetch_metadata(session, nid, timeout)
    if not metadata:
        counters["unavailable_public_records"] += 1
        print(f"No usable public metadata for node {nid} (compound_object_id={compound_object.id})", file=sys.stderr)
        return

    raw_internal_type = internal_type(metadata)
    domain = INTERNAL_TYPE_DOMAINS.get(normalise_key(raw_internal_type))
    if not domain:
        counters["unclassified_internal_types"] += 1
        print(f"Unknown internal_type {raw_internal_type!r} for node {nid}", file=sys.stderr)
        return
    source_id = first_value(metadata, "sourceid")
    compound_object.domain = domain
    counters["domains_set"] += 1
    if not source_id:
        counters["missing_source_ids"] += 1
        return
    ensure_identifier(compound_object.object, catalogue_types[domain], source_id, counters)
    counters["processed"] += 1


def main():
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    session = build_session(http_retries=args.http_retries, retry_backoff=args.retry_backoff)
    with app.app_context():
        catalogue_types = {
            "archives": type_row("archives_space"),
            "rare_books": type_row("alma"),
            "museums": type_row("vernon"),
        }
        query = (
            CompoundObjectSourceRecord.query.join(CompoundObject)
            .filter(CompoundObject.domain.is_(None))
            .filter(CompoundObjectSourceRecord.source_system == "archipelago")
            .filter(CompoundObjectSourceRecord.source_url.isnot(None))
            .order_by(CompoundObject.id)
        )
        if args.limit is not None:
            query = query.limit(args.limit)
        source_records = query.all()
        counters = Counter(candidates=len(source_records))
        state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
        if state is None:
            state = SyncState(job_name=SYNC_JOB_NAME)
            db.session.add(state)
        if not args.dry_run:
            state.status, state.details_json = "running", None
            db.session.commit()
        try:
            for source_record in source_records:
                savepoint = db.session.begin_nested()
                try:
                    log(f"Checking {source_record.source_url}", not args.quiet)
                    process(source_record, session, catalogue_types, args.timeout, counters)
                    db.session.flush()
                    savepoint.commit()
                except Exception as exc:
                    savepoint.rollback()
                    counters["errors"] += 1
                    print(f"Skipping compound_object_id={source_record.compound_object_id}: {exc}", file=sys.stderr)
            if args.dry_run:
                db.session.rollback()
                print("Dry run only: rolled back all database changes.")
            else:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).one()
                state.status = "complete"
                state.last_completed_at = utcnow()
                state.details_json = json.dumps(dict(sorted(counters.items())), sort_keys=True)
                db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        print(
            "Compound-object public-page repair complete. "
            f"candidates={counters['candidates']} processed={counters['processed']} "
            f"domains_set={counters['domains_set']} "
            f"created_catalogue_identifiers={counters['created_catalogue_identifiers']} "
            f"unavailable_public_records={counters['unavailable_public_records']} "
            f"unclassified_internal_types={counters['unclassified_internal_types']} "
            f"missing_source_ids={counters['missing_source_ids']} errors={counters['errors']}"
        )


if __name__ == "__main__":
    main()
