"""Green + red pairs by screening and ratio ranking (user, 2026-11-25), as an alternative to a ranking formula.

    python scripts/pair_screen.py --candidates runs/wide_search_v4/poolv3_1000/candidates.csv \
        --risk runs/wide_search_v4/poolv3_1000/risk.npz --quarters runs/wide_search_v4/poolv3_1000/quarters.csv \
        --loyo --out runs/wide_search_v4/poolv3_1000/pairs_screen.csv

Steps (every figure on train data only, shares of --capital, both legs of a pair at full size):
  a. strategies with post-tax CAGR < --leg-cagr (0.10) are dropped (and fewer than --min-trades train trades, a sanity floor)
  b, c. green and red strategies are each ranked by (Sharpe + Sortino + Calmar) / 3
  d. the best --top-legs (1000) of each side are paired: up to 1,000,000 pairs
  e. pairs with post-tax CAGR < --pair-cagr (0.25) are dropped
  f. pairs whose worst train half-year < --min-half (0.025) are dropped (a partial first half, 2021H2 from 1 Sep, is not
     judged unless --floor-partial)
  g. pairs whose train drawdown > --max-dd (0.20) are dropped
  h. pairs whose worst open trade loss > --max-trade-loss (0.10) are dropped
  i. pairs whose range of complete-year returns (2022-2025, best - worst) > --max-fy-range (0.40) are dropped
  j. the rest are ranked by (Sortino + Calmar) / 2; the best --top are taken greedily with no signal setup repeated on either
     side (--distinct, as scripts/pair_picker.py)
Ratios come from daily mark-to-market P&L on fixed capital (scripts/strategy_risk.py): r = daily change of equity / capital;
Sharpe = mean(r) / sd(r) x sqrt(252); Sortino = mean(r) / sqrt(mean(min(r, 0)^2)) x sqrt(252); Calmar = mean(r) x 252 / max(MDD,
0.01); no risk-free rate; days with no position count as 0. A pair's daily P&L is the sum of its legs' (they never overlap).
Post-tax CAGR: 35% tax on each year's net gain after loss carry-forward, compounded yearly (wide_search.post_tax_cagr).
--loyo repeats the whole screen with each train year left out (ratios, drawdowns and worst trades without that year's trades or
days) and shows the picked pairs in the held-out year, as pair_picker does."""
import argparse,csv,os,sys
import datetime as dt
import numpy as np
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pair_picker as pp
from nifty_iv.diagnostics import wide_search as ws

def ratios(r,dd):
    """(Sharpe, Sortino, Calmar) of daily return rows r (..., days) with drawdowns dd (...)."""
    m=r.mean(-1);sd=r.std(-1);dn=np.sqrt(np.mean(np.minimum(r,0.)**2,-1))
    with np.errstate(divide='ignore',invalid='ignore'):
        sh=np.where(sd>0,m/sd,0.)*np.sqrt(252);so=np.where(dn>0,m/dn,np.where(m>0,1e6,0.))*np.sqrt(252)
    return sh,so,m*252/np.maximum(dd,.01)

class Fold:
    """Everything the screen needs for one scoring: all train years (held None) or one held out."""
    def __init__(self,per,R,days,held):
        self.held=held;self.keep,self.yrs=ws.fold(per,held);self.f=0 if held is None else 1+R['years'].index(held)
        yr=np.array([str(dt.date.fromordinal(int(d)).year) for d in days]);self.dmask=yr!=held if held else np.ones(len(days),bool)
        self.fy=ws.full_years(per,held);lab=np.array([p[:4] for p in per.train]);self.fymask=[self.keep&(lab==y) for y in self.fy]
        st=dt.date.fromisoformat(per.lo).toordinal();self.judge=self.keep&np.array([ws.period_start(p)>=st for p in per.train])

def daily(E,fold):
    """Daily P&L rows (shares of capital) of equity rows E (..., days), held-out days removed."""
    r=np.diff(E.astype(float),axis=-1,prepend=0.);return r[...,fold.dmask]

