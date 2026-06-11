"""Security invariants: nothing sensitive in the DB, nothing trackable by git."""

from pathlib import Path

from ledgerline.ingest import ingest_file
from tests.conftest import FIXTURES

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dump_all_text(conn) -> str:
    """Every text value in every user table, concatenated."""
    chunks = []
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    for t in tables:
        for row in conn.execute(f"SELECT * FROM {t}"):  # noqa: S608 - test code, table names from sqlite_master
            chunks.extend(str(v) for v in row)
    return "\n".join(chunks)


def test_no_account_numbers_anywhere_in_db(conn):
    """The OFX fixture contains ACCTID 9999888877 and BANKID 061000052.
    Neither may be stored anywhere (acceptance checklist)."""
    ingest_file(conn, FIXTURES / "sample.ofx", "US Checking")
    dump = _dump_all_text(conn)
    assert "9999888877" not in dump
    assert "061000052" not in dump


def test_gitignore_blocks_data_from_first_commit():
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    patterns = {line.strip() for line in gitignore if line.strip()}
    for required in ("data/", "*.db", "*.csv", "*.ofx", "*.qfx", ".env"):
        assert required in patterns, f".gitignore must contain {required}"


def test_fixtures_are_fabricated_markers():
    """Cheap tripwire: fixture merchants are invented names; a real export
    would carry real FI ids. Assert the fixtures stay obviously fake."""
    text = (FIXTURES / "us_checking_jan.csv").read_text()
    assert "ACME" in text  # fabricated employer


def test_simplefin_token_never_in_db(conn, monkeypatch):
    """Sync stores only SimpleFIN's opaque account id, never the access URL."""
    from ledgerline.connectors.simplefin import sync_payload

    secret_url = "https://user:secretpass@bridge.example.com/simplefin"
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", secret_url)
    payload = {
        "accounts": [
            {
                "id": "SF-ACT-1",
                "name": "Checking X1234",
                "currency": "USD",
                "transactions": [
                    {"id": "sf-1", "posted": 1767312000, "amount": "-12.00",
                     "description": "COFFEE"},
                ],
            }
        ]
    }
    sync_payload(conn, payload, resolver=lambda sfid, name: "US Checking")
    dump = _dump_all_text(conn)
    assert "secretpass" not in dump
    assert "bridge.example.com" not in dump
