# Notification System — End-to-End Spec

A 4-layer real-time notification pipeline for FastAPI + Next.js apps:

1. **In-page toast** (sonner) when the app is on screen.
2. **Web Audio chime** for an audible cue.
3. **OS tray notification via Service Worker** when the PWA is
   minimised or another app is in front.
4. **Web Push (VAPID)** so the phone buzzes even when the PWA has
   been force-stopped or the device is locked — same surface as
   WhatsApp / Telegram.

The whole pipeline is **scope-aware**: each notification only reaches
the admins / brokers in the source user's ownership chain. No
platform-wide blasts.

---

## Hand-off prompt for another Claude session

> Implement the notification system described in `notifications.md` on
> this project. Use my existing FastAPI backend (MongoDB / Beanie /
> Redis pub-sub) and Next.js 14 App Router frontends (one admin, one
> user). Keep my existing auth, layout, and routing. Match the file
> paths I'll provide if you ask. Don't add unrelated cleanups. Follow
> the spec section by section: backend model + service + endpoints,
> deposit/withdrawal/wallet wiring, frontend SW + helpers + bridge.
> Web Push VAPID keys come from env. Test that an admin only sees
> notifications for users in their pool.

---

## Stack assumptions

- **Backend:** FastAPI, Beanie (Mongo ODM), Redis pub-sub, asyncio.
- **Frontend (×2 apps):** Next.js 14 App Router, React Query, Zustand
  for auth, Sonner for toasts. PWA via `display:standalone` manifest.
- **Browser:** Modern Chromium (Android Chrome / Edge / Brave),
  desktop Firefox/Edge. iOS Safari 16.4+ for PWA push.
- **User model has these fields:**
  - `assigned_admin_id: ObjectId | None` — the direct admin owner.
  - `broker_ancestry: list[ObjectId]` — every broker in the parent chain.
  - `role: "CLIENT" | "ADMIN" | "BROKER" | "SUPER_ADMIN"`.

---

## High-level architecture

```
                ┌─────────────────────────────────────────────┐
                │  USER ACTION (e.g. submit deposit request)  │
                └──────────────────────┬──────────────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────────┐
                        │  Backend endpoint handler    │
                        │  e.g. POST /user/deposits    │
                        └──────────────┬───────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
   ┌──────────────────┐   ┌─────────────────────┐   ┌────────────────────┐
   │ DB insert (Mongo)│   │ Redis pub-sub:      │   │ Web Push fan-out   │
   │   DepositRequest │   │ admin:events        │   │ via pywebpush      │
   └──────────────────┘   └──────────┬──────────┘   └──────────┬─────────┘
                                     │                         │
                          ┌──────────┴──────────┐              │
                          ▼                     ▼              ▼
              ┌──────────────────┐  ┌────────────────┐  ┌─────────────────┐
              │ Admin WS bridge  │  │ User WS bridge │  │  OS push service│
              │ (in-page toast)  │  │ (wallet/etc.)  │  │ (FCM/APNs/etc.) │
              └──────────────────┘  └────────────────┘  └────────┬────────┘
                                                                 ▼
                                                       ┌──────────────────┐
                                                       │ Browser SW       │
                                                       │ `push` event →   │
                                                       │ showNotification │
                                                       └──────────────────┘
```

**Why both WS and Web Push?**

- WS toast is *instant + free* when the PWA is open. Same TCP socket
  the rest of the app uses for live data.
- Web Push is the *only* path that works when the PWA process is
  dead. Slower (round-trip through FCM) and rate-limited, but it
  reaches a locked phone.

Both run in parallel for every event. No fallback logic needed — the
SW dedupes via the `tag` field so the same event from both sources
collapses into one tray row.

---

## Scope rules (CRITICAL — operator-explicit)

Every notification gets routed to ONLY the chain of owners of the
source user:

| User's place in the org | Recipients |
|---|---|
| Direct under `Admin A` (no broker) | Admin A |
| Under `Broker X`, X is under `Admin A` | Admin A + Broker X |
| Under `Sub-broker Y` → `Broker X` → `Admin A` | Admin A + Broker X + Sub-broker Y |
| Platform-direct (no admin, no broker) | All SUPER_ADMINs |