def screen(G,B,VG,VB,ng,nb,R,per,fold,a,log=False):
    """Steps a-j for one fold. Returns ([(i, j, info)], funnel lines)."""
    f=fold.f;out=[];fun=[]
    def side(V,n,E,W):
        cg=ws.post_tax_cagr(V,per.train,fold.keep,fold.yrs,a.tax);ok=(cg>=a.leg_cagr)&(n>=a.min_trades)
        r=daily(E[:,f],fold);dd=ws.drawdown(np.cumsum(r,-1));sh,so,ca=ratios(r,dd);sc=(sh+so+ca)/3
        idx=np.flatnonzero(ok);idx=idx[np.argsort(-sc[idx],kind='stable')][:a.top_legs]
        return idx,dict(cagr=cg,sharpe=sh,sortino=so,calmar=ca,score=sc,r=r,W=-W[:,f])
    gi,Lg=side(VG,ng,R['eg'],R['wg']);bi,Lb=side(VB,nb,R['eb'],R['wb'])
    fun.append(('strategies with CAGR >= leg floor (and trades)',f'green {int(((Lg["cagr"]>=a.leg_cagr)&(ng>=a.min_trades)).sum())}, red {int(((Lb["cagr"]>=a.leg_cagr)&(nb>=a.min_trades)).sum())}'))
    fun.append((f'top {a.top_legs} per side by (Sharpe+Sortino+Calmar)/3',f'green {len(gi)}, red {len(bi)}: {len(gi)*len(bi):,} pairs'))
    if not len(gi) or not len(bi):return [],fun,Lg,Lb
    V=VG[gi][:,None,:]+VB[bi][None,:,:];m=np.ones(V.shape[:2],bool)
    cg=ws.post_tax_cagr(V,per.train,fold.keep,fold.yrs,a.tax);m&=cg>=a.pair_cagr;fun.append((f'pair post-tax CAGR >= {a.pair_cagr:.0%}',int(m.sum())))
    jh=fold.keep if a.floor_partial else fold.judge;wh=V[...,jh].min(-1);m&=wh>=a.min_half;fun.append((f'worst half >= {a.min_half:.1%}',int(m.sum())))
    I,J=np.nonzero(m)
    if not len(I):return [],fun,Lg,Lb
    dd=np.empty(len(I));so=np.empty(len(I));ca=np.empty(len(I));sh=np.empty(len(I))
    for g in np.unique(I):                                                   # pair daily P&L = sum of the legs', per green leg
        s=np.flatnonzero(I==g);rp=Lg['r'][gi[g]][None,:]+Lb['r'][bi[J[s]]]
        dd[s]=ws.drawdown(np.cumsum(rp,-1));sh[s],so[s],ca[s]=ratios(rp,dd[s])
    keep=dd<=a.max_dd;fun.append((f'pair drawdown <= {a.max_dd:.0%}',int(keep.sum())))
    tl=np.maximum(Lg['W'][gi[I]],Lb['W'][bi[J]]);keep&=tl<=a.max_trade_loss;fun.append((f'worst trade <= {a.max_trade_loss:.1%}',int(keep.sum())))
    FY=np.stack([V[I,J][:,mk].sum(-1) for mk in fold.fymask],-1);rfy=FY.max(-1)-FY.min(-1);keep&=rfy<=a.max_fy_range
    fun.append((f'full-year range <= {a.max_fy_range:.0%}',int(keep.sum())))
    sel=np.flatnonzero(keep)
    if not len(sel):return [],fun,Lg,Lb
    rank=(so+ca)/2;usedg=set();usedb=set()
    for t in sel[np.argsort(-rank[sel],kind='stable')]:
        i,j=gi[I[t]],bi[J[t]];kg,kb=pp.key(G[i],a.distinct),pp.key(B[j],a.distinct)
        if kg in usedg or kb in usedb:continue
        usedg.add(kg);usedb.add(kb)
        out.append((i,j,dict(rank_score=float(rank[t]),sortino=float(so[t]),calmar=float(ca[t]),sharpe=float(sh[t]),cagr=float(cg[I[t],J[t]]),
                             worst_half=float(wh[I[t],J[t]]),mdd=float(dd[t]),trade_loss=float(tl[t]),range_fy=float(rfy[t]))))
        if len(out)==a.top:break
    return out,fun,Lg,Lb

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--risk',required=True,help='scripts/strategy_risk.py output (npz)')
    ap.add_argument('--quarters',help='scripts/strategy_periods.py output: quarter rows of the picks are shown');ap.add_argument('--out',required=True)
    ap.add_argument('--structures',default=pp.OPTIONS);ap.add_argument('--tax',type=float,default=.35)
    ap.add_argument('--leg-cagr',type=float,default=.10,help='a. strategy post-tax CAGR floor (default %(default)s)')
    ap.add_argument('--min-trades',type=int,default=15,help='a. strategy train trades floor (sanity; 0 = off; default %(default)s)')
    ap.add_argument('--top-legs',type=int,default=1000,help='d. strategies per side paired (default %(default)s)')
    ap.add_argument('--pair-cagr',type=float,default=.25,help='e. pair post-tax CAGR floor (default %(default)s)')
    ap.add_argument('--min-half',type=float,default=.025,help='f. worst train half-year floor (default %(default)s)')
    ap.add_argument('--floor-partial',action='store_true',help='f. judge the partial first half (2021H2) too')
    ap.add_argument('--max-dd',type=float,default=.20,help='g. pair train drawdown cap (default %(default)s)')
    ap.add_argument('--max-trade-loss',type=float,default=.10,help='h. pair worst open trade loss cap (default %(default)s)')
    ap.add_argument('--max-fy-range',type=float,default=.40,help='i. range of complete-year returns cap (default %(default)s)')
    ap.add_argument('--distinct',choices=('signal','level','rule'),default='signal');ap.add_argument('--top',type=int,default=5)
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--loyo',action='store_true',help='repeat the screen with each train year left out and show the held-out year')
    a=ap.parse_args(argv)
    print('pair_screen '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    G,B,tr=pp.load(a.candidates,set(a.structures.split(',')));per=ws.Periods(a.train_start,a.split,a.end,'half')
    if [f'train_{p}' for p in per.train]!=tr:sys.exit(f'candidates halves {tr} do not match --train-start/--split ({per.train})')
    VG,wg,dg,ng,og=pp.arr(G,tr);VB,wb,db,nb,obb=pp.arr(B,tr)
    Z=np.load(a.risk);ix={k:n for n,k in enumerate(Z['keys'])};yrs_r=[str(y) for y in Z['years']]
    if yrs_r!=per.years:sys.exit(f'{a.risk} has train years {yrs_r}, not {per.years}: make it with the same --train-start/--split')
    def rk(S):
        n=np.array([ix.get('|'.join(str(x) for x in ws.strategy_key(r)),-1) for r in S]);return n
    kg,kb=rk(G),rk(B);mg,mb=kg>=0,kb>=0
    if (~mg).sum() or (~mb).sum():print(f'  no risk figures for {int((~mg).sum())} green and {int((~mb).sum())} red strategies: left out')
    G=[r for r,x in zip(G,mg) if x];B=[r for r,x in zip(B,mb) if x];VG,wg,dg,ng,og=VG[mg],wg[mg],dg[mg],ng[mg],og[mg];VB,wb,db,nb,obb=VB[mb],wb[mb],db[mb],nb[mb],obb[mb]
    R=dict(wg=Z['worst'][kg[mg]],wb=Z['worst'][kb[mb]],eg=Z['equity'][kg[mg]],eb=Z['equity'][kb[mb]],years=yrs_r);days=Z['days']
    print(f'strategies: green {len(G)}, red {len(B)}; {len(days)} train days')
    Q=None
    if a.quarters:
        qr={ws.strategy_key(r):r for r in csv.DictReader(open(a.quarters))};pq=ws.Periods(a.train_start,a.split,a.end,'quarter')
        Q=(pq,qr)
    fold=Fold(per,R,days,None);P,fun,Lg,Lb=screen(G,B,VG,VB,ng,nb,R,per,fold,a)
    print('\nfunnel:');[print(f'  {k:52s} {v}') for k,v in fun]
    if not P:sys.exit('no pair passes the screen; relax a floor or cap')
    nm=lambda r:f"{r['side']} {r['structure']} {int(float(r['level'])):+d} {r['config']} {r['profit']}/{r['stop']}"
    oh=[k for k in G[0] if k.startswith('oos_20')];rows=[]
    print(f'\n{len(P)} pairs, distinct by {a.distinct}; ranked by (Sortino + Calmar) / 2; halves {tr[0][6:]}..{tr[-1][6:]}, {", ".join(o[4:] for o in oh)} out of sample')
    for n,(i,j,x) in enumerate(P,1):
        v=VG[i]+VB[j];o=og[i]+obb[j]
        print(f"{n}. rank {x['rank_score']:5.2f} = (Sortino {x['sortino']:4.2f} + Calmar {x['calmar']:4.2f}) / 2 | Sharpe {x['sharpe']:4.2f} | post-tax CAGR {100*x['cagr']:5.1f}% | "
              f"worst half {100*x['worst_half']:5.1f}% | DD {100*x['mdd']:4.1f}% | worst trade {100*x['trade_loss']:4.1f}% | full-year range {100*x['range_fy']:4.1f}% | 2026H1 {100*o:6.1f}%")
        for s,r,L,k in (('G',G[i],Lg,i),('R',B[j],Lb,j)):
            print(f"   {s} {nm(r)}  [CAGR {100*L['cagr'][k]:.1f}%, Sharpe {L['sharpe'][k]:.2f}, Sortino {L['sortino'][k]:.2f}, Calmar {L['calmar'][k]:.2f}, "
                  f"worst trade {100*L['W'][k]:.1f}%, {int((ng if s=='G' else nb)[k])} trades]")
        print('   halves '+' '.join(f'{100*z:.0f}' for z in v)+f' | 2026H1 {100*o:.0f}')
        row=dict(rank=n,**{k:round(val,4) for k,val in x.items()},oos_2026H1=round(float(o),4))
        if Q:
            pq,qr=Q;qg,qb=qr.get(ws.strategy_key(G[i])),qr.get(ws.strategy_key(B[j]))
            if qg and qb:
                qt=[float(qg[f'train_{p}'])+float(qb[f'train_{p}']) for p in pq.train];qo=[float(qg[f'oos_{p}'])+float(qb[f'oos_{p}']) for p in pq.oos]
                print('   quarters '+' '.join(f'{100*z:.0f}' for z in qt)+' | oos '+' '.join(f'{100*z:.0f}' for z in qo))
                row.update({f'train_{p}':round(z,4) for p,z in zip(pq.train,qt)});row.update({f'oos_{p}':round(z,4) for p,z in zip(pq.oos,qo)})
        for s,r in (('green',G[i]),('red',B[j])):row.update({f'{s}_{k}':r[k] for k in ('structure','level','config','profit','stop')})
        row.update({h:round(float(z),4) for h,z in zip(tr,v)});rows.append(row)
    with open(a.out,'w',newline='') as fh:
        cols=list(dict.fromkeys(k for r in rows for k in r));w=csv.DictWriter(fh,cols);w.writeheader();w.writerows(rows)
    k=len(P);bh=sum(VG[i]+VB[j] for i,j,_ in P)/k;bo=sum(og[i]+obb[j] for i,j,_ in P)/k
    print(f'\nbasket: the {k} pairs together, 1/{k} of the capital each')
    print('   halves '+' '.join(f'{100*z:.0f}' for z in bh)+f' | 2026H1 {100*bo:.0f} | post-tax CAGR {100*float(ws.post_tax_cagr(bh,per.train,fold.keep,fold.yrs,a.tax)):.1f}%, worst half {100*bh.min():.1f}%')
    if a.loyo:
        print('\nleave-one-year-out: the same screen on the other years; the picked pairs in the held-out year (each pair, %)')
        held=[];bheld=[];stitch=np.full(len(per.train),np.nan)
        for y in per.years:
            P2,_,_,_=screen(G,B,VG,VB,ng,nb,R,per,Fold(per,R,days,y),a);hq=[q for q,p in enumerate(per.train) if p[:4]==y]
            if not P2:print(f'  {y}: no pair passes');continue
            res=[[VG[i][q]+VB[j][q] for q in hq] for i,j,_ in P2];held+=[z for r in res for z in r];bk=np.mean(res,0);bheld+=list(bk);stitch[hq]=bk
            print(f"  {y}: "+'  '.join('/'.join(f'{100*z:.0f}' for z in r) for r in res)+'   | basket '+'/'.join(f'{100*z:.0f}' for z in bk))
        if held:print(f'  held-out pair-halves: {len(held)}, negative {sum(z<0 for z in held)}, mean {100*np.mean(held):.1f}%, worst {100*min(held):.1f}%')
        if bheld:print(f'  held-out basket halves: {len(bheld)}, negative {sum(z<0 for z in bheld)}, mean {100*np.mean(bheld):.1f}%, worst {100*min(bheld):.1f}%')
        if np.isfinite(stitch).all():
            print(f'  held-out basket stitched (each year from the screen run without it): {100*stitch.sum()/fold.yrs:.1f}%/yr pre-tax simple, '
                  f'post-tax CAGR {100*float(ws.post_tax_cagr(stitch,per.train,fold.keep,fold.yrs,a.tax)):.1f}%')
    print(f'-> {a.out}')

if __name__=='__main__':main()
