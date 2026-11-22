# NIFTY_OPTIONS — PostgreSQL import guide

## Dataset inspection findings (2026-11-08)

The supplied inspection shows **3,339 readable Parquet files, 5 schemas, and 501,457,446 source rows** (before overlap). The `ERRORS.json` report is empty. Files were inspected when the root was `~/NIFTY Data`; **that recorded path is historical**. This loader uses the new folder `NIFTY_OPTIONS/NIFTY_Data`.

| Data | Files | Source rows |
|---|---:|---:|
| Rissin/Upstox NIFTY options (`NIFTY_2024`–`2026`) | 3 | 124,458,249 |
| TradeMarkk NIFTY options (`options/NIFTY/*.parquet`) | 267 | 108,139,447 |
| NIFTY spot, two copies/paths | 2 | 972,100 |
| **Default `--scope nifty`** | **272** | **233,569,796** |
| **Entire `--scope all`** | **3,339** | **501,457,446** |

The two NIFTY spot files have identical row counts; the inventory does **not** prove their bytes are identical. Both are imported with separate `source_file_id`s. Source overlap is **not** silently removed. The vendor README warns about incomplete option chain coverage; prices with `volume=0` are retained, not presumed executable.

## Folder layout

```text
~/PycharmProjects/NIFTY_OPTIONS/
├── import_nifty_postgres.py
├── NIFTY_Data/
│   ├── NIFTY.parquet
│   ├── NIFTY_2024.parquet
│   ├── NIFTY_2025.parquet
│   ├── NIFTY_2026.parquet
│   └── india-index-options-1m/
│       ├── index/
│       ├── options/NIFTY/
│       ├── options/BANKNIFTY/
│       ├── stocks_options/
│       └── mcx/
└── nifty_inspection_all/
    ├── SUMMARY.md
    ├── SCHEMA_CATALOG.json
    ├── SAMPLE_ROWS.json
    ├── FILE_INVENTORY.csv
    └── ERRORS.json
```

## Dependencies and credentials

Activate your existing PyCharm `.venv` and run:

```bash
python -m pip install -U pyarrow pandas 'psycopg[binary]'
```

The database `nifty_data` is assumed to exist. Set environment variables in your terminal or PyCharm Run Configuration:

```bash
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=nifty_data
export PGUSER=postgres
read -rs -p 'PostgreSQL password: ' PGPASSWORD; echo
export PGPASSWORD
```

Do not put credentials in the Python source file or commit them to Git.

## Recommended execution sequence

**1 — Inspect import plan, without connecting to PostgreSQL**

```bash
python import_nifty_postgres.py --plan
```

Expected: 272 files selected with default `--scope nifty`.

**2 — Create metadata ledger and partitioned tables; no data loaded**

```bash
python import_nifty_postgres.py --init-only
```

Creates schema `market_data` and its four tables: `import_files`, `spot_bars`, `option_bars`, `future_bars`.

**3 — First pilot: at most 100,000 NIFTY options rows from one expiry file**

```bash
python import_nifty_postgres.py \
  --scope nifty \
  --file-glob '*/options/NIFTY/2021-05-27.parquet' \
  --batch-rows 50000 \
  --max-chunks 2
```

A pilot that stops at 100,000 records is **intentionally partial**, but the first two chunks and their ledger positions have committed together. Resume with:

```bash
python import_nifty_postgres.py \
  --scope nifty \
  --file-glob '*/options/NIFTY/2021-05-27.parquet'
```

**4 — Verify pilot ledger and data**

Run in `psql` or pgAdmin:

```sql
SELECT file_id, relative_path, expected_rows, loaded_rows,
       next_row_group, next_row_offset, status, error_message
FROM market_data.import_files
ORDER BY file_id;

SELECT count(*) AS rows_for_pilot
FROM market_data.option_bars
WHERE source_file_id = (
    SELECT file_id FROM market_data.import_files
    WHERE relative_path = 'india-index-options-1m/options/NIFTY/2021-05-27.parquet'
);

SELECT source_file_id, bar_time, trading_day, underlying, expiry,
       strike, option_type, open, close, volume, open_interest
FROM market_data.option_bars
ORDER BY bar_time
LIMIT 10;
```

For a finished pilot, `rows_for_pilot` should equal `loaded_rows`, which should equal `expected_rows`.

**5 — Load all NIFTY data (after pilot is accepted and disk space is confirmed)**

