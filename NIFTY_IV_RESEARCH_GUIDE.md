# NIFTY relative-IV calendar research — executable kit, revision 1

## Purpose and current state

This is a **research prototype** for systematic NIFTY put calendars, not a production trading system or evidence of profitability. It reads the **existing PostgreSQL `market_data` tables** created by `import_nifty_postgres.py`. No program in this kit modifies PostgreSQL. The database is read only; local research files are created under the PyCharm project.

The installed tables use `market_data.option_bars`, `spot_bars` and `import_files`. Option dates are `trading_day`, observations have `bar_time TIMESTAMPTZ`, expiries have `expiry DATE`, and source provenance is `source_file_id -> import_files.dataset`.

**Do not run the SQLite extraction until the NIFTY import finishes.** The extractor compares the downloaded files with the database import ledger and refuses to create a misleading snapshot while files are unregistered/partial/failed.

## Project layout

Unzip the files **directly inside** `~/PycharmProjects/NIFTY_OPTIONS/`:

```
NIFTY_OPTIONS/
├── .venv/
├── NIFTY_Data/                         # existing downloads
├── nifty_inspection_all/                # existing inspection reports
├── import_nifty_postgres.py             # existing loader
├── prepare_research_cache.py            # new: PostgreSQL -> SQLite filtered snapshot
├── audit_coverage.py                    # new: per-month viable quotes
├── calendar_research.py                 # new: historical candidate simulation
├── rank_strategies.py                   # new: train/holdout screening
├── test_research.py                     # new: synthetic tests
└── NIFTY_IV_RESEARCH_GUIDE.md
```

Only **09:45, 09:46 and 15:29 IST** one-minute bars are extracted for this version. This lets us test 9:45 signal -> 9:46 opening-price *proxy* entry, plus once-daily 15:29 scheduled exits and conditional exits signalled at 15:29 close and executed on the **next trading day** at 09:46 open. A 9:45 candle's *close* cannot have been known at 9:45:00. Volume > 0 is required for both option legs at entry/exit by default, and for marks that trigger an exit. The underlying index is an untradeable index level, and OHLC is **not** a bid/ask quote.

## Dependencies

From the PyCharm terminal with `.venv` active:

```bash
python -m pip install 'psycopg[binary]' scipy
```

Python's `sqlite3`, `csv`, `gzip` and `unittest` are built in. No pandas, DuckDB or extra package is required by the four new research programs. Your existing importer can keep its own Pandas/PyArrow dependencies.

Set your PostgreSQL connection information in your terminal; **do not put passwords in code or Git**:

```bash
export PGHOST=localhost PGPORT=5432 PGDATABASE=nifty_data PGUSER=postgres
read -rsp 'PostgreSQL password: ' PGPASSWORD; echo
export PGPASSWORD
```

## A. Check when NIFTY imports are complete (safe to run while importer works)

```bash
python prepare_research_cache.py --root NIFTY_Data --check-only
```

It checks **all** NIFTY files present on disk (annual Upstox files, NIFTY expiry files and both NIFTY spot copies) against their `market_data.import_files` statuses; simply having one completed pilot file is not enough. It only reads the ledger.

Alternative **psql terminal** progress command:

```bash
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 <<'SQL'
SELECT dataset, market_kind, status,
       COUNT(*) AS files,
       SUM(expected_rows) AS expected_rows,
       SUM(loaded_rows) AS loaded_rows
FROM market_data.import_files
WHERE relative_path LIKE 'NIFTY_20%.parquet'
   OR relative_path LIKE 'india-index-options-1m/options/NIFTY/%'
   OR relative_path IN ('NIFTY.parquet', 'india-index-options-1m/index/NIFTY.parquet')
GROUP BY dataset, market_kind, status
ORDER BY dataset, market_kind, status;
SQL
```

**Do not** build the very large multi-column PostgreSQL indexes just for this first research snapshot. The extraction uses a timestamp expression and annual partitions, so indexing the entire 233-million-record option table may not accelerate this operation. Analyse indexing only after assessing the snapshot query plan.

## B. Extract a reusable local research snapshot (when imports are complete)

