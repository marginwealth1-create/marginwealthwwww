"""Admin chart lines: Excel round-trip + the user-side resolver.

The admin picks a segment, downloads a workbook pre-filled with every
instrument in it, types up to ``MAX_LEVELS`` price/colour pairs per row, and
uploads it back. Each price becomes a horizontal line on that instrument's
chart in the colour that was given.

Ownership and the user-side cascade mirror ``crypto_config_service`` exactly —
the same resolver rule already backs company banks and crypto configs, and a
second, subtly different one would be a bug waiting to happen.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal
from io import BytesIO
from typing import Any

from beanie import PydanticObjectId
from beanie.operators import In
from bson import Decimal128
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models.chart_level import ChartLevel, ChartLevelEntry
from app.models.instrument import Instrument
from app.models.user import User, UserRole
from app.utils.decimal_utils import to_decimal

MAX_LEVELS = 6

# Readable defaults so a sheet filled in without touching the colour columns
# still yields DISTINGUISHABLE lines rather than six identical ones.
DEFAULT_COLORS = [
    "#E31E24", "#0EA5E9", "#16A34A", "#F59E0B", "#7C3AED", "#EC4899",
]

_HEX_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

# Colour words an operator is likely to type instead of a hex code.
_NAMED: dict[str, str] = {
    "red": "#E31E24", "green": "#16A34A", "blue": "#2563EB", "yellow": "#EAB308",
    "orange": "#F59E0B", "purple": "#7C3AED", "violet": "#7C3AED", "pink": "#EC4899",
    "cyan": "#06B6D4", "sky": "#0EA5E9", "black": "#111111", "white": "#FFFFFF",
    "grey": "#6B7280", "gray": "#6B7280", "brown": "#92400E", "teal": "#14B8A6",
    "lime": "#84CC16", "magenta": "#D946EF", "gold": "#D4AF37", "silver": "#C0C0C0",
}

HEADERS = ["Token", "Symbol", "Segment"]
for _i in range(1, MAX_LEVELS + 1):
    # Line (the text on the line), then its colour, then the price it sits at
    # — the order the operator fills them in.
    HEADERS += [f"Line {_i}", f"Color {_i}", f"Price {_i}"]


def _round_like_tick(price: Decimal, tick: Any) -> Decimal:
    """Trim a price to the instrument's DECIMAL PLACES — not to a tick multiple.

    Excel hands back the cached value of a formula, so a cell that reads
    4249.17 arrives as 4249.17065447777 and every chip and line label then
    carries fourteen digits. Rounding to the tick's precision fixes that
    while leaving the operator's own number alone; snapping to a tick
    MULTIPLE would silently move 4249.17 to 4249.15 on a 0.05-tick
    instrument, and a level is a marker, not an order.
    """
    exp = -2
    try:
        t = to_decimal(tick)
        if t > 0 and isinstance(t.as_tuple().exponent, int):
            exp = int(t.as_tuple().exponent)
    except Exception:  # noqa: BLE001
        pass
    places = min(max(-exp, 0), 8)
    q = price.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN).normalize()
    # normalize() turns 100.00 into 1E+2 — put it back into plain notation.
    return q.quantize(Decimal(1)) if q.as_tuple().exponent > 0 else q


# ── segment labels ───────────────────────────────────────────────────
# Two segment taxonomies exist in this codebase: the netting one the Segment
# Settings page shows (NSE_STK_FUT, FOREX, CRYPTO …) and the one instruments
# actually carry (NSE_FUTURE, CRYPTO_SPOT, FOREX …). They overlap but are not
# the same, so the picker is labelled from BOTH — netting display names are
# reused rather than retyped, and the instrument-only names get their own
# entries. Anything unmapped falls back to a title-cased form instead of the
# raw enum, so a new segment can never show up as SCREAMING_SNAKE_CASE.
_INSTRUMENT_SEGMENT_LABELS: dict[str, str] = {
    "NSE_EQUITY": "NSE Equity",
    "NSE_FUTURE": "Stock Future",
    "NSE_INDEX_FUTURE": "Index Future",
    "NSE_STOCK_OPTION_BUY": "Stock Option — Buy",
    "NSE_STOCK_OPTION_SELL": "Stock Option — Sell",
    "NSE_INDEX_OPTION_BUY": "Index Option — Buy",
    "NSE_INDEX_OPTION_SELL": "Index Option — Sell",
    "BSE_EQUITY": "BSE Equity",
    "BSE_FUTURE": "BSE Future",
    "BSE_INDEX_FUTURE": "BSE Index Future",
    "BSE_OPTION_BUY": "BSE Option — Buy",
    "BSE_OPTION_SELL": "BSE Option — Sell",
    "MCX_FUTURE": "MCX Future",
    "MCX_OPTION_BUY": "MCX Option — Buy",
    "MCX_OPTION_SELL": "MCX Option — Sell",
    "CDS_FUTURE": "Currency Future",
    "CDS_OPTION_BUY": "Currency Option — Buy",
    "CDS_OPTION_SELL": "Currency Option — Sell",
    "CRYPTO_SPOT": "Crypto Spot",
    "CRYPTO_FUTURE": "Crypto Future",
}


def segment_label(name: str) -> str:
    from app.services.netting_service import SEGMENT_DEFAULTS

    raw = str(name or "").strip()
    if not raw:
        return ""
    for d in SEGMENT_DEFAULTS:
        if d.get("name") == raw:
            return str(d.get("displayName") or raw)
    if raw in _INSTRUMENT_SEGMENT_LABELS:
        return _INSTRUMENT_SEGMENT_LABELS[raw]
    return raw.replace("_", " ").title()


async def available_segments() -> list[dict[str, Any]]:
    """Segments that actually HAVE instruments, with counts.

    Deliberately not the SegmentType enum: instruments carry values the enum
    doesn't contain (FOREX, COMMODITIES), so an enum-driven picker silently
    made those two impossible to configure — and offered thirteen segments
    that would download an empty sheet.
    """
    rows = await Instrument.get_motor_collection().aggregate(
        [
            {"$group": {"_id": "$segment", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    ).to_list(length=None)
    out: list[dict[str, Any]] = []
    for r in rows:
        name = r.get("_id")
        if not name:
            continue
        out.append(
            {"value": str(name), "label": segment_label(str(name)), "count": r.get("count", 0)}
        )
    return out


# ── ownership ────────────────────────────────────────────────────────
def owner_filter_for_admin(admin: User) -> dict[str, PydanticObjectId | None]:
    """The (owner_admin_id, owner_broker_id) pair identifying THIS actor's
    rows. Super-admin writes the platform default (both null)."""
    if admin.role == UserRole.SUPER_ADMIN:
        return {"owner_admin_id": None, "owner_broker_id": None}
    if admin.role == UserRole.BROKER:
        return {"owner_admin_id": None, "owner_broker_id": admin.id}
    return {"owner_admin_id": admin.id, "owner_broker_id": None}


def normalize_color(raw: Any, fallback: str) -> str:
    """Accept '#RGB', '#RRGGBB', bare hex, or a colour name.

    Anything unrecognised falls back rather than passing through: an invalid
    CSS colour reaches TradingView as a black or invisible line with no error
    anywhere, which reads as "the feature is broken".
    """
    s = str(raw or "").strip()
    if not s:
        return fallback
    if _HEX_RE.match(s):
        return s.upper()
    if _HEX_RE.match("#" + s):
        return ("#" + s).upper()
    return _NAMED.get(s.lower(), fallback)


# ── template ─────────────────────────────────────────────────────────
async def build_template(admin: User, segment: str) -> bytes:
    """Workbook of every instrument in `segment`, pre-filled with whatever
    this admin already saved so editing is a round-trip, not a retype."""
    instruments = await Instrument.find(Instrument.segment == segment).to_list()
    instruments.sort(key=lambda i: (i.symbol or ""))

    f = owner_filter_for_admin(admin)
    existing = {
        r.token: r
        for r in await ChartLevel.find(
            ChartLevel.owner_admin_id == f["owner_admin_id"],
            ChartLevel.owner_broker_id == f["owner_broker_id"],
            ChartLevel.segment == segment,
        ).to_list()
    }

    wb = Workbook()
    ws = wb.active
    # Sheet tab and the Segment column both show the friendly label, matching
    # the Segment Settings page. The parser keys off Token, never these.
    label = segment_label(segment)
    ws.title = (label or "Levels")[:31]

    head_fill = PatternFill("solid", fgColor="0B1220")
    head_font = Font(bold=True, color="FFFFFF")
    for c, h in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill, cell.font = head_fill, head_font
        cell.alignment = Alignment(horizontal="center")

    for r, inst in enumerate(instruments, start=2):
        ws.cell(row=r, column=1, value=inst.token)
        ws.cell(row=r, column=2, value=inst.symbol)
        ws.cell(row=r, column=3, value=segment_label(str(inst.segment)))
        saved = existing.get(inst.token)
        for i in range(MAX_LEVELS):
            base = 4 + i * 3  # Line, Color, Price
            entry = saved.levels[i] if saved and i < len(saved.levels) else None
            ws.cell(row=r, column=base, value=(entry.label or "") if entry else None)
            # The colour is seeded even on an empty line, so typing only a
            # price still yields six distinct colours.
            ws.cell(
                row=r,
                column=base + 1,
                value=entry.color if entry else DEFAULT_COLORS[i],
            )
            if entry is not None:
                ws.cell(
                    row=r,
                    column=base + 2,
                    value=float(_round_like_tick(entry.price.to_decimal(), inst.tick_size)),
                )

    for c in range(1, len(HEADERS) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 16
    ws.freeze_panes = "A2"

    # A second sheet documenting the columns — whoever fills this in is not
    # the person who wrote the parser.
    guide = wb.create_sheet("How to fill")
    rows = [
        ("Column", "What to put"),
        ("Token / Symbol / Segment", "Leave as-is. Token identifies the instrument."),
        (f"Line 1..{MAX_LEVELS}", "Optional text shown on the line, e.g. R1, Support."),
        (f"Color 1..{MAX_LEVELS}", "Hex like #E31E24, or a name: red, green, blue, orange, purple, ..."),
        (f"Price 1..{MAX_LEVELS}", "The price to draw the line at. Leave blank for no line."),
        ("", ""),
        ("Note", "Re-uploading REPLACES that instrument's lines with what the sheet says."),
        ("Note", "A row with every price blank CLEARS that instrument's lines."),
        ("Note", "Columns are found by their HEADING, so a sheet from an older build still imports."),
    ]
    for r, (a, b) in enumerate(rows, start=1):
        guide.cell(row=r, column=1, value=a).font = Font(bold=(r == 1))
        guide.cell(row=r, column=2, value=b).font = Font(bold=(r == 1))
    guide.column_dimensions["A"].width = 26
    guide.column_dimensions["B"].width = 78

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── import ───────────────────────────────────────────────────────────
_COL_RE = re.compile(r"\s*(price|colou?r|line|label)\s*#?\s*(\d+)\s*$", re.I)


def _column_map(header: tuple[Any, ...]) -> dict[int, dict[str, int]]:
    """{level index: {"price"/"color"/"label": column index}} read off the
    HEADING row rather than fixed offsets, so a sheet downloaded from an
    older build (Price / Color / Label, four levels) imports exactly as well
    as the current one (Line / Color / Price, six)."""
    out: dict[int, dict[str, int]] = {}
    for idx, cell in enumerate(header or ()):
        m = _COL_RE.match(str(cell or ""))
        if not m:
            continue
        kind = m.group(1).lower()
        key = "price" if kind == "price" else "color" if kind.startswith("col") else "label"
        out.setdefault(int(m.group(2)) - 1, {})[key] = idx
    return {k: v for k, v in out.items() if "price" in v}


def _cell(row: tuple[Any, ...], cols: dict[str, int], key: str) -> Any:
    idx = cols.get(key)
    return row[idx] if idx is not None and len(row) > idx else None


async def import_workbook(admin: User, data: bytes) -> dict[str, Any]:
    """Parse an uploaded template and upsert this admin's rows.

    Collects problems instead of raising on the first bad cell — an operator
    with forty rows wants every error at once, not one per upload.
    """
    try:
        wb = load_workbook(BytesIO(data), data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Not a readable .xlsx file: {exc}") from exc
    ws = wb.worksheets[0]

    rows = list(ws.iter_rows(min_row=1, values_only=True))
    if not rows:
        raise ValueError("The sheet is empty.")
    cols = _column_map(rows[0])
    if not cols:
        raise ValueError(
            "Could not find the 'Price 1' / 'Color 1' headings — upload the "
            "sheet downloaded from this page, with its heading row intact."
        )

    f = owner_filter_for_admin(admin)
    known = {i.token: i for i in await Instrument.find().to_list()}

    updated = cleared = 0
    errors: list[str] = []
    touched: dict[str, list[Decimal]] = {}

    for row in rows[1:]:
        if not row or row[0] in (None, ""):
            continue
        token = str(row[0]).strip()
        inst = known.get(token)
        if inst is None:
            errors.append(f"{token}: not an instrument on this platform - skipped")
            continue

        entries: list[ChartLevelEntry] = []
        for i in sorted(cols):
            c = cols[i]
            price_raw = row[c["price"]] if len(row) > c["price"] else None
            if price_raw in (None, ""):
                continue
            try:
                # Operators paste formatted numbers ("4,249.17") straight out
                # of another sheet — a comma shouldn't reject the row.
                raw = price_raw.replace(",", "").strip() if isinstance(price_raw, str) else price_raw
                price = _round_like_tick(to_decimal(raw), inst.tick_size)
            except Exception:  # noqa: BLE001
                errors.append(f"{inst.symbol}: 'Price {i + 1}' = {price_raw!r} is not a number")
                continue
            if price <= 0:
                errors.append(f"{inst.symbol}: 'Price {i + 1}' must be greater than 0")
                continue

            label = _cell(row, c, "label")
            entries.append(
                ChartLevelEntry(
                    price=Decimal128(str(price)),
                    color=normalize_color(
                        _cell(row, c, "color"), DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
                    ),
                    label=(str(label).strip() or None) if label else None,
                )
            )

        n = await _upsert(admin, inst, entries, f)
        if n is None:
            continue
        if n:
            updated += 1
            touched[token] = [e.price.to_decimal() for e in entries]
        else:
            cleared += 1

    return {
        "updated": updated,
        "cleared": cleared,
        "errors": errors,
        "warnings": await _offscreen_warnings(touched, known),
    }


async def _upsert(
    admin: User,
    inst: Instrument,
    entries: list[ChartLevelEntry],
    f: dict[str, PydanticObjectId | None],
) -> bool | None:
    """Write one instrument's lines. True = saved, False = cleared, None =
    nothing to do. Shared by the Excel import and the inline editor so the
    two can never drift apart."""
    doc = await ChartLevel.find_one(
        ChartLevel.owner_admin_id == f["owner_admin_id"],
        ChartLevel.owner_broker_id == f["owner_broker_id"],
        ChartLevel.token == inst.token,
    )
    if not entries:
        # Every price blank = clear this instrument's lines.
        if doc is None:
            return None
        await doc.delete()
        return False
    if doc is None:
        doc = ChartLevel(
            owner_admin_id=f["owner_admin_id"],
            owner_broker_id=f["owner_broker_id"],
            token=inst.token,
            symbol=inst.symbol,
            segment=str(inst.segment),
        )
    doc.symbol = inst.symbol
    doc.segment = str(inst.segment)
    doc.levels = entries
    await doc.save()
    return True


# How far from the live price a level has to be before it is almost certainly
# a mistake. A line ten times off isn't "wrong", it is simply drawn where no
# one will ever scroll to — which reads as "the feature does nothing".
_OFFSCREEN_RATIO = Decimal("10")
_OFFSCREEN_MAX_CHECKS = 60


async def _offscreen_warnings(
    touched: dict[str, list[Decimal]], known: dict[str, Instrument]
) -> list[str]:
    """Flag levels that sit nowhere near the instrument's live price.

    This is the whole of the "I uploaded prices and no line appeared" report:
    XAUUSD trades near 4300 and had lines at 63, so they were drawn miles
    below the visible range. Cheap to check, and it names the row to fix.
    """
    tokens = list(touched)[:_OFFSCREEN_MAX_CHECKS]
    if not tokens:
        return []
    from app.services import market_data_service

    try:
        quotes = await market_data_service.get_quotes(tokens)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for token, q in zip(tokens, quotes, strict=False):
        try:
            ltp = to_decimal(q.get("ltp") or 0)
        except Exception:  # noqa: BLE001
            continue
        if ltp <= 0:
            continue
        sym = known[token].symbol if token in known else token
        for price in touched[token]:
            if price > ltp * _OFFSCREEN_RATIO or price * _OFFSCREEN_RATIO < ltp:
                out.append(
                    f"{sym}: {price} is far from the live price {ltp} — that line "
                    f"is drawn off-screen. Check the row."
                )
    return out


# ── reads ────────────────────────────────────────────────────────────
def to_dict(doc: ChartLevel, tick: Any = None) -> dict[str, Any]:
    return {
        "token": doc.token,
        "symbol": doc.symbol,
        "segment": doc.segment,
        "levels": [
            {
                # Rounded on the way out as well as in: rows written before the
                # rounding existed still hold Excel's fourteen-digit float.
                "price": float(_round_like_tick(e.price.to_decimal(), tick)),
                "color": e.color,
                "label": e.label,
            }
            for e in doc.levels
        ],
    }


async def list_for_admin(admin: User, segment: str | None = None) -> list[dict[str, Any]]:
    f = owner_filter_for_admin(admin)
    q = ChartLevel.find(
        ChartLevel.owner_admin_id == f["owner_admin_id"],
        ChartLevel.owner_broker_id == f["owner_broker_id"],
    )
    if segment:
        q = q.find(ChartLevel.segment == segment)
    docs = await q.to_list()
    if not docs:
        return []
    tokens = [d.token for d in docs]
    ticks = {
        i.token: i.tick_size
        for i in await Instrument.find({"token": {"$in": tokens}}).to_list()
    }
    # The live price next to the lines is what makes a fat-fingered row
    # obvious — a level ten times off the LTP draws off-screen and reads as
    # "the feature is broken".
    ltps: dict[str, float] = {}
    try:
        from app.services import market_data_service

        quotes = await market_data_service.get_quotes(tokens)
        for token, q2 in zip(tokens, quotes, strict=False):
            ltps[token] = float(
                _round_like_tick(to_decimal(q2.get("ltp") or 0), ticks.get(token))
            )
    except Exception:  # noqa: BLE001
        pass
    out = []
    for d in docs:
        row = to_dict(d, ticks.get(d.token))
        row["ltp"] = ltps.get(d.token) or None
        out.append(row)
    return out


async def save_levels(
    admin: User, token: str, levels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Inline editor: replace one instrument's lines without the Excel trip.

    Same validation and the same writer as the import, so a price typed here
    is rounded and colour-checked exactly like one typed in the sheet.
    """
    inst = await Instrument.find_one(Instrument.token == token)
    if inst is None:
        raise ValueError("Not an instrument on this platform.")
    entries: list[ChartLevelEntry] = []
    for i, lv in enumerate(levels[:MAX_LEVELS]):
        raw = lv.get("price")
        if raw in (None, ""):
            continue
        try:
            clean = raw.replace(",", "").strip() if isinstance(raw, str) else raw
            price = _round_like_tick(to_decimal(clean), inst.tick_size)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Price {i + 1} is not a number.") from exc
        if price <= 0:
            raise ValueError(f"Price {i + 1} must be greater than 0.")
        label = str(lv.get("label") or "").strip() or None
        entries.append(
            ChartLevelEntry(
                price=Decimal128(str(price)),
                color=normalize_color(lv.get("color"), DEFAULT_COLORS[i % len(DEFAULT_COLORS)]),
                label=label,
            )
        )
    saved = await _upsert(admin, inst, entries, owner_filter_for_admin(admin))
    return {"saved": bool(saved), "levels": len(entries)}


