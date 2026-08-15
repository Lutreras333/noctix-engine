"""The client-facing rendering of a three-way reconciliation.

This is the page the firm's principal actually reads, so it obeys the
house voice: plain sentences, every figure to the cent, no exclamation,
no adjective doing a number's job. Findings are printed as numbered
evidence — candidates for a human to confirm or refute, never
conclusions — because that is what the verify stage produced and a
renderer adds no judgment of its own.

Two properties are load-bearing:

- Deterministic. The same result and documents render byte-for-byte the
  same report. There is no timestamp and no randomness here; real time
  belongs to the run record, which stores this report's digest so any
  copy can be checked against what the run actually produced.
- Honest. The renderer refuses (ReportError) a result whose balances or
  date disagree with the statement and books it is asked to cite —
  a report must never attribute numbers to documents that do not carry
  them (creed #7: the negative space of "every claim traceable").
"""

from __future__ import annotations

from noctix_engine.domain import (
    BankStatement,
    ThreeWayBalanced,
    ThreeWayResult,
    TrustBooks,
)
from noctix_engine.verify.leakage import LeakageFinding


class ReportError(ValueError):
    """The renderer refused to print a claim its inputs do not support.

    Raised instead of guessing (creed #6): a result that disagrees with
    the documents it cites means the caller mixed up runs, and which
    input is the true one is not this module's guess to make.
    """


def render_three_way_report(
    result: ThreeWayResult,
    *,
    statement: BankStatement,
    books: TrustBooks,
    leakage: tuple[LeakageFinding, ...] | None = None,
) -> str:
    """Render one three-way reconciliation as client-readable Markdown.

    `result` must have been computed from exactly this `statement` and
    `books`; the renderer re-checks the date and all three balances and
    raises `ReportError` on any disagreement rather than print a report
    whose citations do not hold.

    `leakage` distinguishes ran-and-found-nothing from never-ran: pass
    the (possibly empty) findings tuple when the stale-funds review ran
    — an empty tuple renders an explicit "flagged nothing" line — and
    None when it did not run, which renders no leakage section at all.

    The output is deterministic: no timestamps, no randomness. The run
    record owns time and stores this report's digest.
    """
    _require_result_matches_documents(result, statement=statement, books=books)
    if leakage is not None:
        _require_leakage_matches_books(leakage, books=books)

    sections = [
        _header(result, statement=statement, books=books),
        _balances_table(result),
        _verdict(result),
    ]
    if leakage is not None:
        sections.append(_leakage_section(leakage))
    sections.append(
        "---\n\nEvery figure above derives from the documents named; "
        "nothing was estimated."
    )
    return "\n\n".join(sections) + "\n"


# ── the honesty gate ───────────────────────────────────────────────────


def _require_result_matches_documents(
    result: ThreeWayResult, *, statement: BankStatement, books: TrustBooks
) -> None:
    """Re-derive every citation the report is about to make.

    Each check restates one claim of the rendered page: the as-of date
    is the books' date and the statement's period end, and the three
    printed balances are the documents' own numbers.
    """
    if result.as_of != books.as_of or result.as_of != statement.period_end:
        raise ReportError(
            f"report refused: the reconciliation result is as of "
            f"{result.as_of.isoformat()}, but the statement ends "
            f"{statement.period_end.isoformat()} and the books are as of "
            f"{books.as_of.isoformat()} — this result was not computed from "
            "these documents"
        )
    if result.bank_balance != statement.closing_balance:
        raise ReportError(
            f"report refused: the result carries a bank balance of "
            f"{result.bank_balance}, but the statement's closing balance is "
            f"{statement.closing_balance} — this result was not computed from "
            "this statement"
        )
    if result.journal_balance != books.journal_balance:
        raise ReportError(
            f"report refused: the result carries a journal balance of "
            f"{result.journal_balance}, but the books state "
            f"{books.journal_balance} — this result was not computed from "
            "these books"
        )
    if result.ledgers_total != books.ledgers_total():
        raise ReportError(
            f"report refused: the result carries a client-ledgers total of "
            f"{result.ledgers_total}, but these books' ledgers sum to "
            f"{books.ledgers_total()} — this result was not computed from "
            "these books"
        )
    if isinstance(result, ThreeWayBalanced) and not (
        result.bank_balance == result.journal_balance == result.ledgers_total
    ):
        # Unrepresentable since ThreeWayBalanced asserts this at birth —
        # restated here because this gate's job is to re-derive every
        # claim the page prints, and BALANCED is the biggest claim on it.
        raise ReportError(
            "report refused: a BALANCED verdict whose three balances "
            f"disagree (bank {result.bank_balance}, journal "
            f"{result.journal_balance}, ledgers {result.ledgers_total})"
        )


