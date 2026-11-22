#!/usr/bin/env bash
# Removes old, superseded run outputs from ~/PycharmProjects/NIFTY_OPTIONS.
# Default is a DRY RUN: it lists what would go and the space freed.
# Delete for real with:   bash cleanup_outputs.sh --delete
#
# Never touched: *.py *.pine *.sql *.sh *.md *.txt *.zip, NIFTY_Data/, archive/,
# legacy_v1/, nifty_inspection_all/, PineScript/, docs/, migrations/, tests/,
# logs/, .idea/, V4_8_3, and every current-pipeline input or output
# (zone_spot.sqlite, zone_signals.json, zone_futures.sqlite, zone_options_levels.sqlite,
#  zone_paths_levels.sqlite, zone_grid_levels/, coverage_levels/, coverage_paths/,
#  research_cache_v4.sqlite, slippage_calibration/).
set -euo pipefail
cd ~/PycharmProjects/NIFTY_OPTIONS

TARGETS=(
  # old grids (pre-levels)
  zone_grid zone_grid484
  # superseded extractions / paths
  zone_options.sqlite zone_options_wide.sqlite zone_paths.sqlite
  # old trade DBs
  zone_trades_s05.sqlite zone_trades_s10.sqlite zone_trades_stop_15.sqlite
  zone_trades_stop_30.sqlite zone_trades_stop_45.sqlite zone_trades_stop_none.sqlite
  zone_trades_wide.sqlite
  # old pilot / fly runs
  pilot_v453_p05.sqlite pilot_v453_p10.sqlite pilot_v454_p05.sqlite pilot_v454_p10.sqlite
  fly_v454_s05.sqlite fly_v454_s10.sqlite
  pilot_exit_lag_v454_p05.json pilot_exit_lag_v454_p10.json spot_source_report.json
  # old report / rank / quality directories
  zone_report_futures zone_report_s05 zone_report_s10 zone_report_stop_15
  zone_report_stop_30 zone_report_stop_45 zone_report_stop_none zone_report_wide
  zone_report_wide_fixed
  pilot_rank_v453_p05 pilot_rank_v453_p10 pilot_rank_v454_p05 pilot_rank_v454_p10
  pilot_quality_v453_p05 pilot_quality_v453_p10
  fly_rank_v454_s05 fly_rank_v454_s10
  coverage_v4 contract_coverage_rissin
  # old top-level run logs (current-run logs are in logs/ and are kept)
  fly_rank_v454_s05.log fly_rank_v454_s10.log fly_v454_s05.log fly_v454_s10.log
  pilot_rank_v453_p05.log pilot_rank_v453_p10.log pilot_rank_v454_p05.log pilot_rank_v454_p10.log
  pilot_v453_p05.log pilot_v453_p10.log pilot_v454_p05.log pilot_v454_p10.log
  zone_bt_s05.log zone_bt_s10.log zone_bt_stop_15.log zone_bt_stop_30.log
  zone_bt_stop_45.log zone_bt_stop_none.log zone_bt_wide.log
  zone_report_futures.log zone_report_s05.log zone_report_s10.log zone_report_stop_15.log
  zone_report_stop_30.log zone_report_stop_45.log zone_report_stop_none.log
  zone_report_wide.log zone_report_wide_fixed.log
  zone_contracts.log zone_contracts_wide.log zone_futures.log zone_grid.log
  zone_paths.log zone_signals.log zone_spot.log
)

# Safety: refuse anything protected, even if added to the list by mistake.
for t in "${TARGETS[@]}"; do
  case "$t" in
    *.py|*.pine|*.sql|*.sh|*.md|*.txt|*.zip|NIFTY_Data*|logs|archive|legacy_v1|docs|tests|migrations|PineScript|nifty_inspection_all|*levels*|coverage_paths|zone_spot.sqlite|zone_signals.json|zone_futures.sqlite|research_cache_v4.sqlite|slippage_calibration|V4_8_3)
      echo "REFUSING protected path: $t"; exit 1;;
  esac
  if [ -d "$t" ] && find "$t" -type f \( -name '*.py' -o -name '*.md' -o -name '*.txt' -o -name '*.zip' -o -name '*.pine' -o -name '*.sql' -o -name '*.sh' \) | grep -q .; then
    echo "REFUSING $t: contains code/notes/zips"; exit 1
  fi
done

EXIST=()
for t in "${TARGETS[@]}"; do [ -e "$t" ] && EXIST+=("$t"); done
[ ${#EXIST[@]} -eq 0 ] && { echo "Nothing to remove."; exit 0; }

du -sh "${EXIST[@]}" | sort -h
echo "TOTAL: $(du -sch "${EXIST[@]}" | tail -1 | cut -f1) in ${#EXIST[@]} paths"

if [ "${1:-}" = "--delete" ]; then
  if pgrep -f 'zone_grid.py|zone_portfolio.py|zone_paths.py' >/dev/null; then
    echo "A pipeline step is still running; not deleting. Re-run when it finishes."; exit 1
  fi
  rm -rf -- "${EXIST[@]}"
  echo "Deleted."; df -h .
else
  echo "(dry run: nothing deleted. Add --delete to remove.)"
fi
