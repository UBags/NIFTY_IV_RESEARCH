# Real-data pilot results, V4.5.4 (November 2026)

All figures are train-period only, net points per unit. No `final_holdout` has been run for either study. Fills are OHLC-minute proxies, not bid/ask executions.

## Status: no edge found in either structure; holdouts left unused

| Study | Data | Train window | Result |
|---|---|---|---|
| Put calendar, grid ATM, weekly, unhedged | `rissin_upstox`, 30d and 60d long | Nov 2024 to 26 Nov 2025 | 30d about zero. 60d +13.6 (t 1.17) at 10% cap, almost all from 18 Mar to 9 May 2025 (+71.4); other weeks -1.9 (t 0.09). Tuesday-expiry era negative in all four tenor and cap cells. SPA p 0.91 |
| ATM iron fly, 1.5 expected-move wings | `thetrademarkk`, June 2021 onward | To 21 Dec 2024 (181 weeks, Thursday era) | Mean: `tm1` -3.9, `p25_s50_tm1` -5.2, `weekly` -11.6. Costs about 9.2 per unit, so gross is about +5, +4 and -2.5. Nested walk-forward 27 trades, -1,754 points, Sharpe -1.89, four of five folds negative |

## Pilot design facts
- Far legs exist only in `rissin_upstox` from Oct 2024; `thetrademarkk` is mostly near-dated.
- A thin far long held after its short is bought back caused 1-7 days of naked-long drift. Tighter participation looked better only because of that. V4.5.4 adds `--max-exit-lag` and `exit_lag_report.py`.
- Spot is absent from both spot files for 2025-10-10, 2026-05-25, 05-26, 05-29, 06-01, 06-02, and 06-03 has 5 bars; option bars exist for the May-June dates. Entries stop at 2026-05-18.
- Expiry eras must be split by short expiry, not entry date (first Tuesday expiry 2025-09-02).
- `nse_holidays.txt`: 89 dates, 2021-2026; only data-gap days disagree with the cache.

## Decisions
- Do not spend either holdout: no selection deserves one.
- A loss on the short fly is not evidence for the long fly: gross is near zero and costs apply to both sides.
- Revisit the 60d calendar only after more Tuesday-era data accumulates, as a fresh test.
