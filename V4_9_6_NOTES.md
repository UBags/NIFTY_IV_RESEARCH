# V4.9.6 notes (2026-11-12)

Restructure on the user's directions + ChatGPT's V4.9.5 review. Table in CHANGES_V4.md.

## User decisions (2026-11-12)
- 22% = simple yield on 25 lakh.
- Position limit = margin: total open margin <= 25 lakh, margin at the UPPER end of the user's table (strategy_spec.MARGIN_TABLE,
  2004 onwards; extend in code). Paths sized with --target-margin 2500000 (21 x 50 in 2022, 31 x 25 mid-2024, 10 x 75 2025, 12 x 65 2026).
- Strikes BOTH ITM and OTM (I had wrongly narrowed to OTM): levels -5..5, step 50 / 100 / 200 below 5,000 / 12,000 / above;
  green PE at ATM - level x step, red CE at ATM + level x step (--offset-mode levels).
- Monthly expiries: naked_m1 (current month, >= 21 days; else skipped, slot free), naked_m2 (next month). No rolling.
- Executable account is the source of truth: all screen passes replayed, ranked on executable metrics.

## Review fixes
Full calendar years as evidence; 9 expected bars per session in the holdout; period end not imputed over missing sessions;
holdout exposure; central holdout registry; screen-labelled robustness; trade-beyond-window refused.

## Agreed process
Stop review rounds after this release; next = real data:
1. zone_contracts.py --offset-mode levels --structures naked,naked_m1,naked_m2,calendar (new extraction)
2. zone_coverage.py --tenor-check (the 2021-24 source lacked many >30-day listings: matters for naked_m2)
3. zone_paths.py --offset-mode levels --target-margin 2500000 ...; zone_grid.py --holidays nse_holidays.txt; zone_portfolio.py
4. Review real results; holdout only after freezing.

## Still open
Buffer-use action (reported only), holdout end (July included?), min full years for the holdout (default 1),
calendar margin factor (default 1 = no spread benefit).
