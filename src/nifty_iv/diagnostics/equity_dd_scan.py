#!/usr/bin/env python3
"""Best single strategy per side under equity-drawdown caps.

Every strategy the grid tests (side x structure x strike level x signal configuration x profit rule x stop rule, layered policies
included, groups passing the fill-rate gate), run alone at its simulated size (V4.9.7: ceil(40% of the margin-table lots)), one
position at a time with the grid's sequencing and data-gap occupancy, as nifty-diag-strategy-scan does. Added here: the equity
curve's maximum drawdown.

  yearly return   rupee P&L (after costs) of the trades ENTERED in that calendar year, each to its exit / --capital
                  (2026: January-June, annualised x 365/181)
  max drawdown    peak-to-trough of the strategy's equity: closed P&L plus the open trade's mark-to-market at every 45-minute bar close
                  and at its exit, starting flat. Computed exactly, trade by trade, over the trades entered before --split (in-sample,
                  each held to its own exit), and separately for the trades entered from --split on (out of sample, starting flat
                  again). Intraday lows between bar closes are not seen, so true drawdowns are somewhat deeper.
  filters         on --judge-years (default 2022, 2023, 2024): every year >= --min-year (7%), at least --min-trades trades; in-sample
                  drawdown <= the largest cap
  ranking         for each side and each cap (--caps, share of --capital): (1) highest mean judged year, (2) highest worst judged year,
                  (3) highest mean judged year / max drawdown (both as shares of capital)

All figures are in-sample selections over many combinations: fitted, not forward evidence. Out-of-sample columns (2025, 2026H1) are
reported for reference only; that period has already been examined (docs/CODEMAP_V4_9_11.md)."""
import argparse,collections,csv,math,sqlite3,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.diagnostics import strategy_scan as ss_
from nifty_iv.core import cli

_prange=zg._prange
def _dd_kernel_py(ts,cptr,cbar,pix,gx,XB,V,LO,HI,DI,yi,ins,C,R,Y):
    """Per configuration, one position at a time per rule. Returns rupees and trades by entry year, and the equity max drawdown for
    in-sample (k=0) and out-of-sample (k=1) trades. Per trade: LO = lowest mark (<= 0), HI = highest mark (>= 0), DI = deepest fall
    from the trade's own running high (>= 0), all including the entry (0) and the exit value; with closed equity e and peak p before
    the trade, the deepest point of the trade is max(p - e - LO, DI), and the peak after it is max(p, e + HI)."""
    s=np.zeros((C,R,Y));n=np.zeros((C,R,Y),np.int32);D=np.zeros((C,R,2))
    for c in _prange(C):
        busy=np.full(R,-1,np.int64);E=np.zeros((R,2));P=np.zeros((R,2))
        for a in range(cptr[c],cptr[c+1]):
            q=cbar[a];pi=pix[q];t=ts[q]
            if pi<0:
                if gx[q]>=0:
                    for r in range(R):
                        if t>busy[r]:busy[r]=gx[q]
                continue
            y=yi[pi];k=0 if ins[pi] else 1
            for r in range(R):
                if t>busy[r]:
                    busy[r]=XB[pi,r];v=V[pi,r]
                    if math.isfinite(v):
                        s[c,r,y]+=v;n[c,r,y]+=1
                        e=E[r,k];p=P[r,k];dd=p-e-LO[pi,r]
                        if DI[pi,r]>dd:dd=DI[pi,r]
                        if dd>D[c,r,k]:D[c,r,k]=dd
                        h=e+HI[pi,r]
                        if h>p:P[r,k]=h
                        E[r,k]=e+v
    return s,n,D
_dd_kernel=zg._nb.njit(parallel=True)(_dd_kernel_py) if zg.HAVE_NUMBA else _dd_kernel_py

def trade_extremes(b):
    """Per path and row: running lowest mark, running highest mark, running deepest fall from the running high (per unit, the entry
    counted as 0). Rows without a valid mark carry the previous values."""
    ok=b.valid&np.isfinite(b.markpu);mk=np.where(ok,b.markpu,np.nan)
    lo=np.minimum.accumulate(np.where(ok,mk,np.inf),1);hi=np.maximum.accumulate(np.where(ok,mk,-np.inf),1)
    hi0=np.maximum(hi,0.);di=np.maximum.accumulate(np.where(ok,hi0-mk,0.),1)
    return lo,hi0,di

