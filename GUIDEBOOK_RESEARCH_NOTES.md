# Testing the Money Press guidebook on NIFTY

Research plan only. The guidebook is treated as a set of testable claims, not as evidence. It reports no audited track record, and its examples are hand-picked single stocks.

## What the method is
A put diagonal: sell a near-the-money weekly put, buy a longer-dated (about 3-6 months) put at a lower strike, resell a new weekly after each expiry, usually for 8-12 weeks. The guidebook's stated examples use protection about 5-15% below the short strike.

## What V4.3 got wrong against it
V4.3 entered every campaign at one strike. The guidebook's structure has a lower long strike and a bullish tilt. V4.4 adds this as `--long-strike-offsets`.

## Claims that fail on the guidebook's own arithmetic
| Claim | Check |
|---|---|
| Weekly premium summed over 26 weeks is "retail", 6-month premium is "bulk" | Its numbers are the square-root-of-time rule: 3.10 x sqrt(26) = 15.81 against a quoted 15.70. Likewise 4 x sqrt(4) = 8 against 7.80-8.20. At the same implied volatility nothing is mispriced |
| Four weeklies pay double one monthly with no added risk | The extra premium pays for selling short-dated gamma four times; each week can lose |
| 17 weeks x $1.60 = $27.20 of income | That is more than the $22.23 stock. It counts every premium as kept. An at-the-money weekly often ends in the money |
| Risk is the strike difference | It omits the debit paid: on its example 3.50 + 1.80, before any value left in the long |
| Risk-free after 2-3 weeks | Only on gross premium. V4.4 measures this on gross and on net |
| Rich premium when markets are choppy is a silver lining | The same conditions are when an at-the-money short loses most |

## Reasons the method may transfer poorly
- It says ETFs mostly disappoint. NIFTY is an index, closer to an ETF than to the $300+ single stocks it prefers.
- A key lesson is fitting between earnings. An index has no earnings; budgets, RBI and elections are the nearest analogue, and need an event calendar.
- NIFTY put skew makes lower-strike protection relatively expensive in volatility terms.
- It gives no loss-handling rule. V4.4 predeclares one: skip the sale when the at-the-money strike would be at or below the long.
- "Tip-Toe Burglar" and "Sleepover Bandit" are named, not defined. No rule is invented for them here.

## Predeclared hypotheses (fix these before seeing results)
Use weekly entries, one tenor set, and the same fill model throughout.
1. **H1 diagonal vs calendar.** Paired on the same day, short strike and policy, a diagonal beats the same-strike calendar per index unit after costs.
2. **H2 premium retention.** `premium_retained` is materially above zero after buybacks and settlements.
3. **H3 payback.** `reached_risk_free_on_net` is close to `reached_book_risk_free_on_gross`. A large gap means the claim is a gross-premium artefact.
4. **H4 weekly hit rate.** `cycle_win_rate` reaches the claimed 70-80%, and winning cycles are not outweighed by losing ones.
5. **H5 source of profit.** `theta` and `vega` dominate `pnl_attribution`, rather than `delta`. A bullish structure in a rising market should be presumed directional until shown otherwise.
6. **H6 roll timing.** `expiry_day_close` adds value over `next_session` after overnight risk.
7. **H7 market cycle.** Entries with `spot_to_sma50` or `trend_21d` above a threshold fixed in advance do better than unfiltered ones. Report the shift control.

Reject a hypothesis if it fails in nested walk-forward, the untouched holdout, or the adverse fill model. Do not retune on the holdout.
