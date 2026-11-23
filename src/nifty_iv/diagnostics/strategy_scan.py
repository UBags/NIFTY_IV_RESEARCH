#!/usr/bin/env python3
"""Every strategy, checked exactly against three requirements: average yearly return, minimum yearly return, and the largest open
loss of any single trade, all as a share of --capital.

Strategy = side x structure x strike level x signal configuration x profit rule x stop rule: every combination the grid would test
(zone_rules profit and stop rules, layered policies included), for every group passing the fill-rate gate. Computed straight from the
paths file, group by group, all rules at once (as nifty-zone-grid does), one position at a time per (configuration, rule) with the grid's
sequencing and data-gap occupancy, at the simulated size (V4.9.7: 40% of the margin-table lots). Nothing is stored but the passes.

  yearly return   rupee P&L (after costs) of the trades ENTERED in that calendar year, each to its exit / --capital
                  (2026: January-June, annualised x 365/181)
  per-trade loss  the trade's worst mark-to-market at 45-minute bar closes up to its exit (or its final result if lower), rupees.
                  Intraday lows are not seen: true per-trade drawdowns are somewhat deeper.
  requirements    on --judge-years (default the in-sample full years 2022, 2023, 2024): mean >= --min-mean (13%), every year
                  >= --min-year (10%), every trade's worst open loss <= --max-trade-dd (3%), at least --min-trades trades
  also reported   2025 and 2026H1 returns and worst trade, and whether those years meet the same requirements (they were studied
                  already: the in-sample years decide, the rest is a check)
Strategies with identical yearly results and trade counts in one group are reported once, with the number of labels merged."""
import argparse,collections,csv,datetime as dt,hashlib,math,os,sqlite3,statistics as stt,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr

_prange=zg._prange
def _scan_kernel_py(ts,cptr,cbar,pix,gx,XB,V,M,yi,C,R,Y):
    """Per configuration, one position at a time per rule (the grid's sequencing): rupees, trades and worst trade by entry year."""
    s=np.zeros((C,R,Y));n=np.zeros((C,R,Y),np.int32);w=np.zeros((C,R,Y))
    for c in _prange(C):
        busy=np.full(R,-1,np.int64)
        for a in range(cptr[c],cptr[c+1]):
            q=cbar[a];pi=pix[q];t=ts[q]
            if pi<0:
                if gx[q]>=0:
                    for r in range(R):
                        if t>busy[r]:busy[r]=gx[q]
                continue
            y=yi[pi]
            for r in range(R):
                if t>busy[r]:
                    busy[r]=XB[pi,r];v=V[pi,r]
                    if math.isfinite(v):
                        s[c,r,y]+=v;n[c,r,y]+=1
                        m=M[pi,r]
                        if m<w[c,r,y]:w[c,r,y]=m
    return s,n,w
_scan_kernel=zg._nb.njit(parallel=True)(_scan_kernel_py) if zg.HAVE_NUMBA else _scan_kernel_py

