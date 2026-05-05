# ERIC Routing Notes

These notes capture planned routing behaviour for legacy image links. They are
implementation sketches, not currently active code.

## Data shape

Keep object identity data separate from legacy routing data.

The main ERIC ingest dataset should stay one row per object/image and only
carry stable identifiers such as:

```text
object_type,luna,arch,file,cantaloupe
```

TinyURL mappings should not be folded into that CSV. A single LUNA identifier
can legitimately have many TinyURLs pointing at different behaviours, for
example:

- a detail page
- a widget detail page
- a workspace
- a search result
- a browse view

That means TinyURLs are a routing concern, not an object-identity field. Store
them in a separate table (or, during preparation, a separate CSV) keyed by the
TinyURL token and preserving the route type and original target URL.

## MediaManager image URLs

Users may have stored URLs like:

```text
https://images.is.ed.ac.uk/MediaManager/srvr?mediafile=/Size4/UoEgal~4~4/520/0169775c.jpg
```

The stable bridge is the filename identifier already stored in ERIC. Treat the
final derivative filename as matching the source filename stem:

```text
0169775c.jpg -> 0169775c.tif
```

Then look up `0169775c.tif` as a `file` identifier, find the same object's
`cantaloupe` identifier, and redirect to the Cantaloupe IIIF image.

Known size mapping:

```text
Size3 -> 384 pixels on the long side
Size4 -> 768 pixels on the long side
```

Because `Size3` and `Size4` are defined in terms of the long side, ERIC needs
to know whether the source image is landscape or portrait before building the
IIIF size segment:

```text
landscape -> /384,/ or /768,/
portrait  -> /,384/ or /,768/
```

That orientation decision should not depend on LUNA being available at request
time. The safer approach is:

1. During data generation in `get_data.py`, extract `width` and `height` from
   Archipelago image metadata where possible.
2. If Archipelago metadata does not expose them directly, fetch the IIIF
   `info.json` during ingest and store the dimensions in ERIC's database.
3. At redirect time, read the stored dimensions and build the IIIF size segment
   locally, falling back to `!<pixels>,<pixels>` only if no stored dimensions
   are available.

Suggested runtime route:

```python
def get_cantaloupe_base_url(cant_identifier):
    cant_url = construct_url(cant_identifier.type.url_construct, cant_identifier.id)
    if not cant_url:
        abort(404)
    return cant_url.rsplit("/", 4)[0]

def build_long_side_size_param(width, height, pixels):
    if width >= height:
        return f"{pixels},"
    return f",{pixels}"

def fetch_object_dimensions(object_id):
    dims = ImageDimensions.query.filter_by(object_id=object_id).first()
    if not dims:
        return None, None
    return dims.width, dims.height

@app.route("/MediaManager/srvr")
def media_manager():
    mediafile = request.args.get("mediafile")
    if not mediafile:
        abort(404)

    parts = mediafile.strip("/").split("/")
    if len(parts) < 2:
        abort(404)

    size = parts[0]
    filename = parts[-1]

    size_map = {
        "Size3": 384,
        "Size4": 768,
    }

    pixels = size_map.get(size)
    if not pixels:
        abort(404)

    stem = filename.rsplit(".", 1)[0]
    file_identifier = f"{stem}.tif"

    ident = Identifier.query.filter_by(id=file_identifier).first()
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

    cant_ident = next(
        (i for i in obj.identifiers if i.type.shortcode == "cantaloupe"),
        None
    )
    if not cant_ident:
        abort(404)

    width, height = fetch_object_dimensions(obj.id)
    if width is not None and height is not None:
        size_param = build_long_side_size_param(width, height, pixels)
    else:
        size_param = f"!{pixels},{pixels}"

    final_url = (
        f"{get_cantaloupe_base_url(cant_ident)}/full/{size_param}/0/default.jpg"
    )

    return redirect(final_url, code=302)
```

The bounded `!768,768` and `!384,384` forms remain a safe fallback if ERIC does
not yet have stored dimensions for the object, but the preferred redirect is
the explicit long-side form chosen from the database-backed dimensions.

