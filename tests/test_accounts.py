import pytest

from ledgerline.accounts import set_context
from ledgerline.ingest import get_or_create_account


def test_set_business_context_defaults_to_full_business_use(conn):
    get_or_create_account(conn, "Business Card")

    account = set_context(
        conn,
        "Business Card",
        purpose="business",
        entity_name="Northwind Consulting",
        context_note="Professional expenses only",
    )

    assert account["purpose"] == "business"
    assert account["entity_name"] == "Northwind Consulting"
    assert account["business_use_percent"] == 100
    assert account["context_note"] == "Professional expenses only"


def test_set_mixed_context_accepts_percentage(conn):
    get_or_create_account(conn, "Mixed Chequing")

    account = set_context(
        conn, "Mixed Chequing", purpose="mixed", business_use_percent=70
    )

    assert account["purpose"] == "mixed"
    assert account["business_use_percent"] == 70


def test_set_context_validates_account_and_percentage(conn):
    with pytest.raises(ValueError, match="unknown account"):
        set_context(conn, "Missing", purpose="personal")

    get_or_create_account(conn, "Checking")
    with pytest.raises(ValueError, match="between 0 and 100"):
        set_context(conn, "Checking", business_use_percent=101)
