# V4.9.5 validation record

- Unit tests: 465 pass (V4.9.4's 439 + PortfolioV495 and the holdout test's coverage cases: executable qualification cuts a
  full-size pass to a fail; main exposure invariant to top-K alternatives; full 2023 ending Friday 29 Dec = 22.0% over 365 days;
  half the weekdays missing, one missing session, a thin session all caught; holiday not counted missing). With ZONE_NO_NUMBA=1:
  tests/test_paths.py 156 pass.
- Synthetic dry run (/home/claude/dry493, grid unchanged): portfolio 24 s with qualification (29 / 50 / 50 / 9 replayed, all
  executable on this data); stress run (margin 20%, 10-lot legs) replays end to end. Holdout refuses without a holiday list;
  with --weak-coverage-check it runs, labelled weak.
- Not yet run on real data.
