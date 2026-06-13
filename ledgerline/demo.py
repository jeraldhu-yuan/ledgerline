"""Fabricated demo data: the full product experience with zero real financial data.

Everything goes through the normal pipeline — insert_transactions for dedupe
and merchant cleaning, categorize_rules_only plus the merchant cache for
categories, detect() for recurring groups — so the demo exercises exactly the
code paths real data does. All merchants and institutions are invented
(fixture convention: ACME, Brightstone, Brass Badger); no real bank ids exist
anywhere in the seed.
"""

import random
import sqlite3
from datetime import date, timedelta

from ledgerline import LedgerlineError
from ledgerline.accounts import set_context
from ledgerline.categorize import categorize_rules_only, set_manual
from ledgerline.ingest import get_or_create_account, insert_transactions
from ledgerline.ingest.types import ParsedTxn
from ledgerline.normalize import clean_merchant
from ledgerline.recurring import add_manual_group, detect

DEMO_MONTHS = 6
DEMO_SOURCE = "demo-seed"

# Merchants the static rules cannot resolve (invented brands carry no generic
# keyword); seeded into the merchant cache the same way `ledgerline review`
# corrections are.
_CACHE_SEED = [
    ("ORCHARD CROSSING MARKET", "groceries"),
    ("FLIXBURROW.COM", "subscriptions"),
    ("TUNEDRIFT MUSIC", "subscriptions"),
    ("BRIGHTSTONE TRAINING LLC", "professional"),
    ("ZIMBERLY OFFICE SUPPLY CO", "shopping"),
    ("HATTERLY BOOKS", "shopping"),
    ("PINEGATE HARDWARE", "shopping"),
    ("CASCADE FERRY TICKETS", "travel"),
]


def _monthly_dates(day: int, today: date, months: int = DEMO_MONTHS) -> list[date]:
    """The last `months` occurrences of day-of-month `day`, all on or before
    `today`, clamping to short months."""
    year, month = today.year, today.month
    out: list[date] = []
    while len(out) < months:
        last_dom = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
        candidate = date(year, month, min(day, last_dom))
        if candidate <= today:
            out.append(candidate)
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return sorted(out)


def _every(days: int, today: date, end_offset: int = 0) -> list[date]:
    """Dates every `days` days covering the demo window, newest first occurrence
    `end_offset` days before today."""
    horizon = today - timedelta(days=DEMO_MONTHS * 30)
    d = today - timedelta(days=end_offset)
    out = []
    while d >= horizon:
        out.append(d)
        d -= timedelta(days=days)
    return sorted(out)


