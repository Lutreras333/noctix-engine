"""The executable spec for Money (creed #10): the money math is the heart,
so it gets the SQLite treatment — every documented behavior pinned by an
example, every refusal pinned by an error test, and Hypothesis sweeping
the space no example table can cover.

The properties here ARE the engine's central promises: parsing round-trips
its own formatting, addition is exact and associative, and allocation
never creates or destroys a cent.
"""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from functools import reduce
from operator import add
from typing import Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import noctix_engine.money
from noctix_engine.money import Money, MoneyError, total

# Hypothesis properties run with the per-example deadline disabled: the
# deadline measures wall-clock, and wall-clock flakes on shared machines.
# Creed #10 tolerates zero flakes, so time never decides a verdict here.
NO_DEADLINE = settings(deadline=None)

# Cent amounts spanning ±$10,000,000.00 — wider than any ledger this
# engine will meet, negatives included, per the spec for these properties.
cent_amounts = st.integers(min_value=-(10**9), max_value=10**9)
monies = st.builds(Money.from_cents, cent_amounts)


@st.composite
def weight_tuples(draw: st.DrawFn) -> tuple[int, ...]:
    """Non-degenerate allocation weights: zeros are allowed, but at least
    one entry is strictly positive — exactly the domain `allocate`
    accepts. Single-element tuples occur, so the degenerate-looking
    "split one way" case is swept too."""
    weights = draw(st.lists(st.integers(min_value=0, max_value=9_999), max_size=7))
    guaranteed_positive = draw(st.integers(min_value=1, max_value=9_999))
    position = draw(st.integers(min_value=0, max_value=len(weights)))
    weights.insert(position, guaranteed_positive)
    return tuple(weights)


# ── the parsing boundary ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "formatted"),
    [
        ("1,234.56", "$1,234.56"),
        ("$1,234.56", "$1,234.56"),
        ("1200", "$1,200.00"),
        ("$-45.00", "-$45.00"),
        ("-$45.00", "-$45.00"),
        ("(45.00)", "-$45.00"),
        ("($1,234.56)", "-$1,234.56"),
        ("  17.25  ", "$17.25"),
        ("0.00", "$0.00"),
    ],
)
def test_parse_understands_document_amounts(raw: str, formatted: str) -> None:
    """Every notation a bank export uses lands on the one display form."""
    assert str(Money.parse(raw)) == formatted


@pytest.mark.parametrize("raw", ["", "   "])
def test_parse_refuses_empty(raw: str) -> None:
    with pytest.raises(MoneyError, match="empty amount"):
        Money.parse(raw)


@pytest.mark.parametrize("raw", ["abc", "$", "()", "12.3.4", "1 200", "USD 5.00"])
def test_parse_refuses_garbage(raw: str) -> None:
    # The offending text is preserved in the message: the run record must
    # show a human exactly what refused to parse (creed #6).
    with pytest.raises(MoneyError, match="unparseable"):
        Money.parse(raw)


@pytest.mark.parametrize("raw", ["−5.00", "$−45.00"])
def test_parse_rejects_unicode_minus(raw: str) -> None:
    # The ASCII hyphen and accounting parentheses are the only negative
    # markers this engine trusts. A lookalike minus (U+2212) is a document
    # oddity a human must see, not something to silently normalize.
    with pytest.raises(MoneyError, match="unparseable"):
        Money.parse(raw)


@pytest.mark.parametrize("raw", ["--5.00", "(-5.00)", "-$-45.00", "(--5.00)"])
def test_parse_refuses_double_negation(raw: str) -> None:
    # '--5.00' and '(-5.00)' have no honest reading; guessing a sign on a
    # trust ledger is exactly what creed #6 forbids.
    with pytest.raises(MoneyError, match="negated more than once"):
        Money.parse(raw)


