#!/usr/bin/env python3
"""Final P&L of trades by how deep their open loss went (drawdown bins), and what a stop at each level would have done.

Population as nifty-diag-dd-recovery: paths entered BEFORE the holdout (2025-01-01), held to their natural end (zone change or expiry;
no stop, no target), at the simulated size (V4.9.7: 40% of the margin-table lots). Drawdown = worst mark-to-market P&L at a
45-minute bar close, rupees (intraday lows are not seen). Signal configurations are pooled (one path per signal bar, unsequenced).

Per strategy, two tables:
  1. Bins of drawdown [lo, hi): trades, % ending in profit, mean / median / total final P&L, mean profit of the winners and
     mean loss of the losers in that bin.
  2. A stop at X (for each bin edge X): the trades whose drawdown reached X; holding them gave their mean final P&L, a stop
     would have given about -X each (before slippage and the bar-close lag). stop_minus_hold > 0: the stop was better per
     trade; saved = that x trades. Bar-close marks mean a real stop fills worse than -X."""
import argparse,csv,sqlite3,sys
import numpy as np
from nifty_iv.zone import paths as zp

EDGES=[0,12_500,25_000,37_500,50_000,75_000,100_000,125_000,150_000,175_000,200_000,225_000,250_000,275_000,300_000,325_000,350_000,float('inf')]
DEFAULT=('bear,naked,-2','bear,naked,0','bear,naked,2','bull,naked,-2','bull,naked,0','bull,naked,2','bull,futures_zone,0','bear,futures_zone,0')
k=lambda x:'inf' if x==float('inf') else (f'{x/1000:g}K')

def trades(db,side,st,off,before):
    mae=[];fin=[]
    for fb,eb,pnl,blob in db.execute("SELECT first_bar,end_bar,end_pnl,data FROM paths WHERE side=? AND structure=? AND offset=? AND status='closed' AND entry_day<? AND end_pnl IS NOT NULL",(side,st,off,before)):
        m=zp.unpack(blob,eb-fb+1)['mark'];m=m[np.isfinite(m)]
        mae.append(max(0.,-float(m.min())) if len(m) else 0.);fin.append(float(pnl))
    return np.array(mae),np.array(fin)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',default='zone_paths_levels.sqlite');ap.add_argument('--before',default='2025-01-01')
    ap.add_argument('--groups',default=';'.join(DEFAULT),help='side,structure,level;... (default: the 8 strategies)')
    ap.add_argument('--out',default='dd_bins.csv')
    a=ap.parse_args(argv);db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);rows=[]
    for g in a.groups.split(';'):
        side,st,off=g.split(',');mae,fin=trades(db,side,st,float(off),a.before)
        name=f'{side} {st} {int(float(off)):+d}'
        if not len(fin):print(f'\n{name}: no trades');continue
        print(f'\n=== {name}: {len(fin)} trades, {100*(fin>0).mean():.0f}% profitable, total Rs {fin.sum():,.0f}, mean Rs {fin.mean():,.0f} ===')
        print(f"{'drawdown bin':>16}{'trades':>8}{'win%':>6}{'mean':>10}{'median':>10}{'total':>12}{'win avg':>10}{'loss avg':>10}")
        for lo,hi in zip(EDGES[:-1],EDGES[1:]):
            s=(mae>=lo)&(mae<hi);f=fin[s];n=int(s.sum())
            if not n:continue
            w=f[f>0];l=f[f<=0]
            r=dict(strategy=name,table='bin',lo=lo,hi=hi,trades=n,win_pct=round(100*(f>0).mean(),1),mean=round(f.mean()),median=round(float(np.median(f))),total=round(f.sum()),
                   win_avg=round(w.mean()) if len(w) else None,loss_avg=round(l.mean()) if len(l) else None);rows.append(r)
            print(f"{k(lo)+'-'+k(hi):>16}{n:>8}{r['win_pct']:>6.0f}{r['mean']:>10,}{r['median']:>10,}{r['total']:>12,}{(format(r['win_avg'],',') if r['win_avg'] is not None else '-'):>10}{(format(r['loss_avg'],',') if r['loss_avg'] is not None else '-'):>10}")
        print(f"{'stop at':>16}{'reach':>8}{'win%':>6}{'hold mean':>11}{'stop':>10}{'stop-hold':>11}{'saved total':>13}")
        for X in EDGES[1:-1]:
            s=mae>=X;n=int(s.sum())
            if not n:continue
            hold=fin[s].mean();d=-X-hold
            r=dict(strategy=name,table='stop',lo=X,hi=None,trades=n,win_pct=round(100*(fin[s]>0).mean(),1),mean=round(hold),stop=-X,stop_minus_hold=round(d),saved_total=round(d*n));rows.append(r)
            print(f"{k(X):>16}{n:>8}{r['win_pct']:>6.0f}{round(hold):>11,}{-X:>10,.0f}{round(d):>11,}{round(d*n):>13,}")
    db.close()
    if not rows:sys.exit('no trades')
    keys=[];[keys.append(c) for r in rows for c in r if c not in keys]
    with open(a.out,'w',newline='') as f:w=csv.DictWriter(f,keys);w.writeheader();w.writerows(rows)
    print(f'\nstop-hold > 0: a stop at that level beat holding, per trade (before slippage; real fills are worse than the bar-close level).\n-> {a.out}')

if __name__=='__main__':main()
