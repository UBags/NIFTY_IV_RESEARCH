# V4.9.7 validation

Synthetic only; nothing here is evidence of profitability.

## Test suite
`python -m pytest tests/`: 560 passed: 498 carried over from V4.9.6 (six updated for the new entry rule, below) and 62 new (counting cases inherited by the new test classes). The key cases
also pass with `ZONE_NO_NUMBA=1` (numpy path).

Updated for the deliberate behaviour change (full size or nothing):
- `test_entry_sliced_across_minutes`: 46 lots with 45 tradable in one window is now no trade; with `entry_bars=3` the second window completes 46 lots and the rows start at bar 2.
- `test_legs_fill_together_minute_by_minute`: by default a partial match is nothing; an explicit lower `need` still accepts a partial match (used by no caller).
- `test_thin_long_gives_a_smaller_matched_calendar`: 45 of 50 lots in one window is no trade; with 3 windows the full 50 fills in window 2.
- The `gap_holds_leg_in_account` cases: the ledger text now reads "no fill (data gap, or entry not completed in its windows)".

New cases:
| Case | Checks |
|---|---|
| StrategySpecV497 | entry lots = ceil(0.4 x max): 5 x 65 (2026), 4 x 75 (2025), 9 x 50 (2022), 13 x 25 (mid-2024); at least 1; paths and portfolio compute the same number |
| PathsV497 | completes in the third window, rows start after it; 135 of 200 lots after three windows: no trade, attempt end recorded; zone change during the entry cancels it; the grid reads `first_bar` (Batch row 0 = bar 3), holds the slot while the order works, fill stats and the 75% gate |
| GridV497 | windows: 2022-24 full years, no walk-forward year, LOYO 2022/2023/2024; with 2025 in sample, 2025 is a walk-forward year trained on 2022-24; band mask (worst / mean 0.683 out, 0.705 in, a negative year out); LOYO sums |
| SelectionDryRunV497 | synthetic grid directory with three full years: the band picks the steady combination (600 / 600 / 600) over a larger one-good-year one (2,400 / 200 / 200); LOYO folds present; walk-forward empty with its note; the 50%-fill group is untestable and never picked; the console summary has no `np.str_` |
| PortfolioV497 | the band gate in the search keeps the even curve and drops the skewed one; the account marks a trade from its first row |
| PortfolioRunV497 | a full portfolio run whose grid lacks three training years: no test years, the note, INSUFFICIENT EVIDENCE |
| AllocatedBasisV497 | leg share 5/12, pair 10/12; a steady 10% a year leg fails on the account basis (22%), passes allocated (9.17%); a pair of them passes at 10/12 (18.3%), fails at 1/2 each (22%); a 1.2 lakh dip fails the 5/12 drawdown limit (1.04 lakh) |
| CliDryRunV497 | `zone_paths.py`, `zone_grid.py` and `zone_portfolio.py` run from their command lines on a synthetic extraction (added after the first V4.9.7 zip shipped a `zone_paths.py` main that failed at start-up: a statement had been joined onto a validation line) |
| CoverageV497 | moneyness from strike - ATM: put above ATM = ITM, call below ATM = ITM |

## Known limits
- Full size or nothing still depends on volume traded after the signal (the user chose no ex-ante eligibility rule). The 75% fill gate limits how much of a group's signal set the fill rule can select, and `grid_fill_bias.csv` measures the selection directly; neither removes it.
- A calendar roll re-entry still has one window.
- The selection-bias check uses the ATM weekly natural-end result as the reference for every option group, including monthly and calendar groups (a different trade, the same signal).
- Returns are P&L over 25 lakh at 40% of the maximum lots per leg: a green + red pair uses at most about 80% of the margin budget, so the 22% target is harder to reach than with full-size legs.
