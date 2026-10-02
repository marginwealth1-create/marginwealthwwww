# Zerodha Auto-Login — Production Deployment

Daily-scheduled Kite Connect access-token refresh. Drives the Kite OAuth +
TOTP screen with a headless Playwright Chromium so the admin doesn't have
to manually login every weekday.

This guide covers ONLY the server-side deployment. Local dev is irrelevant —
the feature only runs in production where the daily 07:00 IST trigger
fires against the real Kite endpoints.

---

## 1. What was added

### Backend
- `backend/app/utils/crypto.py` — AES-256-GCM encrypt/decrypt for credentials at rest
- `backend/app/models/zerodha_auto_login.py` — singleton Beanie document for encrypted creds + scheduler state
- `backend/app/services/zerodha_auto_login.py` — Playwright login flow with 3-layer request_token capture + WebSocket-safe token handoff
- `backend/app/services/zerodha_auto_login_scheduler.py` — daily IST loop with Redis SETNX leader lock
- `backend/app/api/v1/admin/zerodha_auto_login.py` — 5 super-admin-only endpoints
- Wired into `backend/app/main.py` lifespan (start/stop) and `backend/app/api/v1/admin/__init__.py` (router)
- Model registered in `backend/app/core/database.py`
- New env var `ZERODHA_CREDS_KEY` added to `backend/app/core/config.py`
- New dependency `playwright>=1.45.0,<2` in `backend/requirements.txt`

### Frontend (admin)
- `frontend-admin/lib/api.ts` — `ZerodhaAutoLoginAPI` namespace
- `frontend-admin/components/zerodha/CredentialsModal.tsx` — credentials capture dialog
- `frontend-admin/components/zerodha/AutoLoginPanel.tsx` — main status + controls card
- Integrated into `frontend-admin/app/(admin)/zerodha/page.tsx`

---

## 2. EC2 deployment steps (one-time setup)

SSH into the production server first.

### 2.1 Generate a per-environment encryption key

NEVER reuse the dev key in production. Generate a fresh one on the server:

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Output looks like: `KqB8...PnQ=` (44 chars).

### 2.2 Add it to the production .env

```bash
sudo nano /etc/marginwealth/.env   # or wherever your prod env lives
```

Add this line (paste the key generated above):

```bash
ZERODHA_CREDS_KEY=KqB8...PnQ=
```

Save and exit. **Do not commit this key to git.** Verify file permissions are 600:

```bash
sudo chmod 600 /etc/marginwealth/.env
sudo chown <backend-user>:<backend-user> /etc/marginwealth/.env
```

### 2.3 Install Python deps

```bash
cd /path/to/marginwealth_ind_web/backend
sudo -u <backend-user> .venv/bin/pip install -r requirements.txt
```

Make sure you run this **as the same user that runs the backend systemd unit**
(usually `ubuntu` or a dedicated `marginwealth` user). Otherwise the
backend won't find the new packages.

### 2.4 Install Chromium for Playwright

```bash
sudo -u <backend-user> .venv/bin/playwright install chromium
sudo -u <backend-user> .venv/bin/playwright install-deps chromium   # Linux only — installs apt deps
```

`install-deps` requires sudo and pulls down ~30 MB of shared libraries
(libnss3, libnspr4, libdbus-1, etc.) that Chromium needs to run headless.
If you skip this step, browser launch will fail with:

```
Error: browserType.launch: Host system is missing dependencies
```

### 2.5 Verify everything imports

```bash
sudo -u <backend-user> .venv/bin/python -c "
from app.services.zerodha_auto_login import zerodha_auto_login
from app.services.zerodha_auto_login_scheduler import zerodha_auto_login_loop
from app.utils.crypto import encrypt, decrypt
c, iv = encrypt('test')
print('crypto roundtrip:', decrypt(c, iv))
"
```

Should print `crypto roundtrip: test`.

### 2.6 Restart the backend service

```bash
sudo systemctl restart marginwealth-backend
sudo journalctl -u marginwealth-backend -f --since '1 minute ago'
```

Look for these log lines on startup:

```
zerodha_auto_login_scheduler_started
app_started ...
```

If you see `playwright not installed` in the scheduler logs, repeat 2.3 +
2.4 making sure you used the correct backend user.

---

## 3. First-time admin setup (one-time)

This is the operator workflow — done once via the admin panel.

### 3.1 Get the TOTP secret from Kite

You MUST have the base32 TOTP secret. Kite doesn't show the existing one
for security — you have to reset TOTP to get a fresh secret.

