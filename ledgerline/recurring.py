"""Recurring payment detection and expected-charge projection."""

import sqlite3
from datetime import date, timedelta
from statistics import median, mode

# Spec tolerances: monthly +/-4 days, weekly +/-2, annual +/-10
_CADENCE_WINDOWS = {
    "monthly": (24, 35),
    "weekly": (5, 9),
    "annual": (355, 375),
}
AMOUNT_TOLERANCE = 0.10


def _classify_intervals(days: list[int]) -> str | None:
    """Match intervals to a cadence, tolerating skipped occurrences: a gap of
    ~2x the cadence (a missed month, an unpaid week) counts as that cadence
    rather than disqualifying the whole history. At least half the intervals
    must be single-cadence so e.g. a purely bi-weekly charge is not "weekly"."""
    for cadence, (lo, hi) in _CADENCE_WINDOWS.items():
        nominal = (lo + hi) / 2
        single = 0
        for d in days:
            k = max(1, round(d / nominal))
            if not lo * k <= d <= hi * k:
                break
            if k == 1:
                single += 1
        else:
            if single * 2 >= len(days):
                return cadence
    return None


def detect(conn: sqlite3.Connection) -> list[dict]:
    """Flag merchant groups with >=3 occurrences, amounts within +/-10%, and a
    consistent interval. Returns the groups created or updated."""
    found = []
    merchants = conn.execute(
        "SELECT account_id, currency, merchant_clean, COUNT(*) AS n FROM transactions"
        " WHERE merchant_clean IS NOT NULL AND amount_cents < 0"
        " GROUP BY account_id, currency, merchant_clean HAVING n >= 3"
    ).fetchall()
    for row in merchants:
        txns = conn.execute(
            "SELECT id, posted_date, amount_cents FROM transactions"
            " WHERE account_id = ? AND currency = ? AND merchant_clean = ?"
            " AND amount_cents < 0 ORDER BY posted_date",
            (row["account_id"], row["currency"], row["merchant_clean"]),
        ).fetchall()
        amounts = [t["amount_cents"] for t in txns]
        med = int(median(amounts))
        if any(abs(a - med) > abs(med) * AMOUNT_TOLERANCE for a in amounts):
            continue
        dates = [date.fromisoformat(t["posted_date"]) for t in txns]
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        cadence = _classify_intervals(intervals)
        if cadence is None:
            continue
        expected_day = mode(d.day for d in dates) if cadence == "monthly" else None

        existing = conn.execute(
            "SELECT id FROM recurring_groups WHERE account_id = ? AND currency = ?"
            " AND merchant_clean = ?",
            (row["account_id"], row["currency"], row["merchant_clean"]),
        ).fetchone()
        if existing:
            group_id = existing["id"]
            conn.execute(
                "UPDATE recurring_groups SET expected_amount_cents = ?,"
                " cadence = ?, expected_day = ? WHERE id = ?",
                (med, cadence, expected_day, group_id),
            )
        else:
            group_id = conn.execute(
                "INSERT INTO recurring_groups"
                " (label, expected_amount_cents, cadence, expected_day, merchant_clean,"
                " account_id, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["merchant_clean"], med, cadence, expected_day,
                    row["merchant_clean"], row["account_id"], row["currency"],
                ),
            ).lastrowid
        conn.execute(
            "UPDATE transactions SET recurring_group_id = ? WHERE account_id = ?"
            " AND currency = ? AND merchant_clean = ? AND amount_cents < 0",
            (group_id, row["account_id"], row["currency"], row["merchant_clean"]),
        )
        found.append(
            {"label": row["merchant_clean"], "cadence": cadence,
             "expected_amount_cents": med, "expected_day": expected_day,
             "account_id": row["account_id"], "currency": row["currency"]}
        )
    conn.commit()
    return found


def add_manual_group(
    conn: sqlite3.Connection,
    label: str,
    expected_amount_cents: int,
    cadence: str,
    expected_day: int | None = None,
    merchant_clean: str | None = None,
    account_id: int | None = None,
    currency: str | None = None,
) -> int:
    """Manual group for known installments with <3 occurrences so far, so
    `upcoming` warns before the pattern is statistically detectable."""
    if cadence not in (*_CADENCE_WINDOWS, "irregular"):
        raise ValueError(f"cadence must be one of monthly/weekly/annual/irregular")
    if merchant_clean and account_id is None:
        matches = conn.execute(
            "SELECT DISTINCT account_id, currency FROM transactions"
            " WHERE merchant_clean = ?",
            (merchant_clean,),
        ).fetchall()
        if len(matches) == 1:
            account_id = matches[0]["account_id"]
            currency = matches[0]["currency"]
        elif len(matches) > 1:
            raise ValueError("merchant exists in multiple accounts; specify account_id")
    group_id = conn.execute(
        "INSERT INTO recurring_groups"
        " (label, expected_amount_cents, cadence, expected_day, merchant_clean,"
        " account_id, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            label, expected_amount_cents, cadence, expected_day, merchant_clean,
            account_id, currency,
        ),
    ).lastrowid
    if merchant_clean:
        clauses = ["merchant_clean = ?"]
        params: list[object] = [merchant_clean]
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if currency is not None:
            clauses.append("currency = ?")
            params.append(currency)
        conn.execute(
            "UPDATE transactions SET recurring_group_id = ? WHERE " + " AND ".join(clauses),
            [group_id, *params],
        )
    conn.commit()
    return group_id


def _next_monthly(expected_day: int, start: date) -> date:
    """First occurrence of day-of-month `expected_day` on or after `start`,
    clamping to short months."""
    year, month = start.year, start.month
    while True:
        last_dom = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
        candidate = date(year, month, min(expected_day, last_dom))
        if candidate >= start:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1


def upcoming(conn: sqlite3.Connection, days: int = 30, today: date | None = None) -> list[dict]:
    """Expected charges in the window, from active recurring groups."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    expected = []
    groups = conn.execute(
        "SELECT r.*, a.name account, a.analysis_treatment, a.balance_cents"
        " FROM recurring_groups r"
        " LEFT JOIN accounts a ON a.id = r.account_id WHERE r.active = 1"
    ).fetchall()
    for g in groups:
        last_row = conn.execute(
            "SELECT MAX(posted_date) AS last FROM transactions WHERE recurring_group_id = ?",
            (g["id"],),
        ).fetchone()
        last = date.fromisoformat(last_row["last"]) if last_row["last"] else None

        nxt: date | None = None
        if g["cadence"] == "monthly" and g["expected_day"]:
            # Start looking the day after the last charge (or today) so a
            # just-paid installment doesn't re-warn for the same month.
            start = max(today, last + timedelta(days=1)) if last else today
            nxt = _next_monthly(g["expected_day"], start)
        elif g["cadence"] == "weekly" and last:
            nxt = last + timedelta(days=7)
            while nxt < today:
                nxt += timedelta(days=7)
        elif g["cadence"] == "annual" and last:
            nxt = last + timedelta(days=365)
            while nxt < today:
                nxt += timedelta(days=365)
        if nxt and today <= nxt <= horizon:
            expected.append(
                {
                    "label": g["label"],
                    "date": nxt.isoformat(),
                    "expected_amount_cents": g["expected_amount_cents"],
                    "cadence": g["cadence"],
                    "currency": g["currency"],
                    "account": g["account"],
                    "analysis_treatment": g["analysis_treatment"],
                    "balance_cents": g["balance_cents"],
                }
            )
    expected.sort(key=lambda e: e["date"])
    return expected
