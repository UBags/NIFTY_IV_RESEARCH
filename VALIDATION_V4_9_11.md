# V4.9.11 validation

Synthetic only. StrategyScanV4910: the vectorised scan's yearly returns, trade counts and worst trade equal a trade-by-trade replay
(zone_grid.Detail) for 25 strategies; impossible thresholds give none; a tighter per-trade limit removes the deepest. StrategyScanMarksV4911:
the worst trade is the worst bar-close mark up to the exit (a -90K mark is seen with no exit rule, not by an exit before it). Both pass with
and without numba. `python -m pytest tests/`: 601 pass.