Operators that should NEVER get a notification:

- Admin A about Admin B's pool.
- SUPER_ADMIN about a user that already has an admin/broker.
- Broker X about a peer broker's users.

The backend computes this list once per event and:

1. Sends Web Push only to those recipients' subscriptions.
2. Includes the same id list (`recipient_admin_ids`) in the WS
   broadcast so the frontend bridge can suppress out-of-scope toasts.

---

## Notification matrix

### Admin tier (in-app toast + tray + Web Push)

| User action | Title | Body |
|---|---|---|
| Deposit submitted | 💰 New deposit request | `₹AMT · NAME (CODE) · MODE` |
| Withdrawal submitted | 🏦 New withdrawal request | `₹AMT · NAME (CODE)` |

### User tier (in-app toast + tray + Web Push)

| Admin action | Title | Body |
|---|---|---|
| Deposit approved | ✅ Deposit approved | `₹AMT added to your wallet` |
| Deposit rejected | ❌ Deposit rejected | `₹AMT was rejected — REMARK` |
| Withdrawal processed | ✅ Withdrawal processed | `₹AMT sent to your bank` |
| Withdrawal rejected | ❌ Withdrawal rejected | `₹AMT was rejected — REASON` |
| Admin Add Fund (positive ADJUSTMENT) | 💰 Funds added by admin | `₹AMT credited` |
| Admin Deduct Fund (negative ADJUSTMENT) | ⚠️ Funds deducted by admin | `₹AMT debited` |

Trade brokerage / margin lock / P&L bookings stay **silent** —
operator request: the trader doesn't want a phone buzz on every fill.

---

# Backend implementation

## 1. Add dependency

`backend/requirements.txt`:

```
pywebpush>=2.0.0,<3
```

## 2. Config — VAPID keys

`backend/app/core/config.py`, inside the `Settings` class:

```python
# RFC 8292 application-server identity. Generate ONCE per
# deployment; rotating invalidates every existing subscription.
VAPID_PUBLIC_KEY: str = ""
VAPID_PRIVATE_KEY: SecretStr = Field(default=SecretStr(""))
VAPID_SUBJECT: str = "mailto:admin@yourplatform.com"
```

## 3. Key-gen script

`backend/scripts/generate_vapid_keys.py`:

```python
"""Generate a VAPID key pair for Web Push.

    python -m scripts.generate_vapid_keys

Paste the printed lines into the backend .env. DO NOT commit the
private key.
"""
import base64, sys

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("cryptography is required — pip install cryptography", file=sys.stderr)
    raise SystemExit(1)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    print(f"VAPID_PUBLIC_KEY={_b64url(pub_bytes)}")
    print(f"VAPID_PRIVATE_KEY={_b64url(priv_bytes)}")
    print(f"VAPID_SUBJECT=mailto:admin@yourplatform.com")


if __name__ == "__main__":
    main()
```

## 4. Model — `PushSubscription`

`backend/app/models/push_subscription.py`:

```python
from __future__ import annotations

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class PushSubjectType(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(TimestampMixin):
    subject_type: PushSubjectType
    subject_id: PydanticObjectId
    label: str | None = None
    endpoint: str
    keys: PushKeys
    user_agent: str | None = None

    class Settings:
        name = "push_subscriptions"
        indexes = [
            IndexModel([("endpoint", ASCENDING)], unique=True),
            IndexModel([("subject_type", ASCENDING), ("subject_id", ASCENDING)]),
        ]
```

Register in your Beanie init alongside the other models.

## 5. Service — scope + send

