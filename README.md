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
- `python3 scripts/get_data.py --resume` crawls `dams-live2` oldest-first and resumes from the last completed source page using `data/output/get_data_checkpoint.json`.
- `python3 scripts/ingest_csv.py` is safe to rerun; it reuses existing objects and only fills in missing identifiers, dimensions, and ARKs.
- `python3 scripts/ingest_luna_routes.py` loads the generated TinyURL route CSV into the database.
