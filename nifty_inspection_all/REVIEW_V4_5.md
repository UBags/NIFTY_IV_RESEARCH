# V4.4.5 deep review and V4.5.0 changes

> V4.5.1 corrected the iron fly, T-1, path identity, holdout calendar and sealing items. See the top of `CHANGES_V4.md`.

**Status:** research software. Every number below comes from code run on a synthetic chain (477 sessions, 3.8M option rows, stochastic vol, gaps, a short-dated variance premium, skew, patchy far-tenor volume, the Thursday-to-Tuesday expiry move and the lot eras). The chain was written for this review. **It says nothing about whether any strategy makes money on NIFTY.** No real-data run exists.

## 1. Findings

| # | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | High | **Training and fold samples were filtered on the realized exit date.** A trade entered just before a cutoff stayed only if it exited early. Stop and target exits are outcome-dependent, so the boundary kept quick resolutions and dropped slow ones. | Entries whose short expiry fell after a cutoff, in points per unit: `p50_s100` kept +30.16 (n 272), dropped -8.12 (n 835). `p25_s50` kept +10.46, dropped -9.20. | Fixed. `scheduled_exit_day` is fixed at entry. `--train-censor realized` restores the old rule. |
| 2 | High | **The holdout was cut by the longest tenor of any strategy.** With `--split 2026-01-01`, data to 30 June and a 120-day tenor, weekly strategies could enter only until 2 March. Sharpe was then computed over all 129 sessions, including the flat tail. | 43 of 129 sessions were usable. Median top-25 holdout trades rose from 4 to 9 under the new rule. | Fixed. Default `--holdout-entry-cutoff per_trade`, with the Sharpe window ending at the last planned exit. |
| 3 | High | **The 1-2 / 3-5 session buckets change meaning at the 2025-09-01 expiry move.** Before: bucket `1-2` was Wednesday to Thursday, one session, no weekend. After: Friday to Tuesday, two sessions, a weekend. Training mixes both. The holdout is all post-move. | Ledger counts confirm it. The old top-ranked result had zero holdout trades. | Fixed. Default `--bucket-scheme exact_sessions`. Each result reports `expiry_regime_mix`. Only 4-session entries (pre Friday, post Wednesday) exist in both regimes at scale. |
| 4 | Medium | **Duplicates across policies were counted as separate trials.** V4.4.5 removed aliases inside a policy only. `economic_id` includes the policy, so identical fills under `weekly`, `p25_s50` and `carry30` counted separately. | 11,214 trade rows, 9,457 economic ids, 3,699 distinct paths. Effective trials on one ranking fell from 27,787 to 16,061. | Fixed. `path_id` drives trials, SPA and the shortlist. |
| 5 | High, process | **The sample cannot support the search.** A 5% one-sided test on a 109-session holdout needs a Sharpe of about 2.5. A true Sharpe of 1.5 is detected about 25% of the time. | `power_analysis.py`. It assumes independent days, so real power is lower. | Open. Needs more history. Check whether your vendors go back to 2021 (the lot rules already do), then extend. |
| 6 | Medium | **Re-entering every week pays the far leg's costs every week.** | 90-day put calendar under the current cost model: `weekly` 9.67 points per short cycle, `rolling_static` 3.13. | Reported. The slippage model is unverified (see 7). |
| 7 | Medium | **Far-leg slippage is an assumption.** It scales with sqrt(tenor/7), about 1.8 points at 90 days. Far-dated NIFTY spreads may be wider. A 09:45 print on the far leg is also required, which may remove most days. | Not measurable on synthetic data. | Tool added: `calibrate_slippage.py` (smile residual and bar range by tenor). Run it on your cache. |
| 8 | Medium | **Expiry-day margin is not modelled.** The calendar-spread benefit is gone on expiry day, and short index options carry a further 2% ELM. | NSE/SEBI rule, not a code test. | Partly addressed by the T-1 exits below. SPAN is still open. |
| 9 | Low | Every order waits for a fixed 09:45 signal and stops check three times a day. Gaps between checkpoints are not observed. | Design limit of a 16-minute cache. | Open. |

