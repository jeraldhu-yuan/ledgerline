"""Per-bank CSV column mappings.

Adding a bank is a small dict: column names, date format, sign convention
(some banks export debits as positive), header rows to skip, and optionally
a column carrying a bank-side unique id.
"""

PROFILES: dict[str, dict] = {
    # Example: typical US big-bank checking export (debits already negative)
    "us_checking": {
        "columns": {"date": "Posting Date", "amount": "Amount", "description": "Description"},
        "date_format": "%m/%d/%Y",
        "sign": 1,
        "skip_rows": 0,
        "external_id_column": None,
    },
    # Example: credit card export where charges are positive (sign flipped)
    "generic_visa": {
        "columns": {"date": "Transaction Date", "amount": "Transaction Amount", "description": "Description 1"},
        "date_format": "%Y-%m-%d",
        "sign": -1,
        "skip_rows": 0,
        "external_id_column": None,
    },
}


def detect_profile(header: list[str]) -> str | None:
    """Pick the profile whose mapped columns all appear in the CSV header."""
    matches = [
        name
        for name, p in PROFILES.items()
        if set(p["columns"].values()) <= set(h.strip() for h in header)
    ]
    return matches[0] if len(matches) == 1 else None