def prepare_group(g):
    """Shared by the scans: the group's filled paths as a rule Batch, and each configuration's signal bars in time order (CSR:
    cptr / cbar), with the path index (pix, -1 = no path) and data-gap occupancy (gx) of each signal bar. None + reason if skipped."""
    c=zg._CTX;side,st,off=g
    if zg._nb is not None:zg._nb.set_num_threads(max(1,min(zg._nb.config.NUMBA_NUM_THREADS,(os.cpu_count() or 1)//max(1,c['workers']))))
    db=sqlite3.connect(f'file:{c["paths"]}?mode=ro',uri=True)
    stat=zg.fill_stats(db,side,st,off)
    if not zg.testable(stat,c['min_fill_rate']):db.close();return None,f"fill rate {stat.get('fill_rate')}"
    paths=zg.load_paths(db,side,st,off);filled=[p for p in paths.values() if p is not None and p['entry_day']<c['end']]
    if not filled:db.close();return None,'no trades'
    gaps=zg.gap_occupancy(db,side,st,off,c['gap_policy']);db.close()
    b=zr.Batch(filled,c['bars'],st);years=c['years'];yi=np.array([years.index(p['entry_day'][:4]) for p in filled],np.int64)
    configs=c['configs'];by_bar=collections.defaultdict(list)
    for ci,cf in enumerate(configs):
        for t in c['signals'].get((cf,side),()):by_bar[t].append(ci)
    ts=np.array(sorted(by_bar),np.int64)
    sizes=np.array([len(by_bar[t]) for t in ts],np.int64);cfg=np.concatenate([np.asarray(by_bar[t],np.int64) for t in ts]) if len(ts) else np.zeros(0,np.int64)
    bar_of=np.repeat(np.arange(len(ts)),sizes);order=np.argsort(cfg,kind='stable');cbar=bar_of[order].astype(np.int64)
    C=len(configs);cptr=np.zeros(C+1,np.int64);np.add.at(cptr,cfg+1,1);cptr=np.cumsum(cptr)
    idx={p['signal_bar']:i for i,p in enumerate(filled)};pix=np.array([idx.get(int(t),-1) for t in ts],np.int64);gx=np.array([gaps.get(int(t),-1) for t in ts],np.int64)
    return dict(stat=stat,filled=filled,b=b,yi=yi,configs=configs,ts=ts,cptr=cptr,cbar=cbar,pix=pix,gx=gx,C=C,years=years),None

def scan_group(g):
    c=zg._CTX;side,st,off=g;t0=time.time()
    G,why=prepare_group(g)
    if G is None:return dict(group=g,skipped=why,rows=[])
    stat,filled,b,yi,configs,ts,cptr,cbar,pix,gx,C,years=(G[k] for k in ('stat','filled','b','yi','configs','ts','cptr','cbar','pix','gx','C','years'))
    mk=np.where(b.valid&np.isfinite(b.markpu),b.markpu,np.inf);cm=np.minimum.accumulate(mk,1)          # running worst mark per unit
    P=zr.profit_rules(st);chunks=zg.stop_chunks(zr.stop_rules(st)+(zr.compound_rules(st) if c['compound'] else []))
    PC=[x for x in zr.compound_profits(st) if x in P];pc_idx=np.array([P.index(x) for x in PC]);FP=zr.evaluate(P,b);ar=np.arange(b.Np)[None,:]
    cap=c['capital'];J_=[years.index(y) for y in c['judge']];O_=[years.index(y) for y in c['oos'] if y in years];ann=np.array([c['ann'].get(y,1.) for y in years]);rows=[];seen={}
    for kind,S in chunks.items():
        Pk=PC if kind.startswith('compound') else P;FPk=FP[pc_idx] if kind.startswith('compound') else FP
        FS=zr.evaluate(S,b);J=np.minimum(FPk[:,None,:],FS[None,:,:]).reshape(len(Pk)*len(S),b.Np)          # (rules, paths)
        XB=np.ascontiguousarray(b.xb[ar,J].T,np.int64)
        pnl=b.pnl[ar,J];V=np.ascontiguousarray((pnl*b.units[None,:]).T)
        M=np.ascontiguousarray((np.minimum(np.minimum(cm[ar,J],pnl),0.)*b.units[None,:]).T)
        s,n,w=_scan_kernel(ts,cptr,cbar,pix,gx,XB,V,M,yi,C,len(Pk)*len(S),len(years))
        ret=s/cap*ann[None,None,:];RJ=ret[:,:,J_]
        ok=(n[:,:,J_].sum(2)>=c['min_trades'])&(RJ.min(2)>=c['min_year'])&(RJ.mean(2)>=c['min_mean'])&(w[:,:,J_].min(2)>=-c['max_dd']*cap)
        for ci,ri in zip(*np.nonzero(ok)):
            sig=(tuple(np.round(s[ci,ri],0)),tuple(n[ci,ri]))
            if sig in seen:seen[sig]['identical_rules']+=1;continue
            pi,si=divmod(int(ri),len(S));wO=w[ci,ri,O_] if O_ else np.zeros(0);RO=ret[ci,ri,O_] if O_ else np.zeros(0)
            row=dict(side=side,structure=st,level=int(off),config=configs[ci],profit=Pk[pi],stop=S[si],trades=int(n[ci,ri].sum()),identical_rules=1,
                     **{f'ret_{y}':round(float(ret[ci,ri,k]),4) for k,y in enumerate(years)},**{f'trades_{y}':int(n[ci,ri,k]) for k,y in enumerate(years)},
                     mean_judged=round(float(RJ[ci,ri].mean()),4),min_judged=round(float(RJ[ci,ri].min()),4),worst_trade_pct_judged=round(float(w[ci,ri,J_].min())/cap,4),
                     worst_trade_pct_oos=round(float(wO.min())/cap,4) if len(wO) else None,
                     oos_meets=bool(len(RO)) and bool(RO.min()>=c['min_year'] and RO.mean()>=c['min_mean'] and wO.min()>=-c['max_dd']*cap))
            seen[sig]=row;rows.append(row)
    return dict(group=g,rows=rows,seconds=round(time.time()-t0),fill_rate=stat.get('fill_rate'),paths=len(filled))

def evaluate(keys,paths,holidays=(),gap_policy='busy',start='2000-01-01',end='2026-07-01'):
    """Reference: the same figures for given strategies by replaying their trades one by one (zone_grid.Detail)."""
    out={};sel=lambda k:dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5])
    with zg.Detail(paths,holidays,gap_policy) as D:
        for k in keys:
            yr=collections.defaultdict(float);n=collections.Counter();worst=collections.defaultdict(float)
            for x in D.trades(sel(k),start,end,value_to=end):
                if not math.isfinite(x['unit']):continue
                r=x['unit']*x['units'];y=x['entry_day'][:4];yr[y]+=r;n[y]+=1
                m=x['marks'][np.isfinite(x['marks'])];worst[y]=min(worst[y],min(float(m.min()) if len(m) else 0.,x['unit'],0.)*x['units'])
            out[k]=dict(years=dict(yr),trades=dict(n),worst=dict(worst))
    return out

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays',help='the exchange holiday list the grid uses (weekend exits)')
    ap.add_argument('--grid',help='optional: a grid folder, to take its holidays and gap policy (and check they match --holidays)')
    ap.add_argument('--capital',type=float,default=2.5e6,help="base of every percentage (default 25 lakh; 1041667 = one strategy's 5/12 allocation)")
    ap.add_argument('--min-mean',type=float,default=.13);ap.add_argument('--min-year',type=float,default=.10);ap.add_argument('--max-trade-dd',type=float,default=.03)
    ap.add_argument('--judge-years',default='2022,2023,2024');ap.add_argument('--min-trades',type=int,default=10,help='trades in the judged years')
    ap.add_argument('--min-fill-rate',type=float,default=zg.MIN_FILL_RATE);ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--no-compound',action='store_true',help='skip the layered policies');ap.add_argument('--structures',help='limit to these structures (comma separated)')
    ap.add_argument('--end',default='2026-07-01',help='trades entered before this date');ap.add_argument('--workers',type=int,default=4);ap.add_argument('--out',default='strategy_scan.csv')
    a=ap.parse_args(argv);t0=time.time()
    hol=zr.read_holidays(a.holidays) if a.holidays else [];gp=a.gap_policy
    if a.grid:
        gi=zg.read_json(Path(a.grid)/'grid_index.json');gp=gi.get('gap_policy',gp)
        if sorted(gi.get('holidays',[]))!=sorted(hol):sys.exit('--holidays differs from the list the grid was built with')
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);bars=zr.BarArrays.from_db(db,hol)
    signals=collections.defaultdict(list)
    for cf,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[cf,side].append(t)
    groups=db.execute('SELECT DISTINCT side,structure,offset FROM paths ORDER BY 2,1,3').fetchall()
    years=sorted({y for (y,) in db.execute("SELECT DISTINCT substr(entry_day,1,4) FROM paths WHERE status!='no_fill' AND entry_day<?",(a.end,)) if y})
    db.close()
    if a.structures:groups=[g for g in groups if g[1] in a.structures.split(',')]
    judge=[y.strip() for y in a.judge_years.split(',') if y.strip()];bad=[y for y in judge if y not in years]
    if bad:sys.exit(f'--judge-years {bad} have no trades in the paths file (years {years})')
    oos=[y for y in years if y>max(judge)]
    zg._CTX.clear();zg._CTX.update(paths=a.paths,bars=bars,signals=dict(signals),configs=sorted({cf for cf,_ in signals}),years=years,judge=judge,oos=oos,
        ann={'2026':365/181} if a.end=='2026-07-01' else {},capital=a.capital,min_mean=a.min_mean,min_year=a.min_year,max_dd=a.max_trade_dd,min_trades=a.min_trades,
        min_fill_rate=a.min_fill_rate,gap_policy=gp,compound=not a.no_compound,end=a.end,workers=a.workers)
    print(f'{len(groups)} groups, {len(zg._CTX["configs"])} signal configurations, years {years}; judged {judge}, also reported {oos} ({time.time()-t0:.0f}s)',flush=True)
    def imap(fn,xs,w):
        if w<=1 or len(xs)<=1:
            for x in xs:yield fn(x)
            return
        import multiprocessing as mp
        with mp.get_context('fork').Pool(min(w,len(xs)),maxtasksperchild=2) as pool:yield from pool.imap_unordered(fn,xs,chunksize=1)
    rows=[];skipped=[]
    for i,r in enumerate(imap(scan_group,groups,a.workers),1):
        g=r['group']
        if r.get('skipped'):skipped.append(f"{g[0]} {g[1]} {int(g[2]):+d} ({r['skipped']})")
        else:print(f"  {i}/{len(groups)} {g[0]} {g[1]} {int(g[2]):+d}: {len(r['rows'])} distinct passes ({r['seconds']}s; total {time.time()-t0:.0f}s)",flush=True)
        rows+=r['rows']
    if skipped:print('skipped (fill-rate gate or no trades): '+'; '.join(skipped))
    rows.sort(key=lambda r:(-r['oos_meets'],-r['min_judged'],-r['mean_judged']))
    if rows:
        keys=[];[keys.append(k) for r in rows for k in r if k not in keys]
        with open(a.out,'w',newline='') as fh:w=csv.DictWriter(fh,keys);w.writeheader();w.writerows(rows)
    cap=a.capital;pc=lambda v:'-' if v is None else f'{100*v:.1f}%'
    print(f"\n{len(rows):,} distinct strategies meet: mean >= {a.min_mean:.0%}, every year >= {a.min_year:.0%} ({', '.join(judge)}), every trade's worst open loss <= {a.max_trade_dd:.1%} of Rs {cap:,.0f}"
          f"; {sum(r['oos_meets'] for r in rows):,} of them also meet it in {', '.join(oos) or '-'} (2026 = Jan-Jun, annualised)")
    cnt=collections.Counter((r['side'],r['structure'],r['level']) for r in rows);cnt2=collections.Counter((r['side'],r['structure'],r['level']) for r in rows if r['oos_meets'])
    if cnt:print('by group (all / also out of sample): '+'; '.join(f'{s} {t} {l:+d}: {n} / {cnt2[(s,t,l)]}' for (s,t,l),n in sorted(cnt.items())))
    for r in rows[:40]:
        ys=' / '.join(pc(r[f'ret_{y}']) for y in judge);os_=' / '.join(f"{y} {pc(r.get(f'ret_{y}'))}" for y in oos)
        print(f"{'OOS OK ' if r['oos_meets'] else '       '}{r['side']} {r['structure']} {r['level']:+d} {r['config']} {r['profit']}/{r['stop']}: {ys} (mean {pc(r['mean_judged'])}), worst trade {pc(r['worst_trade_pct_judged'])}"
              f" | {os_}, worst trade {pc(r['worst_trade_pct_oos'])}  [{r['trades']} trades, {r['identical_rules']} identical]")
    print(f'-> {a.out if rows else "(no file: nothing passes)"} ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