def scan_group(g):
    c=zg._CTX;side,st,off=g;t0=time.time()
    G,why=ss_.prepare_group(g)
    if G is None:return dict(group=g,skipped=why,rows=[])
    b,yi,configs,ts,cptr,cbar,pix,gx,C,years,filled=(G[k] for k in ('b','yi','configs','ts','cptr','cbar','pix','gx','C','years','filled'))
    ins=np.array([p['entry_day']<c['split'] for p in filled],np.bool_)
    lo,hi0,di=trade_extremes(b)
    P=zr.profit_rules(st);chunks=zg.stop_chunks(zr.stop_rules(st)+(zr.compound_rules(st) if c['compound'] else []))
    PC=[x for x in zr.compound_profits(st) if x in P];pc_idx=np.array([P.index(x) for x in PC]);FP=zr.evaluate(P,b);ar=np.arange(b.Np)[None,:]
    cap=c['capital'];J_=[years.index(y) for y in c['judge']];O_=[years.index(y) for y in c['oos'] if y in years];ann=np.array([c['ann'].get(y,1.) for y in years])
    rows=[];seen={};u=b.units[None,:]
    for kind,S in chunks.items():
        Pk=PC if kind.startswith('compound') else P;FPk=FP[pc_idx] if kind.startswith('compound') else FP
        FS=zr.evaluate(S,b);J=np.minimum(FPk[:,None,:],FS[None,:,:]).reshape(len(Pk)*len(S),b.Np)
        XB=np.ascontiguousarray(b.xb[ar,J].T,np.int64);f=b.pnl[ar,J]
        V=np.ascontiguousarray((f*u).T)
        LO=np.ascontiguousarray((np.minimum(np.minimum(lo[ar,J],f),0.)*u).T)
        HI=np.ascontiguousarray((np.maximum(hi0[ar,J],np.where(np.isfinite(f),f,0.))*u).T)
        DI=np.ascontiguousarray((np.maximum(di[ar,J],np.where(np.isfinite(f),hi0[ar,J]-f,0.))*u).T)
        s,n,D=_dd_kernel(ts,cptr,cbar,pix,gx,XB,V,LO,HI,DI,yi,ins,C,len(Pk)*len(S),len(years))
        ret=s/cap*ann[None,None,:];RJ=ret[:,:,J_]
        ok=(n[:,:,J_].sum(2)>=c['min_trades'])&(RJ.min(2)>=c['min_year'])&(D[:,:,0]<=c['max_dd']*cap)
        for ci,ri in zip(*np.nonzero(ok)):
            sig=(tuple(np.round(s[ci,ri],0)),tuple(n[ci,ri]),round(float(D[ci,ri,0])))
            if sig in seen:seen[sig]['identical_rules']+=1;continue
            pi,si=divmod(int(ri),len(S));RO=ret[ci,ri,O_] if O_ else np.zeros(0)
            mean=float(RJ[ci,ri].mean());ddp=float(D[ci,ri,0])/cap
            row=dict(side=side,structure=st,level=int(off),config=configs[ci],profit=Pk[pi],stop=S[si],
                     mean_judged=round(mean,4),min_judged=round(float(RJ[ci,ri].min()),4),max_dd_pct=round(ddp,4),
                     mean_over_dd=round(mean/ddp,3) if ddp>0 else None,max_dd_rupees=round(float(D[ci,ri,0])),
                     trades_judged=int(n[ci,ri,J_].sum()),trades=int(n[ci,ri].sum()),identical_rules=1,
                     **{f'ret_{y}':round(float(ret[ci,ri,k]),4) for k,y in enumerate(years)},**{f'trades_{y}':int(n[ci,ri,k]) for k,y in enumerate(years)},
                     oos_mean=round(float(RO.mean()),4) if len(RO) else None,oos_max_dd_pct=round(float(D[ci,ri,1])/cap,4) if len(RO) else None)
            seen[sig]=row;rows.append(row)
    return dict(group=g,rows=rows,seconds=round(time.time()-t0),fill_rate=G['stat'].get('fill_rate'),paths=len(filled))

RANKINGS=(('mean year','mean_judged'),('worst year','min_judged'),('mean / drawdown','mean_over_dd'))
def best(rows,side,cap,key,top=1):
    pool=[r for r in rows if r['side']==side and r['max_dd_pct']<=cap+1e-12 and r[key] is not None]
    return sorted(pool,key=lambda r:(-r[key],r['max_dd_pct'],-r['mean_judged']))[:top]