`backend/app/services/push_service.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from beanie import PydanticObjectId

from app.core.config import settings
from app.models.push_subscription import PushSubjectType, PushSubscription

logger = logging.getLogger(__name__)


def _push_enabled() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY) and bool(
        settings.VAPID_PRIVATE_KEY.get_secret_value()
    )


def _vapid_claims() -> dict[str, str]:
    return {"sub": settings.VAPID_SUBJECT}


def _send_one_sync(sub: PushSubscription, payload: dict[str, Any]) -> tuple[bool, int | None]:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return (False, None)
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.keys.p256dh, "auth": sub.keys.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY.get_secret_value(),
            vapid_claims=_vapid_claims(),
            ttl=60 * 60 * 24,
        )
        return (True, 200)
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        return (False, status)
    except Exception:
        logger.exception("webpush_unexpected_error")
        return (False, None)


async def _send_one(sub: PushSubscription, payload: dict[str, Any]) -> None:
    ok, status = await asyncio.to_thread(_send_one_sync, sub, payload)
    if not ok and status in (404, 410):
        # Browser pushed the subscription away — prune.
        try:
            await sub.delete()
        except Exception:
            pass


async def _fan_out(subs: list[PushSubscription], payload: dict[str, Any]) -> None:
    if not subs or not _push_enabled():
        return
    await asyncio.gather(*[_send_one(s, payload) for s in subs], return_exceptions=True)


async def _compute_recipient_admin_ids(
    source_user_id: PydanticObjectId | str,
) -> list[PydanticObjectId]:
    """Walk the ownership chain. Returns the set of admin/broker IDs
    that legitimately own this user. Super-admins are included ONLY
    when the user is platform-direct (no admin / no broker)."""
    from app.models.user import User, UserRole

    try:
        uid = (
            PydanticObjectId(source_user_id)
            if isinstance(source_user_id, str)
            else source_user_id
        )
    except Exception:
        return []
    user = await User.get(uid)
    if user is None:
        return []

    recipients: set[PydanticObjectId] = set()
    if user.assigned_admin_id is not None:
        recipients.add(user.assigned_admin_id)
    for bid in user.broker_ancestry or []:
        recipients.add(bid)
    if not recipients:
        coll = User.get_motor_collection()
        async for doc in coll.find({"role": UserRole.SUPER_ADMIN.value}, {"_id": 1}):
            recipients.add(doc["_id"])
    return list(recipients)


async def send_to_user_owners(
    source_user_id: PydanticObjectId | str,
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> list[PydanticObjectId]:
    """Scope-aware fan-out. Returns the recipient list so callers can
    mirror it in the WS publish."""
    recipients = await _compute_recipient_admin_ids(source_user_id)
    if not recipients:
        return []
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.ADMIN,
        {"subject_id": {"$in": recipients}},
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})
    return recipients


async def send_to_user(
    user_id: PydanticObjectId | str,
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Direct push to one trader."""
    uid = PydanticObjectId(user_id) if isinstance(user_id, str) else user_id
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.USER,
        PushSubscription.subject_id == uid,
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})
```

## 6. Subscribe/unsubscribe endpoints

`backend/app/api/v1/admin/push.py`:

```python
from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import CurrentAdmin
from app.models.push_subscription import PushKeys, PushSubjectType, PushSubscription
from app.schemas.common import APIResponse

router = APIRouter(prefix="/push", tags=["admin-push"])


class _SubscribeBody(BaseModel):
    endpoint: str
    keys: PushKeys
    label: str | None = None


class _UnsubBody(BaseModel):
    endpoint: str


@router.get("/vapid-key", response_model=APIResponse[dict])
async def vapid_key(admin: CurrentAdmin):
    return APIResponse(data={"public_key": settings.VAPID_PUBLIC_KEY})


@router.post("/subscribe", response_model=APIResponse[dict])
async def subscribe(
    body: _SubscribeBody,
    admin: CurrentAdmin,
    user_agent: str | None = Header(default=None, alias="User-Agent"),
):
    existing = await PushSubscription.find_one(PushSubscription.endpoint == body.endpoint)
    if existing:
        existing.subject_type = PushSubjectType.ADMIN
        existing.subject_id = admin.id
        existing.keys = body.keys
        existing.label = body.label or existing.label
        existing.user_agent = user_agent or existing.user_agent
        await existing.save()
        return APIResponse(data={"id": str(existing.id), "created": False})
    sub = PushSubscription(
        subject_type=PushSubjectType.ADMIN,
        subject_id=admin.id,
        endpoint=body.endpoint,
        keys=body.keys,
        label=body.label,
        user_agent=user_agent,
    )
    await sub.insert()
    return APIResponse(data={"id": str(sub.id), "created": True})


@router.post("/unsubscribe", response_model=APIResponse[dict])
async def unsubscribe(body: _UnsubBody, admin: CurrentAdmin):
    sub = await PushSubscription.find_one(
        PushSubscription.endpoint == body.endpoint,
        PushSubscription.subject_id == admin.id,
    )
    if sub is None:
        return APIResponse(data={"ok": True, "found": False})
    await sub.delete()
    return APIResponse(data={"ok": True, "found": True})
```

