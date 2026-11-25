#!/usr/bin/env python3
"""Export Vernon compound-object ARKs and IIIF manifest URLs as XML.

Each exported record is a CompoundObject whose ERIC Object has a ``vernon``
identifier.  The export uses the current ``ark`` identifier (not an
``ark_legacy`` alias), publishing it in modern, dashless form together with
its N2T manifest URL.
"""

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET

import bootstrap
from sqlalchemy.orm import aliased

from app import CompoundObject, Identifier, IdentifierType, Object, app, db


DEFAULT_OUTPUT = Path("data/output/vernon_manifests.xml")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="XML output path (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be exported without writing XML",
    )
    return parser.parse_args()


def modern_dashless_ark(ark):
    """Convert a stored classic-form ARK to the published modern form."""
    if not ark.startswith("ark:/"):
        raise ValueError(f"Expected classic-form ARK, got {ark!r}")

    modern_ark = f"ark:{ark[5:]}"
    prefix, ark_name = modern_ark.split("/", 1)
    return f"{prefix}/{ark_name.replace('-', '')}"


def n2t_manifest_url(ark):
    """Return the canonical N2T manifest URL for a stored classic-form ARK."""
    return f"https://n2t.net/{modern_dashless_ark(ark)}/manifest"


def main():
    args = parse_args()

    with app.app_context():
        vernon_type = IdentifierType.query.filter_by(shortcode="vernon").first()
        ark_type = IdentifierType.query.filter_by(shortcode="ark").first()
        if not vernon_type or not ark_type:
            raise SystemExit("ERIC needs both the vernon and ark identifier types.")

        vernon_identifier = aliased(Identifier)
        ark_identifier = aliased(Identifier)
        rows = (
            db.session.query(
                CompoundObject.id.label("compound_object_id"),
                vernon_identifier.value.label("vernon_id"),
                ark_identifier.value.label("ark"),
            )
            .join(Object, Object.id == CompoundObject.object_id)
            .join(
                vernon_identifier,
                (vernon_identifier.object_id == Object.id)
                & (vernon_identifier.type_id == vernon_type.id),
            )
            .outerjoin(
                ark_identifier,
                (ark_identifier.object_id == Object.id)
                & (ark_identifier.type_id == ark_type.id),
            )
            .order_by(vernon_identifier.value, CompoundObject.id)
            .all()
        )

    root = ET.Element("recordList")
    exported = 0
    skipped = 0
    for row in rows:
        if not row.ark:
            skipped += 1
            print(
                f"Skipping compound_object_id={row.compound_object_id}: "
                f"Vernon record {row.vernon_id!r} has no current ARK."
            )
            continue

        record = ET.SubElement(root, "record")
        ET.SubElement(record, "id").text = row.vernon_id
        ET.SubElement(record, "ark").text = modern_dashless_ark(row.ark)
        ET.SubElement(record, "manifest").text = n2t_manifest_url(row.ark)
        exported += 1

    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(root, space="  ")
        tree = ET.ElementTree(root)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)

    action = "Would write" if args.dry_run else "Wrote"
    print(
        f"{action} {exported} Vernon manifest record(s)"
        + (f" to {args.output}" if not args.dry_run else "")
        + f"; skipped={skipped}."
    )


if __name__ == "__main__":
    main()
