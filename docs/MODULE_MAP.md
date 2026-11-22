# Module map: flat scripts (≤ V4.9.11) → `nifty_iv` package

The guides and release notes in `docs/` were written for the flat layout, where every tool was run as `python <script>.py`.
Each script now lives in the `nifty_iv` package and runs as a console script or as `python -m <module>`; arguments are unchanged.

```bash
python zone_grid.py --paths p.sqlite --out g          # before
nifty-zone-grid      --paths p.sqlite --out g          # now (after `pip install -e .`)
python -m nifty_iv.zone.grid --paths p.sqlite --out g  # equivalent
```

| Old script | New file | Module | Command |
|---|---|---|---|
| `strategy_spec.py` | `src/nifty_iv/core/strategy_spec.py` | `nifty_iv.core.strategy_spec` | — |
| `contract_specs_v2.py` | `src/nifty_iv/core/contract_specs.py` | `nifty_iv.core.contract_specs` | — |
| `lineage.py` | `src/nifty_iv/core/lineage.py` | `nifty_iv.core.lineage` | — |
| `import_nifty_postgres.py` | `src/nifty_iv/data/import_postgres.py` | `nifty_iv.data.import_postgres` | `nifty-data-import-postgres` |
| `inspect_nifty_parquet.py` | `src/nifty_iv/data/inspect_parquet.py` | `nifty_iv.data.inspect_parquet` | `nifty-data-inspect-parquet` |
| `prepare_research_cache.py` | `src/nifty_iv/data/research_cache.py` | `nifty_iv.data.research_cache` | `nifty-data-research-cache` |
| `migrate_research.py` | `src/nifty_iv/data/migrate.py` | `nifty_iv.data.migrate` | `nifty-data-migrate` |
| `explain_month.py` | `src/nifty_iv/data/explain_month.py` | `nifty_iv.data.explain_month` | `nifty-data-explain-month` |
| `zone_spot.py` | `src/nifty_iv/zone/spot.py` | `nifty_iv.zone.spot` | `nifty-zone-spot` |
| `zone_signals.py` | `src/nifty_iv/zone/signals.py` | `nifty_iv.zone.signals` | `nifty-zone-signals` |
| `zone_contracts.py` | `src/nifty_iv/zone/contracts.py` | `nifty_iv.zone.contracts` | `nifty-zone-contracts` |
| `zone_backtest.py` | `src/nifty_iv/zone/backtest.py` | `nifty_iv.zone.backtest` | `nifty-zone-backtest` |
| `zone_paths.py` | `src/nifty_iv/zone/paths.py` | `nifty_iv.zone.paths` | `nifty-zone-paths` |
| `zone_coverage.py` | `src/nifty_iv/zone/coverage.py` | `nifty_iv.zone.coverage` | `nifty-zone-coverage` |
| `zone_rules.py` | `src/nifty_iv/zone/rules.py` | `nifty_iv.zone.rules` | — |
| `zone_grid.py` | `src/nifty_iv/zone/grid.py` | `nifty_iv.zone.grid` | `nifty-zone-grid` |
| `zone_portfolio.py` | `src/nifty_iv/zone/portfolio.py` | `nifty_iv.zone.portfolio` | `nifty-zone-portfolio` |
| `zone_holdout.py` | `src/nifty_iv/zone/holdout.py` | `nifty_iv.zone.holdout` | `nifty-zone-holdout` |
| `zone_futures.py` | `src/nifty_iv/zone/legacy/futures.py` | `nifty_iv.zone.legacy.futures` | `nifty-zone-legacy-futures` |
| `zone_report.py` | `src/nifty_iv/zone/legacy/report.py` | `nifty_iv.zone.legacy.report` | `nifty-zone-legacy-report` |
| `zone_summary.py` | `src/nifty_iv/zone/legacy/summary.py` | `nifty_iv.zone.legacy.summary` | `nifty-zone-legacy-summary` |
| `strategy_scan.py` | `src/nifty_iv/diagnostics/strategy_scan.py` | `nifty_iv.diagnostics.strategy_scan` | `nifty-diag-strategy-scan` |
| `dd_cutoffs.py` | `src/nifty_iv/diagnostics/dd_cutoffs.py` | `nifty_iv.diagnostics.dd_cutoffs` | `nifty-diag-dd-cutoffs` |
| `dd_bins.py` | `src/nifty_iv/diagnostics/dd_bins.py` | `nifty_iv.diagnostics.dd_bins` | `nifty-diag-dd-bins` |
| `dd_recovery.py` | `src/nifty_iv/diagnostics/dd_recovery.py` | `nifty_iv.diagnostics.dd_recovery` | `nifty-diag-dd-recovery` |
| `zone_diagnose.py` | `src/nifty_iv/diagnostics/zone_diagnose.py` | `nifty_iv.diagnostics.zone_diagnose` | `nifty-diag-zone-diagnose` |
| `pair_exits.py` | `src/nifty_iv/diagnostics/pair_exits.py` | `nifty_iv.diagnostics.pair_exits` | `nifty-diag-pair-exits` |
| `quant_v2.py` | `src/nifty_iv/ivcal/quant_v2.py` | `nifty_iv.ivcal.quant_v2` | — |
| `quant_v3.py` | `src/nifty_iv/ivcal/quant_v3.py` | `nifty_iv.ivcal.quant_v3` | — |
| `quant_fast_v3.py` | `src/nifty_iv/ivcal/quant_fast_v3.py` | `nifty_iv.ivcal.quant_fast_v3` | — |
| `candidate_core.py` | `src/nifty_iv/ivcal/candidate_core.py` | `nifty_iv.ivcal.candidate_core` | — |
| `settlements_v3.py` | `src/nifty_iv/ivcal/settlements.py` | `nifty_iv.ivcal.settlements` | — |
| `stats_v4.py` | `src/nifty_iv/ivcal/stats.py` | `nifty_iv.ivcal.stats` | — |
| `calendar_research.py` | `src/nifty_iv/ivcal/calendar_research.py` | `nifty_iv.ivcal.calendar_research` | `nifty-iv-calendar-research` |
| `enrich_cache.py` | `src/nifty_iv/ivcal/enrich_cache.py` | `nifty_iv.ivcal.enrich_cache` | `nifty-iv-enrich-cache` |
| `rank_strategies.py` | `src/nifty_iv/ivcal/rank_strategies.py` | `nifty_iv.ivcal.rank_strategies` | `nifty-iv-rank-strategies` |
| `publish_results.py` | `src/nifty_iv/ivcal/publish_results.py` | `nifty_iv.ivcal.publish_results` | `nifty-iv-publish-results` |
| `audit_quality.py` | `src/nifty_iv/ivcal/audit_quality.py` | `nifty_iv.ivcal.audit_quality` | `nifty-iv-audit-quality` |
| `audit_contract_coverage.py` | `src/nifty_iv/ivcal/audit_contract_coverage.py` | `nifty_iv.ivcal.audit_contract_coverage` | `nifty-iv-audit-contract-coverage` |
| `audit_spot_sources.py` | `src/nifty_iv/ivcal/audit_spot_sources.py` | `nifty_iv.ivcal.audit_spot_sources` | `nifty-iv-audit-spot-sources` |
| `calibrate_slippage.py` | `src/nifty_iv/ivcal/calibrate_slippage.py` | `nifty_iv.ivcal.calibrate_slippage` | `nifty-iv-calibrate-slippage` |
| `calibrate_spa.py` | `src/nifty_iv/ivcal/calibrate_spa.py` | `nifty_iv.ivcal.calibrate_spa` | `nifty-iv-calibrate-spa` |
| `power_analysis.py` | `src/nifty_iv/ivcal/power_analysis.py` | `nifty_iv.ivcal.power_analysis` | `nifty-iv-power-analysis` |
| `compare_fill_models.py` | `src/nifty_iv/ivcal/compare_fill_models.py` | `nifty_iv.ivcal.compare_fill_models` | `nifty-iv-compare-fill-models` |
| `compare_structures.py` | `src/nifty_iv/ivcal/compare_structures.py` | `nifty_iv.ivcal.compare_structures` | `nifty-iv-compare-structures` |
| `exit_lag_report.py` | `src/nifty_iv/ivcal/exit_lag_report.py` | `nifty_iv.ivcal.exit_lag_report` | `nifty-iv-exit-lag-report` |
| `campaign_report.py` | `src/nifty_iv/ivcal/campaign_report.py` | `nifty_iv.ivcal.campaign_report` | `nifty-iv-campaign-report` |
| `benchmark_v3.py` | `src/nifty_iv/ivcal/benchmark_v3.py` | `nifty_iv.ivcal.benchmark_v3` | `nifty-iv-benchmark-v3` |
| `smoke_v3.py` | `scripts/smoke_v3.py` | — | `python scripts/smoke_v3.py` |
| `VERSION.txt` | `src/nifty_iv/VERSION.txt` | | |
| `migrations/` | `src/nifty_iv/data/migrations/` | | |
| `nse_holidays.txt` | `config/nse_holidays.txt` | | |
| `INDEX_v15_zone_swing.pine`, `PineScript/` | `pine/INDEX_v15_zone_swing.pine` | | |
| `check_import_psql.sh`, `cleanup_outputs.sh` | `scripts/` | | |
| `V4_9_6_SCREEN_PROBE.py`, `legacy_v1/`, `archive/*` | `archive/scripts/`, `archive/legacy_v1/`, `archive/v2_tools/` | | |
| `README_V2.md`, `README_V4.md`, `NIFTY_*GUIDE*.md` | `docs/guides/` | | |
| `V4_*_NOTES.md` | `docs/notes/` | | |
| `VALIDATION_*.md` | `docs/validation/` | | |
| `REVIEW_V4_5.md`, `PILOT_RESULTS_V4_5_4.md`, `GUIDEBOOK_RESEARCH_NOTES.md`, `V4_9_6_REAL_DATA_READINESS_REVIEW.md` | `docs/research/` | | |

