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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ledgerline import LedgerlineError
from ledgerline.ingest import get_or_create_account, insert_transactions
from ledgerline.ingest.types import IngestResult, ParsedTxn
from ledgerline.money import parse_amount_to_cents

# resolver(simplefin_id, display_name) -> local account label
Resolver = Callable[[str, str], str]


def access_url_from_env() -> str:
    url = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if not url:
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("SIMPLEFIN_ACCESS_URL="):
                    url = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not url:
        raise LedgerlineError(
            "SIMPLEFIN_ACCESS_URL is not set (env var or .env). Claim a setup "
            "token at https://bridge.simplefin.org, exchange it for an access "
            "URL, and store it there."
        )
    return url


def fetch_accounts(access_url: str, since: str | None = None) -> dict:
    """GET /accounts from the SimpleFIN access URL (credentials embedded in
    the URL's userinfo, sent as HTTP basic auth)."""
    parts = urllib.parse.urlsplit(access_url)
    if "@" in parts.netloc:
        creds, host = parts.netloc.rsplit("@", 1)
    else:
        creds, host = None, parts.netloc
    url = urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path.rstrip("/") + "/accounts", "", "")
    )
    if since:
        start = int(
            datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()
        )
        url += f"?start-date={start}"
    req = urllib.request.Request(url)
    if creds:
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


def sync_payload(conn: sqlite3.Connection, payload: dict,
                 resolver: Resolver) -> dict[str, IngestResult]:
    """Feed a SimpleFIN /accounts payload through the standard ingest path.
    Partial syncs are safe to re-run — idempotency does the work."""
    results: dict[str, IngestResult] = {}
    for acct in payload.get("accounts", []):
        sfid = acct["id"]
        display = acct.get("name", sfid)
        account_id = map_account(conn, sfid, display, resolver)
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


def sync(conn: sqlite3.Connection, resolver: Resolver,
         since: str | None = None) -> dict[str, IngestResult]:
    payload = fetch_accounts(access_url_from_env(), since=since)
    return sync_payload(conn, payload, resolver)
