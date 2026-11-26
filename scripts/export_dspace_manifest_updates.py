#!/usr/bin/env python3
"""Create a BOM-free DSpace CSV containing public ARK manifest URLs.

Rows are matched from ``dc.identifier`` to ERIC compound-object shelfmarks.
Only unambiguous matches with a current ARK are exported. The output retains
the DSpace ``id`` and ``collection`` columns and adds ``dc.identifier.manifest``.
"""

import argparse
import csv
from collections import Counter
from pathlib import Path

import bootstrap
from sqlalchemy.orm import joinedload

from app import CompoundObject, Identifier, Object, app
from dspace_helpers import compound_lookup, n2t_manifest_url, shelfmark_key
from project_paths import DATA_DIR


DEFAULT_SOURCE = DATA_DIR / "input" / "dspaceupdate.csv"
DEFAULT_OUTPUT = DATA_DIR / "output" / "dspace_manifest_updates.csv"
REQUIRED_COLUMNS = {"id", "collection", "dc.identifier"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Report matches without writing CSV.")
    parser.add_argument(
        "--report-limit",
        type=int,
        default=25,
        help="Maximum unmatched/ambiguous rows printed (default: %(default)s).",
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
        compounds = (
            CompoundObject.query
            .options(
                joinedload(CompoundObject.object)
                .joinedload(Object.identifiers)
                .joinedload(Identifier.type)
            )
            .filter(CompoundObject.shelfmark_normalised.isnot(None))
            .all()
        )
        lookup = compound_lookup(compounds)

        output_rows = []
        counters = Counter()
        reported = 0
        for row_number, row in enumerate(rows, start=2):
            shelfmark = (row.get("dc.identifier") or "").strip()
            matches = lookup.get(shelfmark_key(shelfmark), []) if shelfmark else []
            if not shelfmark:
                issue = "blank shelfmark"
                counters["invalid"] += 1
            elif len(matches) != 1:
                issue = f"matched {len(matches)} compound objects"
                counters["ambiguous" if matches else "unmatched"] += 1
            else:
                compound = matches[0]
                ark = next(
                    (identifier.value for identifier in compound.object.identifiers
                     if identifier.type.shortcode == "ark"),
                    None,
                )
                if not ark:
                    issue = "matched compound object has no current ARK"
                    counters["missing_ark"] += 1
                else:
                    output_rows.append(
                        {
                            "id": row["id"],
                            "collection": row["collection"],
                            "dc.identifier.manifest": n2t_manifest_url(ark),
                        }
                    )
                    counters["written"] += 1
                    continue

            if reported < args.report_limit:
                print(f"Skipping {args.source}:{row_number}: {shelfmark!r} {issue}.")
                reported += 1

    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.DictWriter(
                output_file,
                fieldnames=["id", "collection", "dc.identifier.manifest"],
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(output_rows)

    action = "Would write" if args.dry_run else "Wrote"
    print(
        f"{action} {counters['written']} row(s)"
        + (f" to {args.output}" if not args.dry_run else "")
        + f"; unmatched={counters['unmatched']} ambiguous={counters['ambiguous']} "
        f"missing_ark={counters['missing_ark']} invalid={counters['invalid']}."
    )


if __name__ == "__main__":
    main()
