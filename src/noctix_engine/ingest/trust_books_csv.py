"""Firm trust-books ingest: the NOCTIX trust-books interchange CSV v1.

Every practice-management package exports trust ledgers differently —
Clio, CosmoLex, QuickBooks, a paralegal's spreadsheet. During onboarding
we transform whatever the firm actually has into THIS format, once, by
hand or by a client-specific script; the engine then ingests only the
interchange file. One hostile-document problem per client, solved at the
boundary, and the parser below never grows per-vendor branches (creed #3:
the engine is judged by how thin each client's overlay stays).

The interchange format, v1
--------------------------
A UTF-8 CSV. A leading comment block carries the two numbers the firm's
books must state for a three-way reconciliation, then a fixed header,
then one row per trust movement:

    # NOCTIX trust-books interchange CSV v1
    # as_of=2026-07-31
    # journal_balance=87421.50
    client,date,kind,description,amount
    "Alvarez, Maria",2026-06-05,deposit,Settlement funds received,50000.00
    "Alvarez, Maria",2026-07-10,disbursement,Medical lien payoff,-12000.00

Rules, all enforced here (creed #5 — after ingest, illegal data is
unrepresentable):

- Comment lines start with ``#`` and appear only above the header.
  A comment containing ``=`` is metadata; ``as_of`` (YYYY-MM-DD) and
  ``journal_balance`` are required, each exactly once, and no other key
  exists in v1. Comments without ``=`` are free text and ignored.
- ``kind`` is one of ``deposit``, ``disbursement``, ``fee_transfer`` —
  the values of `TrustEntryKind`, verbatim.
- ``amount`` is signed per domain.py's contract: deposits positive,
  money leaving trust negative. A row whose sign contradicts its kind
  (a negative deposit, a positive disbursement) is refused: whether the
  transcriber flipped the sign or mislabeled the kind is exactly the
  ambiguity creed #6 halts on rather than guesses about.
- Fields never contain line breaks: one physical line is one movement,
  so every refusal can cite an exact line number.
- The file carries each client's ledger FROM INCEPTION (or from an
  opening deposit row), not just the period's movements — otherwise a
  running balance would be an offset, not a balance, and the negative-
  balance check below would be meaningless.

Rows are grouped into per-client ledgers in order of first appearance,
entries kept in file order. A client ledger whose running balance ever
goes negative halts the parse: a negative client trust balance means the
firm disbursed one client's money against another's — the classic
commingling red flag — and it must never pass silently into a
reconciliation that could otherwise total clean.

This parser proves the file's internal shape only. Whether
journal_balance equals the sum of the ledgers — and whether either
matches the bank — is the verify stage's three-way comparison, not a
parse question; `TrustBooks` deliberately carries both numbers unjudged.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from noctix_engine.domain import (
    ClientLedger,
    TrustBooks,
    TrustEntryKind,
    TrustLedgerEntry,
)
from noctix_engine.money import Money, MoneyError

_HEADER = "client,date,kind,description,amount"
_KIND_BY_LABEL = {kind.value: kind for kind in TrustEntryKind}
_METADATA_KEYS = frozenset({"as_of", "journal_balance"})


class TrustBooksError(ValueError):
    """A trust-books interchange file failed to parse or broke an invariant.

    Raised instead of guessing (creed #6). Every message names the line
    and the offending value so the firm can find the row in their own
    export without an engineer in the room.
    """


def parse_trust_books(path: Path) -> TrustBooks:
    """Parse a NOCTIX trust-books interchange CSV v1 file into `TrustBooks`.

    The only public entry point of this module. Raises `TrustBooksError`
    with a client-readable reason on any deviation from the format
    documented in the module docstring; on success the returned value is
    proven — signed per kind, no client ledger ever negative, ledgers in
    first-appearance order with entries in file order.
    """
    try:
        # utf-8-sig: a leading BOM from an Excel save is noise, not meaning.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise TrustBooksError(
            f"cannot read trust-books file {path}: {error}"
        ) from error
    lines = text.splitlines()

    metadata, data_start = _split_at_header(lines, path)
    as_of, journal_balance = _finalize_metadata(metadata, path)

    # Insertion order of these dicts IS the ledger order contract:
    # first appearance in the file, entries in file order.
    entries_by_client: dict[str, list[TrustLedgerEntry]] = {}
    running_by_client: dict[str, Money] = {}
    for line_number, line in enumerate(lines[data_start:], start=data_start + 1):
        stripped = line.strip()
        if not stripped:
            continue  # a trailing or separator blank line carries no meaning
        if stripped.startswith("#"):
            raise TrustBooksError(
                f"line {line_number}: comment below the header row — metadata "
                "lives only in the leading block, so a late comment means the "
                "file was assembled wrong"
            )
        entry = _parse_entry_row(stripped, line_number, as_of)
        balance = running_by_client.get(entry.client, Money.zero()) + entry.amount
        if balance.amount < 0:
            raise TrustBooksError(
                f"client {entry.client!r} trust balance goes negative on "
                f"{entry.entered.isoformat()}: {balance} after line "
                f"{entry.source_line} — a negative client trust balance means "
                "money left trust that this client never had, the classic "
                "commingling red flag; halting for human review"
            )
        running_by_client[entry.client] = balance
        entries_by_client.setdefault(entry.client, []).append(entry)

    ledgers = tuple(
        ClientLedger(client=client, entries=tuple(client_entries))
        for client, client_entries in entries_by_client.items()
    )
    return TrustBooks(as_of=as_of, journal_balance=journal_balance, ledgers=ledgers)


def _split_at_header(lines: list[str], path: Path) -> tuple[dict[str, str], int]:
    """Consume the leading comment block up to the header row.

    Returns the collected metadata and the 0-based index of the first
    line AFTER the header. The header must match `_HEADER` exactly — the
    interchange format is ours, so tolerance would only hide a file that
    was never transformed for us at all.
    """
    metadata: dict[str, str] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            _absorb_metadata_line(stripped, index + 1, metadata)
            continue
        if stripped != _HEADER:
            raise TrustBooksError(
                f"line {index + 1}: expected the v1 header row {_HEADER!r}, "
                f"got {stripped!r} — is this file really a NOCTIX trust-books "
                "interchange export?"
            )
        return metadata, index + 1
    raise TrustBooksError(
        f"{path.name}: no header row found — an empty or truncated file is a "
        "document problem to put in front of a human, not an empty ledger"
    )


def _absorb_metadata_line(
    stripped: str, line_number: int, metadata: dict[str, str]
) -> None:
    """Record one ``# key=value`` line; free-text comments pass through."""
    body = stripped.lstrip("#").strip()
    if "=" not in body:
        return  # e.g. the "# NOCTIX trust-books interchange CSV v1" title line
    key, _, value = body.partition("=")
    key = key.strip()
    value = value.strip()
    if key not in _METADATA_KEYS:
        known = ", ".join(sorted(_METADATA_KEYS))
        raise TrustBooksError(
            f"line {line_number}: unknown metadata key {key!r}; v1 metadata "
            f"is exactly: {known}"
        )
    if key in metadata:
        raise TrustBooksError(
            f"line {line_number}: metadata key {key!r} appears twice — which "
            "value is true is not this engine's guess to make"
        )
    if not value:
        raise TrustBooksError(f"line {line_number}: metadata key {key!r} has no value")
    metadata[key] = value


def _finalize_metadata(metadata: dict[str, str], path: Path) -> tuple[date, Money]:
    """Prove the metadata block: both keys present, both values parse."""
    for key in ("as_of", "journal_balance"):
        if key not in metadata:
            raise TrustBooksError(
                f"{path.name}: metadata block is missing '# {key}=...' — a "
                "three-way reconciliation cannot anchor without it"
            )
    try:
        as_of = date.fromisoformat(metadata["as_of"])
    except ValueError as error:
        raise TrustBooksError(
            f"metadata as_of {metadata['as_of']!r} is not a YYYY-MM-DD date"
        ) from error
    try:
        journal_balance = Money.parse(metadata["journal_balance"])
    except MoneyError as error:
        raise TrustBooksError(f"metadata journal_balance refused: {error}") from error
    return as_of, journal_balance


def _parse_entry_row(line: str, line_number: int, as_of: date) -> TrustLedgerEntry:
    """Prove one movement row. Everything refusable is refused here, with
    the line number, so the grouping loop above handles only proven values."""
    try:
        fields = next(csv.reader([line]))
    except (csv.Error, StopIteration) as error:
        raise TrustBooksError(
            f"line {line_number}: row is not parseable CSV: {error}"
        ) from error
    if len(fields) != 5:
        raise TrustBooksError(
            f"line {line_number}: expected 5 fields ({_HEADER}), got {len(fields)}"
        )
    client, date_raw, kind_raw, description, amount_raw = (
        field.strip() for field in fields
    )

    if not client:
        raise TrustBooksError(
            f"line {line_number}: client is empty — every trust movement "
            "belongs to a named client"
        )
    try:
        entered = date.fromisoformat(date_raw)
    except ValueError as error:
        raise TrustBooksError(
            f"line {line_number}: date {date_raw!r} is not a YYYY-MM-DD date"
        ) from error
    if entered > as_of:
        raise TrustBooksError(
            f"line {line_number}: entry dated {entered.isoformat()} is after "
            f"the books' as_of {as_of.isoformat()} — the export and its own "
            "cutoff disagree"
        )
    kind = _KIND_BY_LABEL.get(kind_raw)
    if kind is None:
        known = ", ".join(sorted(_KIND_BY_LABEL))
        raise TrustBooksError(
            f"line {line_number}: unknown kind {kind_raw!r}; v1 kinds are "
            f"exactly: {known}"
        )
    try:
        amount = Money.parse(amount_raw)
    except MoneyError as error:
        raise TrustBooksError(f"line {line_number}: amount refused: {error}") from error
    _require_sign_matches_kind(kind, amount, line_number)

    return TrustLedgerEntry(
        client=client,
        entered=entered,
        kind=kind,
        description=description,
        amount=amount,
        source_line=line_number,
    )


def _require_sign_matches_kind(
    kind: TrustEntryKind, amount: Money, line_number: int
) -> None:
    """The signing contract, both directions (creed #7: assert the positive
    and the negative space). Deposits are strictly positive; disbursements
    and fee transfers strictly negative; zero says nothing and is refused."""
    if amount.is_zero:
        raise TrustBooksError(
            f"line {line_number}: amount is zero — a movement of nothing is a "
            "question for the firm, not a ledger entry"
        )
    money_left_trust = amount.amount < 0
    if kind is TrustEntryKind.DEPOSIT and money_left_trust:
        raise TrustBooksError(
            f"line {line_number}: deposit with negative amount {amount} — "
            "either the sign or the kind is wrong, and which one is exactly "
            "the guess this engine refuses to make"
        )
    if kind is not TrustEntryKind.DEPOSIT and not money_left_trust:
        raise TrustBooksError(
            f"line {line_number}: {kind.value} with positive amount {amount} "
            "— money leaving trust must be negative; either the sign or the "
            "kind is wrong, and which one is exactly the guess this engine "
            "refuses to make"
        )