@pytest.mark.parametrize("raw", ["12,34.56", "1,23", "1,2345.00", ",100", "1,,000"])
def test_parse_refuses_malformed_comma_grouping(raw: str) -> None:
    # Commas are digit grouping, not decoration: '12,34.56' is a typo a
    # human must see, never $1,234.56.
    with pytest.raises(MoneyError, match="unparseable"):
        Money.parse(raw)


@pytest.mark.parametrize("raw", ["1.234", "$0.005", "(0.001)", "-2.5001"])
def test_parse_refuses_sub_cent_precision(raw: str) -> None:
    # Sub-cent precision is a document problem for the human, never a
    # rounding decision the machine may make.
    with pytest.raises(MoneyError, match="sub-cent"):
        Money.parse(raw)


def test_parse_refuses_unsupported_currency() -> None:
    with pytest.raises(MoneyError, match="unsupported currency"):
        Money.parse("1.00", currency="EUR")


# ── the constructor's seals ────────────────────────────────────────────


def test_float_seal_fires_at_runtime() -> None:
    # The type checker already bans this; the runtime seal exists for the
    # untyped caller who smuggles a float past the annotations. cast()
    # plays that caller here.
    with pytest.raises(MoneyError, match="float"):
        Money(amount=cast(Any, 1.23))


def test_constructor_refuses_unquantized_decimal() -> None:
    with pytest.raises(MoneyError, match="not quantized"):
        Money(Decimal("1.234"))


def test_constructor_refuses_unsupported_currency() -> None:
    with pytest.raises(MoneyError, match="unsupported currency"):
        Money(Decimal("1.00"), "GBP")


# ── boundary constructors: from_cents and zero ─────────────────────────


def test_from_cents_is_exact() -> None:
    assert Money.from_cents(123456) == Money.parse("1,234.56")
    assert str(Money.from_cents(-5)) == "-$0.05"
    assert str(Money.from_cents(10**9)) == "$10,000,000.00"
    assert Money.from_cents(0) == Money.zero()


def test_zero() -> None:
    assert Money.zero().is_zero
    assert str(Money.zero()) == "$0.00"
    assert not Money.from_cents(1).is_zero
    assert not Money.from_cents(-1).is_zero


# ── arithmetic: exact, closed, same-currency ───────────────────────────


def test_addition_is_exact_where_float_famously_is_not() -> None:
    # 0.1 + 0.2 != 0.3 is THE float bug. Decimal-in-a-frozen-type exists
    # so this assertion holds to the cent, every time (creed #4).
    assert Money.parse("0.10") + Money.parse("0.20") == Money.parse("0.30")


def test_subtraction_and_negation_are_exact() -> None:
    assert Money.from_cents(500) - Money.from_cents(725) == Money.from_cents(-225)
    assert -Money.from_cents(41) == Money.from_cents(-41)
    assert -Money.zero() == Money.zero()


def test_currency_mismatch_refused_on_every_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # v1 speaks USD only, so a second currency cannot be built through the
    # front door yet. Widening the allowlist for this test exercises the
    # real guard on the real constructor path — no back-door objects.
    monkeypatch.setattr(
        noctix_engine.money, "_SUPPORTED_CURRENCIES", frozenset({"USD", "EUR"})
    )
    dollars = Money.parse("10.00")
    euros = Money.parse("10.00", currency="EUR")
    with pytest.raises(MoneyError, match="cannot add"):
        _ = dollars + euros
    with pytest.raises(MoneyError, match="cannot subtract"):
        _ = dollars - euros


# ── value semantics: frozen, hashable ──────────────────────────────────


def test_money_is_a_hashable_value() -> None:
    parsed = Money.parse("12.34")
    constructed = Money.from_cents(1234)
    assert parsed == constructed
    assert hash(parsed) == hash(constructed)
    # Usable as a dict key or set member — reconciliation matches on it.
    assert len({parsed, constructed}) == 1


def test_money_is_frozen() -> None:
    amount = Money.parse("12.34")
    with pytest.raises(FrozenInstanceError):
        amount.amount = Decimal("99.99")  # pyright: ignore