1. Go to https://kite.zerodha.com → Profile → Password & Security
2. Find the "External 2FA TOTP" section → click **Reset TOTP**
3. Confirm with Authy + password
4. On the new QR screen, click **"Can't scan?"** / **"Manual entry"**
5. Copy the 16–32 char base32 string (looks like `JBSWY3DPEHPK3PXP6X7K…`)
6. **Add the same secret to Authy** before completing the verification —
   old Authy entry is now dead. Add Account → Enter Code Manually → paste secret
7. Verify the new TOTP works by completing the Kite setup screen

Store the secret in a password manager (Bitwarden / 1Password). Don't
screenshot it, don't email it, don't paste it into general notes apps.

### 3.2 Save creds in the admin panel

1. Open the admin panel → **Zerodha Connect** page (super-admin only)
2. Scroll to the **Auto-login (daily)** card
3. Click **Save credentials** → fill in:
   - **Kite Client ID** (e.g. `ZK1234`)
   - **Password** (the same one you use to login to kite.zerodha.com)
   - **TOTP Secret** (from step 3.1)
4. Click **Save credentials** — toast confirms "Credentials saved"

### 3.3 Test the login

First, in the Kite developer console, set each account's app **Redirect URL**
to exactly the URL the Zerodha page shows, e.g.
`https://api.<domain>/api/v1/admin/zerodha/callback`. Account A and B use the
same URL: the login URL sends `redirect_params=account=N`, so `/callback`
exchanges the token with the right account's API secret.

Before enabling the daily scheduler, do a manual test:

1. Click **Test login now**. It is queued and runs on the feed-leader
   process (the one that owns the Kite WS pool) within ~30 s.
2. Watch the status card, which polls every 3 s for 2 minutes. After ~30–60 s you'll see one of:
   - ✅ **Last attempt: Success** → all good, proceed
   - ❌ **Last run failed at "stage_name": …** → check the troubleshooting section below

### 3.4 Set the schedule + enable

1. Set **Daily schedule** (default 07:00 IST) — this is 2h 15m before the
   09:15 IST market open, giving you a buffer to manually fallback if the
   3 automatic retries all fail
2. Click **Update time** to save
3. Click **Enable** to flip the scheduler ON

Status pill turns green: "Enabled". Done.

---

## 4. How a login runs (safe for both accounts)

1. Only the feed-leader process runs logins. The daily scheduler lives there, and **Test login now** just queues a Redis flag (`zerodha_auto_login:run_now:<account>`) that the scheduler claims.
2. `refresh_now()` takes a per-account Redis SETNX lock (`zerodha_auto_login:refresh_lock:<account>`, 5 min TTL). It pauses self-heal for the run so the token probe can't clear the token being refreshed. It does **not** tear the WS pool down, so the other account's feed keeps streaming.
3. Headless Chromium logs in to Kite (user id → password → TOTP) and follows Kite's redirect to `/api/v1/admin/zerodha/callback?request_token=…&account=N`.
4. `/callback` does the **only** exchange of the single-use `request_token` (`generate_session`, with that account's API secret) and saves the token. The old design also exchanged it inside the login flow, so one of the two always failed with "Token is invalid or has expired".
5. The login counts as successful when that account's `lastConnected` is stamped after the run started. The token value can't be used for this, because Kite repeats it on a same-day re-login.
6. The WS reconnect happens on the feed leader, for the logged-in account only: Account A → `connect_ws(force=True)`, Account B → its own slot. If `/callback` landed on another worker, it publishes `{"action": "reconnect"}` on `zerodha:failover:cmd` instead of opening a socket there.
7. `finally:` re-arms self-heal and releases the lock.

---

## 5. Daily scheduler behavior

- Runs on the feed-leader process, wakes every 30 s, and checks Account A and B independently
- First claims any queued **Test login now** for the account (runs even when the schedule is disabled)
- Fires the daily login only when:
  - `is_enabled == True` (admin toggled on)
  - Current IST time is from 2 min before to 50 min after `schedule_time_ist` (all 7 days)
  - The scheduler hasn't already fired for this account today (DB `last_attempt_at` with `last_attempt_source == "scheduler"`, so restarts don't double-fire)
  - This worker won the `zerodha_auto_login:scheduler_leader` lock (30 min TTL)
- Retries up to 3× with 5 min gap between attempts on failure
- For Account A it then verifies A's own socket connected (→ WS reconnect → one more full login)
- After all retries exhausted: dispatches a `NotificationLevel.DANGER`
  Notification to every super-admin so they get the bell-icon alert

