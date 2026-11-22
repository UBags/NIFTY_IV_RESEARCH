# NIFTY Relative-IV Research Engine — V2

**Status:** functional, synthetic-tested research prototype. No live PostgreSQL integration run has been possible here. **Not** a validated historical-return report or deployable execution system.

## 1. What changed

This is a **new directory alongside V1**. `legacy_v1/` contains the unchanged V1 Python programs. No script here modifies `market_data.*` or interferes with `import_nifty_postgres.py`.

| Gemini / independent review concern | V2 implementation | Qualification |
|---|---|---|
| Put–call parity forwards | `quant_v2.py`, `enrich_cache.py` | Median of matched liquid CE/PE observations; rejects conflicting parity. Minutes are OHLC, not synchronous order-book quotes. |
| Forward Black IV | `quant_v2.py` | Separate forward for each expiry, no fixed dividend assumption. Assumed interest rate remains configurable. |
| Vectorized IV | `implied_vol_batch()` | Safeguarded Halley, Brent fallback, reconstructed-price verification. No claimed 3-iteration guarantee. |
| Forward volatility and vega | `forward_iv`, `forward_iv_ratio`, `net_vega` candidate fields | A positive forward-variance gate filters inconsistent term structures. Vega is not a guarantee of P&L. |
| 5/15/30/45/60/75/90/120 IV ratios | All interpolated tenors stored; `rank_strategies.py --signals all` searches every pair | **No extrapolation**; missing tenors remain NULL. |
| Relative strike placement | `--strike-mode points`, `pct`, `std`, `delta` | Vol-standardized and delta target strikes are calculated using short-expiry implied forward. Target delta is approximate. |
| Actual NIFTY expiry changes | Uses actual `expiry` from contracts, not a fabricated Tuesday for old data | `short_dte_bucket` uses **remaining spot trading sessions**, not calendar DTE, and is optionally grouped in rankings. |
| Intraday stop exits | 11:30/11:31, 13:30/13:31, 15:15/15:16 signal/fill pairs | One-minute idealized fills; must stress-test slippage. |
| Daily MTM | Every simulated open trade carries a daily 15:29 value; ranking reconstructs portfolio equity and max drawdown | Requires matching daily leg bars. Missing marks reject trades and are counted, risking coverage selection bias. |
| Multi-week short-put sales against one long put | `rolling` policy; same strike; short closed before expiry at 15:16, new short no earlier than next eligible Wed–Fri | One new short per weekly cycle. **Not** an auto-rebalanced/ATM-restuck short. Long exits at <=30 DTE. |
| Historical lots | `contract_specs_v2.py` and optional authoritative contract-master CSV | Transition ambiguity is rejected. This is **circular-derived inference**, not a complete daily contract master. |
| Delta/gamma | `delta_entry_proxy`, `delta_daily_proxy`, `gamma_reduced_delta_proxy` | **Spot proxy is NOT tradable futures; gamma-neutral cannot be guaranteed with integer option lots.** Rolling hedges not implemented. |
| Source duplicates | SQLite `opt_raw` retains every selected row and `opt` picks canonical rows by deterministic volume/source-ID priority | Original PostgreSQL remains the immutable source; no merges across providers. |
| Research migrations | Versioned additive SQL, `migrate_research.py` | Only creates `research_v2.*`. No changes to `market_data` and no large indexes while importer is active. |
| Database snapshot efficiency | One timepoint CTE and range predicate; single local SQLite intermediate for candidate marks | Not measured 10–100x; no binary COPY, partition-specific indexes, or SQLite `WITHOUT ROWID` yet. |
| Ranking efficiency | Train-only grouped SQL prescreen, then MTM for top `--screen-pool`; no in-memory load of all marks | Large grids still need real benchmarks; no parallelized/vectorized policy state machine yet. |
| Regime reporting | Pre-entry 20-session spot-return and realised-vol buckets | These are not macro-event labels; event-calendar enrichment pending. |

### Intentionally still outstanding

Actual NSE futures quotes and hedge fills, true bid/ask depth, official expiry settlement and margin/SPAN history, verified daily lot contract masters, dynamically gamma-neutral portfolios, margin-based RoC, independent multi-fold walk-forward tests, and full execution sensitivity/latency calibration. **Do not use the optimiser to select a live trading position until these gaps are addressed.** The reported currency P&L is a *research approximation* using historical lot rules and OHLC prices.

