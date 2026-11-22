# Code map — NIFTY_IV_RESEARCH V4.9.11 (2026-11-14)

601 tests pass (numpy path, no numba), SHA256SUMS verify. Two pipelines share one repo; only the zone pipeline is active.

## Zone pipeline (active)

```
PostgreSQL market_data (read-only)
 ├─ zone_spot.py ─────────────► zone_spot.sqlite  spot_1m (09:15–15:29, from 2020 for EMA warm-up)
 │                                   │
 │        zone_signals.py ◄──────────┘  INDEX v15 port: 45-min bars + 90/180/360 HTF EMAs → zone G/R/None
 │          (3-bar persistence) → swing pivots (N,k,m,j) + tw window → entries
 │          GRID 2×6×4×6×5 = 1,440 swing configs + 5 zone_only_tw2..6 = 1,445 configs
 │
 ├─ zone_contracts.py ────────► zone_options_levels.sqlite  listing / targets / opt_1m
 │     plans contracts by running zone_backtest.Sim on PlanMarket (every contract liquid at price 1),
 │     then pulls every minute of only those contract-days (resumable by month)
 │
 └─ strategy_spec.py  (shared spec, hashed into grid + freeze lineage)
       MARGIN_TABLE (upper end per lot / period lot → margin per unit), entry_lots = ceil(0.40 × max lots on 25 L)
       strikes: levels −5..+5 × step (50/100/200 by index); bull sells PE at ATM−L·step, bear CE at ATM+L·step
       expiry policies: naked (≥ min_short_days), naked_m1 (current monthly ≥21d), naked_m2 (next monthly)

zone_paths.py  PathSim  ───► zone_paths_levels.sqlite  (bars, signals, paths[blob], gaps, meta)
   one path per (signal bar × side × structure × level), shared by every config that fires on that bar
   structures: naked, naked_m1, naked_m2, calendar (roll at 15:15 short expiry / long ≤30d), futures_zone, futures_expiry
   entry: all legs matched minute by minute, ≤20% of minute volume, whole lots, up to 3 windows, full size or nothing
   exit memoised on (held, bar): shorts first, then longs; settlement on 15:00–15:29 TWAP; forced long booking after lag
   per bar row: cash_total, cash_pair, base, value, mark, exit_cash, exit_bar/day, roll, 8 exit-session slots
   futures = spot proxy (no basis/volume), 1 pt slippage, carry assumed later (5.5%)
        │
        ├─ zone_coverage.py   gap classification (not_extracted / no_long_expiry / listing), --tenor-check
        │
        ▼
zone_rules.py  Batch (paths × bars matrices) + evaluate(rules) → first firing row per path
   stops ≈ 1,000–1,250 per structure (atr/eatr/pct/pts/trail/chand/donch/be/gb/time/exp/ema/vr/tr/tod/wkend/stag/
   loss/dloss/mult/mloss 1–10% of margin) + layered c:init|manage|term ; profits 34–50 (tp/target/tpct/ptime)
        ▼
zone_grid.py  run_grid → grid_<side>_<struct>_<lvl>__<stopfamily>.npz  (n,total,wins,sumsq per config×window×profit×stop)
   sequence_all: one position at a time per (config, rule); data gaps hold the slot to zone flip (gap policy busy)
   windows: train_YYYY, train_full [start, split), test_YYYY = in-sample year slices; split hard-capped at 2025-01-01
   report: fill-rate gate 75%, year band (every full year > 0, worst ≥ 0.70 × mean), modes total / stable
           (lower quartile over 1-step neighbours), LOYO diagnostic, fill_bias (ATM filled vs unfilled), buy & hold
   lineage: algorithm_digest (rules file, kernels, layout, holidays, gap policy, strategy_spec) + per-file SHA
   Detail: exact trade-by-trade replay of one rule (used by portfolio, holdout, scans)
        ▼
zone_portfolio.py  pools (top by total / worst year / stability) → curves → search all green×red pairs
   screen (target, floor, band) → combo_eval (drawdown, funds, lots) → qualify in executable Account
   (orders admitted at signal close vs funds & margin table) → _robust neighbours → walk_forward → verdict
   allocated basis 5/12 per leg; walk-forward needs 3 full training years → none exists; freeze → portfolio_freeze.json
        ▼
zone_holdout.py   replays the freeze once on 2025-01..2026-07 (code + paths digests, coverage check, ledger/registry)

Diagnostics on the paths file (V4.9.8–4.9.11):
   dd_recovery.py, dd_bins.py (drawdown → recovery / bins), dd_cutoffs.py (mloss cut-offs, one-at-a-time, --optimise),
   zone_diagnose.py (half-year market & trade anatomy, exit-after-k, delayed entry),
   pair_exits.py (fixed entries, searched exits, OOS replay), strategy_scan.py (numba-able kernel, all rules,
   13% mean / 10% min / 3% worst-trade gates on 2022–24, 2025–26H1 flagged)
Legacy zone tools: zone_backtest.py (Sim, Market — still imported for Market/planning), zone_futures.py, zone_report.py, zone_summary.py
```

## V3/V4 relative-IV calendar pipeline (dormant)
prepare_research_cache → enrich_cache (quant_v2/v3/fast) → calendar_research (simulator; fill_detail/hedge_cost reused by zone_paths) →
rank_strategies (nested walk-forward, SPA via stats_v4, circular-shift control) → publish_results / lineage. Audits: audit_quality,
audit_contract_coverage, audit_spot_sources, calibrate_slippage/spa, power_analysis, compare_*.

## Research state at end of chat 3dca0828 (V4.9.6 → 4.9.11)
- V4.9.6 bear naked −4 result was a fill-selection artefact → V4.9.7 entry model, fill gate, band, 3-year WF rule, 5/12 allocation.
- Monthlies and calendars untestable in-sample (far expiries listed regularly only from 2024).
- No portfolio pair passes drawdown; low-drawdown pairs were beta + fitted exits and failed 2025–26 (pair_exits).
- 2025–26 has been looked at (dd_cutoffs etc.): no untouched holdout remains; clean test = data after June 2026.
- strategy_scan: 19,808 pass 13/10/3 on 2022–24; long side fails 2026H1 almost entirely; 6 pass both periods = 2 strategies:
  bear naked −2, N5_k0.25_m12_j{1.0,1.25}_tw5, stop eatr{30,45,60}x0.25 (profit none / ptime5). Pending: write frozen spec, forward-test.
