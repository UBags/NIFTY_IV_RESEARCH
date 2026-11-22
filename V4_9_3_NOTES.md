# V4.9.3 notes (2026-11-12)

From ChatGPT's V4.9.2 review + integrated research plan. Only items judged real improvements were implemented. Table in CHANGES_V4.md.

## Implemented
- Combined open-lot cap (--max-open-lots 10 reference lots of 65 units, both sides, intraday included) in training and account.
- Executable sizing at the source: zone_paths.py --target-units 650 -> round(650/lot) exchange lots per entry (13 / 26 / 9 / 10).
  The old 650-unit valuation had claimed 13-26 lots never simulated in 2021-24.
- Same-day trades now hold margin/lots; account tracks margin per bar (daily intraday peak).
- Order admission at the signal bar's close (no look-ahead).
- Buffer use reported apart from funds short; CAGR beside simple return; full leap year factor 1.
- OOS verdict PASS / FAIL / INSUFFICIENT EVIDENCE; portfolio_freeze.json + zone_holdout.py (one-shot, refuses changed data/code).
- --rank dispersion option; SHA-256 trade identity for dedup; API refuses training split inside the holdout.
- zone_coverage.py --tenor-check: H1 / H2 monthly-expiry coverage from the source listing.

## Not implemented (why)
- Portfolio-aware pools: no defined criterion; pool width sensitivity = rerun with --pool.
- SPAN margins, fill lag, stale-mark valuation, bootstrap/SPA: need broker data or a defined method.
- Time-blocked combo_eval: measured on 0.1 s workloads; not a bottleneck.
- Five-strike / monthly-tenor study: deferred until --tenor-check shows coverage (H2 needs >30-day listings that 2021-24 often lacks;
  H1 is only possible in roughly the first week of a month). Current engine applies the same signed offset to both sides, so
  "OTM on both sides" needs a side-aware offset (bull -d, bear +d) if the study goes ahead; strikes to +/-1000 need re-extraction.

## Decisions for the user
1. 22% basis: simple yield on 25 L (default gate) or CAGR (--return-basis cagr).
2. Lot cap unit: reference lots of 65 units, calendar package = 1 (current); or gross contracts.
3. Buffer-use action: currently reported only (no forced de-risking).
4. Gap policy busy (default) vs free.

## Run order
zone_paths.py --target-units 650 (rebuild) -> zone_grid.py -> zone_portfolio.py -> (after freezing everything) zone_holdout.py once.
