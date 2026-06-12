# ledgerline

[![CI](https://github.com/jeraldhu-yuan/ledgerline/actions/workflows/ci.yml/badge.svg)](https://github.com/jeraldhu-yuan/ledgerline/actions/workflows/ci.yml)

Local-first personal finance pipeline: ingest bank exports into SQLite,
categorize transactions, detect recurring payments, and expose accurate,
read-only finance tools to AI agents through MCP. Single user, no cloud, no
live bank credentials — optional read-only sync via SimpleFIN Bridge.

## Quick start

[uv](https://docs.astral.sh/uv/getting-started/installation/) is the only
prerequisite.

```sh
git clone https://github.com/jeraldhu-yuan/ledgerline
cd ledgerline
uv sync
```

Then get transactions in. Both paths work, and they can be mixed freely —
the importer deduplicates.

**Bank sync.** Sign up at <https://bridge.simplefin.org> (SimpleFIN Bridge,
a small paid service that turns your bank logins into read-only transaction
feeds — Ledgerline never sees your banking credentials), link your bank(s),
and create a new app on your account page to get a one-time setup token.
Then:

```sh
uv run ledgerline connect    # paste the setup token when prompted
uv run ledgerline sync       # pull your transactions
```

`connect` exchanges the token for an access URL and stores it owner-only in
`~/.config/ledgerline/simplefin.env`. Nothing else ever reads it.

**File import.** Download a CSV/OFX/QFX export from your bank's website:

```sh
uv run ledgerline ingest export.csv --account "Checking"
```

The database lives at `data/ledgerline.db` (gitignored); override with
`--db` or `LEDGERLINE_DB`. No API key is needed for any of this — the two
optional embedded LLM commands (`categorize`, `ask`) read
`ANTHROPIC_API_KEY` from the environment, and everything else runs keyless.

## AI agent access (recommended)

Ledgerline runs as a local stdio MCP server. It exposes read-only tools for
data freshness, transaction search, spending summaries, period comparisons,
account balances, upcoming payments, and constrained SQL. Tools return exact
integer-cents data, never add different currencies together, and warn agents
when history is stale, incomplete, or uncategorized.

`refresh_data` can update the local cache from SimpleFIN when current data is
needed. SimpleFIN itself remains read-only; this tool only writes the local
SQLite cache and rate-limits refresh attempts to once per hour by default. A
refresh that completes with provider errors is recorded as an attempt but not
a success, and `data_status` discloses the difference.

The tool contract is deliberately small and uniform: every money figure is an
exact integer-cent value scoped to one currency (plus a formatted string),
totals are always lists keyed by currency, and limitations (staleness,
uncategorized spend, unknown account purpose) are reported as data rather
than baked into prescriptive workflow text. The reasoning is the client
model's job; the server's job is exact, truthful primitives.

```sh
# Codex (user scope)
codex mcp add ledgerline -- /absolute/path/to/ledgerline/.venv/bin/ledgerline-mcp

# Claude Code (user scope)
claude mcp add --scope user --transport stdio ledgerline -- \
  /absolute/path/to/ledgerline/.venv/bin/ledgerline-mcp
```

Restart the client after registration, then ask questions such as “How much
did I spend on dining in January?” or “What recurring charges are coming up?”
The agent should call `data_status` first and disclose whether the local data
actually covers the requested period.

## Usage

```sh
# Import a bank export (CSV profile auto-detected, OFX/QFX sniffed)
uv run ledgerline ingest data/raw/export.csv --account "US Checking"
# -> 8 new / 0 duplicate / 0 failed rows

# Monthly summary: income/outflow by category, top merchants, deltas
uv run ledgerline summary --month 2026-06

# Resolve uncached merchants with ONE batched LLM call
uv run ledgerline categorize

# Confirm/correct categories; corrections apply retroactively
uv run ledgerline review

# Recurring payments
uv run ledgerline recurring detect
uv run ledgerline recurring add --label "Course tuition installment" \
    --amount 850.00 --cadence monthly --day 21
uv run ledgerline upcoming --days 30

# Optional legacy embedded Q&A (requires ANTHROPIC_API_KEY; MCP is preferred)
uv run ledgerline ask "why was June so expensive?"

# CSV dump for analysis elsewhere
uv run ledgerline export --month 2026-06 --out june.csv

# SimpleFIN Bridge sync (see below)
uv run ledgerline sync --since 2026-05-01

# Durable account context for agents and reports
uv run ledgerline accounts set-context "Business VISA" --purpose business \
  --entity "Northwind Consulting" --context "Professional expenses and reimbursable travel"
uv run ledgerline accounts set-context "Chequing" --purpose mixed \
  --business-use-percent 70 --context "Practice receipts plus personal debt payments"
```

Account context persists in SQLite and is exposed through MCP. Accounts can be
marked `personal`, `business`, `mixed`, or `unknown`, with an optional owning
entity, business-use percentage, and free-form note. This lets agents segment
cash flow before judging spending or estimating business income.

## Adding a bank profile

A profile is a small dict in `ledgerline/ingest/profiles.py`: column names for
date/amount/description, the date format, the sign convention (some banks
export debits as positive), and rows to skip. Two examples ship configured.

## Idempotency

Re-importing a file, overlapping export ranges, and sync + file import of the
same period all produce zero duplicates (tested in `tests/test_ingest.py` and
`tests/test_sync.py`).

**Design note — one deliberate deviation from the spec:** the spec folds
FITID into `dedupe_hash` when present. Done literally, that would *create*
duplicates in mixed mode: a CSV row (no FITID) and a SimpleFIN row (with id)
for the same transaction would hash differently. Instead:

- `dedupe_hash = sha256(account_id | posted_date | amount_cents | merchant_raw | occurrence_index)`
  with occurrence counting — the Nth identical row in a batch is a duplicate
  only if the DB already holds more than N such rows. Two genuinely distinct
  same-day, same-amount, same-merchant transactions survive because they
  arrive in the same export with occurrence indexes 0 and 1.
- Bank-side ids (OFX FITID, SimpleFIN txn id) are stored in `external_id`
  with a unique per-account index, short-circuit re-imports, and are
  backfilled onto rows that originally arrived without one.

This satisfies every acceptance test, including both orders of mixed-mode.
Caveat: cross-source dedupe matches on the raw description, so it works when
both sources export the same description string (typical for OFX/SimpleFIN
from the same institution).

## Security invariants

- `data/`, `*.db`, `*.csv`, `*.ofx`, `*.qfx`, `.env`, `analysis/`, and
  `*.ipynb` gitignored from the first commit; test fixtures are fabricated
  data only.
- Account numbers are never parsed: the OFX reader and SimpleFIN connector
  drop `ACCTID`/`BANKID`-class fields at parse time. Only short labels
  ("US Checking") identify accounts. Asserted in `tests/test_security.py`.
- The model gets full transaction detail through `run_sql` — by design. What
  it can never see is what the DB never contains: account numbers,
  credentials, raw export files.
- `run_sql`: read-only connection (`mode=ro` URI), single-statement
  SELECT/WITH only, keyword denylist, SQLite authorizer denying everything
  but reads, 200-row cap, a 5-second time limit, and statement/result size
  limits enforced server-side. String literals and comments are stripped
  before the keyword scan, so a merchant named "UPDATE" doesn't
  false-positive — the authorizer and read-only mode remain the real
  guards. Tested with hostile inputs.
- SimpleFIN access URL from `SIMPLEFIN_ACCESS_URL` or a `0600` config file
  only — never the repo, the DB, or the LLM context. `https` is required,
  HTTP redirects are refused (credentials are never replayed to another
  host), and loose file permissions produce a warning.
- New database files are created owner-only (`0600`).
- `ANTHROPIC_API_KEY` from env only; LLM steps fail loudly without it,
  everything else runs keyless.

## Bank sync notes

Check that your institutions appear in SimpleFIN's catalog before relying
on sync — smaller or regional institutions may be missing. Any account not
covered simply stays on OFX/CSV import; mixed-mode is a supported steady
state, not a fallback.

The first `sync` prompts to map each SimpleFIN account to a local label.
Partial syncs are safe to re-run, and later syncs resume from local history
with overlap, using provider-friendly 45-day windows so a stale database
can catch up without gaps.

## Tests

```sh
uv run pytest
```

Tests cover the acceptance checklist: double-import and overlap
idempotency, mixed-mode dedupe in both orders, quarantine of malformed rows,
integer-cents money math, per-currency summaries, keyless operation,
`run_sql` hardening (hostile inputs, literals, time limit), recurring
detection with gap tolerance, manual groups, MCP query tools, sync
attempt-vs-success bookkeeping, and the no-account-numbers/no-tokens/0600
invariants.

## License

MIT — see [LICENSE](LICENSE).