async def clear_for_admin(admin: User, token: str) -> bool:
    f = owner_filter_for_admin(admin)
    doc = await ChartLevel.find_one(
        ChartLevel.owner_admin_id == f["owner_admin_id"],
        ChartLevel.owner_broker_id == f["owner_broker_id"],
        ChartLevel.token == token,
    )
    if doc is None:
        return False
    await doc.delete()
    return True


async def resolve_for_user(user: User, token: str) -> list[dict[str, Any]]:
    """Lines the USER should see for `token`. Closest owner in the cascade
    wins — identical ordering to ``crypto_config_service.resolve_for_user``."""
    tried: list[dict[str, PydanticObjectId | None]] = []
    if user.assigned_broker_id is not None:
        tried.append({"owner_admin_id": None, "owner_broker_id": user.assigned_broker_id})
    for parent in reversed(list(user.broker_ancestry or [])):
        if parent == user.assigned_broker_id:
            continue
        tried.append({"owner_admin_id": None, "owner_broker_id": parent})
    if user.assigned_admin_id is not None:
        tried.append({"owner_admin_id": user.assigned_admin_id, "owner_broker_id": None})
    else:
        # Direct super-admin user only — never leak the platform default to a
        # sub-admin's users (same rule as company banks).
        tried.append({"owner_admin_id": None, "owner_broker_id": None})

    # The chart addresses some instruments by SYMBOL where the catalog keys
    # them by token — crypto is "BTCUSD" on screen and "CRYPTO_BTCUSD" in the
    # collection — so a token-only match silently returned no lines there.
    inst = await Instrument.find_one(Instrument.token == token)
    if inst is None:
        inst = await Instrument.find_one(Instrument.symbol == token)
    tokens = {token}
    if inst is not None:
        tokens.add(inst.token)

    for f in tried:
        doc = await ChartLevel.find_one(
            ChartLevel.owner_admin_id == f["owner_admin_id"],
            ChartLevel.owner_broker_id == f["owner_broker_id"],
            In(ChartLevel.token, list(tokens)),
        )
        if doc is not None and doc.levels:
            return to_dict(doc, inst.tick_size if inst else None)["levels"]
    return []
