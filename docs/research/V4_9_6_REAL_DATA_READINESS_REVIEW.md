# NIFTY IV RESEARCH V4.9.6 — real-data readiness review

**Review date:** 12 November 2026. **Scope:** the supplied V4.9.6 ZIP and notes; this is not a market-data backtest.

## Decision

**GO: start the first real-data contract extraction, monthly-tenor coverage study, and exploratory path/grid run.**
**CONDITIONAL GO: interpret portfolio output as an executable-account result, but not as an exhaustive proof of optimality.**
**NO-GO: final 2025–2026 holdout or live allocation until evidence and frozen assumptions are reviewed.**

The project should move to real data rather than restart a large synthetic patch/review loop. The two narrowly scoped issues below should be tracked, with their practical effect measured on real output.

## Verified in this review

- ZIP archive SHA-256 manifest: **63/63 files OK**.
- Focused tests: **33/33 PASS** with `ZONE_NO_NUMBA=1` (`StrategySpecV496`, `PathsV496`, `PortfolioV496`).
- Full suite: attempted, did not finish in the available execution window; the **498 PASS** figure is the release's own validation claim, not an independently reproduced full-suite result.
- No real options dataset was supplied; real fills, profitability, monthly coverage and liquidity were not independently measured.
- The source wires `--offset-mode levels`, `naked_m1`, `naked_m2`, `--target-margin`, table margins, executable replay qualification and ranking, monthly coverage checks, and holdout validation as described.

## F496-A — The fast screen is not a *safe* feasibility filter (important for optimization conclusions)

**Code:** `zone_portfolio.py`, `search()` and `run()`.

The vectorized screen first requires profit/floor targets, daily-close drawdown, margin and open-lot gates to pass at the **unadjusted candidate sizes** (`q=p3 & p4 & p5`). Only those screen-passing candidates reach the chronological account, which can **cut or reject orders** to satisfy funds and margin. This means the screen can discard combinations that the executable account would accept *after resizing*, including some that might meet the return and drawdown goals.

**Independent mathematical counterexample, with the actual search() function:** Starting capital ₹25 lakh; one green and one red candidate each initially requires ₹15 lakh margin and has an increasing ₹4 lakh annual profit at its full size. Together, the screen sees ₹30 lakh and rejects the pair despite 32% modeled combined yield. In a sequential-entry executable account that cuts the second leg from 10 to 6 lots, combined margin can be ₹24 lakh and a linear illustrative profit ₹6.4 lakh = **25.6%**, with no daily-close drawdown in the constructed example. `search()` actually returns `screen_passed=False`, `pass_target_and_floor=1`, `pass_all=0` on these arrays. The 25.6% executable outcome is an illustrative size arithmetic calculation, **not** a historical or independently executed `Account` backtest.

This is **not** a reason to stop extraction or the first real-data run. But a report saying “no qualifying portfolio exists” would not be logically justified by this screen. Audit candidate pairs rejected **only** on screen margin/drawdown, especially those meeting or nearly meeting profit/floor targets; replay a representative / ranked set through the account and report how often cuts turn the verdict around. If that frequently changes winners, replace the screen gates with conservative bounds or label the search heuristic, not exhaustive.

## F496-B — Bull put ITM/OTM labeling is reversed in the data-gap report

**Code:** `zone_coverage.py`, `moneyness(cp,offset)`.

The helper was written for the earlier signed-*point* offset convention. In the new `levels` convention, **positive level is OTM for both bull PE and bear CE**. Its current expression `(offset>0)==(cp=='PE')` mislabels bull PE levels:

| Side | Level | Actual strike at NIFTY 20,040 | Correct | Current `zone_coverage` label |
|---|---:|---:|---|---|
| bull PE | +5 | 19,000 | OTM | `ITM5` (incorrect) |
| bull PE | −5 | 21,000 | ITM | `OTM5` (incorrect) |
| bear CE | +5 | 21,000 | OTM | `OTM5` (correct) |
| bear CE | −5 | 19,000 | ITM | `ITM5` (correct) |

The issue is **diagnostic labeling**, not the strike computation or the actual path trades. Fix the helper to take the stored offset mode (from the extraction/path metadata) and in `levels` mode label +level as OTM, −level as ITM, 0 as ATM; preserve the older points interpretation for legacy data. Do this before publishing coverage by moneyness.

## Important assumptions and research caveats (not new code defects)