## 2. Directory layout

Extract the ZIP under your existing project:

```text
~/PycharmProjects/NIFTY_OPTIONS/
  .venv/
  NIFTY_Data/                          # downloaded Parquets, UNCHANGED
  nifty_inspection_all/                # previous inspection output
  import_nifty_postgres.py             # currently running, UNCHANGED
  NIFTY_IV_RESEARCH_V2/
    README_V2.md
    migrations/0001_research_v2.sql
    migrate_research.py
    prepare_research_cache.py
    enrich_cache.py
    audit_quality.py
    quant_v2.py
    contract_specs_v2.py
    calendar_research.py
    rank_strategies.py
    publish_results.py
    tests/test_v2.py
    legacy_v1/...
```

Activate the venv and move into V2:

```bash
cd "$HOME/PycharmProjects/NIFTY_OPTIONS"
source .venv/bin/activate
cd NIFTY_IV_RESEARCH_V2
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 3. What you may do **NOW**, while the 501M-row importer is running

The migration **only adds a new schema**. It does not alter existing imported tables or the loader's checkpoint table. You can run it now, or after the import, whichever you prefer:

```bash
python migrate_research.py --dry-run
python migrate_research.py
```

Verify through PyCharm terminal (all SQL provided as runnable **psql**):

```bash
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 <<'SQL'
SELECT version, applied_at FROM research_v2.schema_migrations ORDER BY version;
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema='research_v2'
ORDER BY table_name;
SQL
```

The `research_v2` schema stores **metadata, optional contract overrides, quality summaries, ranked outcomes**. It does not duplicate hundreds of millions of OHLC rows in PostgreSQL. Detailed research bars and trade MTM remain local in SQLite. This is deliberate: there is no reason to bloat the actively importing database.

You can monitor the import safely:

```bash
bash check_import_psql.sh
python prepare_research_cache.py \
    --root "$HOME/PycharmProjects/NIFTY_OPTIONS/NIFTY_Data" --check-only
```

**Do not generate the research cache while imports remain partial.** The extractor refuses incomplete input by default. `--allow-partial` is diagnostics-only and creates an **ineligible** cache that the backtester will reject.

## 4. After the NIFTY import completes

Run from `NIFTY_IV_RESEARCH_V2/`, avoiding the legacy V1 cache names.

```bash
python prepare_research_cache.py \
  --root "$HOME/PycharmProjects/NIFTY_OPTIONS/NIFTY_Data" \
  --from-date 2024-11-01 --to-date 2026-07-01 \
  --out research_cache_v2.sqlite
```

This runs two **read-only PostgreSQL queries** on existing `market_data.spot_bars` and `market_data.option_bars`. No indexes, SQL views, materialized tables or DDL are added to the source database. The cache includes 13 timepoints: `09:45,09:46,09:47,09:48,09:49,09:50,11:30,11:31,13:30,13:31,15:15,15:16,15:29` IST. The scan may still be expensive on your full PostgreSQL holdings; we have **not benchmarked** this query on your machine.

Enrich options once with parity-derived forward prices, IV and Greeks:

```bash
python enrich_cache.py --cache research_cache_v2.sqlite \
  --rate .06 --min-pair-volume 1 --max-parity-bps 40
python audit_quality.py --cache research_cache_v2.sqlite --out coverage_v2
```

Inspect `coverage_v2/quality_by_month.csv` **before running a large strategy grid**. Sparse long-tenor options and the requirement for valid same-minute calls may sharply reduce eligibility. Never present results from a tiny surviving subset as unconditional strategy returns.

## 5. Small pilot (suggested first)

```bash
python calendar_research.py \
  --cache research_cache_v2.sqlite \
  --out pilot_trades_v2.sqlite \
  --provider rissin_upstox \
  --from-date 2025-09-01 --to-date 2026-06-01 \
  --strike-mode points --round-steps 500 --strike-values 0 \
  --long-tenors 60,90 --hedges none \
  --policies weekly,carry30,rolling \
  --max-candidates 30
