# Security hardening — MarginWealth backend

Practical, do-this-before-production checklist. Ordered by impact. The app
already has solid defaults (bcrypt passwords, JWT + refresh rotation,
rate-limited login, admin API-key + IP gate, security headers, docs disabled
in prod). The items below close the gaps that matter for a money platform.

A **production boot guard** (`app/core/config.py::_enforce_production_secrets`)
refuses to start when `APP_ENV=production` and any of #1–#3 / #6 below is still
insecure. Development/staging are exempt, so local work is unaffected.

---

## 1. Lock down MongoDB (highest priority)

Passwords are bcrypt-hashed (cost 12), so even a full DB *read* cannot reveal
them. **But anyone with DB *write* access can overwrite a `password_hash` and
log in as super-admin.** So the database must never be reachable from the
internet, and must require authentication.

1. **Enable authentication (SCRAM):**
   ```js
   // mongosh — create an admin user, then an app user scoped to the DB
   use admin
   db.createUser({ user: "mpadmin", pwd: "<STRONG_RANDOM>", roles: ["root"] })
   use marginwealth
   db.createUser({ user: "mpapp", pwd: "<STRONG_RANDOM>", roles: [{ role: "readWrite", db: "marginwealth" }] })
   ```
   Start `mongod` with `--auth` (or `security.authorization: enabled` in
   `mongod.conf`), then point the backend at it:
   ```
   MONGODB_URL=mongodb://mpapp:<STRONG_RANDOM>@127.0.0.1:27017/marginwealth?authSource=marginwealth
   ```
   (MongoDB Atlas already enforces auth + TLS — prefer it if you don't want to
   self-manage.)

2. **Network isolation:** bind Mongo to `127.0.0.1` only (`net.bindIp: 127.0.0.1`
   in `mongod.conf`) so it is NOT exposed on a public interface. If the DB is on
   a separate host, restrict the port (27017) with a firewall/security-group to
   the backend's IP only — never `0.0.0.0`.

3. **TLS** between backend and DB if they're on different hosts (Atlas does this
   automatically; self-hosted: `mongod --tlsMode requireTLS`).

> The boot guard rejects a credential-less `MONGODB_URL` (no `user:pass@`) in
> production.

## 2. Replace the JWT secret + admin API key

The dev `.env` ships placeholders (`...change_in_prod`). If these reach
production, an attacker can **forge a valid admin JWT** — game over.

Generate fresh random values and put them in the **production** `.env` only:
```bash
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('ADMIN_API_KEY=' + secrets.token_urlsafe(36))"
```
`ADMIN_API_KEY` must match `NEXT_PUBLIC_ADMIN_KEY` in `frontend-admin/.env`.
Note: because it ships in the admin JS bundle, the API key is a *soft gate*,
not a true secret — the real admin protections are the password, 2FA, and IP
allow-list (below). Rotating the JWT secret logs everyone out (expected).

## 3. Change the super-admin password + enable 2FA

- The seed password (`Admin@123`) is public knowledge. Set a strong unique
  `SEED_SUPER_ADMIN_PASSWORD` for the first prod seed, **and** change it from
  the admin panel after first login.
- **Enable TOTP 2FA on every admin/super-admin account.** 2FA is enforced only
  when `two_fa_enabled=true` on the account, so it must be turned on per admin —
  it is not automatic. With 2FA on, a stolen password alone is useless.

## 4. Restrict admin access by IP

Set `ADMIN_IP_WHITELIST` in production to your office/VPN egress IPs
(comma-separated). When set, admin endpoints reject any other source IP even
with a valid token + API key. Empty (current default) = any IP allowed.

## 5. Terminate TLS at the edge + tighten hosts

- Serve everything over HTTPS (the app emits HSTS in production). Redirect
  HTTP→HTTPS at nginx/the load balancer.
- `TrustedHostMiddleware` is registered in prod with `allowed_hosts=["*"]` —
  tighten it to your real hostnames (`api.<domain>`, tenant domains) so the app
  rejects Host-header spoofing.

## 6. Operational

- Keep `APP_DEBUG=false` and `APP_ENV=production` in prod (guard enforces).
- `/metrics` (Prometheus) is currently public — restrict it to your monitoring
  network at the reverse proxy, or it leaks request/latency telemetry.
- Secrets live only in the server `.env` (already git-ignored). Never commit
  real secrets. Rotate on staff offboarding.
- Take regular encrypted MongoDB backups and test restores.
- **Optional — brute-force lockout:** account lockout is intentionally disabled
  (`auth_service.MAX_FAILED_ATTEMPTS = 0`) for UX; login is still IP
  rate-limited to 5/min. To add per-account lockout, set
  `MAX_FAILED_ATTEMPTS`/`LOCKOUT_MINUTES` and re-enable the check in
  `authenticate()`.

---

### What's already good (no action needed)

- Passwords: bcrypt, cost 12, auto-rehash on cost bump. Never stored plaintext.
- JWT: short-lived access (24h) + rotating refresh with a Redis JTI allow-list;
  logout/blocked-account revokes immediately (user re-fetched from DB per request).
- Login: IP rate-limited (5/min); admin requires JWT + API key (+ IP list if set).
- Headers: `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, HSTS in prod.
- API docs (`/docs`, `/redoc`, `/openapi.json`) disabled in production.
- CORS locked to configured origins (+ verified tenant domains).
