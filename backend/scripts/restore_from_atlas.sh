#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  MarginWealth — DISASTER RECOVERY: Atlas → local MongoDB
#
#  Run this on a FRESH VPS (or after a wipe) to pull the latest Atlas
#  warm-standby copy back into the local MongoDB the app talks to. It is the
#  exact reverse of backup_to_atlas.sh.
#
#  ⚠️  DESTRUCTIVE: --drop overwrites the local DB with Atlas's contents.
#      Run only when local is empty/stale and you WANT Atlas to win. The
#      script asks for an explicit "yes" first (skip with FORCE=1).
#
#  CONFIG (from backend .env, override with ENV_FILE=...):
#    MONGODB_URL        local destination URI (where the app reads from)
#    MONGODB_ATLAS_URL  Atlas source URI
#    MONGODB_DB_NAME    database name
#
#  USAGE
#    bash restore_from_atlas.sh            # interactive confirm
#    FORCE=1 bash restore_from_atlas.sh    # no prompt (automation)
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE="${ENV_FILE:-/root/marginwealth/backend/.env}"

get_env() {
  grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "FATAL: env file not found: $ENV_FILE (set ENV_FILE=...)" >&2
  exit 1
fi

DST_URI="$(get_env MONGODB_URL)"        # local — where we RESTORE INTO
SRC_URI="$(get_env MONGODB_ATLAS_URL)"  # Atlas — where we PULL FROM
DB_NAME="$(get_env MONGODB_DB_NAME)"

[[ -z "$SRC_URI" ]] && { echo "FATAL: MONGODB_ATLAS_URL missing in $ENV_FILE" >&2; exit 1; }
[[ -z "$DST_URI" ]] && { echo "FATAL: MONGODB_URL missing in $ENV_FILE" >&2; exit 1; }

echo "About to OVERWRITE local DB '${DB_NAME:-<from-uri>}' with the Atlas copy."
echo "   source (Atlas): ${SRC_URI%%@*}@…"
echo "   target (local): ${DST_URI%%@*}@…"
if [[ "${FORCE:-0}" != "1" ]]; then
  read -r -p "Type 'yes' to proceed: " ans
  [[ "$ans" == "yes" ]] || { echo "aborted."; exit 1; }
fi

echo "[$(date '+%H:%M:%S')] restoring Atlas → local …"
mongodump --uri="$SRC_URI" --archive --gzip \
  | mongorestore --uri="$DST_URI" --archive --gzip --drop \
      --nsInclude="${DB_NAME:-*}.*"
echo "[$(date '+%H:%M:%S')] restore complete ✓ — point the app at local Mongo and restart."
