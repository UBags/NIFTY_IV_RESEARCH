# NIFTY IV Research V2 — Validation and Remaining Gates

Date: 2026-11-08

## Results that passed in the current development environment

| Test | Status | Evidence |
|---|---|---|
| V1 regression tests | PASS (3/3) | `cd legacy_v1 && python -m unittest test_research -v` |
| V2 unit tests | PASS (7/7) | `python -m unittest discover -s tests -v` |
| V2 Python syntax compilation | PASS | `python -m py_compile *.py` |
| Research migration dry run | PASS | `python migrate_research.py --dry-run` lists one additive migration |
| Synthetic enrichment | PASS | 3,120 expiry/time groups, 18,720 cached Greeks on test fixture |
| Synthetic calendar simulation | PASS | 25 signals; 200 completed policy/hedge variants; 4 candidate-grid exclusions |
| Synthetic forward/IV ranking | PASS | 1,632 cheap grid screens; 100 MTM screens; produced `ranking.csv` and `ranking.json` |
| Rolling trade cash ledger | PASS | Example roll had 24 option fills; summed cash flows reconciled to P&L exactly (within output precision) |
| Standalone vector IV numerical test | PASS | 2,000 synthetic option prices, maximum vector-versus-Brent difference about 2.51e-12 |
| Standalone IV runtime in this environment | OBSERVED | 4.53x faster than scalar for the specific 2,000 synthetic prices; **not** 80–150x universally |
| Historical NSE data / live PostgreSQL integration | NOT RUN | The historical parquet files and user's live PostgreSQL are not available in this environment |
| User import table mutations | NONE | No script executed against user's PostgreSQL. Source `market_data.*` read-only in the kit. |

Synthetic examples assume simplified constant underlying levels and are **not real historical NIFTY returns**.

## Outstanding accuracy and acceptance gates

1. Validate all PostgreSQL read-only SELECT queries against imported, partitioned `market_data` with `EXPLAIN (ANALYZE, BUFFERS)` only after the current import finishes.
2. Run NIFTY spot/option coverage audits by provider and date. Measure how many trade candidates are excluded for parity, volume, lot mismatch and missing daily exit marks.
3. Verify exact contract-master lot sizes across 2021, 2024 and 2025–26 revisions; supply authoritative overrides where rule-derived sizing is ambiguous.
4. Reconstruct option forward levels against independent NIFTY futures quotes; validate minute OHLC parity noise.
5. Run all same-strike rolling cash paths against actual observation days; check incomplete exits and avoid survivorship bias.
6. Independently verify margin/capital required, bid-ask execution, and official cash settlement procedures before calculating tradable return on capital.
7. Add repeated walk-forward training/validation (not just one final holdout), macro-event calendars, bootstrap confidence intervals and slippage stress tests.
8. Measure actual runtime of PostgreSQL extraction, enrichment, simulation and ranking on realistic data; additional SQLite layout and single-pass policy optimizations depend on measured bottlenecks.
9. Gamma hedges currently reduce gamma with *integer* calls; exact gamma-neutral options construction and tradable futures-based delta hedging require additional instrument data.

No strategy rankings in this project should be treated as actionable investment advice or evidence of consistent profit until the above gates are satisfied.
