"""NOCTIX bank-CSV interchange format v1: the door where a bank's story
becomes proven values.

Banks export chaos; the engine refuses to ingest chaos. A client overlay
converts whatever its bank emits into THIS format, and `parse_bank_csv`
is the only reader. Per creed #5, everything past this door is
unrepresentable if illegal: a `BankStatement` cannot exist unless the
document internally adds up to the cent.

The format, in full (this docstring is the spec — creed #12):

    # period_start=2026-07-01
    # period_end=2026-07-31
    # opening_balance=25,000.00
    # closing_balance=33,940.00
    date,description,amount,balance
    2026-07-01,CLIENT TRUST DEPOSIT - ACEVEDO SETTLEMENT,"7,500.00","32,500.00"

- UTF-8 text. Exactly four metadata comment lines come first, in exactly
  the order shown, each `# key=value`. Reordering is an error: v1 is
  rigid on purpose, so a malformed export halts instead of half-parsing.
- Dates are ISO calendar dates, `YYYY-MM-DD` and nothing else (no week
  dates, no compact form). Transaction dates must fall inside the
  metadata period.
- Amounts and balances are any string `Money.parse` accepts, including
  accounting parentheses for negatives: `(45.00)` means -45.00.
  Sub-cent precision is refused, never rounded (creed #4/#6).
- The header row is exactly `date,description,amount,balance`.
- Every later line is one transaction: `amount` is the signed movement
  (deposits positive, disbursements negative), `balance` is the printed
  running balance AFTER the transaction. Descriptions are normalized to
  single-spaced text and must be non-empty; blank lines inside the
  listing are errors (trailing blank lines at end-of-file are tolerated).

Proofs performed before a `BankStatement` may exist:

1. Duplicate rule: two ADJACENT lines identical in all four fields are
   rejected. Banks legitimately repeat a date, description, and amount
   (two identical wire fees the same day), but each posting moves the
   running balance — an exact adjacent repeat, printed balance included,
   is the same line printed twice, a document defect to show a human.
   Identical non-adjacent lines are legal: intervening postings can
   return the balance to a prior value.
2. Every printed running balance is recomputed from the opening balance
   and must match to the cent, line by line.
3. opening + sum(transactions) == closing, against the metadata.

Any failure raises `BankDocumentError` naming the line and the
difference to the cent — written for the firm's principal, not for us.
"""

from __future__ import annotations

import csv
import datetime as dt
import itertools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    ValidationError,
    field_validator,
)

from noctix_engine.domain import BankStatement, BankTransaction
from noctix_engine.money import Money, MoneyError, total

_METADATA_KEYS: Final = (
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
)
_HEADER: Final = "date,description,amount,balance"
_HEADER_LINE_NUMBER: Final = len(_METADATA_KEYS) + 1
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class BankDocumentError(ValueError):
    """A bank document refused to parse or failed its internal proof.

    Raised instead of guessing (creed #6). Every message names the
    document location and, for arithmetic failures, the difference to
    the cent — readable by the firm's principal in a run record.
    """


def normalize_text(text: str) -> str:
    """Collapse all whitespace runs to single spaces and trim.

    The CSV and PDF renderings of one statement must normalize
    identically — this is the shared normalization both doors apply to
    descriptions, so the cross-format equality proof compares content,
    not typography.
    """
    return " ".join(text.split())


def iso_calendar_date(value: object) -> object:
    """BeforeValidator: v1 speaks `YYYY-MM-DD` only — no week dates, no
    compact form, nothing `date.fromisoformat` merely tolerates."""
    if isinstance(value, str):
        candidate = value.strip()
        if not _ISO_DATE.fullmatch(candidate):
            raise ValueError(
                f"{value!r} is not an ISO calendar date (expected YYYY-MM-DD)"
            )
        try:
            return dt.date.fromisoformat(candidate)
        except ValueError as error:
            raise ValueError(
                f"{value!r} is not a real calendar date: {error}"
            ) from error
    return value


