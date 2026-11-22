#!/usr/bin/env bash
set -euo pipefail
psql -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" -U "${PGUSER:-postgres}" \
  -d "${PGDATABASE:-nifty_data}" -v ON_ERROR_STOP=1 <<'SQL'
SELECT dataset, market_kind, status, count(*) AS files,
 sum(loaded_rows) AS loaded_rows,sum(expected_rows) AS expected_rows,
 round(100.0*sum(loaded_rows)/nullif(sum(expected_rows),0),2) AS imported_pct
FROM market_data.import_files
WHERE relative_path LIKE 'NIFTY_20%.parquet'
 OR relative_path IN ('NIFTY.parquet','india-index-options-1m/index/NIFTY.parquet')
 OR relative_path LIKE 'india-index-options-1m/options/NIFTY/%'
GROUP BY dataset,market_kind,status ORDER BY dataset,market_kind,status;
SQL
