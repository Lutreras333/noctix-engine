# Glossary — the committed names

Creed #12: full-word names from one committed glossary. If a concept
needs a name, it gets it here first; code follows the glossary, never
the other way around.

- **statement** — the bank's account of a period: `BankStatement`.
- **books** — the firm's trust records for a date: `TrustBooks`
  (journal balance + per-client ledgers).
- **journal** — the firm's single running trust-account total.
- **ledger** — one client's individual trust history: `ClientLedger`.
- **three-way** — the Florida Rule 5-1.2 comparison: bank balance vs
  journal balance vs sum of client ledgers, same date, to the cent.
- **finding** — one human-readable piece of evidence in a discrepancy
  or leakage report. Findings present candidates; they never assert
  causes.
- **halt** — the engine stopping loudly with a client-readable reason.
  A halt is the product working (creed #6), recorded as an event and a
  terminal run status.
- **run** — one execution of the engine for one client, recorded
  append-only.
- **record** — the hash-chained SQLite file of runs and events; the
  proof surface (creed #9).
- **slice** — the First Slice: the read-only proof engagement; also the
  orchestrator module that performs it.
- **intent** — a typed, recorded proposal to act externally. In the
  First Slice, intents are declared and never executed.
- **interchange format** — the documented NOCTIX CSV shapes that client
  exports are transformed into during onboarding (see ADR-001).
- **leakage** — money or work sitting where it shouldn't: stale client
  trust funds (v1), unbilled time and aged receivables (later phases).
- **stale funds** — a client trust balance with no activity beyond the
  configured threshold; a candidate for disbursement or billing review.