```

Each trade also retains an auditable option fill/cash-flow ledger and its daily MTM path in the local results database.

The CLI **refuses to overwrite existing results**. Use a new output filename for every experiment.

Review `pilot_trades_v2.sqlite.diagnostics.json`. If few trades survive, the reason may be poor data coverage, not an unprofitable strategy. It is essential to investigate the exclusions before selecting winners.

## 6. Larger parameter sweeps

The next examples are **separate** runs, not parameter flags that silently reinterpret the strike grid.

**A. Fixed point offsets:**

```bash
python calendar_research.py --cache research_cache_v2.sqlite \
 --out points_grid_v2.sqlite --strike-mode points \
 --round-steps 100,500 --strike-values=-500,-250,-100,0,100,250,500 \
 --long-tenors 30,45,60,75,90,120 \
 --hedges none,delta_entry_proxy,delta_daily_proxy,gamma_reduced_delta_proxy \
 --policies weekly,p25_s50,p50_s100,iv_converge,carry30,rolling
```

**B. Percentage moneyness:**

```bash
python calendar_research.py --cache research_cache_v2.sqlite \
 --out percentage_grid_v2.sqlite --strike-mode pct \
 --round-steps 100,500 --strike-values=-3,-2,-1,0,1,2,3 \
 --long-tenors 30,60,90,120 --hedges none \
 --policies weekly,rolling,carry30
```

**C. Standard-deviation moneyness (short tenor):**

```bash
python calendar_research.py --cache research_cache_v2.sqlite \
 --out std_grid_v2.sqlite --strike-mode std \
 --round-steps 100,500 --strike-values=-1.5,-1,-.5,0,.5,1,1.5 \
 --long-tenors 30,60,90,120 --hedges none --policies weekly
```

**D. Delta-target strikes:**

```bash
python calendar_research.py --cache research_cache_v2.sqlite \
 --out delta_grid_v2.sqlite --strike-mode delta \
 --round-steps 100,500 --strike-values=-0.25,-0.35,-0.50 \
 --long-tenors 30,60,90,120 --hedges none --policies weekly
```

All four write local SQLite, not PostgreSQL. Need a new output filename if rerunning. `--strike-values=-...` is intentional; it prevents argparse from mistaking a leading minus sign for a new argument.

**Ranking.** Start with 2 IV signals, then use `--signals all` for every tenor-pair combination if sufficient data exists:

```bash
python rank_strategies.py \
 --trades points_grid_v2.sqlite --cache research_cache_v2.sqlite \
 --split 2026-01-01 --embargo-days 35 \
 --signals iv_ratio,forward_iv_ratio \
 --short-dte-buckets all,1-2,3-5,6-8 \
 --min-train 25 --min-holdout 10 \
 --max-open 3 --screen-pool 200 --out points_rank_v2
```

A second run can search all 28 interpolated tenor-pair ratios (when defined) plus short/long and forward-IV ratios:

```bash
python rank_strategies.py --trades points_grid_v2.sqlite \
 --cache research_cache_v2.sqlite \
 --split 2026-01-01 --embargo-days 35 \
 --signals all --max-open 3 --min-train 25 --out all_ratios_rank_v2
```

`ranking.csv` contains training-vs-holdout profit, DAILY MTM drawdowns and daily Sharpe. `ranking.json` additionally reports the regime breakdown. Grid selection is **training-only**. The final holdout is examined only after finalists are chosen. This first version does **not** implement a complete repeated walk-forward search. Do not repeatedly adjust the grid based on disappointing holdout results and then call it an untouched test.

## 7. Optional contract-size overrides

Our historical lot-size rules are derived from [NSE/FAOP/47854](https://archives.nseindia.com/content/circulars/FAOP47854.pdf), [NSE/FAOP/61415](https://nsearchives.nseindia.com/content/circulars/FAOP61415.pdf), [NSE/FAOP/64625](https://nsearchives.nseindia.com/content/circulars/FAOP64625.pdf), and [NSE/FAOP/70616](https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf), **not a complete contract-master history**. Exceptional/ambiguous combinations are excluded.

If you obtain historical contract-master records, add verified overrides to `research_v2.contract_lot_overrides` (separately from source OHLC). Export those as CSV through **psql in PyCharm terminal**:

```bash
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 \
  -c "\\copy (SELECT trading_day,expiry,lot_size,evidence AS source FROM research_v2.contract_lot_overrides WHERE symbol='NIFTY') TO STDOUT WITH (FORMAT csv, HEADER true)" \
  > contract_master.csv
