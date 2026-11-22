# V4.9.5 notes (2026-11-12)

From ChatGPT's V4.9.4 review (5 findings, all accepted). Table in CHANGES_V4.md.

- Executable qualification: screen passes are replayed through the chronological account (same admission rules as the
  walk-forward) in rank order; the pick is the first that passes there (--qualify-top 200; count reported). Freeze needs it.
- Holdout completeness: every expected session (weekdays minus frozen holiday list) and per-session bar counts checked;
  no holiday list -> refuses unless --weak-coverage-check (labelled). Grid / freeze must be built with --holidays for an exact check.
- Main account exposure no longer overwritten by top-K replays (my bug).
- CAGR basis now also in neighbour robustness.
- Full year ending on a Friday annualised over 365 days (horizon_end).
- Perf: carried marks precomputed for admission.

Decisions still open (frozen in portfolio_freeze.json): --return-basis, --lot-cap-unit, buffer-use action, --gap-policy,
--holdout-end, --holdout-min-full-years. Build the grid with --holidays nse_holidays.txt.
Strike / tenor study: deferred until zone_coverage.py --tenor-check runs on the real options file.
