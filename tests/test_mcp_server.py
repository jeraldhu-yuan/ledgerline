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

    totals = result["currency_totals"]
    assert len(totals) == 1 and totals[0]["currency"] == "USD"
    assert totals[0]["spent_cents"] == 132214
    assert totals[0]["income_cents"] == 350000
    assert totals[0]["net_cents"] == 217786
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

    # No combined top-level totals exist; every figure is inside its currency.
    assert "spent_cents" not in result
    assert {
        row["currency"]: row["spent_cents"] for row in result["currency_totals"]
    } == {"CAD": 20000, "USD": 10000}


@pytest.mark.parametrize(
    "key", ["simplefin_last_success", "simplefin_last_attempt"]
)
def test_refresh_skips_recent_attempt_without_network(db_file, monkeypatch, key):
    from ledgerline import db

    conn = db.connect(db_file)
    now = datetime.now(tz=timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?)", (key, now)
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))

    result = refresh_data(force=False)

    assert result["refreshed"] is False
    assert "less than one hour" in result["reason"]


def test_data_status_flags_refresh_with_provider_errors(db_file):
    from ledgerline import db

    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES"
        " ('simplefin_last_success', '2026-06-10T00:00:00+00:00'),"
        " ('simplefin_last_attempt', '2026-06-11T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    status = _data_status(db_file, today=date(2026, 6, 11))

    assert status["simplefin_last_attempt"] > status["simplefin_last_success"]
    assert any("provider errors" in warning for warning in status["warnings"])


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
    assert result["currency_totals"] == [
        {
            "currency": "CAD",
            "transaction_count": 1,
            "spent_cents": 10000,
            "spent": "$100.00",
            "income_cents": 0,
            "income": "$0.00",
            "net_cents": -10000,
            "net": "-$100.00",
            "uncategorized_spend_cents": 10000,
            "uncategorized_spend": "$100.00",
        }
    ]


def test_set_merchant_categories_applies_retroactively(populated_db, monkeypatch):
    from ledgerline import db
    from ledgerline.mcp_server import set_merchant_categories

    monkeypatch.setenv("LEDGERLINE_DB", str(populated_db))
    # "Zimberly Office Supply Co" is uncategorized in the fixture
    result = set_merchant_categories(
        {"Zimberly Office Supply Co": "professional", "No Such Merchant": "other"}
    )

    assert result["transactions_recategorized"]["Zimberly Office Supply Co"] == 1
    assert result["merchants_with_zero_matches"] == ["No Such Merchant"]
    conn = db.connect(populated_db)
    row = conn.execute(
        "SELECT category FROM transactions WHERE merchant_clean = 'Zimberly Office Supply Co'"
    ).fetchone()
    cache = conn.execute(
        "SELECT confirmed FROM merchant_category_cache"
        " WHERE merchant_clean = 'Zimberly Office Supply Co'"
    ).fetchone()
    conn.close()
    assert row["category"] == "professional"
    assert cache["confirmed"] == 1


def test_set_merchant_categories_unconfirmed_stays_reviewable(populated_db, monkeypatch):
    from ledgerline import db
    from ledgerline.categorize import unconfirmed
    from ledgerline.mcp_server import set_merchant_categories

    monkeypatch.setenv("LEDGERLINE_DB", str(populated_db))
    set_merchant_categories(
        {"Zimberly Office Supply Co": "shopping"}, confirmed=False
    )

    conn = db.connect(populated_db)
    queued = [r["merchant_clean"] for r in unconfirmed(conn)]
    conn.close()
    assert "Zimberly Office Supply Co" in queued


def test_set_merchant_categories_rejects_off_taxonomy(populated_db, monkeypatch):
    from ledgerline.mcp_server import set_merchant_categories

    monkeypatch.setenv("LEDGERLINE_DB", str(populated_db))
    with pytest.raises(ValueError, match="not in the taxonomy"):
        set_merchant_categories({"Zimberly Office Supply Co": "splurges"})
    with pytest.raises(ValueError, match="must not be empty"):
        set_merchant_categories({})


