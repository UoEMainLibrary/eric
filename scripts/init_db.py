import bootstrap
from app import app, db, IdentifierType, ObjectType

def init_identifier_types():
    """Insert default identifier types if not present."""
    defaults = [
        ("ark", "Archival Resource Key", "https://n2t.net/ark:/83794/<id>"),
        ("luna", "LUNA Image ID", "https://images.is.ed.ac.uk/luna/servlet/detail/<id>"),
        ("arch", "Archipelago UUID", "https://digital.collections.ed.ac.uk/do/<id>"),
        ("file", "Source Filename", None),
        ("cantaloupe", "IIIF Cantaloupe ID", 
         "https://digital.collections.ed.ac.uk/cantaloupe/iiif/2/<id>/full/600,/0/default.jpg")
    ]

    for shortcode, desc, url in defaults:
        row = IdentifierType.query.filter_by(shortcode=shortcode).first()
        if not row:
            db.session.add(
                IdentifierType(
                    shortcode=shortcode,
                    description=desc,
                    url_construct=url
                )
            )
    db.session.commit()
    print("Identifier types initialized.")


def init_object_types():
    """Insert minimal default object types."""
    defaults = [
        ("Image", None),  # no URL construct at object level
    ]

    for name, url in defaults:
        row = ObjectType.query.filter_by(name=name).first()
        if not row:
            db.session.add(
                ObjectType(
                    name=name,
                    url_construct=url
                )
            )
    db.session.commit()
    print("Object types initialized.")


if __name__ == "__main__":
    with app.app_context():
        print("Creating tables (if not existing)…")
        db.create_all()

        init_identifier_types()
        init_object_types()

        print("Database initialization complete.")
