"""ledgerline — local-first personal finance tracker."""

__version__ = "0.3.2"


class LedgerlineError(Exception):
    """User-facing error: print message and exit nonzero, no traceback."""
