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
import sys
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SimpleFIN endpoints have no business redirecting; urllib would replay
    the basic-auth Authorization header to the redirect target, so a redirect
    must fail loudly instead of leaking credentials to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _url_from_file(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        print(
            f"warning: {path} is readable by other users; run: chmod 600 {path}",
            file=sys.stderr,
        )
    for line in path.read_text().splitlines():
        if line.strip().startswith("SIMPLEFIN_ACCESS_URL="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def access_url_from_env() -> str:
    url = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if not url:
        url = _url_from_file(PROTECTED_ENV_FILE)
    if not url:
        raise LedgerlineError(
            "no SimpleFIN access URL is configured. Run `ledgerline connect` "
            "to set up bank sync (or set SIMPLEFIN_ACCESS_URL)."
        )
    return url


def claim_setup_token(token: str) -> str:
    """Exchange a one-time SimpleFIN setup token for the durable access URL.

    A setup token is the base64-encoded claim URL; POSTing to the claim URL
    (no auth, empty body) returns the access URL and invalidates the token.
    """
    compact = "".join(token.split())  # browser copy/paste wraps long tokens
    try:
        claim_url = base64.b64decode(compact, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise LedgerlineError(
            "that does not look like a SimpleFIN setup token (expected base64)"
        ) from exc
    if not claim_url.startswith("https://"):
        raise LedgerlineError("setup token must decode to an https claim URL")
    req = urllib.request.Request(claim_url, data=b"", method="POST")
    req.add_header("User-Agent", "Ledgerline/0.1")
    try:
        with _OPENER.open(req, timeout=60) as resp:
            access_url = resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        raise LedgerlineError(
            f"SimpleFIN claim failed with HTTP {e.code}: {e.reason}. "
            "Setup tokens are single-use — create a fresh one and retry."
        ) from e
    except urllib.error.URLError as e:
        raise LedgerlineError(f"could not reach SimpleFIN: {e.reason}") from e
    parts = urllib.parse.urlsplit(access_url)
    if parts.scheme != "https" or not parts.hostname:
        raise LedgerlineError("SimpleFIN returned an invalid access URL")
    return access_url


def store_access_url(access_url: str, path: Path = PROTECTED_ENV_FILE) -> Path:
    """Write the access URL to the protected config file, owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"SIMPLEFIN_ACCESS_URL={access_url}\n")
    os.chmod(path, 0o600)  # tighten pre-existing files too
    return path


def fetch_accounts(
    access_url: str,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """GET /accounts from the SimpleFIN access URL (credentials embedded in
    the URL's userinfo, sent as HTTP basic auth)."""
    parts = urllib.parse.urlsplit(access_url)
    if parts.scheme != "https":
        raise LedgerlineError(
            "SimpleFIN access URL must use https; refusing to send credentials in plaintext"
        )
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
        with _OPENER.open(req, timeout=60) as resp:
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
    # An attempt that reached the provider is always recorded (the refresh
    # rate limit keys off it), but "last success" stays truthful: it is only
    # advanced when the provider reported no errors.
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    _set_sync_state(conn, "simplefin_last_attempt", now_iso)
    if not errors:
        _set_sync_state(conn, "simplefin_last_success", now_iso)
    conn.commit()
    return totals, errors


def _set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
