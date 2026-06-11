"""M3: SimpleFIN sync through the identical ingest path, mixed-mode dedupe."""

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
