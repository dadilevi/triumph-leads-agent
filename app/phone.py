"""Israeli phone normalization and wa.me links."""
from __future__ import annotations

import re
from urllib.parse import quote


def normalize_il_phone(raw: object) -> str | None:
    """Return phone in international digits-only form (e.g. '972501234567'), or None.

    Accepts 050-123-4567, 0501234567, +972 50 123 4567, 972501234567,
    and numbers Excel turned into ints/floats (501234567.0 — leading 0 lost).
    """
    if raw is None:
        return None
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        raw = str(int(raw))
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("972"):
        rest = digits[3:]
    elif digits.startswith("0"):
        rest = digits[1:]
    else:
        rest = digits  # Excel dropped the leading 0
    rest = rest.lstrip("0")
    # Israeli mobile: 9 digits starting with 5 (50, 52, 53, 54, 55, 58 ...)
    # Landlines are 8 digits — kept, but WhatsApp usually means mobile.
    if len(rest) == 9 and rest.startswith("5"):
        return "972" + rest
    if len(rest) == 8:
        return "972" + rest
    return None


def display_phone(intl: str | None) -> str:
    """'972501234567' -> '050-123-4567'."""
    if not intl:
        return ""
    local = "0" + intl[3:] if intl.startswith("972") else intl
    if len(local) == 10:
        return f"{local[:3]}-{local[3:6]}-{local[6:]}"
    return local


def wa_link(intl_phone: str, text: str) -> str:
    """Click-to-chat link. Opens WhatsApp with the message pre-filled; human presses send."""
    return f"https://wa.me/{intl_phone}?text={quote(text, safe='')}"
