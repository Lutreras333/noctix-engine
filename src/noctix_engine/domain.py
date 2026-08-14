"""The engine's shared value types: frozen, exact, and boring on purpose.

Everything here is an immutable value produced by a boundary parser
(creed #5). Verification computes over these; nothing downstream ever
re-parses a document or re-derives a number a parser already proved.

Dates are `datetime.date` — trust accounting is a calendar-day world.
Timestamps appear only in the run record, which owns real time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from noctix_engine.money import Money

# ── the bank's side of the story ───────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BankTransaction:
    """One line of a trust-account bank statement, as printed."""

    posted: date
    description: str
    amount: Money  # deposits positive, disbursements negative — parser's contract
    source_line: int  # where in the document this came from; every claim traceable


@dataclass(frozen=True, slots=True)
class BankStatement:
    """A parsed trust-account statement for one period.

    The parser proves opening + sum(transactions) == closing before this
    object can exist; a statement that doesn't internally add up is a
    document problem to put in front of a human, not an input.
    """

    account_label: str  # e.g. "IOLTA ****1234" — never a full account number
    period_start: date
    period_end: date
    opening_balance: Money
    closing_balance: Money
    transactions: tuple[BankTransaction, ...]


# ── the firm's side of the story ───────────────────────────────────────


class TrustEntryKind(Enum):
    """What the firm's books say a trust movement was."""

    DEPOSIT = "deposit"
    DISBURSEMENT = "disbursement"
    FEE_TRANSFER = "fee_transfer"  # earned fees moving to operating


@dataclass(frozen=True, slots=True)
class TrustLedgerEntry:
    """One movement in one client's trust ledger, per the firm's records."""

    client: str  # the firm's client/matter label, verbatim from their export
    entered: date
    kind: TrustEntryKind
    description: str
    amount: Money  # signed: deposits positive, money leaving trust negative
    source_line: int


@dataclass(frozen=True, slots=True)
class ClientLedger:
    """One client's complete trust ledger for the period."""

    client: str
    entries: tuple[TrustLedgerEntry, ...]

    def balance(self) -> Money:
        running = Money.zero()
        for entry in self.entries:
            running = running + entry.amount
        return running


@dataclass(frozen=True, slots=True)
class TrustBooks:
    """The firm's trust records: the journal total and the per-client split.

    Florida's three-way reconciliation (Rule 5-1.2) compares three
    numbers as of the same date: the bank's closing balance, the firm's
    journal balance, and the sum of every client ledger. This type
    carries the firm's two.
    """

    as_of: date
    journal_balance: Money
    ledgers: tuple[ClientLedger, ...]

    def ledgers_total(self) -> Money:
        running = Money.zero()
        for ledger in self.ledgers:
            running = running + ledger.balance()
        return running


# ── what verification concludes ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThreeWayBalanced:
    """All three balances agree to the cent. The good ending."""

    as_of: date
    bank_balance: Money
    journal_balance: Money
    ledgers_total: Money


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One human-readable reason the balances disagree.

    Findings are written for the firm's principal and the auditor, not
    for engineers: every one names dates, amounts, and document lines.
    """

    summary: str
    detail: str
    amount: Money


@dataclass(frozen=True, slots=True)
class ThreeWayDiscrepancy:
    """The balances disagree. The engine halts here and shows its work.

    Per creed #6 this is not an error state of the engine — it is the
    product doing its job: surfacing, with evidence, that the books and
    the bank tell different stories.
    """

    as_of: date
    bank_balance: Money
    journal_balance: Money
    ledgers_total: Money
    findings: tuple[ReconciliationFinding, ...]

    def bank_versus_journal(self) -> Money:
        return self.bank_balance - self.journal_balance

    def journal_versus_ledgers(self) -> Money:
        return self.journal_balance - self.ledgers_total


type ThreeWayResult = ThreeWayBalanced | ThreeWayDiscrepancy
