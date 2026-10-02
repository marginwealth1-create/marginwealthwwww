"""Indian-specific format validators (PAN, Aadhaar, IFSC, mobile)."""

from __future__ import annotations

import re

PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
AADHAAR_REGEX = re.compile(r"^\d{12}$")
IFSC_REGEX = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
MOBILE_IN_REGEX = re.compile(r"^[6-9]\d{9}$")
PINCODE_REGEX = re.compile(r"^[1-9]\d{5}$")
GST_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
USER_CODE_REGEX = re.compile(r"^[A-Z]{2,5}\d{3,8}$")


def is_valid_pan(value: str) -> bool:
    return bool(PAN_REGEX.fullmatch(value or ""))


def is_valid_aadhaar(value: str) -> bool:
    if not AADHAAR_REGEX.fullmatch(value or ""):
        return False
    return _verhoeff_validate(value)


def is_valid_ifsc(value: str) -> bool:
    return bool(IFSC_REGEX.fullmatch(value or ""))


def is_valid_mobile_in(value: str) -> bool:
    cleaned = (value or "").lstrip("+").replace(" ", "")
    if cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    return bool(MOBILE_IN_REGEX.fullmatch(cleaned))


def is_valid_pincode(value: str) -> bool:
    return bool(PINCODE_REGEX.fullmatch(value or ""))


def is_valid_gst(value: str) -> bool:
    return bool(GST_REGEX.fullmatch(value or ""))


def normalize_mobile_in(value: str) -> str:
    cleaned = (value or "").lstrip("+").replace(" ", "").replace("-", "")
    if cleaned.startswith("91") and len(cleaned) == 12:
        return cleaned[2:]
    return cleaned


# ── Verhoeff checksum (used by Aadhaar) ───────────────────────────────
_D_TABLE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P_TABLE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff_validate(number: str) -> bool:
    c = 0
    for i, digit in enumerate(reversed(number)):
        c = _D_TABLE[c][_P_TABLE[i % 8][int(digit)]]
    return c == 0