def _require_leakage_matches_books(
    leakage: tuple[LeakageFinding, ...], *, books: TrustBooks
) -> None:
    """Re-derive every leakage citation against the books it will sit under.

    The leakage findings arrive as a separate argument, so nothing ties
    them to `books` structurally — this gate is that tie. Each finding's
    client must exist in the cited books, and its balance and last
    activity must be the ledger's own numbers.
    """
    ledgers_by_client = {ledger.client: ledger for ledger in books.ledgers}
    for finding in leakage:
        ledger = ledgers_by_client.get(finding.client)
        if ledger is None or not ledger.entries:
            raise ReportError(
                f"report refused: leakage finding names client "
                f"{finding.client!r}, but the cited books carry no ledger "
                "with activity under that name — this finding was not "
                "computed from these books"
            )
        if finding.balance != ledger.balance():
            raise ReportError(
                f"report refused: leakage finding states a balance of "
                f"{finding.balance} for client {finding.client!r}, but the "
                f"cited ledger balances to {ledger.balance()}"
            )
        last_activity = max(entry.entered for entry in ledger.entries)
        if finding.last_activity != last_activity:
            raise ReportError(
                f"report refused: leakage finding states last activity "
                f"{finding.last_activity.isoformat()} for client "
                f"{finding.client!r}, but the cited ledger's latest entry is "
                f"{last_activity.isoformat()}"
            )


# ── sections, in page order ────────────────────────────────────────────


def _header(
    result: ThreeWayResult, *, statement: BankStatement, books: TrustBooks
) -> str:
    entry_count = sum(len(ledger.entries) for ledger in books.ledgers)
    return (
        f"# Three-way trust reconciliation — {statement.account_label}\n"
        f"\n"
        f"As of {result.as_of.isoformat()}.\n"
        f"\n"
        f"## Documents read\n"
        f"\n"
        f"- Bank statement, {statement.account_label}, period "
        f"{statement.period_start.isoformat()} to "
        f"{statement.period_end.isoformat()}: "
        f"{_count(len(statement.transactions), 'transaction')}, "
        f"opening balance {statement.opening_balance}, "
        f"closing balance {statement.closing_balance}.\n"
        f"- Firm trust books as of {books.as_of.isoformat()}: "
        f"journal balance {books.journal_balance}; "
        f"{_count(len(books.ledgers), 'client ledger')}, "
        f"{_count(entry_count, 'entry', 'entries')}."
    )


def _balances_table(result: ThreeWayResult) -> str:
    return (
        f"## The three balances\n"
        f"\n"
        f"| Balance | Amount |\n"
        f"| --- | ---: |\n"
        f"| Bank closing balance | {result.bank_balance} |\n"
        f"| Firm trust journal | {result.journal_balance} |\n"
        f"| Sum of client ledgers | {result.ledgers_total} |"
    )


def _verdict(result: ThreeWayResult) -> str:
    if isinstance(result, ThreeWayBalanced):
        return (
            f"**BALANCED.** All three balances agree to the cent as of "
            f"{result.as_of.isoformat()}."
        )

    # A discrepancy is the product working (creed #6): print the two
    # deltas the comparison is defined by, then the itemized evidence.
    lines = [
        f"**NOT BALANCED.** The three balances disagree as of "
        f"{result.as_of.isoformat()}.",
        "",
        f"- Bank closing balance less firm trust journal: "
        f"{result.bank_versus_journal()}.",
        f"- Firm trust journal less sum of client ledgers: "
        f"{result.journal_versus_ledgers()}.",
        "",
        "## Findings",
        "",
        "Each finding is evidence, not a conclusion: matching is by exact "
        "amount only, and a human confirms or refutes each item before it "
        "is treated as explained.",
    ]
    for number, finding in enumerate(result.findings, start=1):
        lines.append("")
        lines.append(f"{number}. {finding.summary}.")
        lines.append("")
        lines.append(f"   {finding.detail}")
    return "\n".join(lines)


def _leakage_section(leakage: tuple[LeakageFinding, ...]) -> str:
    if not leakage:
        return (
            "## Leakage review\n"
            "\n"
            "The stale-funds review ran and flagged no client balances."
        )
    lines = [
        "## Leakage review",
        "",
        "Client balances untouched beyond the staleness threshold. Each is "
        "a candidate for disbursement or billing review, not a conclusion.",
    ]
    for number, finding in enumerate(leakage, start=1):
        lines.append("")
        lines.append(f"{number}. {finding.summary}.")
    return "\n".join(lines)


def _count(quantity: int, singular: str, plural: str | None = None) -> str:
    """'1 transaction', '4 transactions' — grammar the reader never notices."""
    if quantity == 1:
        return f"1 {singular}"
    return f"{quantity} {plural if plural is not None else singular + 's'}"