**Read and found consistent (not re-sourced in this review):** dated STT, stamp, exchange and SEBI charges with GST; exercise STT on the buyer only; delta-to-strike algebra; `next_bar_open` causality; no-lookahead fields in `trend_fields` and RV; Yang-Zhang; lot-rule transition logic.

**Tested and rejected:** I expected t-stat or lower-confidence-bound selection, and a higher minimum history, to beat raw Sharpe in nested selection. A stylised simulation showed the opposite: at 20% edge share, raw Sharpe with a 5-trade minimum earned 22.1 at 0.86 risk-adjusted, against 17.5 at 0.67 for the t-stat rule with a 20-trade minimum. A top-decile equal-weight ensemble was steadier: 1.16 against 0.86 risk-adjusted at 20% edge share, and better in 3 of 4 settings. So no new selection score was added, and `unfiltered_family_books` is reported instead.

## 2. New in V4.5.0

**Exits and structures (all opt-in):**
- `tm1`: flat at the close of the last session before the short expires.
- `p25_s50_tm1`: price exits, plus flat by T-1.
- `rolling_tm1`: at T-1, buy the expiring short and sell next week's in one minute. Falls back to the ordinary roll.
- `iron_fly`: same-expiry defined-risk short volatility. Wing distance `--fly-wing-z` (default 1.5 expected moves). It is the baseline a calendar has to beat, since it holds no long-dated leg.

**Filters and sizing:**
- `--event-calendar FILE --event-policy skip_event_weeks|only_event_weeks`: dated events only (RBI policy, Budget, election results), which you supply as `event_date,first_known_on`.
- `--score-basis risk_normalized_score`: finished trades reweighted to one expected short-leg move. A counterfactual score, not executed sizing.

**Reports:** `unfiltered_family_books` (equal-weight every strike and tenor, no filter, no selection), `expiry_regime_mix`.

**Performance (same machine, synthetic):**

| Step | V4.4.5 | V4.5.0 |
|---|---|---|
| Simulator, the 11.2k-trade grid, 1 worker | 19.5 s | 20.7 s |
| Same, `--workers 2` (2-core sandbox) | n/a | 11.8 s |
| Ranker, `--signals all`, 27.8k masks | 58.9 s | about 42 s |

Lot-rule memoization, one equity series per mask, and no per-fold array copies account for the ranker gain. Parallel ranking was not added: under a minute does not justify it.

## 3. Parity

- 11,214 trades, all marks and diagnostics identical to V4.4.5 on the same flags. `--workers 2` also matches `--workers 1` on trades, marks, fills and diagnostics.
- Ranking under `--train-censor realized --holdout-entry-cutoff global_longest --bucket-scheme legacy`: results present in both agree to 7e-15, and nested selections and portfolio Sharpe are equal. The shortlists differ only because `path_id` now collapses duplicates (15 of 25 overlap).

## 4. Predeclared experiments

Fix these before running. Use weekly entries and the same fill model throughout. Reject on nested walk-forward or adverse fills; do not retune on the holdout.

1. **T-1 against expiry.** `tm1` against `weekly`, paired by day and strike, net of cost.
2. **Fly against calendar.** `iron_fly` with `tm1` against put calendars with `tm1`. If calendars lose, the far leg costs more than it protects.
3. **Rolling against re-entry.** `rolling_tm1` and `rolling_static` at 60-90 days, against `weekly`. Per-cycle cost is the claim being tested.
4. **Event gate.** `skip_event_weeks` against `all`. No threshold to fit.
5. **Sizing.** `risk_normalized_score` against `per_unit`, as a diagnostic, with the same trades.
6. **Ensemble.** Any single selected rule against its family ensemble. A rule that cannot beat its own family is noise.

Variance risk pricing is reported to fall with maturity (Andries et al., Review of Finance, 2025, on S&P data), which is the reason to expect (2) and (3) to matter. Whether it holds on NIFTY after costs is what these runs decide.

## 5. Not done
Real-data run. SPAN margin. Live PostgreSQL re-run (the cache SQL is unchanged). Contract master. Futures-based hedges. A weekly-expiry ban is under discussion at SEBI, which would remove the short leg's instrument.
