"""Diagnose MetaAPI 0.0000 prices for commodities / indices.

The subscription logs say "success" yet USOIL / UKOIL / NATGAS / SPX500 / DE40
show 0.0000 on the frontend. The near-certain cause is a SYMBOL-NAME mismatch:
the MT broker names those instruments differently (USOIL->WTI/XTIUSD,
NATGAS->XNGUSD, SPX500->US500, DE40->GER40/DE30, ...). ``get_tick`` maps the
platform symbol through ``METAAPI_SYMBOL_MAP`` and calls
``terminal_state.price(symbol=broker_name)``; when the broker name is wrong the
call returns None -> get_tick returns None -> the (expired) Infoway fallback
yields 0 -> the frontend shows 0.0000.

This connects with the SAME live config the backend uses, then:
  1. Lists EVERY symbol the broker actually offers (get_symbols()).
  2. Prints the ones matching oil / gas / index / SPX / DAX keywords so you can
     read off the real broker names.
  3. For each configured platform symbol, resolves it through the current
     alias map and reports whether terminal_state has a live bid/ask.

Read-only. Makes NO changes. Run on the server:

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.diag_metaapi
"""

from __future__ import annotations

import asyncio

from app.core.config import settings

# Platform symbols we care about (the ones reported frozen at 0.0000), plus a
# few known-good forex/metals as a control so you can confirm the feed itself
# is live.
TARGETS = [
    # commodities / energy (reported broken)
    "USOIL", "UKOIL", "NATGAS", "WTI", "BRENT",
    # indices (reported broken)
    "SPX500", "US500", "US30", "DJ30", "NAS100", "USTEC",
    "DE40", "GER40", "GER30", "UK100", "JP225",
    # US stocks — probe BOTH the plain ticker AND the broker's ".US" suffix
    # so we can read off which form this broker actually streams.
    "AAPL", "AAPL.US", "MSFT", "MSFT.US", "GOOGL", "GOOGL.US",
    "AMZN", "AMZN.US", "TSLA", "TSLA.US", "NVDA", "NVDA.US",
    "META", "META.US", "NFLX", "NFLX.US",
    # controls (expected working)
    "EURUSD", "GBPUSD", "XAUUSD", "XAGUSD",
]

# US-stock tickers we want live; used to surface their real broker names in the
# full symbol dump (the broker suffixes them, e.g. AAPL.US).
STOCK_TICKERS = (
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "TSLA", "NVDA", "META",
    "NFLX", "AMD", "INTC", "BA", "DIS", "JPM", "V", "KO", "PEP", "BABA",
)

# keywords used to surface likely broker names in the full symbol list
KEYWORDS = (
    "OIL", "WTI", "BRENT", "GAS", "NG", "XNG", "XTI", "XBR",
    "SPX", "US500", "US30", "DJ", "NAS", "USTEC", "NDX",
    "DE40", "GER", "DAX", "UK100", "FTSE", "JP225", "NIK", "INDEX",
)


async def main() -> None:
    try:
        token = settings.METAAPI_TOKEN.get_secret_value()
    except Exception:
        token = ""
    acc_id = getattr(settings, "METAAPI_ACCOUNT_ID", "")
    region = (getattr(settings, "METAAPI_REGION", "") or "").strip()

    if not (token and acc_id):
        print("❌ METAAPI_TOKEN / METAAPI_ACCOUNT_ID not configured — aborting.")
        return

    # Current alias map exactly as the running feed parses it.
    alias: dict[str, str] = {}
    raw_map = (getattr(settings, "METAAPI_SYMBOL_MAP", "") or "").strip()
    for pair in raw_map.split(","):
        if ":" in pair:
            p, m = pair.split(":", 1)
            p, m = p.strip().upper(), m.strip()
            if p and m:
                alias[p] = m
    print(f"METAAPI_SYMBOL_MAP entries: {len(alias)}")
    for p, m in sorted(alias.items()):
        print(f"    {p:10} -> {m}")
    print()

    from metaapi_cloud_sdk import MetaApi

    api = MetaApi(token, {"region": region}) if region else MetaApi(token)
    account = await api.metatrader_account_api.get_account(acc_id)

    try:
        state = getattr(account, "state", None)
        if state and state not in ("DEPLOYED",):
            await account.deploy()
        await account.wait_connected()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  deploy/wait_connected warning: {str(e)[:160]}")

    # Use an RPC connection (request/response) — NOT streaming. The running
    # backend already holds the ONE streaming connection to this MT account, so
    # a second streaming connection here times out on wait_synchronized. RPC is
    # independent and lightweight (get_symbols / get_symbol_price on demand).
    conn = account.get_rpc_connection()
    await conn.connect()
    try:
        await conn.wait_synchronized({"timeoutInSeconds": 60})
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  rpc wait_synchronized warning (continuing): {str(e)[:120]}")
    print("✅ rpc connected\n")

    # 1) full broker symbol universe
    all_symbols: list[str] = []
    try:
        all_symbols = list(await conn.get_symbols())
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  could not list broker symbols: {str(e)[:160]}")

    print(f"Broker offers {len(all_symbols)} symbols total.")
    if all_symbols:
        matches = sorted(
            s for s in all_symbols
            if s and any(k in s.upper() for k in KEYWORDS)
        )
        print(f"\n── Broker symbols matching oil/gas/index keywords ({len(matches)}) ──")
        for s in matches:
            print(f"    {s}")

        # US-stock names: the broker suffixes them (AAPL.US). Surface every
        # symbol whose base (before a '.') matches a ticker we want, plus any
        # '.US' symbol, so we can build the stock alias map.
        stock_matches = sorted(
            s for s in all_symbols
            if s and (
                s.upper().split(".")[0] in STOCK_TICKERS
                or s.upper().endswith(".US")
            )
        )
        print(f"\n── Broker STOCK symbols (.US / matching tickers) ({len(stock_matches)}) ──")
        for s in stock_matches:
            print(f"    {s}")
    print()

    # 2) probe each target through the CURRENT alias map via an RPC price call
    print("── Live price probe (platform symbol -> broker symbol) ──")
    print(f"{'PLATFORM':12}{'BROKER':14}{'BID':>12}{'ASK':>12}  RESULT")
    print("-" * 70)
    for sym in TARGETS:
        broker = alias.get(sym, sym)
        bid = ask = 0.0
        result = "NO PRICE (name wrong / not offered)"
        try:
            price = await conn.get_symbol_price(broker)
        except Exception as e:  # noqa: BLE001
            price = None
            result = f"price error: {str(e)[:40]}"
        if price:
            bid = float(price.get("bid") or 0)
            ask = float(price.get("ask") or 0)
            if bid > 0 or ask > 0:
                result = "OK — live"
            else:
                result = "price present but bid/ask 0"
        print(f"{sym:12}{broker:14}{bid:>12.4f}{ask:>12.4f}  {result}")

    print("\nNext step: for every PLATFORM row that shows NO PRICE, find its real")
    print("name in the keyword list above and add PLATFORM:BROKER to")
    print("METAAPI_SYMBOL_MAP in backend/.env, then restart the backend.")

    try:
        await conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
