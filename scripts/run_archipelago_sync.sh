#!/usr/bin/env bash
# Run the complete, snapshot-based Archipelago synchronisation in dependency order.
# Intended for cron: it logs all output and refuses to overlap another run.

set -Eeuo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR="$PROJECT_ROOT/data/logs"
LOG_FILE="$LOG_DIR/archipelago_sync.log"
LOCK_FILE="$LOG_DIR/archipelago_sync.lock"
PYTHON_BIN=${PYTHON_BIN:-python3}
MODE=${1:---full}

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

timestamp() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

log() {
    printf '%s %s\n' "$(timestamp)" "$*"
}

format_duration() {
    local total_seconds=$1
    printf '%02dh:%02dm:%02ds' \
        "$((total_seconds / 3600))" \
        "$(((total_seconds % 3600) / 60))" \
        "$((total_seconds % 60))"
}

RUN_STARTED_AT=$(timestamp)
RUN_STARTED_EPOCH=$(date +%s)
CURRENT_STEP='initialisation'

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "RUN_SKIPPED reason=another_run_is_active"
    exit 0
fi

on_error() {
    local exit_status=$?
    local finished_epoch
    finished_epoch=$(date +%s)
    log "RUN_FAILED step=$CURRENT_STEP exit_status=$exit_status finished_at=$(timestamp) duration=$(format_duration "$((finished_epoch - RUN_STARTED_EPOCH))")"
    exit "$exit_status"
}
trap on_error ERR

run_step() {
    local step=$1
    shift
    local step_started_epoch step_finished_epoch
    CURRENT_STEP=$step
    step_started_epoch=$(date +%s)
    log "STEP_STARTED step=$step"
    "$@"
    step_finished_epoch=$(date +%s)
    log "STEP_COMPLETE step=$step finished_at=$(timestamp) duration=$(format_duration "$((step_finished_epoch - step_started_epoch))")"
}

cd "$PROJECT_ROOT"
case "$MODE" in
    --full|--incremental) ;;
    *)
        log "RUN_FAILED reason=invalid_mode mode=$MODE"
        exit 2
        ;;
esac
log "RUN_STARTED started_at=$RUN_STARTED_AT project_root=$PROJECT_ROOT mode=$MODE"

if [[ "$MODE" == '--full' ]]; then
    # A fresh snapshot is required before pruning; a failed snapshot stops the
    # run before any deletion is attempted.
    run_step snapshot_images "$PYTHON_BIN" scripts/snapshot_archipelago_uuids.py images --fresh
    run_step snapshot_collections "$PYTHON_BIN" scripts/snapshot_archipelago_uuids.py collections --fresh
    run_step prune_images "$PYTHON_BIN" scripts/prune_archipelago_snapshot.py images --apply
    run_step prune_collections "$PYTHON_BIN" scripts/prune_archipelago_snapshot.py collections --apply
    run_step sync_new_images "$PYTHON_BIN" scripts/sync_archipelago_images.py --from-snapshot
    run_step sync_collections "$PYTHON_BIN" scripts/sync_archipelago_collections.py
    run_step enrich_compound_objects "$PYTHON_BIN" scripts/sync_archipelago_compound_object_catalogue_data.py
    run_step sync_compound_memberships "$PYTHON_BIN" scripts/sync_archipelago_collection_items.py
else
    run_step sync_changed_images "$PYTHON_BIN" scripts/sync_archipelago_images.py --since-last-success
    run_step sync_collections "$PYTHON_BIN" scripts/sync_archipelago_collections.py
    run_step enrich_compound_objects "$PYTHON_BIN" scripts/sync_archipelago_compound_object_catalogue_data.py
    run_step sync_changed_compound_memberships "$PYTHON_BIN" scripts/sync_archipelago_collection_items.py --since-last-success
fi

RUN_FINISHED_EPOCH=$(date +%s)
log "RUN_COMPLETE finished_at=$(timestamp) duration=$(format_duration "$((RUN_FINISHED_EPOCH - RUN_STARTED_EPOCH))")"
log "RUN_LOG_REVIEW search='ATTENTION:|RUN_FAILED|Skipping |Warning:' log_file=$LOG_FILE"
