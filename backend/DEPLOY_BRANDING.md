# White-Label Branding — Deploy Notes

End-to-end ops checklist for shipping the white-label branding +
auto-SSL custom domains feature to production.

## 0. Zero-impact contract (re-confirm)

* All new fields on `User` are optional with `None` default.
* No existing user row is written to during deploy.
* Feature is gated by `BRANDING_ENABLED=false` (default) — code is
  inert until the flag is flipped.
* APK + existing web payloads are byte-identical (verified against
  `marginwealth_apk/src/features/auth/api/auth.api.ts`).

## 1. Backend deploy (any of the 4 phases)

```powershell
# 1. Pull + restart
git pull --ff-only
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt        # adds nothing new in P1; dnspython for P4 (see §4)
# Restart uvicorn / supervisord / systemd unit as you do today.

# 2. Verify the new schema landed cleanly (read-only):
cd backend
python -m scripts.verify_branding_indexes
# Expected output:
#   [OK] User model exposes all 7 branding fields
#   [OK] Index `custom_domain_unique_sparse` is unique+sparse as expected
#   [INFO] users with custom_domain set: 0
#   [INFO] BRANDING_ENABLED=False
#   All branding schema checks passed.
```

If anything other than `0 exit code` comes back → roll back the
deploy. Fields stay in DB as harmless residue.

## 2. Flip the feature flag (Phase 2+)

In `backend/.env`:

```env
BRANDING_ENABLED=true
PLATFORM_PUBLIC_IP=<your.public.ipv4>   # required only for §4 (custom domains)
```

Restart backend. Verify:

* `GET /api/v1/branding/by-code/<some_admin_user_code>` returns 404
  (admin hasn't set a brand_name yet) instead of 503.
* `GET /api/v1/admin/branding/me` returns the admin's row.

If anything looks wrong: set `BRANDING_ENABLED=false`, restart →
0-second rollback.

## 3. Frontend deploys

* `frontend-user`: rebuild + redeploy. The `BrandingProvider` is wired
  in `app/providers.tsx`; `?ref=<user_code>` is wired in the existing
  `app/(auth)/register/page.tsx`. No new top-level routes — the
  same `/login` and `/register` work with branding applied.
* `frontend-admin`: rebuild + redeploy. New page at
  `/settings/branding` (sidebar entry "Branding" appears for ADMIN
  and SUPER_ADMIN; hidden from BROKER).

`NEXT_PUBLIC_PLATFORM_PUBLIC_IP=<your.public.ipv4>` should be set in
the admin frontend env so the DNS-record copy buttons show the right
IP. If unset, the UI prints "(ask operator)" — non-fatal.

## 4. Server prep for custom-domain SSL (Phase 4 only)

One-time, on the host running the backend + nginx:

```bash
# 1. Install certbot + nginx plugin
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# 2. Add dnspython to the backend env
. .venv/bin/activate
pip install dnspython==2.6.1
deactivate

# 3. Allow the backend OS user to run certbot + nginx without password.
#    Replace `marginwealth` with the actual UNIX user running uvicorn /
#    celery (run `id -un` while su'd as that user).
sudo tee /etc/sudoers.d/marginwealth-branding > /dev/null <<'EOF'
marginwealth ALL=(root) NOPASSWD: /usr/bin/certbot, /usr/sbin/nginx
EOF
sudo chmod 0440 /etc/sudoers.d/marginwealth-branding
sudo visudo -c    # must print "/etc/sudoers.d/marginwealth-branding: parsed OK"

# 4. Confirm the existing nginx config has a catch-all server block
#    (server_name _;) that proxies to the FastAPI upstream. Certbot
#    --nginx adds per-domain server blocks alongside; never edits the
#    catch-all.

# 5. Boot the celery worker so SSL provisioning tasks actually run:
celery -A app.workers.celery_app worker -Q default --loglevel=info
#    Must be a long-running systemd unit / supervisord program in prod.

# 6. Confirm the public IP is reachable:
curl -I http://<your-public-ip>/    # should return a 200 / 308 from nginx

# 7. Verify Let's Encrypt rate-limit headroom (we use the prod
#    endpoint by default; staging is for testing):
sudo certbot certificates       # lists current certs and expiry
```

## 5. Production smoke test (after Phase 4 ships)

Pick one admin (preferably an internal test account, not a real
revenue-generating sub-admin) and walk through the full flow:

1. Admin logs into `https://marginwealth.com/admin`.
2. Navigates to `Settings → Branding`.
3. Uploads a logo, types brand name, hits Save.
4. Opens `https://marginwealth.com/register?ref=<their user_code>` in
   incognito. Should see admin's logo + brand name, registers a fake
   user. Verify in Mongo: `db.users.findOne({email: "fake@..."})` →
   `assigned_admin_id` matches admin, `signup_origin: "BRANDED_REFERRAL"`.
5. Back in admin panel, type a test domain (`branding-test.<your-tld>`)
   under "Connect Custom Domain", Save.
6. Update DNS at registrar (`A @` and `A www` → PLATFORM_PUBLIC_IP),
   wait for propagation.
7. Click "Verify & Connect". Watch status:
   `PENDING_DNS → DNS_VERIFIED → PROVISIONING → READY`.
8. Hit `https://branding-test.<your-tld>/login` — should show admin's
   branding and HTTPS lock.
9. Login with the fake user from step 4 → after login, browser should
   auto-redirect to `https://branding-test.<your-tld>/dashboard`
   (signup_origin gate fires).
10. Existing 10k users (sample 5) — login on `marginwealth.com/login`
    works as before, no redirect, dashboard loads.

If step 10 ever fails for any existing user → set
`BRANDING_ENABLED=false` immediately, file an issue, repro on staging.

## 6. Phased rollout summary

| Phase | What ships | How to roll back |
|---|---|---|
| 1 | Schema + flag (`BRANDING_ENABLED=false`) | Leave as-is. Fields are harmless null residue. |
| 2 | Public `/branding/*` + admin profile UI (logo, brand name) | `BRANDING_ENABLED=false` → endpoints 503; UI shows "feature not enabled". |
| 3 | Branded register `?ref=` + login attribution | Same flag. New users with `signup_origin != null` simply stay on the platform. |
| 4 | Custom domain + auto-SSL | Same flag. Already-issued certs stay on disk (harmless). |

**Each phase is one commit per repo, one `git revert` away.**
