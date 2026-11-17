# Fixtures

Every file in this directory is invented: no real bank, firm, client, matter,
account, or transaction. The HTML and PDF statements say so inside the document;
the trust-books CSVs carry a comment line saying so. The bank-statement CSVs
cannot carry one, because the v1 interchange format is exactly four metadata
lines and a header (see `src/noctix_engine/ingest/bank_csv.py`).

Amounts are chosen so that the books and the bank agree, or disagree by a
known figure that a test asserts. Change a fixture only together with the test
that pins it.
