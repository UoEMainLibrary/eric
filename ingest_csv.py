import csv
import uuid
from app import app, db, Identifier, IdentifierType, ImageDimensions, Object, ObjectType, mint_ark


def parse_optional_int(value):
    if value is None:
        return None

    value = str(value).strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None

with app.app_context():

    with open("recent_items.csv") as f:
        reader = csv.DictReader(f)

        for row in reader:

            # 1. Get or create the object type
            obj_type = ObjectType.query.filter_by(name=row['object_type']).first()
            if not obj_type:
                print(f"ERROR: ObjectType '{row['object_type']}' does not exist in DB!")
                continue

            # 2. Create object with proper UUID
            obj = Object(
                uuid=uuid.uuid4(),
                type_id=obj_type.id,
                primary_id=row['luna']
            )
            db.session.add(obj)
            db.session.commit()

            width = parse_optional_int(row.get("width"))
            height = parse_optional_int(row.get("height"))
            if width is not None and height is not None:
                db.session.add(
                    ImageDimensions(
                        object_id=obj.id,
                        width=width,
                        height=height,
                    )
                )

            # 3. Insert identifiers for this object
            ids_to_add = {
                "luna": row["luna"],
                "arch": row["arch"],
                "file": row["file"],
                "cantaloupe": row["cantaloupe"]
            }

            for shortcode, value in ids_to_add.items():
                id_type = IdentifierType.query.filter_by(shortcode=shortcode).first()
                if not id_type:
                    print(f"ERROR: Missing identifier type: {shortcode}")
                    continue

                db.session.add(
                    Identifier(
                        id=value,
                        object_id=obj.id,
                        type_id=id_type.id
                    )
                )

            # 4. Create ARK
            ark_type = IdentifierType.query.filter_by(shortcode="ark").first()
            ark_id = mint_ark()

            db.session.add(
                Identifier(
                    id=ark_id,
                    object_id=obj.id,
                    type_id=ark_type.id
                )
            )

            db.session.commit()

        print("CSV ingestion complete!")
