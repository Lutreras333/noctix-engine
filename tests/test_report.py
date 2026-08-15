"""Executable spec for the three-way report renderer (creed #10).

Every scenario is constructed in code with fixed dates and amounts so
the golden snapshots in tests/golden/ are stable: a diff in a golden is
a deliberate wording change or a bug, never noise. Results are computed
through the real verify stage — the renderer is specified against what
the engine actually produces, not against hand-built results.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.amber import AmberSnapshotExtension

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
from noctix_engine.money import Money
from noctix_engine.report.three_way_report import ReportError, render_three_way_report
from noctix_engine.verify.leakage import LeakageFinding, find_stale_client_funds
from noctix_engine.verify.three_way import reconcile

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)


class GoldenExtension(AmberSnapshotExtension):
    """Snapshots live in tests/golden/, the goldens directory this repo
    names, instead of syrupy's default __snapshots__."""

    snapshot_dirname = "golden"


@pytest.fixture
def golden(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(GoldenExtension)


# ── construction helpers: one statement/books story per test ───────────


def _transaction(
    amount: str, *, posted: date, line: int, description: str
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
    client: str,
    entered: date,
    kind: TrustEntryKind,
    line: int,
    description: str,
) -> TrustLedgerEntry:
    return TrustLedgerEntry(
        client=client,
        entered=entered,
        kind=kind,
        description=description,
        amount=Money.parse(amount),
        source_line=line,
    )


def _balanced_pair() -> tuple[BankStatement, TrustBooks]:
    """Three clients whose books agree with the bank to the cent —
    the same story the slice fixtures tell, pinned here in code."""
    statement = BankStatement(
        account_label="IOLTA ****1234",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        opening_balance=Money.parse("80,000.00"),
        closing_balance=Money.parse("87,421.50"),
        transactions=(
            _transaction(
                "25,000.00",
                posted=date(2026, 7, 2),
                line=6,
                description="INCOMING WIRE - CHEN ESTATE SALE PROCEEDS",
            ),
            _transaction(
                "-12,000.00",
                posted=date(2026, 7, 10),
                line=7,
                description="CHECK 2101 - NORTHSIDE IMAGING LIEN PAYOFF",
            ),
            _transaction(
                "-2,578.50",
                posted=date(2026, 7, 15),
                line=8,
                description="TRANSFER TO OPERATING - INVOICE 2026-041",
            ),
            _transaction(
                "-3,000.00",
                posted=date(2026, 7, 20),
                line=9,
                description="CHECK 2102 - CLERK OF COURT AND APPRAISER",
            ),
        ),
    )
    books = TrustBooks(
        as_of=PERIOD_END,
        journal_balance=Money.parse("87,421.50"),
        ledgers=(
            ClientLedger(
                client="Alvarez, Maria",
                entries=(
                    _entry(
                        "50,000.00",
                        client="Alvarez, Maria",
                        entered=date(2026, 6, 5),
                        kind=TrustEntryKind.DEPOSIT,
                        line=5,
                        description="Settlement funds received",
                    ),
                    _entry(
                        "-12,000.00",
                        client="Alvarez, Maria",
                        entered=date(2026, 7, 10),
                        kind=TrustEntryKind.DISBURSEMENT,
                        line=8,
                        description="Medical lien payoff",
                    ),
                ),
            ),
            ClientLedger(
                client="Brennan Holdings LLC",
                entries=(
                    _entry(
                        "30,000.00",
                        client="Brennan Holdings LLC",
                        entered=date(2026, 6, 12),
                        kind=TrustEntryKind.DEPOSIT,
                        line=6,
                        description="Retainer deposit",
                    ),
                    _entry(
                        "-2,578.50",
                        client="Brennan Holdings LLC",
                        entered=date(2026, 7, 15),
                        kind=TrustEntryKind.FEE_TRANSFER,
                        line=9,
                        description="Invoice 2026-041 earned fees to operating",
                    ),
                ),
            ),
            ClientLedger(
                client="Chen, Robert",
                entries=(
                    _entry(
                        "25,000.00",
                        client="Chen, Robert",
                        entered=date(2026, 7, 2),
                        kind=TrustEntryKind.DEPOSIT,
                        line=7,
                        description="Estate sale proceeds",
                    ),
                    _entry(
                        "-3,000.00",
                        client="Chen, Robert",
                        entered=date(2026, 7, 20),
                        kind=TrustEntryKind.DISBURSEMENT,
                        line=10,
                        description="Court filing and appraisal costs",
                    ),
                ),
            ),
        ),
    )
    return statement, books


def _discrepancy_pair() -> tuple[BankStatement, TrustBooks]:
    """The balanced story with two deliberate wrinkles: Chen's books
    disbursement is $45.00 more than the bank's check, and a fourth
    client's funds have sat untouched since December — one wrinkle for
    the findings section, one for the leakage section."""
    statement, books = _balanced_pair()
    statement = replace(
        statement,
        opening_balance=Money.parse("81,500.00"),
        closing_balance=Money.parse("88,921.50"),
    )
    chen = books.ledgers[2]
    chen_perturbed = replace(
        chen,
        entries=(
            chen.entries[0],
            replace(chen.entries[1], amount=Money.parse("-3,045.00")),
        ),
    )
    dawes = ClientLedger(
        client="Dawes, Elena",
        entries=(
            _entry(
                "1,500.00",
                client="Dawes, Elena",
                entered=date(2025, 12, 15),
                kind=TrustEntryKind.DEPOSIT,
                line=11,
                description="Cost retainer received",
            ),
        ),
    )
    books = replace(
        books,
        journal_balance=Money.parse("88,921.50"),
        ledgers=(books.ledgers[0], books.ledgers[1], chen_perturbed, dawes),
    )
    return statement, books


# ── the balanced page ──────────────────────────────────────────────────


def test_balanced_report_cites_the_three_balances_to_the_cent() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    assert isinstance(result, ThreeWayBalanced)
    report = render_three_way_report(result, statement=statement, books=books)

    assert "# Three-way trust reconciliation — IOLTA ****1234" in report
    assert "As of 2026-07-31." in report
    assert "| Bank closing balance | $87,421.50 |" in report
    assert "| Firm trust journal | $87,421.50 |" in report
    assert "| Sum of client ledgers | $87,421.50 |" in report
    assert "**BALANCED.**" in report
    assert "NOT BALANCED" not in report
    assert "## Findings" not in report
    assert (
        "Every figure above derives from the documents named; "
        "nothing was estimated." in report
    )


def test_report_names_the_documents_with_their_own_numbers() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    report = render_three_way_report(result, statement=statement, books=books)

    assert "period 2026-07-01 to 2026-07-31" in report
    assert "4 transactions" in report
    assert "opening balance $80,000.00" in report
    assert "3 client ledgers" in report
    assert "6 entries" in report


def test_report_speaks_plainly_without_exclamation() -> None:
    # The house voice: plain and evidential. No sentence in either
    # verdict may exclaim.
    balanced_statement, balanced_books = _balanced_pair()
    discrepancy_statement, discrepancy_books = _discrepancy_pair()
    for statement, books in (
        (balanced_statement, balanced_books),
        (discrepancy_statement, discrepancy_books),
    ):
        result = reconcile(statement, books)
        report = render_three_way_report(result, statement=statement, books=books)
        assert "!" not in report


def test_rendering_is_deterministic() -> None:
    statement, books = _discrepancy_pair()
    result = reconcile(statement, books)
    leakage = find_stale_client_funds(books, as_of=books.as_of)
    first = render_three_way_report(
        result, statement=statement, books=books, leakage=leakage
    )
    second = render_three_way_report(
        result, statement=statement, books=books, leakage=leakage
    )
    assert first == second


def test_singular_counts_read_as_singular() -> None:
    statement = BankStatement(
        account_label="IOLTA ****0000",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        opening_balance=Money.zero(),
        closing_balance=Money.parse("2,000.00"),
        transactions=(
            _transaction(
                "2,000.00",
                posted=date(2026, 7, 3),
                line=6,
                description="CLIENT TRUST DEPOSIT",
            ),
        ),
    )
    books = TrustBooks(
        as_of=PERIOD_END,
        journal_balance=Money.parse("2,000.00"),
        ledgers=(
            ClientLedger(
                client="Dawes, Elena",
                entries=(
                    _entry(
                        "2,000.00",
                        client="Dawes, Elena",
                        entered=date(2026, 7, 3),
                        kind=TrustEntryKind.DEPOSIT,
                        line=5,
                        description="Retainer received",
                    ),
                ),
            ),
        ),
    )
    report = render_three_way_report(
        reconcile(statement, books), statement=statement, books=books
    )
    assert "1 transaction," in report
    assert "1 client ledger," in report
    assert "1 entry." in report


# ── the discrepancy page: numbered evidence ────────────────────────────


def test_discrepancy_report_prints_numbered_findings_with_amounts_and_dates() -> None:
    statement, books = _discrepancy_pair()
    result = reconcile(statement, books)
    assert isinstance(result, ThreeWayDiscrepancy)
    assert len(result.findings) == 2
    report = render_three_way_report(result, statement=statement, books=books)

    assert "**NOT BALANCED.**" in report
    assert "- Bank closing balance less firm trust journal: $0.00." in report
    assert "- Firm trust journal less sum of client ledgers: $45.00." in report
    assert "## Findings" in report
    # Both sides of the $3,000.00 versus $3,045.00 wrinkle, numbered,
    # each carrying its amount and date.
    assert "1. Bank transaction of -$3,000.00 on 2026-07-20" in report
    assert "2. Books entry of -$3,045.00 for client 'Chen, Robert' on 2026-07-20" in (
        report
    )
    assert "statement line 9" in report
    assert "books line 10" in report
    assert "a human confirms or refutes each item" in report


# ── the leakage section: ran versus never ran ──────────────────────────


def test_leakage_section_absent_when_review_did_not_run() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    report = render_three_way_report(
        result, statement=statement, books=books, leakage=None
    )
    assert "Leakage" not in report


def test_leakage_section_states_when_review_flagged_nothing() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    report = render_three_way_report(
        result, statement=statement, books=books, leakage=()
    )
    assert "## Leakage review" in report
    assert "flagged no client balances" in report


def test_leakage_findings_print_as_numbered_evidence() -> None:
    statement, books = _discrepancy_pair()
    result = reconcile(statement, books)
    leakage = find_stale_client_funds(books, as_of=books.as_of)
    assert len(leakage) == 1
    report = render_three_way_report(
        result, statement=statement, books=books, leakage=leakage
    )
    assert "## Leakage review" in report
    assert "1. Client 'Dawes, Elena' has $1,500.00 in trust untouched" in report
    assert "2025-12-15" in report
    assert "candidate" in report


# ── the honesty gate: no report for someone else's documents ───────────


def test_report_refuses_a_result_from_a_different_statement() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    tampered = replace(statement, closing_balance=Money.parse("87,421.51"))
    with pytest.raises(ReportError) as caught:
        render_three_way_report(result, statement=tampered, books=books)
    message = str(caught.value)
    assert "$87,421.50" in message
    assert "$87,421.51" in message
    assert "not computed from this statement" in message


def test_report_refuses_a_result_from_books_of_another_date() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    with pytest.raises(ReportError) as caught:
        render_three_way_report(
            result,
            statement=statement,
            books=replace(books, as_of=date(2026, 8, 31)),
        )
    assert "2026-08-31" in str(caught.value)


def test_report_refuses_a_result_from_a_different_journal() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    with pytest.raises(ReportError) as caught:
        render_three_way_report(
            result,
            statement=statement,
            books=replace(books, journal_balance=Money.parse("90,000.00")),
        )
    assert "journal balance" in str(caught.value)


def test_report_refuses_a_result_from_different_ledgers() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    with pytest.raises(ReportError) as caught:
        render_three_way_report(
            result,
            statement=statement,
            books=replace(books, ledgers=books.ledgers[:2]),
        )
    assert "ledgers sum to" in str(caught.value)


# ── the goldens: the page itself is the spec ───────────────────────────


def test_golden_balanced_report(golden: SnapshotAssertion) -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    report = render_three_way_report(
        result, statement=statement, books=books, leakage=()
    )
    assert report == golden


def test_golden_discrepancy_report(golden: SnapshotAssertion) -> None:
    statement, books = _discrepancy_pair()
    result = reconcile(statement, books)
    leakage = find_stale_client_funds(books, as_of=books.as_of)
    report = render_three_way_report(
        result, statement=statement, books=books, leakage=leakage
    )
    assert report == golden


# ── the honesty gate covers the leakage section too ────────────────────


def test_report_refuses_leakage_for_a_client_absent_from_the_books() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    fabricated = LeakageFinding(
        client="Nobody, Ever",
        last_activity=date(2026, 1, 15),
        balance=Money.parse("12000.00"),
        days_stale=200,
        summary="Client 'Nobody, Ever' balance $12,000.00 untouched for 200 days",
    )
    with pytest.raises(ReportError, match="no ledger"):
        render_three_way_report(
            result, statement=statement, books=books, leakage=(fabricated,)
        )


def test_report_refuses_leakage_whose_balance_disagrees_with_the_ledger() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    ledger = books.ledgers[0]
    inflated = LeakageFinding(
        client=ledger.client,
        last_activity=max(entry.entered for entry in ledger.entries),
        balance=ledger.balance() + Money.parse("1000.00"),
        days_stale=200,
        summary="inflated balance",
    )
    with pytest.raises(ReportError, match="balances to"):
        render_three_way_report(
            result, statement=statement, books=books, leakage=(inflated,)
        )


def test_report_refuses_leakage_whose_last_activity_disagrees() -> None:
    statement, books = _balanced_pair()
    result = reconcile(statement, books)
    ledger = books.ledgers[0]
    backdated = LeakageFinding(
        client=ledger.client,
        last_activity=date(2020, 1, 1),
        balance=ledger.balance(),
        days_stale=2400,
        summary="backdated activity",
    )
    with pytest.raises(ReportError, match="latest entry"):
        render_three_way_report(
            result, statement=statement, books=books, leakage=(backdated,)
        )
