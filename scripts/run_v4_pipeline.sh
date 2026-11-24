#!/usr/bin/env bash
# run_v4_pipeline.sh: the V4.9.14+ pipeline end to end, safe to rerun at any time.
#
#   1/5  combo legs: PostgreSQL extraction -> combo paths -> merge into runs/zone_paths_v4914.sqlite
#   2/5  wide search on the merged paths file (stage 1 resumes group by group)
#   3/5  quarter returns of the wide-search pool
#   4/5  v3-rule pool, up to 1000 strategies per side
#   5/5  pairs (top 5 with funnel + LOYO + quarter report, top 50) for both pools
#
# Usage (from anywhere):
#   bash scripts/run_v4_pipeline.sh                 # update code, check everything, run detached, print the log path
#   bash scripts/run_v4_pipeline.sh --follow        # same, then follow the log until the run ends
#   bash scripts/run_v4_pipeline.sh --status        # is a run going on? last lines of the latest log
#
# Options:
#   --no-pull          skip git fetch / ff-merge of origin/$BRANCH and pip install -e ".[fast]"
#   --foreground       run in this terminal instead of nohup
#   --follow           after launching, follow the log (tail ends when the run ends)
#   --workers N        wide-search workers (default 4)
#   --from N           start at step N (1-5); earlier steps must already be complete
#   --force            rerun steps 2-5 even if complete (stage-1 group files are still reused)
#   --fresh-search     delete runs/wide_search_v4 first (wide search from scratch; keeps the merged paths file)
#   --rebuild-paths    delete the merged + combo paths files and rebuild them (keeps the resumable PG extraction)
#   --rebuild-all      also delete the PG extraction runs/zone_options_combos.sqlite (full re-extraction)
#   --clean            delete superseded outputs (listed in a dry run on every start; see clean_targets)
#   --status           show whether a run is active and the tail of the latest log, then exit
#   --stop             stop a running pipeline (its whole process group, worker processes included), then exit
#   -h | --help        this text
#
# Every step leaves a marker in runs/wide_search_v4/.pipeline/ when it finishes, so a rerun skips finished work and
# redoes anything that was interrupted. Logs: runs/logs/v4_pipeline_<timestamp>.log (latest: runs/logs/v4_pipeline_latest.log).
# Exit codes: 0 ok, 1 failure, 2 another run is active / bad usage, 3 finished but some pair picks found no qualifying pair.

main() {
set -Eeuo pipefail

# ------------------------------------------------------------------ settings
ROOT="${NIFTY_ROOT:-$HOME/PycharmProjects/NIFTY_OPTIONS}"
BRANCH="${NIFTY_BRANCH:-equity-dd-scan}"
export PGHOST="${PGHOST:-localhost}" PGPORT="${PGPORT:-5432}" PGDATABASE="${PGDATABASE:-nifty_data}" PGUSER="${PGUSER:-postgres}"
export PGPASSWORD="${PGPASSWORD:-postgres}"          # local server; an exported PGPASSWORD or ~/.pgpass entry still wins if set
export PYTHONUNBUFFERED=1                            # progress lines reach the log as they happen (python buffers under nohup)

BASE=runs/zone_paths_levels.sqlite                   # existing levels paths file (inputs of the combo build)
OPTS_LEVELS=runs/zone_options_levels.sqlite          # only used to size the disk check
OCOMBO=runs/zone_options_combos.sqlite               # PG extraction of the combo legs (resumable month by month)
PCOMBO=runs/zone_paths_combos.sqlite                 # combo + carry-futures paths (written atomically by nifty-zone-paths)
MERGED=runs/zone_paths_v4914.sqlite                  # base + combos; built as $MERGED.merging and renamed when verified
OUT=runs/wide_search_v4
POOL=$OUT/poolv3_1000
H=config/nse_holidays.txt
LOGDIR=runs/logs
LOCK=runs/.v4_pipeline.lock
PIDF=runs/.v4_pipeline.pid
MARK=$OUT/.pipeline
OTHER_JOBS='(^|/)(nifty-diag-wide-search|nifty-zone-contracts|nifty-zone-paths|stage1_pool\.py|strategy_periods\.py|pair_picker\.py|merge_paths\.py|v4_pipeline\.sh)$'

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
PULL=1 FG=0 FOLLOW=0 WORKERS=4 FROM=1 FORCE=0 FRESH=0 REBUILD_PATHS=0 REBUILD_ALL=0 CLEAN=0 WORKER=0 STATUS=0 STOP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) PULL=0;; --foreground) FG=1;; --follow) FOLLOW=1;; --force) FORCE=1;; --clean) CLEAN=1;; --status) STATUS=1;; --stop) STOP=1;;
    --fresh-search) FRESH=1;; --rebuild-paths) REBUILD_PATHS=1;; --rebuild-all) REBUILD_ALL=1; REBUILD_PATHS=1;;
    --workers) WORKERS="${2:?--workers N}"; shift;;
    --from) FROM="${2:?--from N}"; shift;;
    --_worker) WORKER=1;;
    -h|--help) sed -n '2,/^# Exit codes/p' "$SELF" | sed 's/^# \{0,1\}//'; return 0;;
    *) echo "unknown option: $1 (see --help)"; return 2;;
  esac; shift
