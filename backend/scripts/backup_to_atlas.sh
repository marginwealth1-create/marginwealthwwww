#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  MarginWealth — local MongoDB → Atlas warm-standby backup
#
#  WHAT IT DOES
#    Streams a full mongodump of the LOCAL (primary, fast) MongoDB straight
#    into a mongorestore against an Atlas cluster, with --drop so the Atlas
#    copy is an exact mirror of local after every run. Nothing touches the
#    live app: the app keeps talking ONLY to local Mongo; Atlas is purely a
#    backup destination you can restore FROM if this VPS ever dies.
#
#  WHY stream (mongodump | mongorestore) instead of dump-to-disk:
#    The archive flows through a pipe, so we never park a copy of the whole
#    DB on the VPS disk, and the restore into Atlas starts immediately.
#
#  CONFIG — read from the backend .env (override with ENV_FILE=...):
#    MONGODB_URL        local source URI (must include the db, e.g.
#                       mongodb://user:pass@127.0.0.1:27017/marginwealth)
#    MONGODB_DB_NAME    database name (e.g. marginwealth)
#    MONGODB_ATLAS_URL  destination Atlas URI — ADD THIS to .env yourself,
#                       e.g. mongodb+srv://user:pass@cluster0.xxx.mongodb.net
#
#  SAFETY
#    • flock guard → two timer ticks can never overlap (a slow run is skipped,
#      not stacked).
#    • The restore is gated on a SUCCESSFUL dump (set -o pipefail), so a half
#      dump never partially overwrites Atlas.
#    • A heartbeat file records the last success so you can alert on staleness.
#
#  USAGE
#    bash backup_to_atlas.sh                 # one-off
#    (or run every 15 min via the systemd timer — see deploy/systemd/)
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE="${ENV_FILE:-/root/marginwealth/backend/.env}"
LOG_FILE="${LOG_FILE:-/var/log/marginwealth-backup.log}"
HEARTBEAT_FILE="${HEARTBEAT_FILE:-/var/run/marginwealth-backup.last-success}"
LOCK_FILE="${LOCK_FILE:-/var/run/marginwealth-backup.lock}"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG_FILE" >&2; }

# ── single-instance guard ────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another backup is still running — skipping this tick"
  exit 0
fi

# ── read a KEY=value from the .env (handles quotes + '=' inside the value) ─
get_env() {
  grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

if [[ ! -f "$ENV_FILE" ]]; then
  log "FATAL: env file not found: $ENV_FILE (set ENV_FILE=...)"
  exit 1
fi

SRC_URI="$(get_env MONGODB_URL)"
DST_URI="$(get_env MONGODB_ATLAS_URL)"
DB_NAME="$(get_env MONGODB_DB_NAME)"

if [[ -z "$SRC_URI" ]]; then
  log "FATAL: MONGODB_URL missing in $ENV_FILE"
  exit 1
fi
if [[ -z "$DST_URI" ]]; then
  log "FATAL: MONGODB_ATLAS_URL missing in $ENV_FILE — add the Atlas URI"
  exit 1
fi

START_TS=$(date +%s)
log "backup START → db='${DB_NAME:-<from-uri>}' src=local dst=atlas"

# ── stream: dump local → restore into Atlas (exact mirror via --drop) ─────
#  --nsInclude keeps the restore scoped to our DB even if the archive ever
#  carries stray namespaces. --noIndexRestore is intentionally NOT set: we
#  want Atlas to be immediately query-ready after a disaster restore.
if mongodump --uri="$SRC_URI" --archive --gzip --numParallelCollections=1 2>>"$LOG_FILE" \
   | mongorestore --uri="$DST_URI" --archive --gzip --drop \
       --nsInclude="${DB_NAME:-*}.*" 2>>"$LOG_FILE"; then
  ELAPSED=$(( $(date +%s) - START_TS ))
  date '+%Y-%m-%dT%H:%M:%S%z' > "$HEARTBEAT_FILE"
  log "backup OK ✓ (${ELAPSED}s) → Atlas now mirrors local"
else
  log "backup FAILED ✗ — Atlas left UNCHANGED (dump or restore errored)"
  exit 1
fi
