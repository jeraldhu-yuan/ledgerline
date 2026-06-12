from datetime import date

from ledgerline.ingest import get_or_create_account, insert_transactions
from ledgerline.ingest.types import ParsedTxn
from ledgerline.recurring import add_manual_group, detect, upcoming


def _seed(conn, rows, account="US Checking"):
    account_id = get_or_create_account(conn, account)
    txns = [ParsedTxn(d, cents, merchant) for d, cents, merchant in rows]
    insert_transactions(conn, account_id, txns, "seed")
    return account_id


def test_detects_monthly_subscription(conn):
    _seed(conn, [
        ("2025-11-07", -1549, "NETFLIX.COM"),
        ("2025-12-07", -1549, "NETFLIX.COM"),
        ("2026-01-07", -1549, "NETFLIX.COM"),
    ])
    found = detect(conn)
    assert len(found) == 1
    g = found[0]
    assert g["cadence"] == "monthly"
    assert g["expected_day"] == 7
    assert g["expected_amount_cents"] == -1549
    # transactions linked to the group
    linked = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE recurring_group_id IS NOT NULL"
    ).fetchone()[0]
    assert linked == 3


def test_amount_within_ten_percent_still_groups(conn):
    _seed(conn, [
        ("2025-11-10", -14000, "DUKE ENERGY"),
        ("2025-12-09", -14500, "DUKE ENERGY"),
        ("2026-01-11", -15200, "DUKE ENERGY"),
    ])
    assert len(detect(conn)) == 1


def test_wild_amounts_do_not_group(conn):
    _seed(conn, [
        ("2025-11-10", -5000, "KROGER"),
        ("2025-12-09", -12000, "KROGER"),
        ("2026-01-11", -3000, "KROGER"),
    ])
    assert detect(conn) == []


def test_irregular_intervals_do_not_group(conn):
    _seed(conn, [
        ("2025-11-01", -1000, "COFFEE CART"),
        ("2025-11-04", -1000, "COFFEE CART"),
        ("2026-01-11", -1000, "COFFEE CART"),
    ])
    assert detect(conn) == []


def test_two_occurrences_not_enough(conn):
    _seed(conn, [
        ("2025-12-07", -1549, "NETFLIX.COM"),
        ("2026-01-07", -1549, "NETFLIX.COM"),
    ])
    assert detect(conn) == []


def test_detect_is_idempotent(conn):
    _seed(conn, [
        ("2025-11-07", -1549, "NETFLIX.COM"),
        ("2025-12-07", -1549, "NETFLIX.COM"),
        ("2026-01-07", -1549, "NETFLIX.COM"),
    ])
    detect(conn)
    detect(conn)
    n = conn.execute("SELECT COUNT(*) FROM recurring_groups").fetchone()[0]
    assert n == 1


def test_detection_does_not_merge_accounts_or_currencies(conn):
    _seed(conn, [
        ("2025-11-07", -1549, "NETFLIX.COM"),
        ("2025-12-07", -1549, "NETFLIX.COM"),
        ("2026-01-07", -1549, "NETFLIX.COM"),
    ], account="USD Checking")
    account_id = get_or_create_account(conn, "CAD Checking", currency="CAD")
    insert_transactions(conn, account_id, [
        ParsedTxn("2025-11-07", -2000, "NETFLIX.COM"),
        ParsedTxn("2025-12-07", -2000, "NETFLIX.COM"),
        ParsedTxn("2026-01-07", -2000, "NETFLIX.COM"),
    ], "seed", currency="CAD")

    found = detect(conn)
    assert len(found) == 2
    assert {group["currency"] for group in found} == {"USD", "CAD"}


def test_manual_group_with_one_txn_surfaces_in_upcoming(conn):
    """Acceptance: a 4-payment course tuition plan billing on the 21st warns
    BEFORE the third charge exists."""
    _seed(conn, [("2026-01-21", -85000, "BRIGHTSTONE TRAINING LLC")])
    add_manual_group(
        conn,
        label="Brightstone Training installment",
        expected_amount_cents=-85000,
        cadence="monthly",
        expected_day=21,
        merchant_clean="Brightstone Training Llc",
    )
    expected = upcoming(conn, days=30, today=date(2026, 2, 10))
    assert [e["label"] for e in expected] == ["Brightstone Training installment"]
    assert expected[0]["date"] == "2026-02-21"
    assert expected[0]["expected_amount_cents"] == -85000


def test_manual_group_with_zero_txns_still_projects(conn):
    add_manual_group(conn, "Rent", -180000, "monthly", expected_day=1)
    expected = upcoming(conn, days=30, today=date(2026, 2, 10))
    assert expected[0]["date"] == "2026-03-01"


def test_upcoming_respects_window(conn):
    add_manual_group(conn, "Rent", -180000, "monthly", expected_day=1)
    # window ends before the 1st of next month
    assert upcoming(conn, days=5, today=date(2026, 2, 10)) == []


def test_just_paid_installment_does_not_rewarn_same_month(conn):
    _seed(conn, [("2026-02-21", -85000, "BRIGHTSTONE TRAINING LLC")])
    add_manual_group(
        conn, "Course installment", -85000, "monthly", 21,
        merchant_clean="Brightstone Training Llc",
    )
    expected = upcoming(conn, days=30, today=date(2026, 2, 22))
    assert expected[0]["date"] == "2026-03-21"


def test_monthly_day_clamps_to_short_months(conn):
    add_manual_group(conn, "Card payment", -50000, "monthly", expected_day=31)
    expected = upcoming(conn, days=30, today=date(2026, 2, 10))
    assert expected[0]["date"] == "2026-02-28"
