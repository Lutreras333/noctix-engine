"""Executable spec for the NOCTIX bank-CSV interchange format v1.

The committed fixture (tests/fixtures/sample_statement.csv) is the
golden document; every refusal path gets a small hand-built statement
so the failing line is visible in the test itself. The shared
arithmetic proof (`_prove_statement`) is exercised here for BOTH ingest
doors — the PDF tests prove agreement with this one.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from noctix_engine.domain import BankStatement
from noctix_engine.ingest.bank_csv import BankDocumentError, parse_bank_csv
from noctix_engine.money import Money, MoneyError, total

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES / "sample_statement.csv"
ACCOUNT = "IOLTA ****0000"

# A minimal statement that adds up: opening 100.00, two 25.00 deposits,
# closing 150.00. Error tests mutate one line and name the expectation.
BASE_LINES: tuple[str, ...] = (
    "# period_start=2026-07-01",
    "# period_end=2026-07-31",
    "# opening_balance=100.00",
    "# closing_balance=150.00",
    "date,description,amount,balance",
    "2026-07-02,DEPOSIT A,25.00,125.00",
    "2026-07-03,DEPOSIT B,25.00,150.00",
)


def write_statement(directory: Path, lines: tuple[str, ...] | list[str]) -> Path:
    target = directory / "statement.csv"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def edited(index: int, replacement: str) -> list[str]:
    lines = list(BASE_LINES)
    lines[index] = replacement
    return lines


# ── the golden fixture ─────────────────────────────────────────────────


def test_fixture_parses_to_proven_statement() -> None:
    statement = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)
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
    # Data begins after 4 metadata lines and the header: file line 6.
    assert first.source_line == 6
    assert statement.transactions[-1].source_line == 23


def test_fixture_satisfies_the_internal_sum_proof() -> None:
    statement = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)
    movements = total([transaction.amount for transaction in statement.transactions])
    assert statement.opening_balance + movements == statement.closing_balance


def test_fixture_parentheses_negative_parsed_as_disbursement() -> None:
    statement = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)
    check_1041 = statement.transactions[2]
    assert check_1041.description == "CHECK 1041 - DISBURSEMENT TO ACEVEDO"
    assert check_1041.amount == -Money.parse("3,000.00")


def test_fixture_deposit_reversal_pair_cancels() -> None:
    statement = parse_bank_csv(SAMPLE_CSV, account_label=ACCOUNT)
    deposit = statement.transactions[5]
    reversal = statement.transactions[6]
    assert deposit.description == "MOBILE DEPOSIT - RIVERA RETAINER"
    assert reversal.description == "DEPOSIT REVERSAL - RIVERA ITEM RETURNED"
    assert (deposit.amount + reversal.amount).is_zero


# ── the internal-sum proof and its refusals ────────────────────────────


def test_closing_mismatch_names_difference_and_line(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(3, "# closing_balance=150.01"))
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(path, account_label=ACCOUNT)
    message = str(caught.value)
    assert "does not add up" in message
    assert "$0.01" in message
    assert "metadata line 4" in message


def test_printed_running_balance_mismatch_names_its_line(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(6, "2026-07-03,DEPOSIT B,25.00,151.00"))
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(path, account_label=ACCOUNT)
    message = str(caught.value)
    assert "line 7" in message
    assert "$1.00" in message
    assert "running balance" in message


def test_zero_transactions_is_legal_when_opening_equals_closing(tmp_path: Path) -> None:
    lines = list(BASE_LINES[:5])
    lines[3] = "# closing_balance=100.00"
    statement = parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)
    assert statement.transactions == ()
    assert statement.opening_balance == statement.closing_balance


def test_zero_transactions_with_movement_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, list(BASE_LINES[:5]))
    with pytest.raises(BankDocumentError, match="does not add up"):
        parse_bank_csv(path, account_label=ACCOUNT)


# ── MoneyError propagation and sub-cent refusal ────────────────────────


def test_unparseable_amount_carries_money_error_and_line(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "2026-07-02,DEPOSIT A,25.0.0,125.00"))
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(path, account_label=ACCOUNT)
    assert "line 6" in str(caught.value)
    assert isinstance(caught.value.__cause__, MoneyError)


def test_sub_cent_amount_refused_never_rounded(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "2026-07-02,DEPOSIT A,25.005,125.00"))
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(path, account_label=ACCOUNT)
    assert "sub-cent" in str(caught.value)
    assert isinstance(caught.value.__cause__, MoneyError)


def test_sub_cent_metadata_balance_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(2, "# opening_balance=100.001"))
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(path, account_label=ACCOUNT)
    assert "metadata line 3" in str(caught.value)
    assert "sub-cent" in str(caught.value)


# ── malformed metadata ─────────────────────────────────────────────────


def test_missing_metadata_line_refused(tmp_path: Path) -> None:
    lines = [line for line in BASE_LINES if not line.startswith("# closing_balance")]
    with pytest.raises(BankDocumentError, match="metadata line 4"):
        parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)


def test_reordered_metadata_refused(tmp_path: Path) -> None:
    lines = list(BASE_LINES)
    lines[0], lines[1] = lines[1], lines[0]
    with pytest.raises(BankDocumentError, match="metadata line 1"):
        parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)


def test_impossible_metadata_date_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(0, "# period_start=2026-13-05"))
    with pytest.raises(BankDocumentError, match="metadata line 1"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_non_iso_metadata_date_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(1, "# period_end=07/31/2026"))
    with pytest.raises(BankDocumentError, match="ISO calendar date"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_backwards_period_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(0, "# period_start=2026-08-15"))
    with pytest.raises(BankDocumentError, match="runs backwards"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_wrong_header_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(4, "Date,Description,Amount,Balance"))
    with pytest.raises(BankDocumentError, match="line 5"):
        parse_bank_csv(path, account_label=ACCOUNT)


# ── document shape refusals ────────────────────────────────────────────


def test_empty_file_refused(tmp_path: Path) -> None:
    target = tmp_path / "empty.csv"
    target.write_text("", encoding="utf-8")
    with pytest.raises(BankDocumentError, match="empty"):
        parse_bank_csv(target, account_label=ACCOUNT)


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(BankDocumentError, match="cannot read"):
        parse_bank_csv(tmp_path / "no_such.csv", account_label=ACCOUNT)


def test_blank_line_inside_listing_refused(tmp_path: Path) -> None:
    lines = list(BASE_LINES)
    lines.insert(6, "")
    with pytest.raises(BankDocumentError, match="blank line"):
        parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)


def test_trailing_blank_lines_tolerated(tmp_path: Path) -> None:
    lines = list(BASE_LINES) + ["", ""]
    statement = parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)
    assert len(statement.transactions) == 2


def test_wrong_field_count_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "2026-07-02,DEPOSIT A,25.00"))
    with pytest.raises(BankDocumentError, match="expected 4 fields"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_non_iso_row_date_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "07/02/2026,DEPOSIT A,25.00,125.00"))
    with pytest.raises(BankDocumentError, match="line 6"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_empty_description_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "2026-07-02,,25.00,125.00"))
    with pytest.raises(BankDocumentError, match="description"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_date_outside_period_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, edited(5, "2026-08-02,DEPOSIT A,25.00,125.00"))
    with pytest.raises(BankDocumentError, match="outside the statement period"):
        parse_bank_csv(path, account_label=ACCOUNT)


def test_quoted_description_with_comma_parses(tmp_path: Path) -> None:
    path = write_statement(
        tmp_path, edited(5, '2026-07-02,"SMITH, JONES & CO RETAINER",25.00,125.00')
    )
    statement = parse_bank_csv(path, account_label=ACCOUNT)
    assert statement.transactions[0].description == "SMITH, JONES & CO RETAINER"


def test_blank_account_label_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path, list(BASE_LINES))
    with pytest.raises(BankDocumentError, match="account_label"):
        parse_bank_csv(path, account_label="   ")


# ── the duplicate rule: positive and negative space ────────────────────


def test_exact_adjacent_duplicate_refused(tmp_path: Path) -> None:
    lines = list(BASE_LINES)
    lines[6] = lines[5]  # line 7 repeats line 6 in all four fields
    with pytest.raises(BankDocumentError) as caught:
        parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)
    message = str(caught.value)
    assert "duplicate line" in message
    assert "line 7 repeats line 6" in message


def test_repeated_amount_with_moving_balance_is_legitimate(tmp_path: Path) -> None:
    # Same date, description, and amount twice in a row — two identical
    # wire fees, say. The running balance differs, so this is real
    # activity, not a duplicated line.
    lines = list(BASE_LINES)
    lines[5] = "2026-07-02,DEPOSIT A,25.00,125.00"
    lines[6] = "2026-07-02,DEPOSIT A,25.00,150.00"
    statement = parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)
    assert len(statement.transactions) == 2


def test_identical_non_adjacent_lines_are_legitimate(tmp_path: Path) -> None:
    # Intervening postings can return the balance to a prior value, so a
    # verbatim repeat is only a defect when the lines are adjacent.
    lines = list(BASE_LINES[:5])
    lines[3] = "# closing_balance=125.00"
    lines += [
        "2026-07-02,DEPOSIT A,25.00,125.00",
        "2026-07-02,COUNTER WITHDRAWAL,(25.00),100.00",
        "2026-07-02,DEPOSIT A,25.00,125.00",
    ]
    statement = parse_bank_csv(write_statement(tmp_path, lines), account_label=ACCOUNT)
    assert len(statement.transactions) == 3


# ── the money-math property (creed #10) ────────────────────────────────


@given(
    opening_cents=st.integers(min_value=0, max_value=100_000_00),
    amount_cents=st.lists(
        st.integers(min_value=-10_000_00, max_value=10_000_00), min_size=1, max_size=25
    ),
)
def test_generated_statements_round_trip_exactly(
    tmp_path_factory: pytest.TempPathFactory,
    opening_cents: int,
    amount_cents: list[int],
) -> None:
    """Any statement whose balances chain must parse back to the exact
    cents it was written from — and refuse a closing balance off by one
    cent. (tmp_path_factory is session-scoped, so Hypothesis may reuse
    it across examples without the function-scoped-fixture hazard.)"""
    opening = Money.from_cents(opening_cents)
    amounts = [Money.from_cents(cents) for cents in amount_cents]
    closing = opening + total(amounts)

    lines = [
        "# period_start=2026-07-01",
        "# period_end=2026-07-31",
        f"# opening_balance={opening}",
        f"# closing_balance={closing}",
        "date,description,amount,balance",
    ]
    running = opening
    for index, amount in enumerate(amounts):
        running = running + amount
        # Distinct descriptions keep generated lines from ever tripping
        # the adjacent-duplicate rule; amounts are unconstrained.
        lines.append(f'2026-07-15,"GENERATED TXN {index}","{amount}","{running}"')

    directory = tmp_path_factory.mktemp("generated")
    statement = parse_bank_csv(write_statement(directory, lines), account_label=ACCOUNT)
    assert isinstance(statement, BankStatement)
    assert [transaction.amount for transaction in statement.transactions] == amounts
    assert statement.opening_balance + total(amounts) == statement.closing_balance

    corrupted = list(lines)
    corrupted[3] = f"# closing_balance={closing + Money.from_cents(1)}"
    with pytest.raises(BankDocumentError, match="does not add up"):
        parse_bank_csv(write_statement(directory, corrupted), account_label=ACCOUNT)
