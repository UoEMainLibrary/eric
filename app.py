# ERIC is a Flask resolver for University of Edinburgh Digital Libraries
# identifiers, linking LUNA, Archipelago, IIIF/Cantaloupe, filenames, and ARKs
# for the same digital objects.
# This file is version controlled; prefer small, reviewable changes with clear
# intent so routing and identifier-resolution behaviour can be traced over time.

from flask import Flask, jsonify, request, redirect, url_for, make_response, abort
from flask_sqlalchemy import SQLAlchemy
from flask_uuid import FlaskUUID
from sqlalchemy import func
from sqlalchemy_utils import UUIDType
from sqlalchemy.orm import joinedload
from markupsafe import escape
from urllib.parse import unquote, urlparse
from datetime import datetime
import re
import uuid

app = Flask(__name__)
app.url_map.strict_slashes = False

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
    source_created_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class ObjectType(db.Model):
    __tablename__ = 'object_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    url_construct = db.Column(db.String(256), nullable=True)

class Identifier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.String(128), unique=True, nullable=False, index=True)
    object_id = db.Column(db.Integer, db.ForeignKey('object.id'), nullable=False)
    object = db.relationship('Object', backref=db.backref('identifiers', lazy=True))
    type_id = db.Column(db.Integer, db.ForeignKey('identifier_type.id'), nullable=False)
    type = db.relationship('IdentifierType', lazy=False, backref=db.backref('identifiers', lazy=True))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class IdentifierType(db.Model):
    __tablename__ = 'identifier_type'
    id = db.Column(db.Integer, primary_key=True)
    shortcode = db.Column(db.String(32), nullable=False)
    description = db.Column(db.String(128), nullable=False)
    url_construct = db.Column(db.String(256), nullable=True)

class LunaRoute(db.Model):
    __tablename__ = "luna_route"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    route_type = db.Column(db.String(64), nullable=False)
    target_url = db.Column(db.String(2048), nullable=False)

# ------------------------------------------------------------
# ARK MINTING
# ------------------------------------------------------------
NAAN = "83794"
LUNA_IDENTIFIER_RE = re.compile(r"\b[A-Za-z0-9]+~\d+~\d+~\d+~\d+\b")
BROWSER_SAFE_LONG_SIDE_PIXELS = 1536

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

def normalise_identifier_value(identifier):
    return (identifier or "").strip().rstrip("/")

def fetch_object_for_identifier(identifier):
    identifier = normalise_identifier_value(identifier)
    ident = Identifier.query.filter_by(value=identifier).first()
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

    return ident, obj

def fetch_object_for_file_identifier(identifier):
    ident = (
        Identifier.query
        .join(Identifier.type)
        .filter(IdentifierType.shortcode == "file")
        .filter(func.lower(Identifier.value) == identifier.lower())
        .first()
    )
    if not ident:
        return None, None

    obj = (
        Object.query
        .options(joinedload(Object.identifiers).joinedload(Identifier.type))
        .filter_by(id=ident.object_id)
        .first()
    )
    if not obj:
        abort(404)

    return ident, obj

def fetch_object_for_media_filename(filename):
    stem = filename.rsplit(".", 1)[0]
    candidates = [
        filename,
        f"{stem}.tif",
        f"{stem}.jp2",
        f"{stem}.jpg",
        f"{stem}.jpeg",
    ]

    seen = set()
    for candidate in candidates:
        normalised = candidate.lower()
        if normalised in seen:
            continue
        seen.add(normalised)

        ident, obj = fetch_object_for_file_identifier(candidate)
        if ident and obj:
            app.logger.info(
                "MediaManager matched filename %s to file identifier %s (object_id=%s)",
                filename,
                ident.value,
                obj.id,
            )
            return ident, obj

    stem_matches = (
        Identifier.query
        .join(Identifier.type)
        .filter(IdentifierType.shortcode == "file")
        .filter(func.lower(Identifier.value).like(f"{stem.lower()}.%"))
        .limit(2)
        .all()
    )
    if len(stem_matches) == 1:
        app.logger.info(
            "MediaManager matched filename %s via unique stem fallback to %s",
            filename,
            stem_matches[0].value,
        )
        return fetch_object_for_identifier(stem_matches[0].value)

    app.logger.warning(
        "MediaManager could not match filename %s. Candidates tried=%s stem_matches=%s",
        filename,
        candidates,
        [match.value for match in stem_matches],
    )
    abort(404)

def get_identifier_by_shortcode(obj, shortcode):
    return next((i for i in obj.identifiers if i.type.shortcode == shortcode), None)

def get_cantaloupe_base_url(cant_identifier):
    cant_url = construct_url(cant_identifier.type.url_construct, cant_identifier.value)
    if not cant_url:
        abort(404)
    return cant_url.rsplit("/", 4)[0]

def normalise_iiif_size_request(iiif_params, object_id):
    parts = iiif_params.split("/", 3)
    if len(parts) < 4:
        return iiif_params

    region, size, rotation, remainder = parts
    if region != "full" or size != "full":
        return iiif_params

    safe_size = f"!{BROWSER_SAFE_LONG_SIDE_PIXELS},{BROWSER_SAFE_LONG_SIDE_PIXELS}"
    app.logger.info(
        "Rewriting IIIF full/full request for object_id=%s to bounded browser-safe size %s",
        object_id,
        safe_size,
    )
    return f"{region}/{safe_size}/{rotation}/{remainder}"