def seed_demo(
    conn: sqlite3.Connection, today: date | None = None, force: bool = False
) -> dict:
    """Seed ~6 months of fabricated transactions across two USD accounts.

    Refuses to touch a database that already contains transactions unless
    `force` is set — fabricated rows must never mix into a real ledger
    unnoticed."""
    today = today or date.today()
    existing = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if existing and not force:
        raise LedgerlineError(
            f"this database already contains {existing} transactions; "
            "refusing to mix fabricated demo data into a real ledger. "
            "Pass --force to seed anyway, or point --db / LEDGERLINE_DB at a fresh file."
        )

    rng = random.Random(2026)
    checking_id = get_or_create_account(
        conn, "Demo Checking", "ACME Federal Bank", "checking", "USD"
    )
    card_id = get_or_create_account(
        conn, "Demo Credit Card", "Brass Badger Bank", "credit", "USD"
    )

    checking: list[ParsedTxn] = []
    card: list[ParsedTxn] = []

    def add(txns: list[ParsedTxn], d: date, cents: int, merchant: str) -> None:
        txns.append(ParsedTxn(d.isoformat(), cents, merchant))

    # Income: biweekly payroll, last payday three days ago
    for d in _every(14, today, end_offset=3):
        add(checking, d, 261245, "PAYROLL ACME CORP DIRECT DEP")

    # Fixed monthly obligations (stable amounts -> detect() groups them)
    for d in _monthly_dates(1, today):
        add(checking, d, -185000, "WILLOWMERE PROPERTY MGMT RENT")
    for d in _monthly_dates(10, today):
        # Within detect()'s +/-10%-of-median tolerance even in the worst case
        jitter = rng.uniform(-0.04, 0.04)
        add(checking, d, -int(13800 * (1 + jitter)), "GLOWLINE ELECTRIC PMT")
    for d in _monthly_dates(3, today):
        add(checking, d, -4500, "IRONHOLLOW GYM")
    for d in _monthly_dates(17, today):
        add(checking, d, -9800, "SHELTERSTONE INSURANCE PMT")
    for d in _monthly_dates(7, today):
        add(card, d, -1549, "FLIXBURROW.COM")
    for d in _monthly_dates(12, today):
        add(card, d, -1099, "TUNEDRIFT MUSIC")

    # Monthly installment, only two charges so far: below detect()'s threshold,
    # which is exactly what the manual recurring group below is for (the
    # Brightstone Training pattern from the test fixtures).
    for d in _monthly_dates(21, today)[-2:]:
        add(checking, d, -85000, "BRIGHTSTONE TRAINING LLC")

    # Credit-card payments: checking outflow and card credit on the same day,
    # both categorized as transfers so they never count as spending or income
    for d in _monthly_dates(25, today):
        cents = rng.randint(60000, 92000)
        add(checking, d, -cents, "ONLINE TRANSFER TO BRASS BADGER CARD")
        add(card, d, cents, "ONLINE TRANSFER FROM ACME CHECKING")

    # Everyday card spending (amounts vary too much to look recurring)
    for d in _every(7, today, end_offset=2):
        add(card, d, -rng.randint(5200, 11800), "ORCHARD CROSSING MARKET")
    for d in _every(4, today, end_offset=1):
        add(card, d, -rng.choice([450, 525, 575, 675]), "SQ *BRASS BADGER COFFEE")
    for d in _every(16, today, end_offset=6):
        add(card, d, -rng.randint(2700, 4100), "MOONPETAL PIZZA CO")
    for d in _every(19, today, end_offset=9):
        add(card, d, -rng.randint(1900, 3300), "SADDLEROCK TAQUERIA")

    # One-off purchases
    for txns, offset, cents, merchant in [
        (checking, 9, -22000, "ZIMBERLY OFFICE SUPPLY CO"),
        (card, 33, -8600, "CASCADE FERRY TICKETS"),
        (card, 61, -3420, "HATTERLY BOOKS"),
        (checking, 102, -5891, "PINEGATE HARDWARE"),
    ]:
        add(txns, today - timedelta(days=offset), cents, merchant)

    result_checking = insert_transactions(conn, checking_id, checking, DEMO_SOURCE, "USD")
    result_card = insert_transactions(conn, card_id, card, DEMO_SOURCE, "USD")

    categorize_rules_only(conn)
    for raw, category in _CACHE_SEED:
        set_manual(conn, clean_merchant(raw), category)

    detected = detect(conn)
    add_manual_group(
        conn,
        "Brightstone Training installment",
        -85000,
        "monthly",
        expected_day=21,
        merchant_clean=clean_merchant("BRIGHTSTONE TRAINING LLC"),
        account_id=checking_id,
        currency="USD",
    )

    # Point-in-time balances, as a SimpleFIN sync would provide
    conn.execute(
        "UPDATE accounts SET balance_cents = ?, available_balance_cents = ?,"
        " balance_date = ? WHERE id = ?",
        (428012, 428012, today.isoformat(), checking_id),
    )
    conn.execute(
        "UPDATE accounts SET balance_cents = ?, available_balance_cents = ?,"
        " balance_date = ? WHERE id = ?",
        (-103755, None, today.isoformat(), card_id),
    )
    conn.commit()
    for name in ("Demo Checking", "Demo Credit Card"):
        set_context(
            conn, name, purpose="personal",
            context_note="Fabricated demo data — not a real account.",
        )

    return {
        "accounts": 2,
        "transactions": result_checking.new + result_card.new,
        "recurring_groups": len(detected) + 1,
        "first_date": (today - timedelta(days=DEMO_MONTHS * 30)).isoformat(),
        "last_date": today.isoformat(),
    }
