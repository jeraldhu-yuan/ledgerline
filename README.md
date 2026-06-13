# ledgerline

[![CI](https://github.com/jeraldhu-yuan/ledgerline/actions/workflows/ci.yml/badge.svg)](https://github.com/jeraldhu-yuan/ledgerline/actions/workflows/ci.yml)

Give AI agents read-only access to your finances without giving anyone your
data: one SQLite file on your machine, no cloud, exact integer-cent answers
over MCP.

Everything runs locally. Bank access (via SimpleFIN Bridge) is read-only by
construction — Ledgerline never sees your banking credentials and cannot
move money. Account numbers are dropped at parse time, so the model can
never see what the database never contains. Delete the one `.db` file and
every trace is gone.

![ledgerline demo: seed fabricated data, then summary and upcoming](docs/demo.gif)

<!-- Re-record with vhs: see docs/demo.tape -->

## Try it in 90 seconds

No clone, no signup, no API key, no real financial data — the demo seeds six
months of clearly fabricated transactions so you can evaluate everything
before connecting anything. [uv](https://docs.astral.sh/uv/getting-started/installation/)
is the only prerequisite.

```sh
uvx --from ledgerline ledgerline demo
uvx --from ledgerline ledgerline summary    # income/outflow by category
uvx --from ledgerline ledgerline upcoming   # expected charges, next 30 days
```

`demo` prints copy-paste one-liners that connect the MCP server to Codex or
Claude Code; then ask things like "What recurring charges are coming up?" or
"Why was last month so expensive?". When you're done evaluating, delete
`data/ledgerline.db` and start fresh with real data below. (`demo` refuses
to write into a database that already has transactions.)

## Quick start with real data

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

`connect` stores the resulting access URL owner-only in
`~/.config/ledgerline/simplefin.env`. The first `sync` prompts to map each
bank account to a local label; re-running is always safe, and a stale
database catches up in provider-friendly 45-day windows. If an institution
is missing from SimpleFIN's catalog, that account just stays on file
import — mixing both paths is a supported steady state.

**File import.** Download a CSV/OFX/QFX export from your bank's website:

```sh
uv run ledgerline ingest export.csv --account "Checking"
```

The database lives at `data/ledgerline.db` (gitignored); override with
`--db` or `LEDGERLINE_DB`. No API key is needed for any of this — the two
optional embedded LLM commands (`categorize`, `ask`) read
`ANTHROPIC_API_KEY` from the environment, and everything else runs keyless.

## AI agent access (recommended)

Ledgerline runs as a local stdio MCP server exposing read-only tools: data
freshness, transaction search, spending summaries, period comparisons,
account balances, upcoming payments, and constrained SQL. The contract is
deliberately small and uniform — exact integer cents, totals always per
currency and never combined, and limitations (staleness, uncategorized
spend, unknown account purpose) reported as data rather than prescriptive
workflow text. The reasoning is the client model's job; the server's job is
exact, truthful primitives.

The one cache-writing tool, `refresh_data`, pulls from SimpleFIN at most
once an hour. A refresh that hits provider errors is recorded as an attempt
but not a success, and `data_status` discloses the difference.

```sh
# Codex (user scope)
codex mcp add ledgerline --env LEDGERLINE_DB=/absolute/path/to/ledgerline.db -- \
  uvx --from ledgerline ledgerline-mcp

# Claude Code (user scope)
claude mcp add --scope user --transport stdio \
  --env LEDGERLINE_DB=/absolute/path/to/ledgerline.db ledgerline -- \
  uvx --from ledgerline ledgerline-mcp
```

(From a repo checkout, point the command at
`/path/to/ledgerline/.venv/bin/ledgerline-mcp` instead of `uvx`.) Restart
the client, then ask things like "How much did I spend on dining in
January?" or "What recurring charges are coming up?"

## Usage

```sh
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

# Embedded Q&A for use without an MCP client (needs ANTHROPIC_API_KEY)
uv run ledgerline ask "why was June so expensive?"

# CSV dump for analysis elsewhere
uv run ledgerline export --month 2026-06 --out june.csv

# Durable account context for agents and reports
uv run ledgerline accounts set-context "Chequing" --purpose mixed \
  --entity "Northwind Consulting" --business-use-percent 70 \
  --context "Business income plus personal spending"
```

Account context (`personal`/`business`/`mixed`/`unknown`, owning entity,
business-use percentage, free-form note) persists in SQLite and rides along
on every MCP result, so agents segment cash flow before judging it.

## Contributing a bank profile

If your bank's CSV doesn't auto-detect, the fix is a ~10-line pull request:
add one dict to `PROFILES` in
[`ledgerline/ingest/profiles.py`](ledgerline/ingest/profiles.py). OFX/QFX
needs no profile.

```python
"us_checking": {
    "columns": {"date": "Posting Date", "amount": "Amount", "description": "Description"},
    "date_format": "%m/%d/%Y",
    "sign": 1,            # -1 if the export shows charges as positive
    "skip_rows": 0,
    "external_id_column": None,  # column with a bank-side unique id, if any
},
```

Include a small fabricated CSV fixture (invented merchants, never real
account data) in `tests/fixtures/` and a test asserting it ingests with the
right sign convention — see `test_sign_convention_profile` in
[`tests/test_ingest.py`](tests/test_ingest.py) for the pattern.

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
  `*.ipynb` gitignored from the first commit; test fixtures and `demo`
  data are fabricated only.
- Account numbers are never parsed: the OFX reader and SimpleFIN connector
  drop `ACCTID`/`BANKID`-class fields at parse time. Only short labels
  ("US Checking") identify accounts. Asserted in `tests/test_security.py`.
- The model gets full transaction detail through `run_sql` — by design. What
  it can never see is what the DB never contains: account numbers,
  credentials, raw export files.
- `run_sql`: read-only connection (`mode=ro` URI), single-statement
  SELECT/WITH only, keyword denylist, SQLite authorizer denying everything
  but reads, 200-row cap, 5-second time limit, statement/result size limits.
  Literals and comments are stripped before the keyword scan (a merchant
  named "UPDATE" is not a false positive); the authorizer and read-only mode
  are the real guards. Tested with hostile inputs.
- SimpleFIN access URL from `SIMPLEFIN_ACCESS_URL` or a `0600` config file
  only — never the repo, the DB, or the LLM context. `https` is required,
  HTTP redirects are refused (credentials are never replayed to another
  host), and loose file permissions produce a warning.
- New database files are created owner-only (`0600`).
- `ANTHROPIC_API_KEY` from env only; LLM steps fail loudly without it,
  everything else runs keyless.

## Tests

```sh
uv run pytest
```

The suite covers the acceptance checklist: mixed-mode dedupe in both
orders, quarantine of malformed rows, integer-cents math, per-currency
reporting, `run_sql` hardening against hostile inputs, recurring detection
with gap tolerance, the MCP tools, the demo seeder, and the security
invariants above.

## License

MIT — see [LICENSE](LICENSE).
