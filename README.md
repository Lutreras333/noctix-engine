# NOCTIX ENGINE

[![ci](https://github.com/Lutreras333/noctix-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Lutreras333/noctix-engine/actions/workflows/ci.yml)

The reusable core of every NOCTIX client system: documents arrive → data gets
verified → actions are proposed → a record is kept → a human-readable report
goes out. The First Slice runs this engine read-only against a firm's real
data and proves, to the cent, what their books actually say.

Nothing in this engine can move money. There is no disbursement code, no
signatory capability, and no write path to any ledger. `act/` produces typed
*intents* that a human approves elsewhere; in the First Slice it is dormant
by design. Creed 8 binds that layer the day it is built: no intent becomes
an external write without an idempotency key committed to the run record
first.

## Run it

    uv sync
    uv run pytest                              # 209 tests, 2 golden reports
    uv run ruff check . && uv run ruff format --check .
    uv run python tools/check_money_paths.py   # the float ban, enforced by AST
    uv run pyright                             # strict, zero errors

Python 3.13. Every dependency is pinned in `uv.lock` and justified in
`DEPENDENCIES.md`. The fixtures under `tests/fixtures` are fictional bank
statements and trust books, labeled as such inside the files.

## Status

The First Slice is complete: a read-only three-way trust-account
reconciliation for law firms (bank statement, firm trust journal, sum of
client ledgers, to the cent, as Florida Rule 5-1.2 requires), with
hostile-document ingest for CSV and PDF statements, stale-funds leakage
review, a hash-chained run record, and a client-readable report. `act/`
is declared and dormant. The production billing system NOCTIX runs today
lives in a separate repository; this engine is the reusable core for the
clients that follow it.

## The creed

1. Write for the client's next engineer; the interpreter is the second reader.
2. Simple means unentangled, not short: a money path reads top-to-bottom in
   one place, and no function asks a reader to hold more than about four
   live facts.
3. Modules are deep — interfaces far smaller than implementations — and the
   engine is judged by how thin each client's overlay stays.
4. Money is a Decimal inside a frozen type, born only at a parsing boundary;
   float never touches a ledger path.
5. Parse, don't validate: after ingest, illegal data is unrepresentable.
6. In ambiguity, halt loudly with a client-readable reason — never guess,
   never let an error pass silently, never auto-resolve past the
   verification gate.
7. Assert the positive and the negative space of every invariant; a firing
   assertion is the product working, not the product failing.
8. Every external write carries an idempotency key committed to the run
   record before the call.
9. The append-only run record is the proof: every report claim resolves to a
   timestamped record a stranger — or an auditor — can reperform.
10. Tests are the executable spec: golden documents per client, Hypothesis
    properties on the money math, a regression test for every incident,
    zero tolerated flakes.
11. Duplication is cheaper than the wrong abstraction: the engine absorbs a
    pattern only after it repeats verbatim across clients.
12. Full-word names from one committed glossary; comments carry the why and
    the contract — updated in the same diff or deleted — and no cleverness
    anywhere money moves.

## Layout

    src/noctix_engine/
      money.py     the Money type — the only currency representation
      domain.py    frozen value types shared across stages
      ingest/      boundary parsers: hostile documents in, proven values out
      verify/      reconciliation and leakage analysis over proven values
      act/         typed intents (dormant in the First Slice)
      record/      the append-only, hash-chained run record
      report/      client-readable artifacts from the record
    tools/         build gates, including the float ban
    tests/         the executable spec

## Gates

Every change passes, in order: `ruff check` and `ruff format --check`,
`python tools/check_money_paths.py`, `pyright` (strict, zero errors),
`pytest`. Red means stop and look — never rerun to green.

Lucas reviews every diff that touches `money.py`, `verify/`, or `act/`
before it merges, even when every gate is green.

## License

Source-available for reading and evaluation. All rights reserved; see
`LICENSE`.
