"""Amount messages → integer grosz (0.01 PLN), clamping, and the ``000000,00`` text.

Accepted messages (WebSocket text/bytes or an HTTP POST body):

* JSON object with the amount at ``json_path`` (default ``amount``): ``{"amount": 1234.56}``,
  also as a string ``{"amount": "1234.56"}``; nested paths use dots (``data.total``);
* a bare number: ``1234.56``, ``1234,56`` (Polish decimal comma), ``"1234.56"`` (JSON string).

Everything else (bad JSON, missing key, not a number, NaN/∞, negative) raises :class:`InvalidAmount`;
the caller logs it and keeps the current display. Amounts above the maximum are clamped.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DIGITS = 8
DECIMALS = 2
MAX_CENTS = 10 ** DIGITS - 1     # 999999.99


class InvalidAmount(ValueError):
    """The message does not carry a usable amount."""


@dataclass(frozen=True)
class Amount:
    cents: int          # what will be shown (after clamping)
    requested: str      # the amount as received (for the log)
    clamped: bool


def _to_decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InvalidAmount(f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidAmount(f"not a finite number: {value!r}")
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace(" ", "")
        if text.count(",") == 1 and "." not in text:
            text = text.replace(",", ".")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise InvalidAmount(f"not a number: {value!r}") from None
        if not d.is_finite():
            raise InvalidAmount(f"not a finite number: {value!r}")
        return d
    raise InvalidAmount(f"not a number: {value!r}")


def _extract(doc, json_path: str):
    cur = doc
    for key in json_path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise InvalidAmount(f"no {json_path!r} in the message")
        cur = cur[key]
    return cur


def parse_message(message: str | bytes, json_path: str = "amount", max_cents: int = MAX_CENTS) -> Amount:
    """Parse one message; raises :class:`InvalidAmount` (see the module docstring)."""
    if isinstance(message, (bytes, bytearray)):
        try:
            message = bytes(message).decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidAmount("message is not UTF-8 text") from None
    text = message.strip()
    if not text:
        raise InvalidAmount("empty message")
    try:
        doc = json.loads(text, parse_constant=lambda c: float(c))   # NaN/Infinity → rejected below
    except (json.JSONDecodeError, ValueError):
        doc = text                                                   # maybe a bare "1234,56"
    value = _extract(doc, json_path) if isinstance(doc, dict) else doc
    if isinstance(value, (list, dict)):
        raise InvalidAmount(f"not a number: {value!r}")
    return from_value(value, max_cents)


def from_value(value, max_cents: int = MAX_CENTS) -> Amount:
    """A number (or numeric string) in PLN → :class:`Amount` (rounded to grosz, clamped)."""
    d = _to_decimal(value)
    if d < 0:
        raise InvalidAmount(f"negative amount: {value!r}")
    cents = int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    requested = f"{d:f}"
    if cents > max_cents:
        return Amount(max_cents, requested, True)
    return Amount(cents, requested, False)


def max_cents_for(max_amount: float) -> int:
    return min(MAX_CENTS, int((Decimal(str(max_amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def digits_of(cents: int) -> list[int]:
    """The 8 module digits, left (100 000 PLN) to right (0.01 PLN)."""
    if not 0 <= cents <= MAX_CENTS:
        raise ValueError(f"cents out of range: {cents}")
    return [int(c) for c in f"{cents:0{DIGITS}d}"]


def format_digits(digits: list[int]) -> str:
    """``[0,0,1,2,3,4,5,6]`` → ``001234,56`` (the display text)."""
    s = "".join(str(d) for d in digits)
    return f"{s[:-DECIMALS]},{s[-DECIMALS:]}"


def format_cents(cents: int) -> str:
    return format_digits(digits_of(cents))
