# V4.7.x notes (2026-11-11)

## Bug found: forced long exit sign (V4.6.0–4.6.4)
`zone_backtest.Sim.close` booked a calendar long force-closed at the lag cap as `cash = -units × price` (a purchase) instead of `+units × price`. In the V4.6 trades file: 332 forced exits (320 bear call calendars, 12 bull put calendars), median understatement 2,317 points per unit each. **All V4.6.x bear-calendar results are invalid (biased down).** Naked options and futures unaffected. Fixed in V4.7.0 with a test.

## Parameter provenance (answer given to user)
The frozen rules in ZONE_STUDY_V4_6_1 (red: N3 k1.5 m8 j0.25 tw4, strike +400 ITM, TP20; green: zone-only tw6) are in-sample winners picked from about 260k combinations (2021–24), so their training figures are flattered. Only the walk-forward figures (re-picked each year on earlier data) are honest.

## Buy-and-hold vs green trend rule (per unit, spot prices, before carry)
- 2021-06-01 → 2025-01-01: buy and hold +8,097 index points (max DD 3,141); green zone-only tw6 ATR30 futures +6,662 over 28 trades, in the market about 46% of sessions. From 2021-09: green +6,822 vs buy-and-hold +6,610.
- Carry is not in the data: at an assumed 5.5%/yr, buy and hold pays about 3,750 points; the green rule pays only while held.
- 2025-01 → 2026-07: index +229 (flat), max DD 3,956.

## V4.7.0 design
- `zone_paths.py`: every (signal bar, side, structure, strike) simulated once to its natural end. Minute-sliced fills: ≤10% of each minute's volume, whole lots, (O+H+L+C)/4. Spilled roll exits complete before the new pair opens.
- `zone_grid.py`: profit × stop rules × every configuration / strike / structure, all stored in `.npz`.
  - Families: stop class × configuration set; walk-forward by year.
  - Exact detail for every pick.
  - Buy-and-hold benchmark.
- Reproduces `zone_backtest.py` exactly where both apply (tests).
- Vectorised after profiling, outputs checked identical to the loop versions.

## V4.7.1 (after the external review of V4.7.0)
Reproduced and fixed:
- **P01:** rows between calendar pairs after a spilled roll held cash that only arrived later. Now as of each bar, matching the old engine's boundary mark.
- **Exit-bar bug (found while fixing P01):** rows between pairs released the one-position lock early.
- **P02:** a futures exit with no next-bar print was booked at the bar close. It now waits for a print, or stays open.
- **P03:** float32 sums replaced by float64.
- **P04:** `trail` now starts at the index at the entry fill; `strail` keeps the old signal-anchored definition.

Added:
- Count of entries where one calendar leg filled and the other didn't.
- `--explain`, which writes any combination's trades.
- Atomic writes and `--resume`.
- Bar-close drawdown and MAE/MFE.

Grid size: options 24 × 61 rules, futures 13 × 55. 336 tests pass. Not yet run on real data.

Open decision: the reviewer's Tier A expansion (~440 stops × ~55 profits) would be about 16× the current grid, roughly 180 GB uncompressed if every entry config is kept. Recommended: run V4.7.1 on real data first to measure runtime and disk, then decide.
