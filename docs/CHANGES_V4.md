# V4.9.16 (2026-11-24)

| Item | Change |
|---|---|
| Period floors (user) | `scripts/pair_picker.py --min-half X`: every train half-year of the pair > X; `--min-quarter X --quarters FILE`: every train quarter > X (default off). Partial first periods (2021H2 = Sep-Dec, 2021Q3 = Sep only) are skipped unless `--floor-partial`; `--include-oos` also applies the floors to 2026H1 / 2026Q1-Q2 (then nothing is out of sample). `--loyo` drops the held-out year from the floors. v3: the 5 default picks already have every half > 10%; with `--include-oos` pairs 3-4 (2026H1 -7%, -6%) are replaced (2026H1 +9%, +20% / +20%, +20%); held-out pair-halves negative 2-4 of 45, mean 24.5-25.3% |
| `scripts/strategy_periods.py` | Quarter (or half) returns per strategy of a candidates.csv from the paths file with the wide search's engine (same windows); checks against candidates.csv when the period matches (synthetic: 141 strategies, 0 differ) |
| Tests | `tests/test_pair_picker.py` 4 (floors, partial periods, out-of-sample floor, quarters file) |
| `scripts/stage1_pool.py` (user) | A wider pool for pair_picker: the top `--top` (default 1000) option strategies per side from the wide search's stage-1 files (every strategy stage 1 kept, not only candidates.csv's 40 per family per scoring), within `--max-trade-loss` 0.099 and `--min-trades` 20, identical results once, ranked by the stage-1 score; recomputed exactly (halves, quarters, trades, worst trade, train account drawdown) into candidates.csv + quarters.csv. Synthetic run: stage-1 vs recomputed 0 differ; worst trade and drawdown equal candidates.csv for all 100 shared strategies. pair_picker on 1000 x 1000 pairs with quarters, funnel and LOYO: 7 s |
| `stage1_pool.py --select v3` (user) | The wide search's pool rule with a larger quota: the best N of every structure under each of its scorings (all train years, each year left out), united per side; N is the largest that keeps the union within `--top` (all if fewer). Synthetic: the rule applied to the stage-1 files gives exactly candidates.csv's option strategies at N = 40 |
| `scripts/pair_report.py` | Quarter-by-quarter and half-year returns of the pairs in a pair_picker output and of their equal-weight basket (negatives, worst, mean; train and out of sample); `--csv` |
| `scripts/pair_presence.py` | For pairs picked from one pool: is each leg in another pool (stage-1 rank), does the pair qualify there, how many qualifying pairs rank above it, and which pick took its signal if it was not picked |
| `--min-trades` default 20 -> 15 (user) | `scripts/pair_picker.py` and `scripts/stage1_pool.py`: a leg needs at least 15 train trades |
| `pair_picker.py` basket, `--include-oos half|quarter`, stitched held-out CAGR | `--loyo` also prints the held-out basket halves stitched into one 2021H2-2025H2 record (each year from the pick made without it): simple yearly return and post-tax CAGR. `--include-oos` takes an optional value: both (bare flag, as before), half (only the half floor also on 2026H1), quarter (only the quarter floor also on 2026Q1-Q2). Reports the picked pairs together at 1/k of the capital each (halves, 2026H1, quarters, worst full quarter) and, with `--loyo`, the basket's held-out halves. Floor labels show decimals (2.5%, was rounded to 2%) |
| `pair_picker.py --funnel` | Pairs left after each criterion in turn (distinct green / red signals), the worst-period distribution before each floor and which periods bind most often; printed automatically when no pair qualifies |

# V4.9.15 (2026-11-24)

| Item | Change |
|---|---|
| Per-trade loss limit (user) | `nifty-diag-wide-search --max-trade-loss` default 0.03 -> 0.099. In v3, 619 of 681 option strategies (31% of their trades) went beyond 3% at full size, so 3% forced every strategy to 1/4 size; beyond 9.9-10% only 3% of those strategies' trades. 9.9% keeps 12,285 pairs (all 9 train halves > 0, post-tax CAGR > 20%; 7,200 also positive in 2026H1) against 12,551 at 10% and 0 at 3-5%. Worst losses are at 45-minute bar closes: true intraday worst somewhat deeper |
| `scripts/trade_loss_counts.py` | Per strategy, trades whose worst open loss exceeds 3 / 5 / 7.5 / 9.9 / 10 / 12.5% of capital, by entry year. With `--entry-end` = split its worst trade equals the wide search's `worst_trade` (checked on 681 real strategies: max difference 5e-6, the CSV rounding) |
| `scripts/pair_picker.py` (user) | Green + red pairs from candidates.csv with a different strategy in every pair (`--distinct signal` default: no signal configuration repeats on either side; `level`, `rule`). Criteria: per-leg worst trade (`--max-trade-loss` 0.099), train drawdown (`--max-dd` 0.20), trades (`--min-trades` 20); pair post-tax CAGR (`--cagr-floor` 0.20), positive halves (`--min-positive` 0.85; all-positive ranked first), range (`--range-cap` 0.40); ranked by the composite objective. `--loyo` repeats the pick with each year left out. v3: 5 pairs all 9/9 positive, post-tax CAGR 43-49%, 2026H1 +29, +26, -7, -6, +18%; held-out pair-halves 45, negative 2, mean 24.5%, worst -10.2%. `tests/test_pair_picker.py` (2) |

# V4.9.14 (2026-11-24)

| Item | Change |
|---|---|
| Multi-leg structures (user) | `strategy_spec.COMBOS`, path engine and rules: `vert_w1`, `vert_w2` (bull put / bear call credit spread, long leg 1-2 levels further OTM, weekly expiry as naked); `married` (married put / protective call: future + long protective option), `covered` (covered call / covered put: future + short option on the profit side), `collar_w1`, `collar_w2` (future + protective option + short option 0-1 levels further OTM on the profit side), `longopt` (futures replacement: long call / long put), `backspread_w1`, `backspread_w2` (call ratio backspread: short 1, long 2 calls 1-2 levels further OTM; bull only, as listed). Futures combos and long options: current monthly expiry >= 21 days (user), end at expiry or zone change. Levels 0-3 OTM for the sold / hedge leg (user). All legs fill together minute by minute (ratio legs in proportion), full size or nothing |
| Margin and size (user) | Entry lots = floor(naked lots x factor): 2-leg capped loss 1.6x (margin 60% of naked), 3-4 legs 0.9x (110%), covered and long options 1.0x (100%); `mloss` uses the structure's margin share |
| Rules per structure | Credit spreads: the naked premium rules (tp, loss, mult) on the net credit; long options: the calendar ones (tp, dloss) on the debit; futures combos and backspreads: spot-, time-, trailing- and margin-based rules only (base = the position's margin); `exp` for every expiring structure. Existing structures' rule sets unchanged |
| Futures carry (user) | `nifty-zone-paths --carry` (default 0.055): futures and futures legs priced at spot x (1 + carry x days to expiry / 365), settling at the spot reference; `futures_zone` (no expiry): 30 days of carry at entry, paid down while held. 0 = the earlier spot proxy (bit-identical: checked) |
| Data | The extraction is targeted: covered, collar, long-option and backspread legs (the other option type on that side's days) were never extracted. `nifty-zone-contracts --structures <combos>` plans them (`plan_combo_targets`; use a new --out). `scripts/combo_build_cmds.py` prints extraction, paths and merge commands with the existing paths file's settings; `scripts/merge_paths.py` merges (checks bars, signals, sizing) |
| Checks | `tests/test_combos_v4914.py` (12): legs and strikes both sides, margin factors, expiry policies, rule families, virtual future = spot x carry, expiry settlement of married put, covered put, vertical (loss capped at the width) and 1x2 backspread against hand payoffs (ratio mutation caught), carry on futures_expiry / futures_zone, planner, merge. Existing structures (carry 0): synthetic paths identical to V4.9.13. Grid, strategy scan and wide search run on the new structures (wide-search pool vs exact engine: 0 differences) |

# V4.9.13 (2026-11-24)

Speed only: every result is bit-identical to V4.9.12 (no tolerance). Reviewed against proposals from Gemini and ChatGPT;
only changes that kept exact equality and measured faster were taken.

| Item | Change |
|---|---|
| `zone/grid.py` | Exit-row dedup: rules whose exit rows (J) are identical on every path give identical XB, window values and sequencing, so each distinct row is sequenced once and the results are copied back (`_dedup_rows`). Gain depends on how many stops never fire or fire together (synthetic fixture: 1.7% distinct, grid 6.2 s -> 3.3 s; production to be measured). The sequencing kernel is unchanged: the proposed rule tiling and per-path window lists measured 0.52-0.83x (slower) and were not taken |
| `zone/rules.py` | `chand`: one rolling maximum per lookback shared by its multipliers; `donch`: the previous-L low/high gathered once per lookback (latest lookback kept). chand+donch 4x, a full stop set 1.85x (2,025 calendar paths) |
| `zone/paths.py` | Day cache is least-recently-used (was first-loaded: a day still in use could be evicted and re-read from SQLite); whole-lot per-minute capacities computed once per (contract, lot, participation) per cached day |
| `strategy_scan.py`, `equity_dd_scan.py` | Same exit-row dedup (outputs byte-identical on the end-to-end fixture) |
| `wide_search.py` (user: redo the wide search for account stability) | `nifty-diag-wide-search`. Stage 1: every group passing the fill gate, all configurations x all rules (layered included) at once with the grid's sequencing; train period returns (entries and exits in [--train-start, --split)), out-of-sample returns, train trades; each configuration's --per-config best rules kept under --rank (lcb = yearly return - one yearly sd; sharpe; worst), scored on all train years and with each train year left out; one resumable file per group. Stage 2: the --pool best per scoring recomputed with an exact engine (stage-1 figures must agree bit for bit; reported), then the account built by greedy forward selection of (strategy, size in --weights) on trade_structure's objective (worst period, yearly return >= --min-annual, spread), peak margin exposure <= --max-exposure, then single replacements / resizes. Stage 3: exit baskets (--baskets) on the chosen strategies. Leave-one-year-out over the whole procedure (pool, account, baskets chosen without the year, the year scored); out-of-sample periods reported. The engine is trade_structure's simulate / period_returns / stats / objective on integer arrays (numba): identical results (tests: 2,100 random cases; stage 1 vs brute force; mutation-checked) |
| `wide_search.py` composite objective (user, 2026-11-24) | `--objective composite` (default): 0.4 / max(range of train half-year returns, --range-floor 5%) counted only when post-tax CAGR > --cagr-target 20%, + 0.4 x 7 x [post-tax CAGR > 20%] (user: binary; `--ramp-from` optional ramp), + 0.2 x 40 x post-tax CAGR, - 2.8 x [half-year range > --range-cap 40%] (user). Target configurable: --cagr-target (default 0.20). Post-tax: 35% on each calendar year's net gain after loss carry-forward, after-tax years compounded on capital, no interest. Used for the account, the baskets and (`--rank composite`) stage-1 screening of single strategies at full size. `--split` default 2026-01-01 (2025 in training; 2026H1 the only out-of-sample half; leave-one-year-out over 2021-25). `--objective floor` = the previous worst-half objective |
| `wide_search.py` v2 (first real run: no usable account) | First run (2026-11-24, real data): the pool was 1,092 / 1,108 bull futures candidates (full-size long-NIFTY futures look best in 2021-24); all of them lost in 2026H1 (median -50%); the account was one strategy (every train half positive, 2026H1 -57.5%); leave-one-year-out held-out halves negative 3/7, worst -89%. Causes: no risk limit, no pool diversity, single-best ranking. Now (user decisions): worst open loss of a trade x size <= --max-trade-loss 3% of capital, account equity drawdown (daily mark-to-market) <= --max-dd 20%, both in stage 1 (trade loss at the smallest size) and in stages 2-3, leave-one-year-out folds without the held-out year's trades and days; pool = --pool-per-family best per side x structure; --robust neighbourhood median ranking over swing-filter configurations; groups without train entries skipped (calendars). Futures kept, under the limits |
| `trade_structure.py` | The prior-trade feature no longer rescans every earlier trade (heap over exit dates; the original scan if entries ever go back in time): O(N log N) instead of O(N^2) |
| Lineage | `grid.algorithm_digest` hashes rules.py and the grid functions, so grid folders built by V4.9.12 no longer verify: rebuild them (`nifty-zone-grid ... --resume` recomputes each file). `portfolio.code_digest` changes too (any freeze must be redone). Stored paths files stay valid |
| Production parity (2026-11-24) | `scripts/grid_parity_rebuild.py` on the real paths file (all six structures, layered policies, 89 holidays): the V4.9.13 grid equals the V4.9.12 grid in all 2,682 files, 89,460,466,522 cells, 0 differences; build 1.05 h vs ~1.7 h (old estimate from file timestamps, excludes the first group) |
| Checks | `tests/test_perf_v4913.py` (11 tests: old kernel verbatim, rules one at a time vs together, slice/caps vs the old arithmetic, LRU, prior-trade feature vs the quadratic scan); end-to-end on a 30-session synthetic market: paths rows, all 2,736 grid arrays and both scanners' CSVs identical to V4.9.12; `scripts/compare_grid_dirs.py` for the same check on production grids. 667 tests pass with and without numba |

# V4.9.12 (2026-11-23)

| Item | Change |
|---|---|
| Entry window (user) | `nifty-zone-paths --entry-minutes` (default 30): a first entry must reach full size within this many trading minutes after the signal bar's close (continuing into the next session's opening minutes if the day ends first), still at most --max-participation (default 0.20) of each minute's volume and inside --entry-bars windows. Before: up to 3 x 45 = 135 minutes. Paths files built earlier have no limit (behaviour unchanged when replayed) |
| `trade_ledger.py` | `--max-participation`, `--entry-minutes`: replay a strategy's trades under other fill settings without rebuilding the paths file (signals, sequencing and exit decision bars stay the paths file's) |
| `trade_ledger.py --exit-at` (user) | `next-bar` (default): rules checked at 45-minute bar closes, as everywhere else. `same-bar`: price-based rules (atr, eatr, pct, epct, pts, target, tpct, mloss, loss, tp, mult) also checked on every minute's close; the exit fills from the following minute under the same participation rules. Time, ptime, trailing, zone-change and expiry exits keep their bar-close decision. Ledger replay only (the grid and scans stay bar-close) |
| `trade_ledger.py --intrabar-stop` (user: recommended behaviour as default) | A protective stop resting at the broker, on top of the bar-close strategy rules: checked on every minute at the minute's worst price (index low / high, option high for a short leg), filled from that minute. Default `mloss7.5` (loss >= 7.5% of margin), chosen from a sweep on green atr15x1 + red eatr30x1.5 (25% / 225 min, full size): none 78.84 L, mult1.75 80.36 L, mult2 79.17 L, mult2.5 78.84 L (never fired), mloss5 74.02 L (fired 24x), mloss7.5 80.36 L (fired 2x); any stop family (mult, mloss, loss, atr, eatr, pct, epct, pts); `none` = off |
| Every command (user) | `--help` lists each argument's meaning, accepted values and default (`core/cli.py`); each run starts by printing the full command with every default filled in, plus the options left unset |
| Cost and execution defaults (user) | `trade_ledger --exit-fill adverse` (default): every buy-back fills at its minute's high plus slippage; sales unchanged at (O+H+L+C)/4 less slippage (new fill-price mode `adverse_buys`); `--exit-fill ohlc4` reproduces the paths file. `--slippage` (default Rs 0.5 base, scaled by days to expiry and minute volume) and `--brokerage` (default Rs 20 per lot per side, an upper bound on Rs 20 per executed order) are explicit; STT / NSE / SEBI / stamp / 18% GST follow the dated schedule (option STT 0.15% of sell premium from 2026-04-01). The ledger's replay check applies only when every setting equals the paths file's. `reinvest_sim` defaults: FD 31.25 L at 6.25% (0.8:1 = a 25 L line), tax 35%, line at 10% |
| `reinvest_sim --loss-funding` (user) | `fd-surplus` (default): a year-end loss (or tax the year's income does not cover) is paid by breaking FDs while they exceed the capital put in so far; only the part that would take them below it is paid in as new capital; the line is then capped at --leverage x the remaining FDs. `pay-in` = the earlier behaviour (all paid in). On green atr15x1 + red eatr30x1.5 (conservative exits, 35% tax): put in 42.66 L -> 32.50 L, IRR 30.9% -> 31.4% |
| `trade_structure.py` (user) | `nifty-diag-trade-structure`: exit baskets (each position split into --baskets slices, each with its own profit/stop pair from ~140 candidates; a new signal waits until every slice has exited), searched on the combined account to maximise the worst half-year / quarter subject to --min-annual, on entries before --split; leave-one-year-out check of the search; walk-forward meta-labelling sizing (logistic model on signal-time features, terciles -> --size-map lots) and volatility-managed sizing, each against constant size at the same average exposure; strategies' period-return correlation |
| New tools | `equity_dd_scan`, `trade_ledger`, `stop_compare`, `reinvest_sim` (FD-backed line, year-end reinvestment, tax, --leverage, --leverage-interest) |

# V4.9.11 (2026-11-14)

| Item | Change |
|---|---|
| `strategy_scan.py` (rewritten) | The two-stage version screened returns from the grid files, but the binding requirement (the worst open loss of a single trade) is not in them: 3.9 million combinations passed the return screen (3.3 million after merging identical rules). It now computes everything straight from the paths file, group by group, all rules at once as the grid does: for every (configuration, profit rule, stop rule) one position at a time with the grid's sequencing and data-gap occupancy, the rupee P&L and the worst trade (worst bar-close mark up to the exit, or the final result) by entry year, for 2021 .. 2026H1. The three requirements are applied exactly; only passes are kept. No grid folder needed (`--grid` optional, to check the holiday list). A reference replay (`evaluate`) and tests confirm the vectorised figures equal a trade-by-trade replay |

# V4.9.10 (2026-11-14)

| Item | Change |
|---|---|
| Fix: "closest" pairs ignored the year band | When no pair passes, zone_portfolio.py lists the closest failing pairs; their ranking now adds the shortfall from the band (a year <= 0 or the worst full year below band x the mean). Before, the printed "in-sample best (closest)" could violate the band (e.g. 98% / 59% / 28%) |
| `strategy_scan.py` | Every strategy in the grid (side x structure x level x configuration x profit x stop, testable groups) against: mean yearly return >= 13%, every judged year >= 10%, every trade's worst open loss <= 3% of --capital (default 25 lakh), judged on 2022-24; 2025 and 2026H1 reported with a pass flag. Two stages: grid-file screen (points x the group's median units per year, 25% slack), then an exact replay of the survivors from the paths; identical-trade rules reported once |
| `pair_exits.py` | Fixed entries (one green signal configuration, one or more red), searched exits: each leg's grid rules (profit x stop, margin stops, layered) prescreened from the grid files, rules with identical trades counted once, every green x red pair screened for --target a year, every judged year >= --floor, the band; the best replayed through the executable account and ranked by drawdown; the best --top replayed on 2025-01 .. 2026-07 |

