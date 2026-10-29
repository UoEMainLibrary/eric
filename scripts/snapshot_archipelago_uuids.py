#!/usr/bin/env python3
"""Create a resumable, file-backed snapshot of live Archipelago UUIDs.

The completed snapshot is the only input accepted by the corresponding prune
script.  A partially downloaded snapshot is never promoted to the live
manifest, so an interrupted API crawl cannot cause a destructive prune.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urljoin

import bootstrap
from project_paths import OUTPUT_DIR
from scripts.archipelago_sweep import (
    DEFAULT_HTTP_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    REQUEST_TIMEOUT,
    build_session,
    log,
)


DEFAULT_JSONAPI_ROOT = "http://lac-dams-live2.is.ed.ac.uk/jsonapi"
RESOURCE_TYPES = {
    "images": ("node/digital_object", "node--digital_object"),
    "collections": ("node/digital_object_collection", "node--digital_object_collection"),
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=sorted(RESOURCE_TYPES))
    parser.add_argument(
        "--jsonapi-root",
        default=os.environ.get("ARCHIPELAGO_JSONAPI_ROOT", DEFAULT_JSONAPI_ROOT),
    )
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--http-retries", type=int, default=DEFAULT_HTTP_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument("--resume", action="store_true", help="Continue a partial snapshot.")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Discard a partial or completed snapshot and begin again.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def snapshot_paths(kind):
    directory = OUTPUT_DIR / "archipelago_snapshots"
    return (
        directory / f"{kind}.json",
        directory / f"{kind}.partial.txt",
        directory / f"{kind}.txt",
    )


def read_state(path):
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_state(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def manifest_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def initial_url(root, kind, page_limit):
    resource, resource_type = RESOURCE_TYPES[kind]
    return f"{root.rstrip('/')}/{resource}?" + urlencode({
        f"fields[{resource_type}]": "drupal_internal__nid",
        "sort": "drupal_internal__nid",
        "page[limit]": page_limit,
    })


def main():
    args = parse_args()
    if args.page_limit < 1:
        raise SystemExit("--page-limit must be at least 1")
    if args.resume and args.fresh:
        raise SystemExit("Use only one of --resume and --fresh.")

    state_path, partial_path, manifest_path = snapshot_paths(args.kind)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(state_path)

    if args.fresh:
        for path in (state_path, partial_path, manifest_path):
            if path.exists():
                path.unlink()
        state = None

    if state and state.get("status") == "complete" and not args.fresh:
        raise SystemExit(
            f"Completed {args.kind} snapshot already exists at {manifest_path}. "
            "Use --fresh to replace it."
        )
    if state and not args.resume:
        raise SystemExit(
            f"Partial {args.kind} snapshot exists at {partial_path}. Use --resume or --fresh."
        )

    if state:
        if state.get("jsonapi_root") != args.jsonapi_root or state.get("page_limit") != args.page_limit:
            raise SystemExit("Snapshot settings differ; use --fresh to begin a new snapshot.")
        next_url = state.get("next_url")
        page_count = state.get("page_count", 0)
        uuid_count = state.get("uuid_count", 0)
    else:
        next_url = initial_url(args.jsonapi_root, args.kind, args.page_limit)
        page_count = uuid_count = 0
        state = {
            "kind": args.kind,
            "jsonapi_root": args.jsonapi_root,
            "page_limit": args.page_limit,
            "status": "running",
        }

    session = build_session(args.http_retries, args.retry_backoff)
    with partial_path.open("a", encoding="utf-8") as output:
        while next_url:
            response = session.get(next_url, timeout=args.timeout)
            response.raise_for_status()
            payload = response.json()
            rows = [row for row in payload.get("data", []) if isinstance(row, dict)]
            for row in rows:
                source_uuid = str(row.get("id") or "").strip()
                if source_uuid:
                    output.write(f"{source_uuid}\n")
                    uuid_count += 1
            output.flush()
            os.fsync(output.fileno())

            page_count += 1
            link = payload.get("links", {}).get("next", {})
            href = link.get("href") if isinstance(link, dict) else None
            next_url = urljoin(next_url, href) if isinstance(href, str) and href else None
            state.update({
                "status": "running",
                "next_url": next_url,
                "page_count": page_count,
                "uuid_count": uuid_count,
            })
            write_state(state_path, state)
            log(
                f"Snapshot {args.kind}: page={page_count} rows={len(rows)} uuids={uuid_count}",
                not args.quiet,
            )

    # A sorted, de-duplicated manifest gives pruning a stable and inspectable input.
    with partial_path.open(encoding="utf-8") as handle:
        source_uuids = sorted({line.strip() for line in handle if line.strip()})
    temporary_manifest = manifest_path.with_suffix(".tmp")
    with temporary_manifest.open("w", encoding="utf-8") as output:
        output.write("\n".join(source_uuids))
        if source_uuids:
            output.write("\n")
    temporary_manifest.replace(manifest_path)
    partial_path.unlink()

    state.update({
        "status": "complete",
        "next_url": None,
        "uuid_count": len(source_uuids),
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_digest(manifest_path),
    })
    write_state(state_path, state)
    print(f"Completed {args.kind} snapshot: {len(source_uuids)} UUIDs in {manifest_path}")


if __name__ == "__main__":
    main()
