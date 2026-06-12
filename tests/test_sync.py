"""M3: SimpleFIN sync through the identical ingest path, mixed-mode dedupe."""

from copy import deepcopy

from ledgerline.connectors.simplefin import sync_payload
from ledgerline.ingest import ingest_file
from tests.conftest import FIXTURES

# Unix timestamps (UTC) matching dates in us_checking_jan.csv
TS_JAN_04 = 1767484800  # 2026-01-04
TS_JAN_07 = 1767744000  # 2026-01-07
TS_JAN_21 = 1768953600  # 2026-01-21
TS_FEB_02 = 1769990400  # 2026-02-02

PAYLOAD = {
    "accounts": [
        {
            "id": "SF-ACT-1",
            "name": "Demo Checking",
            "currency": "USD",
            "transactions": [
                # overlap with the January CSV export (same descriptions)
                {"id": "sf-101", "posted": TS_JAN_04, "amount": "-82.45",
                 "description": "KROGER #423 SPRINGFIELD IL"},
                {"id": "sf-102", "posted": TS_JAN_07, "amount": "-15.49",
                 "description": "NETFLIX.COM"},
                {"id": "sf-103", "posted": TS_JAN_21, "amount": "-850.00",
                 "description": "BRIGHTSTONE TRAINING LLC"},
                # new transaction only present in sync
                {"id": "sf-104", "posted": TS_FEB_02, "amount": "-31.20",
                 "description": "SQ *BRASS BADGER COFFEE"},
            ],
        }
    ]
}


def _resolver(sfid, name):
    return "US Checking"


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_sync_uses_ingest_pipeline(conn):
    results = sync_payload(conn, PAYLOAD, _resolver)
    assert results["Demo Checking"].new == 4
    # merchant_clean populated -> same normalize path as file imports
    row = conn.execute(
        "SELECT merchant_clean, external_id FROM transactions WHERE external_id = 'sf-104'"
    ).fetchone()
    assert row["merchant_clean"] == "Brass Badger Coffee"


def test_sync_then_file_import_no_duplicates(conn):
    """Acceptance: sync + file import covering the same period -> no dupes."""
    sync_payload(conn, PAYLOAD, _resolver)
    result = ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    # 3 of the CSV's 8 rows were already synced
    assert result.new == 5
    assert result.duplicates == 3
    assert _count(conn) == 9


def test_file_import_then_sync_no_duplicates_and_backfills_ids(conn):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    results = sync_payload(conn, PAYLOAD, _resolver)
    assert results["Demo Checking"].new == 1  # only the February txn
    assert results["Demo Checking"].duplicates == 3
    assert _count(conn) == 9
    # the matched CSV rows got the SimpleFIN ids backfilled
    row = conn.execute(
        "SELECT external_id FROM transactions WHERE merchant_raw = 'NETFLIX.COM'"
    ).fetchone()
    assert row["external_id"] == "sf-102"


def test_resync_is_idempotent(conn):
    sync_payload(conn, PAYLOAD, _resolver)
    results = sync_payload(conn, PAYLOAD, _resolver)
    assert results["Demo Checking"].new == 0
    assert results["Demo Checking"].duplicates == 4


def test_account_mapping_persisted(conn):
    calls = []

    def resolver(sfid, name):
        calls.append(sfid)
        return "US Checking"

    sync_payload(conn, PAYLOAD, resolver)
    sync_payload(conn, PAYLOAD, resolver)
    assert calls == ["SF-ACT-1"]  # prompted once, then mapped


def test_sync_stores_balance_institution_currency_and_type(conn):
    payload = deepcopy(PAYLOAD)
    payload["connections"] = [{"conn_id": "CON-1", "name": "Demo Bank"}]
    account = payload["accounts"][0]
    account.update({
        "conn_id": "CON-1",
        "balance": "1234.56",
        "available-balance": "1200.00",
        "balance-date": TS_FEB_02,
        "currency": "CAD",
    })

    sync_payload(conn, payload, _resolver)

    row = conn.execute(
        "SELECT institution, currency, type, balance_cents,"
        " available_balance_cents, balance_date FROM accounts"
    ).fetchone()
    assert row["institution"] == "Demo Bank"
    assert row["currency"] == "CAD"
    assert row["type"] == "checking"
    assert row["balance_cents"] == 123456
    assert row["available_balance_cents"] == 120000
    assert row["balance_date"].startswith("2026-02-02T")
