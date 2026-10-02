"""Checks for the chart-lines Excel round-trip.

The parser is the part worth pinning: a colour that silently falls through to
something invalid reaches TradingView as a black or invisible line with no
error anywhere, and a price column read at the wrong offset would attach a
level to the wrong colour.

    pytest -q backend/tests/test_chart_levels.py
"""

from __future__ import annotations

from decimal import Decimal

from app.services.chart_level_service import (
    DEFAULT_COLORS,
    HEADERS,
    MAX_LEVELS,
    _column_map,
    _round_like_tick,
    normalize_color,
)


def test_hex_colors_pass_through_uppercased():
    assert normalize_color("#e31e24", "#000000") == "#E31E24"
    assert normalize_color("#FFF", "#000000") == "#FFF"
    # Bare hex, no leading '#': the operator typed it out of a colour picker.
    assert normalize_color("16A34A", "#000000") == "#16A34A"


def test_color_names_map():
    assert normalize_color("red", "#000000") == "#E31E24"
    assert normalize_color("  Green ", "#000000") == "#16A34A"
    assert normalize_color("BLUE", "#000000") == "#2563EB"


def test_unknown_and_blank_fall_back():
    # Never pass an unrecognised value through — an invalid CSS colour draws a
    # black/invisible line and looks like the feature is broken.
    assert normalize_color("chartreuse-ish", "#123456") == "#123456"
    assert normalize_color("", "#123456") == "#123456"
    assert normalize_color(None, "#123456") == "#123456"
    assert normalize_color("#12345", "#123456") == "#123456"  # 5 digits


def test_header_layout():
    """Line, then its colour, then the price it sits at — the order the
    operator fills them in."""
    assert HEADERS[:3] == ["Token", "Symbol", "Segment"]
    assert len(HEADERS) == 3 + MAX_LEVELS * 3
    for i in range(MAX_LEVELS):
        base = 3 + i * 3
        assert HEADERS[base] == f"Line {i + 1}"
        assert HEADERS[base + 1] == f"Color {i + 1}"
        assert HEADERS[base + 2] == f"Price {i + 1}"


def test_default_colors_are_distinct():
    """A sheet filled in without touching the colour columns must still give
    lines that can be told apart."""
    assert len(DEFAULT_COLORS) == MAX_LEVELS
    assert len(set(DEFAULT_COLORS)) == MAX_LEVELS


def test_column_map_reads_the_current_sheet():
    cols = _column_map(tuple(HEADERS))
    assert len(cols) == MAX_LEVELS
    for i in range(MAX_LEVELS):
        assert cols[i] == {"label": 3 + i * 3, "color": 4 + i * 3, "price": 5 + i * 3}


def test_column_map_still_reads_the_old_four_column_sheet():
    """Sheets downloaded before the reorder had Price / Color / Label and only
    four levels. An operator re-uploading one must not have every price land
    in the label."""
    old = ["Token", "Symbol", "Segment"]
    for i in range(1, 5):
        old += [f"Price {i}", f"Color {i}", f"Label {i}"]
    cols = _column_map(tuple(old))
    assert len(cols) == 4
    assert cols[0] == {"price": 3, "color": 4, "label": 5}
    assert cols[3]["price"] == 12


def test_column_map_ignores_a_sheet_that_is_not_ours():
    assert _column_map(("Name", "Qty", "Rate")) == {}
    # A colour column with no matching price is not a level.
    assert _column_map(("Token", "Colour 1")) == {}


def test_price_is_trimmed_to_the_tick_precision_not_snapped_to_a_tick():
    """Excel hands back a formula's cached float, so 4249.17 arrives as
    4249.17065447777. Trim the tail — but never move the operator's price to
    the nearest tick multiple."""
    assert _round_like_tick(Decimal("4249.17065447777"), Decimal("0.05")) == Decimal("4249.17")
    assert _round_like_tick(Decimal("65.69661920027963"), Decimal("0.01")) == Decimal("65.7")
    # Crypto keeps its precision.
    assert _round_like_tick(
        Decimal("0.000012345678901"), Decimal("0.00000001")
    ) == Decimal("0.00001235")
    # Whole numbers stay whole (normalize() would give 1E+2).
    assert str(_round_like_tick(Decimal("100.00"), Decimal("0.05"))) == "100"
    # No tick on the instrument: fall back to paise.
    assert _round_like_tick(Decimal("1253.4567"), None) == Decimal("1253.46")
