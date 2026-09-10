"""Shared validators: handler and FSM wizard input, and money computed in code."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TypeVar

T = TypeVar("T")

# Product.price is Numeric(10, 2)
MAX_PRICE = Decimal("99999999.99")
MIN_PRICE = Decimal("0.01")
MONEY_QUANTUM = Decimal("0.01")

PHONE_MIN_DIGITS = 8
PHONE_MAX_DIGITS = 15
PHONE_MAX_STORED_LEN = 64

_PRICE_RE = re.compile(r"^\d{1,8}([.,]\d{1,2})?$")
_PHONE_DIGITS_RE = re.compile(r"^\+?\d+$")


def nonempty(
    text: str | None,
    *,
    min_len: int = 1,
    max_len: int | None = None,
) -> str | None:
    """Return stripped text when it meets length bounds; otherwise None."""
    if text is None:
        return None
    value = text.strip()
    if len(value) < min_len:
        return None
    if max_len is not None and len(value) > max_len:
        return None
    return value


def parse_price(raw: str | None) -> Decimal | None:
    """
    Parse a shop price.

    Rules:
    - digits with optional ``.`` / ``,`` and up to 2 fractional digits
    - no scientific notation
    - ``0.01`` … ``99999999.99``
    """
    if raw is None:
        return None
    cleaned = raw.strip().replace(" ", "")
    if not cleaned or not _PRICE_RE.fullmatch(cleaned):
        return None
    normalized = cleaned.replace(",", ".")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    if value < MIN_PRICE or value > MAX_PRICE:
        return None
    return value.quantize(Decimal("0.01"))


def to_money(value: Decimal | int | str) -> Decimal:
    """
    A price that fits ``Numeric(10, 2)``, rounded to cents.

    For amounts that come from code or configuration; text a person typed goes
    through :func:`parse_price`. Floats are refused rather than converted —
    ``Decimal(19.99)`` is ``19.98999…`` — because money is Decimal end to end.
    """
    if isinstance(value, float | bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"Money must be a Decimal, not {type(value).__name__}")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a number") from exc
    if not amount.is_finite() or not MIN_PRICE <= amount <= MAX_PRICE:
        raise ValueError(f"{value!r} is not a price between {MIN_PRICE} and {MAX_PRICE}")
    return amount.quantize(MONEY_QUANTUM)


def normalize_phone(raw: str | None) -> str | None:
    """
    Normalize a phone number to ``+`` + digits or digits-only international form.

    Accepts spaces, dashes, parentheses; requires 8–15 digits; max stored length 64.
    """
    if raw is None:
        return None
    compact = (
        raw.strip()
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
    )
    if not compact or not _PHONE_DIGITS_RE.fullmatch(compact):
        return None

    digits = compact[1:] if compact.startswith("+") else compact
    if not digits.isdigit():
        return None
    if not (PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS):
        return None

    normalized = f"+{digits}" if compact.startswith("+") else digits
    if len(normalized) > PHONE_MAX_STORED_LEN:
        return None
    return normalized


# Every primary key in the schema is a PostgreSQL ``integer``, so nothing larger
# can name a real row. Values above this are rejected here rather than sent to
# the database, where an out-of-range bind raises: on PostgreSQL a DataError,
# on SQLite an OverflowError. Either lands in the "unexpected" bucket and logs a
# full traceback — which any customer could then trigger at will, since Telegram
# allows 64 bytes of callback data and the parsers accept every digit of it.
MAX_DB_INT = 2_147_483_647


def parse_positive_int(raw: str | None) -> int | None:
    """Parse a positive integer (``0 < value <= MAX_DB_INT``)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit() or len(text) > 10:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 0 < value <= MAX_DB_INT else None


def parse_nonnegative_int(raw: str | None) -> int | None:
    """Parse an integer ``0 <= value <= MAX_DB_INT`` (e.g. a pagination page)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit() or len(text) > 10:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 0 <= value <= MAX_DB_INT else None


def parse_callback_id(data: str | None, prefix: str) -> int | None:
    """Extract a positive int id from ``prefix{id}`` callback data."""
    if data is None or not data.startswith(prefix):
        return None
    return parse_positive_int(data.removeprefix(prefix))


def parse_callback_page(data: str | None, prefix: str) -> int | None:
    """Extract a non-negative page index from ``prefix{page}`` callback data."""
    if data is None or not data.startswith(prefix):
        return None
    return parse_nonnegative_int(data.removeprefix(prefix))


def allowlist(value: str | None, allowed: set[str] | frozenset[str]) -> str | None:
    if value is None:
        return None
    return value if value in allowed else None
