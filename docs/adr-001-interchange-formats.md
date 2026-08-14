# ADR-001: Documented interchange formats instead of per-vendor parsers

Status: accepted · 2026-08-14

## Context

Practice-management exports vary by vendor, version, and firm
configuration (Clio, MyCase, PracticePanther all export differently, and
firms customize columns). Bank statements vary by institution. The
engine's verification math, by contrast, is universal.

## Decision

The engine consumes two documented NOCTIX interchange formats — the
bank-statement CSV and the trust-books CSV — plus native bank-statement
PDFs for supported layouts. Transforming a client's raw exports into the
interchange formats is an onboarding step (scripted per client, one
script per source, kept in that client's overlay repo), not engine code.

## Consequences

- The engine's parsers stay deep and stable; client variability lives in
  thin, disposable transform scripts (creed #3, #11).
- A new practice-software vendor costs one transform script, not an
  engine change.
- The interchange documents are part of handover: a firm can see exactly
  what data the engine reads — and produce it themselves if they ever
  leave (the "yours when it's done" promise).
- PDF parsing is the deliberate exception (bank PDFs are the moat
  workload); each supported statement layout earns its own parser with
  its own golden fixtures.
