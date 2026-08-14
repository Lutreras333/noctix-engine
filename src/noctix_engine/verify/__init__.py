"""Verify: the three-way reconciliation and the stale-funds review.

Read-only by construction — nothing in this package can touch a bank.
A discrepancy is a product outcome with evidence, never an error; the
package halts (VerificationError) only when the documents themselves
make verification impossible.
"""

from noctix_engine.verify.leakage import LeakageFinding, find_stale_client_funds
from noctix_engine.verify.three_way import VerificationError, reconcile

__all__ = [
    "LeakageFinding",
    "VerificationError",
    "find_stale_client_funds",
    "reconcile",
]
