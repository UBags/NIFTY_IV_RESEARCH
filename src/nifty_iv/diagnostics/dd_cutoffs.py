#!/usr/bin/env python3
"""Margin-based cut-offs per strategy, in-sample and out of sample: no cut-off vs a fixed cut-off vs a cut-off optimised on all data.

Strategy = one (side, structure, strike level) held to its natural end (zone change or expiry; no other stop, no target), at the
simulated size (V4.9.7: 40% of the margin-table lots). Cut-off = the zone_rules `mloss X` stop: at the first 45-minute bar close
where the trade's mark-to-market loss reaches X% of its margin (units x the strategy_spec margin per unit on the entry day), the
trade is closed at the fills the path engine stored for an exit decided at that close (next bar's minutes, participation limit,
slippage, charges), not at an idealised -X.

Two views of each strategy:
  per trade   every path pooled (one per signal bar, all signal configurations, overlapping): mean, win %, profit factor, average
              loss, worst trade. Not an account: the same capital is counted many times.
  one at a time  the same pooled signals taken in time order, a new trade only once the previous one has closed (a cut-off frees
              the slot earlier, so more trades can be taken): total P&L, % a year on the strategy's allocated capital (--allocation x
              --capital, default 5/12 x 25 lakh) and on the full capital, trade-close drawdown. An executable sequence ("trade every
              zone signal of any configuration, one position at a time"), not the grid's chosen configuration.

--optimise: each strategy's cut-off is also chosen on ALL the data (in-sample + out of sample) from --levels, by the largest one-at-a-
time total over the whole period, and reported split into the two periods: how much optimising over the whole timeframe flatters.

!! The out-of-sample window (default 2025-01-01 .. 2026-07-01) IS the holdout period; the optimised cut-offs are fitted on it."""
import argparse,csv,datetime as dt,sqlite3,sys
import numpy as np
from nifty_iv.zone import paths as zp
from nifty_iv.core import strategy_spec as ss
from nifty_iv.core import cli

CUTOFFS={'bear,naked,-2':5.,'bear,naked,0':3.75,'bear,naked,2':7.5,'bull,naked,-2':3.75,'bull,naked,0':5.,'bull,naked,2':3.75,
         'bull,futures_zone,0':7.5,'bear,futures_zone,0':5.}
LEVELS=(2.5,3.75,5,6.25,7.5,10,12.5,15,20)

def load(db,side,st,off,lo,hi,levels):
    """Trades of one strategy entered in [lo, hi): signal bar, entry day, and per variant (None = no cut-off, else a level) the
    final P&L and the exit bar (NaN value: the stop exit had no value)."""
    out=[]
    for sb,fb,eb,pnl,units,day,blob in db.execute("SELECT signal_bar,first_bar,end_bar,end_pnl,units,entry_day,data FROM paths WHERE side=? AND structure=? AND offset=? AND status='closed' AND entry_day>=? AND entry_day<? AND end_pnl IS NOT NULL AND units>0 ORDER BY signal_bar",(side,st,off,lo,hi)):
        n=eb-fb+1;r=zp.unpack(blob,n);mg=units*ss.margin_per_unit(day);xb_nat=int(r['exit_bar'][n-1]) if n else eb
        v={None:(float(pnl),xb_nat)}
        for p in levels:
            with np.errstate(invalid='ignore'):hit=np.flatnonzero(r['mark']<=-p/100*mg)
            if len(hit) and hit[0]<n-1:j=hit[0];v[p]=(float(r['cash_total'][j]+r['exit_cash'][j]),int(r['exit_bar'][j]))
            else:v[p]=(float(pnl),xb_nat)
        out.append(dict(sb=sb,day=day,v=v))
    return out

def per_trade(tr,p):
    x=np.array([t['v'][p][0] for t in tr]);bad=int((~np.isfinite(x)).sum());x=x[np.isfinite(x)]
    stopped=sum(1 for t in tr if p is not None and t['v'][p]!=t['v'][None])
    if not len(x):return dict(trades=0,bad=bad,stopped=stopped)
    w=x[x>0];l=x[x<=0]
    return dict(trades=len(x),mean=round(x.mean()),win_pct=round(100*len(w)/len(x),1),profit_factor=round(w.sum()/-l.sum(),2) if l.sum()<0 else None,
                avg_loss=round(l.mean()) if len(l) else 0,worst=round(x.min()),total=round(x.sum()),stopped=stopped,bad=bad)

def sequenced(tr,p):
    """One position at a time in signal order: (trades taken, total, trade-close drawdown)."""
    busy=-1;tot=0.;pk=0.;dd=0.;n=0
    for t in tr:
        v,xb=t['v'][p]
        if t['sb']<=busy or not np.isfinite(v):continue
        busy=xb;tot+=v;n+=1;pk=max(pk,tot);dd=max(dd,pk-tot)
    return n,tot,dd

