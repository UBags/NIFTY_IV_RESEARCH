# V4.9.6 validation record

- Unit tests: 498 pass (V4.9.5's 465 + StrategySpecV496, PathsV496, PortfolioV496 and parity extras: margin table lots
  21 / 31 / 10 / 12; side-aware ITM / OTM strikes with steps 50 / 100 / 200; H1 at 20 / 21 / 23 days, H2 February monthly,
  unlisted monthly = data gap, policy_ineligible not a gap; path engine strikes, monthly expiries and margin lots; exhaustive
  search (205 of 205); executable ranking; part year never a full year; equally truncated sessions caught; missing final week not
  imputed; trade beyond window refused). With ZONE_NO_NUMBA=1: tests/test_paths.py 189 pass. Existing behaviour (points mode,
  naked / calendar) unchanged: the earlier suites pass as they were.
- Synthetic dry run (/home/claude/dry496): futures paths 2021-08 to 2026-06 at --target-margin 25 lakh (1,050 units = 21 x 50,
  775 = 31 x 25, 750 = 10 x 75, 780 = 12 x 65), grid 31 s, portfolio 34 s with every screen pair replayed (2024 window: 375 of
  444 executable; 69 screen passes failed in the executable account). Verdict FAIL on noise. Holdout: refused without a holiday
  list, ran with --weak-coverage-check; central registry caught a copied freeze; exposure reported; full years [2025].
- Not yet run on real data. Real options with levels / monthly policies need a new extraction (zone_contracts.py, new --out).