class _TransactionRow(BaseModel):
    """Shape proof for one data row, before any money parsing.

    Pydantic strict mode is the runtime seal here, exactly as
    `_require_decimal` is for Money: typed callers never trip it; a
    malformed document is who it exists for. Amount and balance stay
    text at this stage — Money.parse is the only amount parser in the
    engine, and it runs with line context in `parse_bank_csv`.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    posted: Annotated[dt.date, BeforeValidator(iso_calendar_date)]
    description: str
    amount_text: str
    balance_text: str

    @field_validator("description")
    @classmethod
    def _description_present(cls, value: str) -> str:
        normalized = normalize_text(value)
        if not normalized:
            raise ValueError(
                "description is empty; every bank line must say what it was"
            )
        return normalized


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One parsed-but-not-yet-proven transaction line.

    Shared by the CSV and PDF doors so `prove_statement` runs the SAME
    proof over both: `where` is the human-readable location for error
    messages ("line 7" / "page 1, table row 5"); `source_line` is the
    number stored on the resulting BankTransaction.
    """

    posted: dt.date
    description: str
    amount: Money
    printed_balance: Money
    source_line: int
    where: str


def parse_document_amount(raw: str, *, field: str, where: str) -> Money:
    """Money.parse with document context: a MoneyError alone cannot name
    the line, so the door wraps it — cause preserved for the record."""
    try:
        return Money.parse(raw)
    except MoneyError as error:
        raise BankDocumentError(f"{where}: {field} {raw!r} refused: {error}") from error


def prove_statement(
    *,
    account_label: str,
    period_start: dt.date,
    period_end: dt.date,
    opening_balance: Money,
    closing_balance: Money,
    lines: tuple[StatementLine, ...],
    closing_context: str,
) -> BankStatement:
    """Run every internal proof; only a statement that adds up returns.

    This function is the single arithmetic gate for BOTH ingest doors
    (CSV and PDF import it): one proof, no drift between formats.
    """
    if period_end < period_start:
        raise BankDocumentError(
            f"statement period runs backwards: period_start {period_start.isoformat()} "
            f"is after period_end {period_end.isoformat()}"
        )

    # Proof 1 — the duplicate rule (see module docstring for the why).
    for previous, current in itertools.pairwise(lines):
        if (
            previous.posted,
            previous.description,
            previous.amount,
            previous.printed_balance,
        ) == (
            current.posted,
            current.description,
            current.amount,
            current.printed_balance,
        ):
            raise BankDocumentError(
                f"duplicate line: {current.where} repeats {previous.where} exactly "
                f"({current.posted.isoformat()}, {current.description!r}, "
                f"{current.amount}, balance {current.printed_balance}). Banks legitimately "
                "repeat a date, description, and amount, but each posting moves the running "
                "balance; an exact adjacent repeat with the same printed balance is the same "
                "line printed twice. Correct the export before ingest."
            )

    for line in lines:
        if not period_start <= line.posted <= period_end:
            raise BankDocumentError(
                f"{line.where}: transaction date {line.posted.isoformat()} falls outside "
                f"the statement period {period_start.isoformat()} to {period_end.isoformat()}"
            )

    # Proof 2 — every printed running balance recomputed from opening.
    running = opening_balance
    for line in lines:
        running = running + line.amount
        if running != line.printed_balance:
            difference = line.printed_balance - running
            raise BankDocumentError(
                f"{line.where}: printed balance {line.printed_balance} disagrees with the "
                f"computed running balance {running} (opening balance plus every transaction "
                f"through this line); the document is off by {difference} here"
            )

    # Proof 3 — opening + sum(transactions) == closing.
    if running != closing_balance:
        difference = closing_balance - running
        raise BankDocumentError(
            f"statement does not add up: opening balance {opening_balance} plus "
            f"{len(lines)} transaction(s) totals {running}, but {closing_context} "
            f"says {closing_balance} — off by {difference}"
        )

    transactions = tuple(
        BankTransaction(
            posted=line.posted,
            description=line.description,
            amount=line.amount,
            source_line=line.source_line,
        )
        for line in lines
    )
    # Creed #7: restate the theorem just proven, independently of the
    # running loop above — a firing assertion here is a bug in this door.
    assert (
        opening_balance + total([transaction.amount for transaction in transactions])
        == closing_balance
    ), (
        "internal-sum proof passed line-by-line but failed in total — bug in prove_statement"
    )
    return BankStatement(
        account_label=account_label,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transactions=transactions,
    )


