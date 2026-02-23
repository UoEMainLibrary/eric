from flask import Flask, jsonify, request, redirect, url_for, make_response, abort
from flask_sqlalchemy import SQLAlchemy
from flask_uuid import FlaskUUID
from sqlalchemy_utils import UUIDType
from sqlalchemy.orm import joinedload
from markupsafe import escape
import uuid

app = Flask(__name__)

# ------------------------------------------------------------
# DATABASE CONFIGURATION (MySQL)
# ------------------------------------------------------------
# Load config from file
app.config.from_object('config')

db = SQLAlchemy(app)
FlaskUUID(app)

# ------------------------------------------------------------
# DATABASE MODELS
# ------------------------------------------------------------
class Object(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(UUIDType(binary=False), default=uuid.uuid4, unique=True, nullable=False)
    type_id = db.Column(db.Integer, db.ForeignKey('object_type.id'), nullable=False)
    type = db.relationship('ObjectType', lazy=False, backref=db.backref('objects', lazy=True))
    primary_id = db.Column(db.String(64))

class ObjectType(db.Model):
    __tablename__ = 'object_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    url_construct = db.Column(db.String(256), nullable=True)

class Identifier(db.Model):
    id = db.Column(db.String(128), primary_key=True)
    object_id = db.Column(db.Integer, db.ForeignKey('object.id'), nullable=False)
    object = db.relationship('Object', backref=db.backref('identifiers', lazy=True))
    type_id = db.Column(db.Integer, db.ForeignKey('identifier_type.id'), nullable=False)
    type = db.relationship('IdentifierType', lazy=False, backref=db.backref('identifiers', lazy=True))

class IdentifierType(db.Model):
    __tablename__ = 'identifier_type'
    id = db.Column(db.Integer, primary_key=True)
    shortcode = db.Column(db.String(32), nullable=False)
    description = db.Column(db.String(128), nullable=False)
    url_construct = db.Column(db.String(256), nullable=True)

# ------------------------------------------------------------
# ARK MINTING
# ------------------------------------------------------------
NAAN = "83794"

def mint_ark():
    suffix = str(uuid.uuid4())
    return f"ark:/{NAAN}/{suffix}"

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def construct_url(url_format, id):
    if url_format and id:
        return url_format.replace("<id>", id)
    return None

def init_identifier_types():
    defaults = [
        ("ark", "Archival Resource Key", "https://n2t.net/ark:/83794/<id>"),
        ("luna", "LUNA Image ID", "https://images.is.ed.ac.uk/luna/servlet/detail/<id>"),
        ("arch", "Archipelago UUID", "https://digital.collections.ed.ac.uk/do/<id>"),
        ("file", "Source Filename", None),
        ("cantaloupe", "IIIF Cantaloupe ID",
         "https://digital.collections.ed.ac.uk/cantaloupe/iiif/2/<id>/full/600,/0/default.jpg")
    ]
    for shortcode, desc, url in defaults:
        if not IdentifierType.query.filter_by(shortcode=shortcode).first():
            db.session.add(IdentifierType(shortcode=shortcode, description=desc, url_construct=url))
    db.session.commit()

# ------------------------------------------------------------
# ERROR HANDLING
# ------------------------------------------------------------
@app.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'status': 404, 'error': 'Not found'}), 404)

# ------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------
@app.route('/')
def index():
    return 'Welcome to ERIC!'

@app.route('/object/<uuid:user_uuid>')
def view_object(user_uuid):
    uuid_hex = user_uuid.hex

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter(Object.uuid == uuid_hex)
        .first_or_404()
    )

    return jsonify({
        "id": obj.id,
        "uuid": obj.uuid,
        "type": obj.type.name,
        "primary_id": obj.primary_id,
        "identifiers": [
            {
                "shortcode": ident.type.shortcode,
                "description": ident.type.description,
                "identifier": ident.id
            }
            for ident in obj.identifiers
        ]
    })

@app.route('/identifier/<identifier>')
def view_identifier(identifier):
    obj = Identifier.query.get_or_404(identifier)
    return jsonify({
        "identifier": obj.id,
        "type": obj.type.description,
        "eric_uuid": str(obj.object.uuid),
        "eric_url": url_for('view_object', uuid=obj.object.uuid, _external=True),
        "url": construct_url(obj.type.url_construct, obj.id),
    })

