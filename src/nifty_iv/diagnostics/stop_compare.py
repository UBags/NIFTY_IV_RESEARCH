#!/usr/bin/env python3
"""Alternative stop-loss rules on a strategy's own entries.

Takes a strategy (side, structure, level, configuration, profit rule, stop rule), finds its trades exactly as the grid and the scans
sequence them (zone_grid.Detail), then keeps those ENTRIES fixed and applies each alternative stop (with the same profit rule) to the
same stored paths: every trade exits at the first of the profit rule, the alternative stop, or the path's natural end (zone change,
expiry). Because the entries are fixed, a stop that holds a trade longer can overlap the next entry; such overlaps are counted (they
would need margin for two positions, or would skip the later signal in live trading).

Per stop and period: trades, win ratio, profit factor (gross profit / gross loss), payoff (average win / average loss), annual return,
maximum drawdown, Sharpe, Sortino, Calmar. Returns are rupees after slippage and charges, less interest at --rate on the position's
margin (strategy_spec table, upper end, entry day) for the calendar days held, booked on the exit day, as a share of --capital, over
every weekday of the period (Sharpe and Sortino annualised with sqrt(252), risk-free 0); drawdown on the realised (closed-trade)
equity; annual return = net / capital / years; Calmar = annual return / max drawdown. Sizes are those of the paths file (rupees scale
with --scale; every ratio above is unaffected by size).

"Better than the current stop" is reported on several criteria (net, Calmar, Sharpe, and all three together) over --judge (default the
whole period); in-sample (entries before --split) and out-of-sample columns are shown for each. Choosing a stop on data it is then
judged on is in-sample selection: the out-of-sample columns are the check, and 2025-26 has been examined before."""
import argparse,collections,csv,datetime as dt,math,statistics as stt,sys
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.core import strategy_spec as ss
from nifty_iv.core import cli

CANDIDATES=['none','mloss1','mloss1.5','mloss2','mloss2.5','mloss3','mloss5','eatr15x1','eatr30x1','eatr30x1.5','eatr30x2','eatr45x1.5','eatr60x1','eatr60x2',
            'atr15x1','atr30x1.5','atr45x2','atr60x3','pct0.5','pct1','epct0.5','epct0.75','epct1','pts100','pts150','pts200','pts300',
            'trail15x1.5','trail30x2','trail45x2.5','trailpct0.5','trailpct1','trailpts150','trailpts300','chand12x2','chand24x3','donch12c0.25','donch24c0.5',
            'be1c0','be2c0.25','gb1x0.5','gb2x0.5','time3','time5','time7','vr1.5','vr2','stag2a0.5','stag3a0.5','loss100','loss200','mult2','mult3',
            'ema20p2','exp1','wkend','tod1515','strail30x2','tr2','mloss3.75']

def default_stops(structure,current,n=50):
    valid=set(zr.stop_rules(structure));out=[s for s in CANDIDATES if s in valid and s!=current]
    return out[:n]

def metrics(trades,capital):
    """trades: dicts with entry, exit (ISO dates) and net (rupees after interest)."""
    if not trades:return dict(trades=0)
    v=[t['net'] for t in trades];W=[x for x in v if x>0];L=[x for x in v if x<=0]
    d0=dt.date.fromisoformat(min(t['entry'] for t in trades));d1=dt.date.fromisoformat(max(t['exit'] for t in trades))
    days=[d0+dt.timedelta(i) for i in range((d1-d0).days+1)];days=[d.isoformat() for d in days if d.weekday()<5] or [d0.isoformat()]
    pnl=collections.defaultdict(float)
    for t in trades:pnl[t['exit']]+=t['net']
    r=[pnl.get(d,0.)/capital for d in days];mu=stt.mean(r);sd=stt.pstdev(r);dn=math.sqrt(stt.mean([min(x,0.)**2 for x in r]))
    eq=pk=mdd=0.
    for d in days:eq+=pnl.get(d,0.);pk=max(pk,eq);mdd=max(mdd,pk-eq)
    yrs=max((d1-d0).days,1)/365.25;ann=sum(v)/capital/yrs
    return dict(trades=len(v),win_ratio=round(len(W)/len(v),3),profit_factor=round(sum(W)/-sum(L),2) if L and sum(L) else (math.inf if W else None),
                payoff=round(stt.mean(W)/-stt.mean(L),2) if W and L and stt.mean(L) else None,net_rs=round(sum(v)),ann_return=round(ann,4),max_dd=round(mdd/capital,4),
                sharpe=round(mu/sd*math.sqrt(252),2) if sd else None,sortino=round(mu/dn*math.sqrt(252),2) if dn else (math.inf if mu>0 else None),calmar=round(ann/(mdd/capital),2) if mdd else (math.inf if ann>0 else None))

