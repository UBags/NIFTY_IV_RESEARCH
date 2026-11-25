"""Print the commands that build a separate date block (V4.9.18: 2017-2020) with the same settings as existing paths files, so the
block's paths can go through the same search as the main period.

    python scripts/block_build_cmds.py --like runs/zone_paths_levels.sqlite runs/zone_paths_combos.sqlite > runs/b1720_build.sh
    cat runs/b1720_build.sh && bash runs/b1720_build.sh

The block is its own spot file, extraction and paths file: the database has no data for January-May 2021, so the 2017-2020 bars
and the 2021+ bars must not be joined (nifty-zone-spot refuses that). Settings (sizing, fills, charges, levels, entry window) come from
the --like files' recorded arguments; their structures are combined (they must agree on every other setting). The option blackout
(config/option_blackouts.csv) applies in the extraction. Trading starts at --from-date; spot from --spot-from warms up the EMAs
(the slowest is a 48-period EMA on 360-minute bars, about one bar per session: by July 2017 a start-up value has decayed below 1%).

1. nifty-zone-spot: spot minutes of the block.
2. nifty-zone-contracts: one extraction for every option structure (needs the PG* credentials).
3. nifty-zone-paths: every structure, the --like settings."""
import argparse,json,shlex,sqlite3,sys
from pathlib import Path

KEEP=('lots','min_short_days','long_target','contract_master','offset_mode','offsets','slippage','brokerage','max_participation','max_exit_lag_bars','forced_exit_haircut',
      'slippage_points','fill_price','target_margin','target_fraction','target_units','entry_bars','entry_minutes','carry')
EXTRACT=('min_short_days','long_target','contract_master','offset_mode','offsets')   # as scripts/combo_build_cmds.py (the extraction's own --lots stays 1)

def args_of(p):
    r=sqlite3.connect(f'file:{p}?mode=ro',uri=True).execute("SELECT value FROM meta WHERE name='args'").fetchone()
    if not r:sys.exit(f'{p} has no recorded arguments')
    return json.loads(r[0])

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--like',nargs='+',required=True,help='existing paths files whose settings the block copies (structures are combined)')
    ap.add_argument('--spot-from',default='2017-01-01');ap.add_argument('--from-date',default='2017-07-03',help='first signal day of the block (default %(default)s)')
    ap.add_argument('--to-date',default='2020-12-31');ap.add_argument('--dir',default='runs/b1720',help='output folder of the block (default %(default)s)')
    a=ap.parse_args(argv);olds=[(p,args_of(p)) for p in a.like]
    print(f'# block_build_cmds.py --like {" ".join(a.like)} --spot-from {a.spot_from} --from-date {a.from_date} --to-date {a.to_date} --dir {a.dir}',file=sys.stderr)
    base={}
    for p,o in olds:
        for k in KEEP:
            v=o.get(k)
            if k=='entry_minutes' and not v:v=0
            if v is None:continue
            if k in base and base[k]!=v:sys.exit(f'--like files disagree on {k}: {base[k]} vs {v} ({p})')
            base[k]=v
    structures=[];[structures.append(s) for _,o in olds for s in o.get('structures','').split(',') if s and s not in structures]
    options=[s for s in structures if not s.startswith('futures')]
    d=Path(a.dir);spot=d/'zone_spot.sqlite';opt=d/'zone_options.sqlite';paths=d/'zone_paths.sqlite';q=lambda x:shlex.quote(str(x))
    def flag(k,v):
        v=','.join(str(x) for x in v) if isinstance(v,list) else v
        return ['--'+k.replace('_','-')+'='+q(v)]                         # --k=v: a value starting with '-' (offsets) is not taken for a flag
    c0=['mkdir','-p',q(d)]
    c1=['nifty-zone-spot','--from-date',a.spot_from,'--to-date',a.to_date,'--out',q(spot)]
    c2=['nifty-zone-contracts','--spot',q(spot),'--from-date',a.from_date,'--to-date',a.to_date,'--structures',','.join(options),'--out',q(opt)]
    for k in EXTRACT:
        if k in base:c2+=flag(k,base[k])
    c3=['nifty-zone-paths','--options',q(opt),'--spot',q(spot),'--from-date',a.from_date,'--to-date',a.to_date,'--structures',','.join(structures),'--out',q(paths)]
    for k in KEEP:
        if k in base:c3+=flag(k,base[k])
    print('set -euo pipefail\n# settings from '+', '.join(str(p) for p,_ in olds)+'\n'+'\n'.join(' '.join(c) for c in (c0,c1,c2,c3)))

if __name__=='__main__':main()
