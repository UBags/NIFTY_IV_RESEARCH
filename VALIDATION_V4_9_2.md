# V4.9.2 validation record

- Unit tests: 397 pass (V4.9.1's 389 with its portfolio tests replaced by 16 V4.9.2 tests covering review acceptance cases 1-8 and
  T01/T03/T04/T06/T09, plus gap tests). With ZONE_NO_NUMBA=1: tests/test_paths.py 88 pass.
- Reproductions of the V4.9.1 review now fixed: overlapping 23.4 L positions -> second order rejected (peak margin <= 25 L; at 13 L
  each the second is cut to 9 lots); open loss 3 L + margin 24 L fails the funds gate; decoy test passes with every survivor
  evaluated; 4-of-10 matched path gives 1, 2, 4, 4, 4 lots at 1, 2, 4, 5, 10; numba = numpy with +/-inf, NaN, 1e150 and gaps.
- Synthetic dry run (/home/claude/dry491, futures paths 2021-08 to 2024-12): grid rebuilt (gap policy busy); numba and numpy grids
  identical (108,654,000 values); portfolio 16 s, results identical to V4.9.1 where nothing binds (full fills, funds ample).
  Stress run (futures margin 20%, legs 0 or 10 lots): 17 of 29 orders cut to 9 lots as losses reduced funds; funds never short;
  training had passed the 10-lot legs, so the account's admission check is what bound.
- Not yet run on real data. The grid must be rebuilt (algorithm digest: kernel finite check, gap policy).
