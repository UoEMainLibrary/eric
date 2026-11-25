# ERIC: Edinburgh Resource Identifier Collator
Disambiguator for various different identifier types used in University of Edinburgh Digital Libraries systems

Mike's original ERIC has been expanded to take into account
- LUNA URLs
- Archipelago Digital Objects
- Archipelago Cantloupe URLs
- IIIF Manifests
- ARKs

A massive thank you is due to him for getting us going.

Project layout:
- `app.py` is the Flask web app.
- `scripts/` contains operational/admin scripts.
- `data/input/` contains source input files such as `tinyurls.csv`.
- `data/output/` contains generated CSV exports.
- `data/logs/` and `data/db/` hold local runtime artifacts.
- `docs/` and `archive/` keep notes and old snapshots out of the app root.

Useful commands:
- `python3 scripts/init_db.py` creates the current schema and seed rows.
- `python3 scripts/repair_ark_url_construct.py` previews the one-off repair for
  the ARK link template; add `--apply` to update the existing database row.
- `python3 scripts/rekey_arks_with_shoulders.py` previews re-keying existing
  unshouldered UUID ARKs to their `a-`, `b-`, `m-`, or `d-` canonical
  forms. Apply migration 005 first, review its output, then add `--apply` to
  retain the old ARKs as resolver aliases while writing the new canonical ARKs.
  Archives objects are deferred by default, pending their authoritative
  ArchivesSpace ARKs.
- `python3 scripts/backfill_image_dimensions.py` fetches missing IIIF source
  dimensions for existing image objects. Run a small dry-run batch first, then
  use `--apply` to persist them after migration 006. Add `--luna <identifier>`
  to fill one known image first.
- `python3 scripts/get_data.py --resume` crawls `dams-live2` oldest-first and resumes from the last completed source page using `data/output/get_data_checkpoint.json`.
- `python3 scripts/get_data.py --clear-csvs --resume` clears the generated export CSVs but keeps the crawl checkpoint, so the next batch starts where the previous crawl left off.
- `python3 scripts/get_data.py --fresh` clears the generated export CSVs and checkpoint before starting a new crawl from the beginning.
- `python3 scripts/ingest_csv.py` is safe to rerun; it reuses existing objects and only fills in missing identifiers, dimensions, and ARKs.
- `python3 scripts/sync_archipelago_images.py --dry-run` sweeps Archipelago directly and previews image reconciliation without committing.
- `python3 scripts/sync_archipelago_images.py --max-pages 1 --dry-run` is the quickest way to time one page of image-sync work.
- `python3 scripts/sync_archipelago_images.py --resume` continues an interrupted full sync from `data/output/image_sync_checkpoint.json`.
- `python3 scripts/sync_archipelago_images.py --from-snapshot --dry-run` previews reconciliation of only live Archipelago image UUIDs absent from ERIC. It requires a completed `images` snapshot and is the intended incremental cron mode.
- `python3 scripts/sync_archipelago_images.py --repair-missing-cantaloupe-from-snapshot --dry-run` previews Cantaloupe-ID repair only for live ERIC images which do not yet have one.
- `python3 scripts/snapshot_archipelago_uuids.py images` creates a resumable,
  file-backed snapshot of the live image UUIDs. Use `--resume` after an
  interrupted run; a completed snapshot must be deliberately replaced with
  `--fresh`.
- `python3 scripts/prune_archipelago_snapshot.py images` previews image
  objects whose Archipelago UUID is absent from a completed snapshot. Add
  `--apply` only after reviewing the report. The equivalent `collections`
  commands handle Compound Objects.
- `python3 scripts/ingest_luna_routes.py` loads the MariaDB TinyURL export at
  `data/tinyurls.txt` into `luna_route`. It is safe to re-run: existing tokens
  are updated only when their stored route changes. Use `--dry-run` to verify
  the source first, or pass `data/output/recent_item_tinyurls.csv` to import
  the generated CSV workflow instead.
- `python3 scripts/backfill_compound_object_reference_identifiers.py` previews
  the domain-scoped compound-object reference identifiers derived from existing
  shelfmark values. Run it with `--apply` after migration 004 to write the
  unambiguous rows.
- `python3 scripts/ingest_luna_book_identifiers.py` previews the exceptional
  LUNA book-reader IDs that resolve to compound-object manifests. Run it with
  `--apply` after migration 004 to write the unambiguous rows.
- `python3 scripts/export_vernon_manifests.py` writes a Vernon XML import file
  containing each Vernon-linked compound object's current ARK and its modern,
  dashless N2T manifest URL. Use `--dry-run` to review the record count first.