First, use the broad range that includes the Upstox dataset and sufficient historical training time:

```bash
python prepare_research_cache.py \
  --root NIFTY_Data \
  --from-date 2024-10-01 \
  --to-date 2026-07-01 \
  --out research_cache.sqlite
```

The SQLite file contains both option providers **under separate provider keys** plus the nested NIFTY index spot prices. The extractor does not infer missing bars, manufacture IV, or mix prices from two providers to create an artificial spread. Duplicate same-provider keys are resolved in favour of the positive/higher-volume record, and original source IDs are preserved. Important: this is a pragmatic deterministic choice, not validation that the chosen last-trade price represents the bid or offer.

To test 2021–2026 using the longer, incomplete TradeMarkk history, specify:

```bash
python prepare_research_cache.py \
  --root NIFTY_Data \
  --from-date 2021-05-24 \
  --to-date 2026-07-01 \
  --out research_cache_long.sqlite
```

The cache scan is once per requested date range and can be disk-intensive. Restarting a partially written cache requires `--force-rebuild`; this **deletes and recreates only the named local SQLite cache**, never your PostgreSQL data.

## C. Data-coverage audit (before any optimisation)

```bash
python audit_coverage.py --cache research_cache.sqlite --out coverage_report
```

Outputs: `coverage_report/COVERAGE_REPORT.md` and `coverage_report/coverage_by_month.csv`. They show how many dates have near-ATM weekly puts and 60–120-day puts at the **same strike**, and how many also have positive-volume 9:46 a.m. entry candles.

If the numbers are small, we must not solve the problem by treating stale or missing quotes as executed trades. You can run sensitivity cases with `--min-fill-volume 0 --min-exit-volume 0`, but **label those results non-executable**.

## D. Candidate trade generation: begin SMALL

The most faithful first baseline uses **unhedged calendars with weekly exits**:

```bash
python calendar_research.py \
 --cache research_cache.sqlite \
 --provider rissin_upstox \
 --from-date 2024-10-01 --to-date 2026-06-01 \
 --round-steps 100,500 \
 --offsets=-500,-250,-100,0,100,250,500 \
 --long-tenors 15,30,45,60,75,90,120 \
 --hedges none --policies weekly \
 --max-candidates 50 \
 --out pilot_trades.csv.gz
```

The initial 50-candidate cap is deliberate: it validates that actual historical quotes are present, and that the pricing/CSV pipeline works without undertaking the full search. `--max-candidates` counts eligible **entry configurations**, not completed trades. Re-running without the limit performs the full baseline:

```bash
python calendar_research.py \
 --cache research_cache.sqlite \
 --provider rissin_upstox \
 --from-date 2024-10-01 --to-date 2026-06-01 \
 --hedges none --policies weekly \
 --out base_trades.csv.gz
```

The generator reports WHY candidates were skipped: no weekly option, no long tenor, zero-volume entry candle, invalid implied vol, missing exit, etc. It also writes `*.metadata.json` with parameters and counters. Read the metadata before accepting an optimisation.

Next, test a **targeted** advanced search on selected strikes/tenors. This includes delta-hedge proxies and a third-option gamma-reduction variant, as well as EOD-triggered exits and the separate long-to-30-DTE single-write treatment:

```bash
python calendar_research.py \
 --cache research_cache.sqlite \
 --provider rissin_upstox \
 --from-date 2024-10-01 --to-date 2026-06-01 \
 --round-steps 100,500 --offsets=-500,0,500 \
 --long-tenors 60,90,120 \
 --hedges none,delta_entry_proxy,delta_daily_proxy,gamma_integer_delta_daily_proxy \
 --policies weekly,p25_s50,p50_s100,iv_converge,carry30 \
 --out advanced_trades.csv.gz
```

This is deliberately not a blind exhaustive run across every combination, which would be computationally costly and would greatly increase the risk of overfitting.

### What is actually simulated

