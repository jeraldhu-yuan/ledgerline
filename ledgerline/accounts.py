"""Durable account metadata used to interpret financial activity."""

import sqlite3
from typing import Any

PURPOSES = ("personal", "business", "mixed", "unknown")
ANALYSIS_TREATMENTS = ("include", "monitor_only", "exclude")


def set_context(
    conn: sqlite3.Connection,
    account_name: str,
    *,
    purpose: str | None = None,
    entity_name: str | None = None,
    business_use_percent: int | None = None,
    context_note: str | None = None,
    analysis_treatment: str | None = None,
) -> dict[str, Any]:
    """Update interpretive metadata without changing bank-sourced fields."""
    row = conn.execute("SELECT * FROM accounts WHERE name = ?", (account_name,)).fetchone()
    if not row:
        raise ValueError(f"unknown account: {account_name}")
    if purpose is not None and purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}")
    if business_use_percent is not None and not 0 <= business_use_percent <= 100:
        raise ValueError("business_use_percent must be between 0 and 100")
    if analysis_treatment is not None and analysis_treatment not in ANALYSIS_TREATMENTS:
        raise ValueError(
            f"analysis_treatment must be one of {', '.join(ANALYSIS_TREATMENTS)}"
        )

    updates: dict[str, object | None] = {}
    if purpose is not None:
        updates["purpose"] = purpose
        if business_use_percent is None:
            if purpose == "personal":
                updates["business_use_percent"] = 0
            elif purpose == "business":
                updates["business_use_percent"] = 100
            elif purpose == "unknown":
                updates["business_use_percent"] = None
    if entity_name is not None:
        updates["entity_name"] = entity_name.strip() or None
    if business_use_percent is not None:
        updates["business_use_percent"] = business_use_percent
    if context_note is not None:
        updates["context_note"] = context_note.strip() or None
    if analysis_treatment is not None:
        updates["analysis_treatment"] = analysis_treatment
    if not updates:
        raise ValueError("provide at least one account metadata field to update")

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE accounts SET {assignments} WHERE id = ?",
        [*updates.values(), row["id"]],
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone())
