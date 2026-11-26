#!/usr/bin/env python3
"""Attach canonical DSpace Handle identifiers to matching ERIC compound objects.

The input's ``dc.identifier`` field identifies the compound object by
shelfmark; ``dc.identifier.uri`` supplies the DSpace Handle. The script is a
dry run by default. Use ``--apply`` only after reviewing its report.
"""

import argparse
import csv
from collections import Counter
from pathlib import Path

import bootstrap

from app import CompoundObject, Identifier, IdentifierType, app, db
from dspace_helpers import compound_lookup, dspace_handle_from_uri, shelfmark_key
from project_paths import DATA_DIR


DEFAULT_SOURCE = DATA_DIR / "input" / "dspaceupdate.csv"
REQUIRED_COLUMNS = {"dc.identifier", "dc.identifier.uri"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--apply", action="store_true", help="Create identifiers after review.")
    parser.add_argument(
        "--report-limit",
        type=int,
        default=25,
        help="Maximum skipped/conflicting rows printed (default: %(default)s).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.report_limit < 0:
        raise SystemExit("--report-limit must be zero or greater.")

    with args.source.open(newline="", encoding="utf-8-sig") as source_file:
        rows = list(csv.DictReader(source_file))
    if not rows or not REQUIRED_COLUMNS.issubset(rows[0]):
        raise SystemExit(f"{args.source} must contain: {', '.join(sorted(REQUIRED_COLUMNS))}")

    with app.app_context():
        identifier_type = IdentifierType.query.filter_by(shortcode="dspace_handle").first()
        if identifier_type is None:
            raise SystemExit("Missing IdentifierType 'dspace_handle'; apply migration 007 first.")

        compounds = (
            CompoundObject.query
            .filter(CompoundObject.shelfmark_normalised.isnot(None))
            .all()
        )
        lookup = compound_lookup(compounds)
        counters = Counter()
        reported = 0

        for row_number, row in enumerate(rows, start=2):
            shelfmark = (row.get("dc.identifier") or "").strip()
            handle = dspace_handle_from_uri(row.get("dc.identifier.uri"))
            matches = lookup.get(shelfmark_key(shelfmark), []) if shelfmark else []
            if not shelfmark or not handle:
                issue = "missing shelfmark or a recognisable DSpace Handle"
                counters["invalid"] += 1
            elif len(matches) != 1:
                issue = f"shelfmark {shelfmark!r} matched {len(matches)} compound objects"
                counters["ambiguous" if matches else "unmatched"] += 1
            else:
                compound = matches[0]
                existing = Identifier.query.filter_by(
                    type_id=identifier_type.id,
                    value=handle,
                ).first()
                if existing is None:
                    counters["would_create"] += 1
                    if args.apply:
                        db.session.add(
                            Identifier(
                                type_id=identifier_type.id,
                                object_id=compound.object_id,
                                value=handle,
                            )
                        )
                        counters["created"] += 1
                    continue
                if existing.object_id == compound.object_id:
                    counters["unchanged"] += 1
                    continue

                issue = (
                    f"Handle {handle!r} already belongs to object_id={existing.object_id}, "
                    f"not compound_object_id={compound.id}"
                )
                counters["conflicts"] += 1

            if reported < args.report_limit:
                print(f"Skipping {args.source}:{row_number}: {issue}.")
                reported += 1

        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()

    mode = "Applied" if args.apply else "Dry run"
    print(
        f"{mode}: rows={len(rows)} would_create={counters['would_create']} "
        f"created={counters['created']} unchanged={counters['unchanged']} "
        f"unmatched={counters['unmatched']} ambiguous={counters['ambiguous']} "
        f"conflicts={counters['conflicts']} invalid={counters['invalid']}"
    )


if __name__ == "__main__":
    main()
