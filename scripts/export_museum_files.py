#!/usr/bin/env python3
"""Export ERIC museum compound-object files without crawling Archipelago.

ERIC does not retain the top-level Archipelago collection membership, so this
reports every CompoundObject classified as ``museums`` rather than only Musical
Instruments Collection.  It retains the requested six-column layout; Child
Node Id is blank because ERIC does not store the ``as:image`` dictionary key.
``Child View`` is the linked Digital Object's stored label.
"""

import argparse
import csv
import importlib.util
import os
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "museum_files_from_eric.csv"


def default_eric_config():
    """Find Eric's config both from this folder and from Eric/scripts."""
    candidates = (
        SCRIPT_DIR.parent / "config.py",
        SCRIPT_DIR.parents[2] / "eric" / "config.py",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[-1])


DEFAULT_ERIC_CONFIG = default_eric_config()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--domain", default="museums")
    parser.add_argument(
        "--source-type",
        default="vernon",
        help="ERIC identifier type for Source Id (default: %(default)s)",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("ERIC_DATABASE_URL"),
        help="SQLAlchemy database URL (or set ERIC_DATABASE_URL)",
    )
    parser.add_argument(
        "--eric-config",
        type=Path,
        default=DEFAULT_ERIC_CONFIG,
        help="ERIC config.py used when --database-url is not supplied",
    )
    return parser.parse_args()


def database_url_from_config(config_path):
    if not config_path.is_file():
        raise SystemExit(
            f"ERIC config file not found: {config_path}. Supply --database-url or --eric-config."
        )
    spec = importlib.util.spec_from_file_location("eric_export_config", config_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load ERIC config file: {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database_url = getattr(module, "SQLALCHEMY_DATABASE_URI", "")
    if not database_url:
        raise SystemExit(f"No SQLALCHEMY_DATABASE_URI in {config_path}")
    return database_url


QUERY = """
SELECT
    COALESCE(source_identifier.value, '') AS source_id,
    COALESCE(compound_object.shelfmark, '') AS shelfmark,
    COALESCE(digital_object_nid.value, '') AS node_id,
    COALESCE(compound_object_item.label, '') AS child_view,
    file_identifier.value AS filename
FROM compound_object
JOIN object AS compound_record
    ON compound_record.id = compound_object.object_id
JOIN compound_object_item
    ON compound_object_item.compound_object_id = compound_object.id
JOIN object AS digital_object
    ON digital_object.id = compound_object_item.object_id
JOIN identifier AS file_identifier
    ON file_identifier.object_id = digital_object.id
JOIN identifier_type AS file_identifier_type
    ON file_identifier_type.id = file_identifier.type_id
   AND file_identifier_type.shortcode = 'file'
LEFT JOIN (
    SELECT identifier.object_id, identifier.value
    FROM identifier
    JOIN identifier_type ON identifier_type.id = identifier.type_id
    WHERE identifier_type.shortcode = :source_type
) AS source_identifier
    ON source_identifier.object_id = compound_record.id
LEFT JOIN (
    SELECT identifier.object_id, identifier.value
    FROM identifier
    JOIN identifier_type ON identifier_type.id = identifier.type_id
    WHERE identifier_type.shortcode = 'arch_nid'
) AS digital_object_nid
    ON digital_object_nid.object_id = digital_object.id
WHERE compound_object.domain = :domain
ORDER BY source_id, shelfmark, node_id, filename
"""


def main():
    args = parse_args()
    database_url = args.database_url or database_url_from_config(args.eric_config)

    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:
        raise SystemExit(
            "SQLAlchemy is required. Run with ERIC's virtual environment, e.g. "
            "../eric/.venv/bin/python export_eric_museum_files.py"
        ) from exc

    engine = create_engine(database_url)
    fieldnames = [
        "Source Id",
        "Shelfmark or Catalogue Number",
        "Node Id",
        "Child Node Id",
        "Child View",
        "Child Filename",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0

    with engine.connect() as connection, args.output.open(
        "w", newline="", encoding="utf-8"
    ) as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        result = connection.execute(text(QUERY), {"domain": args.domain, "source_type": args.source_type})
        for row in result.mappings():
            writer.writerow(
                {
                    "Source Id": row["source_id"],
                    "Shelfmark or Catalogue Number": row["shelfmark"],
                    "Node Id": row["node_id"],
                    "Child Node Id": "",
                    "Child View": row["child_view"],
                    "Child Filename": row["filename"],
                }
            )
            rows_written += 1

    print(f"Wrote {rows_written} museum file row(s) to {args.output}")


if __name__ == "__main__":
    main()
