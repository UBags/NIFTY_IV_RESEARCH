#!/usr/bin/env python3
"""Fixed entries, searched exits: for one green (bull) signal and one or more red (bear) signals, try the grid's exit rules on each leg
(profit rule x stop rule, incl. margin stops and layered policies) and find the green + red pairs that reach --target a year while
keeping the drawdown lowest. Then replay the best pairs on the out-of-sample window.

Steps
  1. Candidates per leg: from the grid files of that (side, structure, level), the given signal configuration's rules with
     >= --min-trades training trades and a positive training total; the top --per-leg by training total, by worst training year and
     by total among loss-limiting stops (margin, points, %, ATR, trailing, layered) are kept.
  2. Screen: every green x red pair at full size on the training window (the grid's), as zone_portfolio.py does: overall >= --target,
     every judged year >= --floor, the year band (every full year positive, worst >= --band x the mean), funds; ranked by drawdown.
  3. Executable replay of the best --qualify screen pairs through the chronological account (orders admitted at the signal close
     against funds and the margin budget); ranked by the executable drawdown.
  4. The best --top distinct pairs replayed on --oos (default 2025-01-01 .. 2026-07-01), adopted flat at its start.
Returns are simple P&L / --capital a year (not compounded), as zone_portfolio.py. All in-sample figures are fitted: the exits were
chosen on them. The out-of-sample window is the old holdout period, already studied."""
import argparse,collections,datetime as dt,json,math,sys,time
from pathlib import Path
import numpy as np
import zone_grid as zg
import zone_rules as zr
import zone_portfolio as zpf

LIMITING=('mloss','pts','pct','epct','atr','eatr','trail','strail','trailpct','trailpts','chand','donch','be','gb','loss','dloss','mult','compound')