```bash
python import_nifty_postgres.py --scope nifty 2>&1 | tee nifty_import.log
```

A repeated run skips completed files and continues partial files from the last committed position. There is no need to repeat the first pilot data.

**6 — Optional: all other instruments (large)**

```bash
python import_nifty_postgres.py --scope all 2>&1 | tee nifty_import_all.log
```

**7 — Build indexes *after* bulk import**

```bash
python import_nifty_postgres.py --build-indexes
```

Building indexes may be expensive in both time and disk space. Do not run it before loading the large files unless you have a specific reason.

## Table mapping

| Table | Data | Distinguishing fields |
|---|---|---|
| `market_data.import_files` | Per-file ledger/checkpoint | relative path, file identity, next row group/offset, rows loaded, status |
| `market_data.spot_bars` | Index spot minute prices | symbol, bar_time, OHLC, volume |
| `market_data.option_bars` | NIFTY/BankNifty/Sensex/stock option prices | underlying, expiry, strike, option_type, OHLC, volume, OI, original source/granularity |
| `market_data.future_bars` | MCX future prices | expiry, instrument type, contract identifiers, OHLC, volume, OI |

All bars retain `source_file_id` and the original `source_row_index` (0-based), and preserve any provider day field in `provider_trading_day`. Canonical `trading_day` is derived from the timezone-aware bar timestamp in **Asia/Kolkata**. Raw source timestamps are converted to `timestamptz`. Data are partitioned by trading year.

**Important:** Rows from different providers with matching natural keys remain separate. The loader does not claim that provider close prices are tradable bid/ask quotes. We will define source priority and detect disagreements **after** initial import.

## Verify overall load

```sql
SELECT dataset, market_kind, status,
       count(*) AS files, sum(expected_rows) AS expected,
       sum(loaded_rows) AS loaded,
       sum(date_mismatches) AS source_day_disagreements
FROM market_data.import_files
GROUP BY dataset, market_kind, status
ORDER BY dataset, market_kind, status;

SELECT count(*) FILTER (WHERE status='failed') AS failed_files,
       count(*) FILTER (WHERE status='partial') AS partial_files,
       count(*) FILTER (WHERE status='completed') AS complete_files
FROM market_data.import_files;
```

For a quick NIFTY price check after the relevant source files finish:

```sql
SELECT o.bar_time, o.underlying, o.expiry, o.strike,
       o.option_type, o.close, o.volume, f.dataset, f.relative_path
FROM market_data.option_bars o
JOIN market_data.import_files f ON f.file_id=o.source_file_id
WHERE o.underlying='NIFTY'
  AND o.trading_day=DATE '2025-11-05'
  AND (o.bar_time AT TIME ZONE 'Asia/Kolkata')::time = TIME '09:45:00'
  AND o.option_type='PE'
  AND o.strike BETWEEN 25000 AND 26000
ORDER BY o.expiry, o.strike, f.dataset
LIMIT 30;
```

## Operational cautions

- **Storage:** 501 million rows are highly compressed in Parquet; PostgreSQL heap, WAL and indexes can require far more space. Check `df -h`, PostgreSQL's data directory and the filesystem holding it before loading all data. Keep backups as appropriate.
- **First-pass imports are intentionally index-free.** No uniqueness constraint is imposed on the market-bar tables, because source conflicts and intra-file duplicate observations need review first. Import-ledger + single-session advisory lock prevent reprocessing committed chunks when using this importer.
- **File identity:** each file is identified by relative path, byte length, modification timestamp, row group count and expected row count. A file changed after partial loading causes a hard error; it will not be silently blended with its earlier version.
- **Interruption:** `Ctrl+C` or a process crash rolls back an in-flight chunk; all earlier committed chunks remain. Run the command again to resume.
- **Data errors:** missing timestamps/expiry/option type/strike and malformed numerical cells stop that file, with its location recorded in `import_files.error_message`. This is deliberate: the importer does not discard problematic rows silently.
- **No DB server is available in this ChatGPT environment.** The loader is syntax-checked and mapping-tested on representative synthetic records, but it has not undergone a real PostgreSQL COPY integration test on your Ubuntu computer. Run the pilot and share its output before attempting the bulk import.

## After the pilot

Please share (1) the terminal output, (2) the ledger status query result, and (3) the single-file row count result. Once verified, load the complete NIFTY dataset. After that we can assess actual weekly and 60–120 DTE coverage at 9:45 a.m., compute implied volatility, and build source-aware calendar-spread backtests.
