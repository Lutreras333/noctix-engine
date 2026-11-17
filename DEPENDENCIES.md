# Dependencies — every one justified

Doctrine: no dependency Claude could write and test in an afternoon; every
entry carries a written justification and an exact pin (see `uv.lock`).

## Runtime

- **pydantic** — strict-mode parsing at every trust boundary (bank rows,
  export cells, API payloads, config). Writing an equivalent validation
  layer with its error reporting is months, not an afternoon, and its
  ubiquity means any successor engineer already reads it.
- **pdfplumber** — extraction of text and tables from bank-statement PDFs.
  This is the hardest extraction workload; pdfplumber is the reference tool for hostile
  table extraction in Python and irreplaceable in an afternoon.

## Development only (never shipped in a money path)

- **pytest** — the test runner; the ecosystem standard.
- **hypothesis** — property-based testing for the money invariants
  (conservation, allocation, round-trip). Hand-rolled generators would be
  strictly worse.
- **syrupy** — snapshot/golden-file assertions for report artifacts.
- **pyright** — the strict type gate.
- **ruff** — formatter and linter; zero-debate style.
