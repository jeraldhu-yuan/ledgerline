# ledgerline

Local-first personal finance pipeline: ingest bank exports into SQLite,
categorize with LLM assistance (cached), detect recurring payments, and ask
questions about spending in natural language. Single user, no cloud, no live
bank credentials — optional read-only sync via SimpleFIN Bridge.

## Setup

```sh
uv sync
export ANTHROPIC_API_KEY=sk-...   # only needed for `categorize` and `ask`
```

Everything except the two LLM features works with no API key at all.
The database lives at `data/ledgerline.db` (gitignored); override with
`--db` or `LEDGERLINE_DB`.

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

# Agentic Q&A over the full history (read-only SQL tool loop)
uv run ledgerline ask "why was June so expensive?"

# CSV dump for analysis elsewhere
uv run ledgerline export --month 2026-06 --out june.csv

# SimpleFIN Bridge sync (see below)
uv run ledgerline sync --since 2026-05-01
```

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

- `data/`, `*.db`, `*.csv`, `*.ofx`, `*.qfx`, `.env` gitignored from the
  first commit; test fixtures are fabricated data only.
- Account numbers are never parsed: the OFX reader and SimpleFIN connector
  drop `ACCTID`/`BANKID`-class fields at parse time. Only short labels
  ("US Checking") identify accounts. Asserted in `tests/test_security.py`.
- The model gets full transaction detail through `run_sql` — by design. What
  it can never see is what the DB never contains: account numbers,
  credentials, raw export files.
- `run_sql`: read-only connection (`mode=ro` URI), single-statement
  SELECT/WITH only, keyword denylist, SQLite authorizer denying everything
  but reads, 200-row cap enforced server-side. Tested with hostile inputs.
- SimpleFIN access URL from `SIMPLEFIN_ACCESS_URL` or `.env` only — never
  the repo, the DB, or the LLM context.
- `ANTHROPIC_API_KEY` from env only; LLM steps fail loudly without it,
  everything else runs keyless.

## SimpleFIN sync (M3)

1. **Precondition:** check your institutions are covered in the SimpleFIN/MX
   catalog (claim a token at <https://bridge.simplefin.org> and look at the
   institution search before relying on it). Your institution may be
   niche — if absent, that account stays on OFX/CSV import permanently, and
   mixed-mode is a supported steady state, not a fallback.
2. Claim the setup token via the Bridge web flow, exchange it for an access
   URL, link institutions in the Bridge UI.
3. `export SIMPLEFIN_ACCESS_URL=https://...` (or put it in `.env`).
4. `uv run ledgerline sync` — first sync prompts to map each SimpleFIN
   account to a local label; partial syncs are safe to re-run.

## Tests

```sh
uv run pytest
```

76 tests cover the acceptance checklist: double-import and overlap
idempotency, mixed-mode dedupe in both orders, quarantine of malformed rows,
integer-cents money math, keyless operation, `run_sql` hardening, recurring
detection + manual groups, and the no-account-numbers/no-tokens invariants.
