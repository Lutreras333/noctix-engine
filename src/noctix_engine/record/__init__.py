"""The append-only run record: the engine's proof of what it did.

Creed #9: every report claim resolves to a timestamped record a stranger
can reperform. This module is that record — SQLite, with an append-only
events table (no UPDATE or DELETE ever touches it) where each event is
hash-chained to the previous one, so any after-the-fact edit breaks the
chain visibly. The runs table is the mutable index beside it: status,
timestamps, and the chain-head commitment.

Three properties make the chain worth an auditor's trust:

- Each digest covers the run_id (via a per-run genesis) and the event's
  sequence number, so one run's events cannot be relabeled as another's
  and a renumbered tail cannot verify.
- Every append also commits the current chain head and event count to
  the run's row, so a truncated chain — even truncated to nothing —
  fails verification instead of passing vacuously.
- verify_chain() recomputes everything from raw rows; asking about a
  run that does not exist is a caller error (RecordError), never True.

The public surface is deliberately narrow: open a record, begin a run,
append events, close the run, read a run back. There is no other API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    client_label  TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    started_utc   TEXT NOT NULL,
    finished_utc  TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'completed', 'halted')),
    head_digest   TEXT NOT NULL,
    event_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    run_id   TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL,
    at_utc   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL,
    digest   TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
"""


def _genesis_digest(run_id: str) -> str:
    """The chain's starting digest, derived from the run itself.

    A shared constant genesis would let one run's validly-chained events
    be re-pointed at another run's id and still verify; deriving genesis
    from run_id ties every chain to the one run it describes.
    """
    return hashlib.sha256(f"noctix-run-genesis\n{run_id}".encode()).hexdigest()


class RecordError(RuntimeError):
    """The run record refused an operation that would weaken its guarantees."""


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """One event read back from the record, digest included."""

    sequence: int
    at_utc: str
    kind: str
    payload: dict[str, object]
    digest: str


def _canonical(payload: dict[str, object]) -> str:
    """Stable JSON so the digest of a payload is reproducible anywhere."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _chain_digest(
    previous_digest: str, sequence: int, at_utc: str, kind: str, payload_json: str
) -> str:
    # The sequence is part of the material so a renumbered or reordered
    # tail cannot produce the same digests.
    material = f"{previous_digest}\n{sequence}\n{at_utc}\n{kind}\n{payload_json}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class RunHandle:
    """An open run accepting events. Use as a context manager: an
    unhandled exception closes the run as 'halted' with the reason
    recorded — a halt is a product outcome, never a silent death."""

    def __init__(self, connection: sqlite3.Connection, run_id: str) -> None:
        self._connection = connection
        self.run_id = run_id
        self._sequence = 0
        self._previous_digest = _genesis_digest(run_id)
        self._closed = False

    def event(self, kind: str, payload: dict[str, object]) -> str:
        """Append one event; returns its digest for cross-referencing."""
        if self._closed:
            raise RecordError("run is closed; the record is append-only and final")
        at_utc = datetime.now(UTC).isoformat()
        payload_json = _canonical(payload)
        digest = _chain_digest(
            self._previous_digest, self._sequence, at_utc, kind, payload_json
        )
        self._connection.execute(
            "INSERT INTO events (run_id, sequence, at_utc, kind, payload, digest)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.run_id, self._sequence, at_utc, kind, payload_json, digest),
        )
        # Commit the chain head and length to the run's row in the same
        # transaction: without this, a truncated events table would still
        # verify against whatever prefix survived.
        self._connection.execute(
            "UPDATE runs SET head_digest = ?, event_count = ? WHERE run_id = ?",
            (digest, self._sequence + 1, self.run_id),
        )
        self._connection.commit()
        self._sequence += 1
        self._previous_digest = digest
        return digest

    def close(self, status: str) -> None:
        if status not in ("completed", "halted"):
            raise RecordError(f"unknown terminal status {status!r}")
        if self._closed:
            return
        self._connection.execute(
            "UPDATE runs SET status = ?, finished_utc = ? WHERE run_id = ?",
            (status, datetime.now(UTC).isoformat(), self.run_id),
        )
        self._connection.commit()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exception is not None and not self._closed:
            self.event(
                "halt",
                {
                    "reason": str(exception),
                    "exception_type": type(exception).__name__,
                },
            )
            self.close("halted")
        elif not self._closed:
            self.close("completed")


class RunRecord:
    """The record file for one client. One SQLite file, many runs."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.executescript(_SCHEMA)

    def begin(self, *, client_label: str, engine_version: str) -> RunHandle:
        run_id = uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO runs (run_id, client_label, engine_version,"
            " started_utc, head_digest, event_count) VALUES (?, ?, ?, ?, ?, 0)",
            (
                run_id,
                client_label,
                engine_version,
                datetime.now(UTC).isoformat(),
                _genesis_digest(run_id),
            ),
        )
        self._connection.commit()
        return RunHandle(self._connection, run_id)

    def events(self, run_id: str) -> tuple[RecordedEvent, ...]:
        rows = self._connection.execute(
            "SELECT sequence, at_utc, kind, payload, digest FROM events"
            " WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return tuple(
            RecordedEvent(
                sequence=row[0],
                at_utc=row[1],
                kind=row[2],
                payload=json.loads(row[3]),
                digest=row[4],
            )
            for row in rows
        )

    def verify_chain(self, run_id: str) -> bool:
        """Recompute every digest from this run's genesis; True iff untampered.

        This is the reperformance an auditor does without trusting us.
        False means the chain does not hold: an edited event, a truncated
        or emptied chain, a renumbered tail, or events relabeled from
        another run. Asking about a run this record never held is a
        caller error, not a verification result — that raises RecordError.
        """
        row = self._connection.execute(
            "SELECT head_digest, event_count FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RecordError(
                f"run {run_id!r} does not exist in this record; "
                "a missing run cannot verify"
            )
        stored_head, stored_count = row

        previous = _genesis_digest(run_id)
        events = self.events(run_id)
        for event in events:
            expected = _chain_digest(
                previous,
                event.sequence,
                event.at_utc,
                event.kind,
                _canonical(event.payload),
            )
            if expected != event.digest:
                return False
            previous = event.digest
        return previous == stored_head and len(events) == stored_count

    def close(self) -> None:
        self._connection.close()
