#!/usr/bin/env python3
"""Re-key ERIC's unshouldered UUID ARKs while retaining resolver aliases.

By default this is a report-only dry run. With --apply, the old ARK becomes an
``ark_legacy`` identifier and a shoulder-prefixed ``ark`` identifier is
created for the same object. Existing direct citations therefore continue to
resolve, but redirect to the new canonical ARK. Archives objects are excluded
by default because ArchivesSpace is their authoritative ARK minter.
"""

import argparse
import uuid
from collections import Counter

import bootstrap
from sqlalchemy.orm import joinedload

from app import (
    ARK_IDENTIFIER_SHORTCODES,
    ARK_SHOULDERS,
    NAAN,
    CompoundObject,
    Identifier,
    IdentifierType,
    app,
    db,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the re-keying; without this flag the script only reports",
    )
    parser.add_argument(
        "--include-archives",
        action="store_true",
        help=(
            "also re-key Archives compound objects locally; normally these should "
            "wait for their authoritative ArchivesSpace ARKs"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="show only exceptions and the final summary, not routine re-key proposals",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help=(
            "commit this many re-keyed ARKs at a time when using --apply "
            "(default: %(default)s); makes an interrupted run safely resumable"
        ),
    )
    return parser.parse_args()


def unshouldered_uuid(value):
    prefix = f"ark:/{NAAN}/"
    if not value.startswith(prefix):
        return None
    name = value[len(prefix):]
    try:
        parsed = uuid.UUID(name)
    except (ValueError, AttributeError):
        return None
    return str(parsed)


def namespace_for_object(obj, compound_domains):
    if obj.id in compound_domains:
        compound_domain = compound_domains[obj.id]
        if compound_domain in ARK_SHOULDERS:
            return compound_domain
        return None
    return "media"


def main():
    args = parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    with app.app_context():
        ark_type = IdentifierType.query.filter_by(shortcode="ark").first()
        legacy_type = IdentifierType.query.filter_by(shortcode="ark_legacy").first()
        if not ark_type or not legacy_type:
            raise SystemExit(
                "Missing ark or ark_legacy identifier type; apply migration 005 first."
            )

        all_arks = (
            Identifier.query
            .join(Identifier.type)
            .options(joinedload(Identifier.object))
            .filter(IdentifierType.shortcode.in_(ARK_IDENTIFIER_SHORTCODES))
            .all()
        )
        normalised_values = {
            identifier.value.replace("-", ""): identifier
            for identifier in all_arks
        }
        candidates = [
            identifier
            for identifier in all_arks
            if identifier.type_id == ark_type.id and unshouldered_uuid(identifier.value)
        ]
        compound_domains = {
            object_id: domain
            for object_id, domain in db.session.query(
                CompoundObject.object_id, CompoundObject.domain
            ).all()
        }

        counters = Counter()
        pending_rekeys = 0
        for identifier in candidates:
            obj = identifier.object
            namespace = namespace_for_object(obj, compound_domains)
            if not namespace:
                counters["unclassified"] += 1
                print(
                    f"Skipping object_id={obj.id}: compound object has no recognised domain "
                    f"(old ARK {identifier.value})."
                )
                continue
            if namespace == "archives" and not args.include_archives:
                counters["archives_deferred"] += 1
                if not args.quiet:
                    print(
                        f"Deferring object_id={obj.id}: wait for the authoritative "
                        f"ArchivesSpace ARK (old ARK {identifier.value})."
                    )
                continue

            uuid_name = unshouldered_uuid(identifier.value)
            new_value = f"ark:/{NAAN}/{ARK_SHOULDERS[namespace]}-{uuid_name}"
            equivalent = normalised_values.get(new_value.replace("-", ""))
            if equivalent and equivalent.object_id != obj.id:
                counters["conflicts"] += 1
                print(
                    f"Skipping object_id={obj.id}: {new_value} is hyphen-equivalent to "
                    f"{equivalent.value} on object_id={equivalent.object_id}."
                )
                continue

            if equivalent:
                counters["already_current"] += 1
                continue

            counters["would_rekey"] += 1
            if not args.quiet:
                print(f"{identifier.value} -> {new_value} (object_id={obj.id})")
            if args.apply:
                identifier.type_id = legacy_type.id
                db.session.add(
                    Identifier(value=new_value, object_id=obj.id, type_id=ark_type.id)
                )
                if obj.primary_id == identifier.value:
                    obj.primary_id = new_value
                normalised_values[new_value.replace("-", "")] = identifier
                counters["rekeyed"] += 1
                pending_rekeys += 1
                if pending_rekeys >= args.batch_size:
                    db.session.commit()
                    print(f"Committed batch: total_rekeyed={counters['rekeyed']}")
                    pending_rekeys = 0

        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()

        mode = "Applied" if args.apply else "Dry run"
        print(f"{mode}: " + " ".join(
            f"{key}={value}" for key, value in sorted(counters.items())
        ))


if __name__ == "__main__":
    main()