def compare(paths,strategy,stops,holidays=(),gap_policy='busy',start='2000-01-01',end='2026-07-01',rate=.10,scale=1.):
    side,st,off,cf,pr,cur=strategy;sel=dict(side=side,structure=st,offset=float(off),config=cf,profit=pr,stop=cur)
    allstops=[cur]+[s for s in stops if s!=cur];h=dt.date.fromisoformat(end).toordinal()
    with zg.Detail(paths,holidays,gap_policy) as D:
        base=D.trades(sel,start,end,value_to=end)
        b,index=D.group(side,st,float(off))
    o=zr.outcomes(b,[pr],allstops,[h]);out={s:[] for s in allstops};check=[]
    for x in base:
        i=index[x['signal_bar']];p=b.paths[i];units=float(b.units[i]);entry=p['entry_day'];mpu=ss.margin_per_unit(entry)
        for k,s in enumerate(allstops):
            v=float(o['vh'][0,0,k,i])
            if not math.isfinite(v):continue
            xd=int(o['xd'][0,k,i]);xday=dt.date.fromordinal(min(xd,h)).isoformat() if xd<10**8 else end
            days=max(1,(dt.date.fromisoformat(xday)-dt.date.fromisoformat(entry)).days)
            gross=v*units*scale;interest=mpu*units*scale*rate*days/365
            out[s].append(dict(signal_bar=x['signal_bar'],entry=entry,exit=xday,gross=gross,interest=interest,net=gross-interest,days=days))
            if k==0:check.append(abs(v*units-x['unit']*x['units']))
    for s,tr in out.items():                                     # overlaps: a trade still open when the next fixed entry fills
        tr.sort(key=lambda t:t['entry']);n=0
        for a,c in zip(tr,tr[1:]):n+=c['entry']<a['exit']
        out[s]=(tr,n)
    return out,max(check) if check else 0.,len(base)

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays');ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--strategy',action='append',required=True,help='side,structure,level,config,profit,stop: its stop is the current one (repeat)')
    ap.add_argument('--stops',help='comma-separated alternative stops (default: a fixed list of 50 across every stop family)')
    ap.add_argument('--capital',type=float,default=2.5e6);ap.add_argument('--rate',type=float,default=.10);ap.add_argument('--scale',type=float,default=1.,help='multiply rupees (ratios unaffected)')
    ap.add_argument('--split',default='2025-01-01');ap.add_argument('--start',default='2000-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--judge',choices=('all','in_sample','out_of_sample'),default='all',help='period on which "better" is decided')
    ap.add_argument('--out',default='stop_compare.csv')
    a=ap.parse_args(argv);hol=zr.read_holidays(a.holidays) if a.holidays else [];rows=[]
    for sdef in a.strategy:
        p=[x.strip() for x in sdef.split(',')]
        if len(p)!=6:sys.exit(f'--strategy needs 6 fields: {sdef}')
        strat=(p[0],p[1],float(p[2]),p[3],p[4],p[5]);stops=[s for s in a.stops.split(',') if s] if a.stops else default_stops(p[1],p[5])
        bad=[s for s in stops if s not in set(zr.stop_rules(p[1]))]
        if bad:sys.exit(f'unknown stops for {p[1]}: {bad}')
        res,err,n=compare(a.paths,strat,stops,hol,a.gap_policy,a.start,a.end,a.rate,a.scale)
        if err>1:sys.exit(f'current stop does not reproduce the paths file (max difference Rs {err:.0f}): stop')
        name='|'.join(p[:5])
        for s,(tr,ov) in res.items():
            r=dict(strategy=name,stop=s,current=s==p[5],overlaps=ov)
            for per,f in (('all',lambda t:True),('in_sample',lambda t:t['entry']<a.split),('out_of_sample',lambda t:t['entry']>=a.split)):
                for k,v in metrics([t for t in tr if f(t)],a.capital).items():r[f'{per}_{k}']=v
            rows.append(r)
        cur=next(r for r in rows if r['strategy']==name and r['current']);alt=[r for r in rows if r['strategy']==name and not r['current']]
        J=a.judge;g=lambda r,k:r.get(f'{J}_{k}') if r.get(f'{J}_{k}') is not None else -1e18
        crit={'net':lambda r:g(r,'net_rs')>g(cur,'net_rs'),'calmar':lambda r:g(r,'calmar')>g(cur,'calmar'),'sharpe':lambda r:g(r,'sharpe')>g(cur,'sharpe')}
        for r in alt:
            for c,f in crit.items():r[f'better_{c}']=f(r)
            r['better_all3']=all(r[f'better_{c}'] for c in crit)
        print(f"\n=== {name}: current stop {p[5]} ({n} trades, entries fixed; replay of the current stop matches the paths file) ===")
        hdr=f"{'stop':<16}{'trades':>7}{'win':>7}{'PF':>6}{'payoff':>8}{'ann ret':>9}{'max DD':>8}{'Sharpe':>8}{'Sortino':>9}{'Calmar':>8}{'net Rs':>12}  | in-sample ann/DD  | 2025-26 ann/DD  overlaps"
        print(hdr)
        def line(r,tag=''):
            pc=lambda v:'-' if v is None else f'{100*v:.1f}%'
            nm=lambda v,w,d=2:f"{'-' if v is None else ('inf' if v==math.inf else f'{v:.{d}f}'):>{w}}"
            print(f"{(r['stop']+tag):<16}{r[J+'_trades']:>7}{nm(r[J+'_win_ratio'],7,3)}{nm(r[J+'_profit_factor'],6)}{nm(r[J+'_payoff'],8)}{pc(r[J+'_ann_return']):>9}{pc(r[J+'_max_dd']):>8}"
                  f"{nm(r[J+'_sharpe'],8)}{nm(r[J+'_sortino'],9)}{nm(r[J+'_calmar'],8)}{r[J+'_net_rs']:>12,}  | {pc(r.get('in_sample_ann_return'))} / {pc(r.get('in_sample_max_dd'))}"
                  f"  | {pc(r.get('out_of_sample_ann_return'))} / {pc(r.get('out_of_sample_max_dd'))}  {r['overlaps']}")
        line(cur,' (now)')
        for r in sorted(alt,key=lambda r:-(r.get(J+'_calmar') or -1e9)):line(r)
        print(f"Better than {p[5]} on the {J} period, out of {len(alt)}: net {sum(r['better_net'] for r in alt)}, Calmar {sum(r['better_calmar'] for r in alt)}, "
              f"Sharpe {sum(r['better_sharpe'] for r in alt)}, all three {sum(r['better_all3'] for r in alt)}: {', '.join(r['stop'] for r in alt if r['better_all3']) or '-'}")
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with open(a.out,'w',newline='') as f:w=csv.DictWriter(f,keys);w.writeheader();w.writerows(rows)
    print(f'\n-> {a.out}  (rupees at the paths\' size x --scale; ratios do not depend on size)')

if __name__=='__main__':main()
