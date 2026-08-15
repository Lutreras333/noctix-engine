"""Executable spec for the domain invariants.

The one that matters most: a false BALANCED verdict is unrepresentable
(creed #5). Everything downstream — the report, the record, the firm's
principal — leans on that being a property of the type, not a promise
of the caller.
"""

from __future__ import annotations

from datetime import date

import pytest

from noctix_engine.domain import ThreeWayBalanced
from noctix_engine.money import Money

AS_OF = date(2026, 7, 31)


def test_balanced_constructs_when_all_three_agree() -> None:
    amount = Money.parse("87421.50")
    result = ThreeWayBalanced(
        as_of=AS_OF,
        bank_balance=amount,
        journal_balance=amount,
        ledgers_total=amount,
    )
    assert result.bank_balance == result.ledgers_total


def test_balanced_with_unequal_balances_is_unrepresentable() -> None:
    # No honesty gate has to catch what cannot exist.
    with pytest.raises(ValueError, match="all three balances equal"):
        ThreeWayBalanced(
            as_of=AS_OF,
            bank_balance=Money.parse("100.00"),
            journal_balance=Money.parse("200.00"),
            ledgers_total=Money.parse("100.00"),
        )
