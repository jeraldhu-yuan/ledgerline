ALTER TABLE recurring_groups ADD COLUMN account_id INTEGER REFERENCES accounts(id);
ALTER TABLE recurring_groups ADD COLUMN currency TEXT;
CREATE INDEX idx_recurring_scope
  ON recurring_groups(account_id, currency, merchant_clean);
