# V4.9.4 notes (2026-11-12)

From ChatGPT's V4.9.3 review. Table in CHANGES_V4.md.

## Fixed
- Admission funds are time-causal: realised P&L so far + open positions marked at the decision bar (a same-day loss now counts).
  Also: exits hold margin/lots/slot through the bar of their last fill (off-by-one found while fixing this).
- Lot cap: a full-size position counts 10 reference lots whatever the rounding (9 lots of 75 no longer cut to 8);
  --lot-cap-unit exchange for actual exchange lots; peak exchange lots and gross contracts always reported.
- --return-basis cagr now governs selection (exact conversion: total/capital >= (1+t)^years - 1).
- Overall return = P&L over elapsed period / capital / years (was mean of yearly figures: my error; synthetic holdout 5.4% -> -3.5%).
- Holdout uses the same verdict (no PASS on thin evidence unless pre-registered via --holdout-min-full-years, default 1);
  horizon frozen (--holdout-end, default 2026-07-01 exclusive = through June); refuses incomplete session coverage;
  holdout runner, contract sizes and costs in the code digest.

## Rejected
- F493-07 (planner lacks --target-units): planner assumes full liquidity, size does not change planned contracts; adding it would
  force re-extraction. Real risk = exit spill beyond --buffer-sessions; check zone_coverage not_planned after the rebuild.

## Decisions for the user (unchanged list, now explicit flags)
1. --return-basis simple|cagr.  2. --lot-cap-unit reference|exchange.  3. Buffer-use action (reported only).
4. --gap-policy busy|free.  5. --holdout-end (July included?) and --holdout-min-full-years (1 full year acceptable?).
Set all before the first holdout run; they are frozen in portfolio_freeze.json.

## Still deferred
Five strikes / H1 / H2: run zone_coverage.py --tenor-check on the real options file first.
