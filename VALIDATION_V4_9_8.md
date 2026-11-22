# V4.9.8 validation

Synthetic only. `python -m pytest tests/`: 564 pass (the V4.9.7 suite, its naked stop count updated from 368 to 374, plus MarginStopV498):
- `mloss` fires at the first bar close whose mark is at or beyond X% of the position margin (2.5% at row 2, 5% at row 4 for a -5.2% mark, 7.5% at row 5), never if not reached; present for futures and in the layered policies.
- `dd_cutoffs.load` takes the stored exit (cash so far + exit cash and exit bar at the triggering row), leaves trades that never reach the level at their own end; `sequenced` takes one position at a time and a stop that frees the slot earlier lets the next signal in; the command line runs with `--optimise`.
