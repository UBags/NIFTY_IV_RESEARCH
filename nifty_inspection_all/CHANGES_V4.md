# V4.8.3 (corrections after the review of V4.8.2)

| Finding | Decision | Change |
|---|---|---|
| V482-01 window end between two fill days used the earlier fill day's prices | Reproduced, fixed | The exit's state is stored at the close of every trading session from its first possible fill day to completion (8 sessions), fills or not: cash received so far and the open legs at that session's close. The review's case now gives 3,500 / 6,500 for a 31 Dec close of 60 / 120 |
| V482-02 weekend inferred from gaps in the data | Fixed | Exchange calendar: the last session of the week is the one after which every weekday to Saturday is a holiday. `zone_grid.py --holidays FILE` (any common date format; stored in the grid index and the algorithm digest). Without it only Fridays qualify; data gaps never do |
| V482-03 slot limit counted fill days, not sessions | Fixed | Rows store `exit_sessions` and `exit_fill_days`; counts `exit_simulations_over_several_sessions`, `exit_simulations_with_several_fill_days`, `exit_simulations_beyond_session_slots`, `exit_session_slot_unpriced` |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, 247,095,000 values, identical |

# V4.8.2 (corrections after the review of V4.8.1)

| Finding | Decision | Change |
|---|---|---|
| 481-01 exit proceeds spread over fill days by quantity | Reproduced (my V4.7.1 design), fixed | Every fill window is booked on its own with its own costs as a dated event; settlements and forced bookings too. The review's case now records Rs 500 on 29 Dec and Rs 5,000 on 2 Jan |
| 481-02 window end valued from the first exit day only | Reproduced, fixed | Each row stores the exit's state at the close of each of its first 6 days (actual cash received, legs still open at that close); a window end uses the last exit day before it. Exits longer than 6 days give NaN (unpriceable) for window ends past the 6th day and are counted |
| 481-03 missing residual price became zero | Fixed | NaN (unpriceable), counted as `exit_day_slot_unpriced` |
| 481-04 digest missed indicator code | Reproduced, fixed | `algorithm_digest` also hashes zone_signals atr/rma/ema/Bar/minute_of and the numpy version; robust to functions without source |
| 481-05 "25th percentile" | Kept by design, renamed | Conservative lower quartile: the floor((n-1)/4)-th smallest value (the minimum when n <= 4). Documented and tested |
| 481-10 weekend rule used the next observed date | Fixed | Calendar-based: the last session before a Saturday; a missing feed day is not a weekend; the data's last session counts only if it is a Friday |
| Diagnostics | Added | `exit_simulations_over_several_days`, `exit_simulations_beyond_day_slots`, `exit_day_slot_unpriced` in the paths counts |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, every window, identical (no exit in the synthetic set straddles a year end; those cases have their own tests, one against the old event engine) |

# V4.8.1 (corrections after the review of V4.8.0)

| Finding | Decision | Change |
|---|---|---|
| V480-01 partially executed quantities dropped | Reproduced, fixed | Every executed quantity is recorded per leg (complete or not) and the position is unwound with costs. Synthetic run: 92 of 98 partial calendar entries had a partly filled long that V4.8.0 discarded. Tests for 0/0, 50/50, 100/50, 50/100, 100/100 |
| V480-02 re-entry minute reused across days | Guarded | The engine always reopens on the bar whose minutes held the last exit fill, so the windows share a date; the start is now carried as (date, minute) and ignored on another date. Test |
| V480-03 resume/report bound only to the rule module | Fixed | `algorithm_digest` hashes the rule module and the source of the path reader/layout, sequencing, group computation and writer, plus `GRID_ALGORITHM`; `--resume` and report reading refuse a mismatch (report code may change freely). Test |
| V480-04 stability excluded edge settings | Fixed | Needs >= 1 valid neighbour and >= half of the existing ones; coverage reported; float64; `baselines.csv` keeps every zone-only none/none result. The review's example now scores tw2 900, tw6 600 |
| V480-05 futures vs buy-and-hold carry | Fixed | Both gross (spot proxy); net figures apply the same assumed carry to both (picks only while held, long pays, short receives). Note text corrected |
| V480-06 window end inside a stop's exit | Reproduced, fixed | Rows store the cash an exit decided there receives by the close of its first fill day and the open legs' value then; a window end after that day uses it. Matches the old event engine's boundary mark (test) where the natural path's mark differs |
| V480-07 layered exit policies | Added | 659-879 layered policies per structure (initial protection x management x mandatory exit), vectorised as the earliest of their parts; family `compound` (and `any_incl_compound`) |
| V480-08 execution proxy | Stress option | `zone_paths.py --fill-price adverse|open` |
| V480-09 unequal minimums | Made explicit | `--zone-min-trades`, `min_trades` column |
| Parity | | Same paths file: all 14,250 rule pairs shared with V4.7.3, 247,095,000 values, identical |

# V4.8.0 (V4.7.2 review, comprehensive stop families, stability ranking, further vectorisation)

| Item | Change |
|---|---|
| Review P0: zone change during a pending calendar roll exit | Reproduced. The path now stays open until the old pair's last fill, opens no new pair, and ends on that bar (V4.7.2 ended it on the flip bar while its exit completed a bar later; the lock was already correct). `end_fill_day` column. Test with the flip before completion, which V4.7.2 fails |
| Review: `--resume` trusted altered files | Each group file re-hashed against its sidecar; mismatch = recompute. Test |
| Review: `--report-only` could mix inputs | Report refuses if any file hash, the rule definitions or the paths file differ from the grid's lineage. Test |
| Review: partial calendar entries treated as never traded | The filled leg is now a position, sold back at the next bar close (`partial_entry_unwound`, counts `partial_entry_short_filled` / `long_filled`), with its cost and exposure |
| Roll re-entry ordering | A new pair fills only after the old pair's last exit minute (no shared minute volume). Old-engine parity kept behind `roll_entry_after_exit=False` in tests |
| Vendor ties | Deterministic (`ORDER BY provider`) in both engines |
| Stop families | 368 stop rules (naked), from 61: see README 4c and `zone_rules.py`. Profit rules 24 -> 50. Every V4.7 label kept |
| Bug found while adding them | Channel-break stop with zero cushion never fired while ATR was not ready (0 x inf); fixed and tested |
| Stability ranking | `stable` mode: lower quartile of the neighbourhood's training totals; walk-forwarded like `total`; `stable_candidates.csv`; strike +-100 totals for every pick |
| Vectorisation | Rules evaluated on padded paths x bars matrices for a whole group at once (sub-batched to bound memory); profit rules once per group; stops one family at a time; group-parallel grid and file-parallel report (`--workers`); fast zlib level 1 output; neighbourhood score via flat-index gathers. Profiled: compute 109 s -> 45 s and report 254 s -> ~110 s on the synthetic run (2 workers) |
| Parity | On the same paths file, every value of every rule shared with V4.7.3 (14,250 rule pairs, 247 million values, 12 groups) is identical |

# V4.7.3

