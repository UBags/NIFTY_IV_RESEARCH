"""Why does a wide-search group keep no candidates? Rescans one group with the screening filters off and reports, over all
configurations x rules: train trades, yearly train return at full size, and how many pass each filter.

    python scripts/wide_search_probe.py --paths runs/zone_paths_levels.sqlite --holidays config/nse_holidays.txt --group bull,calendar,-1
"""
import argparse,collections,json,sqlite3,sys
import numpy as np
from nifty_iv.zone import grid as zg, rules as zr
from nifty_iv.diagnostics import wide_search as ws

ap=argparse.ArgumentParser();ap.add_argument('--paths',required=True);ap.add_argument('--holidays');ap.add_argument('--group',required=True)
ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2025-01-01');ap.add_argument('--end',default='2026-07-01')
ap.add_argument('--min-trades',type=int,default=8);ap.add_argument('--cand-min-annual',type=float,default=.08);ap.add_argument('--capital',type=float,default=2.5e6)
a=ap.parse_args();side,st,off=a.group.split(',');g=(side,st,float(off))
hol=zr.read_holidays(a.holidays) if a.holidays else [];per=ws.Periods(a.train_start,a.split,a.end,'half')
db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
except Exception:meta={}
scale=1/float(meta.get('target_fraction') or 1.);bars=zr.BarArrays.from_db(db,hol);signals=collections.defaultdict(list)
for cf,s,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[cf,s].append(t)
stat=zg.fill_stats(db,*g);paths=zg.load_paths(db,*g);filled=[p for p in paths.values() if p];db.close()
print(f'group {g}: fill stats {stat}; filled paths {len(filled)}; scale {scale:g} (target_fraction {meta.get("target_fraction")})')
if filled:
    ent=sorted(p['entry_day'] for p in filled);u=np.array([p['units'] for p in filled],float)
    print(f'  entries {ent[0]}..{ent[-1]}; in train window {sum(a.train_start<=e<a.split for e in ent)}; units min/median/max {u.min():g}/{np.median(u):g}/{u.max():g}')
    pnl=np.array([p['end_pnl'] if p['end_pnl'] is not None else np.nan for p in filled],float)
    print(f'  path end P&L (rupees, as simulated): median {np.nanmedian(pnl):,.0f}, sum {np.nansum(pnl):,.0f}, NaN {int(np.isnan(pnl).sum())}')
zg._CTX.clear();zg._CTX.update(paths=a.paths,bars=bars,signals=dict(signals),configs=sorted({c for c,_ in signals}),per=per,scale=scale,capital=a.capital,rank='lcb',
    min_trades=0,cand_min_annual=-np.inf,per_config=1,compound=False,min_fill_rate=0.,gap_policy='busy',workers=1,mem=4e8)
r=ws.scan_group(g);rows=r['rows']
if not rows:sys.exit(f"no rows even without filters: {r.get('skipped')}")
yrs=max((ws.dt.date.fromisoformat(a.split)-ws.dt.date.fromisoformat(a.train_start)).days,1)/365.25
n=np.array([x['trades'] for x in rows]);ann=np.array([sum(x['train'])/yrs for x in rows])
print(f"best rules per configuration (no filters, single exits, layered policies skipped): {len(rows)} rows from {len({x['config'] for x in rows})} configurations")
print(f"  train trades: median {np.median(n):g}, max {n.max()}; >= {a.min_trades}: {(n>=a.min_trades).sum()}")
print(f"  yearly train return: median {100*np.median(ann):.1f}%, max {100*ann.max():.1f}%; >= {a.cand_min_annual:.0%}: {(ann>=a.cand_min_annual).sum()}; both filters: {((n>=a.min_trades)&(ann>=a.cand_min_annual)).sum()}")
for x in sorted(rows,key=lambda x:-sum(x['train']))[:5]:
    print(f"  {x['config']} {x['profit']}/{x['stop']}: {x['trades']} trades, {100*sum(x['train'])/yrs:.1f}%/yr, halves {[round(100*v,1) for v in x['train']]}")
