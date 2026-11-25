# Recorded baskets (2026-11-25)

Each basket is 5 green + red pairs, each pair at 1/5 of the capital, both legs of a pair at full size. Train = Sep 2021 - Dec 2025,
2026H1 = Jan - Jun 2026 (after the train period). The legs are in `config/baskets/<name>.csv` (pair_report columns), so a basket can
be re-reported from any pool that holds its legs:

    python scripts/pair_report.py --pairs config/baskets/<name>.csv --candidates <pool>/candidates.csv --quarters <pool>/quarters.csv

To regenerate a pick from scratch (same candidates, same rule), run the command shown; the code is unchanged for `--rank objective`,
and the earlier v4 score versions are kept as `--v4-version 1 | 2 | 3` (5 = current).

## v3_objective
Pool `runs/wide_search_v3/candidates.csv` (328 green, 347 red), pair_picker default (`--rank objective`, the wide search composite).

    python scripts/pair_picker.py --candidates runs/wide_search_v3/candidates.csv --quarters runs/wide_search_v3/quarters.csv --out runs/wide_search_v3/pairs_objective.csv

| # | Green | Red |
|---|---|---|
| 1 | naked -2 N3_k1.0_m12_j0.25_tw6 ptime5/atr20x1.25 | naked -1 N5_k0.25_m8_j1.25_tw5 none/mloss1.5 |
| 2 | naked -2 N3_k1.25_m8_j1.0_tw3 none/c:mloss2\|none\|exp2 | naked -2 N3_k0.25_m8_j1.5_tw2 target30x5/donch6c0.25 |
| 3 | naked -2 N3_k1.0_m12_j1.25_tw3 none/c:atr15x1\|none\|exp2 | naked -1 zone_only_tw2 tp90/stag1a1 |
| 4 | naked -2 N3_k1.25_m8_j0.75_tw2 tp75/c:mloss2\|none\|exp2 | naked -2 zone_only_tw3 tp70/atr20x1.25 |
| 5 | naked -2 N3_k1.0_m12_j1.5_tw3 ptime5/eatr30x1 | naked -1 N5_k0.5_m8_j1.5_tw5 ptime7/pct1 |

Basket train halves (2021H2..2025H2, %): 17 41 39 31 40 33 38 41 26; post-tax CAGR 46.2%; worst half 17.1%; 2026H1 +12%.

## v4pool1000_objective
Pool `runs/wide_search_v4/poolv3_1000/candidates.csv` (985 green, 994 red; stage1_pool --select v3 --top 1000 on wide_search_v4,
paths `runs/zone_paths_v4914.sqlite`), pair_picker default.

    python scripts/pair_picker.py --candidates runs/wide_search_v4/poolv3_1000/candidates.csv --quarters runs/wide_search_v4/poolv3_1000/quarters.csv --out runs/wide_search_v4/poolv3_1000/pairs_objective.csv

| # | Green | Red |
|---|---|---|
| 1 | vert_w1 +0 N3_k1.0_m20_j0.25_tw3 tpct3/stag1a1 | vert_w2 +0 N5_k0.25_m12_j1.25_tw5 none/atr15x1.25 |
| 2 | vert_w1 +0 N3_k1.0_m20_j0.5_tw3 tpct3/stag1a1 | vert_w2 +0 N5_k1.5_m12_j1.25_tw5 none/atr15x1.25 |
| 3 | naked -2 N3_k1.0_m12_j0.25_tw6 ptime5/atr15x1.25 | vert_w2 +0 N5_k1.25_m12_j1.5_tw5 ptime7/atr60x2 |
| 4 | vert_w2 +0 N3_k1.25_m12_j0.25_tw6 ptime5/chand6x1.5 | vert_w1 +0 N3_k1.5_m8_j1.25_tw3 tp90/stag2a0.5 |
| 5 | vert_w2 +0 N3_k1.0_m12_j0.75_tw3 tp80/atr30x1 | naked -1 zone_only_tw2 tp90/stag1a1 |

Basket train halves (%): 17 21 22 21 20 21 21 20 17; post-tax CAGR 27.3%; worst half 16.9%; 2026H1 -2%.
Pairs 1 and 2 are near-copies (k0.25/k1.5 red, j0.25/j0.5 green: almost the same halves).

## v4pool1000_v4v2
Same pool, v4 score version 2 (V4.9.20; version 3 picks the same five).

    python scripts/strategy_risk.py --candidates runs/wide_search_v4/poolv3_1000/candidates.csv --paths runs/zone_paths_v4914.sqlite --holidays config/nse_holidays.txt --out runs/wide_search_v4/poolv3_1000/risk.npz
    python scripts/pair_picker.py --candidates runs/wide_search_v4/poolv3_1000/candidates.csv --quarters runs/wide_search_v4/poolv3_1000/quarters.csv --risk runs/wide_search_v4/poolv3_1000/risk.npz --rank v4 --v4-version 2 --out runs/wide_search_v4/poolv3_1000/pairs_v4v2.csv

| # | Green | Red |
|---|---|---|
| 1 | naked -1 N3_k1.25_m12_j0.25_tw6 ptime5/chand6x1.5 | naked -2 zone_only_tw4 tpct2/atr60x1.25 |
| 2 | naked -2 N3_k0.5_m20_j0.25_tw4 tpct1/epct0.25 | naked -1 N5_k0.5_m12_j1.0_tw5 ptime7/atr20x1.25 |
| 3 | naked -2 N3_k1.0_m20_j0.25_tw4 tpct1/epct0.25 | naked -1 N5_k1.5_m12_j1.0_tw5 ptime7/atr20x1.25 |
| 4 | naked -2 N3_k1.0_m12_j1.25_tw3 none/c:atr15x1\|none\|exp2 | naked -1 zone_only_tw2 tp90/stag1a1 |
| 5 | naked -2 N3_k1.25_m12_j1.5_tw3 ptime5/eatr30x1 | vert_w2 +0 N5_k0.25_m12_j1.25_tw5 none/atr15x1.25 |

Basket train halves (%): 13 30 27 25 39 31 33 27 33; post-tax CAGR 38.6%; worst half 12.8%; 2026H1 -2%.
Pairs 2 and 3 are near-copies (k0.5/k1.0 green, k0.5/k1.5 red: the same halves except 2024H1).

## Earlier v3 floors run (not recorded leg by leg)
From the earlier pair_picker comparison (V4.9.16, 20-trade minimum): v3 pool, every half > 5% (2026H1 included), every quarter
> -2.5%. Its legs were not kept in this repo; regenerate with:

    python scripts/pair_picker.py --candidates runs/wide_search_v3/candidates.csv --quarters runs/wide_search_v3/quarters.csv --min-trades 20 --min-half 0.05 --min-quarter -0.025 --include-oos half --funnel --out runs/wide_search_v3/pairs_h5_q-0.025_half.csv
