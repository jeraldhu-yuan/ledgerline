ALTER TABLE accounts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'unknown'
  CHECK(purpose IN ('personal', 'business', 'mixed', 'unknown'));
ALTER TABLE accounts ADD COLUMN entity_name TEXT;
ALTER TABLE accounts ADD COLUMN business_use_percent INTEGER
  CHECK(business_use_percent BETWEEN 0 AND 100);
ALTER TABLE accounts ADD COLUMN context_note TEXT;
CREATE INDEX idx_accounts_purpose ON accounts(purpose);
