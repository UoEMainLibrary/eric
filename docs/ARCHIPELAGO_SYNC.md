# Archipelago snapshot and reconciliation runbook

ERIC's database is not rebuilt during a routine synchronisation. Archipelago
is authoritative for its UUIDs, node IDs, filenames, Cantaloupe IDs,
collection metadata, and memberships; ERIC retains the ARKs it has minted.

## Before the first live run

1. Take a normal MariaDB backup.
2. Deploy the current code and make sure the application's Python environment
   has the dependencies in `requirements.txt`.
3. Run every command first without `--apply` where that option is offered.

## Normal full synchronisation

Create a complete snapshot of the live UUIDs. These commands write only local
files beneath `data/output/archipelago_snapshots/` and are safe to resume after
an interruption.

```bash
python3 scripts/snapshot_archipelago_uuids.py images
python3 scripts/snapshot_archipelago_uuids.py collections
```

If a snapshot is interrupted, run the same command with `--resume`. Do not use
the manifest for pruning unless the command reports completion. To deliberately
start a new snapshot after a completed run, add `--fresh`.

Review UUID-based deletion candidates:

```bash
python3 scripts/prune_archipelago_snapshot.py images
python3 scripts/prune_archipelago_snapshot.py collections
```

Only when those reports are expected, apply the pruning:

```bash
python3 scripts/prune_archipelago_snapshot.py images --apply
python3 scripts/prune_archipelago_snapshot.py collections --apply
```

The image prune removes image objects whose `arch` UUID does not appear in the
completed image snapshot, together with their identifiers and compound-object
membership rows. The collection prune removes stale compound objects, their
source rows, memberships, identifiers, and backing objects. Neither command
uses filenames as an identity key and neither changes `luna_route`.

Then refresh current source data and resolution links:

```bash
python3 scripts/sync_archipelago_images.py
python3 scripts/sync_archipelago_collections.py
python3 scripts/sync_archipelago_compound_object_catalogue_data.py
python3 scripts/sync_archipelago_collection_items.py
```

`sync_archipelago_images.py` has its own checkpoint. If a full sweep is
interrupted, continue it with `--resume`. Image deletion is deliberately not
part of this job: use the UUID snapshot/prune workflow above. For incremental
cron runs, use `--from-snapshot` after a completed image snapshot; it fetches
only live image UUIDs absent from ERIC. The three compound-object jobs are
idempotent full sweeps at present: rerunning them is safe, but they restart at
page one.

If a parser correction means existing live images need only their Cantaloupe
identifier filled, use the targeted repair mode. It does not prune or fetch the
whole collection:

```bash
python3 scripts/sync_archipelago_images.py --repair-missing-cantaloupe-from-snapshot --dry-run
python3 scripts/sync_archipelago_images.py --repair-missing-cantaloupe-from-snapshot
```

## Important behaviour

- A replacement upload with a new Archipelago UUID is a new ERIC object. The
  stale UUID is pruned before its replacement is loaded, avoiding filename
  based accidental merging.
- Archipelago cannot authoritatively supply LUNA identifiers. The backfill
  retains an existing LUNA identifier or attempts a safe LUNA lookup by
  filename; unresolved lookups are reported rather than treated as deletion.
- A missing/incomplete API sweep must never be used for pruning. The pruning
  script verifies the completed snapshot state and its SHA-256 checksum before
  it even queries the database.