# ── allocation: examples pin the policy, properties pin the invariant ──


def test_allocate_largest_remainder_with_index_tie_break() -> None:
    # $1.00 three ways: the leftover cent goes to the earliest of the
    # tied largest remainders — deterministic by construction, not luck.
    assert Money.from_cents(100).allocate((1, 1, 1)) == (
        Money.from_cents(34),
        Money.from_cents(33),
        Money.from_cents(33),
    )


def test_allocate_negative_amount() -> None:
    # A negative split conserves too: -$1.01 two ways loses no cent.
    assert Money.from_cents(-101).allocate((1, 1)) == (
        Money.from_cents(-50),
        Money.from_cents(-51),
    )


def test_allocate_single_weight_returns_whole() -> None:
    assert Money.from_cents(777).allocate((3,)) == (Money.from_cents(777),)


def test_allocate_zero_weight_gets_nothing() -> None:
    assert Money.from_cents(10).allocate((0, 1)) == (
        Money.zero(),
        Money.from_cents(10),
    )


def test_allocate_refuses_degenerate_weights() -> None:
    with pytest.raises(MoneyError, match="zero weights"):
        Money.from_cents(100).allocate(())
    with pytest.raises(MoneyError, match="non-negative"):
        Money.from_cents(100).allocate((3, -1))
    with pytest.raises(MoneyError, match="all be zero"):
        Money.from_cents(100).allocate((0, 0))


# ── total ──────────────────────────────────────────────────────────────


def test_total_of_empty_sequence_is_zero() -> None:
    assert total([]) == Money.zero()
    assert total(()) == Money.zero()


# ── Hypothesis: the engine's promises, swept ───────────────────────────


@NO_DEADLINE
@given(money=monies, weights=weight_tuples())
def test_allocation_conserves_every_cent(
    money: Money, weights: tuple[int, ...]
) -> None:
    """The conservation invariant: sum(parts) == self, for any amount
    (negatives included) and any non-degenerate weights (singles
    included). This is the contract `allocate` names in its docstring."""
    parts = money.allocate(weights)
    assert len(parts) == len(weights)
    assert total(parts) == money


@NO_DEADLINE
@given(money=monies, weights=weight_tuples())
def test_allocation_is_deterministic(money: Money, weights: tuple[int, ...]) -> None:
    # Same inputs, same split — a reperformed run must reproduce the
    # ledger exactly (creed #9).
    assert money.allocate(weights) == money.allocate(weights)


@NO_DEADLINE
@given(cents=cent_amounts)
def test_parse_format_round_trip(cents: int) -> None:
    # What the engine prints, the engine re-reads to the identical value:
    # a report figure can always be traced back to the exact amount.
    money = Money.from_cents(cents)
    assert Money.parse(str(money)) == money


@NO_DEADLINE
@given(amounts=st.lists(monies, max_size=10))
def test_total_matches_reduce(amounts: list[Money]) -> None:
    assert total(amounts) == reduce(add, amounts, Money.zero())


@NO_DEADLINE
@given(first=monies, second=monies, third=monies)
def test_addition_is_associative(first: Money, second: Money, third: Money) -> None:
    # Associativity is why the ORDER a statement's lines are summed in
    # can never change a closing balance.
    assert (first + second) + third == first + (second + third)


@NO_DEADLINE
@given(first=cent_amounts, second=cent_amounts)
def test_arithmetic_matches_integer_cents(first: int, second: int) -> None:
    # Money arithmetic is integer-cent arithmetic wearing a type: exact
    # agreement with int, the one arithmetic nobody doubts.
    assert Money.from_cents(first) + Money.from_cents(second) == Money.from_cents(
        first + second
    )
    assert Money.from_cents(first) - Money.from_cents(second) == Money.from_cents(
        first - second
    )
    assert -Money.from_cents(first) == Money.from_cents(-first)
