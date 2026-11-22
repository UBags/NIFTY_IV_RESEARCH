-- ADDITIVE ONLY. Never alters, indexes, or locks market_data.*.
CREATE SCHEMA IF NOT EXISTS research_v2;
CREATE TABLE IF NOT EXISTS research_v2.schema_migrations (
    version TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_v2.experiments (
    experiment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_name TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    parameters JSONB NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'created',
    notes TEXT,
    CHECK (lifecycle IN ('created','complete','failed'))
);
CREATE TABLE IF NOT EXISTS research_v2.experiment_results (
    experiment_id BIGINT NOT NULL REFERENCES research_v2.experiments(experiment_id) ON DELETE CASCADE,
    strategy_key TEXT NOT NULL,
    sample_name TEXT NOT NULL,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id,strategy_key,sample_name)
);
CREATE TABLE IF NOT EXISTS research_v2.contract_lot_overrides (
    symbol TEXT NOT NULL DEFAULT 'NIFTY',
    trading_day DATE NOT NULL,
    expiry DATE NOT NULL,
    lot_size INTEGER NOT NULL CHECK (lot_size > 0),
    evidence TEXT NOT NULL,
    PRIMARY KEY (symbol,trading_day,expiry)
);
CREATE TABLE IF NOT EXISTS research_v2.data_quality_summary (
    experiment_id BIGINT NOT NULL REFERENCES research_v2.experiments(experiment_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    trading_month DATE NOT NULL,
    checks JSONB NOT NULL,
    PRIMARY KEY (experiment_id,provider,trading_month)
);
COMMENT ON SCHEMA research_v2 IS 'NIFTY options research; intentionally independent of active market_data imports';
