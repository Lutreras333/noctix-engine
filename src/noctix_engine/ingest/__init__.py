"""Ingest: the document doors. Each parser proves its document's
arithmetic before returning a value (creed #5: parse, don't validate).

These exports are the package's whole public surface. Everything else
in the modules below (StatementLine, prove_statement, ...) is shared
internals between the two bank doors and must stay unexported.
"""

from noctix_engine.ingest.bank_csv import BankDocumentError, parse_bank_csv
from noctix_engine.ingest.bank_pdf import parse_bank_pdf
from noctix_engine.ingest.trust_books_csv import TrustBooksError, parse_trust_books

__all__ = [
    "BankDocumentError",
    "TrustBooksError",
    "parse_bank_csv",
    "parse_bank_pdf",
    "parse_trust_books",
]