# ------------------------------------------------------------
# LOOKUP (your existing resolver)
# ------------------------------------------------------------
@app.route("/lookup/<path:identifier_value>")
def lookup(identifier_value):
    ident = Identifier.query.filter_by(id=identifier_value).first()
    if not ident:
        return jsonify({"error": "Identifier not found"}), 404

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter_by(id=ident.object_id)
        .first()
    )
    if not obj:
        return jsonify({"error": "Object not found"}), 404

    all_ids = {}
    html_rows = []
    for i in obj.identifiers:
        url = construct_url(i.type.url_construct, i.id) or i.id
        all_ids[i.type.shortcode] = url
        html_rows.append(
            f"<tr><td>{escape(i.type.shortcode)}</td>"
            f"<td><a href='{escape(url)}'>{escape(i.id)}</a></td></tr>"
        )

    # Default redirect → ARCH record
    if request.args.get("format") is None:
        arch_ident = next((i for i in obj.identifiers if i.type.shortcode == "arch"), None)
        if arch_ident:
            arch_url = construct_url(arch_ident.type.url_construct, arch_ident.id)
            return redirect(arch_url, code=302)

    if request.args.get("format") == "html":
        html = f"""
        <html>
        <head>
            <title>Lookup: {escape(identifier_value)}</title>
            <style>
                body {{ font-family: sans-serif; margin: 40px; }}
                table {{ border-collapse: collapse; width: 80%; }}
                td, th {{ border: 1px solid #ddd; padding: 8px; }}
                th {{ background-color: #f4f4f4; }}
            </style>
        </head>
        <body>
            <h2>Object Lookup</h2>
            <p><strong>Internal UUID:</strong> {escape(obj.uuid)}</p>
            <p><strong>Primary ID:</strong> {escape(obj.primary_id)}</p>
            <table>
                <tr><th>Identifier Type</th><th>Value</th></tr>
                {''.join(html_rows)}
            </table>
        </body>
        </html>
        """
        return html

    return jsonify({
        "uuid": str(obj.uuid),
        "primary_id": obj.primary_id,
        "object_type": obj.type.name if obj.type else None,
        "identifiers": all_ids
    })

# ------------------------------------------------------------
# NEW: LUNA DETAIL → ARCH
# ------------------------------------------------------------
@app.route("/luna/servlet/detail/<identifier>")
def luna_detail(identifier):
    ident = Identifier.query.filter_by(id=identifier).first()
    if not ident:
        abort(404)

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter_by(id=ident.object_id)
        .first()
    )
    if not obj:
        abort(404)

    arch_ident = next((i for i in obj.identifiers if i.type.shortcode == "arch"), None)
    if not arch_ident:
        abort(404)

    arch_url = construct_url(arch_ident.type.url_construct, arch_ident.id)
    return redirect(arch_url, code=302)

# ------------------------------------------------------------
# NEW: LUNA IIIF → CANTALOUPE
# ------------------------------------------------------------
@app.route("/luna/servlet/iiif/<identifier>/<path:iiif_params>")
def luna_iiif(identifier, iiif_params):
    ident = Identifier.query.filter_by(id=identifier).first()
    if not ident:
        abort(404)

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter_by(id=ident.object_id)
        .first()
    )
    if not obj:
        abort(404)

    cant_ident = next((i for i in obj.identifiers if i.type.shortcode == "cantaloupe"), None)
    if not cant_ident:
        abort(404)

    # Base URL from DB is ".../<id>/full/600,/0/default.jpg"
    cant_url = construct_url(cant_ident.type.url_construct, cant_ident.id)

    # Trim everything after the identifier
    cant_url = cant_url.rsplit("/", 4)[0]

    final_url = f"{cant_url}/{iiif_params}"
    return redirect(final_url, code=302)

# ------------------------------------------------------------
# NEW: Test images.is.ed.ac.uk paths without DNS changes
# ------------------------------------------------------------
@app.route("/images.is.ed.ac.uk/<path:subpath>")
def simulate_images_host(subpath):
    return redirect(f"/{subpath}", code=302)


# ------------------------------------------------------------
# ARK RESOLUTION (simple version)
# ------------------------------------------------------------
@app.route("/ark:/<naan>/<path:suffix>")
def resolve_ark(naan, suffix):
    if naan != NAAN:
        abort(404)

    full_ark = f"ark:/{naan}/{suffix}"

    ident = Identifier.query.filter_by(id=full_ark).first()
    if not ident:
        abort(404)

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter_by(id=ident.object_id)
        .first()
    )
    if not obj:
        abort(404)

    # Find Archipelago identifier
    arch_ident = next(
        (i for i in obj.identifiers if i.type.shortcode == "arch"),
        None
    )

    if not arch_ident:
        abort(404)

    arch_url = construct_url(arch_ident.type.url_construct, arch_ident.id)

    return redirect(arch_url, code=302)

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        init_identifier_types()
    app.run(debug=True)

