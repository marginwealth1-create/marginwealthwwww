"""Check for _align_candles_to_live — the only non-trivial branch added.

Reads the real source and exec's just that function, so it needs no DB /
Redis / third-party imports. Run: python backend/tests/test_align_candles.py
"""
import ast
import asyncio
import logging
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "user" / "instruments.py"
tree = ast.parse(SRC.read_text(encoding="utf-8"))

# ── 1. The helper must sit ABOVE the route decorator ────────────────────
# Defined between `@router.get(".../history")` and `async def history`, the
# decorator would bind the route to the helper and every call would 422.
history_fn = next(
    n for n in tree.body
    if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "history"
)
assert any(
    isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
    and isinstance(d.args[0], ast.Constant) and d.args[0].value.endswith("/history")
    for d in history_fn.decorator_list
), "the /history decorator no longer sits on the `history` coroutine"

# ── 2. The offset logic ─────────────────────────────────────────────────
helper = next(
    n for n in tree.body
    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_align_candles_to_live"
)
ns = {"logger": logging.getLogger("t"), "market_data_service": None}
exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SRC), "exec"), ns)
align = ns["_align_candles_to_live"]


def run(live, candles):
    async def fake_quote(_token):
        return {"ltp": live}

    ns["market_data_service"] = types.SimpleNamespace(get_quote=fake_quote)
    return asyncio.run(align("XAUUSD", candles))


def bar(c):
    return {"open": c, "high": c + 1, "low": c - 1, "close": c}


# Futures-scaled history (~50 pts off spot) → shifted onto the live scale.
out = run(4000.0, [bar(3990.0), bar(4050.0)])
assert out[-1]["close"] == 4000.0, out          # last bar meets the live price
assert out[0]["close"] == 3940.0, out           # same -50 offset on every bar
assert out[0]["high"] - out[0]["low"] == 2.0    # intraday shape untouched

# Same-scale feed (forex "=X", drift < 0.15%) → left alone.
assert run(4000.5, [bar(4000.0)])[0]["close"] == 4000.0

# No live price / no candles → left alone, never raises.
assert run(0.0, [bar(100.0)])[0]["close"] == 100.0
assert run(4000.0, []) == []

print("ok")
