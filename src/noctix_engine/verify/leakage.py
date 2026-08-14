"""Leakage analysis, v1: client funds sitting in trust past their welcome.

"Leakage" is money nobody is watching: funds the firm has earned but
never billed, or a client is owed but was never sent. Its most common
signature needs nothing but the firm's own books — a client balance
with no ledger activity for longer than a staleness threshold. That is
the whole of v1's scope; bank-side leakage patterns wait until they
repeat across clients (creed #11).

Findings are candidates, never conclusions. A settlement held in
escrow may legitimately sit untouched for a year; the engine cannot
know why funds sat still, so it presents the evidence — client,
balance, last activity, days elapsed — and the human decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from noctix_engine.domain import TrustBooks
from noctix_engine.money import Money
from noctix_engine.verify.three_way import VerificationError


@dataclass(frozen=True, slots=True)
class LeakageFinding:
    """One client balance that has sat untouched beyond the threshold.

    Written for the firm's principal: the summary alone should be
    enough to open the right ledger and start asking why.
    """

    client: str
    last_activity: date
    balance: Money
    days_stale: int
    summary: str


def find_stale_client_funds(
    books: TrustBooks, *, as_of: date, stale_days: int = 180
) -> tuple[LeakageFinding, ...]:
    """Flag client balances untouched for strictly more than `stale_days`.

    Scope, stated precisely so the negative space is also a contract:

    - Only positive balances are candidates: a zero balance is not
      sitting money, and a negative balance is a reconciliation
      problem that `three_way` itemizes — flagging it here as leakage
      would misname the disease.
    - A ledger with no entries has nothing to date staleness from and
      no balance to flag; it is skipped.
    - "Untouched" means no entry of any kind since `last_activity`,
      the latest `entered` date in the ledger.
    - Measuring at an `as_of` later than `books.as_of` treats the
      books as complete through `as_of`; that is the caller's
      assertion to make. Measuring at an earlier `as_of` would require
      guessing which entries to un-know, so it halts instead.

    Findings are ordered most-stale first, ties broken by client
    label, so reruns produce identical reports.

    Raises `VerificationError` when `stale_days` is less than one,
    when `as_of` predates `books.as_of`, or when a ledger contains an
    entry dated after `as_of` — books claiming activity in the future
    are a document problem for a human, not an input.
    """
    if stale_days < 1:
        raise VerificationError(
            f"stale_days must be at least 1, got {stale_days}; a threshold of "
            "zero or less would flag every client balance and mean nothing"
        )
    if as_of < books.as_of:
        raise VerificationError(
            f"cannot measure staleness as of {as_of.isoformat()} using books "
            f"prepared as of {books.as_of.isoformat()}: the books contain "
            "activity after that date, and deciding which entries to ignore "
            "would be a guess — use books prepared as of the measurement date"
        )

    findings: list[LeakageFinding] = []
    for ledger in books.ledgers:
        if not ledger.entries:
            continue
        last_activity = max(entry.entered for entry in ledger.entries)
        if last_activity > as_of:
            raise VerificationError(
                f"client ledger {ledger.client!r} contains an entry dated "
                f"{last_activity.isoformat()}, after the as-of date "
                f"{as_of.isoformat()}; books claiming future activity are a "
                "document problem a human must resolve before analysis"
            )
        balance = ledger.balance()
        if balance.amount <= 0:
            continue
        days_stale = (as_of - last_activity).days
        if days_stale <= stale_days:
            continue
        findings.append(
            LeakageFinding(
                client=ledger.client,
                last_activity=last_activity,
                balance=balance,
                days_stale=days_stale,
                summary=(
                    f"Client {ledger.client!r} has {balance} in trust untouched "
                    f"for {days_stale} days (last activity "
                    f"{last_activity.isoformat()}) — candidate funds that "
                    "should have been disbursed or billed; needs human review"
                ),
            )
        )
    findings.sort(key=lambda finding: (-finding.days_stale, finding.client))
    return tuple(findings)
