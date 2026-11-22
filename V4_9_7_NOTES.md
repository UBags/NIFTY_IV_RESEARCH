# V4.9.7 notes (2026-11-13)

## Why
First real-data grid (V4.9.6): best result bear naked level -4 (call 800 pts ITM) was a fill-selection artefact. Same config/rule at ATM: the 11 signals where -4 also filled made +1,779 pts (100% winners, no zone-flip losses); the other 34 made -1,601. Entry required prints in the 45 minutes after the signal; deep-ITM strikes print mostly when price moves toward them (the trade's favour). Bull naked lost out of sample; bull futures_zone mildly positive (t 1.7), not significant vs buy-and-hold.

## User decisions (2026-11-13)
- Entry window: 3 bars (was 1); participation 20% per minute (was 10%).
- Entry size: ceil(40% of margin-table max lots on 25 L) — 9x50 (2022), 13x25 (mid-2024), 4x75 (2025), 5x65 (2026).
- Full size or nothing (accepted simplification).
- No ex-ante liquidity eligibility rule (next-month liquidity spikes now and then).
- Walk-forward year needs >= 3 full training years before it -> none with data from Sept 2021 and holdout from 2025; holdout is the forward test.
- Band: every full training year positive, worst >= 70% of mean full year.
- Fill-rate gate: 75%.
- Targets on allocated capital: each strategy (leg) is allocated a nominal 5/12 of the account. One leg: target 5/12 x 22% = 9.17% of 25 L; floor 5/12 x 22% x 70% = 6.42%; drawdown limit 5/12 x 2.5 L = 1.04 L. A green + red pair: 10/12 of each (18.33%, 12.83%, 2.08 L).

## SIMPLIFICATION TO REVISIT
The 5/12 allocation is NOMINAL and the same in every year. Actual entries are 9/21 (2022), 13/31 (mid-2024), 4/10 (2025), 5/12 (2026) of the maximum lots, with different margins. Tune the allocation (and so the targets, floor, drawdown limit) to actual margin use once we know which strategies work.

## Implemented
- zone_paths: --entry-bars 3, --max-participation 0.20, --target-fraction 0.40; zone flip during entry = no trade; rows start after the completing window (first_bar); unfilled attempts hold the slot to the last window tried; 60 s heartbeat.
- zone_grid: algorithm 4.9.7; band in both ranking modes; --min-train-years; leave-one-year-out (diagnostic only); fill stats per group, --min-fill-rate gate, untestable_groups; grid_fill_bias.csv (ATM natural-end result of the same signals, filled vs not, Welch t); plain console summary.
- zone_portfolio: same entry rule in Sizing; leg = full size or 0; band gate in screen and executable qualification; folds need 3 full training years; fill gate on candidates; strategy_spec in the freeze code digest; --capital-basis allocated (default) --allocation 5/12, --floor default 0.70 x --target; returns still reported on 25 L (and on the allocated share for the in-sample pick); holdout verdict scaled by the frozen allocation.
- zone_coverage: moneyness labels from strike - ATM (F496-B). zone_holdout: --help crash fixed.
- Tests: 559 pass (498 carried, 6 updated for full-size entries, 61 new incl. synthetic selection dry run and allocated-basis cases).

## Open / caveats
- Full-size-or-nothing still depends on post-signal volume; the 75% gate bounds and grid_fill_bias.csv measures the selection, neither removes it.
- Roll re-entry (calendar) still one window.
- Rebuild needed from Step 3 (paths); contracts and tenor coverage unchanged. The allocated basis only affects Step 6.
- Holdout not to be run until a freeze exists from the rebuilt pipeline.
