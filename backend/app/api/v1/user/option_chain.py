"""Option chain endpoint — given an underlying (token OR symbol), return the
strikes × expiries grid with live LTPs.

Uses Zerodha's in-memory instrument cache for instant lookups — no MongoDB
round-trips for option data. Prices come from live KiteTicker ticks first,
falling back to a single batch REST /quote call.

Performance: the picker re-fetches every 2 s. Without caching, each call
would (a) re-scan the 50k-row NFO CSV, (b) issue a Kite REST /quote on
100+ keys, and (c) on-demand-subscribe every visible leg — easily 5-15 s
of work per request and a hard freeze when Kite is slow. Two layers of
cache below keep the hot path ≪ 100 ms:

    _CHAIN_CACHE     : full response, keyed by (und, expiry), TTL 2.5 s.
                       Sized for the picker's 2 s polling cadence so back-
                       to-back requests usually hit the cache.
    _CATALOG_FILTER  : the (filtered options + expiries) tuple from
                       get_option_chain_fast, keyed by underlying, TTL
                       300 s. The CSV catalog itself doesn't change
                       intraday so this is safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import CurrentUser
from app.core.redis_client import idempotency_check_and_set
from app.models.platform_setting import PlatformSetting
from app.schemas.common import APIResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/option-chain", tags=["user-option-chain"])


# Fallback defaults if settings are missing (first-run before seed).
_DEFAULT_UNDERLYINGS = [
    {"label": "Nifty", "symbol": "NIFTY", "color": "emerald"},
    {"label": "BankNifty", "symbol": "BANKNIFTY", "color": "violet"},
    {"label": "Sensex", "symbol": "SENSEX", "color": "rose"},
]
_DEFAULT_STRIKES_AROUND_ATM = 15
_DEFAULT_MAX_EXPIRIES = 6

# Hard cap on the Kite REST batch quote — prevents a slow / hung Kite call
# from blocking the picker. On timeout we serve whatever live ticks are
# already in the in-memory map and the frontend's next 2 s poll picks up
# the rest.
_KITE_BATCH_QUOTE_TIMEOUT_SEC = 3.0

# Settings cache (60s)
_settings_cache: dict[str, tuple[Any, float]] = {}
_SETTINGS_TTL = 60.0

# Full-response cache (2.5s) — sized just above the picker's 2s polling
# cadence so each request lands a hit on the next poll.
_CHAIN_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_CHAIN_TTL = 1.0

# Catalog filter cache (5 min) — the heavy 50k-row scan. CSV doesn't change
# intraday so this can sit much longer than the price cache.
_CATALOG_FILTER: dict[str, tuple[tuple[list[dict[str, Any]], list[date]], float]] = {}
_CATALOG_TTL = 300.0
# Negative-cache TTL for a COLD worker that has never seen the catalog. Kept
# short (3 s): the Kite 429 storm this used to guard against is now fixed at
# the source — `zerodha_service.fetch_instruments` is Redis-shared + lock-gated
# so a re-fetch reads the shared catalog from Redis instead of hitting Kite.
# A short TTL lets a worker that booted into an empty window self-heal within a
# poll or two instead of serving "No options available" for 30 s. (A worker
# that has ALREADY seen the catalog never regresses to empty — see
# `_cached_catalog`.)
_CATALOG_NEG_TTL = 3.0


async def _read_setting(key: str, default: Any) -> Any:
    cached = _settings_cache.get(key)
    now = time.time()
    if cached and (now - cached[1]) < _SETTINGS_TTL:
        return cached[0]
    s = await PlatformSetting.find_one(PlatformSetting.setting_key == key)
    val = s.setting_value if s is not None else default
    _settings_cache[key] = (val, now)
    return val


def _exchange_bucket(exchange: str | None) -> str:
    """Map an instrument exchange onto the NSE / BSE / MCX fallback bucket."""
    ex = (exchange or "").upper()
    if ex in ("NSE", "NFO"):
        return "NSE"
    if ex in ("BSE", "BFO"):
        return "BSE"
    if ex == "MCX":
        return "MCX"
    return ""


def _effective_max_expiries(resolved: dict[str, Any], underlying: str | None, exchange: str | None) -> int:
    """Effective expiry cap for ONE instrument:
        1) the underlying's per-script "Show expiry month" (if set),
        2) else the per-exchange fallback (NSE / BSE / MCX),
        3) else the legacy single fallback,
        4) else 6.
    """
    und = (underlying or "").strip().upper()
    for u in (resolved.get("underlyings") or []):
        if isinstance(u, dict) and str(u.get("symbol") or "").strip().upper() == und:
            mx = u.get("max_expiries")
            if mx not in (None, "", 0):
                try:
                    return max(1, int(mx))
                except (TypeError, ValueError):
                    pass
            break
    bucket = _exchange_bucket(exchange)
    by_ex = resolved.get("max_expiries_by_exchange") or {}
    if bucket and by_ex.get(bucket) not in (None, "", 0):
        try:
            return max(1, int(by_ex[bucket]))
        except (TypeError, ValueError):
            pass
    return max(1, int(resolved.get("max_expiries") or 6))


def _effective_strikes_around_atm(
    resolved: dict[str, Any], segment: str | None, flat_default: int
) -> int:
    """Effective 'strikes around ATM' for ONE option segment:
        1) the per-segment value for this option's admin row
           (NSE_IDX_OPT / NSE_STK_OPT / MCX_OPT / BSE_OPT), if the tier set it,
        2) else the flat platform-wide option_chain.strikes_around_atm.
    The chain then renders 2N+1 strikes centred on the live ATM.
    """
    by_seg = resolved.get("strikes_around_atm_by_segment") or {}
    if segment and by_seg.get(segment) not in (None, "", 0):
        try:
            return max(1, int(by_seg[segment]))
        except (TypeError, ValueError):
            pass
    return max(1, int(flat_default))


def _option_admin_row(opt_exch: str | None, underlying: str | None) -> str | None:
    """Option exchange + underlying → admin option-segment row name
    (NSE_IDX_OPT / NSE_STK_OPT / MCX_OPT / BSE_OPT). Symbol-aware for the NSE
    stock-vs-index split (NIFTY / BANKNIFTY / … → index row, else stock row),
    mirroring netting_service.resolve_admin_row. Used to pick the per-segment
    'strikes around ATM' count for the chain being built."""
    ex = (opt_exch or "").upper()
    if ex == "NFO":
        try:
            from app.services.netting_service import _INDEX_UNDERLYING_ROOTS
        except Exception:
            _INDEX_UNDERLYING_ROOTS = (
                "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                "MIDCAPNIFTY", "SENSEX", "BANKEX",
            )
        u = (underlying or "").upper()
        return "NSE_IDX_OPT" if any(u.startswith(r) for r in _INDEX_UNDERLYING_ROOTS) else "NSE_STK_OPT"
    if ex == "BFO":
        return "BSE_OPT"
    if ex == "MCX":
        return "MCX_OPT"
    return None


async def _resolve_expiry_settings_for_user(
    user_id: Any,
) -> dict[str, Any]:
    """USER → BROKER → ADMIN → GLOBAL, field-by-field, for `underlyings`,
    `max_expiries` (legacy single fallback) and `max_expiries_by_exchange`
    (per NSE / BSE / MCX). A None field on an override row inherits from the
    parent tier. Falls back to the global PlatformSetting + seed defaults.
    """
    from beanie import PydanticObjectId
    from app.models.expiry_override import ExpiryOverride, ExpiryOverrideActor
    from app.models.user import User as _User

    cache_key = f"_resolved:{user_id or 'global'}"
    cached = _settings_cache.get(cache_key)
    now = time.time()
    if cached and (now - cached[1]) < _SETTINGS_TTL:
        return cached[0]

    underlyings: list[dict[str, Any]] | None = None
    fallback: int | None = None
    by_exchange: dict[str, Any] | None = None
    strikes_by_seg: dict[str, Any] | None = None
    # Super-admin pool = a user with NO assigned admin (assigned_admin_id null;
    # see admin/users.py create-user: "SUPER_ADMIN → platform pool"). The
    # GLOBAL PlatformSetting holds THIS pool's expiry config. Admin-pool users
    # (and anyone under a broker, whose assigned_admin_id is the parent admin)
    # must NOT inherit it — a super-admin expiry change must never touch an
    # admin's users — so they fall to the hard seed default unless their own
    # user / broker / admin tier set an expiry override.
    is_super_pool = True

    def _absorb_und(ov) -> None:
        nonlocal underlyings
        if ov is not None and underlyings is None and ov.underlyings is not None:
            underlyings = ov.underlyings

    def _absorb_exp(ov) -> None:
        nonlocal fallback, by_exchange
        if ov is None:
            return
        if fallback is None and ov.max_expiries_fallback is not None:
            fallback = ov.max_expiries_fallback
        if by_exchange is None and getattr(ov, "max_expiries_by_exchange", None):
            by_exchange = ov.max_expiries_by_exchange

    def _absorb_strikes(ov) -> None:
        nonlocal strikes_by_seg
        if (
            ov is not None
            and strikes_by_seg is None
            and getattr(ov, "strikes_around_atm_by_segment", None)
        ):
            strikes_by_seg = ov.strikes_around_atm_by_segment

    def _absorb(ov) -> None:
        _absorb_und(ov)
        _absorb_exp(ov)
        _absorb_strikes(ov)

    def _done() -> bool:
        return underlyings is not None and fallback is not None and by_exchange is not None

    if user_id is not None:
        try:
            uid = PydanticObjectId(str(user_id))
        except Exception:
            uid = None
        if uid is not None:
            _absorb(await ExpiryOverride.find_one(
                ExpiryOverride.actor_kind == ExpiryOverrideActor.USER,
                ExpiryOverride.actor_id == uid,
            ))
            # Always load the user — assigned_admin_id decides super-pool vs
            # admin-pool, which gates GLOBAL inheritance for the expiry fields.
            user_doc = await _User.get(uid)
            if user_doc is not None:
                # Closest broker first (sub-broker shadows top broker).
                for bid in reversed(list(getattr(user_doc, "broker_ancestry", None) or [])):
                    if _done():
                        break
                    _absorb(await ExpiryOverride.find_one(
                        ExpiryOverride.actor_kind == ExpiryOverrideActor.BROKER,
                        ExpiryOverride.actor_id == bid,
                    ))
                aid = getattr(user_doc, "assigned_admin_id", None)
                if aid is not None:
                    is_super_pool = False
                    _absorb(await ExpiryOverride.find_one(
                        ExpiryOverride.actor_kind == ExpiryOverrideActor.ADMIN,
                        ExpiryOverride.actor_id == aid,
                    ))

    # Stock LIST (symbols / labels / colours): platform-wide — always falls to
    # GLOBAL when no override set it. For admin-pool users we keep the list but
    # STRIP the super-admin's per-script "Show expiry month", so an expiry
    # choice made at GLOBAL can't leak into an admin's users via the array.
    if underlyings is None:
        underlyings = await _read_setting("option_chain.underlyings", _DEFAULT_UNDERLYINGS) or []
        if not is_super_pool and isinstance(underlyings, list):
            underlyings = [
                {**u, "max_expiries": None}
                for u in underlyings
                if isinstance(u, dict)
            ]

    # Expiry fallbacks: GLOBAL is the SUPER-ADMIN POOL's config. Only super-
    # admin-pool users inherit it; admin-pool users that reached here without
    # their own expiry override use the hard seed default (never GLOBAL).
    if fallback is None:
        if is_super_pool:
            fallback = int(await _read_setting("option_chain.max_expiries", _DEFAULT_MAX_EXPIRIES))
        else:
            fallback = _DEFAULT_MAX_EXPIRIES
    if by_exchange is None:
        if is_super_pool:
            by_exchange = await _read_setting("option_chain.max_expiries_by_exchange", {}) or {}
        else:
            by_exchange = {}
    # Per-segment strike counts: GLOBAL is the SUPER-ADMIN POOL's config, same
    # gating as the expiry fallbacks — admin-pool users never inherit it.
    if strikes_by_seg is None:
        if is_super_pool:
            strikes_by_seg = await _read_setting("option_chain.strikes_around_atm_by_segment", {}) or {}
        else:
            strikes_by_seg = {}

    out = {
        "underlyings": underlyings if isinstance(underlyings, list) else [],
        "max_expiries": max(1, int(fallback)),
        "max_expiries_by_exchange": by_exchange if isinstance(by_exchange, dict) else {},
        "strikes_around_atm_by_segment": strikes_by_seg if isinstance(strikes_by_seg, dict) else {},
    }
    _settings_cache[cache_key] = (out, now)
    return out


def invalidate_settings_cache(key: str | None = None) -> None:
    """Empty the option-chain settings cache. Called by the admin
    override save/delete paths so a change is visible to users on the
    next poll instead of lagging by up to `_SETTINGS_TTL`."""
    global _settings_cache
    if key is None:
        _settings_cache.clear()
    else:
        _settings_cache.pop(key, None)


async def _cached_catalog(und_key: str):
    """get_option_chain_fast wrapper with a 5-minute cache.

    Why we DON'T cache empty results: if a request lands before Zerodha is
    authenticated (or before the NFO/BFO catalog finishes warming), the
    underlying yields no options and we'd otherwise pin "TCS = []" for the
    next 5 minutes — even after the operator authenticates. By only caching
    non-empty hits, the next call retries the catalog scan and picks up
    fresh data on the same poll the picker is already running.
    """
    now = time.time()
    cached = _CATALOG_FILTER.get(und_key)
    if cached:
        age = now - cached[1]
        has_data = bool(cached[0][0])
        # Fresh positive hit → serve it.
        if has_data and age < _CATALOG_TTL:
            return cached[0]
        # Recent EMPTY result → throttle: don't re-hit Kite for _CATALOG_NEG_TTL
        # so its instruments endpoint stops 429-ing and can recover. This is
        # what breaks the every-poll-refetch storm that pinned the chain empty
        # across all workers ("Kite instruments fetch failed: Too many requests").
        if not has_data and age < _CATALOG_NEG_TTL:
            return cached[0]
    from app.services.zerodha_service import zerodha as _zerodha
    result = await _zerodha.get_option_chain_fast(und_key)
    if result[0]:
        _CATALOG_FILTER[und_key] = (result, now)
        return result

    # Empty fetch. The option catalog (strikes × expiries) is intraday-stable,
    # so a MOMENTARY empty — a worker whose Redis/catalog read briefly missed —
    # must NOT overwrite a good cached catalog. That regression is exactly what
    # made the chain flicker "1 s dikhta phir gayab" under multi-worker: some
    # workers served a negative-cached empty (rows=[] → "No options available")
    # while others returned rows, and the picker's 1 s poll round-robined
    # between them. Once a worker has EVER seen the catalog, keep serving that
    # last-good copy (prices still overlay live from mdlive) instead of empty.
    if cached and bool(cached[0][0]):
        return cached[0]

    # Genuinely cold worker that never fetched successfully — cache the empty
    # (short negative TTL via the age check above) so repeated polls don't
    # re-scan every tick, and surface WHY (Zerodha unauthenticated / catalog
    # not yet warmed). Self-heals on the next fetch once Redis has the catalog.
    _CATALOG_FILTER[und_key] = (result, now)
    try:
        status = await _zerodha.get_status()
        logger.warning(
            "option_chain_empty_catalog",
            extra={
                "underlying": und_key,
                "zerodha_connected": status.get("isConnected"),
                "zerodha_configured": status.get("isConfigured"),
                "ws_status": status.get("wsStatus"),
            },
        )
    except Exception:
        logger.warning("option_chain_empty_catalog", extra={"underlying": und_key})
    return result


# NSE index tradingsymbols (with spaces) → NFO/BFO option-chain underlying keys.
# When the user opens the terminal on e.g. "NIFTY 50" (NSE index) and taps
# "Option chain", the instrument.symbol sent from the frontend is "NIFTY 50".
# After stripping spaces it becomes "NIFTY50" which doesn't match the NFO
# catalog's `name = "NIFTY"`, leaving the chain empty. This map bridges the gap.
_UNDERLYING_ALIASES: dict[str, str] = {
    "NIFTY50": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NIFTYFINSERVICE": "FINNIFTY",
    "NIFTYMIDSELECT": "MIDCPNIFTY",
    "NIFTYMIDCAP150": "MIDCPNIFTY",
    "NIFTYNEXT50": "NIFTYNXT50",
}


def _norm_underlying(s: str) -> str:
    normed = (s or "").strip().upper().replace(" ", "")
    return _UNDERLYING_ALIASES.get(normed, normed)


@router.get("/config", response_model=APIResponse[dict])
async def option_chain_config(user: CurrentUser):
    """Public option-chain settings consumed by the picker UI."""
    # underlyings + max_expiries flow through the override hierarchy
    # (USER → BROKER → ADMIN → GLOBAL). strikes_around_atm stays global —
    # it's not part of the per-actor override surface.
    resolved = await _resolve_expiry_settings_for_user(user.id)
    underlyings = resolved["underlyings"]
    strikes_around_atm = int(await _read_setting("option_chain.strikes_around_atm", _DEFAULT_STRIKES_AROUND_ATM))
    max_expiries = int(resolved["max_expiries"])
    return APIResponse(
        data={
            "underlyings": underlyings,
            "strikes_around_atm": strikes_around_atm,
            "max_expiries": max_expiries,
            "max_expiries_by_exchange": resolved.get("max_expiries_by_exchange", {}),
        }
    )


@router.get("", response_model=APIResponse[dict])
async def option_chain(
    user: CurrentUser,
    underlying: str = Query(..., description="Symbol like NIFTY / BANKNIFTY / RELIANCE"),
    expiry: str | None = Query(default=None, description="ISO date; if omitted, nearest expiry"),
):
    und_key = _norm_underlying(underlying)

    # ── Response cache hit? Bail out fast (matches the picker's 2 s poll). ──
    # Cache key includes user.id so each user's per-symbol block set
    # produces a distinct cached payload — otherwise a row blocked
    # for user A could be served from cache to user B who has access
    # to it.
    cache_key = f"{user.id}|{und_key}|{(expiry or '').strip()}"
    now_t = time.time()
    cached_resp = _CHAIN_CACHE.get(cache_key)
    if cached_resp and (now_t - cached_resp[1]) < _CHAIN_TTL:
        return APIResponse(data=cached_resp[0])

    # ── Catalog filter (cached 5 min — CSV doesn't change intraday) ──
    options, all_expiry_dates = await _cached_catalog(und_key)
    from app.services.zerodha_service import zerodha as _zerodha

    # Distinct expiries (sorted asc) — capped to the configured max for THIS
    # user. A per-script max_expiries (set on the underlying chip) wins over
    # the resolved fallback; both flow through the override hierarchy.
    # Per-underlying value wins; else the per-exchange (NSE/BSE/MCX) fallback
    # picked from this underlying's option exchange; else the legacy single.
    _resolved_exp = await _resolve_expiry_settings_for_user(user.id)
    _sample_ex = (options[0].get("exchange") if options else "") or ""
    max_expiries = _effective_max_expiries(_resolved_exp, und_key, _sample_ex)
    expiries = all_expiry_dates[: max(1, max_expiries)]
    expiry_iso = [d.isoformat() for d in expiries]

    # Pick effective expiry
    target: date | None = None
    if expiry:
        try:
            target = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
        except Exception:
            target = None
    if target is None and expiries:
        target = expiries[0]

    # Build strike → {ce, pe} grid for the chosen expiry
    by_strike: dict[float, dict[str, Any]] = {}
    for o in options:
        if target is not None and o.get("_expiry_date") != target:
            continue
        strike = float(o["strike"]) if o.get("strike") is not None else None
        if strike is None:
            continue
        cell = by_strike.setdefault(strike, {"strike": strike, "ce": None, "pe": None})
        cell["ce" if o["option_type"] == "CE" else "pe"] = o

    all_rows = sorted(by_strike.values(), key=lambda r: r["strike"])

    # ── Cross-worker ATM price source ────────────────────────────────
    # BOTH the strike-far spot proxy (below) and the strikes_around_atm
    # centring (further down) find the ATM via the strike with the smallest
    # |CE-PE|, reading each leg's LTP from the in-process ticker maps
    # (`_zerodha.ticks_by_token` / `ticks_by_symbol`). Those maps are ONLY
    # populated on the feed-LEADER worker — on every other worker they're
    # empty, so ATM fell back to the median strike of the WHOLE catalog and
    # the window centred far below spot, dropping every OTM strike above the
    # ATM (operator: "option chain 24,350 ke upar ka aa nahi raha" under
    # multi-worker). Read the leader's shared `mdlive` snapshot once (a single
    # Redis MGET) so ATM detection is correct on ALL workers; the in-process
    # map stays a fallback for the single-worker / leader path.
    _mdlive_ltps: dict[str, Any] = {}
    try:
        from app.services import market_data_service as _mds

        _strike_tokens = [
            str(cell["token"])
            for _r in all_rows
            for _side in ("ce", "pe")
            if (cell := _r.get(_side)) and cell.get("token") is not None
        ]
        if _strike_tokens:
            _mdlive_ltps = await _mds.get_ltp_batch_mdlive(_strike_tokens)
    except Exception:
        _mdlive_ltps = {}

    def _leg_ltp(cell: dict[str, Any] | None) -> float | None:
        """LTP for one option leg — mdlive (cross-worker) first, then the
        in-process ticker map (leader-only). Returns None when neither has a
        positive price so ATM detection can skip the leg."""
        if not cell:
            return None
        tok = cell.get("token")
        if tok is not None:
            v = _mdlive_ltps.get(str(tok))
            if v is not None:
                try:
                    f = float(v)
                    if f > 0:
                        return f
                except (TypeError, ValueError):
                    pass
        try:
            tok_int = int(tok or 0)
        except (TypeError, ValueError):
            tok_int = 0
        live = _zerodha.ticks_by_token.get(tok_int) if tok_int else None
        if live is None and cell.get("symbol"):
            live = _zerodha.ticks_by_symbol.get(cell["symbol"])
        if live is None:
            return None
        try:
            return float(live.get("ltp") or 0) or None
        except (TypeError, ValueError):
            return None

    # ── Underlying spot — centres the window when option legs aren't subscribed ──
    # With option auto-subscribe off, NO leg has a cached LTP, so the parity
    # ATM below finds nothing and both the strike-far cap and the strikes-
    # around-ATM window fell back to the catalog MEDIAN strike — which sits
    # well below the real spot, so the higher strikes above ATM never rendered
    # ("niche ka strike price nahi aa raha"). The underlying index itself IS
    # subscribed (a default), so read ITS spot and centre on that. Cheap: an
    # in-process / mdlive read, with a Kite REST /quote only as a last resort.
    und_spot: float | None = None
    try:
        _uinst = await _zerodha.find_instrument_by_symbol(und_key)
    except Exception:
        _uinst = None
    if _uinst:
        _utok = _uinst.get("token")
        try:
            _ul = _zerodha.ticks_by_token.get(int(_utok)) if _utok else None
            if _ul and float(_ul.get("ltp") or 0) > 0:
                und_spot = float(_ul["ltp"])
        except Exception:
            pass
        if und_spot is None and _utok is not None:
            try:
                from app.services import market_data_service as _mds3

                _m = await _mds3.get_ltp_batch_mdlive([str(_utok)])
                _v = _m.get(str(_utok))
                if _v is not None and float(_v) > 0:
                    und_spot = float(_v)
            except Exception:
                pass
        if und_spot is None:
            try:
                _snap = await _zerodha.get_quote_snapshot(
                    _uinst.get("exchange"), _uinst.get("symbol")
                )
                if _snap and float(_snap.get("ltp") or 0) > 0:
                    und_spot = float(_snap["ltp"])
            except Exception:
                pass

    # ── Strike-far cap (admin matrix → Options → Max % from underlying) ──
    # Hide every strike outside ±strikeFarPercent of the underlying's spot
    # so the chain dialog only shows tradeable strikes (the validator
    # rejects anything farther anyway). Underlying admin row is derived
    # from the option exchange — NFO → NSE_IDX_OPT (option chains are index
    # underlyings), BFO → BSE_OPT, MCX → MCX_OPT.
    # Zero from admin = no cap, full chain renders.
    # Per-option-segment strike count needs the segment — compute it once here
    # (symbol-aware: NIFTY/BANKNIFTY → index row, RELIANCE → stock row) and
    # reuse it for the ATM window below.
    _opt_seg: str | None = None
    if all_rows:
        sample = all_rows[0].get("ce") or all_rows[0].get("pe") or {}
        opt_exch = (sample.get("exchange") or "").upper()
        _opt_seg = _option_admin_row(opt_exch, und_key)
        admin_row = {
            "NFO": "NSE_IDX_OPT",
            "BFO": "BSE_OPT",
            "MCX": "MCX_OPT",
        }.get(opt_exch)
        if admin_row:
            from app.services.netting_service import resolve_strike_far

            far_pct = await resolve_strike_far(admin_row)
            if far_pct > 0:
                # Underlying spot: take from any cached LTP on the option
                # legs (CE − PE parity gives a working spot proxy for the
                # ATM row), fall back to the median strike. Avoids a
                # blocking Kite REST call on the chain hot path.
                spot_guess: float | None = None
                # Quick proxy: scan rows for both-side LTPs and pick the
                # parity-derived spot at the strike with smallest CE−PE.
                with_both = []
                for idx, r in enumerate(all_rows):
                    # Cross-worker LTP (mdlive first, in-process fallback) so
                    # the spot proxy is right on non-leader workers too.
                    ce_ltp = _leg_ltp(r.get("ce"))
                    pe_ltp = _leg_ltp(r.get("pe"))
                    if ce_ltp and pe_ltp and ce_ltp > 0 and pe_ltp > 0:
                        with_both.append((idx, ce_ltp - pe_ltp, r["strike"]))
                if with_both:
                    # ATM = smallest |CE−PE|; spot ≈ strike + (CE−PE).
                    best = min(with_both, key=lambda x: abs(x[1]))
                    spot_guess = best[2] + best[1]
                # Prefer the underlying's REAL spot over the catalog median so
                # the cap is centred on spot even when no option leg is ticking.
                if spot_guess is None:
                    spot_guess = und_spot
                if spot_guess is None and all_rows:
                    spot_guess = float(all_rows[len(all_rows) // 2]["strike"])

                if spot_guess and spot_guess > 0:
                    lo_bound = spot_guess * (1 - far_pct / 100.0)
                    hi_bound = spot_guess * (1 + far_pct / 100.0)
                    all_rows = [
                        r for r in all_rows
                        if lo_bound <= float(r["strike"]) <= hi_bound
                    ]

    # ── Trim BEFORE we touch Kite ────────────────────────────────────
    # Two-stage ATM detection so we can shrink the work BEFORE doing the
    # expensive subscribe + quote step:
    #   1. Use any cached LTP from the in-memory ticker map to find a real
    #      ATM (parity-derived spot ≈ strike where |CE-PE| is smallest).
    #   2. If no LTPs are cached yet (cold start), fall back to the median
    #      strike — close enough for the first paint; the next 2 s poll
    #      will have real LTPs and recentre.
    # Per-option-segment 'strikes around ATM' (Index / Stock / MCX / BSE
    # option), resolved through the user's expiry-override cascade; falls back
    # to the flat platform-wide value when no tier set a per-segment count.
    _flat_strikes = int(await _read_setting("option_chain.strikes_around_atm", _DEFAULT_STRIKES_AROUND_ATM))
    strikes_around_atm = _effective_strikes_around_atm(_resolved_exp, _opt_seg, _flat_strikes)

    def _row_cached_ltp(row: dict[str, Any], side: str) -> float | None:
        cell = row.get(side)
        if not cell:
            return None
        try:
            tok_int = int(cell.get("token") or 0)
        except (TypeError, ValueError):
            tok_int = 0
        live = _zerodha.ticks_by_token.get(tok_int) if tok_int else None
        if live is None:
            sym = cell.get("symbol")
            if sym:
                live = _zerodha.ticks_by_symbol.get(sym)
        if live is None:
            return None
        try:
            return float(live.get("ltp") or 0) or None
        except (TypeError, ValueError):
            return None

    pre_atm_idx = len(all_rows) // 2
    if all_rows:
        with_both = [
            (i, abs(c - p))
            for i, r in enumerate(all_rows)
            if (c := _leg_ltp(r.get("ce"))) is not None
            and (p := _leg_ltp(r.get("pe"))) is not None
        ]
        if with_both:
            pre_atm_idx = min(with_both, key=lambda x: x[1])[0]
        elif und_spot:
            # No option LTPs (nothing subscribed yet) — centre on the
            # underlying's real spot instead of the catalog median so the
            # window sits ON spot and the higher strikes render too.
            pre_atm_idx = min(
                range(len(all_rows)),
                key=lambda i: abs(float(all_rows[i]["strike"]) - und_spot),
            )

    if all_rows and strikes_around_atm > 0:
        lo = max(0, pre_atm_idx - strikes_around_atm)
        hi = min(len(all_rows), pre_atm_idx + strikes_around_atm + 1)
        rows = all_rows[lo:hi]
    else:
        rows = all_rows

    # ── Enrich ONLY the visible window with live prices ──────────────
    tokens_for_ws: list[int] = []
    sym_map_for_ws: dict[int, dict[str, str]] = {}
    for r in rows:
        for side in ("ce", "pe"):
            cell = r.get(side)
            if cell and cell.get("exchange") and cell.get("symbol"):
                try:
                    t = int(cell["token"])
                    tokens_for_ws.append(t)
                    sym_map_for_ws[t] = {"symbol": cell["symbol"], "exchange": cell["exchange"]}
                except (TypeError, ValueError):
                    pass

    # On-demand subscribe ONLY the visible window. Don't await on it (fire-
    # and-forget) so a slow WS spawn can't block the response.
    if tokens_for_ws:
        try:
            asyncio.create_task(
                _zerodha.subscribe_tokens_on_demand(tokens_for_ws, sym_map_for_ws)
            )
        except Exception:
            pass

    # ── Cross-worker price snapshot (mdlive) ─────────────────────────
    # The feed leader mirrors every subscribed token's quote into Redis
    # `mdlive:{token}`. Reading it here (ONE MGET) lets workers serve live
    # option-chain prices for ALREADY-subscribed strikes without a Kite REST
    # call. It is a COMPLEMENT to the REST fallback below, not a replacement:
    # gating REST on the leader broke prices entirely when nothing was
    # subscribed (auto-subscribe off) — mdlive was empty AND non-leaders had
    # no REST, so every strike showed "—". REST /quote is a DIRECT price query
    # (no WS subscription needed), so it must stay available on every worker.
    _mdlive_quotes: dict[str, dict[str, Any]] = {}
    try:
        from app.services import market_data_service as _mds2

        if tokens_for_ws:
            _mdlive_quotes = await _mds2.get_quote_batch_mdlive(
                [str(t) for t in tokens_for_ws]
            )
    except Exception:
        _mdlive_quotes = {}

    # Kite REST for legs missing from BOTH the in-process ticker AND mdlive —
    # on ANY worker. mdlive-covered legs skip REST (that is the multi-worker
    # saving vs the old REST-everything path), but anything not yet ticking
    # still gets a live price straight from Kite /quote so no strike is blank.
    batch_snapshots: dict[str, dict[str, Any]] = {}
    batch_error: str | None = None
    rest_keys: list[str] = []
    for r in rows:
        for side in ("ce", "pe"):
            cell = r.get(side)
            if not (cell and cell.get("exchange") and cell.get("symbol")):
                continue
            tok = cell.get("token")
            tok_str = str(tok) if tok is not None else ""
            try:
                in_proc = _zerodha.ticks_by_token.get(int(tok)) if tok else None
            except (TypeError, ValueError):
                in_proc = None
            if in_proc is None and not _mdlive_quotes.get(tok_str):
                rest_keys.append(f"{cell['exchange']}:{cell['symbol']}")
    if rest_keys:
        # Shared Redis quote cache + single-flight lock. Kite's REST /quote is
        # rate-limited to ~1 req/s, so 4 gunicorn workers each firing a batch
        # every 1 s poll blew past it → 429 → the far strikes painted once then
        # blanked a second later. Now: read the shared cache first; only ONE
        # worker (the lock winner) actually RESTs the still-missing keys and
        # writes the snapshots back to Redis; every other worker reads them.
        # The cluster issues ~one /quote per _OCQ_TTL window instead of ~4/s.
        from app.core.redis_client import get_redis as _get_redis

        # Seconds a REST snapshot is shared (and the single-flight lock is held)
        # before a refresh. REST is a TRANSITIONAL price source: it only feeds a
        # strike during the ~1-3 s warm-up before its live WS tick lands, after
        # which mdlive takes over. So a few seconds of shared staleness here is
        # invisible, while HALVING the Kite /quote call rate (one per 4 s per
        # underlying instead of one per 2 s) — which is what was tripping Kite's
        # ~1 req/s limit (46 timeouts / 2 h observed) and freezing the chain for
        # a beat until it "fixed itself" once the WS caught up. Fewer 429s → the
        # warm-up window stays smoothly priced instead of stalling.
        _OCQ_TTL = 4  # was 2 — see note above

        async def _ocq_read(keys: list[str]) -> None:
            """Merge any Redis-cached snapshots for `keys` into batch_snapshots."""
            try:
                raw = await _get_redis().mget([f"ocq:{k}" for k in keys])
            except Exception:
                return
            for k, v in zip(keys, raw):
                if v and k not in batch_snapshots:
                    try:
                        batch_snapshots[k] = json.loads(v)
                    except Exception:
                        pass

        await _ocq_read(rest_keys)
        fetch_keys = [k for k in rest_keys if k not in batch_snapshots]
        if fetch_keys:
            got_lock = False
            try:
                got_lock = await idempotency_check_and_set(
                    f"lock:ocq:{und_key}:{target or ''}", ttl_sec=_OCQ_TTL
                )
            except Exception:
                got_lock = True  # Redis down → behave like a single worker
            if got_lock:
                try:
                    fresh, batch_error = await asyncio.wait_for(
                        _zerodha.get_quotes_batch_snapshot(fetch_keys),
                        timeout=_KITE_BATCH_QUOTE_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    fresh, batch_error = {}, f"Kite /quote timed out after {_KITE_BATCH_QUOTE_TIMEOUT_SEC}s"
                except Exception as e:
                    fresh, batch_error = {}, str(e)
                if fresh:
                    batch_snapshots.update(fresh)
                    try:
                        pipe = _get_redis().pipeline(transaction=False)
                        for k, snap in fresh.items():
                            pipe.setex(f"ocq:{k}", _OCQ_TTL, json.dumps(snap, default=str))
                        await pipe.execute()
                    except Exception:
                        pass
            else:
                # A sibling worker is fetching from Kite right now. Poll the
                # shared cache briefly for its result instead of RESTing too.
                for _ in range(5):  # ~1 s
                    await asyncio.sleep(0.2)
                    await _ocq_read(fetch_keys)
                    if all(k in batch_snapshots for k in fetch_keys):
                        break

    def enrich(leg: dict[str, Any] | None) -> dict[str, Any] | None:
        if leg is None:
            return None

        token = leg.get("token")
        symbol = leg.get("symbol")
        exchange = leg.get("exchange")

        # 1) Live tick (KiteTicker push)
        live: dict[str, Any] | None = None
        source: str | None = None
        try:
            live = _zerodha.ticks_by_token.get(int(token)) if token else None
            if live is not None:
                source = "live"
        except (TypeError, ValueError):
            live = None
        if live is None and symbol:
            sym_live = _zerodha.ticks_by_symbol.get(symbol)
            if sym_live is not None:
                live = sym_live
                source = "live"

        # 2) Cross-worker snapshot from the leader's mdlive (Redis). This is
        #    the price source on NON-LEADER workers, where ticks_by_token is
        #    empty — without it every strike showed "—" under multi-worker.
        if live is None and token is not None:
            md = _mdlive_quotes.get(str(token))
            if md:
                live = md
                source = "mdlive"

        # 3) REST batch snapshot pre-fetched above (feed leader only)
        if live is None and exchange and symbol:
            key = f"{exchange}:{symbol}"
            snap = batch_snapshots.get(key)
            if snap is not None:
                live = snap
                source = "rest"

        if not live:
            return {
                **leg,
                "ltp": None, "bid": None, "ask": None,
                "change_pct": None, "volume": None, "source": None,
            }

        ltp = live.get("ltp")
        prev_close = live.get("close") or live.get("prev_close")
        change_pct = None
        try:
            if ltp is not None and prev_close:
                change_pct = round(((float(ltp) - float(prev_close)) / float(prev_close)) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None

        return {
            **leg,
            "ltp": float(ltp) if ltp is not None else None,
            "bid": float(live["bid"]) if live.get("bid") is not None else None,
            "ask": float(live["ask"]) if live.get("ask") is not None else None,
            "change_pct": change_pct,
            "volume": int(live["volume"]) if live.get("volume") is not None else None,
            "source": source,
        }

    # Per-symbol block — drop strikes whose CE / PE symbol is blocked
    # for this user by an admin / broker / user-level override. If
    # both legs are blocked the strike row disappears entirely; if
    # only one side is blocked it's nulled so the chain still shows
    # the remaining leg.
    from app.services.netting_service import (
        get_user_blocked_symbols,
        is_symbol_blocked_for,
    )

    blocked = await get_user_blocked_symbols(user.id)

    def _filter_leg(leg: dict[str, Any] | None) -> dict[str, Any] | None:
        if leg is None:
            return None
        sym = leg.get("symbol") or ""
        if is_symbol_blocked_for(sym, blocked):
            return None
        return leg

    enriched_rows = []
    for r in rows:
        ce_leg = _filter_leg(r["ce"])
        pe_leg = _filter_leg(r["pe"])
        if ce_leg is None and pe_leg is None:
            continue
        enriched_rows.append(
            {
                "strike": r["strike"],
                "ce": enrich(ce_leg),
                "pe": enrich(pe_leg),
            }
        )

    # ATM: strike where |CE LTP - PE LTP| is smallest
    atm_strike = None
    atm_spot = None
    if enriched_rows:
        with_both = [r for r in enriched_rows if r["ce"] and r["pe"] and r["ce"].get("ltp") and r["pe"].get("ltp")]
        if with_both:
            best = min(with_both, key=lambda r: abs(r["ce"]["ltp"] - r["pe"]["ltp"]))
            atm_strike = best["strike"]
            atm_spot = best["strike"] + best["ce"]["ltp"] - best["pe"]["ltp"]
        else:
            atm_strike = enriched_rows[len(enriched_rows) // 2]["strike"]

    # No second trim — we already trimmed BEFORE enrichment (above).

    # Aggregate data source
    leg_sources = [
        cell.get("source")
        for r in enriched_rows
        for side in ("ce", "pe")
        if (cell := r.get(side)) and cell.get("source")
    ]
    # mdlive is the leader's LIVE WS ticks mirrored through Redis, so it counts
    # as "live" for the frontend badge (which only knows live / rest / none —
    # a raw "mdlive" fell through to the red "NO DATA" pill even though prices
    # were streaming fine).
    data_source = (
        "live" if ("live" in leg_sources or "mdlive" in leg_sources)
        else "rest" if "rest" in leg_sources
        else "none"
    )

    from app.utils.time_utils import is_market_open as _is_market_open
    response_data = {
        "underlying": und_key,
        "expiries": expiry_iso,
        "selected_expiry": target.isoformat() if target else None,
        "atm_strike": atm_strike,
        "atm_spot": atm_spot,
        "rows": enriched_rows,
        "data_source": data_source,
        "data_source_error": batch_error,
        # The picker drops the day-change pill when this is False so the
        # strip looks clean after-hours (no big red −20 % numbers on stale
        # ticks). LTP itself is still the last traded price from REST/WS.
        "market_open": _is_market_open(),
    }
    # Cache the full response — next call within _CHAIN_TTL hits the early
    # return above and skips all this work.
    _CHAIN_CACHE[cache_key] = (response_data, time.time())
    return APIResponse(data=response_data)
