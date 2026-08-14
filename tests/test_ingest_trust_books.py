"""Executable spec for the trust-books interchange CSV v1 parser.

Every public behavior of `parse_trust_books` is pinned here, error paths
included — each refusal message is asserted for the client-readable
facts it must carry (line, client, date, amount), because those messages
land in run records read by non-programmers.

`sample_trust_books.csv` is the coordinated fixture: a fictional July
2026 where books and bank AGREE. Its journal_balance and ledgers total
are both 87,421.50 — the bank fixture's closing balance. (The bank
agent's fixture was not yet committed when this was written; 87,421.50
is the agreed closing balance from the assignment.)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from noctix_engine.domain import TrustEntryKind
from noctix_engine.ingest.trust_books_csv import TrustBooksError, parse_trust_books
from noctix_engine.money import Money

FIXTURES = Path(__file__).parent / "fixtures"

# A minimal valid preamble for the inline cases below; rows are appended
# per test so each case reads as exactly one deviation from valid.
PREAMBLE = dedent(
    """\
    # NOCTIX trust-books interchange CSV v1
    # as_of=2026-07-31
    # journal_balance=100.00
    client,date,kind,description,amount
    """
)


def write_books(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "books.csv"
    path.write_text(content, encoding="utf-8")
    return path


# ── the happy path: the coordinated sample month ───────────────────────


def test_sample_fixture_parses_to_agreeing_books() -> None:
    books = parse_trust_books(FIXTURES / "sample_trust_books.csv")

    assert books.as_of == date(2026, 7, 31)
    assert books.journal_balance == Money.parse("87421.50")
    # The fixture is the agree-month: journal, ledgers total, and the
    # bank fixture's closing balance are all the same number.
    assert books.ledgers_total() == books.journal_balance

    # Ledgers in first-appearance order, per-client balances exact.
    assert [ledger.client for ledger in books.ledgers] == [
        "Alvarez, Maria — 2024-CP-0119",
        "Brennan Holdings LLC",
        "Chen, Robert — Estate",
    ]
    balances = {ledger.client: ledger.balance() for ledger in books.ledgers}
    assert balances["Alvarez, Maria — 2024-CP-0119"] == Money.parse("38000.00")
    assert balances["Brennan Holdings LLC"] == Money.parse("27421.50")
    assert balances["Chen, Robert — Estate"] == Money.parse("22000.00")


def test_sample_fixture_entries_carry_proven_fields() -> None:
    books = parse_trust_books(FIXTURES / "sample_trust_books.csv")
    alvarez = books.ledgers[0]

    assert len(alvarez.entries) == 2
    deposit, disbursement = alvarez.entries
    assert deposit.kind is TrustEntryKind.DEPOSIT
    assert deposit.entered == date(2026, 6, 5)
    assert deposit.amount == Money.parse("50000.00")
    assert deposit.description == "Settlement funds received"
    assert deposit.source_line == 5  # first row after 3 comments + header
    assert disbursement.kind is TrustEntryKind.DISBURSEMENT
    assert disbursement.amount == Money.parse("-12000.00")
    assert disbursement.source_line == 8

    brennan = books.ledgers[1]
    assert brennan.entries[1].kind is TrustEntryKind.FEE_TRANSFER


def test_single_client_file() -> None:
    books = parse_trust_books(FIXTURES / "single_client_trust_books.csv")
    assert books.journal_balance == Money.parse("1250.00")
    assert len(books.ledgers) == 1
    (ledger,) = books.ledgers
    assert ledger.client == "Dawes, Elena"
    assert ledger.balance() == Money.parse("1250.00")
    assert books.ledgers_total() == books.journal_balance


def test_quoted_commas_and_blank_lines_tolerated(tmp_path: Path) -> None:
    # A quoted client and description may contain commas; blank separator
    # lines carry no meaning and are skipped.
    path = write_books(
        tmp_path,
        PREAMBLE + '"Ford, Alia",2026-07-01,deposit,"Retainer, initial",100.00\n\n',
    )
    books = parse_trust_books(path)
    (ledger,) = books.ledgers
    assert ledger.client == "Ford, Alia"
    assert ledger.entries[0].description == "Retainer, initial"


def test_header_with_no_rows_is_an_empty_books(tmp_path: Path) -> None:
    # Zero movements is representable — a quiet month, or a brand-new
    # trust account. Whether journal_balance=100.00 squares with zero
    # ledgers is the verify stage's three-way question, not a parse error.
    books = parse_trust_books(write_books(tmp_path, PREAMBLE))
    assert books.ledgers == ()
    assert books.journal_balance == Money.parse("100.00")
    assert books.ledgers_total() == Money.zero()


# ── the negative-balance halt: the commingling red flag ────────────────


def test_running_balance_going_negative_halts(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        PREAMBLE
        + "Grant Estate,2026-07-01,deposit,Opening deposit,100.00\n"
        + "Grant Estate,2026-07-10,disbursement,Overdrawn payout,-150.00\n",
    )
    with pytest.raises(TrustBooksError) as caught:
        parse_trust_books(path)
    message = str(caught.value)
    assert "Grant Estate" in message
    assert "2026-07-10" in message
    assert "-$50.00" in message


def test_negative_dip_halts_even_if_later_deposits_would_cover(
    tmp_path: Path,
) -> None:
    # Negative space of the invariant: a ledger that ends positive but
    # dipped negative mid-period still spent someone else's money.
    path = write_books(
        tmp_path,
        PREAMBLE
        + "Grant Estate,2026-07-01,deposit,Opening deposit,100.00\n"
        + "Grant Estate,2026-07-10,disbursement,Early payout,-150.00\n"
        + "Grant Estate,2026-07-11,deposit,Covering deposit,-999.00\n",
    )
    with pytest.raises(TrustBooksError, match="goes negative"):
        parse_trust_books(path)


def test_balance_touching_exactly_zero_is_allowed(tmp_path: Path) -> None:
    # Positive space: fully paying a client out to the cent is normal.
    path = write_books(
        tmp_path,
        PREAMBLE
        + "Grant Estate,2026-07-01,deposit,Opening deposit,100.00\n"
        + "Grant Estate,2026-07-10,disbursement,Final payout,-100.00\n",
    )
    books = parse_trust_books(path)
    assert books.ledgers[0].balance() == Money.zero()


# ── sign-versus-kind disagreement halts ────────────────────────────────


def test_deposit_with_negative_amount_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-07-01,deposit,Backwards deposit,-50.00\n"
    )
    with pytest.raises(TrustBooksError, match="line 5: deposit with negative"):
        parse_trust_books(path)


def test_disbursement_with_positive_amount_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        PREAMBLE + "Hale LLC,2026-07-01,disbursement,Backwards payout,50.00\n",
    )
    with pytest.raises(TrustBooksError, match="disbursement with positive"):
        parse_trust_books(path)


def test_fee_transfer_with_positive_amount_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        PREAMBLE + "Hale LLC,2026-07-01,fee_transfer,Backwards fees,50.00\n",
    )
    with pytest.raises(TrustBooksError, match="fee_transfer with positive"):
        parse_trust_books(path)


def test_zero_amount_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-07-01,deposit,Nothing moved,0.00\n"
    )
    with pytest.raises(TrustBooksError, match="line 5: amount is zero"):
        parse_trust_books(path)


# ── malformed rows halt with the line named ────────────────────────────


def test_unknown_kind_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-07-01,withdrawal,Cash out,-50.00\n"
    )
    with pytest.raises(TrustBooksError, match="unknown kind 'withdrawal'") as caught:
        parse_trust_books(path)
    # The refusal teaches the valid vocabulary.
    assert "deposit, disbursement, fee_transfer" in str(caught.value)


def test_wrong_field_count_refused(tmp_path: Path) -> None:
    path = write_books(tmp_path, PREAMBLE + "Hale LLC,2026-07-01,deposit,50.00\n")
    with pytest.raises(TrustBooksError, match="expected 5 fields"):
        parse_trust_books(path)


def test_empty_client_refused(tmp_path: Path) -> None:
    path = write_books(tmp_path, PREAMBLE + ",2026-07-01,deposit,Orphan money,50.00\n")
    with pytest.raises(TrustBooksError, match="client is empty"):
        parse_trust_books(path)


def test_unparseable_row_date_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,07/01/2026,deposit,US-style date,50.00\n"
    )
    with pytest.raises(TrustBooksError, match="'07/01/2026' is not a YYYY-MM-DD"):
        parse_trust_books(path)


def test_entry_dated_after_as_of_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-08-01,deposit,From the future,50.00\n"
    )
    with pytest.raises(TrustBooksError, match="after the books' as_of 2026-07-31"):
        parse_trust_books(path)


def test_unparseable_amount_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-07-01,deposit,Word amount,fifty\n"
    )
    with pytest.raises(TrustBooksError, match="line 5: amount refused"):
        parse_trust_books(path)


def test_sub_cent_amount_refused(tmp_path: Path) -> None:
    # Money's refusal to round surfaces with the line number attached.
    path = write_books(
        tmp_path, PREAMBLE + "Hale LLC,2026-07-01,deposit,Sub-cent,50.005\n"
    )
    with pytest.raises(TrustBooksError, match="sub-cent"):
        parse_trust_books(path)


def test_comment_below_header_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        PREAMBLE + "Hale LLC,2026-07-01,deposit,Fine row,50.00\n" + "# stray comment\n",
    )
    with pytest.raises(TrustBooksError, match="line 6: comment below the header"):
        parse_trust_books(path)


# ── metadata errors halt before any row is read ────────────────────────


def test_missing_as_of_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# journal_balance=100.00\nclient,date,kind,description,amount\n",
    )
    with pytest.raises(TrustBooksError, match="missing '# as_of=...'"):
        parse_trust_books(path)


def test_missing_journal_balance_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path, "# as_of=2026-07-31\nclient,date,kind,description,amount\n"
    )
    with pytest.raises(TrustBooksError, match="missing '# journal_balance=...'"):
        parse_trust_books(path)


def test_unparseable_as_of_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# as_of=July 2026\n# journal_balance=100.00\n"
        "client,date,kind,description,amount\n",
    )
    with pytest.raises(TrustBooksError, match="'July 2026' is not a YYYY-MM-DD"):
        parse_trust_books(path)


def test_unparseable_journal_balance_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# as_of=2026-07-31\n# journal_balance=about 100\n"
        "client,date,kind,description,amount\n",
    )
    with pytest.raises(TrustBooksError, match="metadata journal_balance refused"):
        parse_trust_books(path)


def test_duplicate_metadata_key_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# as_of=2026-07-31\n# as_of=2026-06-30\n# journal_balance=100.00\n"
        "client,date,kind,description,amount\n",
    )
    with pytest.raises(TrustBooksError, match="'as_of' appears twice"):
        parse_trust_books(path)


def test_unknown_metadata_key_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# bank_balance=100.00\n" + PREAMBLE,
    )
    with pytest.raises(TrustBooksError, match="unknown metadata key 'bank_balance'"):
        parse_trust_books(path)


def test_metadata_key_without_value_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# as_of=\n# journal_balance=100.00\nclient,date,kind,description,amount\n",
    )
    with pytest.raises(TrustBooksError, match="'as_of' has no value"):
        parse_trust_books(path)


# ── file-level problems ────────────────────────────────────────────────


def test_empty_file_refused(tmp_path: Path) -> None:
    path = write_books(tmp_path, "")
    with pytest.raises(TrustBooksError, match="no header row found"):
        parse_trust_books(path)


def test_wrong_header_refused(tmp_path: Path) -> None:
    path = write_books(
        tmp_path,
        "# as_of=2026-07-31\n# journal_balance=100.00\nmatter,date,type,memo,value\n",
    )
    with pytest.raises(TrustBooksError, match="expected the v1 header row"):
        parse_trust_books(path)


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(TrustBooksError, match="cannot read trust-books file"):
        parse_trust_books(tmp_path / "does_not_exist.csv")