---

## 6. Troubleshooting

Failures surface in the admin panel with the stage name. Map of stages
to root causes:

| Stage | Meaning | Fix |
|---|---|---|
| `precheck` | Kite API key not configured | Set it in the existing Zerodha settings page first |
| `decrypt` | Wrong `ZERODHA_CREDS_KEY` (e.g. key was rotated) | Re-save credentials with the new key, or restore the old key |
| `import` | `playwright` package not installed OR `playwright install chromium` not run on this host | Re-run section 2.3 + 2.4 with the correct backend user |
| `navigate` | Kite login URL did not load in 20s | Server's outbound HTTP to `kite.zerodha.com` is broken — check firewall / DNS |
| `userid` | Username/password form not interactive | Kite changed their login page; selectors need updating in `zerodha_auto_login.py:_run_login_flow` |
| `totp_page` | No 2FA input found | Kite UI changed — update `_TOTP_SELECTORS` in `zerodha_auto_login.py` |
| `redirect` | Kite never sent the browser to /callback (Kite's page text is in the error) | Wrong password or TOTP secret; **server clock drift > 30 s** (`timedatectl status` must show `System clock synchronized: yes`); or the Kite app's redirect URL ≠ this backend's `/api/v1/admin/zerodha/callback` |
| `session` | /callback rejected the token (reason in the error) or saved no fresh session | Wrong API secret for that account; otherwise look for `zerodha_callback_failed` in the logs |
| `lock` | Another auto-login is already in progress | Wait 5 min; lock auto-expires |

### Clock sync check

If TOTP keeps failing despite a correct secret:

```bash
timedatectl status
# Should show:
#   System clock synchronized: yes
#   NTP service: active

# If not:
sudo timedatectl set-ntp true
sudo systemctl restart systemd-timesyncd
```

### Tail backend logs during a test login

```bash
sudo journalctl -u marginwealth-backend -f | grep -E 'zerodha_auto_login|zerodha_scheduler|callback'
```

You should see, in order (on the feed-leader worker):
```
zerodha_auto_login_success
zerodha_scheduler_test_login_result   # manual test only
zerodha_ws_pool_started               # fresh socket on the new token (Account A)
zerodha_account_b_ws_spawned          # Account B
```

---

## 7. Manual fallback

If auto-login is broken for any reason, the existing manual login flow
is **completely untouched**:

1. Open admin → Zerodha Connect
2. Click **Login with Kite** (existing button, not the new auto-login card)
3. Complete the manual Kite + Authy flow
4. Token refreshed exactly as before

Auto-login can stay disabled while you fix whatever broke. There's no
ambient state to clean up — the scheduler just sits idle when `is_enabled`
is False.

---

## 8. Security checklist

Before going live:

- [ ] `ZERODHA_CREDS_KEY` is a fresh per-environment key, not the dev one
- [ ] `.env` file is `chmod 600`, owned by the backend service user
- [ ] Kite API key used has `no_trading` permission (orders can't be placed even if creds leak — defense in depth, doesn't affect B-book operations)
- [ ] TOTP secret was entered ONCE in the admin panel, then cleared from clipboard / password manager session
- [ ] Backup admin code stored separately (Kite generates these — needed if you ever lose access to both Authy and the new TOTP secret)

---

## 9. Files changed (for git review)

```
Backend:
  app/api/v1/admin/__init__.py                   # router registration
  app/api/v1/admin/zerodha_auto_login.py         # NEW — 5 endpoints
  app/core/config.py                              # ZERODHA_CREDS_KEY field
  app/core/database.py                            # register new model
  app/main.py                                     # lifespan task wiring
  app/models/zerodha_auto_login.py               # NEW — Beanie singleton
  app/services/zerodha_auto_login.py             # NEW — Playwright flow
  app/services/zerodha_auto_login_scheduler.py   # NEW — daily IST loop
  app/utils/crypto.py                             # NEW — AES-256-GCM
  requirements.txt                                # + playwright

Frontend (admin):
  app/(admin)/zerodha/page.tsx                    # import + render panel
  components/zerodha/AutoLoginPanel.tsx           # NEW — status card
  components/zerodha/CredentialsModal.tsx         # NEW — capture dialog
  lib/api.ts                                      # ZerodhaAutoLoginAPI namespace

Local-only (DO NOT commit):
  .env                                            # ZERODHA_CREDS_KEY value
```