def test_update_recurring_payment_deactivates(db_file, monkeypatch):
    from ledgerline.mcp_server import (
        add_recurring_payment,
        update_recurring_payment,
        upcoming_payments,
    )

    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))
    add_recurring_payment("Flixburrow.com", 1549, cadence="monthly", expected_day=7)
    assert upcoming_payments(days=45)["payments"]

    result = update_recurring_payment("Flixburrow.com", active=False)

    assert result["active"] is False
    assert result["next_expected_date"] is None
    assert upcoming_payments(days=45)["payments"] == []


def test_update_recurring_payment_changes_amount_and_validates(db_file, monkeypatch):
    from ledgerline.mcp_server import add_recurring_payment, update_recurring_payment

    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))
    add_recurring_payment("Rent", 185000, cadence="monthly", expected_day=1)

    result = update_recurring_payment("Rent", expected_amount_cents=195000)
    assert result["expected_amount_cents"] == -195000
    assert result["active"] is True

    with pytest.raises(ValueError, match="no recurring payment labeled"):
        update_recurring_payment("Not A Thing", active=False)
    with pytest.raises(ValueError, match="nothing to update"):
        update_recurring_payment("Rent")


def test_add_recurring_payment_appears_in_upcoming(db_file, monkeypatch):
    from ledgerline.mcp_server import add_recurring_payment, upcoming_payments

    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))
    result = add_recurring_payment(
        "Course tuition installment", 85000, cadence="monthly", expected_day=21
    )

    # stored as an outflow even though the input was positive
    assert result["expected_amount_cents"] == -85000
    assert result["expected_amount"] == "-$850.00"
    assert result["next_expected_date"] is not None

    payments = upcoming_payments(days=45)
    labels = [p["label"] for p in payments["payments"]]
    assert "Course tuition installment" in labels


def test_add_recurring_payment_links_existing_merchant(db_file, monkeypatch):
    from ledgerline import db
    from ledgerline.ingest import get_or_create_account, insert_transactions
    from ledgerline.ingest.types import ParsedTxn
    from ledgerline.mcp_server import add_recurring_payment

    conn = db.connect(db_file)
    account_id = get_or_create_account(conn, "US Checking")
    insert_transactions(
        conn, account_id,
        [
            ParsedTxn("2026-04-21", -85000, "BRIGHTSTONE TRAINING LLC"),
            ParsedTxn("2026-05-21", -85000, "BRIGHTSTONE TRAINING LLC"),
        ],
        "seed",
    )
    conn.close()
    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))

    result = add_recurring_payment(
        "Brightstone installment", 85000, cadence="monthly",
        expected_day=21, merchant="Brightstone Training Llc",
    )

    assert result["linked_transactions"] == 2
    assert result["account"] == "US Checking"
    assert result["currency"] == "USD"


def test_add_recurring_payment_validates_inputs(db_file, monkeypatch):
    from ledgerline.mcp_server import add_recurring_payment

    monkeypatch.setenv("LEDGERLINE_DB", str(db_file))
    with pytest.raises(ValueError, match="nonzero"):
        add_recurring_payment("X", 0)
    with pytest.raises(ValueError, match="between 1 and 31"):
        add_recurring_payment("X", 1000, expected_day=0)
    with pytest.raises(ValueError, match="unknown account"):
        add_recurring_payment("X", 1000, account="No Such Account")


def test_compare_periods_covers_all_groups(populated_db, monkeypatch):
    from ledgerline.mcp_server import compare_periods

    monkeypatch.setenv("LEDGERLINE_DB", str(populated_db))
    result = compare_periods(
        first_start="2026-01-01",
        first_end="2026-01-31",
        second_start="2026-02-01",
        second_end="2026-02-28",
        group_by="merchant",
    )

    # January had spending, February none: every group change is negative and
    # group changes cover the full January merchant list, not a top-N slice.
    assert result["group_changes_truncated"] is False
    assert len(result["group_changes"]) == len(result["first"]["groups"])
    assert all(c["spending_change_cents"] < 0 for c in result["group_changes"])
    assert result["currency_changes"][0]["spending_change_cents"] == -132214
