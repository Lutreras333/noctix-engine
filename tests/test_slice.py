"""End-to-end executable spec for the First Slice orchestrator (creed #10).

These tests run the whole engine — real fixture files in, a real
SQLite record on disk, a rendered report out — and then reperform the
record like an auditor would: statuses read straight from the runs
table, digests recomputed from genesis, the report hashed against the
digest the run committed.

The three endings under test, and what each means:

- Balanced: the good ending, run completed.
- Discrepancy: ALSO a completed run — a reconciliation that surfaces
  findings with evidence is the product working (creed #6/#9), not an
  error.
- A genuinely broken document: the run halts, the halt is recorded
  with the original reason, and SliceError carries the same words.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from pathlib import Path

import pytest

from noctix_engine.domain import ThreeWayBalanced, ThreeWayDiscrepancy
from noctix_engine.record import RunRecord
from noctix_engine.slice import SliceError, run_first_slice

FIXTURES = Path(__file__).parent / "fixtures"


def _the_only_run(record_path: Path) -> tuple[str, str]:
    """Read (run_id, status) straight from the runs table — the auditor
    does not go through our API to learn what the record says."""
    connection = sqlite3.connect(record_path)
    try:
        rows = connection.execute("SELECT run_id, status FROM runs").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, f"expected exactly one run in the record, found {len(rows)}"
    return str(rows[0][0]), str(rows[0][1])


# ── the balanced ending ────────────────────────────────────────────────


def test_balanced_slice_end_to_end(tmp_path: Path) -> None:
    record_path = tmp_path / "record.sqlite"
    outcome = run_first_slice(
        bank_csv=FIXTURES / "slice_bank_statement.csv",
        bank_pdf=None,
        trust_books=FIXTURES / "sample_trust_books.csv",
        account_label="IOLTA ****1234",
        record_path=record_path,
        client_label="Northgate Family Law",
    )

    assert isinstance(outcome.result, ThreeWayBalanced)
    assert outcome.record_verified is True
    assert "**BALANCED.**" in outcome.report
    assert "| Bank closing balance | $87,421.50 |" in outcome.report
    assert "| Firm trust journal | $87,421.50 |" in outcome.report
    assert "| Sum of client ledgers | $87,421.50 |" in outcome.report

    run_id, status = _the_only_run(record_path)
    assert run_id == outcome.run_id
    assert status == "completed"


def test_balanced_slice_records_every_stage_with_evidence(tmp_path: Path) -> None:
    record_path = tmp_path / "record.sqlite"
    outcome = run_first_slice(
        bank_csv=FIXTURES / "slice_bank_statement.csv",
        bank_pdf=None,
        trust_books=FIXTURES / "sample_trust_books.csv",
        account_label="IOLTA ****1234",
        record_path=record_path,
        client_label="Northgate Family Law",
    )

    record = RunRecord(record_path)
    try:
        events = record.events(outcome.run_id)
        assert record.verify_chain(outcome.run_id) is True
    finally:
        record.close()

    assert [event.kind for event in events] == [
        "ingest",
        "ingest",
        "verify",
        "intent",
        "report",
    ]

    bank_ingest = events[0].payload
    assert bank_ingest["document"] == "slice_bank_statement.csv"
    assert bank_ingest["account_label"] == "IOLTA ****1234"
    assert bank_ingest["transactions"] == 4
    assert bank_ingest["opening_balance"] == "$80,000.00"
    assert bank_ingest["closing_balance"] == "$87,421.50"

    books_ingest = events[1].payload
    assert books_ingest["document"] == "sample_trust_books.csv"
    assert books_ingest["as_of"] == "2026-07-31"
    assert books_ingest["client_ledgers"] == 3
    assert books_ingest["entries"] == 6
    assert books_ingest["journal_balance"] == "$87,421.50"

    verify = events[2].payload
    assert verify["verdict"] == "balanced"
    assert verify["bank_balance"] == "$87,421.50"
    assert verify["journal_balance"] == "$87,421.50"
    assert verify["ledgers_total"] == "$87,421.50"
    assert verify["reconciliation_findings"] == 0
    assert verify["stale_fund_findings"] == 0
    assert verify["stale_days_threshold"] == 180

    intent = events[3].payload
    assert intent["intent"] == "none"
    assert "read-only" in str(intent["reason"])

    # The record holds the report's digest, not the report; hashing the
    # returned report must reproduce the committed digest exactly.
    report_event = events[4].payload
    expected_digest = hashlib.sha256(outcome.report.encode("utf-8")).hexdigest()
    assert report_event["report_digest_sha256"] == expected_digest
    assert report_event["report_characters"] == len(outcome.report)


def test_outcome_is_a_frozen_value(tmp_path: Path) -> None:
    outcome = run_first_slice(
        bank_csv=FIXTURES / "slice_bank_statement.csv",
        bank_pdf=None,
        trust_books=FIXTURES / "sample_trust_books.csv",
        account_label="IOLTA ****1234",
        record_path=tmp_path / "record.sqlite",
        client_label="Northgate Family Law",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        # The type checker refuses this line statically (that is half
        # the guarantee); this test pins the runtime half.
        outcome.report = "tampered"  # pyright: ignore[reportAttributeAccessIssue]


# ── the discrepancy ending: a completed run that shows its work ────────


def test_discrepancy_is_a_completed_run_whose_report_shows_findings(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "record.sqlite"
    outcome = run_first_slice(
        bank_csv=FIXTURES / "slice_bank_statement.csv",
        bank_pdf=None,
        trust_books=FIXTURES / "slice_trust_books_perturbed.csv",
        account_label="IOLTA ****1234",
        record_path=record_path,
        client_label="Northgate Family Law",
    )

    assert isinstance(outcome.result, ThreeWayDiscrepancy)
    assert len(outcome.result.findings) == 2
    assert outcome.record_verified is True
    assert "**NOT BALANCED.**" in outcome.report
    assert "## Findings" in outcome.report
    assert "-$3,000.00" in outcome.report
    assert "-$3,045.00" in outcome.report

    run_id, status = _the_only_run(record_path)
    assert run_id == outcome.run_id
    assert status == "completed"

    record = RunRecord(record_path)
    try:
        events = record.events(outcome.run_id)
    finally:
        record.close()
    verify = next(event for event in events if event.kind == "verify")
    assert verify.payload["verdict"] == "discrepancy"
    assert verify.payload["reconciliation_findings"] == 2


# ── the PDF door: same slice, the other bank source ────────────────────


def test_pdf_bank_source_runs_the_slice_end_to_end(tmp_path: Path) -> None:
    # The PDF fixture tells a different bank story than these books, so
    # the run completes with a discrepancy — which is exactly the point:
    # the door works, the comparison runs, the record verifies. The
    # label is the one the PDF itself names; the PDF door refuses any
    # other.
    record_path = tmp_path / "record.sqlite"
    outcome = run_first_slice(
        bank_csv=None,
        bank_pdf=FIXTURES / "sample_statement.pdf",
        trust_books=FIXTURES / "sample_trust_books.csv",
        account_label="IOLTA ****0000",
        record_path=record_path,
        client_label="Northgate Family Law",
    )
    assert isinstance(outcome.result, ThreeWayDiscrepancy)
    assert outcome.record_verified is True
    _, status = _the_only_run(record_path)
    assert status == "completed"

    record = RunRecord(record_path)
    try:
        events = record.events(outcome.run_id)
    finally:
        record.close()
    assert events[0].payload["document"] == "sample_statement.pdf"


# ── exactly one bank source ────────────────────────────────────────────


def test_both_bank_sources_refuse_before_any_run_begins(tmp_path: Path) -> None:
    record_path = tmp_path / "record.sqlite"
    with pytest.raises(SliceError) as caught:
        run_first_slice(
            bank_csv=FIXTURES / "slice_bank_statement.csv",
            bank_pdf=FIXTURES / "sample_statement.pdf",
            trust_books=FIXTURES / "sample_trust_books.csv",
            account_label="IOLTA ****1234",
            record_path=record_path,
            client_label="Northgate Family Law",
        )
    assert "exactly one bank source" in str(caught.value)
    # Refused before a run began: no record file was even created.
    assert not record_path.exists()


def test_no_bank_source_refuses_before_any_run_begins(tmp_path: Path) -> None:
    record_path = tmp_path / "record.sqlite"
    with pytest.raises(SliceError) as caught:
        run_first_slice(
            bank_csv=None,
            bank_pdf=None,
            trust_books=FIXTURES / "sample_trust_books.csv",
            account_label="IOLTA ****1234",
            record_path=record_path,
            client_label="Northgate Family Law",
        )
    assert "exactly one bank source" in str(caught.value)
    assert not record_path.exists()


# ── the halt: a broken document ends the run loudly and on the record ──


def test_broken_bank_document_halts_with_the_reason_recorded(tmp_path: Path) -> None:
    record_path = tmp_path / "record.sqlite"
    with pytest.raises(SliceError) as caught:
        run_first_slice(
            bank_csv=FIXTURES / "slice_bank_malformed.csv",
            bank_pdf=None,
            trust_books=FIXTURES / "sample_trust_books.csv",
            account_label="IOLTA ****1234",
            record_path=record_path,
            client_label="Northgate Family Law",
        )
    # SliceError carries the parser's original client-readable words.
    message = str(caught.value)
    assert "line 6" in message
    assert "printed balance" in message

    run_id, status = _the_only_run(record_path)
    assert status == "halted"

    record = RunRecord(record_path)
    try:
        events = record.events(run_id)
        # A halted run's chain must still verify: the halt is part of
        # the proof, not damage to it.
        assert record.verify_chain(run_id) is True
    finally:
        record.close()
    assert events[-1].kind == "halt"
    assert events[-1].payload["exception_type"] == "BankDocumentError"
    assert "printed balance" in str(events[-1].payload["reason"])


def test_unreadable_books_halt_after_the_bank_ingest_was_recorded(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "record.sqlite"
    with pytest.raises(SliceError) as caught:
        run_first_slice(
            bank_csv=FIXTURES / "slice_bank_statement.csv",
            bank_pdf=None,
            trust_books=tmp_path / "no_such_books.csv",
            account_label="IOLTA ****1234",
            record_path=record_path,
            client_label="Northgate Family Law",
        )
    assert "cannot read trust-books file" in str(caught.value)

    run_id, status = _the_only_run(record_path)
    assert status == "halted"

    record = RunRecord(record_path)
    try:
        events = record.events(run_id)
        assert record.verify_chain(run_id) is True
    finally:
        record.close()
    # The stage that succeeded stays on the record; the halt follows it.
    assert [event.kind for event in events] == ["ingest", "halt"]
    assert events[0].payload["document"] == "slice_bank_statement.csv"
