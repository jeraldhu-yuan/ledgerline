from click.testing import CliRunner

from ledgerline.cli import cli
from tests.conftest import FIXTURES


def _run(db_file, *args):
    runner = CliRunner()
    return runner.invoke(cli, ["--db", str(db_file), *args], catch_exceptions=False)


def test_ingest_reports_counts(db_file):
    result = _run(db_file, "ingest", str(FIXTURES / "us_checking_jan.csv"),
                  "--account", "US Checking")
    assert result.exit_code == 0
    assert "8 new" in result.output
    assert "0 duplicate" in result.output


def test_ingest_then_summary(db_file):
    _run(db_file, "ingest", str(FIXTURES / "us_checking_jan.csv"),
         "--account", "US Checking")
    result = _run(db_file, "summary", "--month", "2026-01")
    assert result.exit_code == 0
    assert "income" in result.output
    assert "3,500.00" in result.output


def test_ingest_unknown_profile_errors_cleanly(db_file, tmp_path):
    bad = tmp_path / "mystery.csv"
    bad.write_text("ColA,ColB\n1,2\n")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--db", str(db_file), "ingest", str(bad), "--account", "X"]
    )
    assert result.exit_code == 1
    assert "profile" in result.output


def test_recurring_add_and_upcoming(db_file):
    result = _run(db_file, "recurring", "add", "--label", "Course installment",
                  "--amount", "850.00", "--cadence", "monthly", "--day", "21")
    assert result.exit_code == 0
    result = _run(db_file, "upcoming", "--days", "31")
    assert result.exit_code == 0
    assert "Course installment" in result.output


def test_ask_without_key_fails_loudly(db_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(db_file), "ask", "why?"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_accounts_set_context(db_file):
    assert _run(db_file, "accounts", "add", "Business Card").exit_code == 0

    result = _run(
        db_file,
        "accounts",
        "set-context",
        "Business Card",
        "--purpose",
        "business",
        "--entity",
        "Northwind Consulting",
        "--context",
        "Professional expenses",
    )

    assert result.exit_code == 0
    assert "business" in result.output
    from ledgerline import db

    conn = db.connect(db_file)
    row = conn.execute(
        "SELECT purpose, entity_name, context_note FROM accounts WHERE name = ?",
        ("Business Card",),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "purpose": "business",
        "entity_name": "Northwind Consulting",
        "context_note": "Professional expenses",
    }
