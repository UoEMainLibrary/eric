import csv
import uuid
from datetime import datetime, timezone

import bootstrap
from app import app, db, Identifier, IdentifierType, Object, ObjectType, mint_ark
from project_paths import RECENT_ITEMS_CSV


def normalise_identifier_value(value):
    if value is None:
        return None

    value = str(value).strip()
    return value or None


def parse_source_created_at(value):
    value = normalise_identifier_value(value)
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def preferred_primary_id(row):
    return (
        normalise_identifier_value(row.get("luna"))
        or normalise_identifier_value(row.get("arch"))
        or normalise_identifier_value(row.get("file"))
        or normalise_identifier_value(row.get("cantaloupe"))
    )


def find_existing_object(row, identifier_types):
    lookup_order = ("arch", "luna", "file", "cantaloupe")

    for shortcode in lookup_order:
        value = normalise_identifier_value(row.get(shortcode))
        if not value:
            continue

        id_type = identifier_types.get(shortcode)
        if not id_type:
            continue

        ident = Identifier.query.filter_by(value=value, type_id=id_type.id).first()
        if ident:
            return ident.object

    return None


def ensure_identifier(obj, id_type, value):
    existing = Identifier.query.filter_by(value=value).first()
    if existing:
        if existing.object_id != obj.id or existing.type_id != id_type.id:
            print(
                f"ERROR: Identifier {value!r} already belongs to object_id={existing.object_id} "
                f"(type_id={existing.type_id}), not object_id={obj.id}"
            )
            return False
        return False

    db.session.add(
        Identifier(
            value=value,
            object_id=obj.id,
            type_id=id_type.id,
        )
    )
    return True

def main():
    with app.app_context():
        identifier_types = {
            row.shortcode: row
            for row in IdentifierType.query.all()
        }

        ark_type = identifier_types.get("ark")
        inserted_objects = 0
        reused_objects = 0
        inserted_identifiers = 0
        inserted_arks = 0
        skipped_rows = 0

        with open(RECENT_ITEMS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row in reader:

                # 1. Get or create the object type
                obj_type = ObjectType.query.filter_by(name=row['object_type']).first()
                if not obj_type:
                    print(f"ERROR: ObjectType '{row['object_type']}' does not exist in DB!")
                    continue

                # 2. Find or create object with proper UUID
                obj = find_existing_object(row, identifier_types)
                if obj:
                    reused_objects += 1
                else:
                    obj = Object(
                        uuid=uuid.uuid4(),
                        type_id=obj_type.id,
                        primary_id=preferred_primary_id(row),
                        source_created_at=parse_source_created_at(row.get("source_created_at")),
                    )
                    db.session.add(obj)
                    db.session.flush()
                    inserted_objects += 1

                desired_primary_id = preferred_primary_id(row)
                if desired_primary_id and obj.primary_id != desired_primary_id:
                    obj.primary_id = desired_primary_id

                source_created_at = parse_source_created_at(row.get("source_created_at"))
                if source_created_at and obj.source_created_at != source_created_at:
                    obj.source_created_at = source_created_at

                # 3. Insert identifiers for this object
                ids_to_add = {
                    "luna": row["luna"],
                    "arch": row["arch"],
                    "file": row["file"],
                    "cantaloupe": row["cantaloupe"]
                }

                for shortcode, value in ids_to_add.items():
                    value = normalise_identifier_value(value)
                    if not value:
                        print(f"Warning: Skipping blank {shortcode} identifier for file {row.get('file', '')}")
                        continue

                    id_type = identifier_types.get(shortcode)
                    if not id_type:
                        print(f"ERROR: Missing identifier type: {shortcode}")
                        continue

                    if ensure_identifier(obj, id_type, value):
                        inserted_identifiers += 1

                # 4. Create ARK
                if not ark_type:
                    print("ERROR: Missing identifier type: ark")
                    skipped_rows += 1
                    db.session.rollback()
                    continue

                existing_ark = Identifier.query.filter_by(object_id=obj.id, type_id=ark_type.id).first()
                if not existing_ark:
                    ark_id = mint_ark()
                    db.session.add(
                        Identifier(
                            value=ark_id,
                            object_id=obj.id,
                            type_id=ark_type.id
                        )
                    )
                    inserted_arks += 1

                db.session.commit()

            print(
                "CSV ingestion complete! "
                f"inserted_objects={inserted_objects} reused_objects={reused_objects} "
                f"inserted_identifiers={inserted_identifiers} "
                f"inserted_arks={inserted_arks} skipped_rows={skipped_rows}"
            )


if __name__ == "__main__":
    main()
