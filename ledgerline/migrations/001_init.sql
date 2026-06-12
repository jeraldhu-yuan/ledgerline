CREATE TABLE accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,         -- "US Checking", "US Visa"
  institution TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  type TEXT CHECK(type IN ('checking','savings','credit','investment'))
);

CREATE TABLE recurring_groups (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,               -- "Brightstone Training installment"
  expected_amount_cents INTEGER,
  cadence TEXT CHECK(cadence IN ('monthly','weekly','annual','irregular')),
  expected_day INTEGER,              -- day-of-month if monthly
  merchant_clean TEXT,               -- links group to transactions by merchant
  active INTEGER DEFAULT 1
);

CREATE TABLE transactions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  posted_date TEXT NOT NULL,         -- ISO 8601
  amount_cents INTEGER NOT NULL,     -- negative = outflow; NEVER floats for money
  currency TEXT NOT NULL,
  merchant_raw TEXT NOT NULL,        -- exactly as the bank exported it
  merchant_clean TEXT,               -- normalized display name
  category TEXT,                     -- from the fixed taxonomy
  recurring_group_id INTEGER REFERENCES recurring_groups(id),
  source_file TEXT NOT NULL,
  -- sha256 over (account_id, posted_date, amount_cents, merchant_raw, occurrence_index).
  -- The occurrence index disambiguates genuinely distinct same-day/same-amount/
  -- same-merchant transactions; bank-provided ids live in external_id below so
  -- file imports and SimpleFIN sync of the same period dedupe against each other.
  dedupe_hash TEXT NOT NULL UNIQUE,
  external_id TEXT,                  -- FITID (OFX) or SimpleFIN transaction id
  imported_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_txn_external
  ON transactions(account_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX idx_txn_account_date ON transactions(account_id, posted_date);
CREATE INDEX idx_txn_base
  ON transactions(account_id, posted_date, amount_cents, merchant_raw);
CREATE INDEX idx_txn_merchant ON transactions(merchant_clean);

CREATE TABLE merchant_category_cache (
  merchant_clean TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  source TEXT CHECK(source IN ('rule','llm','manual')),
  confirmed INTEGER DEFAULT 0       -- 1 once manually confirmed
);

-- Rows that failed to parse are never silently dropped
CREATE TABLE quarantine (
  id INTEGER PRIMARY KEY,
  source_file TEXT NOT NULL,
  raw_line TEXT NOT NULL,
  reason TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

-- SimpleFIN account -> local account label mapping (M3).
-- Only SimpleFIN's opaque account id is stored, never bank account numbers.
CREATE TABLE simplefin_account_map (
  simplefin_id TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id)
);