* **Unhedged weekly:** short the nearest available put expiring within 1–8 calendar days, long a same-strike put nearest the requested tenor (within 9 calendar days), enter at 09:46 **open**. Exit both on the short's actual expiry at 15:29 **open**. If the bar isn't present or lacks the configured volume, skip.
* **`carry30`:** short expires/closed in the first cycle; keep the original long put until the first session with 30 or fewer days left, then sell at 15:29 open. This does **not** re-sell weekly puts against the held long put. Only unhedged `carry30` is included.
* **`p25_s50`:** evaluate calendar P&L at 15:29 close. If gross/estimated P&L has risen 25% of initial debit or fallen 50% of initial debit, send an exit **next session 09:46 open**, rather than pretending to fill at the close that triggered the rule.
* **`p50_s100`:** same, at +50% or −100% of initial debit.
* **`iv_converge`:** signal a next-session exit when the short/long same-strike put IV ratio is below configurable `--iv-convergence`, default 1.0.
* **`delta_entry_proxy`:** neutralise initial Black–Scholes delta using a **fractional NIFTY spot-index proxy**; no rebalancing. **Not a tradeable futures result.**
* **`delta_daily_proxy`:** rebalance theoretical fractional index delta daily at 09:46 using same-minute prices; a delta band and assumed hedge turnover cost are included. **Not a tradeable futures result.**
* **`gamma_integer_delta_daily_proxy`:** add a short-expiry same-strike call sized in integer lots to reduce entry gamma, plus theoretical daily delta proxy. **It may NOT reach gamma neutrality** due to lot discreteness, option skew and market movement. The gamma quantity is recorded per trade.

### IV tenors and strike placement

* At entry, calculate annualised put IV with Black–Scholes from prices at 09:45, using adjustable `--rate` and `--dividend` assumptions.
* Compute IV at 5, 15, 30, 45, 60, 75, 90, and 120 DTE by **interpolating total implied variance**, `IV² × years_to_expiry`; never extrapolate. Missing surfaces remain missing.
* Store the actual short/long IV ratio and spread too.
* Test nearest strike rounded to 100 or 500, plus positive/negative point offsets. The CSV records moneyness percentage; direct percentage-moneyness and delta-targeted strike **selection is not implemented in this revision**.
* The 09:45 signal and subsequent 09:46 fill are distinguishable in every output. Differences between minute OHLC and executable bid/ask are **not** modelled beyond a simple adverse slippage parameter.

### Historical costs and sizing

* Results are **NIFTY points per index unit**, with option price changes and charges normalised by a user-specified **illustrative** lot size, default 65. This is NOT verified whole-lot INR profit, not a margin-based return, and not an estimate of required capital.
* The `--stt` option defaults to automatic sale-premium STT rates of **0.0625% before 2024-10-01, 0.10% from 2024-10-01 through 2026-03-31, and 0.15% from 2026-04-01 onward**, reflecting NSE circulars. Other fees/slippage are configurable illustrative assumptions.
* **Critical:** NIFTY lot sizes changed 25 → 75 → 65 across the covered history, and individual short and long option contracts may have different *lot sizes during transitions*. Equal unit exposure does not always equal **one whole short lot versus one whole long lot**. Do not treat the current per-index-unit figures as directly executable until contract-specific lot sizes have been reconstructed.
* No actual futures prices, bid/ask quotes, funding costs, broker SPAN/EXPOSURE margins, execution queuing or mark-to-market liquidation risk are available yet. We cannot quote a real return-on-margin or real rupee capital requirement.

Use slippage sensitivity (example, 2 full points **per leg and side**):

```bash
python calendar_research.py --cache research_cache.sqlite \
 --provider rissin_upstox --from-date 2024-10-01 --to-date 2026-06-01 \
 --hedges none --policies weekly --slippage 2 \
 --out base_slippage_2.csv.gz
```

## E. Choose IV bands on training data; report separate holdout

Use a *complete* candidate file (not the 50-candidate pilot). The default signals compare current weekly/long IV, 5/15, 5/30, 5/45, 5/60, 5/75, 5/90, 15/30, 15/60, 30/60, 30/90, 45/90 and 60/90. `--iv-signals all` tests every ordered pair among 5, 15, 30, 45, 60, 75, 90 and 120 days (28 combinations plus short/long). Custom subsets can also be used.

