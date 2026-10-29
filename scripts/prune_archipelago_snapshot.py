#!/usr/bin/env python3
"""Preview or apply UUID-based pruning from a completed Archipelago snapshot.

The script never trusts a partial snapshot.  By default it only reports the
objects that would be removed; ``--apply`` is required to write to the DB.
"""

import argparse
import hashlib
import json

import bootstrap
from app import (
    CompoundObject,
    CompoundObjectItem,
    CompoundObjectSourceRecord,
    Identifier,
    IdentifierType,
    Object,
    ObjectType,
    app,
    db,
)
from project_paths import OUTPUT_DIR


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("images", "collections"))
    parser.add_argument("--apply", action="store_true", help="Commit the planned deletions.")
    return parser.parse_args()


def snapshot_paths(kind):
    directory = OUTPUT_DIR / "archipelago_snapshots"
    return directory / f"{kind}.json", directory / f"{kind}.txt"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_completed_snapshot(kind):
    state_path, manifest_path = snapshot_paths(kind)
    if not state_path.is_file() or not manifest_path.is_file():
        raise SystemExit(f"No completed {kind} snapshot found. Run snapshot_archipelago_uuids.py first.")
    with state_path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("status") != "complete" or state.get("manifest") != str(manifest_path):
        raise SystemExit(f"{kind.capitalize()} snapshot is not complete; refusing to prune.")
    if state.get("manifest_sha256") != sha256(manifest_path):
        raise SystemExit(f"{kind.capitalize()} manifest has changed; refusing to prune.")
    with manifest_path.open(encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def arch_type():
    result = IdentifierType.query.filter_by(shortcode="arch").first()
    if result is None:
        raise RuntimeError("Missing IdentifierType 'arch'.")
    return result


def candidates_for_images(live_uuids, arch_identifier_type):
    image_type = ObjectType.query.filter_by(name="Image").first()
    if image_type is None:
        raise RuntimeError("Missing ObjectType 'Image'.")
    objects = (
        Object.query
        .filter_by(type_id=image_type.id)
        .all()
    )
    candidates = []
    without_arch = 0
    for obj in objects:
        values = {
            ident.value for ident in obj.identifiers
            if ident.type_id == arch_identifier_type.id and ident.value
        }
        if not values:
            without_arch += 1
        elif values.isdisjoint(live_uuids):
            candidates.append(obj)
    return candidates, without_arch


def candidates_for_collections(live_uuids, arch_identifier_type):
    candidates = []
    skipped_non_archipelago_sources = 0
    for compound in CompoundObject.query.all():
        values = {
            ident.value for ident in compound.object.identifiers
            if ident.type_id == arch_identifier_type.id and ident.value
        }
        if not values or not values.isdisjoint(live_uuids):
            continue
        if any(source.source_system != "archipelago" for source in compound.source_records):
            skipped_non_archipelago_sources += 1
            continue
        candidates.append(compound)
    return candidates, skipped_non_archipelago_sources


def delete_image(obj):
    CompoundObjectItem.query.filter_by(object_id=obj.id).delete(synchronize_session=False)
    Identifier.query.filter_by(object_id=obj.id).delete(synchronize_session=False)
    db.session.delete(obj)


def delete_compound(compound):
    CompoundObjectItem.query.filter_by(compound_object_id=compound.id).delete(synchronize_session=False)
    CompoundObjectSourceRecord.query.filter_by(compound_object_id=compound.id).delete(
        synchronize_session=False
    )
    Identifier.query.filter_by(object_id=compound.object_id).delete(synchronize_session=False)
    db.session.delete(compound)
    db.session.delete(compound.object)


def main():
    args = parse_args()
    live_uuids = load_completed_snapshot(args.kind)
    with app.app_context():
        type_arch = arch_type()
        if args.kind == "images":
            candidates, without_arch = candidates_for_images(live_uuids, type_arch)
            for obj in candidates:
                values = [ident.value for ident in obj.identifiers if ident.type_id == type_arch.id]
                print(f"image object_id={obj.id} arch={','.join(values)}")
            print(
                f"Image prune candidates: {len(candidates)}; image objects without an arch UUID: "
                f"{without_arch}."
            )
            if args.apply:
                for obj in candidates:
                    delete_image(obj)
        else:
            candidates, skipped_non_archipelago_sources = candidates_for_collections(
                live_uuids, type_arch
            )
            for compound in candidates:
                values = [
                    ident.value for ident in compound.object.identifiers
                    if ident.type_id == type_arch.id
                ]
                print(f"compound_object_id={compound.id} object_id={compound.object_id} arch={','.join(values)}")
            print(
                f"Collection prune candidates: {len(candidates)}; skipped because they have "
                f"a non-Archipelago source: {skipped_non_archipelago_sources}."
            )
            if args.apply:
                for compound in candidates:
                    delete_compound(compound)

        if not args.apply:
            print("Dry run only: pass --apply to delete these records.")
            return
        db.session.commit()
        print("Prune complete.")


if __name__ == "__main__":
    main()
