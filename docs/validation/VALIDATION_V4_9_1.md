# V4.9.1 validation record

- Unit tests: 389 pass (379 earlier + 10 new: PortfolioV491 T01, T03/T04, T06, T09, sizing, margin and overlap guard, sizes search
  against brute force; GridV491 T05 flat adoption, T08 modified grid refused; numba / numpy sequencing parity). With ZONE_NO_NUMBA=1:
  tests/test_paths.py 79 pass, 1 skipped (the parity test).
- Synthetic multi-year dry run (/home/claude/dry491): spot random walk 2021-08 to 2024-12 with 60-session up / down / flat regimes,
  real zone signals (65 of 1,445 configurations kept), futures_zone and futures_expiry paths at 10 lots (units 500 / 250 / 750 as the
  lot moved), grid with layered policies (104 files, 24 s, 2 workers), portfolio end to end (about 50 s).
- Grid parity on that run: numba and numpy builds identical, 108,654,000 values in 104 files.
- Portfolio on pure noise (expected: nothing real to find): in-sample best passes every gate (22.8% overall, worst year 19.0%,
  drawdown Rs 2.22 L at 2 + 4 lots), walk-forward account -2.5%, -5.1%, +0.4% (top-10 medians -4.8%, -5.8%, -4.2%), continuous
  drawdown Rs 3.13 L vs largest in-year Rs 2.40 L. The winner's neighbour pass share 0.125 was entirely identical neighbours (distinct: 0).
- Not yet run on real data. The grid must be rebuilt (algorithm digest changed).
