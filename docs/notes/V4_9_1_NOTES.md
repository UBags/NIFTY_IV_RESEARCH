# V4.9.1 notes (2026-11-12)

Built from two reviews of V4.9.0 (Claude: claude/REVIEW_V4_9_0.md; ChatGPT: independent deep review). Full table in CHANGES_V4.md.

## Bug that reviewing missed until ChatGPT's pass
- Futures carry was never charged in `zone_portfolio`: it tested `structure=='futures'`, the labels are `futures_zone` / `futures_expiry`.
  All V4.9.0 futures-leg portfolio figures are gross of carry (long legs flattered, short legs understated).

## What the portfolio now does
- Constant-size valuation (650 units per full leg), sizes searched 0..10 lots per leg, assumed margin (futures/naked 12%, calendar 4%
  of notional) with peak margin <= capital as a gate.
- Walk-forward = one chronological account: flat adoption on 1 January, entries in the year, carry-out of open positions,
  continuous drawdown, cash when nothing passed.
- Identical-trade rule labels collapsed to one candidate; neighbour robustness split into identical vs distinct neighbours;
  top-10 distinct passing pairs' test results per year; carry scenarios 0 / 3 / 5.5 / 8%.
- `zg.verify` before the run.

## Synthetic finding worth keeping in mind
On pure noise the procedure still finds an in-sample pair passing every gate (22.8%, worst year 19%, DD 2.22 L), which then loses
out of sample. Passing the gates in training is not evidence by itself; read the walk-forward account and the top-K spread.

## Open decisions (yours)
1. Data gaps (`not_extracted`, `no_long_expiry`) currently free the position slot. Treat as "slot busy, outcome unknown"? Needs a
   path/grid change and a rebuild.
2. Margin rates are assumptions; replace with broker SPAN figures for a few dates per structure.
3. Grid-level dedup of identical exits (would also shrink the multiple-testing count and fix the grid's own stability score).
4. Use 2025-01 to 2026-07 once, as the final holdout, after the real-data V4.9.1 run.

## Run order
Grid must be rebuilt (algorithm digest 4.9.1). `pip install numba` optional (identical results, faster sequencing).
