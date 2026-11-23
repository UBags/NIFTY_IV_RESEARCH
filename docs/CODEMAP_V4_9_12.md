# Code map — NIFTY_IV_RESEARCH V4.9.12 (2026-11-23)

644 tests pass (numpy path, `ZONE_NO_NUMBA=1`; numba optional). Installable package `nifty_iv` (src layout, `pip install -e ".[fast,test]"`),
one console script per tool. Two pipelines share the repo; only the zone pipeline is active. Supersedes `CODEMAP_V4_9_11.md`
(module names below are the post-reorganisation ones; `docs/MODULE_MAP.md` maps old flat names to new).

## Layout

```
src/nifty_iv/
  core/         strategy_spec (margin table, strikes, expiry policies) · contract_specs (lot master) · lineage · cli (shared CLI)
  data/         import_postgres · inspect_parquet · research_cache · migrate · explain_month · migrations/
  zone/         spot · signals · contracts · backtest (Market, PlanMarket, Sim) · paths (PathSim) · coverage · rules · grid
                · portfolio · holdout · legacy/
  diagnostics/  strategy_scan · equity_dd_scan · trade_ledger · stop_compare · reinvest_sim · entry_liquidity
                · dd_cutoffs · dd_bins · dd_recovery · zone_diagnose · pair_exits
  ivcal/        dormant relative-IV pipeline; calendar_research.fill_detail / hedge_cost are the cost model used by zone.paths
tests/  config/nse_holidays.txt  pine/  scripts/  docs/  archive/      data/ and runs/ are not tracked
```

## Zone pipeline (active)

```
PostgreSQL market_data (read-only)
 ├─ nifty-zone-spot ──────────► zone_spot.sqlite  spot_1m (09:15–15:29, from 2020 for EMA warm-up)
 │                                   │
 │        zone/signals.py ◄──────────┘  INDEX v15 port: 45-min bars + 90/180/360 HTF EMAs (lookahead_off) → zone G/R/None
 │          (3-bar persistence) → swing pivots (N,k,m,j) + trigger window tw..tw+16 → entries at bar close
 │          GRID 2×6×4×6×5 = 1,440 swing configs + 5 zone_only_tw2..6 = 1,445 configs
 │
 ├─ nifty-zone-contracts ─────► zone_options_levels.sqlite  listing / targets / opt_1m
 │     plans contracts by running zone/backtest.Sim on PlanMarket (every contract liquid at price 1),
 │     then pulls every minute of only those contract-days, both vendors (resumable by month)
 │
 └─ core/strategy_spec.py  (shared spec, hashed into grid + freeze lineage)
       MARGIN_TABLE (upper end per lot / period lot → margin per unit), entry_lots = ceil(target_fraction × max lots on budget)
       strikes: levels −5..+5 × step (50/100/200 by index); bull sells PE at ATM−L·step, bear CE at ATM+L·step (L<0 = ITM)
       expiry policies: naked (≥ min_short_days, default 4), naked_m1 (current monthly ≥21d), naked_m2 (next monthly)

zone/paths.py  PathSim  ───► zone_paths_levels.sqlite  (bars, signals, paths[blob], gaps, meta incl. args)
   one path per (signal bar × side × structure × level), shared by every config that fires on that bar
   structures: naked, naked_m1, naked_m2, calendar (roll at 15:15 short expiry / long ≤30d), futures_zone, futures_expiry
   entry: all legs matched minute by minute, ≤ --max-participation (0.20) of each minute's volume, whole lots, at (O+H+L+C)/4,
          within --entry-bars windows (3) AND --entry-minutes trading minutes after the signal close (V4.9.12, default 30;
          continues into the next session's opening minutes; paths files built earlier have no minute limit); full size or nothing;
          a window after the first is used only if the zone still holds
   costs: slippage 0.5 × sqrt(max(DTE,7)/7) × (1 + 2/sqrt(volume in lots)) per unit per side; dated STT / exchange / SEBI /
          stamp / GST; brokerage ₹20 per lot per order (per_lot_orders)
   exit (decided at bar k, fills from the next minute) memoised on (held, bar): shorts first, then longs, carrying into later bars
          until closed; settlement on 15:00–15:29 spot TWAP (≥25 minutes, else last spot close); forced long booking after lag
   per bar row: cash_total, cash_pair, base, value, mark, exit_cash, exit_bar/day, roll, 8 exit-session slots
   futures = spot proxy (no basis/volume), 1 pt slippage, carry assumed later (5.5%)
        │
        ├─ zone/coverage.py   gap classification (not_extracted / no_long_expiry / listing)
        ▼
zone/rules.py  Batch (paths × bars matrices) + evaluate(rules) → first firing row per path, at 45-min bar closes
   stops ≈ 1,000–1,250 per structure (atr/eatr/pct/epct/pts/trail/strail/chand/donch/be/gb/time/exp/ema/vr/tr/tod/wkend/stag/
   loss/dloss/mult/mloss 1–10% of margin) + layered c:init|manage|term ; profits 34–50 (tp/target/tpct/ptime)
        ▼
zone/grid.py  run_grid → grid_<side>_<struct>_<lvl>__<stopfamily>.npz  (n,total,wins,sumsq per config×window×profit×stop)
   sequence_all: one position at a time per (config, rule); unfilled entries hold the slot to their last window; data gaps hold
   it to the zone flip (gap policy busy)
   report: fill-rate gate 75%, year band (every full year > 0, worst ≥ 0.70 × mean), modes total / stable, LOYO, fill_bias
   lineage: algorithm_digest (rules file, kernels, layout, holidays, gap policy, strategy_spec) + per-file SHA
   Detail: exact trade-by-trade replay of one rule (used by portfolio, holdout, scans, ledger, stop_compare)
        ▼
zone/portfolio.py  pools → all green×red pairs → executable Account → robust neighbours → walk-forward (none: needs 3 years) → freeze
        ▼
zone/holdout.py    replays the freeze once on 2025-01..2026-07 (code + paths digests, ledger)
```

