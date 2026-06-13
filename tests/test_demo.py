import pytest
from click.testing import CliRunner

from ledgerline import LedgerlineError, db
from ledgerline.cli import cli
from ledgerline.demo import seed_demo
from ledgerline.recurring import upcoming
from tests.conftest import FIXTURES


def _run(db_file, *args):
    runner = CliRunner()
    return runner.invoke(cli, ["--db", str(db_file), *args], catch_exceptions=False)


def test_demo_seeds_fresh_db(db_file):
    result = _run(db_file, "demo")
    assert result.exit_code == 0
    assert "Try these next" in result.output
    assert "ledgerline-mcp" in result.output

    conn = db.connect(db_file)
    accounts = conn.execute("SELECT name, currency, type FROM accounts ORDER BY name").fetchall()
    assert [(a["name"], a["currency"], a["type"]) for a in accounts] == [
        ("Demo Checking", "USD", "checking"),
        ("Demo Credit Card", "USD", "credit"),
    ]
    txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert txns > 100
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category IS NULL"
    ).fetchone()[0]
    assert uncategorized == 0
    # ~6 months of coverage, ending recently
    span = conn.execute(
        "SELECT MIN(posted_date), MAX(posted_date),"
        " julianday(MAX(posted_date)) - julianday(MIN(posted_date)) FROM transactions"
    ).fetchone()
    assert span[2] >= 150
    conn.close()


def test_demo_refuses_populated_db(db_file):
    assert _run(db_file, "demo").exit_code == 0
    result = _run(db_file, "demo")
    assert result.exit_code == 1
    assert "refusing" in result.output
    assert "--force" in result.output


def test_seed_demo_raises_on_populated_db(db_file):
    conn = db.connect(db_file)
    seed_demo(conn)
    with pytest.raises(LedgerlineError, match="refusing"):
        seed_demo(conn)
    conn.close()


def test_demo_force_seeds_populated_db(db_file):
    _run(db_file, "ingest", str(FIXTURES / "us_checking_jan.csv"),
         "--account", "US Checking")
    assert _run(db_file, "demo").exit_code == 1
    result = _run(db_file, "demo", "--force")
    assert result.exit_code == 0

    conn = db.connect(db_file)
    demo_txns = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE source_file = 'demo-seed'"
    ).fetchone()[0]
    assert demo_txns > 100
    conn.close()


def test_demo_recurring_appears_in_upcoming(db_file):
    assert _run(db_file, "demo").exit_code == 0
    conn = db.connect(db_file)
    expected = upcoming(conn, days=35)
    labels = {e["label"] for e in expected}
    # The manually added installment (below detect()'s 3-occurrence threshold)
    assert "Brightstone Training installment" in labels
    # Auto-detected monthly groups
    assert {"Willowmere Property Mgmt Rent", "Ironhollow Gym",
            "Shelterstone Insurance Pmt", "Flixburrow.com"} <= labels
    assert all(e["expected_amount_cents"] < 0 for e in expected)
    conn.close()


def test_demo_cli_upcoming_has_output(db_file):
    _run(db_file, "demo")
    result = _run(db_file, "upcoming", "--days", "35")
    assert result.exit_code == 0
    assert "Brightstone Training installment" in result.output
