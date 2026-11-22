# NIFTY Relative-IV Research V4.8.3: clean-start guide

Research software. OHLC minute candles are not bid/ask quotes, capital is a scenario and not SPAN, and real-data results so far are research findings, not validated trading returns (see the Options Trading project notes). See `CHANGES_V4.md`.

Do not run V3 commands. V4 reads `market_data.*` and never alters it. The PostgreSQL schema is still named `research_v3`: it is a shared name, and no V4 migration is missing because new statistics are stored in existing JSONB columns.

## 1. Install
```bash
cd ~/PycharmProjects/NIFTY_OPTIONS
unzip ~/Downloads/NIFTY_IV_RESEARCH_V4_8_3.zip -d .
cd NIFTY_IV_RESEARCH_V4
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python smoke_v3.py
export PGHOST=localhost PGPORT=5432 PGDATABASE=nifty_data PGUSER=postgres
read -rs -p 'PostgreSQL password: ' PGPASSWORD; echo; export PGPASSWORD
python migrate_research.py --dry-run
python migrate_research.py
psql -h localhost -U postgres -d nifty_data -v ON_ERROR_STOP=1 -c "SELECT version,applied_at FROM research_v3.migration_history ORDER BY version;"
```

## 2. Build a new cache
Check `df -h .` first: a full chain needs many GB, and SQLite can exceed the compressed Parquet size.
```bash
python prepare_research_cache.py --root ../NIFTY_Data --check-only
python prepare_research_cache.py --root ../NIFTY_Data --from-date 2024-11-01 --to-date 2026-07-01 --out research_cache_v4.sqlite
python enrich_cache.py --cache research_cache_v4.sqlite --batch-rows 30000
```
If a build is interrupted, rerun the same command with `--resume`; it continues from the last finished month. The final file only appears once the build verifies, so an existing cache survives a failure. Rerunning `enrich_cache.py` replaces all earlier Greeks.

Do not extend `--to-date` past July 2026 without reviewing closing-auction effects. Rebuild any cache made before V4.4.3: the spot extract now reads one explicit file.

Before a long extract on real data, inspect one month read-only: `python explain_month.py --month 2025-06 > plan_2025_06.txt`. Do not add indexes to `market_data` based on guesses.

## 3. Coverage gate: do this before any strategy run
```bash
python audit_spot_sources.py
python audit_quality.py --cache research_cache_v4.sqlite --out coverage_v4
python audit_contract_coverage.py --cache research_cache_v4.sqlite --provider rissin_upstox --out contract_coverage_rissin
python audit_contract_coverage.py --cache research_cache_v4.sqlite --provider thetrademarkk --out contract_coverage_trademarkk
```
Send back `coverage_v4/quality_summary.json`, both `atm500_monthly.csv` files and the build and enrichment terminal summaries before any strategy run.

Check, per vendor, how far each month and tenor gets through the stages in `atm500_monthly.csv` (`coverage_cube.csv` adds weekday, `opportunities.csv` lists each day): signal, each leg in window, same minute, fill model, lot size, Greeks, admitted. Also: `spot_source_consistency` in `coverage_v4/quality_summary.json` (the close gap should be near zero); `vendor_discordant_rows`; `special_sessions` (short days that can enter marks or Sharpe calendars); `session_quality`; `bar_integrity`; `duplicate_minutes_with_conflicting_close`.

## 4. First pilot: unhedged weekly put calendar only
Choose tenors from your own coverage funnel. End entries before any stretch of missing sessions: a short whose expiry session is absent cannot settle, and is held at a placeholder mark.
```bash
python calendar_research.py --cache research_cache_v4.sqlite --out pilot_v4.sqlite \
  --provider rissin_upstox --from-date 2024-11-01 --to-date 2026-05-18 --weekdays 0,1,2,3,4 \
  --option-type PE --structures put --long-tenors 30,60 --round-steps 500 \
  --strike-mode points --strike-values=0 --policies weekly --hedges none --lots 1 \
  --max-participation 0.10 --volume-unit units --max-exit-lag 1 --workers 6
python audit_quality.py --cache research_cache_v4.sqlite --trades pilot_v4.sqlite --out pilot_quality_v4
python rank_strategies.py --trades pilot_v4.sqlite --cache research_cache_v4.sqlite \
  --split 2026-01-01 --embargo-days 35 --folds 5 \
  --min-train 10 --min-holdout 5 --max-open 3 --signals iv_ratio,forward_iv_ratio,iv_to_rv --out pilot_rankings_v4
python exit_lag_report.py --trades pilot_v4.sqlite --cutoff <train_end_exclusive from rankings.json>
```
`--weekdays` defaults to Wed, Thu, Fri (2,3,4; Monday is 0). `--max-exit-lag N` limits how many sessions a long may wait for a liquid exit once its short is gone, then books it at a modelled 15:29 mark (`forced_exit_lag_cap`): a same-day printed close less `--forced-exit-haircut`, otherwise the mark's lower bound. It is not a fill. The default, -1, keeps waiting. `exit_lag_report.py` compares realized P&L with the mark on the planned exit day, by tenor and expiry era.

