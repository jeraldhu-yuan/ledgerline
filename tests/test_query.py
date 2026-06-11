import sqlite3

import pytest

from ledgerline import db
from ledgerline.ingest import ingest_file
from ledgerline.query import ROW_CAP, month_summary, run_sql
from tests.conftest import FIXTURES


@pytest.fixture
def ro_conn(db_file):
    rw = db.connect(db_file)
    ingest_file(rw, FIXTURES / "us_checking_jan.csv", "US Checking")
    rw.close()
    ro = db.connect_readonly(db_file)
    yield ro
    ro.close()


# --- run_sql hardening (acceptance: hostile inputs rejected) ---

def test_select_works(ro_conn):
    out = run_sql(ro_conn, "SELECT COUNT(*) AS n FROM transactions")
    assert out["columns"] == ["n"]
    assert out["rows"][0][0] == 8
    assert out["truncated"] is False


def test_trailing_semicolon_tolerated(ro_conn):
    out = run_sql(ro_conn, "SELECT 1;")
    assert out["rows"] == [[1]]


def test_with_cte_allowed(ro_conn):
    out = run_sql(
        ro_conn,
        "WITH t AS (SELECT amount_cents FROM transactions) SELECT COUNT(*) FROM t",
    )
    assert out["rows"][0][0] == 8


@pytest.mark.parametrize(
    "hostile",
    [
        "INSERT INTO transactions (id) VALUES (1)",
        "UPDATE transactions SET amount_cents = 0",
        "DELETE FROM transactions",
        "DROP TABLE transactions",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
        "PRAGMA writable_schema = 1",
        "SELECT 1; DROP TABLE transactions",
        "CREATE TABLE pwned (id)",
        "REPLACE INTO accounts (id, name, institution) VALUES (1, 'x', 'y')",
        "",
    ],
)
def test_hostile_inputs_rejected(ro_conn, hostile):
    with pytest.raises(ValueError):
        run_sql(ro_conn, hostile)


def test_row_cap_enforced_server_side(ro_conn):
    out = run_sql(
        ro_conn,
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 500)"
        " SELECT x FROM cnt",
    )
    assert len(out["rows"]) == ROW_CAP
    assert out["truncated"] is True


def test_connection_is_truly_readonly(ro_conn):
    # even if every string check failed, the mode=ro connection refuses writes
    with pytest.raises(sqlite3.OperationalError):
        ro_conn.execute("UPDATE transactions SET amount_cents = 0")


# --- summary ---

def test_month_summary_integer_math(conn):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    s = month_summary(conn, "2026-01")
    assert s["income_cents"] == 350000
    assert s["outflow_cents"] == -(8245 + 450 + 450 + 1549 + 14520 + 22000 + 85000)
    assert s["txn_count"] == 8
    assert isinstance(s["income_cents"], int)


def test_month_summary_deltas_vs_prior_month(conn):
    from ledgerline.categorize import categorize_rules_only
    from ledgerline.ingest import get_or_create_account, insert_transactions
    from ledgerline.ingest.types import ParsedTxn

    account_id = get_or_create_account(conn, "US Checking")
    insert_transactions(conn, account_id, [
        ParsedTxn("2025-12-07", -1549, "NETFLIX.COM"),
        ParsedTxn("2026-01-07", -1549, "NETFLIX.COM"),
        ParsedTxn("2026-01-20", -2000, "NETFLIX.COM"),
    ], "seed")
    categorize_rules_only(conn)
    s = month_summary(conn, "2026-01")
    subs = next(r for r in s["by_category"] if r["category"] == "subscriptions")
    # Jan -35.49 vs Dec -15.49 -> delta -20.00
    assert subs["delta_cents"] == -2000


def test_month_summary_empty_month(conn):
    s = month_summary(conn, "2030-01")
    assert s["txn_count"] == 0
    assert s["income_cents"] == 0
