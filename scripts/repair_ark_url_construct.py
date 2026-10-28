#!/usr/bin/env python3
"""Repair ERIC's ARK link template without changing identifier values.

ARK identifier values are stored in their full form, such as
``ark:/83794/<uuid>``.  This script changes only the ``ark`` identifier type's
URL template, so JSON and HTML metadata links resolve through the Edinburgh
Collections resolver without repeating the ARK prefix.

Run without arguments to preview.  Pass ``--apply`` to commit the update.
"""

import argparse

import bootstrap

from app import IdentifierType, app, db


ARK_URL_CONSTRUCT = "https://id.collections.ed.ac.uk/<id>"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repair. Without this flag, only report the proposed change.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with app.app_context():
        ark_type = IdentifierType.query.filter_by(shortcode="ark").first()
        if not ark_type:
            raise SystemExit("No identifier type with shortcode 'ark' was found; no change made.")

        if ark_type.url_construct == ARK_URL_CONSTRUCT:
            print("ARK URL template is already correct; no change made.")
            return

        print(f"ARK URL template: {ark_type.url_construct!r} -> {ARK_URL_CONSTRUCT!r}")
        if not args.apply:
            print("Dry run: pass --apply to commit this change.")
            return

        ark_type.url_construct = ARK_URL_CONSTRUCT
        db.session.commit()
        print("ARK URL template updated.")


if __name__ == "__main__":
    main()