# V4.9.9 (user's directions of 2026-11-13: retest with margin stops; diagnose the bull strategies)

| Item | Change |
|---|---|
| Margin stops in the grid | `mloss` levels 1 / 1.5 / 2 / 2.5 / 3 / 3.75 / 5 / 6.25 / 7.5 / 10% of the position margin (V4.9.8: 2.5 .. 10); layered policies use 2 / 3 / 5% as initial protection. The whole-period sweep (dd_cutoffs.py) showed plateaus at 2-3.75%, with 2% best for bear naked +2. The grid is recomputed (new rule set) |
| `zone_diagnose.py` | Market by half-year; trade anatomy by half-year (mean / total points per unit, win %, bars held, zone-change exits, best / worst open points, give-back, whipsaw share); exit after k bars; entry delayed by d bars. Per unit, so lot changes do not distort |

# V4.9.8 (user's direction of 2026-11-13: drawdown cut-offs as a share of margin)

| Item | Change |
|---|---|
| Margin-based stop | New stop family `mloss X` (zone_rules): exit at the first 45-minute close where the trade's mark-to-market loss reaches X% of its margin (units x the strategy_spec table margin per unit on the entry day), so it scales with lots and the period's margin. X in 2.5 / 3.75 / 5 / 6.25 / 7.5 / 10, for every structure; 3.75 / 5 / 7.5 also as the initial-protection part of the layered policies. At 40% lots the position margin is Rs 10.0-10.4 lakh in every period, so 3.75 / 5 / 7.5% ~ Rs 37.5K / 50K / 75K |
| Grid | The rule set changed: the grid is recomputed (lineage refuses V4.9.7 grids) |
| `dd_cutoffs.py` | Fixed cut-offs per strategy (bear naked -2: 5%, bear naked 0: 3.75%, bear naked +2: 7.5%, bull naked -2: 3.75%, bull naked 0: 5%, bull naked +2: 3.75%, bull futures_zone: 7.5%, bear futures_zone: 5%), applied in-sample (before 2025) and out of sample (2025-01 .. 2026-07, the holdout period): no cut-off vs with cut-off; a stop exits at the path's stored fills after the triggering close, not at an idealised -X |
| `dd_cutoffs.py` (2nd revision) | Adds the one-position-at-a-time view (total P&L, % a year on the allocated 5/12 of 25 lakh and on 25 lakh, trade-close drawdown) and `--optimise`: each cut-off also chosen on all the data (none or 2.5 .. 20% of margin) by the largest one-at-a-time total, reported split into the two periods |
| `dd_recovery.py`, `dd_bins.py` | The drawdown diagnostics used to choose the cut-offs, now in the release |

# V4.9.7 (user's directions of 2026-11-13, after the first real-data grid)

## Finding that drove it
On the real V4.9.6 grid the best result, bear naked level -4 (call 800 points in the money), was a fill-selection artefact. For the same
config and exit rule, ATM trades on the 11 signals where level -4 also filled made +1,779 points (100% winners, no zone-flip losses);
the other 34 signals made -1,601. An entry happened only if the strike printed tradable volume in the 45 minutes after the signal, and
deep-ITM strikes print mostly when the market moves toward them, i.e. in the trade's favour: whether a trade happened depended on price
action after the signal.

## User decisions implemented
| Decision | Change |
|---|---|
| Entry may use 3 bars, not 1 | `zone_paths.py --entry-bars 3` (default): an entry fills minute by minute over the windows of the signal bar and the next two (each window = the minutes of the bar after it). If the zone changes before a later window, no trade (`zone_flip_before_fill`). Each window booked on its own day with that day's costs. The path's rows start after the bar whose window completed the entry (`first_bar`); the grid (`Batch`) and the account align to it. A calendar roll re-entry still uses one window |
| Participation 20% | `--max-participation` default 0.20 (was 0.10) |
| One strategy = 40% of the maximum lots, rounded up | `strategy_spec.entry_lots` = ceil(0.40 x the lots whose upper-end table margin fits 25 lakh): 9 x 50 (2022), 13 x 25 (mid-2024), 4 x 75 (2025), 5 x 65 (2026). `zone_paths.py --target-fraction` (default 0.40, with `--target-margin`); the portfolio's `Sizing` and the holdout read the same rule from the paths file; a portfolio leg trades that full size or nothing (`--leg-lots` default 0 / full). Returns stay P&L over `--capital` (25 lakh) |
| Full size or nothing | An entry that has not reached its full size by the end of its last window is not traded (`no_liquid_entry`); the slot is held while the order works (to the last window tried), then free |
| Targets per strategy on its allocated capital | Portfolio `--capital-basis allocated` (default) with `--allocation 5/12`: each traded leg must meet 5/12 of the account's target, floor and drawdown limit; a green + red pair 10/12. With the defaults: one leg 9.17% of 25 lakh a year, a pair 18.33%; floor 0.70 x target (`--floor` default now 0.70 x `--target`, was 0.17): one leg 6.42%, a pair 12.83%; drawdown limit 1.04 lakh per leg, 2.08 lakh per pair. Returns are still reported on the full 25 lakh (and on the allocated share for the in-sample pick). The holdout scales its verdict by the frozen allocation. **Simplification:** a nominal 5/12 in every year (actual entries are 9/21, 13/31, 4/10, 5/12 of the maximum lots and their margins differ); to be tuned to actual margin use once the working strategies are known. `--capital-basis account` restores V4.9.6 |
| No ex-ante liquidity eligibility | Not added (next-month liquidity spikes now and then) |
| Walk-forward needs >= 3 full years of training before a test year | `zone_grid.py` / `zone_portfolio.py --min-train-years 3`. With data from Sept 2021 and the holdout from 2025 there is no walk-forward year: the in-sample choice goes to the holdout, which is the forward test. The portfolio's out-of-sample verdict is INSUFFICIENT EVIDENCE by construction until then |
| Balanced across years first | Selection band (`--band-floor 0.70`): every full training year positive and the worst >= 70% of the mean full year. Grid: both ranking modes consider only cells inside the band (stability neighbours need not be); portfolio: screen and executable qualification gate on the account's full years |
| Fill-rate gate 75% | `--min-fill-rate 0.75` (grid and portfolio): a group (side, structure, level) whose entries completed less often is untestable, never ranked, listed in `untestable_groups` |

## Added diagnostics
| Item | Detail |
|---|---|
| Leave one year out | Report only, never used to choose: pick on the other full years (band over them), score the year left out; `leave_one_year_out` per family, LOYO columns in `grid_walk_forward.csv` |
| Fill selection bias | `grid_fill_bias.csv` / report `fill_selection_bias`: for each option group, the ATM natural-end result per unit of the same signals where the group filled vs where its entry did not complete; Welch t, flagged at abs(t) >= 2 |
| Fill statistics | Per group in the grid index: filled, entry_not_completed (by reason), data_gap, fill_rate |
| Paths heartbeat | `zone_paths.py` prints bar k/N, session, live paths, stored paths, bars/s and ETA every 60 s |

## Fixes
| Item | Change |
|---|---|
| Console report printed `np.str_('...')` | Plain values (`print_summary`) |
| F496-B coverage moneyness reversed for bull puts in levels mode | Labels from the gap row's strike - ATM in points |
| Portfolio code digest omitted `strategy_spec.py` (margin table, entry size) | Added: a change there invalidates a freeze |

## Needs a rebuild
Contracts and tenor coverage are unchanged (same strikes and structures). The paths (entry model and size), the grid (algorithm 4.9.7) and the portfolio must be rebuilt; old grids are refused by the lineage check.

# V4.9.6 (restructure on the user's directions of 2026-11-12, and ChatGPT's V4.9.5 review)

## User decisions implemented
| Decision | Change |
|---|---|
| Margin, not a lot count, limits the account: total margin of open positions <= 25 lakh, margin always at the upper end of the user's table | New `strategy_spec.py`: `MARGIN_TABLE` (2004 onwards: period, lot, NIFTY range, margin per lot low / high, context), used at the upper end as a margin per unit (high / period lot), so transition-window contracts are charged by their own lot. `zone_paths.py --target-margin 2500000`: each entry is the whole lots whose table margin fits the budget on the fill day (21 x 50 in 2022, 31 x 25 mid-2024, 10 x 75 in 2025, 12 x 65 in 2026). Portfolio `--margin-model table` (default; `rates` kept), `--max-margin` (default = capital), leg sizes = tenths of the budget, `--margin-buffer` default 0, `--max-open-lots` default off, `--calendar-margin-factor` (default 1: no spread benefit assumed). Extend the table in code when data is added |
| 22% = simple yield on 25 lakh | Default unchanged (`--return-basis simple`) |
| Strike distances on BOTH sides of ATM, step by index level | `--offset-mode levels` (planner, paths, contracts): levels -5..5, > 0 OTM, < 0 ITM, 0 ATM; green PE at ATM - level x step, red CE at ATM + level x step; step 50 / 100 / 200 below 5,000 / 12,000 / above (ATM rounded to 50 below 5,000, else 100). Default `points` mode unchanged |
| Monthly expiries | New naked structures: `naked_m1` (current-month monthly, >= 21 calendar days on the fill day, else `policy_ineligible`: a deliberate skip that never holds the slot) and `naked_m2` (next-month monthly). Monthly = listed expiry in the last 7 days of its month. Not listed that day: `no_short_expiry` (a data gap). No rolling, no extra entries. `zone_contracts.py --structures naked,naked_m1,naked_m2,calendar --offset-mode levels` plans them (a new extraction: the plan differs) |
| Executable account is the source of truth | Every screen-passing pair (no shortlist) is replayed through the executable account; executable passers are ranked on their EXECUTABLE metrics (drawdown, worst year, calmar, dispersion); canonical columns (`overall`, `worst_year`, `daily_close_drawdown`, `assumed_margin_peak`, `year_*`) are executable, `screen_*` keep the screen; `--qualify-max` (default 0 = all) and a stop before the end is `search_incomplete`, never "no pass" |

## ChatGPT V4.9.5 findings
| Finding | Decision | Change |
|---|---|---|
| F495-01 shortlist truncation | Accepted | See above: exhaustive replay |
| F495-02 "minimum full years" counted part years | Accepted (my naming error) | `full_calendar_year` per year; the evidence rule counts complete calendar years only; part years >= `--min-year-days` still face the floor |
| F495-03 bar check relative to the median | Accepted | Holdout checks every regular session for all 9 expected 45-minute bars (10:00 .. 15:15, 15:30); declared short sessions excepted (freeze `short_sessions`) |
| F495-04 ranking by screen metrics | Accepted | See above |
| F495-05 7-day slack could impute a missing final week | Accepted | `horizon_end` takes the period to its end only if every weekday between the last session and it is a holiday (run holiday list; without one, none may be missing) |
| F495-06 holdout omitted exposure | Accepted (my omission) | Snapshot passed; exchange lots and gross contracts reported |
| F495-07 robustness is screen-based | Accepted (labelling) | `screen_neighbour_*` |
| F495-08 ledger resettable by copying | Accepted in part | `zone_holdout.py --registry` (default `~/.nifty_holdout_registry.json`): a central record also checked; true tamper-proofing is out of scope |
| F495-10 lower-level combine() | Accepted (defensive) | `prepare` refuses a trade that exits after the window without a window-end value |

