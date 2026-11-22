# V4.5.3 validation record

**Assumptions, not measurements:** 10% bar participation, Rs 20 exercise fee, slippage model.

**Not done:** any run on your database, real NIFTY history, official settlement reconciliation, broker margin or bid/ask. No profitability claim is supported.

| Check | Result |
|---|---|
| Unit tests | 244 pass, including the acceptance tests from all review rounds |
| Synthetic smoke | Pass: 86 trades, 3,168 marks, 1,404 fills |
| Live pipeline | Not re-run for V4.5.3; cache SQL unchanged since the V4.4.5 run in `docs/live_postgres_run.txt` |
| Crash recovery | `kill -9` mid-extract and during finalization on live PostgreSQL, then `--resume`: all tables identical to a clean build |
| Lineage | Publish refused after a one-mark change; repeat publish skipped; changed resume settings refused |
| Simulator parity | 11,214 trades, marks and diagnostics calendar paths identical to V4.5.2 and to `--workers 1` (`CHANGES_V4.md`) |
| Lookahead | Ledger prefixes unchanged after corrupting all later data, 12 policies, 2 hedges, 7 cut times; signals unchanged with all later data deleted; `next_bar_open` also checked at every cached minute boundary, with the current bar's close and volume corrupted |
| Reviewer repro | `reproduce_v44_findings.py` fails on V4.4; with assertions inverted it passes on V4.4.1 (hedge change 0.0, entries 0) |
| Content digest | Clean build and `kill -9` + `--resume` give the same digest on live PostgreSQL |
| IV solver | 0 fallbacks on 200k quotes and tick-rounded wings; every price reconstructed |
| SPA size | Bootstrap-t: 5-21% at nominal 5% for block 10, 3-10% for block 20 (`CHANGES_V4.md`). Discount-only |

**Methodology limits:** OHLC minute bars are not executable quotes; capital is a scenario, not SPAN; expiry TWAP is a proxy unless a verified CSV row exists; the index-delta hedge is not tradable; IV uses calendar time.

Acceptance before any market conclusion: per-vendor coverage, nested portfolio and untouched holdout on real data, with slippage, delay and capital stress.
