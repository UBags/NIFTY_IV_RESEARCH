# V4.9.12 validation

Synthetic only. New: `nifty-diag-equity-dd-scan` (src/nifty_iv/diagnostics/equity_dd_scan.py), the equity-curve maximum drawdown of
every single strategy (closed P&L plus the open trade's 45-minute bar-close marks and its exit, starting flat; in-sample = trades entered
before --split, out of sample separately), ranked per side under drawdown caps.

- EquityDDScanV4912.test_kernel_drawdown_equals_brute_force_on_random_trades: the O(1)-per-trade kernel (lowest mark, highest mark,
  deepest fall from the trade's own high) equals a brute-force walk over every mark of 200 random trade sequences.
- EquityDDScanV4912.test_scan_matches_replay: yearly returns and drawdowns equal a trade-by-trade replay (zone_grid.Detail) for 40
  strategies; a tiny cap keeps only zero-drawdown strategies.
- EquityDDMarksV4912: a +30K bar-close high followed by a -90K mark gives a drawdown of at least 120K, as the replay.
- 210-session synthetic fixture: 60 random strategies of 2,487 kept, drawdown and 2024 return equal the replay.
- strategy_scan's group set-up moved to strategy_scan.prepare_group (shared); its tests unchanged and passing.

No grid or portfolio digest covers the changed files: existing zone_grid / portfolio outputs stay valid.
`python -m pytest`: 611 pass (numpy and numba paths).