# V4.9.5 (ChatGPT's V4.9.4 review)

| Finding | Decision | Change |
|---|---|---|
| F494-01 training funds gate (previous close) can pass a pair the executable account cuts | Accepted | The vectorised search stays the exhaustive screen; its passing pairs are then replayed in rank order through the same chronological account as the walk-forward (flat at the training start, admission at each decision against funds, margin, lot cap) and the gates applied to that account (`qualify()`). The pick is the first pair passing there; up to `--qualify-top` (200) are checked and the count reported (`executable N of M replayed`). None qualifies: no new entries that year (`no_executable_pass`); the freeze needs an executable pass |
| F494-02 holdout coverage missed systematic missing days | Accepted | Exact check against expected sessions (weekdays not in the frozen holiday list) plus a per-session bar-count check (fewer than half the usual bars); without a holiday list the holdout refuses unless `--weak-coverage-check` (<= 8% missing weekdays, labelled weak). Option-data gap events in the holdout are reported |
| F494-03 top-K replays overwrote the main account's exchange-lot / gross-contract diagnostics | Accepted (my bug) | Snapshot after the main run, passed explicitly to `account_summary` |
| F494-04 neighbour robustness used the simple target under `--return-basis cagr` | Accepted | `_robust` gets the converted target |
| F494-05 full year ending on a Friday annualised over 363 days | Accepted | `horizon_end()`: the intended end when the data reaches within 7 days of it, else the day after the last session; used by year slices and the account |
| Performance: repeated scans of marks at each admission | Done | Carried marks precomputed per trade (O(1) per decision) |
| F494-06 margin / execution / intraday stress assumptions; F494-07 strike / tenor study | Open / deferred | Unchanged; the study waits for `zone_coverage.py --tenor-check` on the real options file |

# V4.9.4 (ChatGPT's V4.9.3 review)

| Finding | Decision | Change |
|---|---|---|
| F493-01 a loss realised earlier the same day did not reduce funds at the next admission | Accepted, fixed | Funds at each decision = capital + P&L realised so far + open positions marked at the decision bar's close (last finite mark, carry accrued). Also fixed an off-by-one found while doing so: an exit's last fill is in the bar after its exit bar, so margin, lots and the slot are now held through that bar |
| F493-02 "10 lots" = reference lots; a full 9-lot position at lot 75 counted 10.385 and was cut to 8 | Accepted, fixed | Reference lots now = share of the path's full target size x 10 (a full position is 10 whatever the rounding). `--lot-cap-unit exchange` caps actual exchange lots instead. Peak exchange lots and gross contracts (calendar = 2 per package) reported either way. Which is binding: your decision |
| F493-03 `--return-basis cagr` did not reach selection | Accepted, fixed | The training target is converted exactly: CAGR >= t  <=>  total / capital >= (1 + t)^years - 1 (fixed size, profits kept); rows report `overall_cagr` |
| F493-04 overall OOS return = mean of yearly figures (an annualised half year weighted as a full year) | Accepted, fixed (my error) | `overall_simple` = account P&L over the elapsed period / capital / elapsed years (calendar days from the period start to the day after the last session); `mean_of_yearly_returns` kept as description only. Holdout of the synthetic run: V4.9.3 printed 5.4% (mean of -21.1% and +32.0%); the real figure is -3.5% a year |
| F493-05 holdout overrode INSUFFICIENT EVIDENCE to PASS on one full year | Accepted, fixed (my error) | One `verdict()` for walk-forward and holdout; `--min-full-years` (walk-forward, default 2) and `--holdout-min-full-years` (default 1, frozen: Jan 2025 - Jun 2026 has one full year) |
| F493-06 holdout runner outside the code digest | Accepted, fixed | Digest adds zone_holdout.py, contract_specs_v2.py, zone_signals.py, calendar_research.py (costs) and the numpy version |
| F493-08 holdout horizon not frozen; coverage unchecked | Accepted, fixed | `--holdout-end` frozen in portfolio_freeze.json (default 2026-07-01 exclusive = through June; 2026-08-01 to include July); `zone_holdout.py --to` may only confirm it; refuses when sessions start or stop more than 7 days from the horizon or have a gap over 7 days |
| F493-07 contract planner lacks `--target-units` | Rejected as stated | The planner simulates every contract liquid at full participation, so position size does not change which contracts it plans; adding it to the plan would force a needless re-extraction. The real exposure is an exit spilling beyond `--buffer-sessions`, which `zone_coverage.py` reports as `not_planned` after the rebuild |
| F493-09 buffer policy | Decision | Unchanged: reported, not acted on |
| Five strikes / H1 / H2 | Still deferred | Gated on `zone_coverage.py --tenor-check` on the real options file |

# V4.9.3 (ChatGPT's V4.9.2 review and integrated research plan: accepted items only)

| Finding | Decision | Change |
|---|---|---|
| V492-01 green 10 + red 10 lots open at once | Accepted (the 25 L objective was derived as 10 lots x 2.5 L) | `--max-open-lots 10`: combined open reference lots (units / 65) across both sides, intraday included, in training (gate) and in the account (orders cut / rejected). A calendar package counts once |
| V492-02 650-unit sizing not executable | Accepted, and worse than stated: paths were simulated at 10 exchange lots, so 650 units claimed 13 (lot 50) or 26 (lot 25) lots that were never simulated, through all of 2021-24 | Root cause in the path engine: `zone_paths.py --target-units 650` sizes each entry at round(650 / lot) exchange lots (13 / 26 / 9 / 10), filled minute by minute as before. Portfolio `--sizing exchange` (default): whole exchange lots, min(matched, floor(k x target lots / 10)); `normalized` kept as a research proxy, never deployable |
| V492-03 same-day trades held zero margin | Accepted (material: `tod` exit rules make every trade intraday, so the margin gate did nothing for them) | A position holds margin and lots from its entry session to its exit session inclusive; within one strategy the session value is the largest of its sequential trades. The account tracks margin and lots per bar and reports each day's intraday peak |
| V492-04 admission used the entry day's eventual close | Accepted (small in practice, but a causality breach) | Admission at the signal bar's close: positions open at the fill bar valued at that index, funds at the previous session close |
| V492-05/15 buffer use after admission not reported | Accepted (reporting) | `buffer_breach_sessions` per year and in total, apart from `funds_short_sessions` (a margin call: year not deployable). No forced de-risking modelled: a policy decision |
| V492-06 simple yearly yield vs equity CAGR | Accepted (reporting) | `cagr_fixed_size_no_withdrawals` beside the simple mean; `--return-basis simple|cagr` for the verdict |
| V492-07 no protected holdout runner | Accepted | `portfolio_freeze.json` (in-sample choice, sizes, assumptions, data / grid / code hashes) and `zone_holdout.py`: replays it once on 2025-01 .. 2026-07 in the same executable account, refuses changed data or code, ledger refuses a second look unless `--acknowledge-repeat` (then labelled). `run_grid` refuses a split inside the holdout at the API too |
| V492-08 no out-of-sample verdict | Accepted | `oos_verdict` PASS / FAIL / INSUFFICIENT EVIDENCE on the stitched account (overall, worst full year, continuous drawdown, funds, deployability) with reasons |
| V492-10 annual consistency | Accepted as an option | `--rank dispersion` (std of judged yearly returns, then drawdown); `return_dispersion` reported. Default stays `dd` (the stated objective) |
| V492-13 full leap year x 365/366 | Accepted (I had rationalised it in a test) | Full calendar years factor 1; part years 365 / days |
| V492-14 dedup by daily arrays and Python `hash` | Accepted | SHA-256 over every trade (signal bar, dates, units, lot, value) plus the daily equity / margin / lots |
| V492-09 portfolio-aware pools | Not coded | No defined criterion offered; pool-width sensitivity is a rerun (`--pool 150/300/600`) |
| V492-11/12/16 SPAN, fill lag, stale-mark valuation, bootstrap / SPA | Not coded now | Need broker snapshots or a defined method; listed as open |
| Time-blocked `combo_eval` (2.39x) | Rejected | Measured on a 0.1 s workload; not a bottleneck |
| Research plan: five OTM strikes, monthly tenors H1 / H2 | Deferred, coverage check added | `zone_coverage.py --tenor-check` counts, per year and on signal entry days, how often H1 (current-month monthly >= 21 days) and H2 (next-month monthly) are listed. Run it before any build |

# V4.9.2 (corrections after ChatGPT's review of V4.9.1)

