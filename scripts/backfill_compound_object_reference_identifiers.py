#!/usr/bin/env python3
"""Backfill domain-scoped reference identifiers from CompoundObject shelfmarks.

Run after migration 004. By default this reports the intended changes without
writing. Pass --apply to create identifiers that are unambiguous and fit the
identifier value column.
"""

import argparse
from collections import Counter

import bootstrap

from app import CompoundObject, Identifier, IdentifierType, app, db


REFERENCE_TYPES = {
    "archives": "archives_reference",
    "rare_books": "rare_books_shelfmark",
    "museums": "vernon_accession",
}
MAX_IDENTIFIER_VALUE_LENGTH = 128


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create unambiguous identifiers (the default is a dry run).",
    )
    args = parser.parse_args()

    with app.app_context():
        types = {
            domain: IdentifierType.query.filter_by(shortcode=shortcode).first()
            for domain, shortcode in REFERENCE_TYPES.items()
        }
        missing = [REFERENCE_TYPES[domain] for domain, item in types.items() if item is None]
        if missing:
            raise SystemExit(
                "Missing identifier types: "
                + ", ".join(missing)
                + ". Apply migration 004 first."
            )

        counters = Counter()
        compound_objects = (
            CompoundObject.query
            .filter(CompoundObject.domain.in_(REFERENCE_TYPES))
            .filter(CompoundObject.shelfmark.isnot(None))
            .order_by(CompoundObject.id)
            .all()
        )
        for compound in compound_objects:
            value = compound.shelfmark.strip()
            if not value:
                counters["blank"] += 1
                continue
            if len(value) > MAX_IDENTIFIER_VALUE_LENGTH:
                counters["too_long"] += 1
                print(
                    f"Skipping compound_object_id={compound.id}: reference exceeds "
                    f"{MAX_IDENTIFIER_VALUE_LENGTH} characters."
                )
                continue

            identifier_type = types[compound.domain]
            existing = Identifier.query.filter_by(
                type_id=identifier_type.id, value=value
            ).first()
            if existing:
                if existing.object_id == compound.object_id:
                    counters["unchanged"] += 1
                else:
                    counters["conflicts"] += 1
                    print(
                        f"Conflict for {identifier_type.shortcode}={value!r}: "
                        f"compound_object_id={compound.id} shares it with "
                        f"object_id={existing.object_id}."
                    )
                continue

            counters["would_create"] += 1
            if args.apply:
                db.session.add(
                    Identifier(
                        type_id=identifier_type.id,
                        object_id=compound.object_id,
                        value=value,
                    )
                )
                counters["created"] += 1

        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()

        mode = "Applied" if args.apply else "Dry run"
        print(
            f"{mode}: candidates={len(compound_objects)} "
            f"would_create={counters['would_create']} created={counters['created']} "
            f"unchanged={counters['unchanged']} conflicts={counters['conflicts']} "
            f"blank={counters['blank']} too_long={counters['too_long']}"
        )


if __name__ == "__main__":
    main()
