# V4.8.2 validation record

- Unit tests: 366 pass (tests/test_paths.py: 57, including ReviewV481: dated cash, as-of state at any window end, unpriceable remainder,
  indicator code in the digest, estimator, calendar weekends, year-end value unchanged when every quote from 1 January changes).
- Review probes (NIFTY_V481_AUDIT_PROBES.py) against V4.8.2: A records ((1, 500.0), (2, 5000.0)) (the probe then stops on the old return format);
  B writes arrays removed in V4.8.2 (replaced by the slots test); C: digest changes when ATR changes; D: NaN; E: estimator kept and documented.
- Parity with V4.7.3 on the same synthetic paths file: 12 groups, 14,250 shared rule pairs, every window, 247,095,000 values identical.
- Synthetic dry run: paths rebuilt; grid incl. layered policies 180 s on 2 workers, peak RSS 1.8 GB.
- Not yet run on real data.