`--holdout-entry-cutoff` defaults to `per_trade`: a trade is in the holdout when its planned exit fits inside the data. `global_longest` is the older data-end-minus-longest-tenor rule. It is refused if it falls before `--split`. Do not tune on the holdout.

## 4b. Zone + swing study (INDEX v15 on 45-minute bars)
`INDEX_v15_zone_swing.pine` is the TradingView indicator; `zone_signals.py` is its port (volume 1 on every bar, bar close only).
```bash
python zone_spot.py --from-date 2020-01-01 --to-date 2026-07-01 --out zone_spot.sqlite
python zone_signals.py --spot zone_spot.sqlite --split 2025-01-01 --out zone_signals.json
python zone_contracts.py --spot zone_spot.sqlite --out zone_options.sqlite
python zone_backtest.py --spot zone_spot.sqlite --options zone_options.sqlite --out zone_trades.sqlite
python zone_report.py --trades zone_trades.sqlite --split 2025-01-01 --out zone_report
```
Wider grids: pass the same `--offsets` to `zone_contracts.py` (new `--out`) and `zone_backtest.py`, and choose `--tps`. `--stops none,15,30,45` adds an index stop at signal close -/+ `--atr-mult` x ATR on 45-minute bars; each stop is ranked as its own family (`naked|atr30`). `zone_futures.py` is the spot-only futures benchmark in the same trade format. `zone_summary.py --trades ... --report ...` prints the top rules per side by 12-month period in rupees.
`zone_contracts.py` pulls every minute of only the contracts a run can touch (found from spot data and the expiry listing, before any option price is read); it is resumable by month. `zone_backtest.py` refuses an options file planned with different `--min-short-days`, `--long-target`, `--lots` or dates. `zone_report.py` reads no holdout outcome; once, when the selection is final: `python zone_report.py --trades zone_trades.sqlite --mode final_holdout --freeze zone_report/selection_freeze.json --out zone_report`.

