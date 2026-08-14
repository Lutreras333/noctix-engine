"""Money: the only representation of a currency amount in this engine.

Doctrine (creed #4): money is a Decimal inside a frozen type, born only at
a parsing boundary; float never touches a ledger path. This module is the
whole of that rule — everything else imports Money and stays out of the
arithmetic business.

Design constraints the reader should know:
- Amounts are always quantized to the cent. A Money that isn't is
  unrepresentable: the constructor refuses it.
- Construction from float is refused at runtime as well as untyped
  callers' peril: bank documents and exports provide STRINGS, and strings
  are the only honest input.
- Only same-currency arithmetic exists. There is no multiplication by
  arbitrary numbers on this type: rate math (hours × rate, fee splits)
  must go through `allocate` or a named policy function that states its
  rounding, because silent per-line rounding is how ledgers drift.
- v1 speaks USD only. The currency field exists so a second currency is a
  parser change, not a type change.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

CENT: Final = Decimal("0.01")
_SUPPORTED_CURRENCIES: Final = frozenset({"USD"})


class MoneyError(ValueError):
    """A currency amount failed to parse or violated a Money invariant.

    Raised instead of guessing (creed #6). The message is written to be
    readable in a run record by a non-programmer.
    """


def _require_decimal(value: object) -> Decimal:
    """Runtime seal for the type annotations dataclasses don't enforce.

    Typed callers can never fail this; untyped callers passing a float
    are exactly who it exists for.
    """
    if not isinstance(value, Decimal):
        raise MoneyError(
            f"Money.amount must be a Decimal, got {type(value).__name__} "
            "— floats are banned from money paths"
        )
    return value


@dataclass(frozen=True, slots=True)
class Money:
    """An exact currency amount, always quantized to the cent.

    Construct via `Money.parse` (documents), `Money.from_cents`
    (arithmetic-safe integers), or `Money.zero`. The bare constructor is
    for the boundary parsers in this module's tests; application code
    should never build one from a raw Decimal it did not quantize.
    """

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        _require_decimal(self.amount)
        if self.currency not in _SUPPORTED_CURRENCIES:
            raise MoneyError(
                f"unsupported currency {self.currency!r}; engine v1 speaks USD"
            )
        if self.amount != self.amount.quantize(CENT):
            raise MoneyError(
                f"amount {self.amount} is not quantized to the cent; "
                "quantize at the parsing boundary with an explicit policy"
            )

    # ── boundary constructors ──────────────────────────────────────────

    @classmethod
    def parse(cls, raw: str, *, currency: str = "USD") -> Money:
        """Parse a document amount: '1,234.56', '$-45.00', '(45.00)', '1200'.

        Accounting parentheses mean negative. Anything ambiguous raises
        MoneyError with the offending text preserved — the run record
        needs to show a human exactly what refused to parse.
        """
        text = raw.strip()
        if not text:
            raise MoneyError("empty amount where a currency value was required")

        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative = True
            text = text[1:-1].strip()
        text = text.replace("$", "").replace(",", "").strip()
        if text.startswith("-"):
            negative = not negative
            text = text[1:].strip()

        try:
            magnitude = Decimal(text)
        except InvalidOperation as error:
            raise MoneyError(f"unparseable amount {raw!r}") from error

        if magnitude != magnitude.quantize(CENT):
            # A bank line with sub-cent precision is a document problem the
            # human must see, not a rounding decision the machine may make.
            raise MoneyError(
                f"amount {raw!r} has sub-cent precision; refusing to round"
            )

        signed = -magnitude if negative else magnitude
        return cls(signed.quantize(CENT), currency)

    @classmethod
    def from_cents(cls, cents: int, *, currency: str = "USD") -> Money:
        """Exact construction from an integer number of cents."""
        return cls((Decimal(cents) * CENT).quantize(CENT), currency)

    @classmethod
    def zero(cls, *, currency: str = "USD") -> Money:
        return cls(Decimal("0.00"), currency)

    # ── arithmetic (same-currency, closed) ─────────────────────────────

    def _require_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise MoneyError(
                f"cannot {operation} {self.currency} and {other.currency}; "
                "convert explicitly at a boundary first"
            )

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other, "add")
        return Money((self.amount + other.amount).quantize(CENT), self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other, "subtract")
        return Money((self.amount - other.amount).quantize(CENT), self.currency)

    def __neg__(self) -> Money:
        return Money((-self.amount).quantize(CENT), self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == Decimal("0.00")

    # ── allocation ─────────────────────────────────────────────────────

    def allocate(self, weights: tuple[int, ...]) -> tuple[Money, ...]:
        """Split this amount by integer weights without creating or
        destroying a cent (largest-remainder method).

        The conservation invariant — sum(parts) == self — is the
        Hypothesis-tested contract of this engine's money math.
        """
        if not weights:
            raise MoneyError("cannot allocate over zero weights")
        if any(weight < 0 for weight in weights):
            raise MoneyError("allocation weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise MoneyError("allocation weights must not all be zero")

        # scaleb(2) shifts the decimal point two places — exact for a
        # cent-quantized Decimal, and division-free by doctrine.
        total_cents = int(self.amount.scaleb(2))
        base_shares = [total_cents * weight // total_weight for weight in weights]
        remainder = total_cents - sum(base_shares)
        # Distribute leftover cents to the largest fractional remainders,
        # index order breaking ties so the result is deterministic.
        fractional = sorted(
            range(len(weights)),
            key=lambda index: (
                -(total_cents * weights[index] % total_weight),
                index,
            ),
        )
        step = 1 if remainder >= 0 else -1
        for index in fractional[: abs(remainder)]:
            base_shares[index] += step

        parts = tuple(
            Money.from_cents(share, currency=self.currency) for share in base_shares
        )
        assert sum((part.amount for part in parts), Decimal("0.00")) == self.amount, (
            "allocation conservation violated — this is a bug in Money.allocate"
        )
        return parts

    # ── presentation ───────────────────────────────────────────────────

    def __str__(self) -> str:
        sign = "-" if self.amount < 0 else ""
        return f"{sign}${abs(self.amount):,.2f}"


def total(amounts: tuple[Money, ...] | list[Money], *, currency: str = "USD") -> Money:
    """Sum a sequence of Money, correctly returning zero for an empty one."""
    result = Money.zero(currency=currency)
    for amount in amounts:
        result = result + amount
    return result
