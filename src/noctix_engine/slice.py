"""The First Slice: the engine's one public entry point.

One call runs the whole read-only engagement: ingest the bank's story
and the firm's, verify them against each other, decline to act (the
slice is read-only by design), render the report, and prove the run
record's chain. Everything in between is one of the deep modules this
file merely sequences — an orchestrator earns its keep by staying thin.

The record discipline (creed #9): every stage appends an event with the
document names, counts, and balances a stranger needs to reperform the
run. The report itself is NOT stored — it is reproducible from the
named documents — but its SHA-256 digest is, so any copy of the report
can be checked against what this run actually produced.

Halting: any stage exception ends the run. The record's context manager
writes the halt event with the original exception's type and message
(that is the record module's contract), and the caller receives a
`SliceError` carrying the same client-readable message. A halt is the
product working, not the product failing (creed #6).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from noctix_engine import __version__
from noctix_engine.domain import ThreeWayBalanced, ThreeWayResult
from noctix_engine.ingest.bank_csv import parse_bank_csv
from noctix_engine.ingest.bank_pdf import parse_bank_pdf
from noctix_engine.ingest.trust_books_csv import parse_trust_books
from noctix_engine.record import RunRecord
from noctix_engine.report.three_way_report import render_three_way_report
from noctix_engine.verify.leakage import find_stale_client_funds
from noctix_engine.verify.three_way import reconcile

# The v1 staleness threshold for the leakage review. One committed
# number, recorded in the verify event so every report's leakage section
# can be reperformed against the same line.
_STALE_DAYS: Final = 180


class SliceError(RuntimeError):
    """The First Slice halted; the message is the original halt reason.

    Whatever stage refused — a document that does not add up, books
    that contradict themselves, a date mismatch — its client-readable
    message is carried here verbatim, and the run record holds the same
    reason as the halt event.
    """


@dataclass(frozen=True, slots=True)
class FirstSliceOutcome:
    """What one completed First Slice run hands back.

    `record_verified` is the auditor's move performed on ourselves:
    after the run closed, the record was reopened cold and every digest
    recomputed from genesis (creed #9).
    """

    run_id: str
    result: ThreeWayResult
    report: str
    record_verified: bool


def run_first_slice(
    *,
    bank_csv: Path | None,
    bank_pdf: Path | None,
    trust_books: Path,
    account_label: str,
    record_path: Path,
    client_label: str,
) -> FirstSliceOutcome:
    """Run the read-only First Slice for one firm, one period.

    Exactly one bank source must be given — `bank_csv` or `bank_pdf`,
    never both, never neither; anything else raises `SliceError` before
    a run is even begun. `trust_books` is the firm's interchange CSV,
    `account_label` names the trust account on the report (never a full
    account number), `record_path` is the SQLite run record to append
    to, and `client_label` names the firm in that record.

    Returns a `FirstSliceOutcome` whose run completed: a reconciliation
    that finds discrepancies still completes, because a discrepancy
    with evidence is the product doing its job. Only a stage that
    cannot proceed — an unreadable or internally inconsistent document,
    a date mismatch — halts the run and raises `SliceError` with the
    original reason.
    """
    if (bank_csv is None) == (bank_pdf is None):
        raise SliceError(
            "exactly one bank source must be given: pass bank_csv or "
            "bank_pdf, not both and not neither — which document tells the "
            "bank's story is the caller's statement to make, not a guess"
        )

    record = RunRecord(record_path)
    try:
        handle = record.begin(client_label=client_label, engine_version=__version__)
        run_id = handle.run_id
        try:
            with handle:
                # ── ingest: hostile documents in, proven values out ────
                if bank_csv is not None:
                    statement = parse_bank_csv(bank_csv, account_label=account_label)
                    bank_document = bank_csv.name
                else:
                    assert bank_pdf is not None, "guarded by the one-source check"
                    statement = parse_bank_pdf(bank_pdf, account_label=account_label)
                    bank_document = bank_pdf.name
                handle.event(
                    "ingest",
                    {
                        "document": bank_document,
                        "content": "bank_statement",
                        "account_label": statement.account_label,
                        "period_start": statement.period_start.isoformat(),
                        "period_end": statement.period_end.isoformat(),
                        "transactions": len(statement.transactions),
                        "opening_balance": str(statement.opening_balance),
                        "closing_balance": str(statement.closing_balance),
                    },
                )

                books = parse_trust_books(trust_books)
                handle.event(
                    "ingest",
                    {
                        "document": trust_books.name,
                        "content": "trust_books",
                        "as_of": books.as_of.isoformat(),
                        "client_ledgers": len(books.ledgers),
                        "entries": sum(len(ledger.entries) for ledger in books.ledgers),
                        "journal_balance": str(books.journal_balance),
                    },
                )

                # ── verify: the three-way comparison, then leakage ─────
                result = reconcile(statement, books)
                # Staleness is measured at the books' own as-of date: the
                # run record owns real time, and a rerun next month must
                # reproduce this run's findings exactly.
                leakage = find_stale_client_funds(
                    books, as_of=books.as_of, stale_days=_STALE_DAYS
                )
                handle.event(
                    "verify",
                    {
                        "verdict": "balanced"
                        if isinstance(result, ThreeWayBalanced)
                        else "discrepancy",
                        "as_of": result.as_of.isoformat(),
                        "bank_balance": str(result.bank_balance),
                        "journal_balance": str(result.journal_balance),
                        "ledgers_total": str(result.ledgers_total),
                        "reconciliation_findings": 0
                        if isinstance(result, ThreeWayBalanced)
                        else len(result.findings),
                        "stale_fund_findings": len(leakage),
                        "stale_days_threshold": _STALE_DAYS,
                    },
                )

                # ── act: the recorded refusal to act ───────────────────
                handle.event(
                    "intent",
                    {
                        "intent": "none",
                        "reason": (
                            "the First Slice is read-only by design: it "
                            "verifies and reports; no external action is "
                            "proposed or executed"
                        ),
                    },
                )

                # ── report: digest committed, document reproducible ────
                report = render_three_way_report(
                    result, statement=statement, books=books, leakage=leakage
                )
                handle.event(
                    "report",
                    {
                        "report_digest_sha256": hashlib.sha256(
                            report.encode("utf-8")
                        ).hexdigest(),
                        "report_characters": len(report),
                    },
                )
        except Exception as error:
            # The context manager has already recorded the halt with the
            # original exception's type and message; the caller gets the
            # same words under the orchestrator's one exception type.
            raise SliceError(str(error)) from error
    finally:
        record.close()

    # The auditor's move, performed on ourselves: reopen the record cold
    # and recompute every digest from genesis (creed #9).
    auditor = RunRecord(record_path)
    try:
        record_verified = auditor.verify_chain(run_id)
    finally:
        auditor.close()

    return FirstSliceOutcome(
        run_id=run_id,
        result=result,
        report=report,
        record_verified=record_verified,
    )
