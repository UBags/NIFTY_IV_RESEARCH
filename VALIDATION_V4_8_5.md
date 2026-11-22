# V4.8.5 validation record

- Unit tests: 372 pass (tests/test_paths.py: 63), including the V4.8.2 review's 31 December case and the Monday-to-Monday data gap.
- The review's reproduce.py (adapted only for the new attribute and the 5-field slot tuple): value at 1 January 3,500 and 6,500 as expected;
  weekend flags for the Monday-to-Monday gap [False, False].
- Parity with V4.7.3 on the same synthetic paths file: 12 groups, 14,250 shared rule pairs, 247,095,000 values identical.
- Synthetic dry run: grid incl. layered policies 183 s on 2 workers, peak RSS 1.8 GB.
- Not yet run on real data.

## V4.8.5 synthetic dry run (/home/claude/dry47, offsets -200/0/+200, 10 lots)
960 paths, 4 not filled; no partial entries; 10 reduced entries (4 to 30-39%, 6 to 60-69%); 185 entry attempts failed for liquidity (173 limited by the long leg, 12 by the short; 184 of them roll reopens);
roll reopens that could not fill the full size ended the path; zone_coverage.py classified the 3 gaps (expiry not listed on the fill day).
The grid ran end to end on the new paths file (no compound policies).
