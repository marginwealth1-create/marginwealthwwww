#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
#  Issue the Let's Encrypt cert for marginwealth.live once its DNS
#  reaches this server, then point nginx's origin.{crt,key} symlinks at
#  it. Run as root. Safe to re-run.
#
#  On the live box the `marginwealth-cert.timer` runs this every 5 min
#  until the cert exists, then disables itself — so the site goes HTTPS
#  on its own as soon as the A records are switched. Renewal afterwards
#  is certbot's own timer (webroot + nginx reload hook are remembered).
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAINS=(marginwealth.live www.marginwealth.live admin.marginwealth.live api.marginwealth.live)
WEBROOT=/var/www/certbot
LIVE=/etc/letsencrypt/live/marginwealth.live

if [ ! -f "$LIVE/fullchain.pem" ]; then
  # Reachability probe over the public DNS name: proves the domain lands on
  # THIS nginx before we spend a Let's Encrypt attempt (they rate-limit).
  mkdir -p "$WEBROOT/.well-known/acme-challenge"
  probe=$(openssl rand -hex 8)
  echo "$probe" > "$WEBROOT/.well-known/acme-challenge/mw-probe"
  for d in "${DOMAINS[@]}"; do
    if [ "$(curl -s -m 10 "http://$d/.well-known/acme-challenge/mw-probe")" != "$probe" ]; then
      echo "waiting for DNS: $d does not reach this server yet"
      exit 0
    fi
  done
  rm -f "$WEBROOT/.well-known/acme-challenge/mw-probe"

  certbot certonly --webroot -w "$WEBROOT" --non-interactive --agree-tos \
    --register-unsafely-without-email --cert-name marginwealth.live \
    $(printf -- '-d %s ' "${DOMAINS[@]}") \
    --deploy-hook "systemctl reload nginx"
fi

ln -sfn "$LIVE/fullchain.pem" /etc/ssl/marginwealth/origin.crt
ln -sfn "$LIVE/privkey.pem" /etc/ssl/marginwealth/origin.key
nginx -t
systemctl reload nginx
systemctl disable --now marginwealth-cert.timer 2>/dev/null || true
echo "TLS live for ${DOMAINS[*]}"
