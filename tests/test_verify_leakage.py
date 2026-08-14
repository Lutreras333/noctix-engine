"""Executable spec for stale-client-funds leakage detection (creed #10).

Dates are all fixed constants — staleness is arithmetic on the given
`as_of`, never on the wall clock, so these tests are immune to the
calendar they run on.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from noctix_engine.domain import (
    ClientLedger,
    TrustBooks,
    TrustEntryKind,
    TrustLedgerEntry,
)
from noctix_engine.money import Money
from noctix_engine.verify.leakage import LeakageFinding, find_stale_client_funds
from noctix_engine.verify.three_way import VerificationError

BOOKS_AS_OF = date(2026, 6, 30)


def _entry(
    amount: str, *, client: str, entered: date, line: int = 1
) -> TrustLedgerEntry:
    parsed = Money.parse(amount)
    kind = TrustEntryKind.DEPOSIT if parsed.amount > 0 else TrustEntryKind.DISBURSEMENT
    return TrustLedgerEntry(
        client=client,
        entered=entered,
        kind=kind,
        description="test entry",
        amount=parsed,
        source_line=line,
    )


def _books(*ledgers: ClientLedger, as_of: date = BOOKS_AS_OF) -> TrustBooks:
    running = Money.zero()
    for ledger in ledgers:
        running = running + ledger.balance()
    return TrustBooks(as_of=as_of, journal_balance=running, ledgers=ledgers)


def _ledger(client: str, *entries: TrustLedgerEntry) -> ClientLedger:
    return ClientLedger(client=client, entries=entries)


# ── the positive space: stale money is flagged with evidence ───────────


def test_stale_balance_is_flagged_with_full_evidence() -> None:
    last_activity = date(2025, 12, 1)
    books = _books(
        _ledger("Alvarez", _entry("2,500.00", client="Alvarez", entered=last_activity))
    )
    findings = find_stale_client_funds(books, as_of=BOOKS_AS_OF)
    assert len(findings) == 1
    finding = findings[0]
    assert isinstance(finding, LeakageFinding)
    assert finding.client == "Alvarez"
    assert finding.last_activity == last_activity
    assert finding.balance == Money.parse("2,500.00")
    assert finding.days_stale == 211
    assert "Alvarez" in finding.summary
    assert "$2,500.00" in finding.summary
    assert "211 days" in finding.summary
    assert "2025-12-01" in finding.summary
    assert "candidate" in finding.summary


def test_last_activity_is_the_latest_entry_of_any_kind() -> None:
    # A disbursement counts as activity: the old deposit alone does not
    # make the balance stale if someone touched the ledger recently.
    books = _books(
        _ledger(
            "Alvarez",
            _entry("2,500.00", client="Alvarez", entered=date(2025, 6, 1), line=1),
            _entry("-500.00", client="Alvarez", entered=date(2026, 5, 15), line=2),
        )
    )
    assert find_stale_client_funds(books, as_of=BOOKS_AS_OF) == ()


def test_threshold_is_strictly_beyond() -> None:
    # Exactly stale_days old is not "beyond the threshold"; one more
    # day is. Both edges pinned so the boundary can never drift.
    at_threshold = BOOKS_AS_OF - timedelta(days=180)
    beyond_threshold = BOOKS_AS_OF - timedelta(days=181)
    books = _books(
        _ledger("AtLimit", _entry("100.00", client="AtLimit", entered=at_threshold)),
        _ledger(
            "PastLimit", _entry("100.00", client="PastLimit", entered=beyond_threshold)
        ),
    )
    findings = find_stale_client_funds(books, as_of=BOOKS_AS_OF)
    assert [finding.client for finding in findings] == ["PastLimit"]
    assert findings[0].days_stale == 181


def test_custom_stale_days_is_honored() -> None:
    books = _books(
        _ledger(
            "Alvarez",
            _entry(
                "100.00", client="Alvarez", entered=BOOKS_AS_OF - timedelta(days=45)
            ),
        )
    )
    assert find_stale_client_funds(books, as_of=BOOKS_AS_OF) == ()
    findings = find_stale_client_funds(books, as_of=BOOKS_AS_OF, stale_days=30)
    assert len(findings) == 1
    assert findings[0].days_stale == 45


def test_findings_are_ordered_most_stale_first_then_by_client() -> None:
    stale_date = date(2025, 1, 10)
    books = _books(
        _ledger("Zamora", _entry("10.00", client="Zamora", entered=stale_date)),
        _ledger("Brennan", _entry("20.00", client="Brennan", entered=stale_date)),
        _ledger("Alvarez", _entry("30.00", client="Alvarez", entered=date(2025, 6, 1))),
    )
    findings = find_stale_client_funds(books, as_of=BOOKS_AS_OF)
    # Zamora and Brennan tie on staleness and sort by client label;
    # Alvarez is less stale and comes last.
    assert [finding.client for finding in findings] == ["Brennan", "Zamora", "Alvarez"]


# ── the negative space: what is deliberately not flagged ───────────────


def test_zero_balance_is_not_flagged_no_matter_how_old() -> None:
    books = _books(
        _ledger(
            "Alvarez",
            _entry("500.00", client="Alvarez", entered=date(2024, 1, 5), line=1),
            _entry("-500.00", client="Alvarez", entered=date(2024, 2, 5), line=2),
        )
    )
    assert find_stale_client_funds(books, as_of=BOOKS_AS_OF) == ()


def test_negative_balance_is_not_flagged_as_leakage() -> None:
    # A negative ledger is a reconciliation emergency, not idle money;
    # three_way itemizes it. Flagging it here would misname the disease.
    books = _books(
        _ledger(
            "Alvarez", _entry("-500.00", client="Alvarez", entered=date(2024, 1, 5))
        )
    )
    assert find_stale_client_funds(books, as_of=BOOKS_AS_OF) == ()


def test_ledger_with_no_entries_is_skipped() -> None:
    books = _books(_ledger("Alvarez"))
    assert find_stale_client_funds(books, as_of=BOOKS_AS_OF) == ()


# ── the halts: refuse to guess ─────────────────────────────────────────


def test_stale_days_below_one_halts() -> None:
    books = _books(
        _ledger("Alvarez", _entry("100.00", client="Alvarez", entered=date(2026, 1, 1)))
    )
    with pytest.raises(VerificationError, match="at least 1"):
        find_stale_client_funds(books, as_of=BOOKS_AS_OF, stale_days=0)


def test_as_of_before_books_date_halts() -> None:
    books = _books(
        _ledger("Alvarez", _entry("100.00", client="Alvarez", entered=date(2026, 1, 1)))
    )
    with pytest.raises(VerificationError) as caught:
        find_stale_client_funds(books, as_of=date(2026, 5, 31))
    message = str(caught.value)
    assert "2026-05-31" in message
    assert "2026-06-30" in message


def test_entry_dated_after_as_of_halts() -> None:
    # The type system cannot prove books contain no future-dated
    # entries, so the analysis must halt on one rather than compute a
    # negative staleness.
    books = _books(
        _ledger(
            "Alvarez", _entry("100.00", client="Alvarez", entered=date(2026, 7, 15))
        ),
        as_of=date(2026, 6, 30),
    )
    with pytest.raises(VerificationError) as caught:
        find_stale_client_funds(books, as_of=date(2026, 6, 30))
    message = str(caught.value)
    assert "Alvarez" in message
    assert "2026-07-15" in message
