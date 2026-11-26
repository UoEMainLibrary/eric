"""Shared parsing and matching helpers for DSpace import/export scripts."""

import re
from collections import defaultdict
from urllib.parse import unquote, urlparse


def shelfmark_key(value):
    """Compare shelfmarks without case, spaces, or full stops."""
    return (value or "").upper().replace(" ", "").replace(".", "")


def compound_lookup(compounds):
    """Index compound rows by the forgiving shelfmark comparison key."""
    lookup = defaultdict(list)
    for compound in compounds:
        key = shelfmark_key(compound.shelfmark_normalised)
        if key:
            lookup[key].append(compound)
    return lookup


def modern_dashless_ark(ark):
    """Convert ERIC's stored classic-form ARK to a modern, dashless ARK."""
    if not ark or not ark.startswith("ark:/"):
        raise ValueError(f"Expected classic-form ARK, got {ark!r}")
    modern_ark = f"ark:{ark[5:]}"
    prefix, ark_name = modern_ark.split("/", 1)
    return f"{prefix}/{ark_name.replace('-', '')}"


def n2t_manifest_url(ark):
    return f"https://n2t.net/{modern_dashless_ark(ark)}/manifest"


def dspace_handle_from_uri(value):
    """Extract the canonical ``10683/<suffix>`` handle from a DSpace URI."""
    uri = (value or "").strip()
    if not uri:
        return None

    decoded = unquote(uri)
    handle_match = re.search(r"(?:^|/)\s*(10683/\d+)(?:/|$)", decoded)
    if handle_match:
        return handle_match.group(1)

    # Some exports use the public Collections record URL rather than a Handle.
    # Its numeric record suffix maps to the University of Edinburgh 10683 prefix.
    path = urlparse(decoded).path
    record_match = re.search(r"/record/?(\d+)(?:/|$)", path, re.IGNORECASE)
    if record_match:
        return f"10683/{record_match.group(1)}"
    return None