| Finding | Decision | Change |
|---|---|---|
| V491-01 carried positions plus the next year's choice could exceed capital (Rs 46.8 L on Rs 25 L reproduced); the account only counted it | Fixed | Executable account: every entry of every year's legs, in time order, is admitted against (1 - `--margin-buffer`) x (capital + P&L at the previous close) minus margin in use; cut to whole lots or rejected (`portfolio_orders.csv`). A year with funds short of margin at any close is not deployable |
| V491-02 margin gate used initial capital, not funds after losses | Fixed | Training gate: margin(t) <= (1 - buffer) x (capital + P&L(t-1)) at every session (`margin_excess_over_available`); `--margin-buffer` default 10% |
| V491-03 `--max-evals` kept the highest totals before the drawdown check: false "none passed" (reproduced) | Fixed | Truncation removed. Every target-and-floor survivor is evaluated exactly in chunks; `evaluated_exactly` = `pass_target_and_floor` in the gate counts |
| V491-04 smaller sizes scaled a partly matched path linearly (1 lot of a 4-of-10 path = 0.4 lot) | Fixed | A leg of k lots holds min(k, lots matched on the path) whole lots per trade; curves built per size from per-unit trade pieces (`prepare` / `combine`). Per-unit P&L from the 10-lot path (conservative for smaller orders); a roll reopen that failed at 10 lots still ends the path |
| V491-05 numba kernel accepted infinite values (`v==v`) | Fixed | `math.isfinite`; parity test with +/-inf, NaN, 1e150, with and without gaps |
| V491-06 a test year without candidates vanished from the report | Fixed | Every planned test year is reported (`no_candidates`, `no_training_sessions`, `no_pair_passed`); `planned_years` vs `reported_years` in the summary |
| V491-07 "cash year" while carried positions were open | Fixed | Decisions: `new entries: ...` / `no new entries: ...`; `flat_cash_year` only if the account held nothing that year; `no_new_entry_years` and `flat_cash_years` reported apart |
| V491-08 `--leg-lots` above the simulated size accepted | Fixed | Rejected (rebuild paths with a larger `--lots`) |
| V491-09 data gaps free the position slot | Fixed, policy choice | `zone_grid.py --gap-policy busy` (default): a signal that did not fill because data is missing (`not_extracted`, `no_long_expiry`, `no_short_expiry`, `lot_unknown`) holds the slot until the zone changes (upper bound of every structure's natural end), outcome unknown and never counted; `free` = V4.9.1. Recorded in the grid lineage; `Detail` and the account follow the grid's policy. Thin markets (`no_liquid_entry`) still free the slot |
| V491-10 names overstate the checks | Renamed | `assumed_margin_*`, `daily_close_drawdown*` |
| V491-11 pure-noise winner | Kept as a standing warning | Holdout 2025-01 to 2026-07 untouched; run once after freezing |
| V491-12 search materialisation | Done | Exact chunked evaluation (10,000 rows); curve building per size from shared pieces (portfolio dry run 48 s -> 16 s) |

# V4.9.1 (corrections after two reviews of V4.9.0: Claude, ChatGPT)

| Finding (source) | Decision | Change |
|---|---|---|
| Futures carry never charged: `equity()` tested `structure=='futures'`, production labels are `futures_zone` / `futures_expiry`; the unit test used the test-only label (ChatGPT C01) | Reproduced, fixed | `structure.startswith('futures')`; tests on both production labels, long and short, carry 0 and 5.5%, options untouched |
| Portfolio did not run the grid integrity check (both, C06/A3) | Fixed | `zg.verify(grid, paths)` before anything; test with one modified npz byte |
| Rupee gates mean a different hurdle each year because lot size moved 50 / 25 / 75 / 65 (Claude A1) | Fixed | `--sizing constant` (default): each trade at `--ref-units` (650) x its filled share of target size; `--sizing actual` keeps V4.9.0 |
| No margin or capital model; both legs could need margin at once (ChatGPT C02, Claude A5) | Fixed (assumed rates) | Per-session margin = rate x units x index close per open position (`--margin`, defaults futures/naked 12%, calendar 4%); gate peak combined margin <= capital; report margin timeline |
| At fixed 10 lots the gates reduce to ratios and ranking by drawdown is arbitrary; calendars ruled out by margin; no green-only / red-only (Claude A2/A5, ChatGPT A9) | Fixed | Sizes searched per leg (`--leg-lots`, default 0..10; 0 = leg absent). Target and floor screened for every combination (linear in size), drawdown and margin for the survivors |
| Walk-forward was slices of full-history curves: a pair chosen for year Y was credited with positions it opened before Y; drawdown reset each year (ChatGPT C03/C12, Claude A8) | Fixed | One chronological account: choice adopted flat on 1 January (`Detail.trades(flat=True)`), entries that year only, open positions run to their own exits; yearly returns, continuous drawdown, peak margin from the account; `portfolio_account_daily.csv` |
| Closest failing pair traded and counted in the walk-forward mean (both, C09/A7) | Fixed | `--if-none cash` (default): no passing pair, cash year; `closest` is research only and never `deployable` |
| No year with >= 90 days fell back to judging all years (ChatGPT C08) | Fixed | Worst year NaN, floor fails, counted `no_judged_year` |
| Winner's curse: only the single best pair evaluated out of sample (Claude A7) | Fixed | Test results of the top `--wf-top` (10) distinct passing pairs per year: median, worst, share meeting the floor |
| Different rule labels with identical trades inflate candidate counts and the neighbour robustness share (Claude A9). Synthetic dry run: 335 pool candidates were 108 distinct curves; the in-sample winner's 12.5% neighbour pass share came entirely from identical neighbours, 0% from distinct ones. Layered `c:none|none|<exit>` always equals the plain `<exit>` rule | Fixed in the portfolio | One candidate per distinct equity + margin history (`*_identical_rules` reported); `neighbour_identical_share`, `neighbour_pass_share_distinct`. The grid itself is unchanged (dedup there needs exit rows) |
| Walk-forward training windows used min trades 15, the grid uses max(10, 15 // 2) (Claude A7) | Fixed | `--wf-min-trades`, default as the grid |
| Carry is an assumption (ChatGPT A6) | Added | Account repeated at `--carry-scenarios` 0, 3, 5.5, 8% |
| Stale marks (both, C05/A6) | Reported | `*_stale_share`: share of bar closes in each leg's trades without a fresh print |
| Candidates silently dropped for unvalued trades (Claude A4) | Reported | `candidates_dropped_for_unvalued_trades` |
| CLI validation (ChatGPT C17) | Added | Positive capital / drawdown / target, floor <= target, counts >= 1, `--min-year-days` 1..366, margin rates 0..1 |
| Grid: pre-window signals hold the slot in `test_Y` windows (ChatGPT C04) | Kept, documented | The grid scores a continuous strategy (its windows attribute entries); the portfolio account uses flat adoption. Changing the grid would change every stored number |
| Last partial year missing from test windows (ChatGPT C07) | Not applicable | `zone_grid.py` refuses `--split` after 2025-01-01, so 2025-26 stays an untouched holdout |
| Data gaps free the position slot (Claude A4, ChatGPT C11) | Open | Needs a path/grid change (gap = slot busy, outcome unknown); deferred to a decision |
| Speed (both) | Done | `sequence_all` numba kernel, one thread per configuration (optional dependency; identical results, tested; synthetic 1,445 x 2,750: 20.8 s -> 4.6 s on 2 cores). Unused per-rule P&L / exit-day copies no longer built in `_grid_group`. `Detail` keeps two groups. `GRID_ALGORITHM` 4.9.1 (rebuild the grid) |

# V4.9.0 (portfolio objective)

| Request | Change |
|---|---|
| Judge strategies as the trader experiences them: green + red together, rupee returns on 25 lakh (10 lots x 2.5 lakh), >= 22%/yr overall, >= 17% every year, drawdown <= 2.5 lakh, minimise drawdown | New `zone_portfolio.py`: candidate pools per side from the grid (top by training total, worst training year, stability), each replayed once as a daily rupee equity curve (closed trades + open trades at each session's last close, each trade's own units; futures carry assumed); every green x red pair evaluated vectorised on combined equity: yearly returns, overall return, worst year, max drawdown; gates and ranking (`--rank dd`/`worst_year`/`calmar`) |
| Hurdles passed in-sample by one of ~10^9 pairs prove nothing | Walk-forward: each test year's pair chosen on earlier years only (pools, gates and ranking on training data); in-sample list labelled; neighbour-swap robustness share for each pick |
| Part years | Annualised over calendar days covered (data end included); shorter than `--min-year-days` (90): not annualised, not held to the floor |
| `Detail.trades` | Also returns each trade's units and mark dates |

# V4.8.5 (from the V4.8.4 real-data paths run)

| Finding | Change |
|---|---|
| V4.8.4 run: 3,154 calendar entries sold the short with no long fill and 2,990 with only part of the long; 6,197 partial entries were then unwound. Legs filled independently, so the engine held naked shorts no calendar trader would hold, and their unwind cost entered the calendar results | All legs fill together minute by minute in equal whole lots (each minute trades the smallest of the legs' tradable quantities; no look-ahead). A first entry takes the matched quantity, one lot up to `--lots` (P&L is per unit). A roll reopen must fill the path's whole size, else the path ends. No entry is ever partial. Counters: `no_liquid_entry_limited_by_long/short`, `entry_reduced`, `entry_reduced_to_<n>pct`, `lot_changed` |
| Review: 4,858 `not_extracted` and 13,140 `no_long_expiry` need explaining before the grid. (The rise from 175 to 4,858 is the V4.8.4 reclassification of the same entries, not lost coverage: V4.7.2 had 4,858 on the same file) | `zone_paths.py` records every gap in a `gaps` table (event, leg, day, side, put/call, strike, ATM, offset, expiry, days to expiry, roll). New `zone_coverage.py` classifies each against the options file (expiry not listed / strike not planned / planned but empty in source) and summarises by year, month, put/call, leg, days to expiry, moneyness, provider, roll; plus listed far expiries per year and provider |
| User check on the options file: only 626 of 1,260 days list any expiry > 30 days out (2021: 127 days without, 2022: 228, 2023: 207, 2024: 68, 2025: 0). | Reported. `no_long_expiry` is a source-listing limit for 2021-24, not an extraction fault; no code change |

# V4.8.4 (from the first V4.8.3 real-data paths run)

| Finding | Change |
|---|---|
| Real run: 10,958 calendar entries filled the short but not (or not all of) the long, while `reject_not_extracted` fell from 4,858 (V4.7.2) to 175. A long contract absent from the extraction was being traded as an illiquid fill: the short was sold and bought back at a cost, a loss invented from a data gap | An entry with any leg absent from the extraction that day is rejected as `not_extracted` (counted `not_extracted_one_leg_long/short`); only an extracted but too-thin contract is a partial fill. Tests for both |

# V4.8.3 (corrections after the review of V4.8.2)

| Finding | Decision | Change |
|---|---|---|
| V482-01 window end between two fill days used the earlier fill day's prices | Reproduced, fixed | The exit's state is stored at the close of every trading session from its first possible fill day to completion (8 sessions), fills or not: cash received so far and the open legs at that session's close. The review's case now gives 3,500 / 6,500 for a 31 Dec close of 60 / 120 |
| V482-02 weekend inferred from gaps in the data | Fixed | Exchange calendar: the last session of the week is the one after which every weekday to Saturday is a holiday. `zone_grid.py --holidays FILE` (any common date format; stored in the grid index and the algorithm digest). Without it only Fridays qualify; data gaps never do |
| V482-03 slot limit counted fill days, not sessions | Fixed | Rows store `exit_sessions` and `exit_fill_days`; counts `exit_simulations_over_several_sessions`, `exit_simulations_with_several_fill_days`, `exit_simulations_beyond_session_slots`, `exit_session_slot_unpriced` |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, 247,095,000 values, identical |

# V4.8.2 (corrections after the review of V4.8.1)

| Finding | Decision | Change |
|---|---|---|
| 481-01 exit proceeds spread over fill days by quantity | Reproduced (my V4.7.1 design), fixed | Every fill window is booked on its own with its own costs as a dated event; settlements and forced bookings too. The review's case now records Rs 500 on 29 Dec and Rs 5,000 on 2 Jan |
| 481-02 window end valued from the first exit day only | Reproduced, fixed | Each row stores the exit's state at the close of each of its first 6 days (actual cash received, legs still open at that close); a window end uses the last exit day before it. Exits longer than 6 days give NaN (unpriceable) for window ends past the 6th day and are counted |
| 481-03 missing residual price became zero | Fixed | NaN (unpriceable), counted as `exit_day_slot_unpriced` |
| 481-04 digest missed indicator code | Reproduced, fixed | `algorithm_digest` also hashes zone_signals atr/rma/ema/Bar/minute_of and the numpy version; robust to functions without source |
| 481-05 "25th percentile" | Kept by design, renamed | Conservative lower quartile: the floor((n-1)/4)-th smallest value (the minimum when n <= 4). Documented and tested |
| 481-10 weekend rule used the next observed date | Fixed | Calendar-based: the last session before a Saturday; a missing feed day is not a weekend; the data's last session counts only if it is a Friday |
| Diagnostics | Added | `exit_simulations_over_several_days`, `exit_simulations_beyond_day_slots`, `exit_day_slot_unpriced` in the paths counts |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, every window, identical (no exit in the synthetic set straddles a year end; those cases have their own tests, one against the old event engine) |

# V4.8.1 (corrections after the review of V4.8.0)

| Finding | Decision | Change |
|---|---|---|
| V480-01 partially executed quantities dropped | Reproduced, fixed | Every executed quantity is recorded per leg (complete or not) and the position is unwound with costs. Synthetic run: 92 of 98 partial calendar entries had a partly filled long that V4.8.0 discarded. Tests for 0/0, 50/50, 100/50, 50/100, 100/100 |
| V480-02 re-entry minute reused across days | Guarded | The engine always reopens on the bar whose minutes held the last exit fill, so the windows share a date; the start is now carried as (date, minute) and ignored on another date. Test |
| V480-03 resume/report bound only to the rule module | Fixed | `algorithm_digest` hashes the rule module and the source of the path reader/layout, sequencing, group computation and writer, plus `GRID_ALGORITHM`; `--resume` and report reading refuse a mismatch (report code may change freely). Test |
| V480-04 stability excluded edge settings | Fixed | Needs >= 1 valid neighbour and >= half of the existing ones; coverage reported; float64; `baselines.csv` keeps every zone-only none/none result. The review's example now scores tw2 900, tw6 600 |
| V480-05 futures vs buy-and-hold carry | Fixed | Both gross (spot proxy); net figures apply the same assumed carry to both (picks only while held, long pays, short receives). Note text corrected |
| V480-06 window end inside a stop's exit | Reproduced, fixed | Rows store the cash an exit decided there receives by the close of its first fill day and the open legs' value then; a window end after that day uses it. Matches the old event engine's boundary mark (test) where the natural path's mark differs |
| V480-07 layered exit policies | Added | 659-879 layered policies per structure (initial protection x management x mandatory exit), vectorised as the earliest of their parts; family `compound` (and `any_incl_compound`) |
| V480-08 execution proxy | Stress option | `zone_paths.py --fill-price adverse|open` |
| V480-09 unequal minimums | Made explicit | `--zone-min-trades`, `min_trades` column |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, 247,095,000 values, identical |

# V4.8.0 (V4.7.2 review, comprehensive stop families, stability ranking, further vectorisation)

| Item | Change |
|---|---|
| Review P0: zone change during a pending calendar roll exit | Reproduced. The path now stays open until the old pair's last fill, opens no new pair, and ends on that bar (V4.7.2 ended it on the flip bar while its exit completed a bar later; the lock was already correct). `end_fill_day` column. Test with the flip before completion, which V4.7.2 fails |
| Review: `--resume` trusted altered files | Each group file re-hashed against its sidecar; mismatch = recompute. Test |
| Review: `--report-only` could mix inputs | Report refuses if any file hash, the rule definitions or the paths file differ from the grid's lineage. Test |
| Review: partial calendar entries treated as never traded | The filled leg is now a position, sold back at the next bar close (`partial_entry_unwound`, counts `partial_entry_short_filled` / `long_filled`), with its cost and exposure |
| Roll re-entry ordering | A new pair fills only after the old pair's last exit minute (no shared minute volume). Old-engine parity kept behind `roll_entry_after_exit=False` in tests |
| Vendor ties | Deterministic (`ORDER BY provider`) in both engines |
| Stop families | 368 stop rules (naked), from 61: see README 4c and `zone_rules.py`. Profit rules 24 -> 50. Every V4.7 label kept |
| Bug found while adding them | Channel-break stop with zero cushion never fired while ATR was not ready (0 x inf); fixed and tested |
| Stability ranking | `stable` mode: lower quartile of the neighbourhood's training totals; walk-forwarded like `total`; `stable_candidates.csv`; strike +-100 totals for every pick |
| Vectorisation | Rules evaluated on padded paths x bars matrices for a whole group at once (sub-batched to bound memory); profit rules once per group; stops one family at a time; group-parallel grid and file-parallel report (`--workers`); fast zlib level 1 output; neighbourhood score via flat-index gathers. Profiled: compute 109 s -> 45 s and report 254 s -> ~110 s on the synthetic run (2 workers) |
| Parity | On the same paths file, every value of every rule shared with V4.7.3 (14,250 rule pairs, 247 million values, 12 groups) is identical |

# V4.7.3

| Change | Detail |
|---|---|
| `--min-trades` default 30 -> 15 (`zone_grid.py`, `zone_report.py`), at your request | Training pick needs 15 trades; walk-forward fold picks need max(10, 15//2) = 10; zone-only keeps 5 |

# V4.7.2 (fixes from the first real-data run)

| Issue | Change |
|---|---|
| `zone_paths.py` crashed on futures entries in June-July 2021: the lot-size rule returns no size during the 2021 transition (`transition_ambiguous`), and the futures path lacked the guard that option paths and `zone_futures.py` have | Rejected as `no_fill` / `lot_unknown`, counted as `reject_futures_lot_unknown`. Regression test; synthetic dry run from May 2021 completes |
| Unclosed files and connections (ResourceWarning in tests) | JSON written via temporary file + rename and read with context managers; `.npz` closed after loading; grid and detail close their SQLite connections; `zone_backtest.Market` gains `close()` / context manager and closes when collected |

# V4.7.1 (corrections after the review of V4.7.0)

| Finding | Decision | Change |
|---|---|---|
| P01 spilled calendar roll: rows between pairs held cash from fills that happened later | Reproduced, fixed | The exit simulation returns a fill schedule (cash known at each bar close, legs still open). Rows between pairs hold cash already received plus open legs at their last close; equals the old engine's boundary mark (test). Effect was on boundary values and mark-to-market only: stops used the final outcome, take-profit was off between pairs |
| (found while fixing P01) rows between pairs gave exit bar = that bar, so a stop there released the one-position lock before the roll exit finished | Fixed | Exit bar = when the roll exit completes |
| P02 futures exit with no next-bar print booked at the bar close without costs | Reproduced, fixed | Exit waits for the first later print; none before data end = open, valued at its mark |
| P03 float32 sums | Fixed | Totals and sums of squares stored float64; int16 counts checked for overflow |
| P04 trailing stop anchored at the signal close | Fixed | `trail` starts at the index at the entry fill (new `entry_index` column); `strail` keeps the V4.7.0 definition by name. Stops 46 -> 61 (futures 40 -> 55) |
| P06 one calendar leg filled, the other not | Kept (rule), measured | Counted as `entry_one_leg_filled_other_not`; the filled leg is still treated as never traded (optimistic, stated) |
| P07 per-trade evidence | Added | `zone_grid.py --explain` writes any combination's trades from the paths |
| P12 durability | Fixed | Paths built into `*.building` and renamed when complete; grid files written atomically with per-group checksums and `--resume` |
| P13 trade-sequence drawdown only | Added | `max_drawdown_mtm` on bar-close equity (closed trades plus the open trade's mark), worst open loss, median MAE/MFE |
| P14 profit and stop on the same bar | Labelled | Exit reason `both_rules_same_bar` (cash is the same either way) |
| P05, P08-P11, P15 | Agreed limitations, already documented | 45-minute close triggers, spot-proxy futures, no margin model, best-of-grid bias, vendor by volume |
| Tests | 336 pass | Regression tests for each reproduction, temporal prefix invariance (option prices and volumes changed from a cut-off day: every as-of field before it unchanged), old-engine boundary parity during a spilled roll |

# V4.7.0 (minute-sliced fills, path engine, exhaustive exit grid)

| Change | Detail |
|---|---|
| **Bug fix** forced long exit sign (`zone_backtest.py`) | V4.6.0-4.6.4 booked a calendar long force-closed at the lag cap as `-units x price` (a purchase) instead of `+units x price`. Every forced chain was understated by twice the long's value. In the supplied V4.6 trades file: 332 forced exits (320 bear call calendars, 12 bull put calendars), median 2,317 points per unit each. Bear calendar results in V4.6.x are biased down; naked options and futures unaffected |
| `zone_paths.py` (new) | Each (signal bar, side, structure, strike) simulated once to its natural end, bar by bar, all live paths stepped together. Stores per bar: cash, pair cash, base, printed value, mark, exit cash if decided there, exit bar/day, forced flags, roll flag |
| Minute-sliced fills | Each leg takes at most 10% of every minute's volume in whole lots at (O+H+L+C)/4 over the next bar's minutes, slippage and charges on the leg total. Entry must complete in its bar; exits carry over (shorts before longs). The old engine needed the whole order in one minute: e.g. 3 lots at 10% needed 30 lots in a single minute |
| Spilled roll exits | A roll whose long takes more than one bar completes first; the new pair opens on the completion bar if the zone holds (as `zone_backtest.py`) |
| Data end vs missing settlement | An exit that runs out of data leaves the path open (valued at its last mark); only a missing settlement price is `unresolved` |
| `zone_grid.py` (new) | Every signal configuration x side x structure x strike x 24 profit rules x 46 stop rules (futures 13 x 40), one position at a time, about 60 million combinations. Every combination's per-window results stored in `.npz`. Families by stop class (none, atr, trail, pct, loss, any) and configuration set (all, swing, zone-only); walk-forward by year; exact detail (weekly t, PF, drawdown, holding, exit reasons, by year) for every pick; buy-and-hold benchmark |
| Vectorisation (profiled, then changed) | Option minutes loaded per session straight from SQL into numpy (vendor de-duplication by sort); each leg's minute slicing is `cumsum` + `searchsorted`; exits memoised per (legs, bar); all rules of a kind evaluated as one broadcast; every signal configuration sequenced at once per signal bar (Python loop over ~1,800 bars instead of ~137,000 configuration-signal pairs); take-profit choice computed once per group and window. Outputs checked identical to the loop versions (paths to 1e-10 rupees; grid `.npz` and report equal) |
| `zone_contracts.py --strike-pad` | Extra calendar strikes around the planned ones, for spilled rolls that reopen at a neighbouring strike |
| Tests | `tests/test_paths.py` (20): path engine reproduces `zone_backtest.py` exactly for expiry, zone change, calendar roll chains, spilled rolls, short-before-long, lag cap and boundary marks; offline take-profit and ATR stop equal the old engine's; vectorised take-profit rule equals `zone_report.choose_tp` on 200 random cases; futures path equals `zone_futures.py` |

# V4.6.4 (corrections after the ChatGPT review of V4.6.3)

| Finding | Decision | Change |
|---|---|---|
| N463-01 position closed by a decision on the last bar of a year, filled next session, had no boundary value and was dropped from training | Reproduced, fixed | Both engines value open positions at the boundary before processing that bar's decisions. Supplied futures file: 12 rows (2 signals) were affected |
| N463-02 pre-entry phantom mark | Reproduced, fixed | Marks only for positions entered before the boundary |
| N463-03 incomplete 45-minute bars | Kept (your decision) | `Bar.minutes` recorded; `zone_signals.py` prints how many bars are short |
| N463-04 summary period P&L by entry date, risk by exit date | Reproduced, fixed | Calendar periods: P&L earned on the daily path, plus a separate cohort column. Futures marked to each session close; options exit-booked and labelled. On the supplied futures file the green trend rule's max drawdown is Rs 8.1 lakh at 10 lots, not Rs 4.8 lakh |
| N463-05 zone-only control hidden by the 30-trade minimum | Accepted, fixed | Controls reported with no minimum, plus their own walk-forward over the same folds |
| N463-06 same-minute volume used for the fill | Kept (your decision) | No change |
| N463-07 expiry listing from the signal day | Kept, documented | The order is decided at the signal close |
| N463-11 excluded chains | Accepted | `excluded_no_value` per candidate in `train_candidates.csv` |
| N463-13 stale README sentence | Fixed | |

# V4.6.3 (ATR stops, futures benchmark, take-profit fix)

| Change | Detail |
|---|---|
| **Bug fix** `zone_report.choose_tp` | V4.6.1-4.6.2 returned `None` both for "no level has enough trades" and for "no take-profit is best", and skipped both, so configurations whose best level was no take-profit were never ranked. Now a separate sentinel. On the V4.6.1 run: candidates 9,922 -> 11,243; top rules unchanged; bear walk-forward +1,835 (t 1.93) -> +278 (t 0.26) |
| `--stops`, `--atr-mult` | Index stop fixed at the signal bar: close -/+ mult x ATR(n) on 45-minute bars, checked on bar close; rolled calendar pairs keep the chain's level. Ranked as families `naked|atrN` |
| `zone_futures.py` | Long/short index proxy on the same signals and exits; `futures_zone` and `futures_expiry` (same horizon as the naked short); dated futures charges and slippage points; same trade format |
| `--keep-fills` | Fills are no longer stored by default (file size) |
| `zone_summary.py --structure all` | Top N per side for every family |

# V4.6.2 (wider strike and take-profit grid)

| Added | What it does |
|---|---|
| `--offsets` on `zone_contracts.py` and `zone_backtest.py` | Strike offsets from ATM; the backtest refuses offsets the extraction did not plan |
| `--tps` on `zone_backtest.py` | Take-profit levels, e.g. `none,.25,.3,.35,.4,.45,.5,.6,.75` |
| `zone_summary.py` | Top-N rules per side from `train_candidates.csv`, identical rules merged, rupees at `--lots` per 12-month period: P&L, win, PF, Sharpe, Sortino, Calmar, drawdown, max loss and win, exits. Warns when `--to-date` passes the split |

# V4.6.1 (corrections to the zone study after review)

| Finding | Decision | Change |
|---|---|---|
| Fill accepted using the same minute's volume | Accepted as a choice (yours) | Kept, and the search now covers every minute of the next bar (09:15-09:59 next session after the last bar). Recorded in trade-file meta |
| Four-day rule broken when the fill is on a later day | Fixed | Days are counted from the fill day; if the nearest expiry falls short, the next one is traded |
| Open positions dropped at the training cutoff | Fixed | Positions are valued at each 1 January and at `--split` (cash plus latest bar-close prices); the report closes them hypothetically and counts them |
| Take-profit chosen on too few trades; losers frozen | Fixed | Each cut-off needs `--min-trades`; only positive training totals are frozen; the report says when nothing qualifies |
| Vendor chosen by order | Fixed | Larger-volume vendor per contract-minute |
| Calendar exits waited for both legs in one minute | Fixed | Short bought back at its first liquid minute, long at its first liquid minute after |
| Weekly pair vs campaign win rate | Added | `pair_win` beside campaign `win`; also `max_drawdown`, `hypothetical`, `forced` |
| Incomplete spot bars; Pine assumptions | Kept by your decision | No change |
| Data needed for minute-by-minute fills | New `zone_contracts.py` | Targeted extraction replaces the 45-minute cache (a full all-minute chain would be about 125 GB). Tested against a local PostgreSQL 16 with synthetic data, which caught two bugs (NUMERIC as Decimal; temp table across months) |

# V4.6.0 (zone + swing study)

Calendar and fly code unchanged; trades from V4.5.4 stay valid (engine tag 4.5.4).

| Added | What it does |
|---|---|
| `INDEX_v15_zone_swing.pine` | v14 with the 3-bar persistence filter working (v14 declared the checks with `var`, so they were computed once), optional volume weighting off by default, MACD removed, swing-filter marks |
| `zone_spot.py` | Every spot minute 09:15-15:29 from `market_data.spot_bars` into SQLite |
| `zone_signals.py` | 45-minute session-aligned bars; 90/180/360-minute values only after their bar closes; zones; pivots; rising valleys and falling peaks; the 1,440-point grid plus zone-only controls; step 0 |
| `zone_backtest.py` | Naked short and 60-day calendar on each signal, five strikes, six take-profit levels; exit on zone change; calendar rolled at 15:15 on short expiry; both vendors per contract; long-exit lag cap of one session |
| `zone_report.py` | One position at a time per configuration; take-profit rule (best win rate within 75% of best total); ranking; yearly walk-forward inside training; freeze; single holdout evaluation with an inspection log |

# V4.5.4 (first real-data pilot)

Rerun simulations: the engine tag changed. Caches stay valid. With `--max-exit-lag -1` (default) trades match V4.5.3.

| Finding on real data | Change |
|---|---|
| After the short was bought back, a thin far long waited 1-7 days for a print, held naked. At a 5% cap this produced 749 of the points that separated it from 10%, on 28 matched trades | `--max-exit-lag`, `--forced-exit-haircut`. The forced exit is a model mark, tagged `model_mark`, `executable_claim` false, and not counted in `executed_fills` |
| Carry and naked-long drift were mixed | Each trade records `scheduled_exit_mark_inr`, `post_schedule_drift_inr`, `exit_lag_sessions`, `forced_exit`. `exit_lag_report.py` gives a train-only view with t-statistics clustered by short expiry |
| Expiry eras were split by entry date in the hand analysis | The report splits by short expiry: entries in late August 2025 can hold a 2025-09-02 Tuesday contract |
| A short whose expiry session is missing from the cache was held at a placeholder upper bound (-Rs 3.26 lakh on one lot) | No code change. End entries before the gap (`--to-date`), or supply `--settlement-master` |

# V4.5.3 (response to the ChatGPT and Gemini reviews of V4.5.2)

Rerun simulations: the engine tag changed, because iron-fly signals changed. Calendar trades are identical to V4.5.2. Caches stay valid. `term_curvature_5_30_90` is renamed; update any saved `--signals` list.

## ChatGPT
| Finding | Verdict | What changed |
|---|---|---|
| BOUNDARY-01 later P&L in training `net` | Reproduced, fixed | A trade open at the cutoff enters ranking with its last mark before the cutoff, status `open_at_cutoff`, and no later identity. `GroupMatrix.evaluate` `net` stops at its last session. `train_boundary` reports counts and as-of marks. Sharpe was never affected: daily equity was already clipped. On one synthetic ranking, 36 trades in the training sample were affected. |
| HOLDOUT-02 sealed report saw later rows | Accepted, fixed | `train_only` drops every row entered after the cutoff. BOUNDARY05 test: changing all later outcomes leaves `rankings.json` byte-identical apart from `lineage`. |
| HOLDOUT-01 log followed the directory | Reproduced, fixed | One per-user log keyed by trade content (`~/.nifty_research/holdout_inspections.jsonl`, or `NIFTY_HOLDOUT_LOG`). A corrupt log stops the run. An attempt is recorded before any holdout number exists. This is inspection tracking, not cryptographic sealing. |
| SIGNAL-01 curvature | Reproduced, fixed | `annual_variance_curvature_5_30_90` (old definition) and `total_variance_curvature_5_30_90`, both predeclared under `--signals all`. |
| PORTFOLIO-01 ties broken by id | Real in principle, no case in current flows | Groups hold one trade per day: `same_day_competing_groups` is now reported, 0 on the synthetic run. Identical entry-known fields cannot be ordered without some tie-break. Needed before any mixed-group book. |
| OPS-01 atomic reports | Accepted, fixed | The report set is written to a temporary directory, validated and renamed. |
| DIAGONAL-01 | Accepted, fixed | `structure_iv_basis` is `cross_strike_term_skew_ratio` when strikes differ. |
| Carry across the split (BOUNDARY-01 item 5) | Declared | `holdout_policy` states a fresh account at the split. |
| EXEC-01, CAPITAL-01, REGIME-01, STATS-01, COST-01, FREEZE-01, PERF-01 | Agreed, open | Need real data, broker notes or SPAN. A byte-level cache check is not added. |

## Gemini
| Claim | Verdict | What changed |
|---|---|---|
| 1.1 Quantile bounds leak across the embargo | Not a leak | Outer bounds come from training rows only, and are labelled `fixed_rule_fold_diagnostic`, in-sample for selection. Nested folds use their own past. Pinning resolved bounds in the freeze is still useful: added. |
| 1.2 Fly term signals empty | Real, different fix | The proposed `curve()` fallback mixes at-the-money IV with strike-`k` IV in one curve, so it puts skew in the term signal and changes calendar signals too. Flies now read `atm_curve`, at-the-money on every tenor. Calendars are unchanged. Synthetic: `iv30` populated on 262 of 262 fly trades. |
| 1.3 `fork` hardcoded | Accepted | Platform-aware; spawn path run once on a synthetic month. |
| 1.4 Fly `var_carry_daily` empty | Accepted in part | Uses `iv_30` only. The proposed fallback to realized vol would put two different quantities in one signal. |
| 2.1 Presorted `choose` | Accepted | The patch added the parameter with no caller. Callers now use it. |
| 2.2, 2.3 SQLite mmap, cache, batch surface | Not applied | Three alternating warm runs: 18.3 s, 18.8 s, 20.3 s with the pragmas against 18.4 s, 17.4 s, 19.0 s without. `surface_enriched` is not in the patch. Measure on real data first. |
| Patch script | Not applied as supplied | `--check` is rejected. `--short-decay-take-profit` is accepted but does nothing. `surface_enriched`, cached holdout marks and `downside_var_share_5d` are described but absent. Three tests fail. |
| 3.x Literature | Partly misattributed | Kilic and Shaliastovich is Management Science, not JFE. Bali and Murray studies the cross-section of equity options, not index put skew. The Andries et al. co-authors are Eisenbach, Kahn and Schmalz. OHLC cannot produce Patton-Sheppard semivariance, which uses intraday signed returns. |
| New signals and the short-decay rule | Deferred | Both reviews argue against widening the search before a real baseline. Add a 30-day anchor to the tenor list when the pilot is extended: the code already supports it. |

**Parity:** calendar trades, marks and identities equal V4.5.2. Legacy-flag ranking: 25 of 25 results, maximum difference 0.

# V4.5.2 (response to the independent V4.5.1 review)

Trade files from V4.5.1 stay valid unless they used `--event-policy`; those are refused and need a rerun. Caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| N01 unresolved trades dropped from structure comparison | Confirmed, fixed | `all_opened` is the default and keeps conservative marks, reported separately from `fully_observed_diagnostic` and `unresolved`. |
| N02 pairing on labels | Key weakness real, severity lower than stated, fixed | One trade file has one `--strike-mode`, and on a real run 166 of 166 (day, step, value) keys map to one strike and expiry. The review's fixture mixes modes and has `strike` and `short_expiry` columns the real table lacks. The join now uses provider, day, short expiry, actual short strike and hedge, with unmatched counts by reason and `many_to_one_keys`. Identical-mark aliases count once. |
| N03 date-level announcements | Confirmed, fixed | `event_date,first_known_at[,superseded_at[,source]]`, compared with the 09:45 decision in IST. A bare date counts from the next day. Each revision is its own row. |
| N04 window depended on supplied runs | Confirmed, fixed | `--eval-start` and `--eval-end` are required. Reports extra entries and the matched-cohort mean. |
| N05 freeze omitted code and cache | Confirmed, fixed | The freeze binds code hash, release, market digest, enrichment run, tariff, masters, calendars and the session list. A refusal names what moved. |
| N09 support across the expiry move | Accepted | `expiry_regime_mix.post_move_train_support` is `sufficient` or `insufficient`. |
| N12 output durability | Not changed | An interrupted run stays `complete=0`, and a full run takes seconds to minutes on the synthetic grid. Revisit if a real run takes hours. |
| N06, N07, N08, N10, N11 | Agreed, already disclosed | No code change. They need real data, broker notes or SPAN. |

The review's `NIFTY_V451_REPRODUCTIONS.py` no longer runs as written: its fixture lacks the `details` and `mtm_id` columns of the real trades table, which the corrected join reads. COMP01-04 in `tests/test_v4.py` cover the same cases on the real schema.

# V4.5.1 (response to the independent V4.5.0 review)

Rerun simulations: the trade schema gained `contract_id`, `mtm_id`, `tm1_outcome`; ranking refuses earlier files. V4.4.3 and later caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| F01 iron fly needed a far-dated quote | Reproduced, fixed | Flies get their own candidates (`target_tenor` 0, no long expiry). Nothing reads a far quote. `compare_structures.py` reports each universe, the dates only one side has, and the paired intersection. |
| F02 entry on T-1 held to expiry | Reproduced, fixed | Option B: rejected up front as `tm1_entry_on_last_session`, also for `p25_s50_tm1` and `rolling_tm1`. A same-day round trip is a different strategy. |
| F03 T-1 wrong without holidays | Reproduced, fixed | `--holiday-calendar` is mandatory for T-1 policies. The file is audited against cache sessions and a mismatch stops the run unless `--allow-calendar-mismatch`. `short_sessions` now uses it. Ranking warns when exact-session groups ran without one. |
| F04 `path_id` not canonical | Reproduced, fixed | Three identities: `contract_id` (dated signed contracts), `path_id` (signed cash flows, hedge included, no labels), `mtm_id` (daily marks and bounds). Trials, SPA and the shortlist use `mtm_id`. `search_counts` reports all three. |
| F05 holdout windows differed by group | Confirmed, fixed | Under `per_trade`, every strategy is scored from the split to the data end, and no trade can move that. `holdout_cohort_followup` is the selected trades' lifecycle view, labelled not comparable. Long-tenor strategies have fewer admitted entries in that window, which `holdout.n` shows. |
| F06 `risk_normalized` is not sizing | Accepted | Renamed `risk_normalized_score`, labelled counterfactual. Integer-lot sizing stays open (P2). |
| F07 failed T-1 exits | Confirmed, and worse than reported, fixed | A failed T-1 close left `pending_exit` set, and that blocked the 15:16 buyback on expiry day, so the short went to settlement. It now retries and buys back on expiry. Every T-1 trade has `tm1_outcome`; `tm1_execution` is per result; roll completed and failed counts are in diagnostics. A margin stress is still open. |
| F08 event calendar needs as-of | Accepted, fixed | Lines must be `event_date,first_known_on`. An event counts only from its announcement. Hash is in lineage. |
| PREV-01 flat sessions | Reproduced, fixed | `compare_fill_models.py` now needs `--cache` and scores every run on the session calendar. |
| PREV-02 break-even slippage | Reproduced, fixed | Total P&L over total traded units. Field renamed `breakeven_extra_slippage_pts_per_traded_unit`. |
| PREV-03 holdout only logged | Reproduced, fixed | `--research-mode train_only` is the default: no holdout number is computed and `selection_freeze.json` is written. `final_holdout` runs once, only if trades, settings and rules hash the same, and refuses a second look without `--allow-reinspection`. `open` is the old report. A person can still delete the log; this stops accidents, not intent. |
| Family ensembles naming | Accepted | Now `unfiltered_family_books`, with a note on admission rules. |
| 13 configurations, not 16 | Agreed | Flies no longer repeat per tenor. |

Deferred, as the review sequenced them: integer-lot sizing, margin shock stress, matrix reuse across basis views (Wave D), everything needing real data.

**The reviewer's script now stops at its TM101 assert. That is the fix:** a Monday-to-Tuesday `tm1` entry is rejected, so `err` is no longer `None`.

**Parity against V4.5.0 (synthetic):** 11,214 trades and all marks identical. Ranking with `--research-mode open --train-censor realized --holdout-entry-cutoff global_longest --bucket-scheme legacy`: all 25 results identical, maximum difference 0.

# V4.5.0 (deep review of V4.4.5)

See `REVIEW_V4_5.md`. Rerun simulations: the trade schema gained `scheduled_exit_day`, `short_sessions`, `entry_weekday`, `short_expiry_weekday`, `entry_risk_pts`, `events_in_short`, `path_id`; ranking refuses earlier files. V4.4.3 and later caches stay valid.

Default changes in `rank_strategies.py`, each with a flag to restore the old rule: `--train-censor scheduled`, `--holdout-entry-cutoff per_trade`, `--bucket-scheme exact_sessions`.

# V4.4.5 (response to the V4.4.4 reviews)

Rerun every simulation: the trade schema gained `economic_id` and ranking refuses earlier files. V4.4.3 and later caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| R01 duplicate economic trades | Reproduced, fixed | One simulation per economic key (actual legs, policy, hedge, lot; target tenor only for `rolling_recenter`, where it steers the path). Aliases keep their own labels and signal features. `economic_id` is on every trade. Effective trials, global SPA, shortlist (`parameter_aliases`), campaign report and fill comparison all count each economic trade once. Alias rows are still written, because step 100 and step 500 are different rules on other days. |
| R02 calendar hashes not rechecked | Reproduced, fixed | `lineage.verify` rechecks all four auxiliary files. A file recorded at the run must exist with the same bytes. |
| R03 `--shift-control-max 1` crash | Reproduced, fixed | One deterministic mid-sample shift. `rank_strategies.py` now validates its arguments. |
| R04 mask count | Confirmed, fixed | `evaluated_unique_full_mtm_masks` counts only scored masks. `search_counts` reports rule cells, thin cells, encountered, scored, economically unique, finalists. |
| R05 zero-volume fills | Accepted as a limit | New stresses: `next_bar_open_traded_only` (ex-post filter, labelled) and `--entry-delay-minutes 1-3`. `compare_fill_models.py` adds zero-volume counts, calendar Sharpe and drawdown of the all-trades book. Delay covers entries and rolls only: exits have two cached minutes per window. |
| R06 holdout reuse | Partly addressed | Each ranking appends to `holdout_inspections.jsonl` beside the trades file and reports `holdout_inspection_number`, with a warning after the first. Sealing a final holdout is still your decision. |
| R07 audit labels and selection | Fixed | Diagnostic stages renamed `any_independent_liquid_print`, `any_synchronous_liquid_print`. `admitted_vs_rejected.csv` adds short and long DTE and IVs. |
| R08 contract master | Open | Ambiguous lots already show as a monthly stage. An NSE contract master must come from you. |
| R09 attribution and margin | Attribution fixed, margin open | The unexplained bucket is split into `transaction_charges`, `slippage`, `exercise_costs`, `hedge_net`, `not_attributed_model_gap`. |
| R10 ranking memory | Fixed | The finalist list is bounded, diagnostic value lists thinned. SPA stays capped by `--spa-max-models`. |
| R11, R12, R13 | Deferred | Need `plan_2025_06.txt` and real runs. No new strategies in this release, as the review asked. |

Also fixed while sweeping: unclosed SQLite connections in `lineage`, `publish_results`, `campaign_report`, `compare_fill_models`; invalid ranking arguments that crashed late.

Gemini's follow-up: margin model needs SPAN inputs. Corporate-action handling does not apply to NIFTY index options. Boosted trees, HMMs and parallel ranking are deferred: the search is already too large for its sample, and ranking took 3.4 s on the synthetic run, so a real profile comes first.

## How many combinations
| Level | Count |
|---|---|
| Simulations per eligible entry day, default flags | at most 216 (2 steps x 3 offsets x 6 tenors x 6 policy/structure pairs) |
| Strategy groups | at most 648 (three DTE buckets); most stay empty |
| Rules per group, default 3 signals | 60 |
| Rules per group, `--signals all` | 850 (38 x 20 fixed + 10 x 9 quantile) |
| Theoretical cells, `--signals all` | 550,800 |

Gemini's 15,552 assumes all hedges and nine policies, which are not defaults. Real counts appear in `search_counts`. Fill models, lots, participation, strike modes and hedges are separate runs.

## Parity against V4.4.4 (synthetic, 100 and 500 steps, puts and doubles, three policies)
187 of 187 trades identical. 98 simulations reused, 187 rows from 127 economic trades.

# V4.4.4 (response to the ChatGPT and Gemini reviews of V4.4.3)

Rerun every simulation; ranking refuses earlier trade files. V4.4.3 caches stay valid.

## ChatGPT
| Finding | Verdict | What changed |
|---|---|---|
| F01 fill depended on later fill-bar volume | Reproduced, fixed | `next_bar_open` no longer reads the fill bar's volume. Its volume, slippage range and eligibility all come from the finished minute. A fill on a zero-volume bar is priced and flagged (`fill_bar_zero_volume`, `fills_on_zero_volume_bar`). |
| F02 valuation tied to the 09:45 bar | Reproduced, fixed | `Data.days` is entries only; `Data.valuation_days` carries open trades and equals the ranking calendar. Missing 15:29 spot falls back to 15:16, then the daily close, flagged in the mark. Expiry settlement gains a daily-close proxy from the expiry session itself. |
| F03 audit ignored order size | Reproduced, fixed | `--lots`, the simulator's own gate, and the signal-debit check. `participation_stress.csv` covers lots 1/2/5/10 by cap. `execute_fills` alone decides admission. `weekly_exit_eligible` is a diagnostic. |
| F04 adverse model called causal | Accepted | Evidence class `ex_post_adverse_stress`; only `next_bar_open` is causal. |
| F05 exercise fee per leg | Accepted | `--exercise-fee-basis` per_contract (default), per_order, per_leg, zero. |
| F06 gamma add-on outside stop basis | Confirmed, fixed | The debit is set after every entry leg, the add-on and hedge cost. |
| F07 volume assumptions | Partly fixed | `volume_unit_source`, `liquidity_asof` and the zero-volume flag are recorded. Vendor documentation is still needed. |
| F08 settlement evidence | Partly fixed | Per-run counts for proxy settlements, assumed exercise fees and late settlement. Settlement already used only the expiry session's own prices. |
| F11 selection bias | Partly fixed | `admitted_vs_rejected.csv` compares 5-day RV and overnight move. Inverse-probability work is not done. |
| F12 auxiliary files | Fixed | Holiday and special-session files are hashed into meta and lineage; `tariff_version` joins lineage. |
| F15 double-calendar forward IV | Fixed | Forward variance per wing, then averaged; labelled `wing_average_descriptive`. |
| F09, F10, F13, F14, F16, F18 | Deferred | Order queue, margin simulator, sealed holdout process, jump stress, 2-D filters and ablations need real-data results first. |
| F17 attribution | Partly fixed | See Gemini E-01. |

COST07 needs a real contract note. PERF04 needs real data.

## Gemini
| Finding | Verdict | What changed |
|---|---|---|
| R-01 recenter | Partly right | The long could move before a short was known possible, on the non-synchronized path. Both paths now share one all-or-none block. Reasons are counted: `recenter_no_long_tenor`, `recenter_lot_mismatch`, `recenter_fell_back_to_plain_roll`. The "expiry == session" desync does not occur: rolls need more than 30 DTE. |
| R-02 iv_converge | Rejected | It reads each leg's own strike and handles pending exits first. The exit rule is defined on the raw ratio, independent of the entry filter, which is chosen after simulation. |
| R-03 curve bypass | Fixed | All signal IVs go through `data.greek`, so signal and simulator read one number. |
| R-04 holdout leakage | Rejected as a leak | Thresholds come from training rows only. The provenance gap was real: `quantile_rule`, `threshold_basis` and fold `resolved_bounds` are now reported. |
| S-01 SPA | Diagnosis wrong, problem real | See below. |
| S-02 shift control | Fixed | Shifts move whole candidate days. The seam is inherent to a circular test. |
| P-01 | Partly | Skips evaluation under `min_train`, reuses the raw basis. `FastChooser` rejected: `choose` barely registers in the profile, and bisecting non-session dates can reorder a same-day comparison, so it is not bit-identical. |
| P-02 hashing | Not changed | 1.85 us per row. |
| Enrich `t_years` caching | Rejected | One call per group, 36,480 calls in a 37 s run. |
| E-01 attribution | Fixed | Reprices one input at a time, adding `vanna_volga` and `rate`. Residual is zero on model prices. |
| 3.2 signals | Partly | `term_curvature_5_30_90` and `fwd_iv_to_rv_yz` added. `net_vega_to_debit` rejected: the debit is a fill price, unknown at 09:45. |
| 3.3 plateau | Fixed for thresholds | `plateau` on each finalist. Across strikes and tenors is open. |

## SPA calibration (trade_book and ma_increments, 150 runs, 300 reps)
Gemini's drop-in (analytic variance, longer auto block) was tested first. Analytic variance barely moved size, and a longer block made it worse: hold 30 went from 32% at block 10 to 52% at block 90. Studentizing each replicate is what helped, so `spa_test` is now a batch-means bootstrap-t.

| Hold | Block | Old size at 5% | New size (trade_book) | New size (ma) | New power (trade_book) |
|---|---|---|---|---|---|
| 5 | 10 | 13.5% | 9.3% | 4.7% | 72% |
| 15 | 10 | 13.5% | 6.7% | 8.0% | 56% |
| 15 | 20 | n/a | 4.0% | 4.0% | 48% |
| 30 | 10 | 32% | 15.3% | 21.3% | 51% |
| 30 | 20 | n/a | 8.0% | 10.0% | 29% |

Size is better but not nominal for long holds, and power falls. Use `--bootstrap-block 20` for campaigns of about 30 days.

## Parity against V4.4.3 (synthetic, `qualified_minute_close`, `--exercise-fee-basis per_leg`)
Unhedged 148 of 148 and hedged 74 of 74 trades unchanged. Under `next_bar_open`, V4.4.4 admits 12 more trades and paired P&L rises Rs 642 per trade, because a later zero-volume bar no longer vetoes a fill. 108 of those 140 trades used at least one zero-volume bar on this synthetic data. Check that count on real data before trusting the result.

# V4.4.3 (response to the V4.4.2 independent review)

Rebuild the cache and rerun every simulation: `SPOT_SQL` changed and ranking refuses earlier trade files.

| Finding | Verdict | What changed |
|---|---|---|
| F01 multi-lot brokerage | Confirmed, fixed | `--lots` and `--brokerage-order-mode` (`per_lot_orders` default, `one_order_per_leg`, `lots_per_order`, `fixed_orders`). `broker_actual` needs contract notes, so it is not built. |
| F02 non-ratio signals could not pass the grid | Confirmed, fixed | Nine features now use training-only quantile intervals (`--quantiles`). In nested walk-forward each fold derives them from its own embargoed past. `rankings.json` reports `signal_coverage`. Ratio features keep the ratio grid. |
| F03 fill not causal | Accepted | New default `next_bar_open`: eligibility from a finished minute's volume, then the next printed minute's open within the window. A later bar's volume or close cannot gate or price it, and slippage reads the eligible minute's volume. `qualified_minute_close` now needs the conditional flag. Every fill carries `evidence_class`; none claims to be executable. `next_bar_open_adverse` is the worst-print stress. |
| F04 cost ledger | Partly fixed | Dated futures exchange charge and futures brokerage capped at min(Rs 20, 0.03%). `tariff_version` stored. **Not done:** contract-note rounding, financing, RMS, shortfall fees. These need your broker's notes and terms. |
| F05 volume units | Partly fixed | Detection now samples every month and refuses if any month is ambiguous or months disagree; `volume_unit_report.json` records it. `audit_contract_coverage.py` writes `participation_stress.csv` at 1/2/5/10%. Bar volume still cannot show displayed depth. |
| F06 hedge and capital not executable | Accepted, deferred | No code change. The hedge stays labelled `UNTRADEABLE_PROXY`. A real hedge needs futures data and SPAN inputs. |
| F07 unobservable stops | Fixed as a stress | `--unobservable-stop-policy force_exit`; diagnostics add P&L on affected trades. Compare runs with `compare_fill_models.py`. |
| F08 SPA | Already disclosed | The warning is already in every `rankings.json`. No code change. Sealing one holdout is a process rule for you: do not reuse it across releases. |
| F09 spot source | Confirmed in code, fixed | `SPOT_SQL` now reads the same file as `DAILY_SQL`. New read-only `audit_spot_sources.py` compares every NIFTY copy and exits 1 on differences. The live run's two synthetic copies differ by construction, so it correctly reported them. |
| F10 order-level ledger | Partly fixed | Every fill records line-item `charges` and `charged_orders`. Hedge marks, rebalances and costs are ledger events, and the ledger alone rebuilds P&L. `observed_open` is renamed `reference_price`. Separate orders/executions tables are not added: OHLC data has one execution per order. |
| F11 invalid settings | Confirmed, fixed | `validate_config` in the simulator and audit; `required_volume` raises on an invalid cap. |
| F12 exploration, attribution | Deferred | Needs real-data results first. |

**Performance recommendations:** not actioned, by design: the review itself says to measure on real data first.

**Acceptance tests in `tests/test_v4.py`, class `V443`:** COST01-04, TIME01-03, VOL01-02, SIG01, MKT01, RISK01-02. COST03's contract-note rounding fixture is open. STATS01-02 and PERF01-02 need real data.

**Rerunning `NIFTY_V442_INDEPENDENT_REPRODUCTIONS.py`:** it stops at D with `ValueError`, the fix. A still prints because it calls `fill_flow` without a lot size; order splitting needs the lot. B exercises the legacy `next_minute` mode, still available behind the flag. C checks numbers against the ratio grid directly, and those signals no longer use it.

## Parity against V4.4.2 (synthetic; `qualified_minute_close`, `one_order_per_leg`)
| Run | Trades | Changed |
|---|---|---|
| Unhedged, 4 policies | 148 / 148 | 0 |
| Delta-entry hedged | 74 / 74 | 14, from the futures brokerage cap only (mean +Rs 6.28, max Rs 44.01) |

**Effect of the new default:** `next_bar_open` drops 20 of 148 trades (no common later print in the entry window) and lowers paired P&L by Rs 708 per trade. `next_bar_open_adverse` takes a further Rs 330. On real data these numbers will differ.

# V4.4.2 (costs, volumes, no lookahead)

Rerun every simulation; ranking refuses earlier trade files. V4.4.1 caches stay valid.

## Costs
| Item | Before | Now |
|---|---|---|
| NSE option charge | Flat 3.5 bps | Dated: 4.95 bps before 2024-10-01, then 3.553 bps including IPFT. `--exchange-bps` overrides |
| SEBI fee | Missing | 0.01 bps, GST applied |
| STT, stamp, brokerage + GST | Correct | Unchanged, now tested by hand per side and across each date boundary |
| Expiry | STT only | Adds `--exercise-fee` (default Rs 20 + GST per ITM leg). A conservative assumption; set 0 if your broker charges none |
| Index hedge | Flat 2 bps | 2 bps plus NIFTY-future statutory costs by side: STT 2 bps sell, 5 bps from 2026-04-01; stamp on buys; exchange, SEBI, GST, brokerage. `--no-hedge-statutory` restores the old charge |
| Duplicate cost code | `candidate_core.fill_cash` | Removed: unused, one cost path remains |

## Volumes
- New `--max-participation` (default 10% of the bar, 0 disables) on every entry, exit, roll, recenter and the gamma add-on; the coverage audit applies the same gate. 10% is an assumption.
- `--volume-unit auto` detects from the data whether vendor volume counts units or contracts, per run, and stops if it cannot tell.
- Slippage now uses contracts traded, so both vendors are priced on one scale.
- The independent recenter path ignored minimum volume altogether (reviewer F01). Fixed.

## No lookahead
| Leak | Fix |
|---|---|
| **Mine, in V4.4.1:** entries were blocked using `complete_session`, which is known only after the close | Eligibility is now weekday plus `--special-sessions`, a list published in advance. Days that later turn out incomplete are admitted and counted as `entry_day_later_incomplete` |
| Generator required the 09:46 spot bar | Removed |
| `short_sessions` counted future days in the data | Weekday count |
| Holidays for business-day IV inferred from gaps in later data | Weekdays unless `--holiday-calendar` supplies a published list |
| Debit gate read the fill price | Reads the 09:45 signal quotes |
| Open-based fill models condition on later volume | CLI and ranking refuse them without `--allow-conditional-fill-models` |

`test_ledger_prefix_ignores_later_data_on_every_policy` corrupts all data after seven cut times and requires the ledger up to each cut to be unchanged, across 9 policies and 2 hedges. `test_signals_ignore_everything_after_0945` deletes all later data and corrupts the entry day's daily row.

## Reviewer repro (`NIFTY_V441_INDEPENDENT_REPRO.py`)
- F01 recenter fill clock: fixed. The script now stops at its own "Fix appears applied" assertion.
- F02 partial sessions erased drawdown: my V4.4.1 regression. Fixed: the ranking calendar includes every day with data. The script calls `GroupMatrix` with a calendar it builds itself, so its assertion still passes; `valuation_sessions` is the fix and has its own test.
- F03 unobservable stops: now reported as `stop_unobservable` ledger events and counts per run. Exits remain delayed when a stop cannot be observed, since no causal price exists.

The V4.4 review's F02 asked that incomplete sessions be excluded from entries. That cannot be done from the day's own outcome without lookahead, so it is replaced by the published-list rule above.

## Parity against V4.4.1 (synthetic, legacy cost flags, participation off)
| Run | Trades | Changed |
|---|---|---|
| Unhedged, 4 policies | 148 / 148 | 0 |
| Delta-entry hedged | 74 / 74 | 0 |

Effect of the new defaults on the same runs: unhedged mean -Rs 147.74 per trade, hedged -Rs 86.34. Four unhedged trades changed fill count because of the participation gate.

# V4.4.1 (correction release: sixth review round)

Rebuild the cache and rerun every simulation. V4.4 caches lack the market content digest, and ranking refuses V4.4 trade files.

| Finding | Status | What changed |
|---|---|---|
| F01 hedge outlived the options | Reproduced, fixed | The index proxy closes at the last option fill. Hedged P&L no longer moves when spot changes after exit. |
| F02 partial sessions admitted entries | Reproduced, fixed | Generator, roll sales, coverage audit, trend features and the ranking calendar require `complete_session`. Open positions are still marked and settled on partial days. `--allow-special-sessions` opts in. |
| F03 open fill conditioned on that minute's volume | Accepted | New default `qualified_minute_close`: fills at the close of the minute whose volume qualified it, using nothing later. Open-based models are labelled conditional-liquidity models. Every fill records `price_field` and `executable_claim=false`. |
| F04 capital does not enforce margin | Deferred | Needs broker or SPAN inputs. Capital stays a labelled scenario. |
| F05 censored trades in ranking | Fixed for the holdout | `--holdout-entry-cutoff` defaults to `auto`: data end minus the longest tenor, independent of trade outcomes. A cutoff before `--split` is refused. |
| F06 lineage did not bind market rows | Fixed for builds | Order-independent per-month digest of every extracted row, resume-safe, carried through trades, rankings and publish. Quality JSON digest stored. A SQLite cache edited after the build is not rescanned. |

`compare_fill_models.py` reports trades lost, paired per-unit difference and break-even slippage across fill models (part of E05).

**One error of mine caught during the live run.** The first `auto` cutoff used the latest trade exit, so it depended on outcomes, and it could silently fall before the split and empty the holdout. Both are fixed and tested.

## Parity against V4.4 (`--fill-model next_minute`, 2025-10-01 to 2026-02-28, synthetic)
| Run | Trades | Changed | Total P&L V4.4 / V4.4.1 |
|---|---|---|---|
| Unhedged | 68 / 68 | 0 | ₹125,400.89 / ₹125,400.89 |
| Delta-entry hedged | 68 / 68 | 68 | ₹180,539.31 / ₹156,887.65 |

The hedged change is the declared F01 correction: per trade it equals hedge units x (spot at last option fill - 15:29 close), with at most ₹0.51 left over from hedge cost. The cache had only complete sessions, so F02 changes nothing here; the default fill model does change results, so use `next_minute` to compare with V4.4.

## Open, needing real data first
E01-E04 and E06-E16, P01-P09. An external NSE session master (E09) is not built: eligibility uses `complete_session` from the data, and `Data.holidays` is still inferred from gaps (affects only the opt-in business-day IV).

# V4.4 (fifth review round, and the guidebook)

Rebuild the cache, re-enrich and re-simulate. Exits need three new minute bars.

## Corrections
| Finding | Status | What changed |
|---|---|---|
| P0-01 lineage | Fixed, reproduced | Digest covers trades, marks, fills, diagnostics and run config; report content, code, requirements, migrations and master-file contents are verified at publish |
| P0-02 hedge timing | Fixed, reproduced | Hedge opens at the spread's fill minute. Missing spot rejects only the hedged path |
| P0-03 exit clock | Fixed, reproduced | Under delayed models every exit qualifies on one minute and fills on the next (11:32, 13:32, 15:17 now cached). Ledger records signal, order, fill and evidence times |
| P0-04 finalization | Fixed, reproduced | Idempotent; `kill -9` during index build then resume matches a clean build |
| P1-05 resume settings | Fixed | A resume must match every extraction setting and the SQL |
| P1-06 provenance | Partly | Master-file contents, requirements, migrations and runtime versions recorded. A market-row content hash is not computed |
| P1-07 mismatched cache | Fixed | Ranking refuses a different cache or enrichment |
| P1-09 / Gemini 4 gamma leg | Fixed | A third leg on the same entry clock |
| P1-23 SPA memory | Fixed | Collection stops at the cap; the family is still counted |
| Gemini 2 debit | Fixed | Stops use actual cost under delayed fills |
| Gemini 3 naked short | Fixed | A long is never sold while its short is open; unfinished exits retry at later checkpoints the same day |
| Gemini 5, 1 | Fixed; labelled | Cache clear; holdout stress is documented as open-position path stress |
| P2-24 | Partly | Within-group SPA memoized |

Parity: with legacy flags, 2,162 of 2,206 trades equal V4.3. The other 44, mostly `p25_s50`, are partial exits changed by the protective-long and same-day-retry rules.

## New diagnostics
- `circular_shift_control`: best threshold against the same search on a shifted signal. At nominal 5%, null rejection was 4.0-5.7% across 300-run simulations with persistent signals and P&L, against 13-32% for SPA. It covers one signal and group only.
- Daily Greek attribution, additive by construction: `details.pnl_attribution`.
- Session-time IV (`iv_ratio_biz`, `iv_to_rv_yz_biz`), a rescale of total variance. Causal trend fields. All opt-in.

## Guidebook paths
`--long-strike-offsets`, `atm_weekly_roll`, `--roll-timing`, `campaign_report.py`. See `GUIDEBOOK_RESEARCH_NOTES.md`.

## Not taken from Gemini's 20 points
- Point 12 was right on the structure, and is implemented. The "15% of sale price" tip-toe rule in Point 13 is not in the guidebook, which only names that strategy.
- Point 10 log-moneyness: over a strike gap of at most 4% the change is negligible.
- Points 7, 8, 9, 11, 17, 18 and 20: deferred until real data shows they matter. Point 7 needs a margin model, not an add-on to a scenario charge.

Deferred from ChatGPT's review: account ledger and liquidation, NSE session master, canonical-reason fields, verified-only settlement tracks. All need real data or external inputs.

---

# V4.3 correction release (fourth review round)

Rebuild the cache, re-enrich and re-simulate. No new strategies or signals.

| Finding | Status | What changed |
|---|---|---|
| F01/F07 fill timing | Fixed | `--fill-model`: `same_minute_first_print` (optimistic), `next_minute` (default), `next_minute_adverse_range`. Ledger records the observed bar and the fill assumption. Single-minute exits such as 15:16 cannot be delayed, because the cache has no later minute |
| F02 stale Greeks | Fixed, reproduced | Each run clears derived rows and reads as not enriched until it finishes; `enrichment_run_id` records its parameters |
| F03 audit vs simulator | Fixed, reproduced | Audit shares expiry selection and fill code with the simulator and reports every stage. `entry_both_liquid` is gone |
| F04/F20 lineage | Done | `lineage` block binds a report to trades, enrichment, arguments and code. Publishing refuses a mismatch and skips a repeat. No migration needed: it lives in the existing JSONB |
| F05 data quality | Done | Complete now means 370+ of 375 minutes; `session_quality`, OHLC violations and duplicate price conflicts audited |
| F08 hedge in stops | Fixed | Hedge marked to the checkpoint |
| F09 independent recenter | Forbidden at the CLI | |
| F19 cache build | Done | Monthly transactions, `--resume`, staged file, row reconciliation, integrity check, atomic replace |
| F06, F11-F17 | Deferred | Margin, account ledger and search discipline belong after a real baseline |

Blueprint items confirmed and fixed: one bad strike failed the whole parity fit; `iv_5` empty for 5-6 DTE front weeklies; a March 2026 expiry before the 31st got lot 65; SPA used a different capital denominator per model; DSR dispersion used survivors only; CSV mixed raw and scored metrics without saying so; duplicate `opt_lookup` index; `longs[0]` staleness; unclosed file; `lru_cache` retention.

Blueprint items not taken: the checkpoint exit is already one minute per leg, so needs no sync; `seen` dedupe is by design, since strike values are separate groups; delta discounting moves a strike by far less than one strike step; routing `generate()` through cached Greeks is speed only and waits for real profiling; `--max-capital` in raw rupees is correct.

---

# V4.2 ranking corrections (third review round)

No re-simulation is needed: trade databases and caches from V4.1 still work. Only `rank_strategies.py` output changes.

| Finding | Status | What changed |
|---|---|---|
| V4.1-F01 cross-fold limits | Fixed, reproduced first | `nested_walk_forward.portfolio` is one portfolio across folds; a position open at a fold boundary keeps its slot and capital. The summed fold aggregate is gone |
| V4.1-F02 flat days dropped | Fixed, reproduced first | Every Sharpe, drawdown, baseline and selection uses a fixed session window: 25.46 becomes 7.32 on the reviewer's case. `active_day_sharpe` and `time_in_market` reported separately |
| V4.1-F03 fold boundaries | Fixed | Fold statistics are MTM clipped to the fold, from one portfolio, counting carried-in trades. `entry_cohort_lifecycle_net` is kept as a labelled diagnostic. A flat fold scores 0 and is no longer skipped |
| V4.1-F04 `fixed_risk` | Renamed | `entry_capital_normalized` |
| V4.1-F05/F06 | Unchanged, labelled | SPA stays discount-only. Real fills, margin and settlements still missing |
| V4.1-F07 | Done | `VALIDATION_V4_2.md` replaces `VALIDATION_REPORT.md` |

On the synthetic grid only 6 of the top 25 rules survive from V4.1, so earlier rankings should not be reused.

## Gemini "V4.1 audit" (12 points)
Its status table repeats claims already disproved: ratio calendars, in-memory index, memoization, delta-adjusted fills, and `nse_twap_settlement` / `opt_enriched`. None exist. `calibrate_spa.py` does not use AR(1)/GARCH processes. `docs/live_postgres_run.txt` is synthetic rows on inferred tables, so it cannot motivate "real-data insights".

| Item | Decision | Reason |
|---|---|---|
| 1 Long-leg continuity gate | Decide from coverage audit | Sensible and lookahead-free, but the ">80%" figures are unmeasured. Snapped quasi-calendars change the structure; not now |
| 2 Session tagging | Audit added | Muhurat evening bars cannot enter: the extract keeps 09:15-15:29 and `data.days` needs a 09:45 bar. Short morning sessions can; `special_sessions` now lists them |
| 3, 6, 8, 10, 11, 12 | Same as last round | Tax cliff remains arithmetically wrong (STT is a fraction of intrinsic); no migration needed, as the independent review agrees |
| 4 Business time | After coverage | Same; its Friday direction and size remain wrong |
| 5 Greek P&L attribution | **Best new idea; after the pilot** | Tells carry from direction. Needs reliable daily IVs from real data first |
| 7 Intraday IV exit | Already exists | `iv_converge` checks 11:30, 13:30, 15:15 |
| 9 CPCV, PBO, plateau | After the pilot | As before |

---

# V4.1 corrections (second review round)

Trade databases from V4.0 must be re-simulated. Rebuild the cache for close-to-close minute variance.

**Correction to my V4 note:** I advised treating SPA p below 0.01 as the bar. On realistic overlapping trades that is not safe; see the table below.

| Finding | Status | What changed |
|---|---|---|
| F-V4-01 recenter order | Fixed, reproduced first | All three legs fill in one minute, or no recenter happens (`recenter_no_common_minute`). On synthetic data 165 of 379 recenter trades changed P&L |
| F-V4-02 nested walk-forward | Fixed | `nested_walk_forward`: configuration and thresholds per fold come only from earlier, fully exited, embargoed trades. Old folds renamed `fixed_rule_fold_diagnostic` |
| F-V4-03 labels | Done | `labels` block, capital-multiplier stress. Real margin and bid/ask remain missing |
| F-V4-04 roll gate | Done | `--roll-signal-policy`, `--roll-iv-min/max`; every decision kept in `roll_decisions` |
| F-V4-05 partial exits | Done | Pending retries pair when a common minute exists; residual days tagged in mark quality and counted. Capital unchanged: the scenario charge already applies per short unit |
| F-V4-06 minute RV | Done | `rv5_1m_range_proxy` and a separate `rv5_1m_closeclose` |
| F-V4-07 lot eras | Done | `--score-basis`, default `per_unit`; `basis_views` reports raw, per unit, fixed risk |
| F-V4-08 SPA calibration | Tested, **not solved** | See below |
| F-V4-09 cohorts | Done | `--holdout-entry-cutoff`, `holdout_cohorts`. Official settlements still need your data |
| F-V4-10 runbook | Done | `README_V4.md` |

Acceptance tests T01-T07 are unit tests. T08 is in `docs/live_postgres_run.txt`, on inferred tables and synthetic rows. T09: on synthetic data `intrabar_range` is close to `tenor_volume`, because the fixture's bars are narrow, so real-data sensitivity is still unknown. T10 is `calibrate_spa.py`.

Regression: with legacy flags all 2,586 trades equal V4.0; with `--score-basis raw` the top 25 rankings are identical.

## SPA calibration (`calibrate_spa.py --runs 200 --reps 300`, nominal size vs observed)
`trade_book` is overlapping campaigns; `ma_increments` is a harsher persistent process.
```
process        hold block size@5% size@1% power@5%(+0.25sd/day)
trade_book        5     5  0.110  0.035    0.840
trade_book        5    10  0.135  0.030    0.860
trade_book        5    20  0.190  0.085    0.880
trade_book        5    40  0.235  0.150    0.980
trade_book       15     5  0.145  0.035    0.840
trade_book       15    10  0.135  0.040    0.820
trade_book       15    20  0.185  0.065    0.800
trade_book       15    40  0.265  0.095    0.820
trade_book       30     5  0.390  0.190    0.700
trade_book       30    10  0.320  0.150    0.640
trade_book       30    20  0.320  0.155    0.620
trade_book       30    40  0.420  0.235    0.620
ma_increments     5     5  0.195  0.060    0.460
ma_increments     5    10  0.185  0.045    0.420
ma_increments     5    20  0.205  0.090    0.420
ma_increments     5    40  0.280  0.140    0.540
ma_increments    15     5  0.420  0.255    0.540
ma_increments    15    10  0.320  0.160    0.400
ma_increments    15    20  0.315  0.135    0.360
ma_increments    15    40  0.375  0.200    0.420
ma_increments    30     5  0.625  0.510    0.640
ma_increments    30    10  0.500  0.390    0.520
ma_increments    30    20  0.445  0.330    0.420
ma_increments    30    40  0.475  0.350    0.480
```
False positives run far above nominal, and worse with longer holds. Treat SPA as a way to discount a result. A high p-value is informative; a low one is not confirmation. Nested walk-forward and the untouched holdout carry the weight.

## Gemini V5 roadmap
Its V4 scorecard is unreliable. Items 5 and 9 are marked incorporated, but ratio calendars and memoization do not exist in the code; it also cites `nse_twap_settlement`, `opt_enriched` and delta-adjusted fills, none of which exist.

| Item | Decision | Reason |
|---|---|---|
| 1 Asian TWAP, smart close | Rejected for now | Rests on settlement being a 30-print average. "Negative net payoff" is wrong: STT is a fraction of intrinsic, so payoff stays positive. Shorts pay no exercise STT. Shorts usually close at 15:16 anyway. Needs partial-window data the cache lacks; full-window TWAP would be lookahead |
| 2 Business time | Next, after coverage | Real issue, wrong numbers. At 09:45, Friday-to-Tuesday calendar IV roughly equals session IV (not 18-25% below). Monday is about 50% above, but default entries are Wed-Fri. Measure the weekday effect on real data first |
| 3 Minimum-variance delta | Rejected for now | The hedge is a non-tradable index proxy, excluded from ranking. Needs futures history. The 30-45% claim is unsupported |
| 4 Term factors | Deferred | More signals enlarge the search, and multiple-testing control is not reliable yet |
| 5 Vega sizing | Partly covered | `fixed_risk` addresses cross-era sizing; revisit after a baseline |
| 6 Intraday IV exit | Already exists | `iv_converge` already checks 11:30, 13:30 and 15:15. The claim of EOD-only is false |
| 7 CPCV, PBO | Good candidate after the pilot | PBO needs real runs to mean anything; the 0.25 gate is arbitrary |
| 8 Plateau gate | Good candidate after the pilot | Cheap, once the grid is real |
| 9 Multiprocessing | Deferred | About 4s for 2,586 simulations; profile real data first |
| 10 BLAS stacking | Rejected | Ranking takes seconds, and its drawdown code repeats the initial-loss error |
| Housekeeping | Benchmark wings added | `0002` migration not needed: new statistics fit the JSONB columns |

---

# V4 changes: review of V3 and the Gemini roadmap

Status: research software. Tested on synthetic data and on a temporary PostgreSQL 16 whose
`market_data` tables were inferred from the SQL in this repo. No real NIFTY result exists yet.

## Rebuild needed
Rebuild the cache to get `spot_daily_ext` (TWAP, 1-minute variance). An older cache still runs, but
TWAP settlement falls back to the 15:29 close and `rv5_1m` is empty. Re-run `enrich_cache.py` and
`calendar_research.py`; `rank_strategies.py` refuses a trade database from before V4.

## Bugs fixed (not in the Gemini review)
| Where | Problem | Fix |
|---|---|---|
| `publish_results.py` | `ON CONFLICT DO UPDATE` with no target is a PostgreSQL error, so `--quality` aborted the whole publish | Conflict target named |
| `prepare_research_cache.py` | `Decimal` from NUMERIC price columns cannot bind to SQLite; extract crashed on the first batch | Cast to float/int. Applies if your columns are NUMERIC; the cast is harmless otherwise |
| `calendar_research.py` | Expiry settled only on `session==expiry`. If that session was missing from `data.days` the leg never settled and blocked the trade to the horizon | `session>=expiry`, counted as `settled_after_missing_expiry_session` |
| `calendar_research.py` | Gamma add-on delta left out of the initial index hedge | Included |
| `candidate_core.py` | IVs discounted at flat `--rate` while forwards used the parity-implied rate; `forward_iv_ratio` disagreed with the recomputed `iv_ratio`, and was stale for double calendars | Parity rate used; ratio recomputed for the traded legs |
| `rank_strategies.py` | `net` in the holdout stress scenarios ignored the stress | Reports stressed end value |
| `audit_quality.py` | `spot_daily` and timed spot come from different source files, unchecked | New `spot_source_consistency` block |

## Roadmap verdicts
| # | Item | Outcome | Correction to the roadmap |
|---|---|---|---|
| 1 | TWAP settlement | Done, as a **labelled proxy** | Not "exact". NSE settles on the index closing price, built from constituents' last-half-hour VWAP. A simple average of index minute closes only approximates it, so it never counts as `verified`. Expiries 2026-08-03 to 2026-12-06 (closing-auction regime) skip TWAP. Impact is small: shorts normally buy back at 15:16 and reach settlement only when that fill is missing |
| 1 | Exercise STT | Done | Buyer only, so the short leg is not charged. 0.125% to 2026-03-31, then 0.15% |
| 2 | Synchronized entry | Done, default on | Delta-shifting an earlier print is rejected: it invents a fill. No common minute means the trade is rejected and counted |
| 3 | Yang-Zhang, gap share | Done | For N=5, k=0.1197, not 0.134. Overnight returns never span a dropped session. The 40-55% gap share claim is unverified |
| 3 | 1-minute RV | Done | The roadmap's SQL is a summed Parkinson estimator, not squared returns. `rv5_1m` adds overnight variance |
| 4 | `bf25_short`, `var_carry_daily` | Done, opt-in | `breakeven_move_std` omitted: imaginary whenever forward vol exceeds short vol |
| 5 | Ratio calendars | **Deferred** | A 3:2 leaves a naked short and the capital model has no SPAN. The 3:2 neutrality only holds near 5/90 DTE. Needs a defined-risk wing and real margin first |
| 6 | Bootstrap, SPA | Done | Hansen's consistent p-value added. Universe is every mask scored, each against its own group's unfiltered baseline, on scenario-capital returns |
| 7 | Vendor concordance | Done | Compares open and close |
| 7 | Intrabar slippage | Done | The roadmap's cap makes a one-print bar free. Parametric slippage is now the floor |
| 8 | IV solver | Done | See below. The diagnosis was wrong: V3 has no Corrado-Miller step and 0 of 156 failures sat at 5.0; all were OTM quotes stuck at a low seed. The roadmap code hardcodes `fallbacks=0` and never verifies; V4 counts real fallbacks and checks every price |
| 9 | In-memory dict index | **Replaced** | The 800k row estimate is far too low: a 31-strike synthetic chain produced 3.06M rows in 217 sessions. Profiling showed point lookups cost about 10us, and the real cost was loading whole surfaces. V4 loads one expiry at a time. First-cycle memoization deferred: risk outweighs gain |
| 10 | Matrix replay | Done | The roadmap's drawdown misses an initial loss; V4 anchors the peak at 0. Agreement is within floating-point, not bit-identical |

## Measured (synthetic, this machine)
| Measure | V3 | V4 |
|---|---|---|
| IV solve, 200k quotes | 1.51s, 16,508 fallbacks | 0.14s, 0 fallbacks |
| IV solve, tick-rounded wings, 200k | 2.98s, 35,468 fallbacks | 0.14s, 0 fallbacks |
| `enrich_cache.py`, 3.06M rows (V4 also runs the vendor join) | 65s | 40s |
| `calendar_research.py`, 2,586 simulations | 7.2s | 4.0s |
| Portfolio evaluation, 300-trade subsets | 17.2ms | 1.2ms |

The roadmap's 50x and 35-60x end-to-end figures were not reproduced.

## Verification
- V4 with `--fill-sync independent --settlement-proxy close_1529 --exercise-stt 0`: all 2,586 trades match V3 on P&L, status, exit day and short sales; 61,578 marks agree within 2.5e-7.
- Rankings on identical trades: same 25 results in the same order, largest metric difference 1.8e-15.
- IV against V3 on 3.06M quotes: 7 differ by more than 1e-4; the three largest match Brent under V4 and not under V3.
- Full pipeline ran on live PostgreSQL 16: migrate, extract, enrich, simulate, audit, rank, publish.
- SPA null rejection at 5%: 8.5% over 200 simulations, so slightly liberal. Treat p below 0.01 as the bar.
- 40 unit tests, smoke run.

## New flags
`calendar_research.py`: `--fill-sync`, `--settlement-proxy`, `--exercise-stt`, `--slippage-mode intrabar_range`, `--intrabar-lambda`, `--intrabar-min`, `--exclude-vendor-discordant`.
`rank_strategies.py`: `--bootstrap-reps`, `--bootstrap-block`, `--spa-max-models`; signals `iv_to_rv_yz`, `iv_to_rv_1m`, `gap_share_5d`, `bf25_short`, `var_carry_daily`.

## Still open
- Sharpe and scoring use raw INR while lot size moves 25, 75, 65 across the sample, so later periods weigh differently.
- IV uses calendar time, so weekends distort short-dated IV. The session-count bucket only partly controls this.
- Sessions after 2026-08-03 have changed closing mechanics. Review before extending `--to-date` past July 2026.
- Every added signal enlarges the search. Read SPA alongside the holdout.