```

Then supply `--contract-master contract_master.csv` to the simulator. A file with a blank `trading_day` applies to all dates for that expiry, but use explicit trading dates where the contract changed size during a transition.

## 8. Optional write-back to research schema

This is only a summary/audit log, not a wholesale migration of bars:

```bash
python publish_results.py --ranking points_rank_v2/ranking.json \
  --trades points_grid_v2.sqlite --name relative_iv_points_grid_v2
```

Verify via **psql**:

```bash
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 <<'SQL'
SELECT experiment_id, experiment_name, engine_version, lifecycle, created_at
FROM research_v2.experiments ORDER BY experiment_id DESC LIMIT 10;
SELECT experiment_id, sample_name, COUNT(*) AS results
FROM research_v2.experiment_results GROUP BY experiment_id,sample_name
ORDER BY experiment_id DESC,sample_name;
SQL
```

## 9. Accuracy and risk limitations

1. **Do not infer executable bid/ask prices from OHLC:** slippage is a stress parameter; zero-volume 15:29 candles are flagged. The ranking excludes simulations with zero-volume daily marks by default, which can itself select more liquid periods.
2. **Only one contract-specific lot estimate is available for some dates:** historical transition cases may be rejected. Even equal contract quantities do **not** prove that NSE margin offsets applied.
3. **Expiry closes are modeled by actual 15:16 OHLC fills**, not final official NSE settlements. Trades missing one leg's pre-expiry fill are rejected, and counted. This can introduce missing-data selection bias.
4. **Rolling policy is one retained long put with repeated short puts at the original strike**, not a continuously ATM-retargeted hedge. Gamma-neutrality is approximated using integer call lots; rolling gamma hedges are not implemented.
5. **Delta is a spot-index proxy;** there is no NIFTY futures history in this dataset. Hedged strategy P&L is hypothetical and should not be treated as tradable.
6. **Return on capital is not reliable:** we report INR P&L and an initial-debit exposure proxy, not daily real margin, broker charges, or exchange-calculated capital.
7. **Real data integration remains untested:** no access to your local PostgreSQL instance or imported market bars. The synthetic tests establish program-path behavior, not profitability or live data fidelity.
8. **The train-only optimizer is still subject to multiple-testing:** a chronological holdout is not sufficient on its own. A complete rolling walk-forward test, regime/event tagging, and block bootstrap confidence intervals remain future acceptance gates.

## 10. Safe rollback and noninterference

`migrate_research.py` creates objects **only** in `research_v2`. No `ALTER`, `DROP`, `CREATE INDEX` or `UPDATE` touches `market_data`. The importer can continue unchanged. To inspect current schema before doing anything:

```bash
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 <<'SQL'
SELECT schema_name FROM information_schema.schemata
WHERE schema_name IN ('market_data','research_v2') ORDER BY schema_name;
SQL
```

**Do not delete `market_data` or its partitions.** If you later choose to discard every experiment and override created in V2, the optional destructive rollback below drops **only** the `research_v2` schema; execute it only after you have backed up any wanted research results:

```bash
# OPTIONAL, DESTRUCTIVE TO RESEARCH_V2 ONLY — DO NOT RUN DURING NORMAL SETUP
# psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 -c 'DROP SCHEMA research_v2 CASCADE;'
```

## 11. What we actually validated here

- Syntax compilation of all V2 Python scripts.
- Synthetic checks for parity consistency/rejection, Black implied-vol inversion, lot-size transition rules, and preservation of same-vendor duplicate raw option rows.
- Synthetic trading runs for weekly paired calendars, a rolling weekly-short strategy, calendar with retained long, intraday exits, delta-index-proxy and gamma-reduction modes.
- Daily portfolio MTM maximum drawdown check in which a profitable close follows an interim large loss.
- End-to-end local SQLite enrichment -> simulation -> train-only ranking -> monthly data-quality report.
- Scalar-vs-vector IV performance benchmark (run `python benchmark_iv.py --samples 3000`; speedups depend on hardware and quote distribution).

There is **no historical P&L output** in this package. No future work is running in the background.
