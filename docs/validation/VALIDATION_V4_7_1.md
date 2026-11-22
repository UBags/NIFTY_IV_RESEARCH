# V4.7.1 validation record

- `python -m unittest discover -s tests`: 336 tests pass (27 new in `tests/test_paths.py`, one added to `tests/test_zone.py` for the forced-exit sign).
- Equivalence: where both engines model the same thing, `zone_paths.py` + `zone_grid.py` give the same rupee P&L as `zone_backtest.py` to 1e-6
  (naked to expiry, zone change, calendar roll chain, spilled roll, short bought back before long sold, lag-cap forced booking after the sign fix,
  boundary mark, take-profit 50%, ATR(15) x 1 stop) and as `zone_futures.py` (futures held to zone change).
- Synthetic CLI dry run (45 sessions, random walk, Black prices, random volume; not evidence): 1,440 paths in 8 s; grid over 1,445 configurations
  in 27 s; `grid_report.json` detail totals equal the grid totals.
- Vectorised versions checked against the loop versions on the dry run: 1,440 paths identical (max difference 1e-10 rupees), grid `.npz` files and `grid_report.json` identical. Dry run: paths 9.3 s -> 7.3 s (now mostly SQLite fetch), grid + report 27 s -> 9 s (now numpy-bound).
- Not yet run on real data. Estimate for the full period: tens of minutes for each step, a few GB of output.
- V4.7.1: the review's probe B now fails (fixed); probe A's assertions only test that cash changes between rows (true: the short buy-back fills that evening) and are superseded by `ReviewV470.test_A_*` and `test_A01_prefix_invariance`; probe C simulates float32 storage, which is no longer used; probe D builds a path without `entry_index`, where `trail` falls back to the signal close (only for path files written by V4.7.0).
