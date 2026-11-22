# V4.9.2 notes (2026-11-12)

Correctness release after ChatGPT's review of V4.9.1 (12 findings; all confirmed in code). Full table in CHANGES_V4.md.

## Fixed
- Executable walk-forward account: each order admitted against (1 - 10% buffer) x (capital + P&L) minus margin in use; cut to
  whole lots or rejected (portfolio_orders.csv). Carried positions + next year's choice can no longer exceed funds.
- Training margin gate uses funds after losses, not initial capital.
- No search truncation: every target-and-floor survivor evaluated exactly (the old cap could produce a false "none passed").
- Integer lots: a leg of k lots holds min(k, lots matched on the 10-lot path); no fractional lots from partial fills.
- Numba kernel: infinities excluded, as numpy does.
- Every planned test year reported; "no new entries" vs "flat cash" separated; --leg-lots capped at the simulated size.
- Names: assumed_margin_*, daily_close_drawdown*.

## Data-gap policy (decided by default, changeable)
`zone_grid.py --gap-policy busy` (default): a signal that did not fill because DATA is missing (not_extracted, no_long_expiry,
no_short_expiry, lot_unknown) holds the slot until the zone changes, outcome unknown, never counted. `free` = old behaviour.
Thin markets (no_liquid_entry) still free the slot. Recorded in the grid lineage. Confirm before the real-data grid build.

## Still open
1. Margin rates are assumptions (futures/naked 12%, calendar 4%); replace with broker SPAN samples.
2. Smaller sizes reuse the 10-lot path's per-unit P&L (conservative); a roll that failed at 10 lots ends the path at every size.
3. Daily-close drawdown only; no intraday / gap stress.
4. Grid-level dedup of identical exits.
5. 2025-01 to 2026-07 holdout: once, after freezing.

## Run order
Rebuild the grid (algorithm digest changed), then zone_portfolio.py. Optional `pip install numba`.
