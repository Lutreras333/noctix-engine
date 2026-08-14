"""Bank-statement PDF ingest: the same proof as the CSV door, read from print.

This door handles a simple single-account statement PDF and nothing
more. The expected shape (anything else halts — creed #6, never skip a
row silently):

- The document text mentions the caller's `account_label` verbatim;
  a statement that never names the account is not attributed to it.
- Exactly one line of the form `Statement period YYYY-MM-DD through
  YYYY-MM-DD` appears; zero or conflicting period lines halt.
- Exactly one bordered table headed Date / Description / Amount /
  Balance exists (one header line). Its first data row is `OPENING
  BALANCE` and its last is `CLOSING BALANCE`, both with the date and
  amount cells blank and the balance cell carrying the figure.
- Every row between them is one transaction: ISO calendar date, a
  non-empty description, a `Money.parse`-able signed amount (accounting
  parentheses mean negative), and the printed running balance.

Cell text is whitespace-normalized with the same rule the CSV door
applies, so the two renderings of one statement parse to equal content.
`source_line` on each transaction is its row number within the printed
table, counting the header line as row 1 — the number a human uses to
find the row on the page.

All arithmetic proofs (duplicate rule, running-balance recomputation,
opening + sum == closing) are performed by `prove_statement`, imported
from `bank_csv`: the interchange module owns the proof, and both doors
run the identical one — a proof that could drift between formats is no
proof at all.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final

import pdfplumber
from pdfplumber.pdf import PDF

from noctix_engine.domain import BankStatement

# Package-internal reuse, deliberate: bank_csv owns the interchange
# format, its error type, and the single arithmetic proof (see its
# docstring). These names are internal to the ingest package — nothing
# outside ingest imports them, and neither door exports them.
from noctix_engine.ingest.bank_csv import (
    BankDocumentError,
    StatementLine,
    iso_calendar_date,
    normalize_text,
    parse_document_amount,
    prove_statement,
)
from noctix_engine.money import Money

_PERIOD_PATTERN: Final = re.compile(
    r"Statement period\s+(\d{4}-\d{2}-\d{2})\s+through\s+(\d{4}-\d{2}-\d{2})"
)
_HEADER_CELLS: Final = ("date", "description", "amount", "balance")
_COLUMN_COUNT: Final = len(_HEADER_CELLS)


def _parse_table_date(cell: str, *, where: str) -> dt.date:
    """ISO-only, same rigidity as the CSV door, with page/row context."""
    try:
        parsed = iso_calendar_date(cell)
    except ValueError as error:
        raise BankDocumentError(f"{where}: date: {error}") from error
    if not isinstance(parsed, dt.date):
        raise BankDocumentError(
            f"{where}: date cell {cell!r} is not an ISO calendar date (expected YYYY-MM-DD)"
        )
    return parsed


def _boundary_balance(row: list[str], *, expected: str, where: str) -> Money:
    """Read the OPENING BALANCE / CLOSING BALANCE row's balance figure."""
    if len(row) != _COLUMN_COUNT:
        raise BankDocumentError(
            f"{where}: expected the {expected} row with {_COLUMN_COUNT} cells, found {len(row)}"
        )
    date_cell, description, amount_cell, balance_cell = row
    if description.upper() != expected:
        raise BankDocumentError(
            f"{where}: expected the {expected} row, found description {description!r} "
            "(this parser requires explicit opening and closing balance rows)"
        )
    if date_cell or amount_cell:
        raise BankDocumentError(
            f"{where}: the {expected} row must leave its date and amount cells blank; "
            f"found date {date_cell!r}, amount {amount_cell!r}"
        )
    return parse_document_amount(balance_cell, field="balance", where=where)


