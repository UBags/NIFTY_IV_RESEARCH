# NIFTY Parquet — structure inspection

Generated (UTC): 2026-11-08T08:42:48+00:00
Dataset root: `/home/uddipan/NIFTY Data`
Inspection scope: `all`
Files selected: **3339**
Files successfully inspected: **3339**
Unreadable files: **0**
Distinct Arrow schemas: **5**
Total rows (sum across files; sources may overlap): **501,457,446**

## Sources / subdirectories

| Source group | Files | Rows | Size (MiB) | Schema IDs |
|---|---:|---:|---:|---|
| india-index-options-1m | 3065 | 267,036,074 | 2,900.7 | 11f4e9871761, 4f6dff38cf0b, 8160ee3d8dd4 |
| rissin_upstox_options | 3 | 124,458,249 | 1,154.4 | f0165d43a0e7 |
| spot_or_unknown_source | 1 | 486,050 | 8.0 | 2e60f8f43d10 |
| thetrademarkk_nifty_options | 267 | 108,139,447 | 890.6 | 11f4e9871761 |
| thetrademarkk_spot | 3 | 1,337,626 | 23.1 | 2e60f8f43d10 |

## Schema details

### Schema `11f4e9871761` — 3199 files

Example file: `india-index-options-1m/options/BANKNIFTY/2021-05-27.parquet`

| Column | Arrow type | Nullable |
|---|---|---|
| `timestamp` | `timestamp[ns, tz=+05:30]` | True |
| `open` | `double` | True |
| `high` | `double` | True |
| `low` | `double` | True |
| `close` | `double` | True |
| `volume` | `int64` | True |
| `open_interest` | `int64` | True |
| `trading_day` | `string` | True |
| `symbol` | `string` | True |
| `strike` | `int32` | True |
| `option_type` | `string` | True |
| `expiry` | `string` | True |

### Schema `8160ee3d8dd4` — 126 files

Example file: `india-index-options-1m/stocks_options/ADANIPOWER/2026-04-28.parquet`

| Column | Arrow type | Nullable |
|---|---|---|
| `timestamp` | `timestamp[ns, tz=+05:30]` | True |
| `open` | `double` | True |
| `high` | `double` | True |
| `low` | `double` | True |
| `close` | `double` | True |
| `volume` | `int64` | True |
| `open_interest` | `int64` | True |
| `trading_day` | `string` | True |
| `symbol` | `string` | True |
| `strike` | `double` | True |
| `option_type` | `string` | True |
| `expiry` | `string` | True |

### Schema `4f6dff38cf0b` — 7 files

Example file: `india-index-options-1m/mcx/ALUMINI/2026-06-30/MCX-ALUMINI-30Jun26-FUT.parquet`

| Column | Arrow type | Nullable |
|---|---|---|
| `timestamp` | `string` | True |
| `open` | `double` | True |
| `high` | `double` | True |
| `low` | `double` | True |
| `close` | `double` | True |
| `volume` | `double` | True |
| `open_interest` | `double` | True |
| `trading_day` | `string` | True |
| `exchange` | `string` | True |
| `segment` | `string` | True |
| `underlying` | `string` | True |
| `groww_symbol` | `string` | True |
| `trading_symbol` | `string` | True |
| `instrument_type` | `string` | True |
| `expiry` | `string` | True |
| `strike` | `double` | True |
| `lot_size` | `string` | True |
| `tick_size` | `string` | True |

### Schema `2e60f8f43d10` — 4 files

Example file: `NIFTY.parquet`

| Column | Arrow type | Nullable |
|---|---|---|
| `timestamp` | `timestamp[ns, tz=+05:30]` | True |
| `open` | `double` | True |
| `high` | `double` | True |
| `low` | `double` | True |
| `close` | `double` | True |
| `volume` | `int64` | True |
| `trading_day` | `string` | True |
| `symbol` | `string` | True |

### Schema `f0165d43a0e7` — 3 files

Example file: `NIFTY_2024.parquet`

| Column | Arrow type | Nullable |
|---|---|---|
| `date` | `large_string` | True |
| `timestamp` | `timestamp[us, tz=+05:30]` | True |
| `underlying` | `large_string` | True |
| `expiry` | `large_string` | True |
| `strike` | `double` | True |
| `option_type` | `large_string` | True |
| `exercise_style` | `large_string` | True |
| `open` | `double` | True |
| `high` | `double` | True |
| `low` | `double` | True |
| `close` | `double` | True |
| `volume` | `int64` | True |
| `oi` | `int64` | True |
| `settle_price` | `null` | True |
| `source` | `large_string` | True |
| `granularity` | `large_string` | True |

## Files containing timestamps/strike/expiry

File-level minimum and maximum values, where available from Parquet metadata,
are in `FILE_INVENTORY.csv`. These are metadata statistics, not full-content
quality checks. Blank values mean no usable metadata statistics.

## Sample rows

`SAMPLE_ROWS.json` includes small examples from a few files per schema.
Sampling does **not** certify 09:45 coverage, valid quotes, correct timezone
assumptions, long-dated contract availability or completeness.

## Important notes for the eventual PostgreSQL import

- Do not import yet; review file overlap and column types first.
- Two independent spot-index sources may contain duplicate observations.
- The source dataset's README warns that options coverage is partial.
- For an IV backtest, we must later verify matching 09:45 IST spot and
  weekly/60–120-DTE option contracts rather than trusting file names alone.
- Preserve timezone and original source/expiry to avoid accidental merges.

## Please upload these reports

1. `SUMMARY.md`
2. `SCHEMA_CATALOG.json`
3. `SAMPLE_ROWS.json`
4. `FILE_INVENTORY.csv`
5. `ERRORS.json` if any files could not be read