def evaluate(keys,paths,split,holidays=(),gap_policy='busy',start='2000-01-01',end='2026-07-01'):
    """Reference: in-sample yearly rupees and the equity max drawdown, replaying each strategy's trades one by one (zone_grid.Detail)."""
    out={};sel=lambda k:dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5])
    with zg.Detail(paths,holidays,gap_policy) as D:
        for k in keys:
            yr=collections.defaultdict(float);dd={0:0.,1:0.};eq={0:0.,1:0.};pk={0:0.,1:0.}
            for x in D.trades(sel(k),start,end,value_to=end):
                if not math.isfinite(x['unit']):continue
                kk=0 if x['entry_day']<split else 1;yr[x['entry_day'][:4]]+=x['unit']*x['units']
                m=x['marks'][np.isfinite(x['marks'])]*x['units']
                for v in np.append(m,x['unit']*x['units']):e=eq[kk]+v;pk[kk]=max(pk[kk],e);dd[kk]=max(dd[kk],pk[kk]-e)
                eq[kk]+=x['unit']*x['units']
            out[k]=dict(years=dict(yr),max_dd=dd[0],max_dd_oos=dd[1])
    return out

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays',help='the exchange holiday list the grid uses (weekend exits)')
    ap.add_argument('--grid',help='optional: a grid folder, to take its holidays and gap policy (and check they match --holidays)')
    ap.add_argument('--capital',type=float,default=2.5e6,help='base of every percentage (default 25 lakh)')
    ap.add_argument('--caps',default='0.15,0.175,0.20',help='equity drawdown caps as shares of --capital')
    ap.add_argument('--min-year',type=float,default=.07,help='every judged year at least this return (default 7%%)')
    ap.add_argument('--judge-years',default='2022,2023,2024');ap.add_argument('--min-trades',type=int,default=10,help='trades in the judged years')
    ap.add_argument('--split',default='2025-01-01',help='trades entered before this date are in-sample (drawdown, judged years)')
    ap.add_argument('--min-fill-rate',type=float,default=zg.MIN_FILL_RATE);ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--no-compound',action='store_true',help='skip the layered policies');ap.add_argument('--structures',help='limit to these structures (comma separated)')
    ap.add_argument('--end',default='2026-07-01',help='trades entered before this date');ap.add_argument('--workers',type=int,default=4)
    ap.add_argument('--top',type=int,default=5,help='strategies listed per side, cap and ranking');ap.add_argument('--out',default='equity_dd_scan.csv')
    a=ap.parse_args(argv);t0=time.time();caps=sorted(float(x) for x in a.caps.split(','))
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
    if any(y>=a.split[:4] for y in judge):sys.exit(f'--judge-years must be before --split {a.split}')
    oos=[y for y in years if y>=a.split[:4]]
    zg._CTX.clear();zg._CTX.update(paths=a.paths,bars=bars,signals=dict(signals),configs=sorted({cf for cf,_ in signals}),years=years,judge=judge,oos=oos,
        ann={'2026':365/181} if a.end=='2026-07-01' else {},capital=a.capital,min_year=a.min_year,max_dd=caps[-1],min_trades=a.min_trades,split=a.split,
        min_fill_rate=a.min_fill_rate,gap_policy=gp,compound=not a.no_compound,end=a.end,workers=a.workers)
    print(f'{len(groups)} groups, {len(zg._CTX["configs"])} signal configurations, years {years}; judged {judge}, in-sample before {a.split} ({time.time()-t0:.0f}s)',flush=True)
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
        else:print(f"  {i}/{len(groups)} {g[0]} {g[1]} {int(g[2]):+d}: {len(r['rows'])} distinct strategies kept ({r['seconds']}s; total {time.time()-t0:.0f}s)",flush=True)
        rows+=r['rows']
    if skipped:print('skipped (fill-rate gate or no trades): '+'; '.join(skipped))
    rows.sort(key=lambda r:(r['side'],r['max_dd_pct'],-r['mean_judged'],r['structure'],r['level'],r['config'],r['profit'],r['stop']))   # full key: deterministic order
    if rows:
        keys=[];[keys.append(k) for r in rows for k in r if k not in keys]
        with open(a.out,'w',newline='') as fh:w=csv.DictWriter(fh,keys);w.writeheader();w.writerows(rows)
    pc=lambda v:'-' if v is None else f'{100*v:.1f}%'
    print(f"\n{len(rows):,} distinct strategies: every year of {', '.join(judge)} >= {a.min_year:.0%}, >= {a.min_trades} trades, in-sample equity drawdown <= {caps[-1]:.1%} of Rs {a.capital:,.0f}"
          f" (fill rate >= {a.min_fill_rate:.0%}); in-sample, fitted. Out of sample ({', '.join(oos) or '-'}) shown for reference.")
    side_name={'bull':'GREEN (bull)','bear':'RED (bear)'}
    for side in ('bull','bear'):
        for cap in caps:
            print(f"\n{side_name[side]}, max drawdown <= {cap:.1%} of capital (Rs {cap*a.capital:,.0f}): {sum(1 for r in rows if r['side']==side and r['max_dd_pct']<=cap+1e-12):,} qualify")
            for label,key in RANKINGS:
                top=best(rows,side,cap,key,a.top)
                if not top:print(f"  best by {label}: none");continue
                for j,r in enumerate(top):
                    ys=' / '.join(pc(r[f'ret_{y}']) for y in judge);os_=' / '.join(f"{y} {pc(r.get(f'ret_{y}'))}" for y in oos)
                    print(f"  {'best by '+label if j==0 else '':<24}{r['structure']} {r['level']:+d} {r['config']} {r['profit']}/{r['stop']}: {ys} (mean {pc(r['mean_judged'])}, worst {pc(r['min_judged'])}),"
                          f" max DD {pc(r['max_dd_pct'])} (Rs {r['max_dd_rupees']:,}), mean/DD {r['mean_over_dd']} | OOS {os_}, DD {pc(r['oos_max_dd_pct'])}  [{r['trades_judged']} trades, {r['identical_rules']} identical]")
    print(f'\n-> {a.out if rows else "(no file: nothing qualifies)"} ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
