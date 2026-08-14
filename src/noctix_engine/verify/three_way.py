"""The Florida Rule 5-1.2 three-way reconciliation over proven values.

Trust accounting has one load-bearing comparison: as of a single date,
the bank's closing balance, the firm's trust journal balance, and the
sum of every client ledger must agree to the cent. This module performs
that comparison and nothing else.

Two refusals define its character:

- Dates must match. A statement ending one day and books "as of"
  another are photographs of two different moments; comparing them
  would be the guess creed #6 forbids, so `reconcile` halts with a
  client-readable reason instead.
- Matching is exact-amount only, by deliberate refusal. Near-miss
  heuristics — dates within a few days, transposed digits, split
  amounts — manufacture explanations, and a manufactured explanation
  is still a guess. A transposed-digit pair therefore surfaces as two
  findings, one per side, because that is the true state of the
  evidence.

Findings never assert causes. Every one is worded as a candidate for
the firm's principal to confirm or refute, and carries the amounts,
dates, and source lines needed to pull the underlying document in
seconds.
"""

from __future__ import annotations

from collections import Counter

from noctix_engine.domain import (
    BankStatement,
    BankTransaction,
    ReconciliationFinding,
    ThreeWayBalanced,
    ThreeWayDiscrepancy,
    ThreeWayResult,
    TrustBooks,
    TrustLedgerEntry,
)
from noctix_engine.money import Money


class VerificationError(ValueError):
    """Verification refused to proceed rather than guess (creed #6).

    The message is written for the firm's principal, not an engineer:
    it names the values in conflict and what a human must resolve
    before the engine will compare anything.
    """


def reconcile(statement: BankStatement, books: TrustBooks) -> ThreeWayResult:
    """Perform the three-way comparison for one date.

    Returns `ThreeWayBalanced` iff bank closing balance, journal
    balance, and the sum of client ledgers agree to the cent.
    Otherwise returns `ThreeWayDiscrepancy` whose findings are the
    three safe diagnoses this engine is willing to attempt:

    (a) a bank transaction with no same-amount books entry in the
        period — candidate uncredited deposit or unrecorded
        disbursement;
    (b) a books entry in the period with no same-amount bank
        transaction — candidate outstanding or uncleared item;
    (c) a client ledger whose balance is negative — one client's money
        may have paid another's obligations.

    Raises `VerificationError` when `statement.period_end` differs
    from `books.as_of`: the three-way comparison is only defined for
    three balances as of the same date.
    """
    if statement.period_end != books.as_of:
        raise VerificationError(
            f"the bank statement ends {statement.period_end.isoformat()} but the "
            f"firm's books are as of {books.as_of.isoformat()}; the three-way "
            "comparison requires all three balances as of the same date — "
            "obtain matching documents rather than compare across dates"
        )

    bank_balance = statement.closing_balance
    journal_balance = books.journal_balance
    ledgers_total = books.ledgers_total()

    if bank_balance == journal_balance == ledgers_total:
        return ThreeWayBalanced(
            as_of=books.as_of,
            bank_balance=bank_balance,
            journal_balance=journal_balance,
            ledgers_total=ledgers_total,
        )

    # The deltas live on ThreeWayDiscrepancy itself (bank_versus_journal,
    # journal_versus_ledgers); the findings below are the itemized
    # evidence a human uses to explain those deltas.
    findings = (
        *_unmatched_bank_transactions(statement, books),
        *_unmatched_book_entries(statement, books),
        *_negative_client_ledgers(books),
    )
    return ThreeWayDiscrepancy(
        as_of=books.as_of,
        bank_balance=bank_balance,
        journal_balance=journal_balance,
        ledgers_total=ledgers_total,
        findings=findings,
    )


# ── exact-amount matching, multiset semantics ──────────────────────────
#
# Both sides are compared as multisets of amounts restricted to the
# statement period: two $100.00 deposits at the bank consume two
# $100.00 entries in the books, never one entry twice. Anything left
# over on either side becomes a finding, in document order so reruns
# produce identical reports.


def _entries_in_period(
    statement: BankStatement, books: TrustBooks
) -> tuple[TrustLedgerEntry, ...]:
    """Every books entry dated inside the statement period, books order.

    Entries outside the period belong to other statements' stories and
    are excluded from matching on both sides.
    """
    return tuple(
        entry
        for ledger in books.ledgers
        for entry in ledger.entries
        if statement.period_start <= entry.entered <= statement.period_end
    )


