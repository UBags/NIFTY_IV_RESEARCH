-- ADDITIVE ONLY. No ALTER/CREATE INDEX/LOCK on market_data.*
CREATE SCHEMA IF NOT EXISTS research_v3;
CREATE TABLE IF NOT EXISTS research_v3.migration_history (
  version TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_v3.experiments (
  experiment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL,engine_version TEXT NOT NULL DEFAULT '3',
  provider TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  source_fingerprint TEXT NOT NULL,configuration JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  CHECK(status IN ('created','completed','failed','invalid'))
);
CREATE TABLE IF NOT EXISTS research_v3.quality_summary (
  experiment_id BIGINT NOT NULL REFERENCES research_v3.experiments(experiment_id) ON DELETE CASCADE,
  provider TEXT NOT NULL,calendar_month DATE NOT NULL,
  observations JSONB NOT NULL,PRIMARY KEY(experiment_id,provider,calendar_month)
);
CREATE TABLE IF NOT EXISTS research_v3.strategy_rankings (
  experiment_id BIGINT NOT NULL REFERENCES research_v3.experiments(experiment_id) ON DELETE CASCADE,
  rank INTEGER NOT NULL CHECK(rank>0),strategy JSONB NOT NULL,
  train JSONB NOT NULL,holdout JSONB NOT NULL,
  evidence_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(experiment_id,rank)
);
CREATE TABLE IF NOT EXISTS research_v3.contract_lot_overrides (
  symbol TEXT NOT NULL DEFAULT 'NIFTY',trading_day DATE NOT NULL,
  expiry DATE NOT NULL,lot_size INTEGER NOT NULL CHECK(lot_size>0),
  provenance TEXT NOT NULL,PRIMARY KEY(symbol,trading_day,expiry)
);
CREATE TABLE IF NOT EXISTS research_v3.official_expiry_settlements (
  symbol TEXT NOT NULL DEFAULT 'NIFTY',expiry DATE NOT NULL,
  settlement_price NUMERIC(18,5) NOT NULL CHECK(settlement_price>0),
  evidence_url TEXT NOT NULL,verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(symbol,expiry)
);
COMMENT ON SCHEMA research_v3 IS 'Additive research metadata; does not alter imported market_data';
