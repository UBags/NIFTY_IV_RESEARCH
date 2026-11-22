# V4.8.0 validation record

- Unit tests: 349 pass (tests/test_paths.py: 40, including the review regressions,
  hand-worked cases for the new stop families and the stability score).
- Parity with V4.7.3 on the same synthetic paths file: 14,250 shared (profit, stop) pairs over 12 groups, 247,095,000 stored values, all identical.
- Review probes (NIFTY_V472_TARGETED_PROBES.py): probe 2 now fails as intended (the corrupted file is recomputed). Probe 1's case has the zone flip on
  the same bar as the exit's completion, so ending there is correct under the bar convention; the defective case (flip before completion) is
  `ReviewV472.test_zone_flip_during_pending_roll_waits_for_the_last_fill`, which V4.7.2 fails (end_bar 71 vs last fill bar 72).
- Synthetic dry run (45 sessions, 1,440 paths): grid 45 s compute + ~110 s report with 2 workers, peak RSS ~1.8 GB, 1.1 GB of output.
- Not yet run on real data.