def leg_candidates(grid,idx,leg,per_leg,min_trades,years):
    side,st,off,cfg=leg;out={};rows=[]
    for e in idx['groups']:
        if (e['side'],e['structure'],float(e['offset']))!=(side,st,float(off)):continue
        with np.load(Path(grid)/e['file']) as z:
            configs=list(z['configs'])
            if cfg not in configs:continue
            ci=configs.index(cfg);wn=list(z['windows']);P=list(z['profits']);S=list(z['stops'])
            N=z['n'][ci,wn.index('train_full')].astype(int);T=z['total'][ci,wn.index('train_full')]
            Y=np.stack([z['total'][ci,wn.index(f'test_{y}')] for y in years if f'test_{y}' in wn]) if years else None
        for pi in range(len(P)):
            for si in range(len(S)):
                if N[pi,si]<min_trades or T[pi,si]<=0:continue
                rows.append(((side,st,float(off),cfg,str(P[pi]),str(S[si])),float(T[pi,si]),float(Y[:,pi,si].min()) if Y is not None else float(T[pi,si]),zr.kind_of(str(S[si])) in LIMITING))
    for crit in (lambda r:r[1],lambda r:r[2]):
        for r in sorted(rows,key=crit,reverse=True)[:per_leg]:out[r[0]]=r
    for r in sorted([r for r in rows if r[3]],key=lambda r:r[1],reverse=True)[:per_leg]:out[r[0]]=r
    return list(out),len(rows)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--grid',required=True);ap.add_argument('--paths',required=True);ap.add_argument('--holidays')
    ap.add_argument('--green',default='bull,futures_zone,0,N3_k1.25_m8_j0.75_tw2',help='side,structure,level,config')
    ap.add_argument('--red',default='bear,naked,-2,N3_k0.25_m8_j0.25_tw2;bear,futures_zone,0,zone_only_tw2;bear,naked,-2,N3_k0.25_m8_j1.0_tw2',help='one or more, ; separated')
    ap.add_argument('--target',type=float,default=.50);ap.add_argument('--floor',type=float,help='every judged year (default 0.70 x --target)')
    ap.add_argument('--band',type=float,default=.70);ap.add_argument('--capital',type=float,default=2.5e6);ap.add_argument('--carry',type=float,default=.055)
    ap.add_argument('--per-leg',type=int,default=400,help='candidates per leg and criterion');ap.add_argument('--min-trades',type=int,default=15)
    ap.add_argument('--qualify',type=int,default=400,help='screen pairs replayed through the executable account');ap.add_argument('--top',type=int,default=15)
    ap.add_argument('--min-year-days',type=int,default=90,help='a part year shorter than this is not judged');ap.add_argument('--oos',default='2025-01-01,2026-07-01');ap.add_argument('--out',default='pair_exits')
    a=ap.parse_args(argv);t0=time.time();floor=a.floor if a.floor is not None else round(.7*a.target,6)
    hol=zr.read_holidays(a.holidays) if a.holidays else [];zpf.HOLIDAYS=frozenset(hol)
    idx=zg.verify(a.grid,a.paths);start,split=idx['start'],idx['split'];wn=[w[0] for w in idx['windows']];years=[w[5:] for w in wn if w.startswith('test_')]
    full=[y for y in years if f'{int(y)+1}-01-01'<=split]
    parse=lambda s:(lambda p:(p[0],p[1],float(p[2]),p[3]))(s.split(','))
    green=parse(a.green);reds=[parse(x) for x in a.red.split(';') if x]
    G,nG=leg_candidates(a.grid,idx,green,a.per_leg,a.min_trades,full);R=[];nR=0
    for r in reds:c,n=leg_candidates(a.grid,idx,r,a.per_leg,a.min_trades,full);R+=c;nR+=n
    print(f'candidates: green {len(G)} of {nG} positive rules, red {len(R)} of {nR} ({time.time()-t0:.0f}s)',flush=True)
    if not G or not R:sys.exit('no candidates on one side (check the configuration names and levels)')
    D=zg.Detail(a.paths,hol,idx.get('gap_policy','free'));L=D.target_lots or 10;mm=zpf.MarginModel('table')
    sz=zpf.Sizing('exchange',650.,L,D.target_units,'reference',D.target_margin,D.target_fraction)
    def sessions(lo,hi):
        d=sorted({x for x in D.bars.day if lo<=x<hi});o=np.array([dt.date.fromisoformat(x).toordinal() for x in d]);lc={};[lc.__setitem__(x,c) for x,c in zip(D.bars.day,D.bars.close)]
        return d,o,np.array([lc[x] for x in d])
    sd,so,sc=sessions(start,split);T=len(sd)
    sel_of=lambda k:dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5])
    curves={}
    for i,k in enumerate(G+R):
        p=zpf.prepare(D.trades(sel_of(k),start,split),so,k[1],a.carry,margin_unit=mm.per_unit(k[1],sd,sc))
        curves[k]=dict(prep=p,at={L:zpf.combine(p,L,sz)},bad=p['bad'],n=p['n'],stale=p['stale'],marks=p['marks'],reduced=0)
        if (i+1)%500==0:print(f'  replayed {i+1}/{len(G)+len(R)} ({time.time()-t0:.0f}s)',flush=True)
    G=[k for k in G if not curves[k]['bad']];R=[k for k in R if not curves[k]['bad']]
    G,dg=zpf._distinct(G,curves);R,dr=zpf._distinct(R,curves)                    # rules with identical trades count once
    print(f'distinct: green {len(G)} (identical dropped {dg}), red {len(R)} (identical dropped {dr})',flush=True)
    ys=zpf.year_slices(sd,start,split);i0,i1=ys[0][1],ys[-1][2];span=sum(y[4] for y in ys)/365.
    md=a.min_year_days;FG={L:zpf.Fold([curves[k]['at'][L] for k in G],i0,i1,ys,md,T)};FR={L:zpf.Fold([curves[k]['at'][L] for k in R],i0,i1,ys,md,T)}
    res,passed,cnt=zpf.search(FG,FR,[L],a.capital,span,a.target,floor,1e15,'dd',None,0.,None,basis='simple',max_margin=a.capital,band=a.band)
    print(f"screen: {cnt['combinations']:,} pairs, target {cnt['pass_target']:,}, floor {cnt['pass_floor']:,}, band {cnt.get('pass_band',0):,}, all {cnt.get('pass_all',0):,} ({time.time()-t0:.0f}s)",flush=True)
    if not passed:sys.exit(f'no pair reaches {a.target:.0%} a year with every year >= {floor:.0%} and the band; lower --target or widen --per-leg')
    tacc=zpf.Account(D,sd,so,sc,split,a.capital,0.,md,mm,sz,sel_of,None,a.capital);rows=[]
    for x in res[:a.qualify]:
        row=dict(_g=G[x['g']],_r=R[x['r']],green_lots=L,red_lots=L)
        q=zpf.qualify(tacc,row,start,split,a.capital,a.target,floor,1e15,'simple',md,a.carry,a.band);row.update(q,screen_overall=x['tot'],screen_drawdown=x['dd'])
        if q.get('executable_pass'):rows.append(row)
    rows.sort(key=lambda r:(r['executable_drawdown'],-(r['executable_overall'] or 0)))
    print(f'executable: {len(rows)} of {min(a.qualify,len(res))} replayed pass ({time.time()-t0:.0f}s)',flush=True)
    lo,hi=a.oos.split(',');od,oo,oc=sessions(lo,hi);out=[]
    if od:
        oacc=zpf.Account(D,od,oo,oc,hi,a.capital,0.,md,mm,sz,sel_of,None,a.capital)
        oyears=sorted({int(d[:4]) for d in od})
    for r in rows[:a.top]:
        rec=dict(green=' '.join(map(str,r['_g'][3:])),red=f"{r['_r'][1]} {int(r['_r'][2]):+d} "+' '.join(map(str,r['_r'][3:])),is_pct_pa=r['executable_overall'],is_worst_year=r['executable_worst_year'],
                 is_drawdown=r['executable_drawdown'],**{k:v for k,v in r.items() if k.startswith('year_')})
        if od:
            legs=[('oos','green',r['_g'],L,lo,hi),('oos','red',r['_r'],L,lo,hi)];A,M,opn,orders,Lo=oacc.run(legs,a.carry)
            s,*_=zpf.account_summary(oacc,A,M,Lo,opn,orders,oyears,0,start_date=lo)
            rec.update(oos_pct_pa=s['overall_simple'],oos_drawdown=s['daily_close_drawdown_continuous'],**{f'oos_{y}':(lambda v:None if v is None else round(float(v),4))(oacc.year(A,y)[0]) for y in oyears})
        out.append(rec)
    D.close();Path(a.out).mkdir(parents=True,exist_ok=True)
    keys=[];[keys.append(k) for r in out for k in r if k not in keys]
    import csv
    with open(Path(a.out)/'pair_exits.csv','w',newline='') as fh:w=csv.DictWriter(fh,keys);w.writeheader();w.writerows(out)
    pc=lambda v:'-' if v is None else f'{100*v:.1f}%'
    print(f"\nBest {len(out)} distinct pairs >= {a.target:.0%} a year in-sample ({start} .. {split}), every year >= {floor:.0%}, band {a.band}, ranked by executable drawdown; out of sample {lo} .. {hi}:")
    for i,r in enumerate(out,1):
        yrs=' / '.join(pc(r.get(f'year_{y}')) for y in full)
        o=' / '.join(f"{k[4:]} {pc(v)}" for k,v in r.items() if k.startswith('oos_2'))
        print(f"{i:>2}. IS {pc(r['is_pct_pa'])} a yr ({yrs}), dd Rs {r['is_drawdown']:,} | OOS {pc(r.get('oos_pct_pa'))} a yr ({o}), dd Rs {r.get('oos_drawdown',0):,}\n    green: {r['green']}\n    red:   {r['red']}")
    print(f'-> {a.out}/pair_exits.csv ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