def redirect_luna_identifier_to_arch(identifier):
    identifier = normalise_identifier_value(identifier).split(":", 1)[0]
    _, obj = fetch_object_for_identifier(identifier)

    arch_ident = get_identifier_by_shortcode(obj, "arch")
    if not arch_ident:
        abort(404)

    arch_url = construct_url(arch_ident.type.url_construct, arch_ident.value)
    return redirect(arch_url, code=302)

def extract_luna_identifier_from_target(target_url):
    parsed = urlparse(target_url)
    decoded = unquote(target_url)

    if "/luna/servlet/detail/" in parsed.path:
        return parsed.path.split("/luna/servlet/detail/", 1)[1].split(":", 1)[0]

    if "/luna/servlet/widget/detail/" in parsed.path:
        return parsed.path.split("/luna/servlet/widget/detail/", 1)[1].split(":", 1)[0]

    match = LUNA_IDENTIFIER_RE.search(decoded)
    if match:
        return match.group(0)

    return None

def init_identifier_types():
    defaults = [
        ("ark", "Archival Resource Key", "https://id.collections.ed.ac.uk/ark:/83794/<id>"),
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
    app.logger.warning(
        "Flask 404 generated: host=%s path=%s query=%s url=%s",
        request.host,
        request.path,
        request.query_string.decode(),
        request.url,
    )
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
                "identifier": ident.value
            }
            for ident in obj.identifiers
        ]
    })

@app.route('/identifier/<identifier>')
def view_identifier(identifier):
    identifier = normalise_identifier_value(identifier)
    obj = Identifier.query.filter_by(value=identifier).first_or_404()
    return jsonify({
        "identifier": obj.value,
        "type": obj.type.description,
        "eric_uuid": str(obj.object.uuid),
        "eric_url": url_for('view_object', uuid=obj.object.uuid, _external=True),
        "url": construct_url(obj.type.url_construct, obj.value),
    })

# ------------------------------------------------------------
# LOOKUP (your existing resolver)
# ------------------------------------------------------------
@app.route("/lookup/<path:identifier_value>")
def lookup(identifier_value):
    identifier_value = normalise_identifier_value(identifier_value)
    ident = Identifier.query.filter_by(value=identifier_value).first()
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
        url = construct_url(i.type.url_construct, i.value) or i.value
        all_ids[i.type.shortcode] = url
        html_rows.append(
            f"<tr><td>{escape(i.type.shortcode)}</td>"
            f"<td><a href='{escape(url)}'>{escape(i.value)}</a></td></tr>"
        )

    # Default redirect → ARCH record
    if request.args.get("format") is None:
        arch_ident = next((i for i in obj.identifiers if i.type.shortcode == "arch"), None)
        if arch_ident:
            arch_url = construct_url(arch_ident.type.url_construct, arch_ident.value)
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
@app.route("/luna/servlet/detail/<path:identifier>")
def luna_detail(identifier):
    return redirect_luna_identifier_to_arch(identifier)

@app.route("/luna/servlet/widget/detail/<path:identifier>")
def luna_widget_detail(identifier):
    return redirect_luna_identifier_to_arch(identifier)

# ------------------------------------------------------------
# NEW: LUNA IIIF → CANTALOUPE
# ------------------------------------------------------------
@app.route("/luna/servlet/iiif/<identifier>/<path:iiif_params>")
def luna_iiif(identifier, iiif_params):
    identifier = normalise_identifier_value(identifier)
    _, obj = fetch_object_for_identifier(identifier)

    cant_ident = get_identifier_by_shortcode(obj, "cantaloupe")
    if not cant_ident:
        abort(404)

    cant_url = get_cantaloupe_base_url(cant_ident)
    rewritten_params = normalise_iiif_size_request(iiif_params, obj.id)
    final_url = f"{cant_url}/{rewritten_params}"
    return redirect(final_url, code=302)

# ------------------------------------------------------------
# NEW: LEGACY LUNA TINYURL → ARCH
# ------------------------------------------------------------
@app.route("/luna/servlet/s/<token>")
def luna_shortlink(token):
    row = LunaRoute.query.filter_by(token=token).first()
    if not row:
        abort(404)

    identifier = extract_luna_identifier_from_target(row.target_url)
    if not identifier:
        abort(404)

    return redirect_luna_identifier_to_arch(identifier)

