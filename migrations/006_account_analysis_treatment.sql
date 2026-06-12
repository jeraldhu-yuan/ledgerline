ALTER TABLE accounts ADD COLUMN analysis_treatment TEXT NOT NULL DEFAULT 'include'
  CHECK(analysis_treatment IN ('include', 'monitor_only', 'exclude'));

CREATE INDEX idx_accounts_analysis_treatment ON accounts(analysis_treatment);