1. **Positions are limited by the assumed ₹25 lakh margin model, not by 10 exchange lots.** The target-margin path builder can simulate 21 × 50 (2022), 31 × 25 (mid-2024), 10 × 75 (2025), or 12 × 65 (2026). This is the approved *margin-budget* convention, not a literal 10-lot strategy.
2. **Margins are approximations**, not date-specific exchange/broker SPAN requirements or intraday peak margins. The margin table uses its upper-end historical estimates. Always label margin compliance “against model assumptions.”
3. **Daily-close drawdown ≤ ₹2.5 lakh is not a guaranteed loss cap.** Intraday MTM, overnight gap losses, slippage, exchange restrictions and stale option marks can produce worse outcomes. Use this as a research performance target, never a guaranteed maximum loss.
4. **Contract listing ≠ executable liquidity.** The next-month monthly strategy is especially exposed to 2021–24 listing gaps; report coverage separately by year, side, expiry policy, and strike/moneyness (correct the label issue first).
5. **Research-selection bias remains.** Candidate pools are top-`--pool` by historical metrics; the executable replay covers all passing *screen* candidates but not every underlying rule, and 22%/17%/₹2.5 lakh objectives are training gates, not promises of future results.
6. **Keep the 2025–26 final holdout untouched** until strike/tenor choice, ranking, sizing, costs, margins, gap policy, horizon, and other research decisions are frozen. Even repeated lookups or holdout-driven tweaks can invalidate a purported independent final test.
7. **Definition:** overall target is **22% simple annual yield on ₹25 lakh**, i.e. ₹5.5 lakh/year on average; each judged year ≥17% and total combined daily-close drawdown ≤₹2.5 lakh.

## Suggested real-data execution sequence

Use the project README for PostgreSQL connection, dates, data locations and holiday file. The commands below assume `zone_spot.sqlite` and `nse_holidays.txt` are present and that the source DB is available.

```bash
python zone_contracts.py --spot zone_spot.sqlite \
  --offset-mode levels --structures naked,naked_m1,naked_m2,calendar \
  --out zone_options_levels.sqlite
python zone_coverage.py --options zone_options_levels.sqlite --tenor-check \
  --out coverage_levels
```

**First inspect** `coverage_levels/coverage_tenor.csv` for 2021–2024: H1 eligibility/listed days and H2 listed days. If H2 is thin, retain the missingness statistics; do not treat unavailable trades as zero-P&L or call the strategy a failure. Then:

```bash
python zone_paths.py --options zone_options_levels.sqlite --spot zone_spot.sqlite \
  --offset-mode levels --structures naked,naked_m1,naked_m2,calendar,futures_zone,futures_expiry \
  --target-margin 2500000 --out zone_paths_levels.sqlite
python zone_coverage.py --options zone_options_levels.sqlite \
  --paths zone_paths_levels.sqlite --out coverage_paths
python zone_grid.py --paths zone_paths_levels.sqlite --holidays nse_holidays.txt \
  --workers 4 --out zone_grid_levels
python zone_portfolio.py --grid zone_grid_levels --paths zone_paths_levels.sqlite \
  --holidays nse_holidays.txt --workers 4 --return-basis simple \
  --margin-model table --max-margin 2500000 --qualify-max 0 \
  --if-none cash --out portfolio_levels
```

Do **not** invoke `zone_holdout.py` during these exploratory runs. For the first run, use the current frozen **2025-01-01 split**. Before subsequent refinements, compare coverage-conditioned results and the screen-vs-account qualification counts; inspect distributions of orders cut/rejected, realistic fills, stale prices, and cash years. Read the *actual executable* columns, not `screen_*` as account performance.

## Decision gates after the first real-data output

- **Coverage:** Enough listed and traded contracts for H1/H2 in each training year, with missingness by month, expiry, and moneyness. If not, report those strategy families as data-limited, not unprofitable.
- **Account:** Per-year simple returns, pooled total annualized simple yield, continuous daily-close drawdown, margin and buffer breaches, orders cut/rejected, actual exchange lots and contracts, and time spent in cash.
- **Selection sensitivity:** Run `--pool` sensitivity and test whether sample screen-rejected near misses turn executable after order resizing. A screen-only no-pass is not proof of absence of feasible portfolios.
- **Trading robustness:** Carry assumptions, alternative fills/slippage, stale-quote sensitivity and date-appropriate margins, preferably with broker confirmation.
- **Statistical robustness:** Multi-year walk-forward profitability, worst full year, return variability, number of trades and concentration, and a bootstrap interval if the effective sample supports it.
- **Holdout:** Run once only after data/code/settings are frozen, preserving independent evaluation of 2025–26.

**Bottom line:** GO for the real-data extraction and exploratory backtests. Do not call the optimizer mathematically exhaustive or the ₹2.5 lakh drawdown guaranteed. The two identified issues have small, targeted remedies/diagnostics and do not justify another general release cycle before obtaining market evidence.
