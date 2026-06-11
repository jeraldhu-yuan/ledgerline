"""Format auto-detection, dispatch, and the idempotent insert path.

Every transaction — CSV import, OFX import, or SimpleFIN sync — enters the
database through insert_transactions(). Dedupe strategy:

1. If the row carries a bank-side id (OFX FITID, SimpleFIN txn id) that this
   account has already stored, it is a duplicate.
2. Otherwise rows are matched on the base tuple
   (account_id, posted_date, amount_cents, merchant_raw) with occurrence
   counting: the Nth identical row in a batch is a duplicate only if the DB
   already holds more than N rows with that tuple. This makes re-imports and
   overlapping exports no-ops, lets sync and file imports of the same period
   coexist, and still keeps two genuinely distinct same-day, same-amount,
   same-merchant transactions.

dedupe_hash = sha256(account_id|posted_date|amount_cents|merchant_raw|occurrence)
is stored UNIQUE as a database-level backstop for the same logic.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ledgerline import LedgerlineError
from ledgerline.ingest import csv_generic, ofx
from ledgerline.ingest.profiles import PROFILES, detect_profile
from ledgerline.ingest.types import IngestResult, ParsedTxn, ParseError
from ledgerline.normalize import clean_merchant


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_or_create_account(
    conn: sqlite3.Connection,
    name: str,
    institution: str = "unknown",
    account_type: str | None = None,
    currency: str = "USD",
) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (name, institution, currency, type) VALUES (?, ?, ?, ?)",
        (name, institution, currency, account_type),
    )
    conn.commit()
    return cur.lastrowid


def dedupe_hash(account_id: int, txn: ParsedTxn, occurrence: int) -> str:
    payload = "|".join(
        [str(account_id), txn.posted_date, str(txn.amount_cents), txn.merchant_raw, str(occurrence)]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def insert_transactions(
    conn: sqlite3.Connection,
    account_id: int,
    txns: list[ParsedTxn],
    source_file: str,
    currency: str = "USD",
) -> IngestResult:
    result = IngestResult()
    imported_at = _now()
    batch_seq: dict[tuple, int] = {}
    for t in txns:
        base = (account_id, t.posted_date, t.amount_cents, t.merchant_raw)
        seq = batch_seq.get(base, 0)
        batch_seq[base] = seq + 1

        if t.external_id is not None:
            known = conn.execute(
                "SELECT id FROM transactions WHERE account_id = ? AND external_id = ?",
                (account_id, t.external_id),
            ).fetchone()
            if known:
                result.duplicates += 1
                continue

        existing = conn.execute(
            "SELECT id, external_id FROM transactions"
            " WHERE account_id = ? AND posted_date = ? AND amount_cents = ?"
            " AND merchant_raw = ? ORDER BY id",
            base,
        ).fetchall()
        if len(existing) > seq:
            result.duplicates += 1
            # The same underlying transaction arrived again with a bank id the
            # stored row lacks (e.g. sync after a CSV import): backfill it so
            # future syncs short-circuit on the id.
            if t.external_id is not None and existing[seq]["external_id"] is None:
                conn.execute(
                    "UPDATE transactions SET external_id = ? WHERE id = ?",
                    (t.external_id, existing[seq]["id"]),
                )
            continue

        cur = conn.execute(
            "INSERT INTO transactions (account_id, posted_date, amount_cents,"
            " currency, merchant_raw, merchant_clean, category, source_file,"
            " dedupe_hash, external_id, imported_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)"
            " ON CONFLICT(dedupe_hash) DO NOTHING",
            (
                account_id,
                t.posted_date,
                t.amount_cents,
                currency,
                t.merchant_raw,
                clean_merchant(t.merchant_raw),
                source_file,
                dedupe_hash(account_id, t, len(existing)),
                t.external_id,
                imported_at,
            ),
        )
        if cur.rowcount:
            result.new += 1
        else:
            result.duplicates += 1
    conn.commit()
    return result


def quarantine_rows(
    conn: sqlite3.Connection, errors: list[ParseError], source_file: str
) -> int:
    imported_at = _now()
    conn.executemany(
        "INSERT INTO quarantine (source_file, raw_line, reason, imported_at)"
        " VALUES (?, ?, ?, ?)",
        [(source_file, e.raw_line, e.reason, imported_at) for e in errors],
    )
    conn.commit()
    return len(errors)


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    account_name: str,
    profile_name: str | None = None,
) -> IngestResult:
    """Auto-detect OFX vs CSV, parse, and run the idempotent insert path."""
    path = Path(path)
    if not path.exists():
        raise LedgerlineError(f"file not found: {path}")

    if ofx.looks_like_ofx(path):
        txns, errors = ofx.parse_ofx(path)
    else:
        if profile_name is None:
            with open(path, newline="", encoding="utf-8-sig") as f:
                header = f.readline().strip().split(",")
            profile_name = detect_profile(header)
            if profile_name is None:
                raise LedgerlineError(
                    f"could not auto-detect a CSV profile for {path.name}; "
                    f"pass --profile (available: {', '.join(PROFILES)})"
                )
        if profile_name not in PROFILES:
            raise LedgerlineError(
                f"unknown profile {profile_name!r} (available: {', '.join(PROFILES)})"
            )
        txns, errors = csv_generic.parse_csv(path, PROFILES[profile_name])

    account_id = get_or_create_account(conn, account_name)
    currency = conn.execute(
        "SELECT currency FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()["currency"]
    result = insert_transactions(conn, account_id, txns, path.name, currency)
    result.failed = quarantine_rows(conn, errors, path.name)
    return result
