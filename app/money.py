"""Money handling. Everything is an int count of minor units (cents, paise, yen).

No float touches an amount: rates are parsed from JSON as Decimal, conversions
use Decimal arithmetic, and splits use integer arithmetic only.
"""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

# ISO 4217 minor-unit exponents that differ from the default of 2.
EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "KWD": 3, "BHD": 3}

MAX_AMOUNT_MINOR = 10**12


def exponent(currency: str) -> int:
    return EXPONENTS.get(currency, 2)


def format_minor(minor: int, currency: str) -> str:
    """12345 + USD -> '123.45'. Integer maths only."""
    exp = exponent(currency)
    sign = "-" if minor < 0 else ""
    q, r = divmod(abs(minor), 10**exp)
    return f"{sign}{q}" if exp == 0 else f"{sign}{q}.{r:0{exp}d}"


class Rates:
    """Static rates file: units of each currency per 1 unit of `base`."""

    def __init__(self, base: str, rates: dict[str, Decimal], as_of: str = ""):
        self.base = base
        self.rates = rates
        self.as_of = as_of

    @classmethod
    def load(cls, path: str | Path) -> "Rates":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f, parse_float=Decimal, parse_int=Decimal)
        return cls(raw["base"], {k: Decimal(v) for k, v in raw["rates"].items()}, raw.get("as_of", ""))

    def currencies(self) -> list[str]:
        return sorted(self.rates)

    def supports(self, currency: str) -> bool:
        return currency in self.rates

    def convert(self, amount_minor: int, src: str, dst: str) -> tuple[int, str]:
        """Convert minor units src -> dst. Returns (minor units, rate used as text).

        Rounded half up to a whole minor unit. Callers store the result, so a
        later change to the rates file never rewrites history.
        """
        if src == dst:
            return amount_minor, "1"
        with localcontext() as ctx:
            ctx.prec = 50
            rate = self.rates[dst] / self.rates[src]
            scaled = Decimal(amount_minor) * Decimal(10) ** (exponent(dst) - exponent(src)) * rate
            out = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
            return out, f"{rate:.10f}"


def allocate(total: int, weights: list[int], rotation: int = 0) -> list[int]:
    """Split `total` minor units proportionally to integer `weights`.

    Largest-remainder method: everyone gets floor(total * w / W), then the
    leftover units (fewer than len(weights)) go to the biggest fractional
    remainders. Ties break by position rotated by `rotation`, so the "extra
    cent" doesn't always land on the same person across expenses, and the
    result is a pure function of its inputs (no randomness, no dict order).
    The parts always sum to exactly `total`.
    """
    n = len(weights)
    if n == 0 or total < 0 or any(w < 0 for w in weights):
        raise ValueError("bad allocation input")
    w_sum = sum(weights)
    if w_sum <= 0:
        raise ValueError("weights must sum to a positive number")
    parts = [total * w // w_sum for w in weights]
    remainders = [total * w % w_sum for w in weights]
    leftover = total - sum(parts)
    order = sorted(range(n), key=lambda i: (-remainders[i], (i - rotation) % n))
    for i in order[:leftover]:
        parts[i] += 1
    return parts