## 4c. Zone study, exhaustive exit grid with stability ranking (V4.8)
Simulate every trade once with minute-sliced fills, then apply every take-profit and stop-loss rule to the stored paths.
Use the options file you extracted with the wide strikes (the same `--offsets`), and the same dates as that extraction.
The paths file must be rebuilt with V4.8.3 (dated exit ledger, a state for every session of a multi-day exit; earlier path files are refused by the grid's algorithm check).
Pass your exchange holiday list to the grid (`--holidays nse_holidays.txt`, any common date format) so weekend exits use the real last session of each week.
```bash
set -o pipefail
python zone_paths.py --spot zone_spot.sqlite --options zone_options_wide.sqlite \
  --offsets=-400,-300,-200,-100,0,100,200,300,400 --lots 10 --out zone_paths.sqlite 2>&1 | tee zone_paths.log \
&& python zone_grid.py --paths zone_paths.sqlite --out zone_grid --workers 4 --holidays nse_holidays.txt 2>&1 | tee zone_grid.log
```
Rules (`zone_rules.py` has the exact definitions): naked options 50 profit x 368 stop rules, calendars 43 x 355, futures 34 x ~348,
about 944 million combinations over all signal configurations, strikes and structures. Stop families: fixed ATR (signal and
entry anchored, ATR 15/20/30/45/60, 0.25-5x), % and points, trailing ATR/%/points, the V4.7 signal-anchored trail, chandelier,
channel break, breakeven, profit give-back, time, expiry proximity, EMA close, volatility expansion, time of day / weekend,
stagnation, premium loss, debit loss (calendars), option price multiple. Profit: none, premium 5-95%, index ATR / % targets,
time-in-profit. All rule labels from V4.7.x are still in the grid with identical results. Layered policies (`c:initial|management|mandatory`,
the earliest part firing): initial protection (fixed ATR, entry ATR, %, premium/debit loss) x profit management (trailing ATR/%, breakeven,
give-back) x mandatory exit (time, expiry, 15:15, weekend), 659-879 per structure, with a compact profit set; `--no-compound` skips them.
`zone_paths.py --fill-price adverse` (high for buys, low for sells) rebuilds the paths for an execution stress run.

Selection is reported in two modes, each walk-forwarded by year: `total` (largest training total, take-profit by win rate within
75% of the best total, as before) and `stable` (largest conservative lower quartile, the floor((n-1)/4)-th smallest value, of the training totals of the combination and its
neighbours one grid step away in N, k, m, j, tw, profit level, stop period and multiplier; a combination needs at least one valid neighbour and half of
those that exist, so edge settings such as zone_only_tw2/tw6 are scored). `--zone-min-trades` (default 5) sets the zone-only minimum; each row shows
the minimum it used. Outputs: `grid_walk_forward.csv` (every
side x structure x stop family x configuration set x mode: walk-forward pooled result, training pick, its stability, its totals at
strike +-100), `stable_candidates.csv` (top 50 stable combinations per side and structure, with their in-sample totals by year and neighbour coverage), `baselines.csv` (every zone-only
no-stop, no-target result, whatever its stability),
`grid_report.json` (also exact trade statistics for every pick and buy-and-hold), and `grid_*__<family>.npz` (every combination).

`--workers` runs groups in parallel (each worker needs roughly 1-3 GB; use 2 if the machine has 16 GB or less). Expect several hours
and tens of GB (the V4.7 grid was 2.7 GB; this one has ~12x the rules). `--resume` keeps finished group files after re-hashing them;
`--report-only` refuses to read a grid whose files, rule definitions or paths file do not match the recorded checksums.
`--explain side,structure,offset,config,profit,stop` writes one combination's trades. Nothing after `--split` is read.

## 5. Reading `rankings.json`
| Field | Meaning |
|---|---|
| `nested_walk_forward.portfolio` | Primary out-of-sample evidence: one portfolio across folds, with slots and capital carried over. Each fold's configuration and thresholds come from earlier data only |
| `results[].nested_portfolio` | The same, inside one strategy group |
| `sharpe` / `active_day_sharpe` / `time_in_market` | Calendar-time over a fixed window (ranking uses this) / position-open days only / share of days invested |
| `fixed_rule_fold_diagnostic` | Rules chosen on the whole training window. In-sample for selection |
| `basis_views` | Raw INR, per index unit, per rupee of entry scenario capital. Comparison scales, not executed sizing |
| `holdout_cohorts` | All opened versus fully observed; compare them |
| `spa_*` | Liberal on overlapping trades: discount with it, never confirm with it |
| `circular_shift_control` | Best threshold against the same search on a time-shifted signal. Held about 4-6% false positives at 5% in simulation, within one signal and group. Does not cover the search across groups |

Scoring defaults to `--score-basis per_unit`. `training` and `holdout` are raw INR; `metrics_basis` states which is which.

The default fill is `--fill-model next_bar_open`: an order becomes eligible on a finished minute's volume and fills at the next available minute's open, so nothing printed later gates or prices it. A fill on a bar that ended with zero volume is kept and flagged; read `trades_with_fills_on_zero_volume_bar` in the run diagnostics. It is still an OHLC proxy, not a confirmed fill. `next_bar_open_adverse` is an ex-post worst-print stress, not a causal model. The other models need `--allow-conditional-fill-models` and are diagnostics. For sensitivity run `--fill-model next_bar_open_traded_only` (ex-post filter) and `--entry-delay-minutes 1`, `2`, `3`, each to its own trades file, then `python compare_fill_models.py --cache research_cache_v4.sqlite --eval-start 2024-11-01 --eval-end 2026-06-30 --runs open=a.sqlite traded=b.sqlite delay1=c.sqlite`. New orders are blocked on weekends and on dates listed in `--special-sessions` (a file of dates published in advance). How a day later ended never decides an entry. Each order may take at most `--max-participation` of a bar's volume (default 10%); `--volume-unit auto` reads whether the vendor counts units or contracts, and stops if it cannot tell. Costs follow dated statutory schedules; `--brokerage-order-mode per_lot_orders` bills one order per lot, and `--lots` sets size. `--signals all` uses training-only quantiles for features without a ratio scale. See `CHANGES_V4.md`.

## 5a. V4.5 experiments, only after the pilot
Predeclared in `REVIEW_V4_5.md` section 4. Check costs first with `python calibrate_slippage.py --cache research_cache_v4.sqlite`, and what the holdout can show with `python power_analysis.py`.
```bash
python calendar_research.py --cache research_cache_v4.sqlite --out v45_a.sqlite --workers 6 --option-type PE --structures put,iron_fly \
  --long-tenors 60,90 --round-steps 100 --strike-values=0 --policies weekly,tm1,p25_s50_tm1,rolling_static,rolling_tm1 --hedges none --holiday-calendar nse_holidays.txt
python rank_strategies.py --trades v45_a.sqlite --cache research_cache_v4.sqlite --split 2026-01-01 --embargo-days 35 --folds 5 --min-train 10 --min-holdout 5 --signals iv_ratio,forward_iv_ratio,iv_to_rv --out v45_a_rank
```
Read `unfiltered_family_books` before `results`. `nse_holidays.txt` is the exchange's published list, one YYYY-MM-DD per line. For the event test add `--event-calendar events.txt --event-policy skip_event_weeks` to its own `--out` file; each line is `event_date,first_known_at[,superseded_at[,source]]`, ISO-8601 in IST, and a bare date counts from the next day. Flies have no long leg, so `iv_ratio` is empty for them; use `iv_to_rv` or the `ivA/ivB` curve signals.

Check T-1 execution before reading its P&L: `tm1_outcome_*` in `diagnostics`, `tm1_execution` per result. Compare structures with `python compare_structures.py --trades v45_a.sqlite --a iron_fly:tm1 --b put:tm1`.

Inspections are tracked per user in `~/.nifty_research/holdout_inspections.jsonl`, keyed by trade content. This records accidents; it is not cryptographic sealing.

**Sealed holdout.** Ranking defaults to `--research-mode train_only`: no holdout figure exists in the report. When selection is final, once:
```bash
python rank_strategies.py <same arguments> --research-mode final_holdout --freeze-file v45_a_rank/selection_freeze.json --out v45_a_final
```

## 6. Roll variants, only after the pilot
Run each into its own file and rank separately:
```bash
python calendar_research.py --cache research_cache_v4.sqlite --out roll_uncond_v4.sqlite --option-type PE --structures put --policies rolling_static --hedges none
python calendar_research.py --cache research_cache_v4.sqlite --out roll_gated_v4.sqlite --option-type PE --structures put --policies rolling_static --hedges none \
  --roll-signal-policy wait_until_eligible --roll-iv-min 1.1
```
`require_ratio` forgoes the week if the check fails; `wait_until_eligible` rechecks each allowed weekday. Every decision is in `details.roll_decisions`.

## 6a. Guidebook experiments, only after the put-calendar pilot
See `GUIDEBOOK_RESEARCH_NOTES.md`; write down the hypotheses first.
```bash
python audit_contract_coverage.py --cache research_cache_v4.sqlite --provider rissin_upstox --long-strike-offset -500 --out contract_coverage_diag500
python calendar_research.py --cache research_cache_v4.sqlite --out diag_v4.sqlite --option-type PE --structures put --hedges none \
  --long-tenors 90,120 --round-steps 100 --strike-values=0 --long-strike-offsets 0,-500,-1000 --policies weekly,atm_weekly_roll
python campaign_report.py --trades diag_v4.sqlite --out campaign_report_v4
```
`--roll-timing expiry_day_close` sells next week's option on expiry afternoon instead of the next session. Run it into a separate file.

## 7. Verified inputs and publishing
`--contract-master` takes `trading_day,expiry,lot_size,source`. `--settlement-master` takes `expiry,settlement_price,evidence_url`. Without a verified row, settlement is a labelled proxy.
```bash
python publish_results.py --name 'NIFTY pilot' --provider rissin_upstox --cache research_cache_v4.sqlite \
  --trades pilot_v4.sqlite --rankings pilot_rankings_v4/rankings.json --quality pilot_quality_v4/quality_summary.json
```

## Licensing
Confirm vendor licences before any commercial use. The 1-minute options dataset README states CC BY-NC 4.0 (per the independent review).
