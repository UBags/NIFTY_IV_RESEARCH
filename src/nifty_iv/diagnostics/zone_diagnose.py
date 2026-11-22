#!/usr/bin/env python3
"""Why does a strategy that did well in 2021-24 lose in 2025-26? Market context, trade anatomy, holding profile and delayed entry.

Reads a paths file (nifty-zone-paths) only. Every path of a strategy (side, structure, strike level) is pooled: one per signal bar, all
signal configurations, held to its natural end (zone change or expiry). Figures are per unit in points (option premium points for
options, index points for futures), after costs, so lot-size changes do not distort the comparison.

  A. market by half-year: index change, largest fall, average daily range, zone changes, median zone length (bars), share of green bars
  B. trades by half-year: count, mean / total points, win %, median bars held, share ending on a zone change, average best (MFE) and
     worst (MAE) open points, average give-back (best open - final), share of trades lasting <= 3 bars (whipsaw)
  C. holding profile: exit after k bars (or the natural end if earlier): mean points and win %, in-sample vs out of sample
  D. waiting before entering: enter d bars after the normal fill, if the zone still holds (the path is still open), at that bar's
     mark: mean points and win % of the trades still open. Approximation: no second entry cost and the option's last close as the price.
Periods: in-sample = before --split, out of sample = from it (the 2025-26 window has been studied already)."""
import argparse,collections,sqlite3,statistics as stt
import numpy as np
from nifty_iv.zone import paths as zp

DEFAULT='bull,naked,0;bull,futures_zone,0;bull,naked,-2;bear,naked,-2'
K=(1,2,3,5,9,18,27,45,90)
D=(0,1,2,3,5,9,18)

def half(day):return f"{day[:4]}H{1 if int(day[5:7])<=6 else 2}"

def context(db):
    bars=db.execute('SELECT idx,day,high,low,close,zone FROM bars ORDER BY idx').fetchall()
    by=collections.defaultdict(list)
    for b in bars:by[half(b[1])].append(b)
    print('A. Market by half-year (NIFTY spot, 45-minute bars)')
    print(f"{'half':8}{'start':>9}{'end':>9}{'change':>9}{'chg %':>7}{'max fall':>10}{'day range%':>11}{'zone chg':>9}{'med zone bars':>14}{'green %':>8}")
    for h,b in sorted(by.items()):
        c=np.array([x[4] for x in b]);pk=np.maximum.accumulate(c);days=collections.defaultdict(list)
        for x in b:days[x[1]].append(x)
        rng=stt.mean((max(y[2] for y in v)-min(y[3] for y in v))/v[-1][4]*100 for v in days.values() if v[-1][4])
        z=[x[5] for x in b];runs=[];n=1
        for i in range(1,len(z)):
            if z[i]==z[i-1]:n+=1
            else:runs.append(n);n=1
        runs.append(n)
        print(f"{h:8}{c[0]:>9,.0f}{c[-1]:>9,.0f}{c[-1]-c[0]:>9,.0f}{100*(c[-1]/c[0]-1):>7.1f}{(pk-c).max():>10,.0f}{rng:>11.2f}{len(runs)-1:>9}{stt.median(runs):>14.0f}{100*z.count('G')/len(z):>8.0f}")
    return {x[0]:x[1] for x in bars}

def trades(db,side,st,off):
    out=[]
    for sb,fb,eb,pnl,units,day,reason,blob in db.execute("SELECT signal_bar,first_bar,end_bar,end_pnl,units,entry_day,reason,data FROM paths WHERE side=? AND structure=? AND offset=? AND status='closed' AND end_pnl IS NOT NULL AND units>0 ORDER BY signal_bar",(side,st,off)):
        n=eb-fb+1;m=zp.unpack(blob,n)['mark']/units;fin=pnl/units
        ok=np.isfinite(m);m=np.where(ok,m,np.nan)
        out.append(dict(sb=sb,day=day,n=n,fin=fin,mark=m,reason=reason,mfe=float(np.nanmax(np.r_[m,0.])),mae=float(np.nanmin(np.r_[m,0.]))))
    return out

def anatomy(tr,name):
    by=collections.defaultdict(list)
    for t in tr:by[half(t['day'])].append(t)
    print(f'\nB. {name}: trades by half-year (points per unit, after costs; pooled signals)')
    print(f"{'half':8}{'trades':>7}{'mean':>8}{'total':>9}{'win%':>6}{'med bars':>9}{'zone end%':>10}{'best open':>10}{'worst open':>11}{'give-back':>10}{'<=3 bars%':>10}")
    for h,v in sorted(by.items()):
        f=np.array([t['fin'] for t in v])
        print(f"{h:8}{len(v):>7}{f.mean():>8.1f}{f.sum():>9,.0f}{100*(f>0).mean():>6.0f}{stt.median(t['n'] for t in v):>9.0f}{100*sum(t['reason']=='zone_flip' for t in v)/len(v):>10.0f}"
              f"{stt.mean(t['mfe'] for t in v):>10.1f}{stt.mean(t['mae'] for t in v):>11.1f}{stt.mean(t['mfe']-t['fin'] for t in v):>10.1f}{100*sum(t['n']<=3 for t in v)/len(v):>10.0f}")

def at(t,k):
    """Mark per unit after k bars (row k-1), or the final result if the trade ended earlier."""
    if k>=t['n']:return t['fin']
    v=t['mark'][k-1];return t['fin'] if not np.isfinite(v) else float(v)

def profiles(tr,name,split):
    parts=(('in-sample',[t for t in tr if t['day']<split]),('out-of-sample',[t for t in tr if t['day']>=split]))
    print(f'\nC. {name}: exit after k bars (or the natural end if earlier): mean points / win %')
    print(f"{'period':15}"+''.join(f'{"k="+str(k):>13}' for k in K)+f"{'natural end':>13}")
    for pname,v in parts:
        if not v:continue
        cells=''
        for k in K:x=np.array([at(t,k) for t in v]);cells+=f"{x.mean():>7.1f}/{100*(x>0).mean():>3.0f}%  "
        f=np.array([t['fin'] for t in v]);print(f"{pname:15}{cells}{f.mean():>7.1f}/{100*(f>0).mean():>3.0f}%")
    print(f'\nD. {name}: enter d bars after the normal fill if the zone still holds: trades still open / mean points / win %')
    print(f"{'period':15}"+''.join(f'{"d="+str(d):>19}' for d in D))
    for pname,v in parts:
        if not v:continue
        cells=''
        for d in D:
            if d==0:x=np.array([t['fin'] for t in v])
            else:x=np.array([t['fin']-t['mark'][d-1] for t in v if t['n']>d and np.isfinite(t['mark'][d-1])])
            cells+=f"{len(x):>6}/{x.mean() if len(x) else float('nan'):>6.1f}/{100*(x>0).mean() if len(x) else 0:>3.0f}%  "
        print(f"{pname:15}{cells}")

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',default='zone_paths_levels.sqlite');ap.add_argument('--split',default='2025-01-01');ap.add_argument('--strategies',default=DEFAULT)
    a=ap.parse_args(argv);db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    context(db)
    for g in a.strategies.split(';'):
        side,st,off=g.split(',');name=f'{side} {st} {int(float(off)):+d}';tr=trades(db,side,st,float(off))
        if not tr:print(f'\n{name}: no trades');continue
        anatomy(tr,name);profiles(tr,name,a.split)
    db.close()

if __name__=='__main__':main()