done
[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo "--workers: a positive integer"; return 2; }
[[ "$FROM" =~ ^[1-5]$ ]] || { echo "--from: 1 to 5"; return 2; }

stamp(){ echo "=== $(date '+%F %T') $*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }
gb(){ du -sh "$@" 2>/dev/null | awk '{print $1}' | paste -sd' '; }

cd "$ROOT" 2>/dev/null || die "project root $ROOT not found"
[ -f pyproject.toml ] && [ -d src/nifty_iv ] || die "$ROOT is not the NIFTY_IV_RESEARCH checkout"
mkdir -p "$LOGDIR"

if [ -z "${VIRTUAL_ENV:-}" ]; then
  [ -f .venv/bin/activate ] || die "no active venv and no $ROOT/.venv"
  # shellcheck disable=SC1091
  set +u; . .venv/bin/activate; set -u
fi

others(){                             # processes whose program or script (first 3 words) is a pipeline tool
  ps -eo pid=,args= | awk -v re="$OTHER_JOBS" -v me=$$ '$1!=me{for(i=2;i<=4&&i<=NF;i++)if($i~re){print;next}}' || true
}

if [ $STOP = 1 ]; then
  pid=$(cat "$PIDF" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    pg=$(ps -o pgid= -p "$pid" | tr -d ' '); echo "stopping run pid $pid (process group $pg)"
    kill -TERM -- "-$pg" 2>/dev/null || kill -TERM "$pid"
    for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && { echo "still alive after 30 s: kill -KILL -- -$pg"; kill -KILL -- "-$pg"; }
    echo "stopped. Rerun the same command to resume."
  else
    o=$(others); [ -z "$o" ] && { echo "no pipeline running"; return 0; }
    echo "no run started by this script is active, but these are:"; echo "$o"; echo "stop them with: pkill -f <name>"
  fi
  return 0
fi

if [ $STATUS = 1 ]; then
  o=$(others); if [ -n "$o" ] || ! flock -n "$LOCK" true 2>/dev/null; then echo "RUNNING:"; echo "${o:-  (lock held)}"; else echo "no pipeline step running"; fi
  [ -e "$LOGDIR/v4_pipeline_latest.log" ] && { echo "--- $(readlink -f "$LOGDIR/v4_pipeline_latest.log")"; tail -n 15 "$LOGDIR/v4_pipeline_latest.log"; }
  ls "$MARK" 2>/dev/null | sed 's/^/done: /' || true
  return 0
fi

# ------------------------------------------------------------------ superseded outputs (--clean)
clean_targets(){                       # print paths that are safe to delete, one per line
  local n_old n_new f
  # 48 GB V4.9.12 grid: replaced by runs/grid_v4913 (verified identical, 2,682 files); tools that verify a grid reject the old one
  if [ -d runs/zone_grid_levels ] && [ -d runs/grid_v4913 ]; then
    n_old=$(find runs/zone_grid_levels -maxdepth 1 -name 'grid_*.npz' | wc -l)
    n_new=$(find runs/grid_v4913 -maxdepth 1 -name 'grid_*.npz' | wc -l)
    [ "$n_old" -gt 0 ] && [ "$n_new" -ge "$n_old" ] && echo runs/zone_grid_levels
  fi
  # wide-search runs 1 and 2 (findings recorded in docs, parked); v3 is kept (the C1-C5 reference)
  for f in runs/wide_search runs/wide_search_v2; do [ -d "$f" ] && echo "$f"; done
  # stray copies at the project root of logs that also exist, identical, under runs/
  for f in ledger_30min_p0.15.log ledger_30min_p0.20.log; do [ -f "$f" ] && cmp -s "$f" "runs/$f" && echo "$f"; done
  return 0
}

# ================================================================== launcher (foreground part)
if [ $WORKER = 0 ]; then
  stamp "v4 pipeline launcher in $ROOT"
  o=$(others)
  if [ -n "$o" ] || ! flock -n "$LOCK" true 2>/dev/null; then
    echo "A pipeline step is already running:"; echo "${o:-  (lock $LOCK held)}"
    echo "Wait for it, or stop it: bash scripts/run_v4_pipeline.sh --stop  (a run of the old runs/v4_pipeline.sh: pkill -f v4_pipeline.sh; pkill -f nifty-diag-wide-search)"; exit 2
  fi

  if [ $PULL = 1 ]; then
    stamp "code: git fetch + ff-merge origin/$BRANCH, pip install -e .[fast]"
    git fetch -q origin || die "git fetch failed (network / ssh key?); rerun with --no-pull to use the current code"
    git merge --ff-only -q "origin/$BRANCH" || die "cannot fast-forward to origin/$BRANCH (local commits or edits); fix, or rerun with --no-pull"
    pip install -q -e ".[fast]" > "$LOGDIR/pip_install.log" 2>&1 || { cat "$LOGDIR/pip_install.log"; die "pip install -e .[fast] failed"; }
    grep -v 'textract\|extract-msg\|~\|^ *$' "$LOGDIR/pip_install.log" | sed 's/^/  pip: /' || true   # the textract warning is harmless
    echo "  $(git log --oneline -1)"
  fi
  python - <<'PY' || die "package check failed (pip install -e \".[fast]\" in the venv)"
import nifty_iv,importlib.util as u
print(f"  nifty_iv {nifty_iv.__version__}; numba {'yes' if u.find_spec('numba') else 'NO (slow: pip install -e .[fast])'}")
PY
  command -v nifty-diag-wide-search >/dev/null || die "nifty-diag-wide-search not on PATH (venv not active?)"
  [ -f "$H" ] || die "missing $H"

  # resets requested on the command line (irreversible: say what goes)
  if [ $REBUILD_ALL = 1 ]; then echo "  --rebuild-all: removing $OCOMBO ($(gb "$OCOMBO"))"; rm -f "$OCOMBO"; fi
  if [ $REBUILD_PATHS = 1 ]; then echo "  --rebuild-paths: removing $MERGED $PCOMBO"; rm -f "$MERGED" "$MERGED.merging" "$PCOMBO" "$PCOMBO.building"; fi
  if [ $FRESH = 1 ] && [ -d "$OUT" ]; then echo "  --fresh-search: removing $OUT ($(gb "$OUT"))"; rm -rf "$OUT"; fi

  # superseded outputs
  mapfile -t CT < <(clean_targets)
  if [ ${#CT[@]} -gt 0 ]; then
    if [ $CLEAN = 1 ]; then
      stamp "--clean: deleting ${#CT[@]} superseded paths"; du -sh "${CT[@]}" | sed 's/^/  /'; rm -rf -- "${CT[@]}"
    else
      echo "  superseded outputs ($(du -sch "${CT[@]}" | tail -1 | cut -f1)): ${CT[*]}  -> add --clean to delete them"
    fi
  fi

  # what step 1 will need
  NEED_BUILD=0; [ -f "$MERGED" ] || NEED_BUILD=1
  if [ $NEED_BUILD = 1 ]; then
    [ $FROM -le 1 ] || die "--from $FROM but $MERGED does not exist yet: start from step 1"
    [ -f "$BASE" ] || die "missing $BASE (the levels paths file the combos are built against)"
  fi

  # disk: extraction <= levels extraction, combo paths + merged <= 2 x base paths, searches a few GB
  need=$(python - "$NEED_BUILD" "$OPTS_LEVELS" "$BASE" <<'PY'
import os,sys
b=sys.argv[1]=='1';sz=lambda p:os.path.getsize(p) if os.path.exists(p) else 0
print(int((sz(sys.argv[2])+2*sz(sys.argv[3]) if b else 0)*1.3+8e9))
PY
)
  free=$(df -B1 --output=avail runs | tail -1 | tr -d ' ')
  echo "  disk: need about $((need/1000000000)) GB, free $((free/1000000000)) GB"
  [ "$free" -gt "$need" ] || die "not enough free disk on $(df --output=target runs | tail -1); free space (try --clean) and rerun"

  # PostgreSQL: only the extraction reads it
  if [ $NEED_BUILD = 1 ]; then
    stamp "PostgreSQL check ($PGUSER@$PGHOST:$PGPORT/$PGDATABASE)"
    if command -v pg_isready >/dev/null && ! pg_isready -q -h "$PGHOST" -p "$PGPORT"; then
      die "PostgreSQL is not accepting connections on $PGHOST:$PGPORT (start it: sudo systemctl start postgresql)"
    fi
    python - <<'PY' || die "PostgreSQL connection or query failed (password? database? see the message above)"
from nifty_iv.data.research_cache import connect
with connect() as pg:
    lo,hi=pg.execute("select min(trading_day),max(trading_day) from market_data.option_bars").fetchone()
print(f"  connection OK; market_data.option_bars {lo} .. {hi}")
PY
  fi

  FWD=(--_worker --workers "$WORKERS" --from "$FROM"); [ $FORCE = 1 ] && FWD+=(--force)
  if [ $FG = 1 ]; then
    exec bash "$SELF" "${FWD[@]}"
  fi
  LOG="$LOGDIR/v4_pipeline_$(date +%Y%m%d_%H%M%S).log"
  nohup setsid -w bash "$SELF" "${FWD[@]}" >"$LOG" 2>&1 </dev/null &
  WPID=$!
  ln -sfn "$(basename "$LOG")" "$LOGDIR/v4_pipeline_latest.log"
  sleep 2
  if ! kill -0 "$WPID" 2>/dev/null; then                         # already over: everything was done, or it failed at once
    rc=0; wait "$WPID" || rc=$?; tail -n 25 "$LOG"
    [ $rc = 0 ] && { stamp "finished (nothing left to do)"; exit 0; }
    die "the run ended with exit $rc (log above: $ROOT/$LOG)"
  fi
  stamp "running detached, pid $WPID"
  echo "  log:    $ROOT/$LOG"
  echo "  follow: tail -f $ROOT/$LOGDIR/v4_pipeline_latest.log"
  echo "  status: bash scripts/run_v4_pipeline.sh --status      stop: bash scripts/run_v4_pipeline.sh --stop"
  [ $FOLLOW = 1 ] || exit 0
  tail -n +1 -f "$LOG" & TP=$!
  trap 'kill $TP 2>/dev/null; echo; echo "(stopped following; the run continues: --status / --stop)"; exit 0' INT
  rc=0; wait "$WPID" || rc=$?          # reaps the run and gives its exit code
  sleep 1; kill $TP 2>/dev/null || true
  exit $rc
fi

# ================================================================== worker
exec 9>"$LOCK"; flock -n 9 || die "another run holds $LOCK"
echo $$ > "$PIDF"
T0=$SECONDS; PAIR_FAIL=0
trap 'rc=$?; stamp "FAILED (exit $rc) at line $LINENO: $BASH_COMMAND"; exit $rc' ERR
trap 'stamp "stopped by signal"; exit 130' INT TERM
stamp "v4 pipeline: $(git log --oneline -1 2>/dev/null || echo 'no git'); workers $WORKERS; from step $FROM"
echo "  python $(command -v python); PG $PGUSER@$PGHOST:$PGPORT/$PGDATABASE"

# temporaries of interrupted runs (nothing else is running: the launcher checked and we hold the lock)
for f in "$MERGED.merging" "$PCOMBO.building"; do [ -e "$f" ] && { echo "  removing leftover $f"; rm -f "$f"; }; done
[ -d "$OUT/stage1" ] && find "$OUT/stage1" -name '*.tmp' -delete
mkdir -p "$MARK"

# adopt a run of the earlier runs/v4_pipeline.sh (no markers): only if its log ends with its final "done" line, which it printed
# only when every step succeeded (set -e), and the log is newer than the merged paths file it ran on
OLDLOG=runs/v4_pipeline.log
if [ -z "$(ls -A "$MARK")" ] && [ -f "$OLDLOG" ] && [ -f "$MERGED" ] && [ "$OLDLOG" -nt "$MERGED" ] \
   && tail -n 1 "$OLDLOG" | grep -qE '^=== [0-9-]+ [0-9:]+ done$'; then
  stamp "adopting the completed run logged in $OLDLOG (steps 2-5 marked done; --force to redo)"
  [ -s "$OUT/candidates.csv" ] && [ -f "$OUT/final.json" ] && touch "$MARK/wide_search"
  [ -s "$OUT/quarters.csv" ] && touch "$MARK/quarters"
  [ -s "$POOL/candidates.csv" ] && [ -s "$POOL/quarters.csv" ] && touch "$MARK/pool"
  [ -s "$OUT/pairs_top50.csv" ] && touch "$MARK/pairs_$(basename "$OUT")"
  [ -s "$POOL/pairs_top50.csv" ] && touch "$MARK/pairs_$(basename "$POOL")"
fi

done_after(){ [ $FORCE = 0 ] && [ -f "$MARK/$1" ] && [ "$MARK/$1" -nt "$2" ]; }   # marker exists and is newer than the input
mark(){ touch "$MARK/$1"; }
took(){ echo "  ($(( (SECONDS-$1)/60 )) min)"; }

# ---------------------------------------------------------------- 1/5
if [ $FROM -le 1 ]; then
  if [ -f "$MERGED" ]; then stamp "1/5 $MERGED exists: build skipped"
  else
    t=$SECONDS; stamp "1/5 combo legs: extraction + paths + merge"
    python scripts/combo_build_cmds.py --paths "$BASE" --options-out "$OCOMBO" --paths-out "$PCOMBO" --merged-out "$MERGED.merging" > runs/combo_build.sh
    sed 's/^/  | /' runs/combo_build.sh
    C1=$(grep -m1 '^nifty-zone-contracts ' runs/combo_build.sh) || die "no extraction command in runs/combo_build.sh"
    C2=$(grep -m1 '^nifty-zone-paths ' runs/combo_build.sh)     || die "no paths command in runs/combo_build.sh"
    C3=$(grep -m1 '^python scripts/merge_paths.py ' runs/combo_build.sh) || die "no merge command in runs/combo_build.sh"
    stamp "1a extraction from PostgreSQL -> $OCOMBO (resumes month by month)"
    bash -c "set -euo pipefail; $C1"
    if [ -f "$PCOMBO" ]; then stamp "1b $PCOMBO exists: paths build skipped (--rebuild-paths to redo)"
    else stamp "1b combo paths -> $PCOMBO"; bash -c "set -euo pipefail; $C2"; fi
    stamp "1c merge -> $MERGED"
    bash -c "set -euo pipefail; $C3"
    python - "$MERGED.merging" <<'PY'
import sqlite3,sys
from nifty_iv.core import strategy_spec as ss
db=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True)
ok=db.execute('PRAGMA quick_check').fetchone()[0]
if ok!='ok':sys.exit(f'merged file failed sqlite quick_check: {ok}')
st={r[0]:r[1] for r in db.execute('SELECT structure,COUNT(*) FROM paths GROUP BY 1')}
miss=[s for s in ss.COMBO_STRUCTURES if s not in st]
print('  merged paths by structure: '+', '.join(f'{k} {v:,}' for k,v in sorted(st.items())))
if not any(s in st for s in ss.COMBO_STRUCTURES):sys.exit('merged file holds none of the combo structures')
if miss:print(f'  note: no paths for {miss} (no qualifying entries or no data)')
PY
    mv -f "$MERGED.merging" "$MERGED"
    echo "  $MERGED $(gb "$MERGED")"; took $t
  fi
fi
[ -f "$MERGED" ] || die "$MERGED missing: run from step 1"
need_done(){ [ -f "$MARK/$1" ] || die "--from $FROM: step $2 has not finished (no $MARK/$1); start from step $2"; }
[ $FROM -le 2 ] || need_done wide_search 2
[ $FROM -le 3 ] || need_done quarters 3
[ $FROM -le 4 ] || need_done pool 4

# ---------------------------------------------------------------- 2/5
if [ $FROM -le 2 ]; then
  if done_after wide_search "$MERGED" && [ -s "$OUT/candidates.csv" ]; then stamp "2/5 wide search complete: skipped (--force to redo)"
  else
    t=$SECONDS; stamp "2/5 wide search (all structures; stage 1 resumable) -> $OUT"
    rm -f "$MARK"/{wide_search,quarters,pool,pairs_*}
    nifty-diag-wide-search --paths "$MERGED" --holidays "$H" --workers "$WORKERS" --out "$OUT"
    [ -s "$OUT/candidates.csv" ] || die "the wide search kept no candidates ($OUT/candidates.csv missing): see its stage-1 lines above"
    mark wide_search; took $t
  fi
fi

# ---------------------------------------------------------------- 3/5
if [ $FROM -le 3 ]; then
  if done_after quarters "$OUT/candidates.csv" && [ -s "$OUT/quarters.csv" ]; then stamp "3/5 quarters complete: skipped"
  else
    t=$SECONDS; stamp "3/5 quarter returns of the wide-search pool -> $OUT/quarters.csv"
    rm -f "$OUT/quarters.csv"
    python scripts/strategy_periods.py --candidates "$OUT/candidates.csv" --paths "$MERGED" --holidays "$H" --out "$OUT/quarters.csv"
    [ -s "$OUT/quarters.csv" ] || die "no $OUT/quarters.csv written"
    mark quarters; took $t
  fi
fi

# ---------------------------------------------------------------- 4/5
if [ $FROM -le 4 ]; then
  if done_after pool "$OUT/candidates.csv" && [ -s "$POOL/candidates.csv" ] && [ -s "$POOL/quarters.csv" ]; then stamp "4/5 v3-rule pool complete: skipped"
  else
    t=$SECONDS; stamp "4/5 v3-rule pool, up to 1000 per side -> $POOL"
    rm -rf "$POOL"
    python scripts/stage1_pool.py --run "$OUT" --paths "$MERGED" --holidays "$H" --select v3 --top 1000 --out "$POOL"
    [ -s "$POOL/candidates.csv" ] && [ -s "$POOL/quarters.csv" ] || die "stage1_pool wrote no candidates/quarters in $POOL"
    mark pool; took $t
  fi
fi

# ---------------------------------------------------------------- 5/5
t=$SECONDS; stamp "5/5 pairs"
for pool in "$OUT" "$POOL"; do
  tag=pairs_$(basename "$pool")
  if done_after "$tag" "$pool/quarters.csv" && [ -s "$pool/pairs_top50.csv" ]; then echo "----- $pool: pairs complete, skipped"; continue; fi
  echo "----- $pool"
  rm -f "$pool"/pairs_top5.csv "$pool"/pairs_top5_quarters.csv "$pool"/pairs_top50.csv
  ok=1
  python scripts/pair_picker.py --candidates "$pool/candidates.csv" --quarters "$pool/quarters.csv" --funnel --loyo --out "$pool/pairs_top5.csv" || ok=0
  if [ $ok = 1 ]; then
    python scripts/pair_report.py --pairs "$pool/pairs_top5.csv" --candidates "$pool/candidates.csv" --quarters "$pool/quarters.csv" --csv "$pool/pairs_top5_quarters.csv" || ok=0
  else echo "  top-5 pick found no qualifying pair (funnel above); report skipped"; fi
  python scripts/pair_picker.py --candidates "$pool/candidates.csv" --quarters "$pool/quarters.csv" --top 50 --loyo --out "$pool/pairs_top50.csv" || ok=0
  if [ $ok = 1 ]; then mark "$tag"; else PAIR_FAIL=1; echo "  WARNING: a pair step failed for $pool (see above); the other pool still runs"; fi
done
took $t

trap - ERR
stamp "done in $(( (SECONDS-T0)/60 )) min"
echo "  outputs:"
for f in "$OUT"/{final.json,candidates.csv,quarters.csv,pairs_top5.csv,pairs_top5_quarters.csv,pairs_top50.csv} \
         "$POOL"/{candidates.csv,quarters.csv,pairs_top5.csv,pairs_top5_quarters.csv,pairs_top50.csv}; do
  [ -e "$f" ] && echo "    $f"
done
[ $PAIR_FAIL = 0 ] || { echo "  finished with pair-step warnings (exit 3)"; exit 3; }
exit 0
}

main "$@"; exit $?      # the whole body is parsed before it runs, so a git merge that rewrites this file mid-run is harmless
