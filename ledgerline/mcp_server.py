"""Read-only MCP server for querying Ledgerline from AI agents.

The MCP host (Codex, Claude Code, or another compatible client) supplies the
model. Ledgerline supplies deterministic finance tools and never calls a model
from this server.
"""

import os
import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ledgerline import db
from ledgerline.money import format_cents
from ledgerline.query import run_sql
from ledgerline.recurring import upcoming

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MCP_DB = PROJECT_ROOT / "data" / "ledgerline.db"
MAX_TRANSACTIONS = 200

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
LOCAL_REFRESH = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
LOCAL_METADATA = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

mcp = FastMCP(
    "ledgerline",
    instructions=(
        "Ledgerline is the user's local personal-finance database (single user; "
        "bank access is read-only). Amounts are exact integer cents; negative means "
        "money out. Never add amounts across currencies — report each currency "
        "separately. Account metadata (purpose, entity_name, business_use_percent, "
        "analysis_treatment, context_note) is authoritative user context: exclude "
        "monitor_only accounts from spending, income, and cash-flow totals, and ask "
        "rather than guess when an unknown account purpose would change a conclusion. "
        "Check data_status when freshness or coverage matters, and disclose stale, "
        "missing, or uncategorized data that could affect an answer. refresh_data "
        "writes only the local cache; set_account_context and "
        "add_recurring_payment write only local metadata; every other tool is "
        "read-only. Nothing here can touch the bank."
    ),
    json_response=True,
)


def _db_path() -> Path:
    return Path(os.environ.get("LEDGERLINE_DB", DEFAULT_MCP_DB)).expanduser().resolve()


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path else _db_path()
    if not target.exists():
        raise FileNotFoundError(
            f"Ledgerline database not found at {target}. Import or sync transactions first."
        )
    return db.connect_readonly(target)


def _validate_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc
    return value


