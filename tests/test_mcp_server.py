from datetime import date, datetime, timezone

import pytest

from ledgerline.categorize import categorize_rules_only
from ledgerline.ingest import ingest_file
from ledgerline.mcp_server import (
    _data_status,
    _search_transactions,
    _spending_summary,
    refresh_data,
    set_account_context,
)
from tests.conftest import FIXTURES


@pytest.fixture
def populated_db(db_file):
    from ledgerline import db

    conn = db.connect(db_file)
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    categorize_rules_only(conn)
    conn.close()
    return db_file


def test_data_status_reports_staleness_and_coverage(populated_db):
    status = _data_status(populated_db, today=date(2026, 6, 11))

    assert status["transactions"] == 8
    assert status["first_transaction_date"] == "2026-01-03"
    assert status["latest_transaction_date"] == "2026-01-21"
    assert status["days_since_latest_transaction"] == 141
    assert status["accounts"][0]["name"] == "US Checking"
    assert any("141 days old" in warning for warning in status["warnings"])


def test_search_transactions_uses_structured_filters(populated_db):
    result = _search_transactions(
        populated_db,
        start_date="2026-01-01",
        end_date="2026-01-31",
        merchant_contains="brass badger",
        direction="outflow",
    )

    assert result["returned"] == 2
    assert {row["amount_cents"] for row in result["transactions"]} == {-450}
    assert all(row["category"] == "dining" for row in result["transactions"])


def test_spending_summary_uses_positive_spend_and_exact_cents(populated_db):
    result = _spending_summary(
        populated_db,
        start_date="2026-01-01",
        end_date="2026-01-31",
        group_by="category",
    )

    assert result["spent_cents"] == 132214
    assert result["income_cents"] == 350000
    assert result["net_cents"] == 217786
    assert sum(row["spent_cents"] for row in result["groups"]) == 132214


def test_invalid_dates_are_rejected(populated_db):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _search_transactions(populated_db, start_date="January")


def test_spending_summary_never_adds_currencies(conn, db_file):
    from ledgerline.ingest import get_or_create_account, insert_transactions
    from ledgerline.ingest.types import ParsedTxn

    usd = get_or_create_account(conn, "USD Checking", currency="USD")
    cad = get_or_create_account(conn, "CAD Chequing", currency="CAD")
    insert_transactions(
        conn, usd, [ParsedTxn("2026-01-10", -10000, "USD MERCHANT")],
        "seed", currency="USD",
    )
    insert_transactions(
        conn, cad, [ParsedTxn("2026-01-10", -20000, "CAD MERCHANT")],
        "seed", currency="CAD",
    )

    result = _spending_summary(
        db_file, start_date="2026-01-01", end_date="2026-01-31"
    )

    assert result["spent_cents"] is None
    assert result["warning"].startswith("Multiple currencies")
    assert {
        row["currency"]: row["spent_cents"] for row in result["currency_totals"]
    } == {"CAD": 20000, "USD": 10000}


def test_refresh_skips_recent_success_without_network(db_file, monkeypatch):
    from ledgerline import db

    conn = db.connect(db_file)
    now = datetime.now(tz=timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('simplefin_last_success', ?)",
        (now,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))

    result = refresh_data(force=False)

    assert result["refreshed"] is False
    assert "less than one hour" in result["reason"]


def test_mcp_account_context_and_purpose_filter(conn, db_file, monkeypatch):
    from ledgerline.ingest import get_or_create_account, insert_transactions
    from ledgerline.ingest.types import ParsedTxn

    business_id = get_or_create_account(conn, "Business Card", currency="CAD")
    personal_id = get_or_create_account(conn, "Personal Card", currency="CAD")
    insert_transactions(
        conn,
        business_id,
        [ParsedTxn("2026-01-10", -10000, "BUSINESS PURCHASE")],
        "seed",
        currency="CAD",
    )
    insert_transactions(
        conn,
        personal_id,
        [ParsedTxn("2026-01-10", -20000, "PERSONAL PURCHASE")],
        "seed",
        currency="CAD",
    )
    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))
    updated = set_account_context(
        "Business Card",
        purpose="business",
        entity_name="Northwind Consulting",
        context_note="Professional expenses",
    )

    result = _spending_summary(
        db_file,
        start_date="2026-01-01",
        end_date="2026-01-31",
        purpose="business",
    )

    assert updated["business_use_percent"] == 100
    assert updated["entity_name"] == "Northwind Consulting"
    assert result["spent_cents"] == 10000
    assert result["purpose_filter"] == "business"
