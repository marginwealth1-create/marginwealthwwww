#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
#  marginwealth-remove-branded-domain.sh
#
#  Tear-down counterpart to marginwealth-add-branded-domain.sh.
#  Removes the nginx config + symlink for <DOMAIN> and reloads nginx.
#  Does NOT delete the Let's Encrypt cert (`certbot delete`) so a
#  re-add later doesn't have to wait out the LE rate limit. Operators
#  can run `certbot delete --cert-name DOMAIN` manually if they want
#  to free up storage.
#
#  Idempotent: missing files are silently ignored.
#
#  Usage:  marginwealth-remove-branded-domain.sh <DOMAIN>
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${1:-}"

if [[ -z "$DOMAIN" ]]; then
  echo "ERROR: domain argument required" >&2
  exit 64
fi

if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{2,251}[A-Za-z0-9]$ ]]; then
  echo "ERROR: invalid domain format: '$DOMAIN'" >&2
  exit 64
fi

DOMAIN="${DOMAIN,,}"
CONF_NAME="marginwealth-branded-${DOMAIN}.conf"
CONF_PATH="/etc/nginx/sites-available/${CONF_NAME}"
SYMLINK_PATH="/etc/nginx/sites-enabled/${CONF_NAME}"

rm -f "$SYMLINK_PATH" "$CONF_PATH"

if nginx -t >/dev/null 2>&1; then
  nginx -s reload || true
fi

echo "OK: ${DOMAIN} removed from nginx"
exit 0