Removed from the tree (still in git history): release zips `NIFTY_IV_RESEARCH_V*.zip`, `NIFTY_Research_Kit_Redownload.zip`,
`fly_.zip`, `zone_results.zip`; `nifty_inspection_all/` (an old copy of the code); run outputs (`*.txt` logs and summaries,
`zone_signals.json`, `slippage_calibration/`); `SHA256SUMS.txt` (it covered the old flat files); `.idea/`; a dangling
`NIFTY_Data/india-index-options-1m` git link.

## Code digests changed

Moving the files changed the bytes and locations that three digests cover, so outputs built before the move are refused
by design:

| Digest | Covers | Effect |
|---|---|---|
| `zone.grid.algorithm_digest` | `zone/rules.py`, `core/strategy_spec.py` bytes + kernel sources | rerun `nifty-zone-grid` on the existing paths file |
| `zone.portfolio.code_digest` | portfolio, grid, rules, paths, holdout, contract_specs, signals, calendar_research, strategy_spec | rerun `nifty-zone-portfolio`; an old `portfolio_freeze.json` is refused by `nifty-zone-holdout` |
| `core.lineage.code_digest` | every file of the package, by package-relative path | IV-pipeline rankings must be rebuilt before `publish_results` |

Path files (`zone_paths*.sqlite`), option extractions and spot caches are data, not code, and stay valid.
Numerical results are unchanged: a synthetic end-to-end run (paths → coverage → grid → portfolio → scans) produced
identical numbers before and after the move.
