# V4.7–4.8 notes (2026-11-11/12)

## Bug found: forced long exit sign (V4.6.0–4.6.4)
`zone_backtest.Sim.close` booked a calendar long force-closed at the lag cap as `cash = -units × price` (a purchase) instead of `+units × price`. In the V4.6 trades file: 332 forced exits (320 bear call calendars, 12 bull put calendars), median understatement 2,317 points per unit each. **All V4.6.x bear-calendar results are invalid (biased down).** Naked options and futures unaffected. Fixed in V4.7.0 with a test.

## Parameter provenance
The frozen rules in ZONE_STUDY_V4_6_1 (red: N3 k1.5 m8 j0.25 tw4, strike +400 ITM, TP20; green: zone-only tw6) are in-sample winners picked from about 260k combinations (2021–24). Only walk-forward figures are honest.

## Buy-and-hold vs green trend rule (per unit, spot prices, before carry)
- 2021-06 → 2025-01: buy and hold +8,097 points (max DD 3,141); green zone-only tw6 ATR30 futures +6,662 over 28 trades, in the market ~46% of sessions.
- Carry not in the data: at an assumed 5.5%/yr, buy and hold pays about 3,750 points.
- 2025-01 → 2026-07: index +229 (flat), max DD 3,956.

## First real-data grid (V4.7.2, 2026-11-12), 80.3M combinations, under 30 minutes
- Every combination was computed, but no calendar reached 30 training trades. The causes:
  - no long expiry more than 30 days out (13,140)
  - one leg filled and the other not (10,505)
  - not extracted (4,858)
  - illiquid (6,645)
  - The put-calendar question is still unanswered: this is a data-coverage problem.
- Wide searches lose most training profit out of sample (bull naked, all configurations, any stop: training +1,422/yr → walk-forward −148/yr). The zone-only settings hold up much better (bull futures to zone change, no stop: +1,796 → +1,729/yr, but only 15 trades).
- The user set the trade minimum to 15 (V4.7.3).

## V4.8.0–4.8.3 (four review rounds)
- **Rules:**
  - naked 50 profit × 368 stop rules, calendar 43 × 355, futures 34 × ~348
  - plus layered policies (initial × management × mandatory exit, 659–879 per structure)
  - about 1.2 billion combinations in all
- **Stability mode:** conservative lower quartile (the floor((n−1)/4)-th smallest value) of the training totals across one-step neighbours; edge settings are scored; `baselines.csv`.
- **Execution and accounting:**
  - each fill window is booked on its own with dated costs (V4.8.2 fixed my V4.7.1 spreading of exit cash by quantity)
  - window ends inside a multi-day exit use the state at the close of the last session before them, fill or no fill (8 session slots; NaN if unpriceable or beyond). V4.8.3 fixed V4.8.2's use of fill days only.
- **Integrity:** `algorithm_digest` covers the computation code, the indicator code (ATR/RMA/EMA), the numpy version and the holiday list; `--resume` and report reading refuse a mismatch.
- **Weekend exits:** from the exchange calendar via `zone_grid.py --holidays nse_holidays.txt` (the user has this file); without it, Fridays only. Data gaps never count.
- **Status:** identical to V4.7.3 on all shared rules (synthetic, 247M values). The reviewer recommends no further grid expansion.

## V4.8.3 real-data paths run and V4.8.4 fix (2026-11-12)
- zone_paths.log (410 s): 59,480 paths, 16,371 not filled. Partial entries 12,104 (short filled, long none 7,954 / part 2,994). `reject_not_extracted` only 175.
- Diagnosis: a calendar long missing from the extraction was treated as an illiquid fill (short sold then bought back: a loss invented from a data gap).
- **V4.8.4:** an entry with any leg absent from that day's extraction is rejected as `not_extracted`.

## V4.8.4 real-data run and V4.8.5 (2026-11-12)
- zone_paths.log (347 s): 21,054 not filled; `reject_not_extracted` 4,858 (same as V4.7.2: the rise from 175 is the V4.8.4 reclassification, not lost coverage — ChatGPT's review misread this). Partials 7,293 (long none 3,154, long part 2,990); 6,197 unwound.
- Options-file check by the user: only 626 of 1,260 days list any expiry > 30 days out (days without: 2021 127, 2022 228, 2023 207, 2024 68, 2025 0, 2026 4); 589 extracted. So the 13,140 `no_long_expiry` are a source-listing limit for 2021–24, not extraction.
- **V4.8.5:**
  - All legs fill together minute by minute in equal whole lots (each minute trades the smallest leg capacity; no look-ahead). No entry is ever partial; a calendar never holds a naked short. First entry: matched size, 1 lot up to `--lots` (P&L per unit). Roll reopen: full path size or the path ends. Trade-off: no legging across minutes, so slightly conservative fills.
  - `gaps` table in the paths file + new `zone_coverage.py`: classifies each gap (expiry not listed / strike not planned / planned but empty in source) by year, month, put/call, leg, DTE, moneyness, provider, roll; listed far expiries per year and provider.
  - 372 tests pass. Paths must be rebuilt.
- Next: run paths → coverage → grid; only `not_planned` gaps are fixable by re-extraction.