# ------------------------------------------------------------
# NEW: MEDIAMANAGER → CANTALOUPE
# ------------------------------------------------------------
@app.route("/MediaManager/srvr")
def media_manager():
    mediafile = request.args.get("mediafile")
    if not mediafile:
        app.logger.warning(
            "MediaManager request missing mediafile parameter: url=%s",
            request.url,
        )
        abort(404)

    parts = mediafile.strip("/").split("/")
    if len(parts) < 2:
        app.logger.warning(
            "MediaManager request has unexpected mediafile format: mediafile=%s url=%s",
            mediafile,
            request.url,
        )
        abort(404)

    size = parts[0]
    filename = parts[-1]

    size_map = {
        "Size0": 96,
        "Size1": 192,
        "Size2": 384,
        "Size3": 768,
        "Size4": 1536,
    }

    pixels = size_map.get(size)
    if not pixels:
        app.logger.warning(
            "MediaManager request uses unsupported size %s for mediafile=%s",
            size,
            mediafile,
        )
        abort(404)

    app.logger.info(
        "MediaManager request received: host=%s path=%s mediafile=%s size=%s filename=%s",
        request.host,
        request.path,
        mediafile,
        size,
        filename,
    )

    matched_ident, obj = fetch_object_for_media_filename(filename)

    cant_ident = get_identifier_by_shortcode(obj, "cantaloupe")
    if not cant_ident:
        app.logger.warning(
            "MediaManager found object_id=%s for filename=%s but no cantaloupe identifier is present",
            obj.id,
            matched_ident.value if matched_ident else filename,
        )
        abort(404)

    size_param = f"!{pixels},{pixels}"
    app.logger.info(
        "MediaManager using bounded IIIF size for object_id=%s cantaloupe=%s size_param=%s",
        obj.id,
        cant_ident.value,
        size_param,
    )

    final_url = (
        f"{get_cantaloupe_base_url(cant_ident)}/full/{size_param}/0/default.jpg"
    )
    app.logger.info(
        "MediaManager redirecting filename=%s matched_file=%s object_id=%s cantaloupe=%s final_url=%s",
        filename,
        matched_ident.value if matched_ident else None,
        obj.id,
        cant_ident.value,
        final_url,
    )

    return redirect(final_url, code=302)

# ------------------------------------------------------------
# NEW: Test images.is.ed.ac.uk paths without DNS changes
# ------------------------------------------------------------
@app.route("/images.is.ed.ac.uk/<path:subpath>")
def simulate_images_host(subpath):
    qs = request.query_string.decode()
    target = f"/{subpath}"
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=302)


# ------------------------------------------------------------
# ARK RESOLUTION WITH MULTIPLE FORMATS
# ------------------------------------------------------------
@app.route("/ark:/<naan>/<path:suffix>")
def resolve_ark(naan, suffix):
    if naan != NAAN:
        abort(404)

    full_ark = f"ark:/{naan}/{suffix}"

    ident = Identifier.query.filter_by(value=full_ark).first()
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

    # Determine requested format
    fmt = request.args.get("format")

    # --- HTML Metadata ---
    if fmt == "html":
        html_rows = []
        for i in obj.identifiers:
            url = construct_url(i.type.url_construct, i.value) or i.value
            html_rows.append(
                f"<tr><td>{escape(i.type.shortcode)}</td>"
                f"<td><a href='{escape(url)}'>{escape(i.value)}</a></td></tr>"
            )

        html = f"""
        <html>
        <head>
            <title>ARK Lookup: {escape(full_ark)}</title>
            <style>
                body {{ font-family: sans-serif; margin: 40px; }}
                table {{ border-collapse: collapse; width: 80%; }}
                td, th {{ border: 1px solid #ddd; padding: 8px; }}
                th {{ background-color: #f4f4f4; }}
            </style>
        </head>
        <body>
            <h2>ARK Lookup</h2>
            <p><strong>ARK:</strong> {escape(full_ark)}</p>
            <p><strong>Internal UUID:</strong> {escape(str(obj.uuid))}</p>
            <p><strong>Primary ID:</strong> {escape(obj.primary_id)}</p>
            <table>
                <tr><th>Identifier Type</th><th>Value</th></tr>
                {''.join(html_rows)}
            </table>
        </body>
        </html>
        """
        return html

    # --- JSON Metadata ---
    elif fmt == "json":
        all_ids = {}
        for i in obj.identifiers:
            url = construct_url(i.type.url_construct, i.value) or i.value
            all_ids[i.type.shortcode] = url

        return jsonify({
            "ark": full_ark,
            "uuid": str(obj.uuid),
            "primary_id": obj.primary_id,
            "object_type": obj.type.name if obj.type else None,
            "identifiers": all_ids
        })

    # --- Future ARK Info endpoint ---
    elif fmt == "info":
        # Minimal info page per ARK specification
        info = {
            "ark": full_ark,
            "naan": naan,
            "policy": "This ARK is maintained by University of Edinburgh Digital Collections.",
            "target": construct_url(
                next((i.type.url_construct for i in obj.identifiers if i.type.shortcode=="arch"), None),
                next((i.value for i in obj.identifiers if i.type.shortcode=="arch"), None)
            )
        }
        return jsonify(info)

    # --- Default: redirect to canonical detail page ---
    arch_ident = next((i for i in obj.identifiers if i.type.shortcode == "arch"), None)
    if not arch_ident:
        abort(404)

    arch_url = construct_url(arch_ident.type.url_construct, arch_ident.value)
    return redirect(arch_url, code=302)

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        init_identifier_types()
    app.run(debug=True)
