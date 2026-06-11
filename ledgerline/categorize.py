"""Categorization pipeline: cache -> static rules -> batched LLM fallback.

Each unique merchant costs at most one LLM call ever (the cache). The LLM
sees merchant_clean strings only — no amounts, dates, or account info.
"""

import json
import re
import sqlite3

from ledgerline.llm import MODEL, require_client

TAXONOMY = [
    "housing", "utilities", "groceries", "dining", "transport", "health",
    "fitness", "insurance", "subscriptions",
    "professional",  # CE courses, licensing, credentialing fees
    "travel", "shopping", "entertainment", "income", "transfers", "fees",
    "taxes", "other",
]

# Obvious cases resolved in code; everything here is written to the cache as
# source='rule' so the LLM never sees these merchants.
_RULES: list[tuple[str, str]] = [
    (r"kroger|safeway|trader joe|whole foods|aldi|publix|wegmans|h-?e-?b|food lion", "groceries"),
    (r"starbucks|mcdonald|chipotle|chick-?fil|restaurant|pizza|cafe|coffee|bakery|doordash|uber eats|grubhub|taqueria|sushi", "dining"),
    (r"uber(?! eats)|lyft|shell|chevron|exxon|marathon petro|parking|marta|mta |transit|toll", "transport"),
    (r"airbnb|hotel|marriott|hilton|hyatt|expedia|delta air|united airlines|american airlines|southwest air", "travel"),
    (r"netflix|spotify|hulu|disney\+|hbo|youtube premium|audible|apple\.com/bill|icloud", "subscriptions"),
    (r"comcast|xfinity|verizon|at&t|t-mobile|georgia power|duke energy|water dept|gas company|electric", "utilities"),
    (r"planet fitness|la fitness|equinox|crossfit|peloton|ymca|\bgym\b", "fitness"),
    (r"geico|state farm|progressive ins|allstate|insurance", "insurance"),
    (r"cvs|walgreens|pharmacy|dental|orthodont|medical|clinic|hospital|labcorp|quest diagnostics", "health"),
    (r"payroll|direct dep|salary|adp wage", "income"),
    (r"zelle|venmo|wire transfer|online transfer|\btransfer\b", "transfers"),
    (r"overdraft|service charge|annual fee|late fee|atm fee|interest charge|foreign transaction", "fees"),
    (r"\birs\b|us treasury|tax payment|dept of revenue", "taxes"),
    (r"rent|mortgage|property mgmt|hoa dues", "housing"),
    (r"amazon|amzn|target|walmart|best buy|ikea|etsy|ebay", "shopping"),
    (r"amc theat|cinema|ticketmaster|steam games|nintendo|playstation", "entertainment"),
]
RULES = [(re.compile(p, re.I), cat) for p, cat in _RULES]


def rule_category(merchant_clean: str) -> str | None:
    for rx, cat in RULES:
        if rx.search(merchant_clean):
            return cat
    return None


def apply_cache(conn: sqlite3.Connection) -> int:
    """Fill in category on transactions from the merchant cache."""
    cur = conn.execute(
        "UPDATE transactions SET category ="
        " (SELECT category FROM merchant_category_cache c"
        "   WHERE c.merchant_clean = transactions.merchant_clean)"
        " WHERE category IS NULL AND merchant_clean IN"
        " (SELECT merchant_clean FROM merchant_category_cache)"
    )
    conn.commit()
    return cur.rowcount


def _cache_put(conn: sqlite3.Connection, merchant: str, category: str, source: str,
               confirmed: int = 0) -> None:
    conn.execute(
        "INSERT INTO merchant_category_cache (merchant_clean, category, source, confirmed)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(merchant_clean) DO UPDATE SET"
        " category = excluded.category, source = excluded.source,"
        " confirmed = excluded.confirmed",
        (merchant, category, source, confirmed),
    )


def categorize_rules_only(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Steps 1+2 of the pipeline (no API key needed).

    Returns (transactions categorized, merchants still uncached).
    """
    applied = apply_cache(conn)
    uncached = [
        r["merchant_clean"]
        for r in conn.execute(
            "SELECT DISTINCT merchant_clean FROM transactions"
            " WHERE category IS NULL AND merchant_clean IS NOT NULL"
            " ORDER BY merchant_clean"
        )
    ]
    still_unknown = []
    for m in uncached:
        cat = rule_category(m)
        if cat:
            _cache_put(conn, m, cat, "rule")
        else:
            still_unknown.append(m)
    conn.commit()
    applied += apply_cache(conn)
    return applied, still_unknown


def categorize_llm(conn: sqlite3.Connection, merchants: list[str]) -> int:
    """Step 3: ONE batched request for all uncached merchants of an import.

    The model sees merchant names only. Every returned category is validated
    against the taxonomy; anything outside it (and any merchant the model
    skipped) is cached as 'other'.
    """
    if not merchants:
        return 0
    client = require_client()
    schema = {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "category": {"type": "string", "enum": TAXONOMY},
                    },
                    "required": ["merchant", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assignments"],
        "additionalProperties": False,
    }
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=(
            "You categorize personal-finance merchant names into a fixed taxonomy. "
            "Assign every merchant in the list exactly one category. "
            "Use 'other' when genuinely unsure."
        ),
        messages=[
            {
                "role": "user",
                "content": "Categorize these merchants:\n"
                + json.dumps(merchants, ensure_ascii=False),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    assignments = json.loads(text)["assignments"]

    wanted = set(merchants)
    resolved: dict[str, str] = {}
    for a in assignments:
        # Belt and braces: the schema enum already constrains category, but
        # invariant says validate in code and reject anything off-taxonomy.
        if a["merchant"] in wanted and a["category"] in TAXONOMY:
            resolved[a["merchant"]] = a["category"]
    for m in merchants:
        _cache_put(conn, m, resolved.get(m, "other"), "llm")
    conn.commit()
    return apply_cache(conn)


def set_manual(conn: sqlite3.Connection, merchant_clean: str, category: str) -> int:
    """Manual correction: cache as confirmed and retroactively recategorize
    ALL matching transactions, not just uncategorized ones."""
    if category not in TAXONOMY:
        raise ValueError(f"{category!r} is not in the taxonomy")
    _cache_put(conn, merchant_clean, category, "manual", confirmed=1)
    cur = conn.execute(
        "UPDATE transactions SET category = ? WHERE merchant_clean = ?",
        (category, merchant_clean),
    )
    conn.commit()
    return cur.rowcount


def confirm(conn: sqlite3.Connection, merchant_clean: str) -> None:
    conn.execute(
        "UPDATE merchant_category_cache SET confirmed = 1 WHERE merchant_clean = ?",
        (merchant_clean,),
    )
    conn.commit()


def unconfirmed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """LLM-assigned first (most likely to need correction), then rules."""
    return conn.execute(
        "SELECT c.merchant_clean, c.category, c.source, COUNT(t.id) AS txn_count"
        " FROM merchant_category_cache c"
        " LEFT JOIN transactions t ON t.merchant_clean = c.merchant_clean"
        " WHERE c.confirmed = 0"
        " GROUP BY c.merchant_clean"
        " ORDER BY c.source = 'llm' DESC, txn_count DESC"
    ).fetchall()