User-side `backend/app/api/v1/user/push.py` is identical except
`CurrentAdmin` → `CurrentUser` and `PushSubjectType.ADMIN` →
`PushSubjectType.USER`. Mount both routers in your aggregator.

## 7. Wire into the existing publish points

**Deposit submit** (`POST /user/deposits`):

```python
# AFTER the Mongo insert, BEFORE returning:
recipient_ids: list[str] = []
try:
    from app.services.push_service import (
        _compute_recipient_admin_ids as _compute_owners,
        send_to_user_owners as _push_owners,
    )

    owners = await _compute_owners(user.id)
    recipient_ids = [str(x) for x in owners]
    asyncio.create_task(
        _push_owners(
            user.id,
            title="💰 New deposit request",
            body=f"₹{payload.amount} · {user.full_name or user.user_code} · {payload.payment_mode.upper()}",
            url="/payments?tab=deposits",
            tag=f"deposit-{req.id}",
        )
    )
except Exception:
    pass

# Then publish the WS event — INCLUDE recipient_admin_ids:
await publish_admin_event(
    "deposit_update",
    {
        "event": "submitted",
        "user_id": str(user.id),
        "deposit_id": str(req.id),
        "user_name": user.full_name,
        "user_code": user.user_code,
        "amount": str(payload.amount),
        "mode": payload.payment_mode,
        "recipient_admin_ids": recipient_ids,
    },
)
```

**Withdrawal submit:** same shape, swap labels.

**Deposit approve** (`POST /admin/deposits/{id}/approve`): your
existing `wallet_service.adjust(reason="DEPOSIT", amount=positive)`
call already fires the user push via `_publish_wallet_event` (see
step 8). No change needed.

**Deposit reject** (no wallet move) — explicit user push:

```python
try:
    from app.services.push_service import send_to_user as _push_user

    amt_label = f"₹{r.amount}" if r.amount else ""
    reason_label = f" — {payload.admin_remark}" if payload.admin_remark else ""
    asyncio.create_task(
        _push_user(
            r.user_id,
            title="❌ Deposit rejected",
            body=f"{amt_label} was rejected{reason_label}",
            url="/wallet",
            tag=f"deposit-rejected-{r.id}",
        )
    )
except Exception:
    pass
```

**Withdrawal reject:** same shape, swap labels.

## 8. User push on wallet move

Inside your `wallet_service.adjust(...)` (or wherever
`_publish_wallet_event` is called), after the existing publish:

```python
# Web Push to the user — survives a force-stopped PWA.
try:
    upper = (reason or "").upper()
    if upper in {"DEPOSIT", "WITHDRAWAL", "ADJUSTMENT"}:
        amt_num = Decimal(str(amount))
        amt_label = f"₹{abs(amt_num):,.2f}"
        if upper == "DEPOSIT":
            title, body = "✅ Deposit approved", f"{amt_label} added to your wallet"
        elif upper == "WITHDRAWAL":
            title, body = "✅ Withdrawal processed", f"{amt_label} sent to your bank"
        else:  # ADJUSTMENT
            if amt_num >= 0:
                title, body = "💰 Funds added by admin", f"{amt_label} credited"
            else:
                title, body = "⚠️ Funds deducted by admin", f"{amt_label} debited"

        from app.services.push_service import send_to_user as _push_user

        asyncio.create_task(
            _push_user(
                user_id,
                title=title,
                body=body,
                url="/wallet",
                tag=f"wallet-{upper.lower()}-{user_id}",
            )
        )
except Exception:
    logger.exception("wallet_push_failed user=%s", user_id)
```

