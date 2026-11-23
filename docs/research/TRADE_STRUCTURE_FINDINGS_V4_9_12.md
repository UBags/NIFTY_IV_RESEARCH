# Trade-structure findings (V4.9.12, 2026-11-23)

Tool: `nifty-diag-trade-structure` (src/nifty_iv/diagnostics/trade_structure.py). Strategies: green naked -1
`N3_k1.25_m8_j1.5_tw2` ptime5/atr15x1, red naked -2 `N5_k0.25_m12_j0.25_tw5` ptime7/eatr30x1.5. Full size (scale 2.5 on the
40% paths file), Rs 25 L capital, pre-tax, (O+H+L+C)/4 fills at bar-close exits, no interest. In-sample = entries 2021-09..2024;
out of sample = 2025..2026H1 (already examined before; not clean). Search limited to these two strategies' entries; only exits
were searched (140 profit x stop pairs each).

## Baseline facts (ledger, 1x, conservative exits)
- Per trade: mean Rs 0.48 L, sd Rs 1.74 L, win 41%, payoff ~2.7. Sums of 8 random trades are positive 77% of the time, 16
  trades 87%: the observed 6/21 negative quarters and 3/11 negative halves are what trade-level noise predicts.
- For ~95% positive periods, mean/sd per trade must reach ~0.58 (quarters, ~8 trades) or ~0.41 (halves, ~16); it is 0.27.
- Position size changes the size of swings, not their sign.

## Exit baskets (5 slices unless noted)
| run | in-sample yr / sd / worst / neg | OOS yr / sd / worst / neg / DD | LOYO (held-out) sd / worst / neg | verdict |
|---|---|---|---|---|
| reference (own exits) | 86.8% / 26.4% / -7.0% / 1/7 halves | 44.5% / 25.5% / -4.3% / 1/3 / 36.3% | 27.0% / -7.0% / 1/7 | |
| K5 half, floor 35% | 37.2% / 7.0% / +8.0% / 0/7 | 32.1% / 15.3% / -0.2% / 1/3 / 24.2% | 20.2% / -3.5% / 1/7 | best; holds up |
| K5 half, floor 45% | 45.0% / 8.1% / +4.2% / 0/7 | 20.4% / 18.0% / -3.6% / 2/3 / 32.2% | 17.2% / -3.5% / 1/7 | worse OOS |
| K5 half, floor 55% | 56.7% / 11.1% / +3.8% / 0/7 | 28.6% / 23.1% / -4.8% / 1/3 / 34.4% | 16.1% / -3.5% / 1/7 | worse OOS |
| K3 half, floor 35% | 42.5% / 13.4% / +4.9% / 0/7 | 8.6% / 9.5% / -6.1% / 1/3 / 37.4% | 23.4% / -3.4% / 1/7 | weak |
| K5 quarter, floor 35% | 40.0% / 5.9% / 0.0% / 0/14 q | 20.2% / 14.6% / -19.3% / 3/6 q / 39.5% | 14.1% / -13.1% / 3/14 (ref 2/14, -7.0%) | overfit |

Chosen K5 half (floor 35%): green 5 x target15x1/atr15x1 (i.e. a better single exit); red 2 x target15x2/atr15x2,
1 x tp50/atr15x2, 1 x tpct1/atr15x2, 1 x ptime7/mloss1.5. Green/red half-year correlation about -0.2 to -0.4.

Lessons
- Exit baskets roughly halve return and cut half-year spread by ~25-40% on held-out data; in-sample smoothing overstates it.
- More slices = more robust (K5 > K3). Quarter objective overfits: perfect in-sample, worse than reference unseen.
- Every-quarter-positive is not reachable with two strategies' exits alone (~8 trades a quarter); needs more independent trades.
- Raising the return floor above 35% bought in-sample return but lost OOS stability.

## Sizing
- Meta-labelling (walk-forward L2 logistic, 10 signal-time features, terciles -> 4/8/12 lots): inconsistent. K5 half: tiers do
  not rank (low 20.3k / med 21.8k / high 19.3k). K3 half: low -10.8k vs high +25.6k. K5 quarter: low 4.0k vs med 35.5k /
  high 33.3k. Beats constant size at equal exposure in none of the robust runs.
- Stable feature signs across every year and run: zone_age negative (late entries in an old zone worse), mom45 negative
  (entries after an extended 45-bar move in the trade's direction worse). Candidate simple filter to test (2 parameters).
- Volatility sizing: within noise of constant size; adds a negative half in-sample. Drop.

## Literature (checked 2026-11-23)
- Meta-labelling (Lopez de Prado): secondary model sizes a primary signal; features must be known at signal time; practitioners
  report sizing models are as hard as signal models.
- Scaling out: reshapes the outcome distribution, smooths equity, reduces return (systematic tests on trend following).
- Volatility-managed sizing (Moreira-Muir): higher Sharpe in equities; for volatility selling, less selling in high vol helps
  (Yang 2024/25); can overshoot targets in some markets.
- VRP timing (IV vs RV): option selling does better when IV is elevated; no IV in the paths file yet (premium/ATR is a proxy).

## Next (agreed direction)
1. Redo the WIDE search (all strategies) with structure in mind: candidate pool from the scans, add strategies one at a time by
   held-out improvement of the worst half / spread (LOYO), preferring low correlation; baskets last.
2. Ledger basket replay (conservative fills, mloss7.5, interest) -> reinvest for the true post-tax IRR.
3. Test the zone_age / mom45 late-entry filter as a fixed rule under LOYO.