## Diagnostics (read the paths file; none affects grid / portfolio digests)

| Command | Module | What it does |
|---|---|---|
| `nifty-diag-strategy-scan` | strategy_scan | every strategy vs mean / min-year / worst-trade gates on judged years; `prepare_group` shared by the scans |
| `nifty-diag-equity-dd-scan` | equity_dd_scan | best single green / red per equity-drawdown cap (bar-close marks, exact O(1)-per-trade kernel) |
| `nifty-diag-trade-ledger` | trade_ledger | replays chosen strategies through `LedgerSim` (a `PathSim`) with every fill logged; per-trade and per-year CSVs, combined account with peak margin, interest on margin at `--rate`. Options below |
| `nifty-diag-stop-compare` | stop_compare | 50 alternative stops on a strategy's fixed entries: win ratio, profit factor, payoff, Sharpe, Sortino, Calmar, drawdown |
| `nifty-diag-reinvest` | reinvest_sim | defaults FD 31.25 L at 6.25%, line 0.8:1 at 10%, tax 35%; ledger trades through an FD-backed credit line: margin borrowed to the next trading day after exit, year-end reinvestment of after-tax profit, line = `--leverage` × FDs, tax with loss set-off / carry-forward, ratios and IRR |
| `nifty-diag-entry-liquidity` | entry_liquidity | per entry window: raw volume, minutes traded, minutes big enough for a whole lot, tradable units (why an entry filled or not) |
| `nifty-diag-dd-cutoffs` / `-dd-bins` / `-dd-recovery` / `-zone-diagnose` / `-pair-exits` | | V4.9.8–4.9.10 studies |

`trade_ledger` options:
- size / fills: `--target-fraction` (1 = whole budget), `--max-participation`, `--entry-minutes`, `--entry-bars`
- `--intrabar-stop` (default `mloss7.5`): resting protective stop on the minute's worst price, fills from that minute; `none` = off
- `--exit-at next-bar|same-bar` (default next-bar): same-bar also checks the strategy's price rules on each minute's close
- `--exit-fill adverse|ohlc4` (default adverse): buy-backs at each minute's high (conservative); ohlc4 = as the paths file
- `--slippage` (₹0.5 base, scaled) and `--brokerage` (₹20 per lot per side) explicit; statutory charges by date
- in every replay, the signals, sequencing and bar-close exit decisions come from the paths file; a replay that cannot fill or exits
  earlier does not free the slot for a later signal; `check_rupees` is the match against the paths file (plain replays only)

`core/cli.py`: every command echoes its full command line with all defaults (stderr) and has a descriptive `--help`.

## V3/V4 relative-IV calendar pipeline (dormant)
prepare_research_cache → enrich_cache (quant_v2/v3/fast) → calendar_research (simulator; fill_detail/hedge_cost reused by zone.paths) →
rank_strategies (nested walk-forward, SPA, circular-shift control) → publish_results / lineage. Audits, calibration, power analysis.

## Research state (end of chat 86d808be, 2026-11-23)
- Grid rerun after the reorganisation: no green × red portfolio pair passes the drawdown gate (unchanged conclusion).
- `equity_dd_scan`: green futures picks fail 2025–26 (and omit carry); the naked picks hold up.
- Current pair, full ₹25 L each (never overlapping): green naked −1 `N3_k1.25_m8_j1.5_tw2` ptime5 / atr15x1, red naked −2
  `N5_k0.25_m12_j0.25_tw5` ptime7 / eatr30x1.5. Ledger replay at 25% participation / 225-minute window, 2021-06..2026-06, after
  slippage, charges and 10% interest: ₹78.8 L net (bar-close exits), ₹80.4 L with the mloss7.5 intrabar stop. Reinvestment at
  0.8:1 and 35% tax (before mloss7.5): gain ₹94.6 L, IRR 30.9%, max drawdown 17.5%.
- Minute-checked strategy stops (`--exit-at same-bar`) are worse (₹65.2 L). Recommended execution: strategy stops at bar close +
  resting disaster stop (now the ledger default).
- Red's deep-ITM calls are thin: at full size 5 entries fail at 20% / 135 minutes and all fill at 25% / 225 minutes.
- The pair was chosen on data including 2025–26; the only clean test is data after June 2026.
- Pending: paths rebuild at `--max-participation 0.25 --entry-minutes 225 --entry-bars 6` (exact sequencing); freeze the pair;
  forward-test. One green trade (signal 2026-05-27) is unvalued: settlement price missing.
