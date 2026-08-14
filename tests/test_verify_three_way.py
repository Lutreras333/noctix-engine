"""Executable spec for the three-way reconciliation (creed #10).

Every scenario is constructed, never sampled from real client data:
the point of each test is a books-versus-bank story a reader can hold
in their head, plus one Hypothesis property proving that internally
consistent pairs always balance and any single-entry perturbation is
always caught with evidence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from noctix_engine.domain import (
    BankStatement,
    BankTransaction,
    ClientLedger,
    ThreeWayBalanced,
    ThreeWayDiscrepancy,
    TrustBooks,
    TrustEntryKind,
    TrustLedgerEntry,
)
from noctix_engine.money import Money, total
from noctix_engine.verify.three_way import VerificationError, reconcile

PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)


# ── construction helpers: one statement/books pair per story ───────────


def _transaction(
    amount: str,
    *,
    posted: date = date(2026, 3, 10),
    line: int = 1,
    description: str = "wire",
) -> BankTransaction:
    return BankTransaction(
        posted=posted,
        description=description,
        amount=Money.parse(amount),
        source_line=line,
    )


def _entry(
    amount: str,
    *,
    client: str = "Alvarez",
    entered: date = date(2026, 3, 10),
    line: int = 1,
    description: str = "wire",
) -> TrustLedgerEntry:
    parsed = Money.parse(amount)
    kind = TrustEntryKind.DEPOSIT if parsed.amount > 0 else TrustEntryKind.DISBURSEMENT
    return TrustLedgerEntry(
        client=client,
        entered=entered,
        kind=kind,
        description=description,
        amount=parsed,
        source_line=line,
    )


def _statement(transactions: tuple[BankTransaction, ...]) -> BankStatement:
    return BankStatement(
        account_label="IOLTA ****1234",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        opening_balance=Money.zero(),
        closing_balance=total([transaction.amount for transaction in transactions]),
        transactions=transactions,
    )


def _books(
    ledgers: tuple[ClientLedger, ...],
    *,
    journal: Money | None = None,
    as_of: date = PERIOD_END,
) -> TrustBooks:
    computed = total([ledger.balance() for ledger in ledgers])
    return TrustBooks(
        as_of=as_of,
        journal_balance=journal if journal is not None else computed,
        ledgers=ledgers,
    )


# ── the good ending ────────────────────────────────────────────────────


def test_balanced_books_return_three_way_balanced() -> None:
    ledgers = (
        ClientLedger(
            client="Alvarez",
            entries=(
                _entry("1,000.00", line=1),
                _entry("-250.00", line=2, entered=date(2026, 3, 15)),
            ),
        ),
        ClientLedger(client="Brennan", entries=(_entry("500.00", client="Brennan"),)),
    )
    statement = _statement(
        (
            _transaction("1,000.00", line=1),
            _transaction("-250.00", line=2, posted=date(2026, 3, 15)),
            _transaction("500.00", line=3),
        )
    )
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayBalanced)
    assert result.bank_balance == Money.parse("1,250.00")
    assert result.journal_balance == Money.parse("1,250.00")
    assert result.ledgers_total == Money.parse("1,250.00")
    assert result.as_of == PERIOD_END


def test_balanced_result_ignores_negative_ledger() -> None:
    # Balanced iff the three totals agree to the cent — that is the
    # whole of the balance test. A negative ledger inside otherwise
    # agreeing totals is itemized only on the discrepancy path; this
    # test pins that boundary of the contract.
    ledgers = (
        ClientLedger(client="Alvarez", entries=(_entry("100.00"),)),
        ClientLedger(
            client="Brennan", entries=(_entry("-40.00", client="Brennan", line=2),)
        ),
    )
    statement = _statement(
        (_transaction("100.00", line=1), _transaction("-40.00", line=2))
    )
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayBalanced)


# ── discrepancies and their evidence ───────────────────────────────────


def test_bank_deposit_missing_from_books_is_flagged() -> None:
    ledgers = (ClientLedger(client="Alvarez", entries=(_entry("1,000.00"),)),)
    statement = _statement(
        (
            _transaction("1,000.00", line=1),
            _transaction("500.00", line=2, posted=date(2026, 3, 20)),
        )
    )
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert result.bank_versus_journal() == Money.parse("500.00")
    assert result.journal_versus_ledgers() == Money.zero()
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.amount == Money.parse("500.00")
    assert "no same-amount books entry" in finding.summary
    assert "2026-03-20" in finding.detail
    assert "candidate deposit" in finding.detail
    assert "statement line 2" in finding.detail


def test_unrecorded_bank_disbursement_is_flagged() -> None:
    ledgers = (ClientLedger(client="Alvarez", entries=(_entry("1,000.00"),)),)
    statement = _statement(
        (
            _transaction("1,000.00", line=1),
            _transaction("-300.00", line=2, posted=date(2026, 3, 22)),
        )
    )
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert result.bank_versus_journal() == Money.parse("-300.00")
    assert len(result.findings) == 1
    assert result.findings[0].amount == Money.parse("-300.00")
    assert "candidate disbursement not yet recorded" in result.findings[0].detail


def test_books_disbursement_absent_from_bank_is_flagged_as_outstanding() -> None:
    ledgers = (
        ClientLedger(
            client="Alvarez",
            entries=(
                _entry("1,000.00", line=1),
                _entry("-250.00", line=2, entered=date(2026, 3, 28)),
            ),
        ),
    )
    statement = _statement((_transaction("1,000.00", line=1),))
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert result.bank_versus_journal() == Money.parse("250.00")
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.amount == Money.parse("-250.00")
    assert "no same-amount bank transaction" in finding.summary
    assert "Alvarez" in finding.summary
    assert "2026-03-28" in finding.summary
    assert "candidate outstanding disbursement" in finding.detail
    assert "books line 2" in finding.detail


def test_transposed_digit_amounts_do_not_false_match() -> None:
    # $1,234.56 at the bank versus $1,243.56 in the books is the classic
    # transposition error. The engine refuses to pair them: fuzzy
    # matching would manufacture an explanation, so both sides surface
    # as separate findings and a human draws the conclusion.
    ledgers = (ClientLedger(client="Alvarez", entries=(_entry("1,243.56"),)),)
    statement = _statement((_transaction("1,234.56", line=1),))
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert len(result.findings) == 2
    amounts = {finding.amount for finding in result.findings}
    assert amounts == {Money.parse("1,234.56"), Money.parse("1,243.56")}


def test_negative_client_ledger_is_itemized_on_discrepancy() -> None:
    ledgers = (
        ClientLedger(client="Alvarez", entries=(_entry("100.00"),)),
        ClientLedger(
            client="Brennan",
            entries=(
                _entry("-50.00", client="Brennan", line=2, entered=date(2026, 3, 5)),
            ),
        ),
    )
    # The bank never saw Brennan's disbursement, so the totals disagree
    # and the discrepancy path itemizes the negative ledger too.
    statement = _statement((_transaction("100.00", line=1),))
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    negative_findings = [
        finding for finding in result.findings if "negative" in finding.summary
    ]
    assert len(negative_findings) == 1
    assert negative_findings[0].amount == Money.parse("-50.00")
    assert "Brennan" in negative_findings[0].summary
    assert "human review" in negative_findings[0].detail


def test_duplicate_amounts_match_as_a_multiset() -> None:
    # Two identical $100.00 deposits at the bank against one in the
    # books must flag exactly one — a single books entry can never
    # explain two bank lines.
    ledgers = (ClientLedger(client="Alvarez", entries=(_entry("100.00"),)),)
    statement = _statement(
        (_transaction("100.00", line=1), _transaction("100.00", line=2))
    )
    result = reconcile(statement, _books(ledgers))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert len(result.findings) == 1
    assert result.findings[0].amount == Money.parse("100.00")


def test_out_of_period_books_entry_does_not_match_a_bank_line() -> None:
    # A books entry dated before the period belongs to an earlier
    # statement's story: it may not explain this period's bank line,
    # and it may not raise a finding of its own against this statement.
    # The journal is deliberately a dollar off so the discrepancy path
    # runs and the matching behavior is observable.
    ledgers = (
        ClientLedger(
            client="Alvarez",
            entries=(_entry("400.00", entered=date(2026, 2, 10)),),
        ),
    )
    statement = _statement((_transaction("400.00", line=1),))
    result = reconcile(statement, _books(ledgers, journal=Money.parse("399.00")))
    assert isinstance(result, ThreeWayDiscrepancy)
    assert len(result.findings) == 1
    assert "no same-amount books entry" in result.findings[0].summary


# ── the halt: never compare across dates ───────────────────────────────


def test_date_mismatch_halts_with_client_readable_reason() -> None:
    ledgers = (ClientLedger(client="Alvarez", entries=(_entry("100.00"),)),)
    statement = _statement((_transaction("100.00"),))
    books = _books(ledgers, as_of=date(2026, 4, 30))
    with pytest.raises(VerificationError) as caught:
        reconcile(statement, books)
    message = str(caught.value)
    assert "2026-03-31" in message
    assert "2026-04-30" in message
    assert "same date" in message


# ── the property: consistency balances, perturbation is caught ─────────

_AMOUNT_CENTS = st.integers(min_value=-500_000, max_value=500_000).filter(
    lambda cents: cents != 0
)


@st.composite
def _consistent_pair(draw: st.DrawFn) -> tuple[BankStatement, TrustBooks]:
    """A random books/statement pair built to agree by construction.

    The statement is derived FROM the books — one bank line per ledger
    entry, closing balance equal to their sum — so the pair is
    internally consistent no matter what amounts Hypothesis draws.
    """
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 31)
    ledgers: list[ClientLedger] = []
    transactions: list[BankTransaction] = []
    line = 1
    for client_index in range(draw(st.integers(min_value=1, max_value=3))):
        entries: list[TrustLedgerEntry] = []
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            amount = Money.from_cents(draw(_AMOUNT_CENTS))
            entered = draw(st.dates(min_value=period_start, max_value=period_end))
            kind = (
                TrustEntryKind.DEPOSIT
                if amount.amount > 0
                else TrustEntryKind.DISBURSEMENT
            )
            entries.append(
                TrustLedgerEntry(
                    client=f"client-{client_index}",
                    entered=entered,
                    kind=kind,
                    description="generated",
                    amount=amount,
                    source_line=line,
                )
            )
            transactions.append(
                BankTransaction(
                    posted=entered,
                    description="generated",
                    amount=amount,
                    source_line=line,
                )
            )
            line += 1
        ledgers.append(
            ClientLedger(client=f"client-{client_index}", entries=tuple(entries))
        )
    closing = total([transaction.amount for transaction in transactions])
    books = TrustBooks(
        as_of=period_end, journal_balance=closing, ledgers=tuple(ledgers)
    )
    statement = BankStatement(
        account_label="IOLTA ****0000",
        period_start=period_start,
        period_end=period_end,
        opening_balance=Money.zero(),
        closing_balance=closing,
        transactions=tuple(transactions),
    )
    return statement, books


@given(pair=_consistent_pair())
def test_internally_consistent_pairs_always_balance(
    pair: tuple[BankStatement, TrustBooks],
) -> None:
    statement, books = pair
    result = reconcile(statement, books)
    assert isinstance(result, ThreeWayBalanced)


@given(data=st.data())
def test_any_single_entry_perturbation_is_caught_with_evidence(
    data: st.DataObject,
) -> None:
    statement, books = data.draw(_consistent_pair())
    perturbation = data.draw(
        st.sampled_from(
            (
                "extra_bank_transaction",
                "missing_bank_transaction",
                "missing_books_entry",
            )
        )
    )
    if perturbation == "extra_bank_transaction":
        extra = BankTransaction(
            posted=statement.period_end,
            description="perturbation",
            amount=Money.from_cents(data.draw(_AMOUNT_CENTS)),
            source_line=999,
        )
        statement = replace(
            statement,
            transactions=(*statement.transactions, extra),
            closing_balance=statement.closing_balance + extra.amount,
        )
    elif perturbation == "missing_bank_transaction":
        index = data.draw(
            st.integers(min_value=0, max_value=len(statement.transactions) - 1)
        )
        removed = statement.transactions[index]
        statement = replace(
            statement,
            transactions=(
                *statement.transactions[:index],
                *statement.transactions[index + 1 :],
            ),
            closing_balance=statement.closing_balance - removed.amount,
        )
    else:
        ledger_index = data.draw(
            st.integers(min_value=0, max_value=len(books.ledgers) - 1)
        )
        ledger = books.ledgers[ledger_index]
        entry_index = data.draw(
            st.integers(min_value=0, max_value=len(ledger.entries) - 1)
        )
        thinned = replace(
            ledger,
            entries=(
                *ledger.entries[:entry_index],
                *ledger.entries[entry_index + 1 :],
            ),
        )
        # journal_balance is deliberately left at the pre-perturbation
        # total: the books now disagree with themselves, exactly the
        # condition the three-way comparison exists to catch.
        books = replace(
            books,
            ledgers=(
                *books.ledgers[:ledger_index],
                thinned,
                *books.ledgers[ledger_index + 1 :],
            ),
        )
    result = reconcile(statement, books)
    assert isinstance(result, ThreeWayDiscrepancy)
    assert len(result.findings) >= 1
