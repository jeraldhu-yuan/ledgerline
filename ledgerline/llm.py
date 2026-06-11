"""Shared Anthropic client plumbing.

Invariant 6: the key comes from the environment only, and any LLM step fails
loudly when it is missing. Rules-only categorization and `summary` never
import a client.
"""

import os

from ledgerline import LedgerlineError

MODEL = "claude-opus-4-8"


def require_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LedgerlineError(
            "ANTHROPIC_API_KEY is not set. LLM features (categorize, ask) need it; "
            "rules-only categorization, summary, and upcoming work without it."
        )
    import anthropic

    return anthropic.Anthropic()
