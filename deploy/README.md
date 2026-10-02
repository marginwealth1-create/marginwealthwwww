# Deployment

CI/CD lives in `.github/workflows/`:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yml` | every push / PR | lint + type-check + build verify |
| `deploy.yml` | push to `main` (and manual) | SSH → EC2 → run `scripts/deploy.sh` |

The actual deploy script is `scripts/deploy.sh` — it's invoked by the
GitHub Action over SSH but can also be run by hand on the EC2.

## One-time setup

### 1. GitHub repository secrets

`Settings → Secrets and variables → Actions → New repository secret`. Add:

| Secret | Value |
|--------|-------|
| `EC2_HOST` | `<your-elastic-ip>` |
| `EC2_USER` | `marginwealth` |
| `EC2_SSH_KEY` | paste the full contents of `marginwealth.live.pem` (the PRIVATE key, including the `-----BEGIN OPENSSH PRIVATE KEY-----` and `-----END OPENSSH PRIVATE KEY-----` lines) |
| `EC2_PORT` | `22` (optional — defaults to 22) |

> Use a deploy-only SSH key in production. The current key gives full sudo
> access to the box, which is fine for a single-developer project but you
> should rotate or restrict it later.

### 2. EC2 passwordless sudo for the systemctl + nginx commands

`scripts/deploy.sh` calls `sudo systemctl restart …` and `sudo nginx -t`.
Without a TTY, sudo will block waiting for a password. Allow the `ubuntu`
user to run only those commands without a password:

```bash
sudo visudo -f /etc/sudoers.d/marginwealth-deploy
```

Paste:

```
marginwealth ALL=(root) NOPASSWD: /usr/bin/systemctl restart marginwealth-backend
marginwealth ALL=(root) NOPASSWD: /usr/bin/systemctl reload marginwealth-backend
marginwealth ALL=(root) NOPASSWD: /usr/bin/journalctl -u marginwealth-backend *
marginwealth ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
marginwealth ALL=(root) NOPASSWD: /usr/sbin/nginx -t
marginwealth ALL=(root) NOPASSWD: /usr/bin/cp * /etc/nginx/sites-available/marginwealth
```

Save (`Ctrl+O`, `Enter`, `Ctrl+X`). visudo will syntax-check before
writing, so a typo here won't lock you out.

### 3. Make sure the EC2 has the repo at `/opt/marginwealth`

If you haven't already:

```bash
sudo mkdir -p /opt/marginwealth
sudo chown ubuntu:ubuntu /opt/marginwealth
git clone https://github.com/marginwealth1-create/marginwealthwwww.git /opt/marginwealth
chmod +x /opt/marginwealth/scripts/deploy.sh
```

### 4. Trigger the first deploy

Push to `main` (any commit, even a README touch). Watch
`Actions → Deploy to EC2` in GitHub. You should see:

1. SSH connects to `<your-elastic-ip>`
2. `scripts/deploy.sh` runs — pulls, rebuilds only the changed pieces,
   restarts services
3. Healthchecks pass (backend returns 401 on auth-required endpoint,
   user/admin domains return 200/307)

## Live server (marginwealth.live)

Host `srv2020148` / `201.18.211.172`, Ubuntu 26.04. Repo at `/opt/marginwealth`,
owned by the `marginwealth` user (who runs `deploy.sh`). Services:

| Piece | How it runs |
|-------|-------------|
| MongoDB 8.2 | Docker container `mongo` (host network, `127.0.0.1:27017`, auth + replica set `rs0`). 8.0.x refuses to start on Linux ≥ 6.19. |
| Redis | `redis-server` (apt), localhost only |
| Backend | systemd `marginwealth-backend` (gunicorn, `127.0.0.1:8000`), venv from `uv` (Python 3.12) |
| Frontends | PM2 `marginwealth-user` (:3000) / `marginwealth-admin` (:3001) under the `marginwealth` user, bound to 127.0.0.1 |
| TLS | Let's Encrypt via certbot webroot `/var/www/certbot`, symlinked to `/etc/ssl/marginwealth/origin.{crt,key}` |

Secrets live only on the server: `backend/.env`, `frontend-*/.env.local`, and
`/root/marginwealth-secrets.env` (Mongo passwords, admin login).

Day-to-day deploy after pushing to `main`:

```bash
ssh root@201.18.211.172
sudo -iu marginwealth bash /opt/marginwealth/scripts/deploy.sh
```

## Manual deploy (no GitHub)

```bash
ssh -i marginwealth.live.pem ubuntu@<your-elastic-ip>
cd /opt/marginwealth
bash scripts/deploy.sh
```

Force a full rebuild (ignore "no changes" optimisation):

```bash
FORCE_FULL=1 bash scripts/deploy.sh
```

## Manual rollback

GitHub Actions can re-run an older successful deploy job (`Actions →
Deploy → click old run → "Re-run all jobs"`). Or on the EC2:

```bash
cd /opt/marginwealth
git reset --hard <old-sha>
FORCE_FULL=1 bash scripts/deploy.sh
```

## What gets rebuilt

`deploy.sh` diffs `HEAD` against `origin/main` and only touches the
affected pieces:

| Changed path | Action |
|--------------|--------|
| `backend/**` | restart `marginwealth-backend.service` |
| `backend/requirements.txt` | `pip install -r requirements.txt` first |
| `frontend-user/**` | rebuild + `pm2 reload marginwealth-user` |
| `frontend-user/package-lock.json` | `npm ci` first |
| `frontend-admin/**` | same, for admin app |
| `deploy/nginx/marginwealth.conf` | copy to `/etc/nginx/sites-available/marginwealth` + `nginx -t` + reload |

A clean deploy with **no app-code changes** finishes in ~5 seconds.
A build-required deploy runs ~2 minutes (npm build is the slow part).

---

## Phase 4 — Custom domain auto-provisioning (Branding white-label)

When an admin connects their own domain (e.g. `mybroker.com`) from
*Settings → Branding*, the backend Celery worker shells out to a
privileged helper script that:

1. Writes an HTTP-only nginx server block proxying the domain to the
   user-frontend (port 3000).
2. Reloads nginx so the block is reachable for ACME HTTP-01.
3. Runs `certbot --nginx` to obtain a Let's Encrypt cert and patch
   the same block with `listen 443 ssl` + redirect.
4. On certbot failure, removes the half-provisioned config so the
   domain never serves the app over plaintext HTTP.

### One-time server setup

```bash
# 1. Install certbot + nginx plugin (skip if already installed)
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# 2. Deploy helper scripts to /usr/local/bin/ (run from the repo root)
sudo install -m 0755 deploy/scripts/marginwealth-add-branded-domain.sh \
  /usr/local/bin/marginwealth-add-branded-domain
sudo install -m 0755 deploy/scripts/marginwealth-remove-branded-domain.sh \
  /usr/local/bin/marginwealth-remove-branded-domain

# 3. Allow the backend's OS user to run the helpers + certbot + nginx
#    passwordless. Replace the file content (NOT append) every redeploy
#    so the rules stay tight.
sudo tee /etc/sudoers.d/marginwealth-branding > /dev/null <<'EOF'
root ALL=(root) NOPASSWD: /usr/local/bin/marginwealth-add-branded-domain
root ALL=(root) NOPASSWD: /usr/local/bin/marginwealth-remove-branded-domain
root ALL=(root) NOPASSWD: /usr/bin/certbot
root ALL=(root) NOPASSWD: /usr/sbin/nginx
EOF
sudo chmod 0440 /etc/sudoers.d/marginwealth-branding

# 4. Verify sudoers parses (CRITICAL — broken sudoers locks you out)
sudo visudo -c

# 5. Smoke test the helper from the backend's user shell:
sudo -n /usr/local/bin/marginwealth-add-branded-domain --help 2>/dev/null \
  || echo "(script ignores --help; expected non-zero exit, that's fine)"
```

> **Note on the OS user**: this project runs the backend as `root`
> on the current single-host setup. If you migrate to a dedicated
> service account (recommended), replace `root` in the sudoers
> rules above with that account's username.

### Renewal

`certbot.timer` is enabled by default after `apt install certbot` and
fires twice daily. It automatically renews any cert in
`/etc/letsencrypt/live/` that's within 30 days of expiry. No app-side
changes required.

### Tear-down (admin disconnects domain)

The admin UI exposes a "Disconnect" action (or the admin can clear
`custom_domain` on their profile). To free nginx capacity:

```bash
sudo /usr/local/bin/marginwealth-remove-branded-domain mybroker.com
# Optional — drop the cert too:
sudo certbot delete --cert-name mybroker.com
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `helper_missing` in admin UI | Scripts not deployed to /usr/local/bin | Re-run step 2 above |
| `sudo: a password is required` | Sudoers rule wrong / not chmod 0440 | Re-run step 3 + `visudo -c` |
| `Could not automatically find a matching server block` | Helper script not run before certbot | Should not happen — worker now uses helper. If seen, check `/etc/nginx/sites-enabled/marginwealth-branded-<domain>.conf` exists |
| Cert obtained but admin UI shows FAILED | API/worker can't reach itself / status update path broken | Check Celery worker logs: `pm2 logs marginwealth-celery-worker` |
