#!/usr/bin/env python3
"""Add catalogue domains and source IDs to ERIC CompoundObjects.

The authoritative current values are read from Archipelago's Digital Object
Collection records.  The ERIC CompoundObject remains source-neutral: `domain`
is deliberately a small controlled vocabulary, while the catalogue ID is a
normal, typed Identifier on its associated ERIC Object.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin

import bootstrap

from app import (
    CompoundObject,
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


DEFAULT_JSONAPI_ROOT = "http://lac-dams-live2.is.ed.ac.uk/jsonapi"
SYNC_JOB_NAME = "archipelago_compound_object_catalogue_data"
SOURCE_ID_KEYS = ("source_id", "source id")
INTERNAL_TYPE_KEYS = ("internal_type", "internal type")
PARENT_KEYS = ("ismemberof", "is_member_of", "is member of", "ispartof", "is_part_of", "is part of")
DOMAINS = {"archives", "rare_books", "museums"}
INTERNAL_TYPE_DOMAINS = {
    "archivesspace": "archives",
    "alma": "rare_books",
    "vernon": "museums",
}

# These are the established top-level Digital Collections nodes.  Keeping this
# small mapping in code makes classification explicit and reviewable; extra
# roots can be supplied as --root-domain NID=DOMAIN without editing the script.
DEFAULT_ROOT_DOMAINS = {
    "3": "museums",   # Comparative Anatomy Collection
    "4": "archives",  # University Archives and Manuscripts
    "5": "museums",   # Art Collection
    "6": "museums",   # Cockburn Museum of Geology
    "7": "archives",  # Lothian Health Services Archive
    "8": "museums",   # Musical Instruments Collection
    "9": "archives",  # New College Archive
    "10": "rare_books",  # Rare Books Collection
    "11": "archives",  # School of Scottish Studies Archives
}


class IdentifierConflict(RuntimeError):
    """A source identifier is already attached to another ERIC Object."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jsonapi-root", default=os.environ.get("ARCHIPELAGO_JSONAPI_ROOT", DEFAULT_JSONAPI_ROOT)
    )
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--http-retries", type=int, default=DEFAULT_HTTP_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument(
        "--root-domain", action="append", default=[], metavar="NID=DOMAIN",
        help="Add or override a top-level Archipelago node mapping; DOMAIN is archives, rare_books, or museums.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def normalise_key(value):
    return " ".join("".join(c if c.isalnum() else " " for c in str(value).lower()).split())


