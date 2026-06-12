"""SimpleFIN Bridge sync (M3).

Pulls accounts + transactions from the access URL and feeds every transaction
through the SAME normalize -> dedupe -> categorize pipeline as file imports.
SimpleFIN transaction ids ride along as external_id, so sync and historical
file imports of the same period coexist without duplicates.

Security invariants honored here:
- The access URL (credentials) comes from SIMPLEFIN_ACCESS_URL or a local
  .env file — never the repo, never the DB, never the LLM.
- Only SimpleFIN's opaque account id and the account's display name are read;
  bank account-number fields are dropped at parse time.
"""

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ledgerline import LedgerlineError
from ledgerline.ingest import get_or_create_account, insert_transactions
from ledgerline.ingest.types import IngestResult, ParsedTxn
from ledgerline.money import parse_amount_to_cents

# resolver(simplefin_id, display_name) -> local account label
Resolver = Callable[[str, str], str]
PROTECTED_ENV_FILE = Path.home() / ".config" / "ledgerline" / "simplefin.env"
SYNC_WINDOW_DAYS = 45
SYNC_OVERLAP_DAYS = 7


def _url_from_file(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.strip().startswith("SIMPLEFIN_ACCESS_URL="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def access_url_from_env() -> str:
    url = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if not url:
        url = _url_from_file(PROTECTED_ENV_FILE)
    if not url:
        url = _url_from_file(Path(".env"))
    if not url:
        raise LedgerlineError(
            "SIMPLEFIN_ACCESS_URL is not set (env var, protected config, or .env). "
            "Claim a setup token at https://bridge.simplefin.org and store the "
            "access URL in ~/.config/ledgerline/simplefin.env with mode 0600."
        )
    return url


def fetch_accounts(
    access_url: str,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """GET /accounts from the SimpleFIN access URL (credentials embedded in
    the URL's userinfo, sent as HTTP basic auth)."""
    parts = urllib.parse.urlsplit(access_url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    url = urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path.rstrip("/") + "/accounts", "", "")
    )
    query = {"version": "2"}
    if since:
        query["start-date"] = int(
            datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()
        )
    if until:
        end_date = date.fromisoformat(until)
        query["end-date"] = int(
            datetime(
                end_date.year, end_date.month, end_date.day, 23, 59, 59,
                tzinfo=timezone.utc,
            ).timestamp()
        )
    url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url)
    # The Bridge rejects Python's default urllib user agent with HTTP 403.
    req.add_header("User-Agent", "Ledgerline/0.1")
    if parts.username is not None:
        username = urllib.parse.unquote(parts.username)
        password = urllib.parse.unquote(parts.password or "")
        creds = f"{username}:{password}"
        token = base64.b64encode(creds.encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise LedgerlineError(f"SimpleFIN returned HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise LedgerlineError(f"could not reach SimpleFIN: {e.reason}") from e


def map_account(conn: sqlite3.Connection, simplefin_id: str, name: str,
                resolver: Resolver) -> int:
    row = conn.execute(
        "SELECT account_id FROM simplefin_account_map WHERE simplefin_id = ?",
        (simplefin_id,),
    ).fetchone()
    if row:
        return row["account_id"]
    label = resolver(simplefin_id, name)
    account_id = get_or_create_account(conn, label)
    conn.execute(
        "INSERT INTO simplefin_account_map (simplefin_id, account_id) VALUES (?, ?)",
        (simplefin_id, account_id),
    )
    conn.commit()
    return account_id


def _infer_account_type(name: str) -> str | None:
    lowered = name.lower()
    if any(word in lowered for word in ("visa", "credit card", "line of credit")):
        return "credit"
    if any(word in lowered for word in ("checking", "chequing", "banking")):
        return "checking"
    if "saving" in lowered:
        return "savings"
    if any(word in lowered for word in ("tfsa", "rsp", "rrsp", "investment")):
        return "investment"
    return None


def _balance_cents(value) -> int | None:
    return None if value in (None, "") else parse_amount_to_cents(str(value))


def sync_payload(conn: sqlite3.Connection, payload: dict,
                 resolver: Resolver) -> dict[str, IngestResult]:
    """Feed a SimpleFIN /accounts payload through the standard ingest path.
    Partial syncs are safe to re-run — idempotency does the work."""
    results: dict[str, IngestResult] = {}
    institutions = {
        connection.get("conn_id"): connection.get("name", "unknown").strip()
        for connection in payload.get("connections", [])
    }
    for acct in payload.get("accounts", []):
        sfid = acct["id"]
        display = acct.get("name", sfid)
        account_id = map_account(conn, sfid, display, resolver)
        balance_date = acct.get("balance-date")
        if balance_date is not None:
            balance_date = datetime.fromtimestamp(
                balance_date, tz=timezone.utc
            ).isoformat()
        conn.execute(
            "UPDATE accounts SET institution = ?, currency = ?, type = ?,"
            " balance_cents = ?, available_balance_cents = ?, balance_date = ?"
            " WHERE id = ?",
            (
                institutions.get(acct.get("conn_id"), "unknown"),
                acct.get("currency", "USD"),
                _infer_account_type(display),
                _balance_cents(acct.get("balance")),
                _balance_cents(acct.get("available-balance")),
                balance_date,
                account_id,
            ),
        )
        conn.commit()
        txns = []
        for t in acct.get("transactions", []):
            posted = datetime.fromtimestamp(t["posted"], tz=timezone.utc).date()
            txns.append(
                ParsedTxn(
                    posted_date=posted.isoformat(),
                    amount_cents=parse_amount_to_cents(str(t["amount"])),
                    merchant_raw=t["description"],
                    external_id=str(t["id"]),
                )
            )
        results[display] = insert_transactions(
            conn, account_id, txns, source_file=f"simplefin:{sfid}",
            currency=acct.get("currency", "USD"),
        )
    return results


def sync(
    conn: sqlite3.Connection,
    resolver: Resolver,
    since: str | None = None,
) -> tuple[dict[str, IngestResult], list[dict]]:
    """Sync in overlapping, provider-friendly windows and surface errors."""
    today = date.today()
    if since:
        start = date.fromisoformat(since)
    else:
        latest = conn.execute("SELECT MAX(posted_date) FROM transactions").fetchone()[0]
        start = (
            date.fromisoformat(latest) - timedelta(days=SYNC_OVERLAP_DAYS)
            if latest
            else today - timedelta(days=SYNC_WINDOW_DAYS - 1)
        )
    if start > today:
        raise LedgerlineError("sync start date cannot be in the future")

    access_url = access_url_from_env()
    totals: dict[str, IngestResult] = {}
    errors: list[dict] = []
    seen_errors: set[tuple] = set()
    while start <= today:
        end = min(start + timedelta(days=SYNC_WINDOW_DAYS - 1), today)
        payload = fetch_accounts(access_url, start.isoformat(), end.isoformat())
        for error in payload.get("errlist", payload.get("errors", [])) or []:
            normalized = error if isinstance(error, dict) else {"code": "unknown", "msg": str(error)}
            key = (
                normalized.get("code"), normalized.get("msg", normalized.get("message")),
                normalized.get("conn_id"), normalized.get("account_id"),
            )
            if key not in seen_errors:
                seen_errors.add(key)
                errors.append(normalized)
        for label, result in sync_payload(conn, payload, resolver).items():
            total = totals.setdefault(label, IngestResult())
            total.new += result.new
            total.duplicates += result.duplicates
            total.failed += result.failed
        start = end + timedelta(days=1)
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('simplefin_last_success', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (datetime.now(tz=timezone.utc).isoformat(),),
    )
    conn.commit()
    return totals, errors
