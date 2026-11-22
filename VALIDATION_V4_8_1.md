# V4.8.1 validation record

- Unit tests: 359 pass (tests/test_paths.py: 50).
- Parity with V4.7.3 on the same synthetic paths file (rebuilt with V4.8.1): every bull group (the synthetic data has no bear signals), 14,250 shared
  (profit, stop) pairs, 247,095,000 values, identical. The new window-end valuation only differs when an exit straddles a window end with fills on both
  sides; none did in this set. Its own test matches the old event engine.
- Review probes (NIFTY_V480_Probes.py): probe 3 now fails as intended. Probe 1's stub lacks the new `buy` argument (the same five cases are in
  `ReviewV480.test_every_executed_quantity_is_recorded`). Probe 2 computes its own index arithmetic without calling the engine. Probe 4 inspects source
  text; `test_resume_and_report_bound_to_the_algorithm` is the behavioural test. Probe 5 confirms futures paths are gross spot-proxy (intended).
- Synthetic dry run (45 sessions): paths rebuilt; grid with layered policies 54 s compute + 116 s report on 2 workers, peak RSS 1.8 GB, 1.3 GB output.
- Not yet run on real data.