def scalar_values(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from scalar_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from scalar_values(nested)
    elif isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text:
            yield text


def metadata_values(metadata, keys):
    wanted = {normalise_key(key) for key in keys}
    found = []

    def walk(value):
        if not isinstance(value, dict):
            return
        for key, nested in value.items():
            if normalise_key(key) in wanted:
                found.extend(scalar_values(nested))
            if isinstance(nested, dict):
                walk(nested)
            elif isinstance(nested, list):
                for item in nested:
                    walk(item)

    walk(metadata)
    return found


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


def parse_root_domains(entries):
    result = dict(DEFAULT_ROOT_DOMAINS)
    for entry in entries:
        nid, separator, domain = entry.partition("=")
        nid, domain = nid.strip(), domain.strip()
        if not separator or not nid.isdigit() or domain not in DOMAINS:
            raise SystemExit(f"Invalid --root-domain {entry!r}; use NID=archives, NID=rare_books, or NID=museums.")
        result[nid] = domain
    return result


def classify_domain(metadata, root_domains):
    """Return domain from Archipelago's backend marker, then safe fallbacks."""
    internal_types = {
        "".join(character for character in value.lower() if character.isalnum())
        for value in metadata_values(metadata, INTERNAL_TYPE_KEYS)
    }
    internal_domains = {
        INTERNAL_TYPE_DOMAINS[value]
        for value in internal_types
        if value in INTERNAL_TYPE_DOMAINS
    }
    if len(internal_domains) > 1:
        raise RuntimeError(f"Conflicting internal_type values: {sorted(internal_types)}")

    parent_nids = metadata_values(metadata, PARENT_KEYS)
    mapped = {root_domains[value] for value in parent_nids if value in root_domains}
    if len(mapped) > 1:
        raise RuntimeError(f"Conflicting top-level domains in parent metadata: {sorted(mapped)}")

    internal_domain = next(iter(internal_domains), None)
    parent_domain = next(iter(mapped), None)
    if internal_domain and parent_domain and internal_domain != parent_domain:
        raise RuntimeError(
            f"internal_type gives {internal_domain}, but parent hierarchy gives {parent_domain}"
        )

    domain = internal_domain or parent_domain
    source_ids = metadata_values(metadata, SOURCE_ID_KEYS)
    source_id = source_ids[0] if source_ids else ""
    # Alma MMS IDs are expected to start 99 (for example 9924294442602466).
    source_id_domain = "rare_books" if source_id.isdigit() and source_id.startswith("99") else None
    if domain and source_id_domain and domain != source_id_domain:
        raise RuntimeError(
            f"Parent hierarchy gives {domain}, but source_id {source_id!r} has Alma's 99... form"
        )
    return domain or source_id_domain, source_id


def iter_compound_object_records(session, jsonapi_root, page_limit, max_pages, timeout, verbose):
    endpoint = f"{jsonapi_root.rstrip('/')}/node/digital_object_collection"
    resource_type = "node--digital_object_collection"
    query = urlencode({
        f"fields[{resource_type}]": "drupal_internal__nid,title,field_descriptive_metadata",
        "sort": "drupal_internal__nid", "page[limit]": page_limit,
    })
    next_url, page_number = f"{endpoint}?{query}", 0
    while next_url:
        page_number += 1
        if max_pages is not None and page_number > max_pages:
            return
        log(f"Fetching compound-object page {page_number}: {next_url}", verbose)
        response = session.get(next_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        yield page_number, [row for row in payload.get("data", []) if isinstance(row, dict)]
        link = payload.get("links", {}).get("next", {})
        href = link.get("href") if isinstance(link, dict) else None
        next_url = urljoin(endpoint, href) if isinstance(href, str) and href else None


def identifier_type(shortcode):
    result = IdentifierType.query.filter_by(shortcode=shortcode).first()
    if result is None:
        raise RuntimeError(f"Missing IdentifierType {shortcode!r}; apply migration 003 first.")
    return result


def ensure_identifier(obj, id_type, value, counters):
    existing = Identifier.query.filter_by(type_id=id_type.id, value=value).first()
    if existing:
        if existing.object_id != obj.id:
            raise IdentifierConflict(
                f"{id_type.shortcode} ID {value!r} belongs to object_id={existing.object_id}"
            )
        return
    db.session.add(Identifier(type_id=id_type.id, object_id=obj.id, value=value))
    counters["created_catalogue_identifiers"] += 1


def process_record(record, arch_type, catalogue_types, root_domains, counters):
    source_uuid = str(record.get("id") or "").strip()
    if not source_uuid:
        raise RuntimeError("record has no Archipelago UUID")
    compound_object = (
        CompoundObject.query.join(Identifier, CompoundObject.object_id == Identifier.object_id)
        .filter(Identifier.type_id == arch_type.id, Identifier.value == source_uuid).first()
    )
    if compound_object is None:
        counters["missing_compound_objects"] += 1
        return

    domain, source_id = classify_domain(parse_metadata(record.get("attributes") or {}), root_domains)
    if not domain:
        counters["unclassified_domains"] += 1
        print(f"No domain mapping for compound object {source_uuid}.", file=sys.stderr)
        return

    compound_object.domain = domain
    counters["domains_set"] += 1
    if not source_id:
        counters["missing_source_ids"] += 1
        print(f"No source_id for compound object {source_uuid} ({domain}).", file=sys.stderr)
        return
    ensure_identifier(compound_object.object, catalogue_types[domain], source_id, counters)
    counters["processed"] += 1


def main():
    args = parse_args()
    if args.page_limit < 1:
        raise SystemExit("--page-limit must be at least 1")
    root_domains = parse_root_domains(args.root_domain)
    session = build_session(http_retries=args.http_retries, retry_backoff=args.retry_backoff)

    with app.app_context():
        arch_type = identifier_type("arch")
        catalogue_types = {
            "archives": identifier_type("archives_space"),
            "rare_books": identifier_type("alma"),
            "museums": identifier_type("vernon"),
        }
        counters = Counter()
        state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
        if state is None:
            state = SyncState(job_name=SYNC_JOB_NAME)
            db.session.add(state)
        if not args.dry_run:
            state.status, state.details_json = "running", None
            db.session.commit()
        try:
            for _, records in iter_compound_object_records(
                session, args.jsonapi_root, args.page_limit, args.max_pages,
                args.timeout, not args.quiet,
            ):
                counters["pages"] += 1
                for record in records:
                    savepoint = db.session.begin_nested()
                    try:
                        process_record(record, arch_type, catalogue_types, root_domains, counters)
                        db.session.flush()
                        savepoint.commit()
                    except Exception as exc:
                        savepoint.rollback()
                        counters["errors"] += 1
                        print(f"Skipping compound object {record.get('id')}: {exc}", file=sys.stderr)
            if args.dry_run:
                db.session.rollback()
                print("Dry run only: rolled back all database changes.")
            else:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).one()
                state.status = "partial" if args.max_pages is not None else "complete"
                if args.max_pages is None:
                    state.last_completed_at = utcnow()
                state.details_json = json.dumps(dict(sorted(counters.items())), sort_keys=True)
                db.session.commit()
        except Exception:
            db.session.rollback()
            if not args.dry_run:
                state = SyncState.query.filter_by(job_name=SYNC_JOB_NAME).first()
                state.status = "failed"
                state.details_json = json.dumps(dict(sorted(counters.items())), sort_keys=True)
                db.session.commit()
            raise

        print(
            "Compound-object catalogue sync complete. "
            f"pages={counters['pages']} processed={counters['processed']} "
            f"domains_set={counters['domains_set']} "
            f"created_catalogue_identifiers={counters['created_catalogue_identifiers']} "
            f"missing_compound_objects={counters['missing_compound_objects']} "
            f"unclassified_domains={counters['unclassified_domains']} "
            f"missing_source_ids={counters['missing_source_ids']} errors={counters['errors']}"
        )


if __name__ == "__main__":
    main()