---

# Frontend implementation (Next.js 14 App Router)

Repeat in BOTH the admin app and the user app. The only difference is
the API base path (`/admin/push/*` vs `/user/push/*`).

## 1. Brand icons in `/public/`

Notifications need PNG (Android drops SVG silently). Put two files
in `frontend/public/`:

- `icon-192.png` — 192×192
- `icon-512.png` — 512×512

(For the user app the existing PWA install icons under
`/public/icons/icon-192.png` work as-is — just point the SW there.)

## 2. Service worker — `public/sw.js`

Notification-only SW (no fetch handler — we're not building an
offline app):

```js
const VERSION = "yourapp-pwa-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Page → SW for in-process notifications (foreground / minimised PWA).
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "notify") return;
  const title = String(data.title || "App");
  const body = String(data.body || "");
  const tag = data.tag || undefined;
  const url = data.url || "/";
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag,
      renotify: true,
      data: { url },
    })
  );
});

// Web Push (VAPID) — wakes the SW even when the PWA is dead.
self.addEventListener("push", (event) => {
  let payload = { title: "App", body: "", url: "/", tag: undefined };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    try {
      const text = event.data && event.data.text();
      if (text) payload.body = String(text);
    } catch {}
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: payload.tag,
      renotify: true,
      data: { url: payload.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.includes(url) && "focus" in w) return w.focus();
      }
      if (wins.length > 0 && "focus" in wins[0]) {
        const w = wins[0];
        if ("navigate" in w) w.navigate(url);
        return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
```

## 3. SW registrar — `components/common/PwaRegister.tsx`

```tsx
"use client";

import { useEffect } from "react";

export function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    const isDev = process.env.NODE_ENV === "development";
    if (isDev) {
      navigator.serviceWorker.getRegistrations().then((regs) => {
        regs.forEach((r) => r.unregister());
      });
      return;
    }
    const idle = (cb: () => void) =>
      ("requestIdleCallback" in window
        ? (window as any).requestIdleCallback(cb)
        : setTimeout(cb, 1000));
    idle(() => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    });
  }, []);
  return null;
}
```

Mount once in `app/layout.tsx` near the body root.

## 4. Notification helpers — `lib/notify-sound.ts`

```ts
"use client";

let _ctx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  try {
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    if (!AC) return null;
    if (!_ctx) _ctx = new AC();
    if (_ctx.state === "suspended") void _ctx.resume();
    return _ctx;
  } catch { return null; }
}

export function playNotifyPing(): void {
  const ctx = getCtx();
  if (!ctx) return;
  try {
    const now = ctx.currentTime;
    const gain = ctx.createGain();
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.16, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.55);
    for (const [freq, at] of [[880, 0], [1320, 0.12]] as Array<[number, number]>) {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(now + at);
      osc.stop(now + at + 0.42);
    }
  } catch {}
}

export function showNativeNotification(
  title: string,
  body: string,
  opts?: { onClick?: () => void; tag?: string; url?: string },
): void {
  if (typeof window === "undefined" || typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  const url = opts?.url ?? window.location.pathname;
  // Prefer SW path (Android PWA only accepts SW-fired notifications).
  if ("serviceWorker" in navigator && navigator.serviceWorker.controller) {
    try {
      navigator.serviceWorker.controller.postMessage({
        type: "notify", title, body, tag: opts?.tag, url,
      });
      return;
    } catch {}
  }
  try {
    const n = new Notification(title, {
      body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: opts?.tag,
      renotify: true,
      silent: false,
    } as NotificationOptions);
    n.onclick = () => {
      window.focus();
      try { opts?.onClick?.(); } catch {}
      n.close();
    };
  } catch {}
}

export async function ensureNotificationPermission(): Promise<boolean> {
  if (typeof window === "undefined" || typeof Notification === "undefined") return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  try {
    const res = await Notification.requestPermission();
    return res === "granted";
  } catch { return false; }
}

function _urlBase64ToUint8(s: string): Uint8Array {
  const padded = s + "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export async function subscribeForWebPush(): Promise<boolean> {
  try {
    if (typeof window === "undefined") return false;
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    if (Notification.permission !== "granted") return false;
    const { PushAPI } = await import("@/lib/api");
    const { public_key } = await PushAPI.vapidKey();
    if (!public_key) return false;
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8(public_key).buffer as ArrayBuffer,
      });
    }
    const raw = sub.toJSON() as { endpoint?: string; keys?: { p256dh?: string; auth?: string } };
    if (!raw.endpoint || !raw.keys?.p256dh || !raw.keys?.auth) return false;
    await PushAPI.subscribe({
      endpoint: raw.endpoint,
      keys: { p256dh: raw.keys.p256dh, auth: raw.keys.auth },
      label: navigator.userAgent.slice(0, 80),
    });
    return true;
  } catch (e) {
    console.warn("[push] subscribe failed", e);
    return false;
  }
}
```

