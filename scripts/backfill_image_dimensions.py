#!/usr/bin/env python3
"""Fetch and persist missing IIIF source dimensions for ERIC image objects.

By default this is a report-only dry run.  Use --apply to save dimensions, and
--limit to run a manageable batch when populating a large existing database.
"""

import argparse
from collections import Counter

import bootstrap
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import Identifier, IdentifierType, Object, app, db, get_cantaloupe_base_url
from scripts.archipelago_sweep import build_session, fetch_iiif_dimensions


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="save fetched dimensions")
    parser.add_argument("--limit", type=int, default=None, help="process at most this many images")
    parser.add_argument(
        "--luna",
        help="populate one image selected by its LUNA identifier",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    session = build_session()
    info_cache = {}
    counters = Counter()
    with app.app_context():
        query = (
            Identifier.query
            .join(Identifier.type)
            .join(Object, Object.id == Identifier.object_id)
            .options(joinedload(Identifier.type), joinedload(Identifier.object))
            .filter(IdentifierType.shortcode == "cantaloupe")
            .filter(or_(Object.width.is_(None), Object.height.is_(None)))
            .order_by(Identifier.id)
        )
        if args.luna:
            luna_type = IdentifierType.query.filter_by(shortcode="luna").first()
            luna_identifier = (
                Identifier.query.filter_by(type_id=luna_type.id, value=args.luna).first()
                if luna_type else None
            )
            if luna_identifier is None:
                raise SystemExit(f"No ERIC object has LUNA identifier {args.luna!r}.")
            query = query.filter(Object.id == luna_identifier.object_id)
        if args.limit is not None:
            query = query.limit(args.limit)

        for identifier in query:
            obj = identifier.object
            info_url = f"{get_cantaloupe_base_url(identifier)}/info.json"
            try:
                width, height = fetch_iiif_dimensions(
                    session, info_url, info_cache, verbose=not args.quiet
                )
            except Exception as exc:
                counters["errors"] += 1
                print(f"Could not fetch object_id={obj.id}: {exc}")
                continue

            counters["would_update"] += 1
            print(f"object_id={obj.id} {obj.width}x{obj.height} -> {width}x{height}")
            if args.apply:
                obj.width, obj.height = width, height
                counters["updated"] += 1

        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()
    mode = "Applied" if args.apply else "Dry run"
    print(f"{mode}: " + " ".join(f"{key}={value}" for key, value in sorted(counters.items())))


if __name__ == "__main__":
    main()
