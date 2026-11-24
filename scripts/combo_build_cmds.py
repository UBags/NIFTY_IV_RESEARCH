"""Print the commands that add the V4.9.14 multi-leg structures (and the carry-adjusted futures) to an existing paths file, with the
same settings the existing file was built with (read from its meta args), so the files can be merged.

    python scripts/combo_build_cmds.py --paths runs/zone_paths_levels.sqlite > /tmp/combo_build.sh && cat /tmp/combo_build.sh

1. nifty-zone-contracts: a NEW options extraction with the combo legs (needs the PostgreSQL credentials, PG* variables).
2. nifty-zone-paths: the combos and futures_zone / futures_expiry (with --carry) from that extraction, same sizing / fill settings.
3. scripts/merge_paths.py: the existing file with its futures replaced and the combos added, into a new file."""
import argparse,json,sqlite3,shlex,sys
from pathlib import Path
from nifty_iv.core import strategy_spec as ss

KEEP=('spot','from_date','to_date','lots','min_short_days','long_target','contract_master','offset_mode','split','slippage','brokerage','max_participation',
      'max_exit_lag_bars','forced_exit_haircut','slippage_points','fill_price','target_margin','target_fraction','target_units','entry_bars','entry_minutes')

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',required=True,help='existing paths file');ap.add_argument('--carry',type=float,default=.055)
    ap.add_argument('--options-out',default=None,help='new extraction (default: next to --paths, zone_options_combos.sqlite)')
    ap.add_argument('--paths-out',default=None);ap.add_argument('--merged-out',default=None)
    a=ap.parse_args(argv);P=Path(a.paths).resolve();d=P.parent
    r=sqlite3.connect(f'file:{P}?mode=ro',uri=True).execute("SELECT value FROM meta WHERE name='args'").fetchone()
    if not r:sys.exit(f'{P} has no recorded arguments')
    old=json.loads(r[0]);combos=','.join(ss.COMBO_STRUCTURES)
    if old.get('offset_mode')!='levels':sys.exit('the existing file is not a levels build; the combos are defined on levels')
    spot=Path(old['spot']);spot=spot if spot.is_absolute() else (d/spot if (d/spot).exists() else spot.resolve())
    opt=Path(a.options_out or d/'zone_options_combos.sqlite');pout=Path(a.paths_out or d/'zone_paths_combos.sqlite');mout=Path(a.merged_out or d/'zone_paths_v4914.sqlite')
    q=lambda x:shlex.quote(str(x))
    c1=['nifty-zone-contracts','--spot',q(spot),'--offset-mode','levels','--from-date',old['from_date'],'--to-date',old['to_date'],
        '--min-short-days',str(old['min_short_days']),'--structures',combos,'--out',q(opt)]
    if old.get('contract_master'):c1+=['--contract-master',q(old['contract_master'])]
    c2=['nifty-zone-paths','--options',q(opt),'--out',q(pout),'--structures','futures_zone,futures_expiry,'+combos,'--carry',str(a.carry)]
    for k in KEEP:
        v=old.get(k)
        if k=='entry_minutes' and not v:v=0                                    # no minute limit (built before V4.9.12)
        if v is None:continue
        c2+=['--'+k.replace('_','-'),q(spot) if k=='spot' else str(v)]
    c3=['python','scripts/merge_paths.py','--base',q(P),'--add',q(pout),'--out',q(mout)]
    print('set -euo pipefail\n# settings from '+str(P)+'\n'+' '.join(c1)+'\n'+' '.join(c2)+'\n'+' '.join(c3))

if __name__=='__main__':main()
