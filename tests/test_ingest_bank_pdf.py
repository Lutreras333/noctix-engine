"""Executable spec for the bank-statement PDF door.

The correctness proof for PDF extraction is agreement: the committed
PDF fixture and its CSV twin describe the SAME statement, so the two
parsers must produce identical content, transaction for transaction.
All arithmetic refusal paths (duplicate rule, running-balance proof,
sub-cent, internal sum) run through `_prove_statement`, which both
doors share — they are specified once, in test_ingest_bank_csv.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from noctix_engine.domain import BankStatement
from noctix_engine.ingest.bank_csv import BankDocumentError, parse_bank_csv
from noctix_engine.ingest.bank_pdf import parse_bank_pdf
from noctix_engine.money import Money

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_PDF = FIXTURES / "sample_statement.pdf"
SAMPLE_CSV = FIXTURES / "sample_statement.csv"
NO_TABLE_PDF = FIXTURES / "no_table.pdf"
ACCOUNT = "IOLTA ****0000"


def without_provenance(statement: BankStatement) -> BankStatement:
    """Zero out source_line for cross-format comparison.

    Provenance deliberately differs between the twins: the CSV parser
    records physical file lines (6..23) and the PDF parser records
    printed table rows (3..20) — each is the number a human uses to find
    the transaction in THAT document. Content must agree; provenance
    must not.
    """
    return replace(
        statement,
        transactions=tuple(
            replace(transaction, source_line=0)
            for transaction in statement.transactions
        ),
    )


# ── the golden fixture ─────────────────────────────────────────────────


def test_pdf_parses_to_proven_statement() -> None:
    statement = parse_bank_pdf(SAMPLE_PDF, account_label=ACCOUNT)
    assert statement.account_label == ACCOUNT
    assert statement.period_start == date(2026, 7, 1)
    assert statement.period_end == date(2026, 7, 31)
    assert statement.opening_balance == Money.parse("25,000.00")
    assert statement.closing_balance == Money.parse("33,940.00")
    assert len(statement.transactions) == 18

    first = statement.transactions[0]
    assert first.posted == date(2026, 7, 1)
    assert first.description == "CLIENT TRUST DEPOSIT - ACEVEDO SETTLEMENT"
    assert first.amount == Money.parse("7,500.00")
    # Table row 1 is the header, row 2 the opening balance: data starts at 3.
    assert first.source_line == 3
    assert statement.transactions[-1].source_line == 20


def test_pdf_parentheses_negative_parsed_as_disbursement() -> None:
    statement = parse_bank_pdf(SAMPLE_PDF, account_label=ACCOUNT)
    assert statement.transactions[2].amount == -Money.parse("3,000.00")


# ── the cross-format agreement proof ───────────────────────────────────


def test_pdf_agrees_with_csv_twin_transaction_for_transaction() -> None:
    from_pdf = parse_bank_pdf(SAMPLE_PDF, account_label=ACCOUNT)
    from_csv = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)

    # Row count first: a silently skipped row is THE failure mode this
    # proof exists to catch.
    assert len(from_pdf.transactions) == len(from_csv.transactions)

    for pdf_transaction, csv_transaction in zip(
        from_pdf.transactions, from_csv.transactions, strict=True
    ):
        assert pdf_transaction.posted == csv_transaction.posted
        assert pdf_transaction.description == csv_transaction.description
        assert pdf_transaction.amount == csv_transaction.amount

    # And the whole statements, provenance aside, are one value.
    assert without_provenance(from_pdf) == without_provenance(from_csv)


def test_provenance_points_into_each_physical_document() -> None:
    from_pdf = parse_bank_pdf(SAMPLE_PDF, account_label=ACCOUNT)
    from_csv = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)
    assert [t.source_line for t in from_pdf.transactions] == list(range(3, 21))
    assert [t.source_line for t in from_csv.transactions] == list(range(6, 24))


# ── refusals ───────────────────────────────────────────────────────────


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(BankDocumentError, match="cannot read"):
        parse_bank_pdf(tmp_path / "no_such.pdf", account_label=ACCOUNT)


def test_empty_file_refused(tmp_path: Path) -> None:
    target = tmp_path / "empty.pdf"
    target.write_bytes(b"")
    with pytest.raises(BankDocumentError):
        parse_bank_pdf(target, account_label=ACCOUNT)


def test_non_pdf_bytes_refused(tmp_path: Path) -> None:
    target = tmp_path / "garbage.pdf"
    target.write_bytes(b"this is not a portable document")
    with pytest.raises(BankDocumentError):
        parse_bank_pdf(target, account_label=ACCOUNT)


def test_statement_never_naming_the_account_refused() -> None:
    with pytest.raises(BankDocumentError, match="never mentions"):
        parse_bank_pdf(SAMPLE_PDF, account_label="IOLTA ****9999")


def test_blank_account_label_refused() -> None:
    with pytest.raises(BankDocumentError, match="account_label"):
        parse_bank_pdf(SAMPLE_PDF, account_label=" ")


def test_document_without_transactions_table_refused() -> None:
    # no_table.pdf has the letterhead, account, and period line, but no
    # table — the parser must halt, not return an empty statement.
    with pytest.raises(BankDocumentError, match="no transactions table"):
        parse_bank_pdf(NO_TABLE_PDF, account_label=ACCOUNT)