```bash
python rank_strategies.py \
 --trades base_trades.csv.gz --cache research_cache.sqlite \
 --split 2026-01-01 --embargo-days 120 \
 --thresholds 0.90,1.00,1.10,1.20,1.30,1.50 \
 --upper-bounds 1.30,1.60,2.00,999 \
 --max-open 3 --min-train 30 --min-test 10 \
 --out base_rankings
```

The **120-day embargo** prevents training trades opened shortly before the holdout from remaining open into the holdout. Use a different walk-forward split only as an intentional new validation experiment. Avoid picking the "best" test-period outcome and then describing it as an independent test.

Results: `base_rankings/rankings.csv` and `base_rankings/rankings.json`. The ranking score uses **training performance only**; the later **2026 holdout** is shown but not used to rank. Entries are capped by `--max-open`; the train and test reports include trade counts, gross/net point totals as simulated, hit rate, worst trade, and **realised-only** drawdowns.

**Risk warning:** These realised-only drawdowns can drastically understate the value decline of open positions. Portfolio mark-to-market paths, historical margins, and actual futures hedges are necessary before use with real money. A 120-day embargo does not itself establish statistical independence; repeated candidate tuning can still overfit.

For longer training coverage, rerun with the 2021–2026 `research_cache_long.sqlite` and `--provider thetrademarkk`, then compare its results against `rissin_upstox`. **Never merge their candidate P&L streams into one false continuous dataset.**

## F. Diagnostic tests

```bash
python test_research.py
```

These are synthetic tests for pricing inversion, calendar cash flows, delta-proxy and gamma-leg accounting, expiry exit, and maximum overlapping positions. Passing does **not** establish the integrity or profitability of the real market data.

## G. Results to share

Please upload:

1. `coverage_report/COVERAGE_REPORT.md` and `coverage_by_month.csv`.
2. The generator's `base_trades.csv.gz.metadata.json` (small; coverage and skipped-reason counters).
3. `base_rankings/rankings.csv` and `rankings.json` (small).

You do **not** need to upload the local PostgreSQL data or the large cache file. If the backtest reports no eligible trades, send the audit and counters first rather than lowering all data-quality filters.

## H. Features deliberately deferred from this research revision

This is a **first-stage screening engine**, not the final automated trading selector. Before treating results as decision-grade, we should implement:

* Proper broker-contract quantity mapping across historical lot-size transitions; futures/index-price basis and financing; transactional margin history.
* Actual bid/ask quotes or defensible spread models, and trade-size/slippage simulations.
* True whole-contract NIFTY futures delta hedges (historical futures not yet in this database); **exact** multi-option delta/gamma neutrality with practical quantity limits, rather than a spot proxy.
* Repeated weekly put sales against the **same retained long put**; rolling when strikes diverge, collateral accounting and calendar-specific risk across time.
* Intraday stops, conditional intraday entries, varying signal/entry minutes and real fills; current extracted snapshot is 3 specific minutes.
* Percentage-moneyness and delta-based strike *selection*, volatility-skew trades, volatility risk premia compared with realised volatility, calendar carry/forward variance curves, and alternative gamma hedge strikes/expiries.
* Historical drawdown on **daily marked-to-market portfolio equity**, tail exposure, crash stress, volatility shock and liquidity-gap stress, risk and return on **actual** allocated capital.
* Rolling/anchored walk-forward validation across multiple years, parameter stability maps and multiple-testing corrections. The present training/holdout report is only **one** split.

### Official references for historically changing market rules

* NSE `NSE/FAOP/68747`: NIFTY weekly expiry moved from Thursday to Tuesday from September 2025, with transitional contract rules.
* NSE `NSE/FAOP/70616`: NIFTY market lot revised from 75 to 65, with contract-specific rollout.
* NSE `NSE/FATAX/63809`: options STT sale premium increased from 0.0625% to 0.10%, effective 2024-10-01.
* NSE options securities-transaction-tax page and FY2026 circular: sale premium STT 0.15% from 2026-04-01.

Read the circulars and contract master if implementing real broker-equivalent trades.
