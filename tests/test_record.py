"""The executable spec for the run record (creed #9): the record is only
proof if a stranger can catch it lying. These tests play both parts —
the engine appending honestly, and an auditor armed with nothing but raw
sqlite3, reperforming the chain and tampering with the file to watch
verification fail.

Nothing here reads the clock or sleeps; the record owns real time and
these tests never depend on what it says.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from noctix_engine.record import RecordError, RunRecord


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    return tmp_path / "record.sqlite"


@pytest.fixture
def record(record_path: Path) -> Iterator[RunRecord]:
    opened = RunRecord(record_path)
    yield opened
    # Windows keeps an open SQLite file locked; close so tmp_path can be
    # reclaimed and no test leaks a handle into the next.
    opened.close()


def read_status(record_path: Path, run_id: str) -> str:
    """Read a run's terminal status the way an auditor would: raw SQL,
    no engine code in the loop."""
    connection = sqlite3.connect(record_path)
    try:
        row = connection.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "run missing from runs table"
    return str(row[0])


def rewrite_payload(
    record_path: Path, run_id: str, sequence: int, payload_json: str
) -> None:
    """Edit a stored payload behind the record's back — the after-the-fact
    tampering `verify_chain` exists to expose."""
    connection = sqlite3.connect(record_path)
    try:
        connection.execute(
            "UPDATE events SET payload = ? WHERE run_id = ? AND sequence = ?",
            (payload_json, run_id, sequence),
        )
        connection.commit()
    finally:
        connection.close()


# ── the happy path ─────────────────────────────────────────────────────


def test_begin_event_close_happy_path(record: RunRecord, record_path: Path) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    first = handle.event("statement_parsed", {"transactions": 42})
    second = handle.event("reconciled", {"variance": "$0.00"})
    handle.close("completed")

    # Digests are SHA-256 hex — 64 characters, and chained, so distinct.
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert first != second
    assert read_status(record_path, handle.run_id) == "completed"


def test_events_round_trip(record: RunRecord) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    payloads: list[dict[str, object]] = [
        {"kind": "deposit", "amount": "$1,250.00", "source_line": 17},
        {"clients": ["Alvarez", "Boone"], "balanced": True},
        {},
    ]
    digests = [
        handle.event(f"step_{index}", payload) for index, payload in enumerate(payloads)
    ]
    handle.close("completed")

    events = record.events(handle.run_id)
    assert [event.sequence for event in events] == [0, 1, 2]
    assert [event.kind for event in events] == ["step_0", "step_1", "step_2"]
    assert [event.payload for event in events] == payloads
    # The digest event() returned for cross-referencing is the digest an
    # auditor reads back — the same fact from both sides of the API.
    assert [event.digest for event in events] == digests


# ── the chain: reperformance and tampering ─────────────────────────────


def test_verify_chain_true_when_untampered(record: RunRecord) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    for index in range(5):
        handle.event("step", {"index": index})
    handle.close("completed")
    assert record.verify_chain(handle.run_id) is True


def test_tampered_payload_breaks_the_chain(
    record: RunRecord, record_path: Path
) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    handle.event("statement_parsed", {"closing_balance": "$52,614.03"})
    handle.event("reconciled", {"variance": "$0.00"})
    handle.close("completed")
    assert record.verify_chain(handle.run_id), "chain must verify before the attack"

    # The attack an append-only record exists to expose: an edit that
    # makes the books look better than they were.
    rewrite_payload(record_path, handle.run_id, 0, '{"closing_balance":"$99,999.99"}')
    assert record.verify_chain(handle.run_id) is False


def test_payload_key_order_does_not_change_digest(
    record: RunRecord, record_path: Path
) -> None:
    # Digests are computed over a canonical sorted-key JSON form, so the
    # byte order a payload happens to be stored in carries no meaning.
    # Proof through the public surface: rewrite the stored payload with
    # its keys reversed — same object, different bytes — and the chain
    # still verifies. Contrast the tamper test above, where changing a
    # VALUE breaks it.
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    handle.event("reconciled", {"alpha": 1, "beta": 2})
    handle.close("completed")

    rewrite_payload(record_path, handle.run_id, 0, '{"beta":2,"alpha":1}')
    assert record.verify_chain(handle.run_id) is True
    assert record.events(handle.run_id)[0].payload == {"alpha": 1, "beta": 2}


# ── append-only and terminal-status guarantees ─────────────────────────


def test_event_after_close_refused(record: RunRecord) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    handle.close("completed")
    with pytest.raises(RecordError, match="append-only"):
        handle.event("late", {"reason": "must never land"})


def test_close_is_idempotent(record: RunRecord, record_path: Path) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    handle.close("completed")
    handle.close("completed")  # a second close is a no-op...
    handle.close("halted")  # ...even with a different status: the first close wins
    assert read_status(record_path, handle.run_id) == "completed"


@pytest.mark.parametrize("status", ["finished", "ok", "running", ""])
def test_unknown_terminal_status_refused(record: RunRecord, status: str) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    with pytest.raises(RecordError, match="unknown terminal status"):
        handle.close(status)


# ── the context manager: a halt is a product outcome ───────────────────


def test_context_manager_records_halt_on_exception(
    record: RunRecord, record_path: Path
) -> None:
    handle = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    with pytest.raises(RuntimeError, match="ledger drifted"), handle:
        handle.event("statement_parsed", {"transactions": 3})
        raise RuntimeError("ledger drifted by $0.02 — halting for human review")

    # The exception propagated (never swallowed), and the record shows a
    # halt event carrying the client-readable reason — creed #6 made
    # durable.
    events = record.events(handle.run_id)
    assert [event.kind for event in events] == ["statement_parsed", "halt"]
    assert events[-1].payload == {
        "reason": "ledger drifted by $0.02 — halting for human review",
        "exception_type": "RuntimeError",
    }
    assert read_status(record_path, handle.run_id) == "halted"
    # The halt is part of the chain, not an annotation outside it.
    assert record.verify_chain(handle.run_id) is True


def test_context_manager_completes_on_clean_exit(
    record: RunRecord, record_path: Path
) -> None:
    with record.begin(client_label="Acme Family Law", engine_version="0.1.0") as handle:
        handle.event("statement_parsed", {"transactions": 3})

    assert read_status(record_path, handle.run_id) == "completed"
    # No halt event on the clean path — the negative space of the halt
    # guarantee (creed #7).
    assert [event.kind for event in record.events(handle.run_id)] == [
        "statement_parsed"
    ]


# ── many runs, one file ────────────────────────────────────────────────


def test_two_runs_in_one_file_are_independent_chains(
    record: RunRecord, record_path: Path
) -> None:
    first = record.begin(client_label="Acme Family Law", engine_version="0.1.0")
    second = record.begin(client_label="Boone Estates", engine_version="0.1.0")
    # Interleave appends to prove the chains share no state through the
    # file: each run starts at sequence 0 from its own genesis digest.
    first.event("step", {"run": "first", "index": 0})
    second.event("step", {"run": "second", "index": 0})
    first.event("step", {"run": "first", "index": 1})
    first.close("completed")
    second.close("halted")

    first_events = record.events(first.run_id)
    second_events = record.events(second.run_id)
    assert [event.sequence for event in first_events] == [0, 1]
    assert [event.sequence for event in second_events] == [0]
    assert all(event.payload["run"] == "first" for event in first_events)
    assert all(event.payload["run"] == "second" for event in second_events)
    assert record.verify_chain(first.run_id) is True
    assert record.verify_chain(second.run_id) is True

    # Tampering one run's history must not cast doubt on the other's.
    rewrite_payload(record_path, first.run_id, 1, '{"run":"first","index":99}')
    assert record.verify_chain(first.run_id) is False
    assert record.verify_chain(second.run_id) is True


# ── the chain commits its own identity and length ──────────────────────


def delete_events_from(record_path: Path, run_id: str, sequence: int) -> None:
    """Truncate a chain's tail behind the record's back."""
    connection = sqlite3.connect(record_path)
    try:
        connection.execute(
            "DELETE FROM events WHERE run_id = ? AND sequence >= ?",
            (run_id, sequence),
        )
        connection.commit()
    finally:
        connection.close()


def test_truncated_tail_breaks_verification(
    record: RunRecord, record_path: Path
) -> None:
    handle = record.begin(client_label="Firm", engine_version="0.1.0")
    handle.event("ingest", {"documents": 2})
    handle.event("report", {"digest": "abc"})
    handle.close("completed")
    delete_events_from(record_path, handle.run_id, 1)
    assert record.verify_chain(handle.run_id) is False


def test_emptied_chain_breaks_verification(
    record: RunRecord, record_path: Path
) -> None:
    handle = record.begin(client_label="Firm", engine_version="0.1.0")
    handle.event("ingest", {"documents": 2})
    handle.close("completed")
    delete_events_from(record_path, handle.run_id, 0)
    assert record.verify_chain(handle.run_id) is False


def test_relabeled_events_verify_for_neither_run(
    record: RunRecord, record_path: Path
) -> None:
    # The attack: delete run B's events and re-point run A's validly
    # chained block at B's id. Genesis is derived from run_id, so the
    # relabeled chain verifies for neither run.
    first = record.begin(client_label="Firm", engine_version="0.1.0")
    first.event("ingest", {"documents": 1})
    first.close("completed")
    second = record.begin(client_label="Firm", engine_version="0.1.0")
    second.event("ingest", {"documents": 9})
    second.close("completed")
    connection = sqlite3.connect(record_path)
    try:
        connection.execute("DELETE FROM events WHERE run_id = ?", (second.run_id,))
        connection.execute(
            "UPDATE events SET run_id = ? WHERE run_id = ?",
            (second.run_id, first.run_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert record.verify_chain(second.run_id) is False
    assert record.verify_chain(first.run_id) is False


def test_verify_chain_of_unknown_run_is_an_error_not_a_verdict(
    record: RunRecord,
) -> None:
    with pytest.raises(RecordError, match="does not exist"):
        record.verify_chain("never-began")


def test_begun_run_with_no_events_verifies_true(record: RunRecord) -> None:
    # Zero events is a real state (begin, then crash before any event).
    # True here honestly means "empty and untampered": the committed
    # count is zero and the committed head is this run's genesis.
    handle = record.begin(client_label="Firm", engine_version="0.1.0")
    assert record.verify_chain(handle.run_id) is True
