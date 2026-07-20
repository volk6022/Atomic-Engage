#!/usr/bin/env bash
#
# backup_all.sh — dump every provisioned Atomic Engage client DB, then push to a
# remote via rclone (M4 hybrid isolation / backups).
#
# For each instance in instances/registry.json:
#   docker exec postgres-<client_id> pg_dump -U <db_user> <db_name>
#     | gzip > $BACKUP_ROOT/<client_id>/<YYYYmmdd-HHMMSS>.sql.gz
# then `rclone copy` the whole tree to $RCLONE_REMOTE and prune local dumps
# older than $RETENTION_DAYS (30-day rotation; remote is the long-term archive).
#
# postgres is internal-only per compose.client.template.yml, so dumps go through
# `docker exec` into each per-client container — no host port needed.
#
# rclone OAuth (Google Drive etc.) is Ivan's one-time manual step:
#   rclone config   # create a remote named to match $RCLONE_REMOTE's prefix
# Leave RCLONE_REMOTE empty to run local-only dumps (skips the upload).
#
# Usage:
#   ./backup_all.sh
#   RCLONE_REMOTE=gdrive-engage:/backups BACKUP_ROOT=/backups RETENTION_DAYS=30 ./backup_all.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_ROOT="$(dirname "$SCRIPT_DIR")"

REGISTRY="${REGISTRY:-$FLEET_ROOT/instances/registry.json}"
BACKUP_ROOT="${BACKUP_ROOT:-/backups}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"          # e.g. gdrive-engage:/backups ; empty => local only
RETENTION_DAYS="${RETENTION_DAYS:-30}"
LOG_FILE="${LOG_FILE:-/var/log/atomic-engage-backup.log}"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE" 2>/dev/null || echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }

command -v jq   >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

[ -f "$REGISTRY" ] || { echo "registry not found: $REGISTRY" >&2; exit 1; }

log "Starting backup; registry=$REGISTRY root=$BACKUP_ROOT retention=${RETENTION_DAYS}d"

ts="$(date +%Y%m%d-%H%M%S)"
rc=0

# .instances is an object keyed by client_id; iterate its values.
# Process-substitution (not a pipe) so `rc` updates in THIS shell, not a subshell.
while IFS='|' read -r client_id db_user db_name status; do
    [ -n "$client_id" ] || continue
    container="postgres-${client_id}"
    out_dir="${BACKUP_ROOT}/${client_id}"
    out_file="${out_dir}/${ts}.sql.gz"
    mkdir -p "$out_dir"

    if ! docker ps --format '{{.Names}}' | grep -qx "$container"; then
        log "SKIP ${client_id}: container ${container} not running (status=${status})"
        continue
    fi

    log "Dumping ${client_id} (db=${db_name}) -> ${out_file}"
    if docker exec "$container" pg_dump -U "$db_user" "$db_name" | gzip > "$out_file"; then
        sz="$(du -h "$out_file" | cut -f1)"
        log "OK ${client_id}: ${out_file} (${sz})"
    else
        log "FAIL ${client_id}: pg_dump failed"
        rm -f "$out_file"
        rc=1
    fi
done < <(jq -r '.instances[] | "\(.client_id)|\(.db_user)|\(.db_name)|\(.status)"' "$REGISTRY")

if [ -n "$RCLONE_REMOTE" ]; then
    command -v rclone >/dev/null || { log "rclone not installed; skipping upload"; exit "$rc"; }
    log "Uploading ${BACKUP_ROOT} -> ${RCLONE_REMOTE}"
    if rclone copy "$BACKUP_ROOT" "$RCLONE_REMOTE" --exclude "*.tmp" --update 2>&1 | tee -a "$LOG_FILE"; then
        log "Upload OK"
    else
        log "Upload FAILED"
        rc=1
    fi
else
    log "RCLONE_REMOTE empty; local-only backup (no upload)."
fi

# 30-day rotation: prune local dumps only; the remote keeps the long-term archive.
log "Pruning local dumps older than ${RETENTION_DAYS} days"
find "$BACKUP_ROOT" -name "*.sql.gz" -type f -mtime "+${RETENTION_DAYS}" -print -delete \
    | tee -a "$LOG_FILE" 2>/dev/null || true

log "Backup cycle complete (rc=${rc})."
exit "$rc"
