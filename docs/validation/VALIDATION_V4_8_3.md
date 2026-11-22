# V4.8.3 validation record

- Unit tests: 367 pass (tests/test_paths.py: 58), including the V4.8.2 review's 31 December case and the Monday-to-Monday data gap.
- The review's reproduce.py (adapted only for the new attribute and the 5-field slot tuple): value at 1 January 3,500 and 6,500 as expected;
  weekend flags for the Monday-to-Monday gap [False, False].
- Parity with V4.7.3 on the same synthetic paths file: 12 groups, 14,250 shared rule pairs, 247,095,000 values identical.
- Synthetic dry run: grid incl. layered policies 183 s on 2 workers, peak RSS 1.8 GB.
- Not yet run on real data.
