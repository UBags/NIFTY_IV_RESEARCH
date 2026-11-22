# V4.9.8 notes (2026-11-13)

## Results that led here (V4.9.7 real data)
- Grid: 26 groups untestable (< 75% fill): naked -3..-5, deep-ITM monthlies, most ITM calendars. Monthly (m1/m2) and calendar families: no candidate inside the year band (listings regular only from 2024) -> untestable in-sample.
- LOYO: swing configs collapse out of year (bear naked swing ~+700 pts, t 0.4); zone_only holds (bear naked t 2.0-2.3, bull naked t 2.8-3.0, n ~22). Bull futures_zone ~ buy-and-hold after carry.
- Portfolio: 2,311 pairs pass target/floor/band, 0 pass drawdown (leg 1.04 L, pair 2.08 L). Lowest drawdowns: bear naked -2 alone 2.93 L (25%/yr in-sample); pairs with bull futures 3.59 L (66%/yr, mostly 2022-24 index beta). Freeze in portfolio_v497_nodd is diagnostic only - never use for the holdout.
- Drawdown diagnostics (pre-2025, natural end, 40% lots): once open loss >= 25K only ~1/3 of trades recover; OTM shorts almost never after 50-75K. Cut-offs chosen (in-sample): bear naked -2 50K, bear 0 37.5K, bear +2 75K, bull -2 37.5K, bull 0 50K, bull +2 37.5K, bull futures_zone 75K; bear futures_zone loses with any stop (drop it). With idealised -X fills these roughly doubled mean per trade and halved average loss (in-sample).

## User decision (2026-11-13)
Stops as % of margin so they scale with lots: 3.75% / 5% / 7.5% of position margin (~37.5K / 50K / 75K at 40% lots; position margin 10.0-10.4 L in every period).

## Implemented
- zone_rules `mloss X` (X 2.5/3.75/5/6.25/7.5/10), all structures; 3.75/5/7.5 in layered-policy initial protection. Grid must be recomputed.
- dd_cutoffs.py: fixed cut-offs per strategy, in-sample vs 2025-01..2026-07, stop exits at the stored fills (realistic), no cut-off vs with cut-off.
- dd_recovery.py, dd_bins.py in the release. Tests 563 pass.

## Holdout status
The user asked to evaluate the cut-offs on 2025-01..2026-07 = the holdout period. Running dd_cutoffs.py uses that period up as an untouched test for anything chosen afterwards; later freezes would need data after June 2026.