## 5. API client — `lib/api.ts`

```ts
export const PushAPI = {
  vapidKey: () => unwrap<{ public_key: string }>(api.get("/admin/push/vapid-key")),
  subscribe: (body: { endpoint: string; keys: { p256dh: string; auth: string }; label?: string }) =>
    unwrap<{ id: string; created: boolean }>(api.post("/admin/push/subscribe", body)),
  unsubscribe: (endpoint: string) =>
    unwrap<{ ok: boolean; found: boolean }>(api.post("/admin/push/unsubscribe", { endpoint })),
};
```

(For user app, swap `/admin/push/*` → `/user/push/*`.)

## 6. WebSocket bridge — `components/common/AdminWsBridge.tsx`

```tsx
"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAdminAuthStore } from "@/stores/authStore";
import { ensureFreshAccessToken, isExpiringSoon } from "@/lib/api";
import { ADMIN_API_KEY, STORAGE_KEYS, WS_URL } from "@/lib/constants";
import {
  ensureNotificationPermission,
  playNotifyPing,
  showNativeNotification,
  subscribeForWebPush,
} from "@/lib/notify-sound";

const NOTIFY_KEY = "admin.notifications.enabled";
function notificationsEnabled(): boolean {
  if (typeof window === "undefined") return true;
  const v = window.localStorage.getItem(NOTIFY_KEY);
  return v === null ? true : v === "1";
}

export function AdminWsBridge() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  useEffect(() => {
    if (!admin) return;
    if (!ADMIN_API_KEY) return;

    // First-mount: prompt → if granted, subscribe for Web Push.
    void (async () => {
      const ok = await ensureNotificationPermission();
      if (ok) await subscribeForWebPush();
    })();

    let stopped = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    async function connect() {
      if (stopped) return;
      let access: string | null =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      if (!access || isExpiringSoon(access)) {
        try { access = (await ensureFreshAccessToken()) || access; } catch {}
      }
      if (!access) {
        reconnectTimer = setTimeout(() => void connect(), 3000);
        return;
      }
      const url =
        `${WS_URL.replace(/\/$/, "")}/ws/admin` +
        `?token=${encodeURIComponent(access)}` +
        `&key=${encodeURIComponent(ADMIN_API_KEY)}`;
      ws = new WebSocket(url);

      // 25 s keep-alive — survives Android battery saver / corporate
      // proxies that drop idle WS after 30-60 s.
      let pingTimer: ReturnType<typeof setInterval> | null = null;
      ws.onopen = () => {
        attempt = 0;
        if (pingTimer) clearInterval(pingTimer);
        pingTimer = setInterval(() => {
          try { if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping"); } catch {}
        }, 25_000);
      };

      ws.onmessage = (ev) => {
        let msg: any;
        try { msg = JSON.parse(ev.data); } catch { return; }
        switch (msg?.type) {
          case "deposit_update":
            qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
            qc.invalidateQueries({ queryKey: ["admin", "payments"] });
            if (msg.event === "submitted" && notificationsEnabled()) {
              // Scope filter: backend ships the owner-id list.
              const recipients: string[] | undefined = msg.recipient_admin_ids;
              const myId = String(admin?.id || "");
              if (Array.isArray(recipients) && myId && !recipients.includes(myId)) break;
              const who = msg.user_name || msg.user_code || "a user";
              const code = msg.user_name && msg.user_code ? ` (${msg.user_code})` : "";
              const amt = msg.amount ? `₹${Number(msg.amount).toLocaleString("en-IN")}` : "";
              const mode = msg.mode ? String(msg.mode).toUpperCase() : "";
              const body = [amt, `${who}${code}`, mode].filter(Boolean).join(" · ");
              toast.success("💰 New deposit request", {
                description: body,
                duration: 9000,
                action: {
                  label: "View",
                  onClick: () => routerRef.current.push("/payments?tab=deposits"),
                },
              });
              playNotifyPing();
              showNativeNotification("💰 New deposit request", body, {
                tag: `deposit-${msg.deposit_id || Date.now()}`,
                onClick: () => routerRef.current.push("/payments?tab=deposits"),
              });
            }
            break;
          case "withdrawal_update":
            qc.invalidateQueries({ queryKey: ["admin", "withdrawals"] });
            qc.invalidateQueries({ queryKey: ["admin", "payments"] });
            if (msg.event === "submitted" && notificationsEnabled()) {
              const recipients: string[] | undefined = msg.recipient_admin_ids;
              const myId = String(admin?.id || "");
              if (Array.isArray(recipients) && myId && !recipients.includes(myId)) break;
              const who = msg.user_name || msg.user_code || "a user";
              const code = msg.user_name && msg.user_code ? ` (${msg.user_code})` : "";
              const amt = msg.amount ? `₹${Number(msg.amount).toLocaleString("en-IN")}` : "";
              const body = [amt, `${who}${code}`].filter(Boolean).join(" · ");
              toast.warning("🏦 New withdrawal request", {
                description: body,
                duration: 12000,
                action: {
                  label: "View",
                  onClick: () => routerRef.current.push("/payments?tab=withdrawals"),
                },
              });
              playNotifyPing();
              showNativeNotification("🏦 New withdrawal request", body, {
                tag: `withdrawal-${msg.withdrawal_id || Date.now()}`,
                onClick: () => routerRef.current.push("/payments?tab=withdrawals"),
              });
            }
            break;
          // … other cases (position_update, order_update, etc.) …
        }
      };

      ws.onclose = () => {
        if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
        if (stopped) return;
        attempt += 1;
        const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt, 4));
        reconnectTimer = setTimeout(() => void connect(), delay);
      };

      ws.onerror = () => ws?.close();
    }

    // Reconnect when the PWA comes back to foreground.
    const onVisible = () => {
      if (document.visibilityState === "visible" && (!ws || ws.readyState >= 2)) {
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        attempt = 0;
        void connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    void connect();

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      document.removeEventListener("visibilitychange", onVisible);
      ws?.close();
    };
  }, [qc, admin?.id]);

  return null;
}
```

