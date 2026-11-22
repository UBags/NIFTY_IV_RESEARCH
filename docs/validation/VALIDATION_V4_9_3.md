# V4.9.3 validation record

- Unit tests: 410 pass (V4.9.2's portfolio tests replaced by 22 V4.9.3 tests covering AT01-AT07, AT10-AT14, AT17, AT20 and the
  earlier cases; holdout one-shot / data-change / code-change refusals; API split guard; monthly-tenor coverage). With
  ZONE_NO_NUMBA=1: tests/test_paths.py 101 pass.
- Reproductions from the V4.9.2 review now fixed: 10 + 10 overlapping lots -> second order rejected at the 10-lot cap, sequential
  trades reuse it; same-day overlapping orders -> second cut to 4 lots ((22.5 L - 15.6 L) / 1.56 L), same day but sequential -> both
  10 lots and the day's peak is one position; changing only the entry day's later closes (10k / 20k / 40k) leaves admissions
  identical; margin rising after admission -> buffer use and funds short reported, year not deployable; full 2024 counts 1.
- Synthetic dry run (/home/claude/dry493): futures paths 2021-08 to 2026-06 built with --target-units 650 (650 units at lots
  50 / 25 / 65, 675 at lot 75), grid (split 2025-01-01), portfolio 19 s: lot cap binding in training (2023 window: 41,280 of
  101,216 survivors pass it), verdict FAIL on noise with reasons; order ledger margin per lot checked by hand (0.12 x 17,197 x 50 =
  Rs 1,03,183). zone_holdout.py ran once (FAIL on noise), refused the second run.
- Not yet run on real data. Paths must be rebuilt with --target-units 650, then the grid, then the portfolio.
