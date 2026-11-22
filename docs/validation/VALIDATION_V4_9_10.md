# V4.9.10 validation

Synthetic only. New: ClosestBandV499 (without the band the skewed pair is closest; with it the even one), PairExitsV499 (candidates
belong to the given configuration; the command line runs screen -> executable -> out-of-sample and writes the CSV).
StrategyScanV4910: the scan writes passing strategies, a tighter per-trade drawdown removes the deepest, impossible thresholds give none.
`python -m pytest tests/`: 596 pass.
