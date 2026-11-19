#!/usr/bin/env python3
"""Attach exceptional LUNA book-reader IDs to their ERIC CompoundObjects.

The input maps each legacy LUNA book ID to a domain-scoped compound shelfmark.
By default this reports the result without writing; pass --apply to insert the
unambiguous identifiers after migration 004.
"""

import argparse
import csv
from collections import Counter
from pathlib import Path

import bootstrap

from app import CompoundObject, Identifier, IdentifierType, app, db
from project_paths import DATA_DIR


DEFAULT_SOURCE = DATA_DIR / "input" / "luna_book_identifiers.csv"
REQUIRED_COLUMNS = {"domain", "shelfmark", "luna_identifier"}


def normalise_shelfmark(value):
    return " ".join((value or "").upper().split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--apply", action="store_true", help="Insert unambiguous identifiers.")
    args = parser.parse_args()

    with args.source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not REQUIRED_COLUMNS.issubset(rows[0]):
        raise SystemExit(f"{args.source} must contain: {', '.join(sorted(REQUIRED_COLUMNS))}")

    with app.app_context():
        identifier_type = IdentifierType.query.filter_by(shortcode="luna_book").first()
        if identifier_type is None:
            raise SystemExit("Missing IdentifierType 'luna_book'; apply migration 004 first.")

        counters = Counter()
        for row_number, row in enumerate(rows, start=2):
            identifier = (row.get("luna_identifier") or "").strip()
            domain = (row.get("domain") or "").strip()
            shelfmark = normalise_shelfmark(row.get("shelfmark"))
            if not identifier or not domain or not shelfmark:
                counters["invalid"] += 1
                print(f"Skipping {args.source}:{row_number}: missing identifier, domain, or shelfmark.")
                continue

            matches = (
                CompoundObject.query
                .filter_by(domain=domain, shelfmark_normalised=shelfmark)
                .all()
            )
            if len(matches) != 1:
                counters["unmatched"] += 1
                print(
                    f"Skipping {args.source}:{row_number}: {domain}/{shelfmark!r} "
                    f"matched {len(matches)} compound objects."
                )
                continue

            compound = matches[0]
            existing = Identifier.query.filter_by(
                type_id=identifier_type.id, value=identifier
            ).first()
            if existing:
                if existing.object_id == compound.object_id:
                    counters["unchanged"] += 1
                else:
                    counters["conflicts"] += 1
                    print(
                        f"Conflict at {args.source}:{row_number}: {identifier!r} belongs to "
                        f"object_id={existing.object_id}, not compound_object_id={compound.id}."
                    )
                continue

            counters["would_create"] += 1
            if args.apply:
                db.session.add(
                    Identifier(
                        type_id=identifier_type.id,
                        object_id=compound.object_id,
                        value=identifier,
                    )
                )
                counters["created"] += 1

        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()

        mode = "Applied" if args.apply else "Dry run"
        print(
            f"{mode}: rows={len(rows)} would_create={counters['would_create']} "
            f"created={counters['created']} unchanged={counters['unchanged']} "
            f"unmatched={counters['unmatched']} conflicts={counters['conflicts']} "
            f"invalid={counters['invalid']}"
        )


if __name__ == "__main__":
    main()