| Change | Detail |
|---|---|
| `--min-trades` default 30 -> 15 (`zone_grid.py`, `zone_report.py`), at your request | Training pick needs 15 trades; walk-forward fold picks need max(10, 15//2) = 10; zone-only keeps 5 |

# V4.7.2 (fixes from the first real-data run)

| Issue | Change |
|---|---|
| `zone_paths.py` crashed on futures entries in June-July 2021: the lot-size rule returns no size during the 2021 transition (`transition_ambiguous`), and the futures path lacked the guard that option paths and `zone_futures.py` have | Rejected as `no_fill` / `lot_unknown`, counted as `reject_futures_lot_unknown`. Regression test; synthetic dry run from May 2021 completes |
| Unclosed files and connections (ResourceWarning in tests) | JSON written via temporary file + rename and read with context managers; `.npz` closed after loading; grid and detail close their SQLite connections; `zone_backtest.Market` gains `close()` / context manager and closes when collected |

# V4.7.1 (corrections after the review of V4.7.0)

| Finding | Decision | Change |
|---|---|---|
| P01 spilled calendar roll: rows between pairs held cash from fills that happened later | Reproduced, fixed | The exit simulation returns a fill schedule (cash known at each bar close, legs still open). Rows between pairs hold cash already received plus open legs at their last close; equals the old engine's boundary mark (test). Effect was on boundary values and mark-to-market only: stops used the final outcome, take-profit was off between pairs |
| (found while fixing P01) rows between pairs gave exit bar = that bar, so a stop there released the one-position lock before the roll exit finished | Fixed | Exit bar = when the roll exit completes |
| P02 futures exit with no next-bar print booked at the bar close without costs | Reproduced, fixed | Exit waits for the first later print; none before data end = open, valued at its mark |
| P03 float32 sums | Fixed | Totals and sums of squares stored float64; int16 counts checked for overflow |
| P04 trailing stop anchored at the signal close | Fixed | `trail` starts at the index at the entry fill (new `entry_index` column); `strail` keeps the V4.7.0 definition by name. Stops 46 -> 61 (futures 40 -> 55) |
| P06 one calendar leg filled, the other not | Kept (rule), measured | Counted as `entry_one_leg_filled_other_not`; the filled leg is still treated as never traded (optimistic, stated) |
| P07 per-trade evidence | Added | `zone_grid.py --explain` writes any combination's trades from the paths |
| P12 durability | Fixed | Paths built into `*.building` and renamed when complete; grid files written atomically with per-group checksums and `--resume` |
| P13 trade-sequence drawdown only | Added | `max_drawdown_mtm` on bar-close equity (closed trades plus the open trade's mark), worst open loss, median MAE/MFE |
| P14 profit and stop on the same bar | Labelled | Exit reason `both_rules_same_bar` (cash is the same either way) |
| P05, P08-P11, P15 | Agreed limitations, already documented | 45-minute close triggers, spot-proxy futures, no margin model, best-of-grid bias, vendor by volume |
| Tests | 336 pass | Regression tests for each reproduction, temporal prefix invariance (option prices and volumes changed from a cut-off day: every as-of field before it unchanged), old-engine boundary parity during a spilled roll |

# V4.7.0 (minute-sliced fills, path engine, exhaustive exit grid)

| Change | Detail |
|---|---|
| **Bug fix** forced long exit sign (`zone_backtest.py`) | V4.6.0-4.6.4 booked a calendar long force-closed at the lag cap as `-units x price` (a purchase) instead of `+units x price`. Every forced chain was understated by twice the long's value. In the supplied V4.6 trades file: 332 forced exits (320 bear call calendars, 12 bull put calendars), median 2,317 points per unit each. Bear calendar results in V4.6.x are biased down; naked options and futures unaffected |
| `zone_paths.py` (new) | Each (signal bar, side, structure, strike) simulated once to its natural end, bar by bar, all live paths stepped together. Stores per bar: cash, pair cash, base, printed value, mark, exit cash if decided there, exit bar/day, forced flags, roll flag |
| Minute-sliced fills | Each leg takes at most 10% of every minute's volume in whole lots at (O+H+L+C)/4 over the next bar's minutes, slippage and charges on the leg total. Entry must complete in its bar; exits carry over (shorts before longs). The old engine needed the whole order in one minute: e.g. 3 lots at 10% needed 30 lots in a single minute |
| Spilled roll exits | A roll whose long takes more than one bar completes first; the new pair opens on the completion bar if the zone holds (as `zone_backtest.py`) |
| Data end vs missing settlement | An exit that runs out of data leaves the path open (valued at its last mark); only a missing settlement price is `unresolved` |
| `zone_grid.py` (new) | Every signal configuration x side x structure x strike x 24 profit rules x 46 stop rules (futures 13 x 40), one position at a time, about 60 million combinations. Every combination's per-window results stored in `.npz`. Families by stop class (none, atr, trail, pct, loss, any) and configuration set (all, swing, zone-only); walk-forward by year; exact detail (weekly t, PF, drawdown, holding, exit reasons, by year) for every pick; buy-and-hold benchmark |
| Vectorisation (profiled, then changed) | Option minutes loaded per session straight from SQL into numpy (vendor de-duplication by sort); each leg's minute slicing is `cumsum` + `searchsorted`; exits memoised per (legs, bar); all rules of a kind evaluated as one broadcast; every signal configuration sequenced at once per signal bar (Python loop over ~1,800 bars instead of ~137,000 configuration-signal pairs); take-profit choice computed once per group and window. Outputs checked identical to the loop versions (paths to 1e-10 rupees; grid `.npz` and report equal) |
| `zone_contracts.py --strike-pad` | Extra calendar strikes around the planned ones, for spilled rolls that reopen at a neighbouring strike |
| Tests | `tests/test_paths.py` (20): path engine reproduces `zone_backtest.py` exactly for expiry, zone change, calendar roll chains, spilled rolls, short-before-long, lag cap and boundary marks; offline take-profit and ATR stop equal the old engine's; vectorised take-profit rule equals `zone_report.choose_tp` on 200 random cases; futures path equals `zone_futures.py` |

# V4.6.4 (corrections after the ChatGPT review of V4.6.3)

| Finding | Decision | Change |
|---|---|---|
| N463-01 position closed by a decision on the last bar of a year, filled next session, had no boundary value and was dropped from training | Reproduced, fixed | Both engines value open positions at the boundary before processing that bar's decisions. Supplied futures file: 12 rows (2 signals) were affected |
| N463-02 pre-entry phantom mark | Reproduced, fixed | Marks only for positions entered before the boundary |
| N463-03 incomplete 45-minute bars | Kept (your decision) | `Bar.minutes` recorded; `zone_signals.py` prints how many bars are short |
| N463-04 summary period P&L by entry date, risk by exit date | Reproduced, fixed | Calendar periods: P&L earned on the daily path, plus a separate cohort column. Futures marked to each session close; options exit-booked and labelled. On the supplied futures file the green trend rule's max drawdown is Rs 8.1 lakh at 10 lots, not Rs 4.8 lakh |
| N463-05 zone-only control hidden by the 30-trade minimum | Accepted, fixed | Controls reported with no minimum, plus their own walk-forward over the same folds |
| N463-06 same-minute volume used for the fill | Kept (your decision) | No change |
| N463-07 expiry listing from the signal day | Kept, documented | The order is decided at the signal close |
| N463-11 excluded chains | Accepted | `excluded_no_value` per candidate in `train_candidates.csv` |
| N463-13 stale README sentence | Fixed | |

# V4.6.3 (ATR stops, futures benchmark, take-profit fix)

| Change | Detail |
|---|---|
| **Bug fix** `zone_report.choose_tp` | V4.6.1-4.6.2 returned `None` both for "no level has enough trades" and for "no take-profit is best", and skipped both, so configurations whose best level was no take-profit were never ranked. Now a separate sentinel. On the V4.6.1 run: candidates 9,922 -> 11,243; top rules unchanged; bear walk-forward +1,835 (t 1.93) -> +278 (t 0.26) |
| `--stops`, `--atr-mult` | Index stop fixed at the signal bar: close -/+ mult x ATR(n) on 45-minute bars, checked on bar close; rolled calendar pairs keep the chain's level. Ranked as families `naked|atrN` |
| `zone_futures.py` | Long/short index proxy on the same signals and exits; `futures_zone` and `futures_expiry` (same horizon as the naked short); dated futures charges and slippage points; same trade format |
| `--keep-fills` | Fills are no longer stored by default (file size) |
| `zone_summary.py --structure all` | Top N per side for every family |

# V4.6.2 (wider strike and take-profit grid)

| Added | What it does |
|---|---|
| `--offsets` on `zone_contracts.py` and `zone_backtest.py` | Strike offsets from ATM; the backtest refuses offsets the extraction did not plan |
| `--tps` on `zone_backtest.py` | Take-profit levels, e.g. `none,.25,.3,.35,.4,.45,.5,.6,.75` |
| `zone_summary.py` | Top-N rules per side from `train_candidates.csv`, identical rules merged, rupees at `--lots` per 12-month period: P&L, win, PF, Sharpe, Sortino, Calmar, drawdown, max loss and win, exits. Warns when `--to-date` passes the split |

# V4.6.1 (corrections to the zone study after review)

| Finding | Decision | Change |
|---|---|---|
| Fill accepted using the same minute's volume | Accepted as a choice (yours) | Kept, and the search now covers every minute of the next bar (09:15-09:59 next session after the last bar). Recorded in trade-file meta |
| Four-day rule broken when the fill is on a later day | Fixed | Days are counted from the fill day; if the nearest expiry falls short, the next one is traded |
| Open positions dropped at the training cutoff | Fixed | Positions are valued at each 1 January and at `--split` (cash plus latest bar-close prices); the report closes them hypothetically and counts them |
| Take-profit chosen on too few trades; losers frozen | Fixed | Each cut-off needs `--min-trades`; only positive training totals are frozen; the report says when nothing qualifies |
| Vendor chosen by order | Fixed | Larger-volume vendor per contract-minute |
| Calendar exits waited for both legs in one minute | Fixed | Short bought back at its first liquid minute, long at its first liquid minute after |
| Weekly pair vs campaign win rate | Added | `pair_win` beside campaign `win`; also `max_drawdown`, `hypothetical`, `forced` |
| Incomplete spot bars; Pine assumptions | Kept by your decision | No change |
| Data needed for minute-by-minute fills | New `zone_contracts.py` | Targeted extraction replaces the 45-minute cache (a full all-minute chain would be about 125 GB). Tested against a local PostgreSQL 16 with synthetic data, which caught two bugs (NUMERIC as Decimal; temp table across months) |

# V4.6.0 (zone + swing study)

Calendar and fly code unchanged; trades from V4.5.4 stay valid (engine tag 4.5.4).

| Added | What it does |
|---|---|
| `INDEX_v15_zone_swing.pine` | v14 with the 3-bar persistence filter working (v14 declared the checks with `var`, so they were computed once), optional volume weighting off by default, MACD removed, swing-filter marks |
| `zone_spot.py` | Every spot minute 09:15-15:29 from `market_data.spot_bars` into SQLite |
| `zone_signals.py` | 45-minute session-aligned bars; 90/180/360-minute values only after their bar closes; zones; pivots; rising valleys and falling peaks; the 1,440-point grid plus zone-only controls; step 0 |
| `zone_backtest.py` | Naked short and 60-day calendar on each signal, five strikes, six take-profit levels; exit on zone change; calendar rolled at 15:15 on short expiry; both vendors per contract; long-exit lag cap of one session |
| `zone_report.py` | One position at a time per configuration; take-profit rule (best win rate within 75% of best total); ranking; yearly walk-forward inside training; freeze; single holdout evaluation with an inspection log |

# V4.5.4 (first real-data pilot)

Rerun simulations: the engine tag changed. Caches stay valid. With `--max-exit-lag -1` (default) trades match V4.5.3.

| Finding on real data | Change |
|---|---|
| After the short was bought back, a thin far long waited 1-7 days for a print, held naked. At a 5% cap this produced 749 of the points that separated it from 10%, on 28 matched trades | `--max-exit-lag`, `--forced-exit-haircut`. The forced exit is a model mark, tagged `model_mark`, `executable_claim` false, and not counted in `executed_fills` |
| Carry and naked-long drift were mixed | Each trade records `scheduled_exit_mark_inr`, `post_schedule_drift_inr`, `exit_lag_sessions`, `forced_exit`. `exit_lag_report.py` gives a train-only view with t-statistics clustered by short expiry |
| Expiry eras were split by entry date in the hand analysis | The report splits by short expiry: entries in late August 2025 can hold a 2025-09-02 Tuesday contract |
| A short whose expiry session is missing from the cache was held at a placeholder upper bound (-Rs 3.26 lakh on one lot) | No code change. End entries before the gap (`--to-date`), or supply `--settlement-master` |

# V4.5.3 (response to the ChatGPT and Gemini reviews of V4.5.2)

Rerun simulations: the engine tag changed, because iron-fly signals changed. Calendar trades are identical to V4.5.2. Caches stay valid. `term_curvature_5_30_90` is renamed; update any saved `--signals` list.

## ChatGPT
| Finding | Verdict | What changed |
|---|---|---|
| BOUNDARY-01 later P&L in training `net` | Reproduced, fixed | A trade open at the cutoff enters ranking with its last mark before the cutoff, status `open_at_cutoff`, and no later identity. `GroupMatrix.evaluate` `net` stops at its last session. `train_boundary` reports counts and as-of marks. Sharpe was never affected: daily equity was already clipped. On one synthetic ranking, 36 trades in the training sample were affected. |
| HOLDOUT-02 sealed report saw later rows | Accepted, fixed | `train_only` drops every row entered after the cutoff. BOUNDARY05 test: changing all later outcomes leaves `rankings.json` byte-identical apart from `lineage`. |
| HOLDOUT-01 log followed the directory | Reproduced, fixed | One per-user log keyed by trade content (`~/.nifty_research/holdout_inspections.jsonl`, or `NIFTY_HOLDOUT_LOG`). A corrupt log stops the run. An attempt is recorded before any holdout number exists. This is inspection tracking, not cryptographic sealing. |
| SIGNAL-01 curvature | Reproduced, fixed | `annual_variance_curvature_5_30_90` (old definition) and `total_variance_curvature_5_30_90`, both predeclared under `--signals all`. |
| PORTFOLIO-01 ties broken by id | Real in principle, no case in current flows | Groups hold one trade per day: `same_day_competing_groups` is now reported, 0 on the synthetic run. Identical entry-known fields cannot be ordered without some tie-break. Needed before any mixed-group book. |
| OPS-01 atomic reports | Accepted, fixed | The report set is written to a temporary directory, validated and renamed. |
| DIAGONAL-01 | Accepted, fixed | `structure_iv_basis` is `cross_strike_term_skew_ratio` when strikes differ. |
| Carry across the split (BOUNDARY-01 item 5) | Declared | `holdout_policy` states a fresh account at the split. |
| EXEC-01, CAPITAL-01, REGIME-01, STATS-01, COST-01, FREEZE-01, PERF-01 | Agreed, open | Need real data, broker notes or SPAN. A byte-level cache check is not added. |

## Gemini
| Claim | Verdict | What changed |
|---|---|---|
| 1.1 Quantile bounds leak across the embargo | Not a leak | Outer bounds come from training rows only, and are labelled `fixed_rule_fold_diagnostic`, in-sample for selection. Nested folds use their own past. Pinning resolved bounds in the freeze is still useful: added. |
| 1.2 Fly term signals empty | Real, different fix | The proposed `curve()` fallback mixes at-the-money IV with strike-`k` IV in one curve, so it puts skew in the term signal and changes calendar signals too. Flies now read `atm_curve`, at-the-money on every tenor. Calendars are unchanged. Synthetic: `iv30` populated on 262 of 262 fly trades. |
| 1.3 `fork` hardcoded | Accepted | Platform-aware; spawn path run once on a synthetic month. |
| 1.4 Fly `var_carry_daily` empty | Accepted in part | Uses `iv_30` only. The proposed fallback to realized vol would put two different quantities in one signal. |
| 2.1 Presorted `choose` | Accepted | The patch added the parameter with no caller. Callers now use it. |
| 2.2, 2.3 SQLite mmap, cache, batch surface | Not applied | Three alternating warm runs: 18.3 s, 18.8 s, 20.3 s with the pragmas against 18.4 s, 17.4 s, 19.0 s without. `surface_enriched` is not in the patch. Measure on real data first. |
| Patch script | Not applied as supplied | `--check` is rejected. `--short-decay-take-profit` is accepted but does nothing. `surface_enriched`, cached holdout marks and `downside_var_share_5d` are described but absent. Three tests fail. |
| 3.x Literature | Partly misattributed | Kilic and Shaliastovich is Management Science, not JFE. Bali and Murray studies the cross-section of equity options, not index put skew. The Andries et al. co-authors are Eisenbach, Kahn and Schmalz. OHLC cannot produce Patton-Sheppard semivariance, which uses intraday signed returns. |
| New signals and the short-decay rule | Deferred | Both reviews argue against widening the search before a real baseline. Add a 30-day anchor to the tenor list when the pilot is extended: the code already supports it. |

**Parity:** calendar trades, marks and identities equal V4.5.2. Legacy-flag ranking: 25 of 25 results, maximum difference 0.

# V4.5.2 (response to the independent V4.5.1 review)

Trade files from V4.5.1 stay valid unless they used `--event-policy`; those are refused and need a rerun. Caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| N01 unresolved trades dropped from structure comparison | Confirmed, fixed | `all_opened` is the default and keeps conservative marks, reported separately from `fully_observed_diagnostic` and `unresolved`. |
| N02 pairing on labels | Key weakness real, severity lower than stated, fixed | One trade file has one `--strike-mode`, and on a real run 166 of 166 (day, step, value) keys map to one strike and expiry. The review's fixture mixes modes and has `strike` and `short_expiry` columns the real table lacks. The join now uses provider, day, short expiry, actual short strike and hedge, with unmatched counts by reason and `many_to_one_keys`. Identical-mark aliases count once. |
| N03 date-level announcements | Confirmed, fixed | `event_date,first_known_at[,superseded_at[,source]]`, compared with the 09:45 decision in IST. A bare date counts from the next day. Each revision is its own row. |
| N04 window depended on supplied runs | Confirmed, fixed | `--eval-start` and `--eval-end` are required. Reports extra entries and the matched-cohort mean. |
| N05 freeze omitted code and cache | Confirmed, fixed | The freeze binds code hash, release, market digest, enrichment run, tariff, masters, calendars and the session list. A refusal names what moved. |
| N09 support across the expiry move | Accepted | `expiry_regime_mix.post_move_train_support` is `sufficient` or `insufficient`. |
| N12 output durability | Not changed | An interrupted run stays `complete=0`, and a full run takes seconds to minutes on the synthetic grid. Revisit if a real run takes hours. |
| N06, N07, N08, N10, N11 | Agreed, already disclosed | No code change. They need real data, broker notes or SPAN. |

The review's `NIFTY_V451_REPRODUCTIONS.py` no longer runs as written: its fixture lacks the `details` and `mtm_id` columns of the real trades table, which the corrected join reads. COMP01-04 in `tests/test_v4.py` cover the same cases on the real schema.

# V4.5.1 (response to the independent V4.5.0 review)

Rerun simulations: the trade schema gained `contract_id`, `mtm_id`, `tm1_outcome`; ranking refuses earlier files. V4.4.3 and later caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| F01 iron fly needed a far-dated quote | Reproduced, fixed | Flies get their own candidates (`target_tenor` 0, no long expiry). Nothing reads a far quote. `compare_structures.py` reports each universe, the dates only one side has, and the paired intersection. |
| F02 entry on T-1 held to expiry | Reproduced, fixed | Option B: rejected up front as `tm1_entry_on_last_session`, also for `p25_s50_tm1` and `rolling_tm1`. A same-day round trip is a different strategy. |
| F03 T-1 wrong without holidays | Reproduced, fixed | `--holiday-calendar` is mandatory for T-1 policies. The file is audited against cache sessions and a mismatch stops the run unless `--allow-calendar-mismatch`. `short_sessions` now uses it. Ranking warns when exact-session groups ran without one. |
| F04 `path_id` not canonical | Reproduced, fixed | Three identities: `contract_id` (dated signed contracts), `path_id` (signed cash flows, hedge included, no labels), `mtm_id` (daily marks and bounds). Trials, SPA and the shortlist use `mtm_id`. `search_counts` reports all three. |
| F05 holdout windows differed by group | Confirmed, fixed | Under `per_trade`, every strategy is scored from the split to the data end, and no trade can move that. `holdout_cohort_followup` is the selected trades' lifecycle view, labelled not comparable. Long-tenor strategies have fewer admitted entries in that window, which `holdout.n` shows. |
| F06 `risk_normalized` is not sizing | Accepted | Renamed `risk_normalized_score`, labelled counterfactual. Integer-lot sizing stays open (P2). |
| F07 failed T-1 exits | Confirmed, and worse than reported, fixed | A failed T-1 close left `pending_exit` set, and that blocked the 15:16 buyback on expiry day, so the short went to settlement. It now retries and buys back on expiry. Every T-1 trade has `tm1_outcome`; `tm1_execution` is per result; roll completed and failed counts are in diagnostics. A margin stress is still open. |
| F08 event calendar needs as-of | Accepted, fixed | Lines must be `event_date,first_known_on`. An event counts only from its announcement. Hash is in lineage. |
| PREV-01 flat sessions | Reproduced, fixed | `compare_fill_models.py` now needs `--cache` and scores every run on the session calendar. |
| PREV-02 break-even slippage | Reproduced, fixed | Total P&L over total traded units. Field renamed `breakeven_extra_slippage_pts_per_traded_unit`. |
| PREV-03 holdout only logged | Reproduced, fixed | `--research-mode train_only` is the default: no holdout number is computed and `selection_freeze.json` is written. `final_holdout` runs once, only if trades, settings and rules hash the same, and refuses a second look without `--allow-reinspection`. `open` is the old report. A person can still delete the log; this stops accidents, not intent. |
| Family ensembles naming | Accepted | Now `unfiltered_family_books`, with a note on admission rules. |
| 13 configurations, not 16 | Agreed | Flies no longer repeat per tenor. |

Deferred, as the review sequenced them: integer-lot sizing, margin shock stress, matrix reuse across basis views (Wave D), everything needing real data.

**The reviewer's script now stops at its TM101 assert. That is the fix:** a Monday-to-Tuesday `tm1` entry is rejected, so `err` is no longer `None`.

**Parity against V4.5.0 (synthetic):** 11,214 trades and all marks identical. Ranking with `--research-mode open --train-censor realized --holdout-entry-cutoff global_longest --bucket-scheme legacy`: all 25 results identical, maximum difference 0.

# V4.5.0 (deep review of V4.4.5)

See `REVIEW_V4_5.md`. Rerun simulations: the trade schema gained `scheduled_exit_day`, `short_sessions`, `entry_weekday`, `short_expiry_weekday`, `entry_risk_pts`, `events_in_short`, `path_id`; ranking refuses earlier files. V4.4.3 and later caches stay valid.

Default changes in `rank_strategies.py`, each with a flag to restore the old rule: `--train-censor scheduled`, `--holdout-entry-cutoff per_trade`, `--bucket-scheme exact_sessions`.

# V4.4.5 (response to the V4.4.4 reviews)

Rerun every simulation: the trade schema gained `economic_id` and ranking refuses earlier files. V4.4.3 and later caches stay valid.

| Finding | Verdict | What changed |
|---|---|---|
| R01 duplicate economic trades | Reproduced, fixed | One simulation per economic key (actual legs, policy, hedge, lot; target tenor only for `rolling_recenter`, where it steers the path). Aliases keep their own labels and signal features. `economic_id` is on every trade. Effective trials, global SPA, shortlist (`parameter_aliases`), campaign report and fill comparison all count each economic trade once. Alias rows are still written, because step 100 and step 500 are different rules on other days. |
| R02 calendar hashes not rechecked | Reproduced, fixed | `lineage.verify` rechecks all four auxiliary files. A file recorded at the run must exist with the same bytes. |
| R03 `--shift-control-max 1` crash | Reproduced, fixed | One deterministic mid-sample shift. `rank_strategies.py` now validates its arguments. |
| R04 mask count | Confirmed, fixed | `evaluated_unique_full_mtm_masks` counts only scored masks. `search_counts` reports rule cells, thin cells, encountered, scored, economically unique, finalists. |
| R05 zero-volume fills | Accepted as a limit | New stresses: `next_bar_open_traded_only` (ex-post filter, labelled) and `--entry-delay-minutes 1-3`. `compare_fill_models.py` adds zero-volume counts, calendar Sharpe and drawdown of the all-trades book. Delay covers entries and rolls only: exits have two cached minutes per window. |
| R06 holdout reuse | Partly addressed | Each ranking appends to `holdout_inspections.jsonl` beside the trades file and reports `holdout_inspection_number`, with a warning after the first. Sealing a final holdout is still your decision. |
| R07 audit labels and selection | Fixed | Diagnostic stages renamed `any_independent_liquid_print`, `any_synchronous_liquid_print`. `admitted_vs_rejected.csv` adds short and long DTE and IVs. |
| R08 contract master | Open | Ambiguous lots already show as a monthly stage. An NSE contract master must come from you. |
| R09 attribution and margin | Attribution fixed, margin open | The unexplained bucket is split into `transaction_charges`, `slippage`, `exercise_costs`, `hedge_net`, `not_attributed_model_gap`. |
| R10 ranking memory | Fixed | The finalist list is bounded, diagnostic value lists thinned. SPA stays capped by `--spa-max-models`. |
| R11, R12, R13 | Deferred | Need `plan_2025_06.txt` and real runs. No new strategies in this release, as the review asked. |

Also fixed while sweeping: unclosed SQLite connections in `lineage`, `publish_results`, `campaign_report`, `compare_fill_models`; invalid ranking arguments that crashed late.

Gemini's follow-up: margin model needs SPAN inputs. Corporate-action handling does not apply to NIFTY index options. Boosted trees, HMMs and parallel ranking are deferred: the search is already too large for its sample, and ranking took 3.4 s on the synthetic run, so a real profile comes first.

## How many combinations
| Level | Count |
|---|---|
| Simulations per eligible entry day, default flags | at most 216 (2 steps x 3 offsets x 6 tenors x 6 policy/structure pairs) |
| Strategy groups | at most 648 (three DTE buckets); most stay empty |
| Rules per group, default 3 signals | 60 |
| Rules per group, `--signals all` | 850 (38 x 20 fixed + 10 x 9 quantile) |
| Theoretical cells, `--signals all` | 550,800 |

Gemini's 15,552 assumes all hedges and nine policies, which are not defaults. Real counts appear in `search_counts`. Fill models, lots, participation, strike modes and hedges are separate runs.

## Parity against V4.4.4 (synthetic, 100 and 500 steps, puts and doubles, three policies)
187 of 187 trades identical. 98 simulations reused, 187 rows from 127 economic trades.

# V4.4.4 (response to the ChatGPT and Gemini reviews of V4.4.3)

Rerun every simulation; ranking refuses earlier trade files. V4.4.3 caches stay valid.

## ChatGPT
| Finding | Verdict | What changed |
|---|---|---|
| F01 fill depended on later fill-bar volume | Reproduced, fixed | `next_bar_open` no longer reads the fill bar's volume. Its volume, slippage range and eligibility all come from the finished minute. A fill on a zero-volume bar is priced and flagged (`fill_bar_zero_volume`, `fills_on_zero_volume_bar`). |
| F02 valuation tied to the 09:45 bar | Reproduced, fixed | `Data.days` is entries only; `Data.valuation_days` carries open trades and equals the ranking calendar. Missing 15:29 spot falls back to 15:16, then the daily close, flagged in the mark. Expiry settlement gains a daily-close proxy from the expiry session itself. |
| F03 audit ignored order size | Reproduced, fixed | `--lots`, the simulator's own gate, and the signal-debit check. `participation_stress.csv` covers lots 1/2/5/10 by cap. `execute_fills` alone decides admission. `weekly_exit_eligible` is a diagnostic. |
| F04 adverse model called causal | Accepted | Evidence class `ex_post_adverse_stress`; only `next_bar_open` is causal. |
| F05 exercise fee per leg | Accepted | `--exercise-fee-basis` per_contract (default), per_order, per_leg, zero. |
| F06 gamma add-on outside stop basis | Confirmed, fixed | The debit is set after every entry leg, the add-on and hedge cost. |
| F07 volume assumptions | Partly fixed | `volume_unit_source`, `liquidity_asof` and the zero-volume flag are recorded. Vendor documentation is still needed. |
| F08 settlement evidence | Partly fixed | Per-run counts for proxy settlements, assumed exercise fees and late settlement. Settlement already used only the expiry session's own prices. |
| F11 selection bias | Partly fixed | `admitted_vs_rejected.csv` compares 5-day RV and overnight move. Inverse-probability work is not done. |
| F12 auxiliary files | Fixed | Holiday and special-session files are hashed into meta and lineage; `tariff_version` joins lineage. |
| F15 double-calendar forward IV | Fixed | Forward variance per wing, then averaged; labelled `wing_average_descriptive`. |
| F09, F10, F13, F14, F16, F18 | Deferred | Order queue, margin simulator, sealed holdout process, jump stress, 2-D filters and ablations need real-data results first. |
| F17 attribution | Partly fixed | See Gemini E-01. |

COST07 needs a real contract note. PERF04 needs real data.

## Gemini
| Finding | Verdict | What changed |
|---|---|---|
| R-01 recenter | Partly right | The long could move before a short was known possible, on the non-synchronized path. Both paths now share one all-or-none block. Reasons are counted: `recenter_no_long_tenor`, `recenter_lot_mismatch`, `recenter_fell_back_to_plain_roll`. The "expiry == session" desync does not occur: rolls need more than 30 DTE. |
| R-02 iv_converge | Rejected | It reads each leg's own strike and handles pending exits first. The exit rule is defined on the raw ratio, independent of the entry filter, which is chosen after simulation. |
| R-03 curve bypass | Fixed | All signal IVs go through `data.greek`, so signal and simulator read one number. |
| R-04 holdout leakage | Rejected as a leak | Thresholds come from training rows only. The provenance gap was real: `quantile_rule`, `threshold_basis` and fold `resolved_bounds` are now reported. |
| S-01 SPA | Diagnosis wrong, problem real | See below. |
| S-02 shift control | Fixed | Shifts move whole candidate days. The seam is inherent to a circular test. |
| P-01 | Partly | Skips evaluation under `min_train`, reuses the raw basis. `FastChooser` rejected: `choose` barely registers in the profile, and bisecting non-session dates can reorder a same-day comparison, so it is not bit-identical. |
| P-02 hashing | Not changed | 1.85 us per row. |
| Enrich `t_years` caching | Rejected | One call per group, 36,480 calls in a 37 s run. |
| E-01 attribution | Fixed | Reprices one input at a time, adding `vanna_volga` and `rate`. Residual is zero on model prices. |
| 3.2 signals | Partly | `term_curvature_5_30_90` and `fwd_iv_to_rv_yz` added. `net_vega_to_debit` rejected: the debit is a fill price, unknown at 09:45. |
| 3.3 plateau | Fixed for thresholds | `plateau` on each finalist. Across strikes and tenors is open. |

## SPA calibration (trade_book and ma_increments, 150 runs, 300 reps)
Gemini's drop-in (analytic variance, longer auto block) was tested first. Analytic variance barely moved size, and a longer block made it worse: hold 30 went from 32% at block 10 to 52% at block 90. Studentizing each replicate is what helped, so `spa_test` is now a batch-means bootstrap-t.

| Hold | Block | Old size at 5% | New size (trade_book) | New size (ma) | New power (trade_book) |
|---|---|---|---|---|---|
| 5 | 10 | 13.5% | 9.3% | 4.7% | 72% |
| 15 | 10 | 13.5% | 6.7% | 8.0% | 56% |
| 15 | 20 | n/a | 4.0% | 4.0% | 48% |
| 30 | 10 | 32% | 15.3% | 21.3% | 51% |
| 30 | 20 | n/a | 8.0% | 10.0% | 29% |

Size is better but not nominal for long holds, and power falls. Use `--bootstrap-block 20` for campaigns of about 30 days.

## Parity against V4.4.3 (synthetic, `qualified_minute_close`, `--exercise-fee-basis per_leg`)
Unhedged 148 of 148 and hedged 74 of 74 trades unchanged. Under `next_bar_open`, V4.4.4 admits 12 more trades and paired P&L rises Rs 642 per trade, because a later zero-volume bar no longer vetoes a fill. 108 of those 140 trades used at least one zero-volume bar on this synthetic data. Check that count on real data before trusting the result.

# V4.4.3 (response to the V4.4.2 independent review)

Rebuild the cache and rerun every simulation: `SPOT_SQL` changed and ranking refuses earlier trade files.

| Finding | Verdict | What changed |
|---|---|---|
| F01 multi-lot brokerage | Confirmed, fixed | `--lots` and `--brokerage-order-mode` (`per_lot_orders` default, `one_order_per_leg`, `lots_per_order`, `fixed_orders`). `broker_actual` needs contract notes, so it is not built. |
| F02 non-ratio signals could not pass the grid | Confirmed, fixed | Nine features now use training-only quantile intervals (`--quantiles`). In nested walk-forward each fold derives them from its own embargoed past. `rankings.json` reports `signal_coverage`. Ratio features keep the ratio grid. |
| F03 fill not causal | Accepted | New default `next_bar_open`: eligibility from a finished minute's volume, then the next printed minute's open within the window. A later bar's volume or close cannot gate or price it, and slippage reads the eligible minute's volume. `qualified_minute_close` now needs the conditional flag. Every fill carries `evidence_class`; none claims to be executable. `next_bar_open_adverse` is the worst-print stress. |
| F04 cost ledger | Partly fixed | Dated futures exchange charge and futures brokerage capped at min(Rs 20, 0.03%). `tariff_version` stored. **Not done:** contract-note rounding, financing, RMS, shortfall fees. These need your broker's notes and terms. |
| F05 volume units | Partly fixed | Detection now samples every month and refuses if any month is ambiguous or months disagree; `volume_unit_report.json` records it. `audit_contract_coverage.py` writes `participation_stress.csv` at 1/2/5/10%. Bar volume still cannot show displayed depth. |
| F06 hedge and capital not executable | Accepted, deferred | No code change. The hedge stays labelled `UNTRADEABLE_PROXY`. A real hedge needs futures data and SPAN inputs. |
| F07 unobservable stops | Fixed as a stress | `--unobservable-stop-policy force_exit`; diagnostics add P&L on affected trades. Compare runs with `compare_fill_models.py`. |
| F08 SPA | Already disclosed | The warning is already in every `rankings.json`. No code change. Sealing one holdout is a process rule for you: do not reuse it across releases. |
| F09 spot source | Confirmed in code, fixed | `SPOT_SQL` now reads the same file as `DAILY_SQL`. New read-only `audit_spot_sources.py` compares every NIFTY copy and exits 1 on differences. The live run's two synthetic copies differ by construction, so it correctly reported them. |
| F10 order-level ledger | Partly fixed | Every fill records line-item `charges` and `charged_orders`. Hedge marks, rebalances and costs are ledger events, and the ledger alone rebuilds P&L. `observed_open` is renamed `reference_price`. Separate orders/executions tables are not added: OHLC data has one execution per order. |
| F11 invalid settings | Confirmed, fixed | `validate_config` in the simulator and audit; `required_volume` raises on an invalid cap. |
| F12 exploration, attribution | Deferred | Needs real-data results first. |

**Performance recommendations:** not actioned, by design: the review itself says to measure on real data first.

**Acceptance tests in `tests/test_v4.py`, class `V443`:** COST01-04, TIME01-03, VOL01-02, SIG01, MKT01, RISK01-02. COST03's contract-note rounding fixture is open. STATS01-02 and PERF01-02 need real data.

**Rerunning `NIFTY_V442_INDEPENDENT_REPRODUCTIONS.py`:** it stops at D with `ValueError`, the fix. A still prints because it calls `fill_flow` without a lot size; order splitting needs the lot. B exercises the legacy `next_minute` mode, still available behind the flag. C checks numbers against the ratio grid directly, and those signals no longer use it.

## Parity against V4.4.2 (synthetic; `qualified_minute_close`, `one_order_per_leg`)
| Run | Trades | Changed |
|---|---|---|
| Unhedged, 4 policies | 148 / 148 | 0 |
| Delta-entry hedged | 74 / 74 | 14, from the futures brokerage cap only (mean +Rs 6.28, max Rs 44.01) |

**Effect of the new default:** `next_bar_open` drops 20 of 148 trades (no common later print in the entry window) and lowers paired P&L by Rs 708 per trade. `next_bar_open_adverse` takes a further Rs 330. On real data these numbers will differ.

# V4.4.2 (costs, volumes, no lookahead)

Rerun every simulation; ranking refuses earlier trade files. V4.4.1 caches stay valid.

## Costs
| Item | Before | Now |
|---|---|---|
| NSE option charge | Flat 3.5 bps | Dated: 4.95 bps before 2024-10-01, then 3.553 bps including IPFT. `--exchange-bps` overrides |
| SEBI fee | Missing | 0.01 bps, GST applied |
| STT, stamp, brokerage + GST | Correct | Unchanged, now tested by hand per side and across each date boundary |
| Expiry | STT only | Adds `--exercise-fee` (default Rs 20 + GST per ITM leg). A conservative assumption; set 0 if your broker charges none |
| Index hedge | Flat 2 bps | 2 bps plus NIFTY-future statutory costs by side: STT 2 bps sell, 5 bps from 2026-04-01; stamp on buys; exchange, SEBI, GST, brokerage. `--no-hedge-statutory` restores the old charge |
| Duplicate cost code | `candidate_core.fill_cash` | Removed: unused, one cost path remains |

## Volumes
- New `--max-participation` (default 10% of the bar, 0 disables) on every entry, exit, roll, recenter and the gamma add-on; the coverage audit applies the same gate. 10% is an assumption.
- `--volume-unit auto` detects from the data whether vendor volume counts units or contracts, per run, and stops if it cannot tell.
- Slippage now uses contracts traded, so both vendors are priced on one scale.
- The independent recenter path ignored minimum volume altogether (reviewer F01). Fixed.

## No lookahead
| Leak | Fix |
|---|---|
| **Mine, in V4.4.1:** entries were blocked using `complete_session`, which is known only after the close | Eligibility is now weekday plus `--special-sessions`, a list published in advance. Days that later turn out incomplete are admitted and counted as `entry_day_later_incomplete` |
| Generator required the 09:46 spot bar | Removed |
| `short_sessions` counted future days in the data | Weekday count |
| Holidays for business-day IV inferred from gaps in later data | Weekdays unless `--holiday-calendar` supplies a published list |
| Debit gate read the fill price | Reads the 09:45 signal quotes |
| Open-based fill models condition on later volume | CLI and ranking refuse them without `--allow-conditional-fill-models` |

`test_ledger_prefix_ignores_later_data_on_every_policy` corrupts all data after seven cut times and requires the ledger up to each cut to be unchanged, across 9 policies and 2 hedges. `test_signals_ignore_everything_after_0945` deletes all later data and corrupts the entry day's daily row.

## Reviewer repro (`NIFTY_V441_INDEPENDENT_REPRO.py`)
- F01 recenter fill clock: fixed. The script now stops at its own "Fix appears applied" assertion.
- F02 partial sessions erased drawdown: my V4.4.1 regression. Fixed: the ranking calendar includes every day with data. The script calls `GroupMatrix` with a calendar it builds itself, so its assertion still passes; `valuation_sessions` is the fix and has its own test.
- F03 unobservable stops: now reported as `stop_unobservable` ledger events and counts per run. Exits remain delayed when a stop cannot be observed, since no causal price exists.

The V4.4 review's F02 asked that incomplete sessions be excluded from entries. That cannot be done from the day's own outcome without lookahead, so it is replaced by the published-list rule above.

## Parity against V4.4.1 (synthetic, legacy cost flags, participation off)
| Run | Trades | Changed |
|---|---|---|
| Unhedged, 4 policies | 148 / 148 | 0 |
| Delta-entry hedged | 74 / 74 | 0 |

Effect of the new defaults on the same runs: unhedged mean -Rs 147.74 per trade, hedged -Rs 86.34. Four unhedged trades changed fill count because of the participation gate.

# V4.4.1 (correction release: sixth review round)

Rebuild the cache and rerun every simulation. V4.4 caches lack the market content digest, and ranking refuses V4.4 trade files.

| Finding | Status | What changed |
|---|---|---|
| F01 hedge outlived the options | Reproduced, fixed | The index proxy closes at the last option fill. Hedged P&L no longer moves when spot changes after exit. |
| F02 partial sessions admitted entries | Reproduced, fixed | Generator, roll sales, coverage audit, trend features and the ranking calendar require `complete_session`. Open positions are still marked and settled on partial days. `--allow-special-sessions` opts in. |
| F03 open fill conditioned on that minute's volume | Accepted | New default `qualified_minute_close`: fills at the close of the minute whose volume qualified it, using nothing later. Open-based models are labelled conditional-liquidity models. Every fill records `price_field` and `executable_claim=false`. |
| F04 capital does not enforce margin | Deferred | Needs broker or SPAN inputs. Capital stays a labelled scenario. |
| F05 censored trades in ranking | Fixed for the holdout | `--holdout-entry-cutoff` defaults to `auto`: data end minus the longest tenor, independent of trade outcomes. A cutoff before `--split` is refused. |
| F06 lineage did not bind market rows | Fixed for builds | Order-independent per-month digest of every extracted row, resume-safe, carried through trades, rankings and publish. Quality JSON digest stored. A SQLite cache edited after the build is not rescanned. |

`compare_fill_models.py` reports trades lost, paired per-unit difference and break-even slippage across fill models (part of E05).

**One error of mine caught during the live run.** The first `auto` cutoff used the latest trade exit, so it depended on outcomes, and it could silently fall before the split and empty the holdout. Both are fixed and tested.

## Parity against V4.4 (`--fill-model next_minute`, 2025-10-01 to 2026-02-28, synthetic)
| Run | Trades | Changed | Total P&L V4.4 / V4.4.1 |
|---|---|---|---|
| Unhedged | 68 / 68 | 0 | ₹125,400.89 / ₹125,400.89 |
| Delta-entry hedged | 68 / 68 | 68 | ₹180,539.31 / ₹156,887.65 |

The hedged change is the declared F01 correction: per trade it equals hedge units x (spot at last option fill - 15:29 close), with at most ₹0.51 left over from hedge cost. The cache had only complete sessions, so F02 changes nothing here; the default fill model does change results, so use `next_minute` to compare with V4.4.

## Open, needing real data first
E01-E04 and E06-E16, P01-P09. An external NSE session master (E09) is not built: eligibility uses `complete_session` from the data, and `Data.holidays` is still inferred from gaps (affects only the opt-in business-day IV).

# V4.4 (fifth review round, and the guidebook)

Rebuild the cache, re-enrich and re-simulate. Exits need three new minute bars.

## Corrections
| Finding | Status | What changed |
|---|---|---|
| P0-01 lineage | Fixed, reproduced | Digest covers trades, marks, fills, diagnostics and run config; report content, code, requirements, migrations and master-file contents are verified at publish |
| P0-02 hedge timing | Fixed, reproduced | Hedge opens at the spread's fill minute. Missing spot rejects only the hedged path |
| P0-03 exit clock | Fixed, reproduced | Under delayed models every exit qualifies on one minute and fills on the next (11:32, 13:32, 15:17 now cached). Ledger records signal, order, fill and evidence times |
| P0-04 finalization | Fixed, reproduced | Idempotent; `kill -9` during index build then resume matches a clean build |
| P1-05 resume settings | Fixed | A resume must match every extraction setting and the SQL |
| P1-06 provenance | Partly | Master-file contents, requirements, migrations and runtime versions recorded. A market-row content hash is not computed |
| P1-07 mismatched cache | Fixed | Ranking refuses a different cache or enrichment |
| P1-09 / Gemini 4 gamma leg | Fixed | A third leg on the same entry clock |
| P1-23 SPA memory | Fixed | Collection stops at the cap; the family is still counted |
| Gemini 2 debit | Fixed | Stops use actual cost under delayed fills |
| Gemini 3 naked short | Fixed | A long is never sold while its short is open; unfinished exits retry at later checkpoints the same day |
| Gemini 5, 1 | Fixed; labelled | Cache clear; holdout stress is documented as open-position path stress |
| P2-24 | Partly | Within-group SPA memoized |

Parity: with legacy flags, 2,162 of 2,206 trades equal V4.3. The other 44, mostly `p25_s50`, are partial exits changed by the protective-long and same-day-retry rules.

## New diagnostics
- `circular_shift_control`: best threshold against the same search on a shifted signal. At nominal 5%, null rejection was 4.0-5.7% across 300-run simulations with persistent signals and P&L, against 13-32% for SPA. It covers one signal and group only.
- Daily Greek attribution, additive by construction: `details.pnl_attribution`.
- Session-time IV (`iv_ratio_biz`, `iv_to_rv_yz_biz`), a rescale of total variance. Causal trend fields. All opt-in.

## Guidebook paths
`--long-strike-offsets`, `atm_weekly_roll`, `--roll-timing`, `campaign_report.py`. See `GUIDEBOOK_RESEARCH_NOTES.md`.

## Not taken from Gemini's 20 points
- Point 12 was right on the structure, and is implemented. The "15% of sale price" tip-toe rule in Point 13 is not in the guidebook, which only names that strategy.
- Point 10 log-moneyness: over a strike gap of at most 4% the change is negligible.
- Points 7, 8, 9, 11, 17, 18 and 20: deferred until real data shows they matter. Point 7 needs a margin model, not an add-on to a scenario charge.

Deferred from ChatGPT's review: account ledger and liquidation, NSE session master, canonical-reason fields, verified-only settlement tracks. All need real data or external inputs.

---

# V4.3 correction release (fourth review round)

Rebuild the cache, re-enrich and re-simulate. No new strategies or signals.

| Finding | Status | What changed |
|---|---|---|
| F01/F07 fill timing | Fixed | `--fill-model`: `same_minute_first_print` (optimistic), `next_minute` (default), `next_minute_adverse_range`. Ledger records the observed bar and the fill assumption. Single-minute exits such as 15:16 cannot be delayed, because the cache has no later minute |
| F02 stale Greeks | Fixed, reproduced | Each run clears derived rows and reads as not enriched until it finishes; `enrichment_run_id` records its parameters |
| F03 audit vs simulator | Fixed, reproduced | Audit shares expiry selection and fill code with the simulator and reports every stage. `entry_both_liquid` is gone |
| F04/F20 lineage | Done | `lineage` block binds a report to trades, enrichment, arguments and code. Publishing refuses a mismatch and skips a repeat. No migration needed: it lives in the existing JSONB |
| F05 data quality | Done | Complete now means 370+ of 375 minutes; `session_quality`, OHLC violations and duplicate price conflicts audited |
| F08 hedge in stops | Fixed | Hedge marked to the checkpoint |
| F09 independent recenter | Forbidden at the CLI | |
| F19 cache build | Done | Monthly transactions, `--resume`, staged file, row reconciliation, integrity check, atomic replace |
| F06, F11-F17 | Deferred | Margin, account ledger and search discipline belong after a real baseline |

Blueprint items confirmed and fixed: one bad strike failed the whole parity fit; `iv_5` empty for 5-6 DTE front weeklies; a March 2026 expiry before the 31st got lot 65; SPA used a different capital denominator per model; DSR dispersion used survivors only; CSV mixed raw and scored metrics without saying so; duplicate `opt_lookup` index; `longs[0]` staleness; unclosed file; `lru_cache` retention.

Blueprint items not taken: the checkpoint exit is already one minute per leg, so needs no sync; `seen` dedupe is by design, since strike values are separate groups; delta discounting moves a strike by far less than one strike step; routing `generate()` through cached Greeks is speed only and waits for real profiling; `--max-capital` in raw rupees is correct.

---

# V4.2 ranking corrections (third review round)

No re-simulation is needed: trade databases and caches from V4.1 still work. Only `rank_strategies.py` output changes.

| Finding | Status | What changed |
|---|---|---|
| V4.1-F01 cross-fold limits | Fixed, reproduced first | `nested_walk_forward.portfolio` is one portfolio across folds; a position open at a fold boundary keeps its slot and capital. The summed fold aggregate is gone |
| V4.1-F02 flat days dropped | Fixed, reproduced first | Every Sharpe, drawdown, baseline and selection uses a fixed session window: 25.46 becomes 7.32 on the reviewer's case. `active_day_sharpe` and `time_in_market` reported separately |
| V4.1-F03 fold boundaries | Fixed | Fold statistics are MTM clipped to the fold, from one portfolio, counting carried-in trades. `entry_cohort_lifecycle_net` is kept as a labelled diagnostic. A flat fold scores 0 and is no longer skipped |
| V4.1-F04 `fixed_risk` | Renamed | `entry_capital_normalized` |
| V4.1-F05/F06 | Unchanged, labelled | SPA stays discount-only. Real fills, margin and settlements still missing |
| V4.1-F07 | Done | `VALIDATION_V4_2.md` replaces `VALIDATION_REPORT.md` |

On the synthetic grid only 6 of the top 25 rules survive from V4.1, so earlier rankings should not be reused.

## Gemini "V4.1 audit" (12 points)
Its status table repeats claims already disproved: ratio calendars, in-memory index, memoization, delta-adjusted fills, and `nse_twap_settlement` / `opt_enriched`. None exist. `calibrate_spa.py` does not use AR(1)/GARCH processes. `docs/live_postgres_run.txt` is synthetic rows on inferred tables, so it cannot motivate "real-data insights".

| Item | Decision | Reason |
|---|---|---|
| 1 Long-leg continuity gate | Decide from coverage audit | Sensible and lookahead-free, but the ">80%" figures are unmeasured. Snapped quasi-calendars change the structure; not now |
| 2 Session tagging | Audit added | Muhurat evening bars cannot enter: the extract keeps 09:15-15:29 and `data.days` needs a 09:45 bar. Short morning sessions can; `special_sessions` now lists them |
| 3, 6, 8, 10, 11, 12 | Same as last round | Tax cliff remains arithmetically wrong (STT is a fraction of intrinsic); no migration needed, as the independent review agrees |
| 4 Business time | After coverage | Same; its Friday direction and size remain wrong |
| 5 Greek P&L attribution | **Best new idea; after the pilot** | Tells carry from direction. Needs reliable daily IVs from real data first |
| 7 Intraday IV exit | Already exists | `iv_converge` checks 11:30, 13:30, 15:15 |
| 9 CPCV, PBO, plateau | After the pilot | As before |

---

# V4.1 corrections (second review round)

Trade databases from V4.0 must be re-simulated. Rebuild the cache for close-to-close minute variance.

**Correction to my V4 note:** I advised treating SPA p below 0.01 as the bar. On realistic overlapping trades that is not safe; see the table below.

| Finding | Status | What changed |
|---|---|---|
| F-V4-01 recenter order | Fixed, reproduced first | All three legs fill in one minute, or no recenter happens (`recenter_no_common_minute`). On synthetic data 165 of 379 recenter trades changed P&L |
| F-V4-02 nested walk-forward | Fixed | `nested_walk_forward`: configuration and thresholds per fold come only from earlier, fully exited, embargoed trades. Old folds renamed `fixed_rule_fold_diagnostic` |
| F-V4-03 labels | Done | `labels` block, capital-multiplier stress. Real margin and bid/ask remain missing |
| F-V4-04 roll gate | Done | `--roll-signal-policy`, `--roll-iv-min/max`; every decision kept in `roll_decisions` |
| F-V4-05 partial exits | Done | Pending retries pair when a common minute exists; residual days tagged in mark quality and counted. Capital unchanged: the scenario charge already applies per short unit |
| F-V4-06 minute RV | Done | `rv5_1m_range_proxy` and a separate `rv5_1m_closeclose` |
| F-V4-07 lot eras | Done | `--score-basis`, default `per_unit`; `basis_views` reports raw, per unit, fixed risk |
| F-V4-08 SPA calibration | Tested, **not solved** | See below |
| F-V4-09 cohorts | Done | `--holdout-entry-cutoff`, `holdout_cohorts`. Official settlements still need your data |
| F-V4-10 runbook | Done | `README_V4.md` |

Acceptance tests T01-T07 are unit tests. T08 is in `docs/live_postgres_run.txt`, on inferred tables and synthetic rows. T09: on synthetic data `intrabar_range` is close to `tenor_volume`, because the fixture's bars are narrow, so real-data sensitivity is still unknown. T10 is `calibrate_spa.py`.

Regression: with legacy flags all 2,586 trades equal V4.0; with `--score-basis raw` the top 25 rankings are identical.

## SPA calibration (`calibrate_spa.py --runs 200 --reps 300`, nominal size vs observed)
`trade_book` is overlapping campaigns; `ma_increments` is a harsher persistent process.
```
process        hold block size@5% size@1% power@5%(+0.25sd/day)
trade_book        5     5  0.110  0.035    0.840
trade_book        5    10  0.135  0.030    0.860
trade_book        5    20  0.190  0.085    0.880
trade_book        5    40  0.235  0.150    0.980
trade_book       15     5  0.145  0.035    0.840
trade_book       15    10  0.135  0.040    0.820
trade_book       15    20  0.185  0.065    0.800
trade_book       15    40  0.265  0.095    0.820
trade_book       30     5  0.390  0.190    0.700
trade_book       30    10  0.320  0.150    0.640
trade_book       30    20  0.320  0.155    0.620
trade_book       30    40  0.420  0.235    0.620
ma_increments     5     5  0.195  0.060    0.460
ma_increments     5    10  0.185  0.045    0.420
ma_increments     5    20  0.205  0.090    0.420
ma_increments     5    40  0.280  0.140    0.540
ma_increments    15     5  0.420  0.255    0.540
ma_increments    15    10  0.320  0.160    0.400
ma_increments    15    20  0.315  0.135    0.360
ma_increments    15    40  0.375  0.200    0.420
ma_increments    30     5  0.625  0.510    0.640
ma_increments    30    10  0.500  0.390    0.520
ma_increments    30    20  0.445  0.330    0.420
ma_increments    30    40  0.475  0.350    0.480
```
False positives run far above nominal, and worse with longer holds. Treat SPA as a way to discount a result. A high p-value is informative; a low one is not confirmation. Nested walk-forward and the untouched holdout carry the weight.

## Gemini V5 roadmap
Its V4 scorecard is unreliable. Items 5 and 9 are marked incorporated, but ratio calendars and memoization do not exist in the code; it also cites `nse_twap_settlement`, `opt_enriched` and delta-adjusted fills, none of which exist.

| Item | Decision | Reason |
|---|---|---|
| 1 Asian TWAP, smart close | Rejected for now | Rests on settlement being a 30-print average. "Negative net payoff" is wrong: STT is a fraction of intrinsic, so payoff stays positive. Shorts pay no exercise STT. Shorts usually close at 15:16 anyway. Needs partial-window data the cache lacks; full-window TWAP would be lookahead |
| 2 Business time | Next, after coverage | Real issue, wrong numbers. At 09:45, Friday-to-Tuesday calendar IV roughly equals session IV (not 18-25% below). Monday is about 50% above, but default entries are Wed-Fri. Measure the weekday effect on real data first |
| 3 Minimum-variance delta | Rejected for now | The hedge is a non-tradable index proxy, excluded from ranking. Needs futures history. The 30-45% claim is unsupported |
| 4 Term factors | Deferred | More signals enlarge the search, and multiple-testing control is not reliable yet |
| 5 Vega sizing | Partly covered | `fixed_risk` addresses cross-era sizing; revisit after a baseline |
| 6 Intraday IV exit | Already exists | `iv_converge` already checks 11:30, 13:30 and 15:15. The claim of EOD-only is false |
| 7 CPCV, PBO | Good candidate after the pilot | PBO needs real runs to mean anything; the 0.25 gate is arbitrary |
| 8 Plateau gate | Good candidate after the pilot | Cheap, once the grid is real |
| 9 Multiprocessing | Deferred | About 4s for 2,586 simulations; profile real data first |
| 10 BLAS stacking | Rejected | Ranking takes seconds, and its drawdown code repeats the initial-loss error |
| Housekeeping | Benchmark wings added | `0002` migration not needed: new statistics fit the JSONB columns |

---

# V4 changes: review of V3 and the Gemini roadmap

Status: research software. Tested on synthetic data and on a temporary PostgreSQL 16 whose
`market_data` tables were inferred from the SQL in this repo. No real NIFTY result exists yet.

## Rebuild needed
Rebuild the cache to get `spot_daily_ext` (TWAP, 1-minute variance). An older cache still runs, but
TWAP settlement falls back to the 15:29 close and `rv5_1m` is empty. Re-run `enrich_cache.py` and
`calendar_research.py`; `rank_strategies.py` refuses a trade database from before V4.

## Bugs fixed (not in the Gemini review)
| Where | Problem | Fix |
|---|---|---|
| `publish_results.py` | `ON CONFLICT DO UPDATE` with no target is a PostgreSQL error, so `--quality` aborted the whole publish | Conflict target named |
| `prepare_research_cache.py` | `Decimal` from NUMERIC price columns cannot bind to SQLite; extract crashed on the first batch | Cast to float/int. Applies if your columns are NUMERIC; the cast is harmless otherwise |
| `calendar_research.py` | Expiry settled only on `session==expiry`. If that session was missing from `data.days` the leg never settled and blocked the trade to the horizon | `session>=expiry`, counted as `settled_after_missing_expiry_session` |
| `calendar_research.py` | Gamma add-on delta left out of the initial index hedge | Included |
| `candidate_core.py` | IVs discounted at flat `--rate` while forwards used the parity-implied rate; `forward_iv_ratio` disagreed with the recomputed `iv_ratio`, and was stale for double calendars | Parity rate used; ratio recomputed for the traded legs |
| `rank_strategies.py` | `net` in the holdout stress scenarios ignored the stress | Reports stressed end value |
| `audit_quality.py` | `spot_daily` and timed spot come from different source files, unchecked | New `spot_source_consistency` block |

## Roadmap verdicts
| # | Item | Outcome | Correction to the roadmap |
|---|---|---|---|
| 1 | TWAP settlement | Done, as a **labelled proxy** | Not "exact". NSE settles on the index closing price, built from constituents' last-half-hour VWAP. A simple average of index minute closes only approximates it, so it never counts as `verified`. Expiries 2026-08-03 to 2026-12-06 (closing-auction regime) skip TWAP. Impact is small: shorts normally buy back at 15:16 and reach settlement only when that fill is missing |
| 1 | Exercise STT | Done | Buyer only, so the short leg is not charged. 0.125% to 2026-03-31, then 0.15% |
| 2 | Synchronized entry | Done, default on | Delta-shifting an earlier print is rejected: it invents a fill. No common minute means the trade is rejected and counted |
| 3 | Yang-Zhang, gap share | Done | For N=5, k=0.1197, not 0.134. Overnight returns never span a dropped session. The 40-55% gap share claim is unverified |
| 3 | 1-minute RV | Done | The roadmap's SQL is a summed Parkinson estimator, not squared returns. `rv5_1m` adds overnight variance |
| 4 | `bf25_short`, `var_carry_daily` | Done, opt-in | `breakeven_move_std` omitted: imaginary whenever forward vol exceeds short vol |
| 5 | Ratio calendars | **Deferred** | A 3:2 leaves a naked short and the capital model has no SPAN. The 3:2 neutrality only holds near 5/90 DTE. Needs a defined-risk wing and real margin first |
| 6 | Bootstrap, SPA | Done | Hansen's consistent p-value added. Universe is every mask scored, each against its own group's unfiltered baseline, on scenario-capital returns |
| 7 | Vendor concordance | Done | Compares open and close |
| 7 | Intrabar slippage | Done | The roadmap's cap makes a one-print bar free. Parametric slippage is now the floor |
| 8 | IV solver | Done | See below. The diagnosis was wrong: V3 has no Corrado-Miller step and 0 of 156 failures sat at 5.0; all were OTM quotes stuck at a low seed. The roadmap code hardcodes `fallbacks=0` and never verifies; V4 counts real fallbacks and checks every price |
| 9 | In-memory dict index | **Replaced** | The 800k row estimate is far too low: a 31-strike synthetic chain produced 3.06M rows in 217 sessions. Profiling showed point lookups cost about 10us, and the real cost was loading whole surfaces. V4 loads one expiry at a time. First-cycle memoization deferred: risk outweighs gain |
| 10 | Matrix replay | Done | The roadmap's drawdown misses an initial loss; V4 anchors the peak at 0. Agreement is within floating-point, not bit-identical |

## Measured (synthetic, this machine)
| Measure | V3 | V4 |
|---|---|---|
| IV solve, 200k quotes | 1.51s, 16,508 fallbacks | 0.14s, 0 fallbacks |
| IV solve, tick-rounded wings, 200k | 2.98s, 35,468 fallbacks | 0.14s, 0 fallbacks |
| `enrich_cache.py`, 3.06M rows (V4 also runs the vendor join) | 65s | 40s |
| `calendar_research.py`, 2,586 simulations | 7.2s | 4.0s |
| Portfolio evaluation, 300-trade subsets | 17.2ms | 1.2ms |

The roadmap's 50x and 35-60x end-to-end figures were not reproduced.

## Verification
- V4 with `--fill-sync independent --settlement-proxy close_1529 --exercise-stt 0`: all 2,586 trades match V3 on P&L, status, exit day and short sales; 61,578 marks agree within 2.5e-7.
- Rankings on identical trades: same 25 results in the same order, largest metric difference 1.8e-15.
- IV against V3 on 3.06M quotes: 7 differ by more than 1e-4; the three largest match Brent under V4 and not under V3.
- Full pipeline ran on live PostgreSQL 16: migrate, extract, enrich, simulate, audit, rank, publish.
- SPA null rejection at 5%: 8.5% over 200 simulations, so slightly liberal. Treat p below 0.01 as the bar.
- 40 unit tests, smoke run.

## New flags
`calendar_research.py`: `--fill-sync`, `--settlement-proxy`, `--exercise-stt`, `--slippage-mode intrabar_range`, `--intrabar-lambda`, `--intrabar-min`, `--exclude-vendor-discordant`.
`rank_strategies.py`: `--bootstrap-reps`, `--bootstrap-block`, `--spa-max-models`; signals `iv_to_rv_yz`, `iv_to_rv_1m`, `gap_share_5d`, `bf25_short`, `var_carry_daily`.

## Still open
- Sharpe and scoring use raw INR while lot size moves 25, 75, 65 across the sample, so later periods weigh differently.
- IV uses calendar time, so weekends distort short-dated IV. The session-count bucket only partly controls this.
- Sessions after 2026-08-03 have changed closing mechanics. Review before extending `--to-date` past July 2026.
- Every added signal enlarges the search. Read SPA alongside the holdout.