For local testing with copied URLs under `/images.is.ed.ac.uk/...`, preserve the
query string in the existing simulation route:

```python
@app.route("/images.is.ed.ac.uk/<path:subpath>")
def simulate_images_host(subpath):
    qs = request.query_string.decode()
    target = f"/{subpath}"
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=302)
```

## LUNA shortlink and legacy LUNA routes

Some users have stored LUNA shortlinks like:

```text
https://images.is.ed.ac.uk/luna/servlet/s/f269k4
```

Users may also have stored other LUNA-derived TinyURLs whose targets are not
shortlink URLs, for example direct detail pages, widget detail pages, searches,
or workspaces. These should be handled by a dedicated routing table, not by a
column on the main ingest CSV.

Suggested table shape:

```text
token:       f269k4
route_type:  detail
target_url:  https://images.is.ed.ac.uk/luna/servlet/detail/UoEcar~3~3~58400~102041:The-Building-for-the-Societies,-Add?qvq=...
```

The route should not redirect to the stored LUNA URL. Instead, ERIC should
look up the stored target internally, classify what kind of legacy LUNA URL it
is, extract the LUNA identifier if one is present, and then resolve that to the
canonical Archipelago object.

Example extraction:

```text
/luna/servlet/detail/UoEcar~3~3~58400~102041:The-Building-for-the-Societies,-Add
```

becomes:

```text
UoEcar~3~3~58400~102041
```

Suggested model:

```python
class LunaRoute(db.Model):
    __tablename__ = "luna_route"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    route_type = db.Column(db.String(64), nullable=False)
    target_url = db.Column(db.String(2048), nullable=False)
```

Useful route types seen in the current TinyURL export include:

```text
detail
widget_detail
workspace
search
browse
shortlink
media_manager
thumbnail_view
luna_other
```

Suggested helper to avoid duplicating LUNA detail resolution:

```python
def redirect_luna_identifier_to_arch(identifier):
    identifier = identifier.split(":", 1)[0]

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

    arch_ident = next(
        (i for i in obj.identifiers if i.type.shortcode == "arch"),
        None
    )
    if not arch_ident:
        abort(404)

    arch_url = construct_url(arch_ident.type.url_construct, arch_ident.id)
    return redirect(arch_url, code=302)
```

Suggested helper to extract the LUNA identifier from a stored target URL:

```python
from urllib.parse import unquote, urlparse
import re


LUNA_IDENTIFIER_RE = re.compile(r"\b[A-Za-z0-9]+~\d+~\d+~\d+~\d+\b")


def extract_luna_identifier_from_target(target_url):
    parsed = urlparse(target_url)
    decoded = unquote(target_url)

    # Detail and widget-detail URLs carry the identifier in the path.
    if "/luna/servlet/detail/" in parsed.path:
        return parsed.path.split("/luna/servlet/detail/", 1)[1].split(":", 1)[0]

    if "/luna/servlet/widget/detail/" in parsed.path:
        return parsed.path.split("/luna/servlet/widget/detail/", 1)[1].split(":", 1)[0]

    # Workspace and some other URLs carry one or more identifiers in the query.
    match = LUNA_IDENTIFIER_RE.search(decoded)
    if match:
        return match.group(0)

    return None
```

Suggested updated detail route:

```python
@app.route("/luna/servlet/detail/<path:identifier>")
def luna_detail(identifier):
    return redirect_luna_identifier_to_arch(identifier)
```

Suggested TinyURL route:

```python
@app.route("/luna/servlet/s/<token>")
def luna_shortlink(token):
    row = LunaRoute.query.filter_by(token=token).first()
    if not row:
        abort(404)

    identifier = extract_luna_identifier_from_target(row.target_url)
    if not identifier:
        abort(404)

    return redirect_luna_identifier_to_arch(identifier)
```

The route uses `/s/` as the signal that the incoming URL is a stored TinyURL
token, but the row it resolves to may represent a detail page, widget, search,
workspace, or other legacy LUNA target.

If some route types later need custom handling instead of "extract identifier
and redirect to Archipelago", that branching should happen from `route_type`
rather than by trying to infer intent from a compressed token column on the main
object CSV.