def _unmatched_bank_transactions(
    statement: BankStatement, books: TrustBooks
) -> tuple[ReconciliationFinding, ...]:
    """Diagnosis (a): bank lines with no same-amount books entry."""
    unmatched: Counter[Money] = Counter(
        transaction.amount for transaction in statement.transactions
    ) - Counter(entry.amount for entry in _entries_in_period(statement, books))
    findings: list[ReconciliationFinding] = []
    for transaction in statement.transactions:
        if unmatched[transaction.amount] > 0:
            unmatched[transaction.amount] -= 1
            findings.append(_bank_side_finding(transaction))
    return tuple(findings)


def _unmatched_book_entries(
    statement: BankStatement, books: TrustBooks
) -> tuple[ReconciliationFinding, ...]:
    """Diagnosis (b): books entries with no same-amount bank line."""
    entries = _entries_in_period(statement, books)
    unmatched: Counter[Money] = Counter(entry.amount for entry in entries) - Counter(
        transaction.amount for transaction in statement.transactions
    )
    findings: list[ReconciliationFinding] = []
    for entry in entries:
        if unmatched[entry.amount] > 0:
            unmatched[entry.amount] -= 1
            findings.append(_books_side_finding(entry))
    return tuple(findings)


def _negative_client_ledgers(books: TrustBooks) -> tuple[ReconciliationFinding, ...]:
    """Diagnosis (c): a client ledger below zero.

    A negative client balance means the trust account may have paid one
    client's obligations with another client's money — the single worst
    signal in trust accounting, so it is always itemized when the
    balances disagree.
    """
    findings: list[ReconciliationFinding] = []
    for ledger in books.ledgers:
        balance = ledger.balance()
        if balance.amount < 0:
            findings.append(
                ReconciliationFinding(
                    summary=(
                        f"Client ledger {ledger.client!r} has a negative "
                        f"balance of {balance}"
                    ),
                    detail=(
                        f"The trust ledger for {ledger.client!r} sums to "
                        f"{balance} across {len(ledger.entries)} entries. A "
                        "client ledger must never go below zero: this is a "
                        "candidate case of one client's funds covering "
                        "another's disbursements, and requires human review "
                        "of every entry in this ledger."
                    ),
                    amount=balance,
                )
            )
    return tuple(findings)


# ── finding wording: evidence, never conclusions ───────────────────────


def _bank_side_finding(transaction: BankTransaction) -> ReconciliationFinding:
    """Word an unmatched bank line as a candidate, by sign."""
    if transaction.amount.amount > 0:
        candidate = "a candidate deposit not yet credited in the books"
    else:
        candidate = "a candidate disbursement not yet recorded in the books"
    return ReconciliationFinding(
        summary=(
            f"Bank transaction of {transaction.amount} on "
            f"{transaction.posted.isoformat()} has no same-amount books entry"
        ),
        detail=(
            f"The bank statement shows {transaction.amount} posted "
            f"{transaction.posted.isoformat()} (statement line "
            f"{transaction.source_line}, {transaction.description!r}) with no "
            f"books entry for the same amount in the period. This is {candidate}. "
            "Matching is by exact amount only — the engine does not pair "
            "near-miss amounts or dates — so a human must confirm or refute "
            "this before it is treated as explained."
        ),
        amount=transaction.amount,
    )


def _books_side_finding(entry: TrustLedgerEntry) -> ReconciliationFinding:
    """Word an unmatched books entry as a candidate, by sign."""
    if entry.amount.amount > 0:
        candidate = "a candidate deposit in transit that has not reached the bank"
    else:
        candidate = "a candidate outstanding disbursement that has not cleared"
    return ReconciliationFinding(
        summary=(
            f"Books entry of {entry.amount} for client {entry.client!r} on "
            f"{entry.entered.isoformat()} has no same-amount bank transaction"
        ),
        detail=(
            f"The trust ledger for {entry.client!r} records {entry.amount} "
            f"entered {entry.entered.isoformat()} ({entry.kind.value}, books "
            f"line {entry.source_line}, {entry.description!r}) with no bank "
            f"transaction for the same amount in the period. This is {candidate}. "
            "Matching is by exact amount only — the engine does not pair "
            "near-miss amounts or dates — so a human must confirm or refute "
            "this before it is treated as explained."
        ),
        amount=entry.amount,
    )