## 7. User-side bridge — `components/common/UserWsBridge.tsx`

Same skeleton as the admin bridge, but the `case "wallet"` handler
maps the wallet event payload to the four operator-facing reasons:

```tsx
function fmtINR(raw: string | number | undefined | null): string {
  if (raw === undefined || raw === null || raw === "") return "";
  const n = Number(raw);
  if (!Number.isFinite(n)) return String(raw);
  return `₹${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function walletReasonToToast(
  reason: string | undefined,
  amount: string | undefined,
): { kind: "in" | "out"; title: string; body: string } | null {
  const r = String(reason || "").toUpperCase();
  const n = Number(amount ?? 0);
  const credit = Number.isFinite(n) ? n > 0 : true;
  switch (r) {
    case "DEPOSIT":
      return { kind: "in", title: "✅ Deposit approved", body: `${fmtINR(amount)} added to your wallet` };
    case "WITHDRAWAL":
      return { kind: "out", title: "✅ Withdrawal processed", body: `${fmtINR(amount)} sent to your bank` };
    case "ADJUSTMENT":
      return credit
        ? { kind: "in", title: "💰 Funds added by admin", body: `${fmtINR(amount)} credited to your wallet` }
        : { kind: "out", title: "⚠️ Funds deducted by admin", body: `${fmtINR(amount)} debited from your wallet` };
    default:
      return null; // silent for brokerage / margin / P&L
  }
}

// inside the switch in onmessage:
case "wallet":
  qc.invalidateQueries({ queryKey: ["wallet"] });
  {
    const p = (msg as any).payload || {};
    const t = walletReasonToToast(p.reason, p.amount);
    if (t) {
      if (t.kind === "in") toast.success(t.title, { description: t.body, duration: 7000 });
      else toast.warning(t.title, { description: t.body, duration: 7000 });
      playNotifyPing();
      showNativeNotification(t.title, t.body, { tag: `wallet-${Date.now()}` });
    }
  }
  break;
```

---

# Deploy steps

1. `pip install -r requirements.txt` on the backend host.
2. `python -m scripts.generate_vapid_keys`. Paste the three lines into
   the backend `.env` (or systemd `EnvironmentFile`). Restart backend.
3. Frontend rebuild + reload.
4. On each device: uninstall the existing PWA, open the app in a
   fresh browser tab, grant the notification permission, then
   reinstall the PWA. The subscription is created in the SW the
   first time the WS bridge mounts after permission lands.

---

# Verification scenarios

| Scenario | Expected |
|---|---|
| PWA open, foreground | Toast + chime + tray (collapsed into the foreground notification by the OS) |
| PWA minimised 30 s | Tray notification (Web Audio doesn't fire when hidden but the OS chime does) |
| PWA minimised 5 min, WhatsApp open | Tray (kept alive by the 25 s WS ping + Web Push) |
| PWA force-stopped from recents | Tray via Web Push only — wakes the SW |
| Phone locked, screen off | Lock-screen notification via Web Push |
| Admin A's pool user submits deposit | Only Admin A's devices ping. Admin B / super-admin / brokers under B see no tray pop. (Their dashboards still refresh the list — only the audio/visual nag is suppressed.) |
| Platform-direct user submits | Super-admin(s) ping. No other admin sees it. |

---

# Caveats

- **iOS Safari** supports Web Push only on 16.4+ AND only when the
  PWA is installed from the Share menu. Regular Safari tabs cannot
  subscribe.
- **Battery saver** modes (Android Doze, iOS Low Power) can delay
  pushes by tens of seconds.
- **Rotating VAPID keys** invalidates every existing subscription —
  users must re-grant permission. Plan a forced re-subscribe via the
  `subscribeForWebPush()` flow (existing call already handles the
  "browser dropped the old endpoint" case via the 404/410 prune).
- The **single Redis pub-sub channel** scales to thousands of admin
  WS connections per node before you need partitioning. The bridge
  switches on `type` so adding new events is a one-line case.

---

# Tags and dedupe

Every notification carries a `tag`:

- `deposit-<deposit_id>` for the submit event.
- `deposit-rejected-<deposit_id>` for the reject event.
- `withdrawal-<withdrawal_id>` / `withdrawal-rejected-<id>`.
- `wallet-<reason>-<user_id>` for wallet moves.

Same tag from the WS path and the Web Push path collapses into one
tray row, so a single submit doesn't show twice when both surfaces
fire ~milliseconds apart. Different tags (different events) DO
stack — the operator sees every fresh deposit as its own row.

---

# Master toggle

Each app stores a `*.notifications.enabled` flag in `localStorage`
(default ON). The WS bridge checks it before firing the toast / tray
notification. React Query invalidation runs regardless — data stays
fresh even with notifications silenced.

A simple settings UI:

```tsx
const [enabled, setEnabled] = useState(() =>
  localStorage.getItem("admin.notifications.enabled") !== "0"
);
function toggle(v: boolean) {
  setEnabled(v);
  localStorage.setItem("admin.notifications.enabled", v ? "1" : "0");
}
```

Drop it on `/settings/platform` (or wherever your app keeps user
preferences). For Web Push, also offer an "Unsubscribe from device"
button that calls `pushManager.getSubscription().unsubscribe()` and
hits `PushAPI.unsubscribe(endpoint)`.
</content>