def years(lo,hi):return (dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days/365.

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',default='zone_paths_levels.sqlite');ap.add_argument('--split',default='2025-01-01');ap.add_argument('--start',help='first entry day (default: the earliest in the file)')
    ap.add_argument('--oos-end',default='2026-07-01');ap.add_argument('--cutoffs',help="override, e.g. 'bear,naked,-2=5;bull,naked,0=5' (percent of margin)")
    ap.add_argument('--optimise',action='store_true',help='also choose each cut-off on all the data from --levels (and none)')
    ap.add_argument('--levels',default=','.join(f'{x:g}' for x in LEVELS));ap.add_argument('--capital',type=float,default=2.5e6);ap.add_argument('--allocation',default='5/12')
    ap.add_argument('--out',default='dd_cutoffs.csv')
    a=ap.parse_args(argv);cuts=dict(CUTOFFS)
    if a.cutoffs:cuts={k:float(v) for k,v in (x.split('=') for x in a.cutoffs.split(';'))}
    n_,_,d_=a.allocation.partition('/');alloc=float(n_)/float(d_) if d_ else float(n_);acap=alloc*a.capital
    levels=sorted({float(x) for x in a.levels.split(',') if x}|set(cuts.values()))
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    start=a.start or db.execute("SELECT MIN(entry_day) FROM paths WHERE status='closed'").fetchone()[0]
    periods=(('in-sample',start,a.split),('out-of-sample',a.split,a.oos_end));Y={n:years(lo,hi) for n,lo,hi in periods};Yall=years(start,a.oos_end)
    rows=[];fmt=lambda d,k,w,f=',':(format(d[k],f) if d.get(k) is not None else '-').rjust(w)
    print(f'In-sample {start} .. {a.split} ({Y["in-sample"]:.2f} y); out of sample {a.split} .. {a.oos_end} ({Y["out-of-sample"]:.2f} y; the holdout period).')
    print(f'Allocated capital per strategy: {alloc:.4f} x Rs {a.capital:,.0f} = Rs {acap:,.0f}.\n')
    data={}
    for key,pct in cuts.items():
        side,st,off=key.split(',');tr=load(db,side,st,float(off),start,a.oos_end,levels)
        data[key]=(pct,tr)
    def best_level(tr):
        cand=[None]+levels;tots={p:sequenced(tr,p)[1] for p in cand};return max(cand,key=lambda p:(tots[p],p is None)),tots
    for key,(pct,tr) in data.items():
        side,st,off=key.split(',');name=f'{side} {st} {int(float(off)):+d}'
        opt,tots=best_level(tr) if a.optimise else (None,{})
        variants=[('no cut-off',None),(f'fixed {pct:g}%',pct)]+([(f'optimised {"none" if opt is None else format(opt,"g")+"%"}',opt)] if a.optimise else [])
        for vname,p in variants:
            for pname,lo,hi in periods:
                t=[x for x in tr if lo<=x['day']<hi];P=per_trade(t,p);n,tot,dd=sequenced(t,p)
                rows.append(dict(strategy=name,variant=vname,cutoff_pct_margin=p,period=pname,**P,seq_trades=n,seq_total=round(tot),seq_drawdown=round(dd),
                                 seq_pct_pa_allocated=round(100*tot/acap/Y[pname],1),seq_pct_pa_capital=round(100*tot/a.capital/Y[pname],1)))
        if a.optimise:rows.append(dict(strategy=name,variant='optimisation curve',period='all',**{f'all_total_{"none" if p is None else format(p,"g")}':round(v) for p,v in tots.items()}))
    db.close()
    print(f"{'':45}| {'PER TRADE (pooled, overlapping)':^56}| {'ONE AT A TIME':^49}")
    print(f"{'strategy':22}{'variant':16}{'period':7}| {'trades':>6}{'mean':>9}{'win%':>6}{'PF':>6}{'avg loss':>10}{'worst':>10}{'stopped':>8} | {'trades':>6}{'total':>12}{'%pa alloc':>10}{'%pa 25L':>9}{'drawdown':>11}")
    last=None
    for r in rows:
        if r['variant']=='optimisation curve':continue
        if r['strategy']!=last and last is not None:print()
        last=r['strategy'];per='IS' if r['period']=='in-sample' else 'OOS'
        if not r.get('trades'):print(f"{r['strategy']:22}{r['variant']:16}{per:7}| no trades");continue
        print(f"{r['strategy']:22}{r['variant']:16}{per:7}| {r['trades']:>6}{fmt(r,'mean',9)}{fmt(r,'win_pct',6,'.0f')}{fmt(r,'profit_factor',6,'.2f')}{fmt(r,'avg_loss',10)}{fmt(r,'worst',10)}{r['stopped']:>8} | "
              f"{r['seq_trades']:>6}{r['seq_total']:>12,}{r['seq_pct_pa_allocated']:>10}{r['seq_pct_pa_capital']:>9}{r['seq_drawdown']:>11,}")
    keys=[];[keys.append(k) for r in rows for k in r if k not in keys]
    with open(a.out,'w',newline='') as fh:w=csv.DictWriter(fh,keys);w.writeheader();w.writerows(rows)
    print(f'\nStops exit at the stored fills after the triggering bar close. "% pa" = one-at-a-time total / capital / period years (simple). '
          f'Optimised = the level (or none) with the largest one-at-a-time total over {start} .. {a.oos_end}: fitted on the out-of-sample years too.\n-> {a.out}')

if __name__=='__main__':main()
