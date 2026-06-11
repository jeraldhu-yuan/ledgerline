"""Idempotency acceptance tests — the non-negotiables."""

from ledgerline.ingest import get_or_create_account, ingest_file, insert_transactions
from ledgerline.ingest.types import ParsedTxn
from tests.conftest import FIXTURES


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_csv_ingest_basic(conn):
    result = ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    assert result.new == 8
    assert result.duplicates == 0
    assert result.failed == 0
    # amounts are integer cents, sign preserved
    row = conn.execute(
        "SELECT amount_cents FROM transactions WHERE merchant_raw LIKE 'PAYROLL%'"
    ).fetchone()
    assert row["amount_cents"] == 350000


def test_double_import_identical_file_zero_new(conn):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    before = _count(conn)
    result = ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    assert result.new == 0
    assert result.duplicates == 8
    assert _count(conn) == before


def test_overlapping_exports_no_duplicates(conn):
    ingest_file(conn, FIXTURES / "overlap_1.csv", "US Checking")
    result = ingest_file(conn, FIXTURES / "overlap_2.csv", "US Checking")
    assert result.new == 1  # only the 01/25 row is new
    assert result.duplicates == 2
    assert _count(conn) == 4


def test_distinct_same_day_same_amount_transactions_both_kept(conn):
    # us_checking_jan.csv contains two identical Brass Badger rows: two real
    # coffees, not duplicates
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    n = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_raw = 'SQ *BRASS BADGER COFFEE'"
    ).fetchone()[0]
    assert n == 2


def test_malformed_rows_quarantined_run_completes(conn):
    result = ingest_file(conn, FIXTURES / "malformed.csv", "US Checking")
    assert result.new == 2
    assert result.failed == 3
    q = conn.execute("SELECT raw_line, reason FROM quarantine").fetchall()
    assert len(q) == 3
    assert all(r["reason"] for r in q)


def test_ofx_ingest_with_fitids(conn):
    result = ingest_file(conn, FIXTURES / "sample.ofx", "US Checking")
    assert result.new == 4
    assert result.failed == 0
    # the two same-day Chipotle charges have distinct FITIDs -> both kept
    n = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_raw LIKE 'CHIPOTLE%'"
    ).fetchone()[0]
    assert n == 2
    # FITIDs stored as external ids
    ext = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE external_id IS NOT NULL"
    ).fetchone()[0]
    assert ext == 4


def test_ofx_double_import_zero_new(conn):
    ingest_file(conn, FIXTURES / "sample.ofx", "US Checking")
    result = ingest_file(conn, FIXTURES / "sample.ofx", "US Checking")
    assert result.new == 0
    assert result.duplicates == 4


def test_different_accounts_do_not_dedupe_against_each_other(conn):
    ingest_file(conn, FIXTURES / "overlap_1.csv", "US Checking")
    result = ingest_file(conn, FIXTURES / "overlap_1.csv", "Joint Checking")
    assert result.new == 3


def test_sign_convention_profile(conn):
    ingest_file(conn, FIXTURES / "generic_visa_jan.csv", "US Visa")
    # charges exported positive must land as negative cents
    row = conn.execute(
        "SELECT amount_cents FROM transactions WHERE merchant_raw LIKE 'DELTA%'"
    ).fetchone()
    assert row["amount_cents"] == -41230


def test_merchant_clean_populated_on_ingest(conn):
    ingest_file(conn, FIXTURES / "us_checking_jan.csv", "US Checking")
    row = conn.execute(
        "SELECT merchant_clean FROM transactions WHERE merchant_raw = 'SQ *BRASS BADGER COFFEE'"
    ).fetchone()
    assert row["merchant_clean"] == "Brass Badger Coffee"


def test_insert_same_tuple_across_batches_with_distinct_external_ids(conn):
    """Two genuinely distinct same-day txns arriving in separate syncs."""
    account_id = get_or_create_account(conn, "US Checking")
    t1 = ParsedTxn("2026-03-01", -999, "GUMBALL MACHINE", external_id="a1")
    t2 = ParsedTxn("2026-03-01", -999, "GUMBALL MACHINE", external_id="a2")
    r1 = insert_transactions(conn, account_id, [t1], "sync1")
    # same batch shape, different id: a second real purchase, not a duplicate?
    # No — without batch context we conservatively match on the base tuple,
    # but the id backfill means re-sending BOTH ids in one batch keeps both.
    r2 = insert_transactions(conn, account_id, [t1, t2], "sync2")
    assert r1.new == 1
    assert r2.new == 1  # t1 deduped by external id, t2 is new
    assert r2.duplicates == 1