def _parse_open_pdf(
    pdf: PDF, *, account_label: str, document_name: str
) -> BankStatement:
    if not pdf.pages:
        raise BankDocumentError(f"{document_name} contains no pages")

    page_texts: list[str] = []
    # Every table whose header matches, with the page it was found on —
    # collected across all pages so a second table halts instead of
    # being silently ignored or silently merged.
    matching_tables: list[tuple[int, list[list[str]]]] = []
    for page_number, page in enumerate(pdf.pages, start=1):
        page_texts.append(page.extract_text() or "")
        for table in page.extract_tables():
            rows = [[normalize_text(cell or "") for cell in row] for row in table]
            if rows and tuple(cell.lower() for cell in rows[0]) == _HEADER_CELLS:
                matching_tables.append((page_number, rows))
    full_text = "\n".join(page_texts)

    if account_label not in normalize_text(full_text):
        raise BankDocumentError(
            f"{document_name} never mentions account {account_label!r}; refusing to "
            "attribute this statement to an account the document does not name"
        )

    periods = {match.groups() for match in _PERIOD_PATTERN.finditer(full_text)}
    if not periods:
        raise BankDocumentError(
            f"{document_name}: no 'Statement period YYYY-MM-DD through YYYY-MM-DD' "
            "line found; without a stated period this statement cannot be placed in time"
        )
    if len(periods) > 1:
        raise BankDocumentError(
            f"{document_name}: the document states {len(periods)} different statement "
            "periods; refusing to choose between them"
        )
    start_text, end_text = periods.pop()
    # The regex guarantees YYYY-MM-DD shape; fromisoformat still rejects
    # impossible dates like 2026-13-01.
    try:
        period_start = dt.date.fromisoformat(start_text)
        period_end = dt.date.fromisoformat(end_text)
    except ValueError as error:
        raise BankDocumentError(
            f"{document_name}: statement period {start_text} through {end_text} "
            f"names an impossible date: {error}"
        ) from error

    if not matching_tables:
        raise BankDocumentError(
            f"{document_name}: no transactions table found (expected one bordered table "
            "headed Date / Description / Amount / Balance)"
        )
    if len(matching_tables) > 1:
        pages_named = ", ".join(str(number) for number, _ in matching_tables)
        raise BankDocumentError(
            f"{document_name}: found {len(matching_tables)} transaction tables "
            f"(pages {pages_named}); this parser handles single-table statements only"
        )
    table_page, rows = matching_tables[0]

    # Header is row 1; opening and closing rows bracket the transactions.
    if len(rows) < 3:
        raise BankDocumentError(
            f"{document_name}: the transactions table on page {table_page} has no "
            "OPENING BALANCE / CLOSING BALANCE rows"
        )
    opening_balance = _boundary_balance(
        rows[1], expected="OPENING BALANCE", where=f"page {table_page}, table row 2"
    )
    closing_where = f"page {table_page}, table row {len(rows)}"
    closing_balance = _boundary_balance(
        rows[-1], expected="CLOSING BALANCE", where=closing_where
    )

    statement_lines: list[StatementLine] = []
    for row_number, row in enumerate(rows[2:-1], start=3):
        where = f"page {table_page}, table row {row_number}"
        if len(row) != _COLUMN_COUNT:
            raise BankDocumentError(
                f"{where}: expected {_COLUMN_COUNT} cells (Date / Description / Amount / "
                f"Balance), found {len(row)}"
            )
        date_cell, description, amount_cell, balance_cell = row
        if not description:
            raise BankDocumentError(
                f"{where}: description cell is empty; every bank line must say what it was"
            )
        statement_lines.append(
            StatementLine(
                posted=_parse_table_date(date_cell, where=where),
                description=description,
                amount=parse_document_amount(amount_cell, field="amount", where=where),
                printed_balance=parse_document_amount(
                    balance_cell, field="balance", where=where
                ),
                source_line=row_number,
                where=where,
            )
        )

    return prove_statement(
        account_label=account_label,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        lines=tuple(statement_lines),
        closing_context=f"the CLOSING BALANCE row ({closing_where})",
    )


def parse_bank_pdf(path: Path, *, account_label: str) -> BankStatement:
    """Parse one simple single-account statement PDF into a proven BankStatement.

    See the module docstring for the exact shape this door accepts.
    Anything not confidently parsed raises BankDocumentError with page
    and row context; no row is ever skipped silently.
    """
    label = normalize_text(account_label)
    if not label:
        raise BankDocumentError(
            "account_label must name the account (e.g. 'IOLTA ****0000'); got blank"
        )
    try:
        pdf = pdfplumber.open(path)
    except OSError as error:
        raise BankDocumentError(
            f"cannot read bank statement PDF {path}: {error}"
        ) from error
    except Exception as error:
        # pdfminer's exception taxonomy is not a stable API; any failure
        # opening the file means "not a readable PDF", which is all the
        # client needs to know. The cause is preserved for the record.
        raise BankDocumentError(
            f"{path.name} could not be opened as a PDF: {error}"
        ) from error
    try:
        return _parse_open_pdf(pdf, account_label=label, document_name=path.name)
    finally:
        pdf.close()