def _parse_metadata(
    lines: list[str],
) -> tuple[dt.date, dt.date, Money, Money]:
    """The four required `# key=value` lines, in fixed order."""
    if len(lines) < _HEADER_LINE_NUMBER:
        raise BankDocumentError(
            f"file ends before the format is established: expected {len(_METADATA_KEYS)} "
            f"metadata lines and the header '{_HEADER}', found {len(lines)} line(s)"
        )
    values: dict[str, str] = {}
    for offset, key in enumerate(_METADATA_KEYS):
        line_number = offset + 1
        line = lines[offset].strip()
        prefix = f"# {key}="
        if not line.startswith(prefix):
            raise BankDocumentError(
                f"metadata line {line_number}: expected '{prefix}...', found {line!r} "
                "(the four metadata lines are required, in this exact order: "
                + ", ".join(_METADATA_KEYS)
                + ")"
            )
        values[key] = line[len(prefix) :].strip()

    def parse_metadata_date(key: str, line_number: int) -> dt.date:
        try:
            parsed = iso_calendar_date(values[key])
        except ValueError as error:
            raise BankDocumentError(
                f"metadata line {line_number}: {key}: {error}"
            ) from error
        if not isinstance(parsed, dt.date):
            raise BankDocumentError(
                f"metadata line {line_number}: {key} {values[key]!r} is not an ISO "
                "calendar date (expected YYYY-MM-DD)"
            )
        return parsed

    period_start = parse_metadata_date("period_start", 1)
    period_end = parse_metadata_date("period_end", 2)
    opening_balance = parse_document_amount(
        values["opening_balance"], field="opening_balance", where="metadata line 3"
    )
    closing_balance = parse_document_amount(
        values["closing_balance"], field="closing_balance", where="metadata line 4"
    )
    return period_start, period_end, opening_balance, closing_balance


def parse_bank_csv(path: Path, *, account_label: str) -> BankStatement:
    """Parse one NOCTIX bank-CSV interchange file into a proven BankStatement.

    `account_label` is the caller's name for the account (never a full
    account number — see BankStatement). Anything malformed, ambiguous,
    or arithmetically inconsistent raises BankDocumentError naming the
    line; nothing is skipped, rounded, or guessed.
    """
    label = normalize_text(account_label)
    if not label:
        raise BankDocumentError(
            "account_label must name the account (e.g. 'IOLTA ****0000'); got blank"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BankDocumentError(f"cannot read bank CSV {path}: {error}") from error
    if not text.strip():
        raise BankDocumentError(
            f"{path.name} is empty where a bank statement was expected"
        )

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    period_start, period_end, opening_balance, closing_balance = _parse_metadata(lines)

    header = lines[_HEADER_LINE_NUMBER - 1].strip()
    if header != _HEADER:
        raise BankDocumentError(
            f"line {_HEADER_LINE_NUMBER}: expected the header '{_HEADER}', found {header!r}"
        )

    statement_lines: list[StatementLine] = []
    for line_number, raw_line in enumerate(
        lines[_HEADER_LINE_NUMBER:], start=_HEADER_LINE_NUMBER + 1
    ):
        where = f"line {line_number}"
        if not raw_line.strip():
            raise BankDocumentError(
                f"{where}: blank line inside the transaction listing "
                "(only trailing blank lines at end-of-file are tolerated)"
            )
        try:
            row = next(csv.reader([raw_line]))
        except csv.Error as error:
            raise BankDocumentError(f"{where}: unreadable as CSV: {error}") from error
        if len(row) != len(_HEADER.split(",")):
            raise BankDocumentError(
                f"{where}: expected 4 fields ({_HEADER}), found {len(row)}"
            )
        try:
            shaped = _TransactionRow.model_validate(
                {
                    "posted": row[0],
                    "description": row[1],
                    "amount_text": row[2],
                    "balance_text": row[3],
                }
            )
        except ValidationError as error:
            first = error.errors()[0]
            field_name = str(first["loc"][0]) if first["loc"] else "row"
            raise BankDocumentError(f"{where}: {field_name}: {first['msg']}") from error
        statement_lines.append(
            StatementLine(
                posted=shaped.posted,
                description=shaped.description,
                amount=parse_document_amount(
                    shaped.amount_text, field="amount", where=where
                ),
                printed_balance=parse_document_amount(
                    shaped.balance_text, field="balance", where=where
                ),
                source_line=line_number,
                where=where,
            )
        )

    return prove_statement(
        account_label=label,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        lines=tuple(statement_lines),
        closing_context="closing_balance on metadata line 4",
    )
