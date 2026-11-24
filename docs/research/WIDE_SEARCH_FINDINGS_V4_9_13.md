# Wide search findings (V4.9.13, 2026-11-24) — parked

Real paths file, train 2021-09..2024, out of sample (OOS) 2025-01..2026-06 (seen before: a check, not a clean test).
Returns on Rs 25 L, full size, pre-tax, before interest.

## Run 1 (no risk limits)
Pool 1,092 / 1,108 bull futures. Account = one bull futures_zone strategy: train 71.8%/yr, every half positive;
2026H1 -57.5%. Leave-one-year-out (LOYO) held-out halves: 3/7 negative, worst -89%, mean 1.1%. Calendars untestable (paths fill from Nov 2024).

## Run 2 (trade loss x size <= 3%, account DD <= 20%, 40 per side x structure, robust ranking)
- 1,045 pool candidates re-checked exactly: 0 differences (worst trade included).
- Account: 6 strategies, all at 0.25 size (the 3% trade limit forces it: full-size worst trades are -7.6% to -10.5%):
  bear futures_zone tw5 target15x5/atr60x1.25; bear naked -2 tw3 tp70/stag1a0.5 and tp75/stag1a0.5 (near-duplicates);
  bear naked -1 tw2 none/pct0.75; bull futures_zone N3_k1.0_m8_j1.25_tw6 tpct3/pct0.75; bull futures_zone N5_k0.25_m8_j0.25_tw6 tpct3/pts100.
- Train: 95.4%/yr, every half positive (worst 38.6%), max DD 19.6%.
- OOS: 2025H1 -15.6%, 2025H2 +4.7%, 2026H1 -29.4%; max DD 59.8%. The two bull futures legs lose ~-52%/yr OOS.
- LOYO held-out halves: 2/7 negative, worst -35.8%, mean 14.5% (run 1: worst -89%, mean 1.1%).
- Exit baskets change almost nothing (they pick the single exit in most slices).

## Pool by family (median, single exit)
| family | n | train %/yr | OOS %/yr | all 3 OOS halves > 0 |
|---|---|---|---|---|
| bear naked | 157 | 45 | +14 | 21 |
| bear naked_m2 | 120 | 8 | +9 | 7 |
| bear futures_zone | 148 | 46 | +3 | 0 |
| bear futures_expiry | 156 | 50 | -2 | 1 |
| bull naked | 154 | 52 | -21 | 1 |
| bull futures_expiry | 159 | 60 | -26 | 6 |
| bull futures_zone | 151 | 104 | -50 | 0 |

## Reading
- Bull strategies of every structure learned the 2021-24 uptrend and fail in 2025-26; bear naked is the only family that holds up OOS.
- The account objective (worst train half) still rewards trend exposure; selection noise is still large (LOYO worst -36%).
- Universe has no defined-risk option structure: every naked trade's worst open loss is ~10% of capital at full size, so the 3% rule caps all to 0.25 size.

## Open options (not decided)
1. Vertical spreads (defined risk) as a new structure: paths rebuild.
2. Side balance in the account (bull exposure <= bear, or delta-neutral pairing).
3. Score candidates on LOYO consistency / regime split rather than the train worst half.
4. Bear naked only, then a small account from it.
