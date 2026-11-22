# V4.9.4 validation record

- Unit tests: 439 pass (V4.9.3's 410 + PortfolioV494: same-day realised loss / gain / open mark in funds, lot-cap units at lots
  25 / 50 / 65 / 75, CAGR-basis selection, elapsed-period overall return (17% + 13.5% over 1.5 years = 20.33% -> FAIL), one
  judged year insufficient unless pre-registered, holdout code in the digest, coverage problems; holdout horizon frozen and
  coverage refusal). With ZONE_NO_NUMBA=1: tests/test_paths.py 130 pass.
- Synthetic dry run (/home/claude/dry493, grid unchanged): portfolio and holdout end to end; walk-forward overall -2.1% a year over
  3.00 years (FAIL on noise); holdout overall -3.5% a year over 1.50 years where V4.9.3 had printed 5.4% (mean of yearly figures).
- Not yet run on real data.