def _period(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    today = date.today()
    start = _validate_date(start_date, "start_date") or today.replace(day=1).isoformat()
    end = _validate_date(end_date, "end_date") or today.isoformat()
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    return start, end


def _money(cents: int | None) -> str | None:
    return format_cents(cents) if cents is not None else None


def _data_status(path: Path | str | None = None, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    with closing(_connect(path)) as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS transactions, MIN(posted_date) AS first_date,"
            " MAX(posted_date) AS latest_date,"
            " SUM(category IS NULL) AS uncategorized_transactions FROM transactions"
        ).fetchone()
        accounts = [
            {
                "name": row["name"],
                "institution": row["institution"],
                "type": row["type"],
                "currency": row["currency"],
                "purpose": row["purpose"],
                "entity_name": row["entity_name"],
                "business_use_percent": row["business_use_percent"],
                "analysis_treatment": row["analysis_treatment"],
                "context_note": row["context_note"],
                "transactions": row["transactions"],
            }
            for row in conn.execute(
                "SELECT a.name, a.institution, a.type, a.currency, a.purpose,"
                " a.entity_name, a.business_use_percent, a.analysis_treatment,"
                " a.context_note,"
                " COUNT(t.id) transactions"
                " FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id"
                " GROUP BY a.id ORDER BY a.name"
            )
        ]
        mappings = conn.execute(
            "SELECT COUNT(*) FROM simplefin_account_map"
        ).fetchone()[0]
        sync_state = dict(
            conn.execute(
                "SELECT key, value FROM sync_state"
                " WHERE key IN ('simplefin_last_success', 'simplefin_last_attempt')"
            ).fetchall()
        )
        unknown_purpose = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE purpose = 'unknown'"
        ).fetchone()[0]

    latest = totals["latest_date"]
    age_days = (today - date.fromisoformat(latest)).days if latest else None
    warnings = []
    if not latest:
        warnings.append("No transactions are loaded.")
    elif age_days > 7:
        warnings.append(
            f"Latest transaction is {age_days} days old; current balances and recent "
            "spending may be missing."
        )
    uncategorized = totals["uncategorized_transactions"] or 0
    if uncategorized:
        warnings.append(
            f"{uncategorized} transactions are uncategorized; category totals are incomplete."
        )
    if mappings == 0:
        warnings.append("No SimpleFIN account mapping exists; data currently comes from file imports.")
    last_success = sync_state.get("simplefin_last_success")
    last_attempt = sync_state.get("simplefin_last_attempt")
    if not last_success:
        warnings.append("No successful SimpleFIN refresh timestamp is recorded.")
    elif last_attempt and last_attempt > last_success:
        warnings.append(
            "The most recent SimpleFIN refresh reported provider errors; "
            "recent data may be incomplete."
        )
    if unknown_purpose:
        warnings.append(
            f"{unknown_purpose} accounts have unknown personal/business purpose; "
            "purpose-level analysis is incomplete."
        )

    return {
        "as_of": today.isoformat(),
        "database": str(Path(path).resolve() if path else _db_path()),
        "transactions": totals["transactions"],
        "first_transaction_date": totals["first_date"],
        "latest_transaction_date": latest,
        "days_since_latest_transaction": age_days,
        "uncategorized_transactions": uncategorized,
        "simplefin_accounts_mapped": mappings,
        "simplefin_last_success": last_success,
        "simplefin_last_attempt": last_attempt,
        "accounts_with_unknown_purpose": unknown_purpose,
        "accounts": accounts,
        "warnings": warnings,
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def data_status() -> dict[str, Any]:
    """Check finance-data coverage, freshness, accounts, and quality warnings.

    Call this before answering any question about current finances or a period
    whose availability is not already established.
    """
    return _data_status()


def _safe_new_account_label(conn: sqlite3.Connection, display_name: str) -> str:
    base = re.sub(r"\s+\([A-Za-z0-9]{4}\)$", "", display_name).strip() or "Account"
    label = base
    suffix = 2
    while conn.execute("SELECT 1 FROM accounts WHERE name = ?", (label,)).fetchone():
        label = f"{base} {suffix}"
        suffix += 1
    return label


@mcp.tool(annotations=LOCAL_REFRESH, structured_output=True)
def refresh_data(force: bool = False) -> dict[str, Any]:
    """Refresh the local cache from SimpleFIN without any bank-side writes.

    Refresh attempts are rate-limited to once per hour unless force=true,
    protecting the provider quota. New accounts receive sanitized local labels.
    """
    from datetime import datetime, timezone

    from ledgerline.categorize import categorize_rules_only
    from ledgerline.connectors.simplefin import sync as simplefin_sync
    from ledgerline.recurring import detect

    conn = db.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT MAX(value) FROM sync_state"
            " WHERE key IN ('simplefin_last_attempt', 'simplefin_last_success')"
        ).fetchone()
        now = datetime.now(tz=timezone.utc)
        if row[0] and not force:
            last = datetime.fromisoformat(row[0])
            if (now - last).total_seconds() < 3600:
                return {
                    "refreshed": False,
                    "reason": "Last refresh attempt was less than one hour ago.",
                    "last_attempt": row[0],
                }

        def resolver(_sfid: str, name: str) -> str:
            return _safe_new_account_label(conn, name)

        results, provider_errors = simplefin_sync(conn, resolver)
        categorized, unknown = categorize_rules_only(conn)
        recurring = detect(conn)
        coverage = conn.execute(
            "SELECT COUNT(*), MIN(posted_date), MAX(posted_date) FROM transactions"
        ).fetchone()
        return {
            "refreshed": True,
            "new_transactions": sum(result.new for result in results.values()),
            "duplicates": sum(result.duplicates for result in results.values()),
            "provider_messages": provider_errors,
            "rule_categorized_transactions": categorized,
            "uncategorized_merchants": len(unknown),
            "recurring_groups_detected_or_updated": len(recurring),
            "transactions": coverage[0],
            "first_transaction_date": coverage[1],
            "latest_transaction_date": coverage[2],
        }
    finally:
        conn.close()


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def account_overview() -> dict[str, Any]:
    """List accounts with balances and durable personal/business context.

    Balances are point-in-time values from SimpleFIN. Credit balances may be
    negative depending on the institution. Never combine balances across currencies.
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT a.name, a.institution, a.type, a.currency, a.balance_cents,"
            " a.available_balance_cents, a.balance_date, a.purpose, a.entity_name,"
            " a.business_use_percent, a.analysis_treatment, a.context_note,"
            " COUNT(t.id) transaction_count,"
            " MIN(t.posted_date) first_transaction_date,"
            " MAX(t.posted_date) latest_transaction_date"
            " FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id"
            " GROUP BY a.id ORDER BY a.institution, a.name"
        ).fetchall()
    return {
        "accounts": [
            {
                **dict(row),
                "balance": _money(row["balance_cents"]),
                "available_balance": _money(row["available_balance_cents"]),
            }
            for row in rows
        ],
        "warning": "Balances in different currencies are not directly additive.",
    }


@mcp.tool(annotations=LOCAL_METADATA, structured_output=True)
def set_account_context(
    account: str,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    entity_name: str | None = None,
    business_use_percent: int | None = None,
    context_note: str | None = None,
    analysis_treatment: Literal["include", "monitor_only", "exclude"] | None = None,
) -> dict[str, Any]:
    """Set durable local metadata that guides future financial analysis.

    This never changes bank data. For mixed accounts, business_use_percent can
    express an approximate allocation and context_note should explain exceptions.
    """
    from ledgerline.accounts import set_context

    conn = db.connect(_db_path())
    try:
        updated = set_context(
            conn,
            account,
            purpose=purpose,
            entity_name=entity_name,
            business_use_percent=business_use_percent,
            context_note=context_note,
            analysis_treatment=analysis_treatment,
        )
    finally:
        conn.close()
    return {
        "account": updated["name"],
        "purpose": updated["purpose"],
        "entity_name": updated["entity_name"],
        "business_use_percent": updated["business_use_percent"],
        "analysis_treatment": updated["analysis_treatment"],
        "context_note": updated["context_note"],
    }


def _search_transactions(
    path: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
    merchant_contains: str | None = None,
    category: str | None = None,
    currency: str | None = None,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    direction: Literal["all", "outflow", "income"] = "all",
    limit: int = 100,
) -> dict[str, Any]:
    start_date = _validate_date(start_date, "start_date")
    end_date = _validate_date(end_date, "end_date")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if not 1 <= limit <= MAX_TRANSACTIONS:
        raise ValueError(f"limit must be between 1 and {MAX_TRANSACTIONS}")

    clauses = []
    params: list[object] = []
    if start_date:
        clauses.append("t.posted_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("t.posted_date <= ?")
        params.append(end_date)
    if account:
        clauses.append("a.name = ?")
        params.append(account)
    if merchant_contains:
        clauses.append("LOWER(COALESCE(t.merchant_clean, t.merchant_raw)) LIKE ?")
        params.append(f"%{merchant_contains.lower()}%")
    if category:
        if category == "(uncategorized)":
            clauses.append("t.category IS NULL")
        else:
            clauses.append("t.category = ?")
            params.append(category)
    if currency:
        clauses.append("t.currency = ?")
        params.append(currency.upper())
    if purpose:
        clauses.append("a.purpose = ?")
        params.append(purpose)
    if direction == "outflow":
        clauses.append("t.amount_cents < 0")
    elif direction == "income":
        clauses.append("t.amount_cents > 0")
    elif direction != "all":
        raise ValueError("direction must be all, outflow, or income")

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit + 1)
    sql = (
        "SELECT t.posted_date, a.name account, a.purpose account_purpose,"
        " a.entity_name, a.business_use_percent, a.analysis_treatment,"
        " t.amount_cents, t.currency,"
        " t.merchant_raw, t.merchant_clean, t.category, r.label recurring_group"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN recurring_groups r ON r.id = t.recurring_group_id"
        f"{where} ORDER BY t.posted_date DESC, t.id DESC LIMIT ?"
    )
    with closing(_connect(path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    truncated = len(rows) > limit
    return {
        "transactions": [
            {
                **dict(row),
                "amount": _money(row["amount_cents"]),
            }
            for row in rows[:limit]
        ],
        "returned": min(len(rows), limit),
        "truncated": truncated,
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def search_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
    merchant_contains: str | None = None,
    category: str | None = None,
    currency: str | None = None,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    direction: Literal["all", "outflow", "income"] = "all",
    limit: int = 100,
) -> dict[str, Any]:
    """Search exact transactions with optional date, account, merchant, and category filters.

    Dates are inclusive YYYY-MM-DD values. Signed amount_cents is authoritative:
    negative means money out and positive means money in.
    """
    return _search_transactions(
        start_date=start_date,
        end_date=end_date,
        account=account,
        merchant_contains=merchant_contains,
        category=category,
        currency=currency,
        purpose=purpose,
        direction=direction,
        limit=limit,
    )


_GROUP_EXPRESSIONS = {
    "category": "COALESCE(t.category, '(uncategorized)')",
    "merchant": "COALESCE(t.merchant_clean, t.merchant_raw)",
    "account": "a.name",
    "purpose": "a.purpose",
    "month": "SUBSTR(t.posted_date, 1, 7)",
}


def _spending_summary(
    path: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: Literal["category", "merchant", "account", "purpose", "month"] = "category",
    account: str | None = None,
    currency: str | None = None,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    include_monitor_only: bool = False,
    limit: int | None = 50,
) -> dict[str, Any]:
    start, end = _period(start_date, end_date)
    if group_by not in _GROUP_EXPRESSIONS:
        raise ValueError("group_by must be category, merchant, account, purpose, or month")
    if limit is not None and not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    clauses = ["t.posted_date >= ?", "t.posted_date <= ?"]
    params: list[object] = [start, end]
    if account:
        clauses.append("a.name = ?")
        params.append(account)
    if currency:
        clauses.append("t.currency = ?")
        params.append(currency.upper())
    if purpose:
        clauses.append("a.purpose = ?")
        params.append(purpose)
    if not account:
        if include_monitor_only:
            clauses.append("a.analysis_treatment IN ('include', 'monitor_only')")
        else:
            clauses.append("a.analysis_treatment = 'include'")
    where = " AND ".join(clauses)
    group_expr = _GROUP_EXPRESSIONS[group_by]

    with closing(_connect(path)) as conn:
        total_rows = conn.execute(
            "SELECT t.currency, COUNT(*) transaction_count,"
            " COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents END), 0) spent_cents,"
            " COALESCE(SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents END), 0) income_cents,"
            " COALESCE(SUM(t.amount_cents), 0) net_cents,"
            " COALESCE(SUM(CASE WHEN t.amount_cents < 0 AND t.category IS NULL"
            " THEN -t.amount_cents END), 0) uncategorized_spend_cents"
            " FROM transactions t JOIN accounts a ON a.id = t.account_id"
            f" WHERE {where} GROUP BY t.currency ORDER BY t.currency",
            params,
        ).fetchall()
        buckets = conn.execute(
            f"SELECT t.currency, {group_expr} label, COUNT(*) transaction_count,"
            " -SUM(t.amount_cents) spent_cents"
            " FROM transactions t JOIN accounts a ON a.id = t.account_id"
            f" WHERE {where} AND t.amount_cents < 0"
            " GROUP BY t.currency, 2 ORDER BY t.currency, spent_cents DESC LIMIT ?",
            [*params, limit if limit is not None else -1],
        ).fetchall()

    return {
        "period": {"start_date": start, "end_date": end},
        "currency_totals": [
            {
                "currency": row["currency"],
                "transaction_count": row["transaction_count"],
                "spent_cents": row["spent_cents"],
                "spent": _money(row["spent_cents"]),
                "income_cents": row["income_cents"],
                "income": _money(row["income_cents"]),
                "net_cents": row["net_cents"],
                "net": _money(row["net_cents"]),
                "uncategorized_spend_cents": row["uncategorized_spend_cents"],
                "uncategorized_spend": _money(row["uncategorized_spend_cents"]),
            }
            for row in total_rows
        ],
        "group_by": group_by,
        "groups": [
            {
                "currency": row["currency"],
                "label": row["label"],
                "transaction_count": row["transaction_count"],
                "spent_cents": row["spent_cents"],
                "spent": _money(row["spent_cents"]),
            }
            for row in buckets
        ],
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def spending_summary(
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: Literal["category", "merchant", "account", "purpose", "month"] = "category",
    account: str | None = None,
    currency: str | None = None,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    include_monitor_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Summarize exact income, spending, net cash flow, and outflow groups for a period.

    Defaults to the current month through today. All totals are per currency
    and never combined. Spending values are positive magnitudes; net_cents is
    signed. Accounts marked monitor_only are excluded unless requested, and
    uncategorized spending is reported separately.
    """
    return _spending_summary(
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
        account=account,
        currency=currency,
        purpose=purpose,
        include_monitor_only=include_monitor_only,
        limit=limit,
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def compare_periods(
    first_start: str,
    first_end: str,
    second_start: str,
    second_end: str,
    group_by: Literal["category", "merchant", "account", "purpose"] = "category",
    account: str | None = None,
    currency: str | None = None,
    purpose: Literal["personal", "business", "mixed", "unknown"] | None = None,
    include_monitor_only: bool = False,
) -> dict[str, Any]:
    """Compare two inclusive periods using the same exact spending calculations.

    Returns both summaries plus signed per-currency changes where positive
    spending_change_cents means spending increased in the second period.
    Group changes are computed over every group in either period.
    """
    first = _spending_summary(
        start_date=first_start,
        end_date=first_end,
        group_by=group_by,
        account=account,
        currency=currency,
        purpose=purpose,
        include_monitor_only=include_monitor_only,
        limit=None,
    )
    second = _spending_summary(
        start_date=second_start,
        end_date=second_end,
        group_by=group_by,
        account=account,
        currency=currency,
        purpose=purpose,
        include_monitor_only=include_monitor_only,
        limit=None,
    )
    first_groups = {
        (row["currency"], row["label"]): row["spent_cents"] for row in first["groups"]
    }
    second_groups = {
        (row["currency"], row["label"]): row["spent_cents"] for row in second["groups"]
    }
    changes = [
        {
            "currency": key[0],
            "label": key[1],
            "first_spent_cents": first_groups.get(key, 0),
            "second_spent_cents": second_groups.get(key, 0),
            "spending_change_cents": second_groups.get(key, 0) - first_groups.get(key, 0),
            "spending_change": _money(
                second_groups.get(key, 0) - first_groups.get(key, 0)
            ),
        }
        for key in sorted(set(first_groups) | set(second_groups))
    ]
    changes.sort(key=lambda row: abs(row["spending_change_cents"]), reverse=True)
    first_totals = {row["currency"]: row for row in first["currency_totals"]}
    second_totals = {row["currency"]: row for row in second["currency_totals"]}
    currency_changes = []
    for code in sorted(set(first_totals) | set(second_totals)):
        old = first_totals.get(code, {})
        new = second_totals.get(code, {})
        spent_change = new.get("spent_cents", 0) - old.get("spent_cents", 0)
        income_change = new.get("income_cents", 0) - old.get("income_cents", 0)
        currency_changes.append(
            {
                "currency": code,
                "spending_change_cents": spent_change,
                "spending_change": _money(spent_change),
                "income_change_cents": income_change,
                "income_change": _money(income_change),
            }
        )
    return {
        "first": first,
        "second": second,
        "currency_changes": currency_changes,
        "group_changes": changes[:100],
        "group_changes_truncated": len(changes) > 100,
    }


@mcp.tool(annotations=LOCAL_METADATA, structured_output=True)
def add_recurring_payment(
    label: str,
    expected_amount_cents: int,
    cadence: Literal["monthly", "weekly", "annual", "irregular"] = "monthly",
    expected_day: int | None = None,
    merchant: str | None = None,
    account: str | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """Register a known upcoming charge so upcoming_payments warns about it.

    Use when the user mentions a new payment obligation (installment plan,
    new subscription, lease) with fewer than three charges so far — recurring
    detection needs three occurrences, and this covers the gap. Established
    patterns are detected automatically on refresh; do not duplicate them.
    Writes only local metadata, never bank data. expected_amount_cents is
    exact integer cents and is always stored as an outflow regardless of
    sign. expected_day is the day of month (monthly cadence only). merchant,
    when given, links existing matching transactions (merchant_clean) to the
    new group.
    """
    from ledgerline.recurring import add_manual_group

    if expected_amount_cents == 0:
        raise ValueError("expected_amount_cents must be nonzero")
    if expected_day is not None and not 1 <= expected_day <= 31:
        raise ValueError("expected_day must be between 1 and 31")

    conn = db.connect(_db_path())
    try:
        account_id = None
        if account:
            row = conn.execute(
                "SELECT id, currency FROM accounts WHERE name = ?", (account,)
            ).fetchone()
            if not row:
                names = [r["name"] for r in conn.execute("SELECT name FROM accounts ORDER BY name")]
                raise ValueError(
                    f"unknown account {account!r}; existing accounts: "
                    f"{', '.join(names) or '(none)'}"
                )
            account_id = row["id"]
            currency = currency or row["currency"]
        group_id = add_manual_group(
            conn,
            label,
            -abs(expected_amount_cents),
            cadence,
            expected_day=expected_day,
            merchant_clean=merchant,
            account_id=account_id,
            currency=currency.upper() if currency else None,
        )
        group = conn.execute(
            "SELECT r.*, a.name account FROM recurring_groups r"
            " LEFT JOIN accounts a ON a.id = r.account_id WHERE r.id = ?",
            (group_id,),
        ).fetchone()
        linked = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE recurring_group_id = ?",
            (group_id,),
        ).fetchone()[0]
        projected = [e for e in upcoming(conn, days=366) if e["label"] == label]
    finally:
        conn.close()
    return {
        "label": group["label"],
        "expected_amount_cents": group["expected_amount_cents"],
        "expected_amount": _money(group["expected_amount_cents"]),
        "cadence": group["cadence"],
        "expected_day": group["expected_day"],
        "account": group["account"],
        "currency": group["currency"],
        "linked_transactions": linked,
        "next_expected_date": projected[0]["date"] if projected else None,
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def upcoming_payments(days: int = 30) -> dict[str, Any]:
    """List expected charges, including monitor-only account funding risks."""
    if not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    with closing(_connect()) as conn:
        rows = upcoming(conn, days=days)
    payments = []
    monitoring_alerts = []
    for row in rows:
        item = {
            **row,
            "expected_amount": _money(row["expected_amount_cents"]),
            "balance": _money(row["balance_cents"]),
        }
        if row["analysis_treatment"] == "monitor_only":
            required = abs(min(row["expected_amount_cents"], 0))
            balance = row["balance_cents"] or 0
            gap = max(required - balance, 0)
            item["funding_gap_cents"] = gap
            item["funding_gap"] = _money(gap)
            item["monitoring_status"] = "funding_needed" if gap else "funded"
            if gap:
                monitoring_alerts.append(
                    {
                        "account": row["account"],
                        "date": row["date"],
                        "currency": row["currency"],
                        "balance_cents": balance,
                        "balance": _money(balance),
                        "expected_payment_cents": required,
                        "expected_payment": _money(required),
                        "funding_gap_cents": gap,
                        "funding_gap": _money(gap),
                    }
                )
        payments.append(item)
    return {
        "as_of": date.today().isoformat(),
        "through": (date.today() + timedelta(days=days)).isoformat(),
        "payments": payments,
        "monitoring_alerts": monitoring_alerts,
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def raw_sql(sql: str) -> dict[str, Any]:
    """Run one advanced read-only SELECT/WITH query (200-row cap, 5s time limit).

    Use structured tools first. Money is signed integer amount_cents (negative =
    outflow); posted_date is ISO YYYY-MM-DD text. Main tables:
    transactions(id, account_id, posted_date, amount_cents, currency, merchant_raw,
    merchant_clean, category, recurring_group_id, external_id);
    accounts(id, name, institution, type, currency, balance_cents,
    available_balance_cents, balance_date, purpose, entity_name,
    business_use_percent, analysis_treatment, context_note);
    recurring_groups(id, label, expected_amount_cents, cadence, expected_day,
    merchant_clean, account_id, currency, active);
    merchant_category_cache(merchant_clean, category, source, confirmed).
    Writes, PRAGMAs, attachment, and multiple statements are rejected by
    validation, SQLite authorization, and the read-only connection.
    """
    with closing(_connect()) as conn:
        return run_sql(conn, sql)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
