"""M3: SimpleFIN sync through the identical ingest path, mixed-mode dedupe."""

from copy import deepcopy

import pytest

import ledgerline.connectors.simplefin as simplefin
from ledgerline import LedgerlineError
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


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, body: bytes):
        self._body = body
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _FakeResponse(self._body)


def _token_for(claim_url: str) -> str:
    import base64

    return base64.b64encode(claim_url.encode()).decode()


def test_claim_setup_token_posts_and_returns_access_url(monkeypatch):
    opener = _FakeOpener(b"https://user:pass@bridge.example.com/simplefin\n")
    monkeypatch.setattr(simplefin, "_OPENER", opener)

    # whitespace-wrapped tokens (browser copy/paste) are tolerated
    token = _token_for("https://bridge.example.com/claim/abc")
    wrapped = token[:10] + "\n" + token[10:]
    access_url = simplefin.claim_setup_token(wrapped)

    assert access_url == "https://user:pass@bridge.example.com/simplefin"
    assert opener.requests[0].get_method() == "POST"
    assert opener.requests[0].full_url == "https://bridge.example.com/claim/abc"


@pytest.mark.parametrize(
    "token",
    [
        "not base64!!!",
        _token_for("http://bridge.example.com/claim/abc"),  # claim URL must be https
    ],
)
def test_claim_setup_token_rejects_bad_tokens(token, monkeypatch):
    monkeypatch.setattr(simplefin, "_OPENER", _FakeOpener(b""))
    with pytest.raises(LedgerlineError):
        simplefin.claim_setup_token(token)


def test_claim_setup_token_rejects_non_https_access_url(monkeypatch):
    monkeypatch.setattr(
        simplefin, "_OPENER", _FakeOpener(b"http://bridge.example.com/simplefin")
    )
    with pytest.raises(LedgerlineError, match="invalid access URL"):
        simplefin.claim_setup_token(_token_for("https://bridge.example.com/claim/abc"))


def test_store_access_url_is_owner_only_and_readable_back(tmp_path):
    target = tmp_path / "config" / "simplefin.env"
    stored = simplefin.store_access_url("https://u:p@bridge.example.com/sf", target)

    assert stored == target
    assert target.parent.stat().st_mode & 0o077 == 0
    assert target.stat().st_mode & 0o077 == 0
    assert simplefin._url_from_file(target) == "https://u:p@bridge.example.com/sf"


def _sync_state(conn, key):
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def test_sync_requires_https(conn, monkeypatch):
    monkeypatch.setenv(
        "SIMPLEFIN_ACCESS_URL", "http://user:pass@bridge.example.com/simplefin"
    )
    with pytest.raises(LedgerlineError, match="https"):
        simplefin.sync(conn, _resolver)
    assert _sync_state(conn, "simplefin_last_attempt") is None


def test_clean_sync_records_attempt_and_success(conn, monkeypatch):
    monkeypatch.setenv(
        "SIMPLEFIN_ACCESS_URL", "https://user:pass@bridge.example.com/simplefin"
    )
    monkeypatch.setattr(
        simplefin, "fetch_accounts",
        lambda url, since=None, until=None: deepcopy(PAYLOAD),
    )
    _, errors = simplefin.sync(conn, _resolver)
    assert errors == []
    assert _sync_state(conn, "simplefin_last_attempt") is not None
    assert _sync_state(conn, "simplefin_last_success") is not None


def test_partially_failed_sync_does_not_claim_success(conn, monkeypatch):
    payload = deepcopy(PAYLOAD)
    payload["errors"] = ["Connection to Demo Bank may need attention"]
    monkeypatch.setenv(
        "SIMPLEFIN_ACCESS_URL", "https://user:pass@bridge.example.com/simplefin"
    )
    monkeypatch.setattr(
        simplefin, "fetch_accounts",
        lambda url, since=None, until=None: deepcopy(payload),
    )
    _, errors = simplefin.sync(conn, _resolver)
    assert errors and errors[0]["msg"]
    assert _sync_state(conn, "simplefin_last_attempt") is not None
    assert _sync_state(conn, "simplefin_last_success") is None


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
