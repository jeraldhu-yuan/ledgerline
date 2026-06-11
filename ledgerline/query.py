"""Query layer: pure-SQL summaries and the agentic `ask` loop.

The `run_sql` tool the model gets is locked down four ways:
read-only connection (mode=ro URI), single-statement SELECT/WITH only,
keyword denylist (ATTACH/PRAGMA/DDL/DML), and a SQLite authorizer that
denies everything except reads. Row cap enforced server-side.
"""

import json
import re
import sqlite3
from datetime import date

from ledgerline import LedgerlineError, db
from ledgerline.llm import MODEL, require_client

ROW_CAP = 200
MAX_TOOL_CALLS = 8

_FORBIDDEN_RE = re.compile(
    r"\b(ATTACH|DETACH|PRAGMA|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE"
    r"|VACUUM|REINDEX|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT)\b",
    re.I,
)

_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # CTEs
}


def run_sql(conn: sqlite3.Connection, sql: str, row_cap: int = ROW_CAP) -> dict:
    """Execute one SELECT on a read-only connection. Raises ValueError on
    anything that isn't a single plain SELECT/WITH statement."""
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise ValueError("empty statement")
    if ";" in s:
        raise ValueError("multiple statements are not allowed")
    first_word = s.split(None, 1)[0].upper()
    if first_word not in ("SELECT", "WITH"):
        raise ValueError("only SELECT statements are allowed")
    if _FORBIDDEN_RE.search(s):
        raise ValueError("statement contains a forbidden keyword")

    def _authorizer(action, *_args):
        return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY

    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(s)
        columns = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchmany(row_cap + 1)
    finally:
        conn.set_authorizer(None)
    truncated = len(rows) > row_cap
    return {
        "columns": columns,
        "rows": [list(r) for r in rows[:row_cap]],
        "truncated": truncated,
    }


def _prior_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def month_summary(conn: sqlite3.Connection, month: str) -> dict:
    """Income/outflow by category, top merchants, deltas vs prior month.
    Pure SQL + integer cents; works with no API key."""
    like = month + "-%"
    prior_like = _prior_month(month) + "-%"

    totals = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS income,"
        " COALESCE(SUM(CASE WHEN amount_cents < 0 THEN amount_cents END), 0) AS outflow,"
        " COUNT(*) AS n"
        " FROM transactions WHERE posted_date LIKE ?",
        (like,),
    ).fetchone()

    by_category = conn.execute(
        "SELECT COALESCE(category, '(uncategorized)') AS category,"
        " SUM(amount_cents) AS total_cents, COUNT(*) AS n"
        " FROM transactions WHERE posted_date LIKE ?"
        " GROUP BY 1 ORDER BY total_cents",
        (like,),
    ).fetchall()

    prior = dict(
        conn.execute(
            "SELECT COALESCE(category, '(uncategorized)'), SUM(amount_cents)"
            " FROM transactions WHERE posted_date LIKE ? GROUP BY 1",
            (prior_like,),
        ).fetchall()
    )

    top_merchants = conn.execute(
        "SELECT merchant_clean, SUM(amount_cents) AS total_cents, COUNT(*) AS n"
        " FROM transactions WHERE posted_date LIKE ? AND amount_cents < 0"
        " GROUP BY merchant_clean ORDER BY total_cents LIMIT 10",
        (like,),
    ).fetchall()

    return {
        "month": month,
        "income_cents": totals["income"],
        "outflow_cents": totals["outflow"],
        "txn_count": totals["n"],
        "by_category": [
            {
                "category": r["category"],
                "total_cents": r["total_cents"],
                "n": r["n"],
                "delta_cents": r["total_cents"] - prior.get(r["category"], 0),
            }
            for r in by_category
        ],
        "top_merchants": [dict(r) for r in top_merchants],
    }


def _stats_preamble(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    dates = conn.execute(
        "SELECT MIN(posted_date), MAX(posted_date) FROM transactions"
    ).fetchone()
    accounts = [
        f"{r['id']}: {r['name']} ({r['type'] or 'unknown'}, {r['currency']})"
        for r in conn.execute("SELECT id, name, type, currency FROM accounts")
    ]
    return (
        f"Transactions: {n} rows spanning {dates[0]} to {dates[1]}.\n"
        f"Accounts:\n" + "\n".join(accounts)
    )


_RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": (
        "Run a single read-only SELECT statement against the personal-finance "
        "SQLite database and get the resulting rows. Call this whenever the "
        "answer depends on transaction data. Aggregate in SQL (GROUP BY, SUM, "
        f"AVG) rather than paging through raw rows — at most {ROW_CAP} rows "
        "are returned per call. amount_cents is integer cents, negative = "
        "outflow. Use posted_date LIKE 'YYYY-MM-%' for month filters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "One SELECT (or WITH ... SELECT) statement. No semicolons, no writes.",
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}


def ask(question: str, db_file=None, max_tool_calls: int = MAX_TOOL_CALLS) -> str:
    """Agentic SQL loop: the model queries the read-only DB, inspects results,
    iterates, then answers."""
    client = require_client()
    conn = db.connect_readonly(db_file)
    try:
        from ledgerline.categorize import TAXONOMY

        system = (
            "You are a personal-finance analyst with read-only SQL access to the "
            "user's own transaction database (single user; this is their data).\n\n"
            f"Schema:\n{db.schema_ddl(conn)}\n\n"
            f"Category taxonomy: {', '.join(TAXONOMY)}\n\n"
            f"{_stats_preamble(conn)}\n\n"
            f"Today's date: {date.today().isoformat()}\n\n"
            "Query as many times as you need (budget: "
            f"{max_tool_calls} queries), then answer concisely with concrete "
            "numbers. Format money as dollars from amount_cents."
        )
        messages = [{"role": "user", "content": question}]
        calls = 0
        while True:
            kwargs = {}
            if calls >= max_tool_calls:
                kwargs["tool_choice"] = {"type": "none"}
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=system,
                tools=[_RUN_SQL_TOOL],
                messages=messages,
                **kwargs,
            )
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return next(
                    (b.text for b in response.content if b.type == "text"),
                    "(no answer)",
                )
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tu in tool_uses:
                calls += 1
                if calls > max_tool_calls:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": "Query budget exhausted. Answer with what you have.",
                            "is_error": True,
                        }
                    )
                    continue
                try:
                    out = run_sql(conn, tu.input["sql"])
                    content = json.dumps(out)
                    is_error = False
                except (ValueError, sqlite3.Error) as e:
                    content = f"SQL error: {e}"
                    is_error = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
    finally:
        conn.close()


def export_month(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.posted_date, a.name AS account, t.amount_cents, t.currency,"
        " t.merchant_raw, t.merchant_clean, t.category"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE t.posted_date LIKE ? ORDER BY t.posted_date",
        (month + "-%",),
    ).fetchall()
