"""Pick green + red pairs from a wide-search candidates.csv, with a different strategy in every pair.

    python scripts/pair_picker.py --candidates runs/wide_search_v3/candidates.csv --out runs/wide_search_v3/pairs.csv

A pair = one bull + one bear strategy, both at full size; their half-year returns are added (green and red positions do not overlap
in time, so both can use the whole capital). A pair qualifies when
  - each leg's worst open trade loss  <= --max-trade-loss       (default 0.099 of capital)
  - each leg's train account drawdown <= --max-dd               (default 0.20; the pair's own drawdown needs the paths file)
  - each leg has >= --min-trades train trades                   (default 15)
  - pair post-tax CAGR (35% tax, loss carry-forward) >= --cagr-floor (default 0.20)
  - share of train half-years > 0 >= --min-positive             (default 0.85, i.e. 8 of 9)
  - half-year range (max - min) <= --range-cap                  (default 0.40)
  - every train half-year > --min-half                           (default off)
  - every train quarter > --min-quarter                          (default off; needs --quarters, made by scripts/strategy_periods.py)
The floors skip a partial first period (2021H2 = Sep-Dec, 2021Q3 = Sep only) unless --floor-partial. --include-oos applies the
floors to the out-of-sample periods too (2026H1, 2026Q1-Q2): then 2026H1 is no longer out of sample.
Qualifying pairs are ranked all-positive first (unless --no-prefer-all-positive), then by --rank (objective: the wide search's
composite with the same --cagr-floor and --range-cap; or cagr; or worst-half; or v4: the user's V4.9.19 score, wide_search.score_v4,
on the pair's halves, quarters, drawdown and worst trade; needs --quarters and --risk from scripts/strategy_risk.py. The pair's
drawdown is that of the two legs' summed daily equity and its worst trade the worse leg's; in --loyo every term of a held-out year's
pick leaves that year out, the drawdown and worst trade included). Pairs are taken greedily: a pair is skipped if either
leg repeats a leg already taken, judged by --distinct:
  signal  (default) same side, structure, strike level and signal configuration (different exits of one signal count as the same)
  level   same side, structure and strike level
  rule    only the identical strategy
2026H1 (out of sample) is shown, never used. --loyo repeats the whole pick on each set of years with one year left out and shows the
picked pairs' returns in the held-out year: the honest estimate of what this selection does on unseen data.
Writes every taken pair (with legs and half-year returns) to --out."""
import argparse,csv,sys
import numpy as np
from nifty_iv.diagnostics import wide_search as ws

OPTIONS='naked,naked_m1,naked_m2,calendar,vert_w1,vert_w2,longopt,backspread_w1,backspread_w2,married,covered,collar_w1,collar_w2'

def load(path,sts):
    R=list(csv.DictReader(open(path)));tr=[k for k in R[0] if k.startswith('train_20')]
    def side(s):
        seen=set();out=[]
        for r in R:
            if r['side']!=s or r['structure'] not in sts:continue
            k=tuple(r[x] for x in tr)+(r['oos_2026H1'],r['worst_trade'])
            if k not in seen:seen.add(k);out.append(r)               # identical results (different labels, same trades): keep the first
        return out
    return side('bull'),side('bear'),tr

def arr(S,tr):
    f=lambda k:np.array([float(r[k]) for r in S])
    return np.array([[float(r[k]) for k in tr] for r in S]).reshape(len(S),len(tr)),f('worst_trade'),f('train_max_dd'),f('trades'),f('oos_2026H1')

def key(r,how):
    return {'rule':(r['side'],r['structure'],r['level'],r['config'],r['profit'],r['stop']),'signal':(r['side'],r['structure'],r['level'],r['config']),
            'level':(r['side'],r['structure'],r['level'])}[how]

def floor_cols(labels,a,held=None):
    """Train periods a floor applies to: not the held-out year, not a partial first period (starting before --train-start) unless
    --floor-partial."""
    st=ws.dt.date.fromisoformat(a.train_start).toordinal()
    return np.array([(held is None or p[:4]!=held) and (a.floor_partial or ws.period_start(p)>=st) for p in labels],bool)

def floors_ok(gi,bi,F,a,held):
    """(len(gi), len(bi)) mask: pairs above --min-half / --min-quarter on the floor periods."""
    m=np.ones((len(gi),len(bi)),bool)
    for name,x in (('half',a.min_half),('quarter',a.min_quarter)):
        if x is None or name not in F:continue
        GT,BT,GO,BO,pt,po=F[name]
        c=floor_cols(pt,a,held)
        if c.any():m&=(GT[gi][:,None,c]+BT[bi][None,:,c]).min(-1)>x
        if oos_on(a,name) and len(po):m&=(GO[gi][:,None,:]+BO[bi][None,:,:]).min(-1)>x
    return m

def oos_on(a,name):return a.include_oos in ('both',name)

def funnel(G,B,VG,VB,okg,okb,per,keep,yrs,a,F):
    """Pairs left after each criterion in turn, and where the period floors bind."""
    gi=np.flatnonzero(okg);bi=np.flatnonzero(okb);print(f'\nfunnel (pairs left after each criterion; distinct-signal pairs in brackets):')
    def dist(m):
        I,J=np.nonzero(m);ug={pp_key(G[gi[i]]) for i in I};ub={pp_key(B[bi[j]]) for j in J}
        return f'{int(m.sum()):7d}  [green signals {len(ug)}, red signals {len(ub)}]'
    pp_key=lambda r:key(r,a.distinct)
    print(f'  all pairs                                {len(G)*len(B):7d}')
    if not len(gi) or not len(bi):print('  none within the per-leg limits');return
    V=VG[gi][:,None,:]+VB[bi][None,:,:];m=np.ones(V.shape[:2],bool);print(f'  legs within trade loss / DD / trades     {dist(m)}')
    Vk=V[...,keep];cg=ws.post_tax_cagr(V,per.train,keep,yrs,a.tax)
    for lab,c in ((f'post-tax CAGR >= {a.cagr_floor:.0%}',cg>=a.cagr_floor),(f'positive halves >= {a.min_positive:.0%}',(Vk>0).mean(-1)>=a.min_positive-1e-9),
                  (f'half range <= {a.range_cap:.0%}',Vk.max(-1)-Vk.min(-1)<=a.range_cap)):
        m&=c;print(f'  {lab:40s} {dist(m)}')
    for name,x in (('half',a.min_half),('quarter',a.min_quarter)):
        if x is None or name not in F:continue
        GT,BT,GO,BO,pt,po=F[name];c=floor_cols(pt,a);T=GT[gi][:,None,c]+BT[bi][None,:,c];mn=T.min(-1)
        sel=m.copy();m&=mn>x;print(f'  {"every train "+name+" > "+f"{100*x:g}%":40s} {dist(m)}')
        if sel.any():
            lab=np.array(pt)[c];arg=T.argmin(-1)[sel];vals=mn[sel]
            print(f'      before this floor: worst train {name} of the {int(sel.sum())} pairs: median {100*np.median(vals):.1f}%, best {100*vals.max():.1f}%; '
                  +', '.join(f'> {100*t:g}%: {int((vals>t).sum())}' for t in (0,.02,.03,.05)))
            cnt=np.bincount(arg,minlength=len(lab));top=np.argsort(-cnt)[:5]
            print(f'      {"halves" if name=="half" else "quarters"} that are most often the worst: '+', '.join(f'{lab[k]} {100*cnt[k]/len(arg):.0f}%' for k in top))
        if oos_on(a,name) and len(po):
            O=(GO[gi][:,None,:]+BO[bi][None,:,:]).min(-1);sel=m.copy();m&=O>x;print(f'  {"every out-of-sample "+name+" > "+f"{100*x:g}%":40s} {dist(m)}')
            if sel.any():print(f'      before this floor: worst out-of-sample {name}: median {100*np.median(O[sel]):.1f}%, best {100*O[sel].max():.1f}%')

def pair_drawdown(EG,EB,chunk=64):
    """(len(EG), len(EB)) drawdowns of every summed pair of daily equity rows (shares of capital)."""
    out=np.empty((len(EG),len(EB)))
    for s in range(0,len(EG),chunk):out[s:s+chunk]=ws.drawdown(EG[s:s+chunk,None,:].astype(float)+EB[None,:,:].astype(float))
    return out

def v4_parts(gi,bi,V,F,a,held):
    """wide_search.score_v4 of the pairs (gi x bi) on the train halves V, train quarters, summed equity and the worse leg's trade."""
    R=F['risk'];f=0 if held is None else 1+R['years'].index(held)
    GT,BT,_,_,_,_=F['quarter'];Q=GT[gi][:,None,:]+BT[bi][None,:,:]
    tl=np.maximum(-R['wg'][gi,f][:,None],-R['wb'][bi,f][None,:]);dd=pair_drawdown(R['eg'][gi,f],R['eb'][bi,f])
    p=ws.score_v4(V,Q,F['per_half'],F['per_quarter'],dd,tl,a.tax,exclude=held);p['mdd']=dd;p['trade_loss']=tl;return p

def pick(G,B,VG,VB,okg,okb,per,keep,yrs,a,top,F=None,held=None):
    """Greedy diverse pick on the halves in keep. Returns [(i, j, cagr, obj, positive share, v4 parts or {})]."""
    C=dict(tax=a.tax,target=a.cagr_floor,ramp_from=None,range_floor=a.range_floor,range_cap=a.range_cap)
    gi=np.flatnonzero(okg);bi=np.flatnonzero(okb)
    if not len(gi) or not len(bi):return []
    V=VG[gi][:,None,:]+VB[bi][None,:,:]
    cg=ws.post_tax_cagr(V,per.train,keep,yrs,a.tax);ob=ws.composite(V,per.train,keep,yrs,C)
    Vk=V[...,keep];pos=(Vk>0).mean(-1);rng=Vk.max(-1)-Vk.min(-1);wh=Vk.min(-1)
    m=(cg>=a.cagr_floor)&(pos>=a.min_positive-1e-9)&(rng<=a.range_cap)
    if F is not None and (a.min_half is not None or a.min_quarter is not None):m&=floors_ok(gi,bi,F,a,held)
    if not m.any():return []
    p4=v4_parts(gi,bi,V,F,a,held) if a.rank=='v4' else None
    sc=p4['score'] if p4 is not None else {'objective':ob,'cagr':cg,'worst-half':wh}[a.rank]
    I,J=np.nonzero(m);tier=(pos[I,J]>=1-1e-9).astype(float) if a.prefer_all_positive else np.zeros(len(I))
    o=np.lexsort((-sc[I,J],-tier));usedg=set();usedb=set();out=[]
    for t in o:
        i,j=gi[I[t]],bi[J[t]];kg,kb=key(G[i],a.distinct),key(B[j],a.distinct)
        if kg in usedg or kb in usedb:continue
        x={k:float(v[I[t],J[t]]) for k,v in p4.items()} if p4 is not None else {}
        usedg.add(kg);usedb.add(kb);out.append((i,j,float(cg[I[t],J[t]]),float(ob[I[t],J[t]]),float(pos[I[t],J[t]]),x))
        if len(out)==top:break
    return out

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--structures',default=OPTIONS,help='structures allowed in either leg (default: the option structures)')
    ap.add_argument('--max-trade-loss',type=float,default=.099);ap.add_argument('--max-dd',type=float,default=.20)
    ap.add_argument('--min-trades',type=int,default=15);ap.add_argument('--cagr-floor',type=float,default=.20)
    ap.add_argument('--min-positive',type=float,default=.85);ap.add_argument('--range-cap',type=float,default=.40)
    ap.add_argument('--range-floor',type=float,default=.05);ap.add_argument('--tax',type=float,default=.35)
    ap.add_argument('--rank',choices=('objective','cagr','worst-half','v4'),default='objective',help='objective (default), cagr, worst-half, or v4 (the V4.9.19 score; needs --quarters and --risk)')
    ap.add_argument('--risk',help='scripts/strategy_risk.py output (npz) for --rank v4')
    ap.add_argument('--no-prefer-all-positive',dest='prefer_all_positive',action='store_false')
    ap.add_argument('--distinct',choices=('signal','level','rule'),default='signal');ap.add_argument('--top',type=int,default=5)
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--min-half',type=float,default=None,help='every train half-year of the pair > this (e.g. 0.05; default off)')
    ap.add_argument('--min-quarter',type=float,default=None,help='every train quarter of the pair > this (e.g. 0.05; default off; needs --quarters)')
    ap.add_argument('--quarters',help='quarter returns per strategy, from scripts/strategy_periods.py --period quarter')
    ap.add_argument('--floor-partial',action='store_true',help='apply the floors to a partial first period too')
    ap.add_argument('--include-oos',nargs='?',const='both',choices=('both','half','quarter'),default=None,
                    help='apply the floors to the out-of-sample periods too: both (bare flag), only the half floor (2026H1), or only the quarter floor (2026Q1-Q2); no out-of-sample test left')
    ap.add_argument('--funnel',action='store_true',help='print the pairs left after each criterion (always printed when none qualifies)')
    ap.add_argument('--loyo',action='store_true',help='also run the pick with each year left out and show the held-out returns')
    a=ap.parse_args(argv)
    print('pair_picker '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    G,B,tr=load(a.candidates,set(a.structures.split(',')))
    per=ws.Periods(a.train_start,a.split,a.end,'half')
    if [f'train_{p}' for p in per.train]!=tr:sys.exit(f'candidates halves {tr} do not match --train-start/--split ({per.train})')
    VG,wg,dg,ng,og=arr(G,tr);VB,wb,db,nb,obb=arr(B,tr)
    okg=(wg>=-a.max_trade_loss)&(dg<=a.max_dd)&(ng>=a.min_trades);okb=(wb>=-a.max_trade_loss)&(db<=a.max_dd)&(nb>=a.min_trades)
    print(f'distinct strategies: green {len(G)} ({int(okg.sum())} within the per-leg limits), red {len(B)} ({int(okb.sum())})')
    oh=[k for k in G[0] if k.startswith('oos_20')] if G else []
    F={'per_half':per,'half':(VG,VB,np.array([[float(r[k]) for k in oh] for r in G]).reshape(len(G),len(oh)),
                                np.array([[float(r[k]) for k in oh] for r in B]).reshape(len(B),len(oh)),per.train,per.oos)}
    if a.rank=='v4' and not (a.quarters and a.risk):sys.exit('--rank v4 needs --quarters (scripts/strategy_periods.py) and --risk (scripts/strategy_risk.py)')
    if a.min_quarter is not None or a.rank=='v4':
        if not a.quarters:sys.exit('--min-quarter needs --quarters (scripts/strategy_periods.py --period quarter)')
        Q=list(csv.DictReader(open(a.quarters)));pq=ws.Periods(a.train_start,a.split,a.end,'quarter')
        if not Q or any(f'train_{p}' not in Q[0] for p in pq.train) or any(f'oos_{p}' not in Q[0] for p in pq.oos):
            sys.exit(f'{a.quarters} lacks the quarters {pq.train + pq.oos}: make it with the same --train-start/--split/--end')
        qi={ws.strategy_key(r):r for r in Q};miss=[0,0]
        def qv(S,cols,side):
            out=[]
            for r in S:
                q=qi.get(ws.strategy_key(r))
                if q is None:miss[side]+=1;out.append([np.nan]*len(cols))      # no quarter figures: the leg cannot pass the floor
                else:out.append([float(q[c]) for c in cols])
            return np.array(out).reshape(len(S),len(cols))
        tc=[f'train_{p}' for p in pq.train];oc=[f'oos_{p}' for p in pq.oos]
        F['per_quarter']=pq;F['quarter']=(qv(G,tc,0),qv(B,tc,1),qv(G,oc,0),qv(B,oc,1),pq.train,pq.oos)
        okg&=np.isfinite(F['quarter'][0]).all(1);okb&=np.isfinite(F['quarter'][1]).all(1)
        if sum(miss):print(f'  no quarter figures for {miss[0]} green and {miss[1]} red strategies: left out')
    if a.rank=='v4':
        Z=np.load(a.risk);ix={k:n for n,k in enumerate(Z['keys'])};yrs_r=[str(y) for y in Z['years']]
        if yrs_r!=per.years:sys.exit(f'{a.risk} has train years {yrs_r}, not {per.years}: make it with the same --train-start/--split')
        def rk(S):
            n=np.array([ix.get('|'.join(str(x) for x in ws.strategy_key(r)),-1) for r in S]);has=n>=0;n=np.where(has,n,0)
            return Z['worst'][n],Z['equity'][n],has
        wgf,egf,hg=rk(G);wbf,ebf,hb=rk(B)
        if (~hg).sum() or (~hb).sum():print(f'  no risk figures for {int((~hg).sum())} green and {int((~hb).sum())} red strategies: left out')
        okg&=hg;okb&=hb;F['risk']=dict(wg=wgf,wb=wbf,eg=egf,eb=ebf,years=yrs_r)
        print('ranking: v4 = 10/max(full-year range,.10) -50[>.40] -40[half range>.30] -40 n 2^(n-1) (n negative halves) -10 m 2^(m-1) (m negative quarters) '
              '+15 at CAGR .25/.30/.35 +100 x (C-.20, C-.25, C-.30, C-.35) - drawdown steps (.075..25) - trade-loss steps (.03..095); '
              'C = post-tax CAGR without the best full year, capped at 50%')
    fl=[f'every half > {100*a.min_half:g}%' if a.min_half is not None else '',f'every quarter > {100*a.min_quarter:g}%' if a.min_quarter is not None else '']
    fl=' and '.join(x for x in fl if x)
    if fl:print(f"floors: {fl} ({'out-of-sample included for the '+a.include_oos+' floor'+('s' if a.include_oos=='both' else '')+': 2026H1 is no longer a test' if a.include_oos else 'train only'}"
                f"{'' if a.floor_partial else ', partial first period skipped'})")
    keep,yrs=ws.fold(per);P=pick(G,B,VG,VB,okg,okb,per,keep,yrs,a,a.top,F)
    if a.funnel or not P:funnel(G,B,VG,VB,okg,okb,per,keep,yrs,a,F)
    if not P:sys.exit('no pair meets the criteria; relax --cagr-floor / --min-positive / --min-half / --min-quarter / --max-trade-loss / --max-dd')
    nm=lambda r:f"{r['side']} {r['structure']} {int(float(r['level'])):+d} {r['config']} {r['profit']}/{r['stop']}"
    rows=[]
    print(f'\n{len(P)} pairs, distinct by {a.distinct}; halves {tr[0][6:]}..{tr[-1][6:]}, 2026H1 out of sample')
    for n,(i,j,cg,ob,pos,x4) in enumerate(P,1):
        v=VG[i]+VB[j];o=og[i]+obb[j]
        print(f"{n}. obj {ob:5.2f} | post-tax CAGR {100*cg:5.1f}% | positive {round(pos*len(tr))}/{len(tr)} | worst half {100*v.min():5.1f}% range {100*(v.max()-v.min()):4.1f}% | 2026H1 {100*o:6.1f}%")
        if x4:print(f"   v4 {x4['score']:6.1f} = stability {x4['stability']:6.1f} + return {x4['return_']:6.1f} - risk {x4['risk']:5.1f} | CAGR w/o best year {100*x4['cagr_ex_best']:5.1f}% | "
                    f"full-year range {100*x4['range_fy']:4.1f}% | negative halves {int(x4['neg_halves'])}, quarters {int(x4['neg_quarters'])} | pair DD {100*x4['mdd']:4.1f}% | worst trade {100*x4['trade_loss']:4.1f}%")
        print(f"   G {nm(G[i])}  [worst trade {100*wg[i]:.1f}%, train DD {100*dg[i]:.1f}%, {int(ng[i])} trades]")
        print(f"   R {nm(B[j])}  [worst trade {100*wb[j]:.1f}%, train DD {100*db[j]:.1f}%, {int(nb[j])} trades]")
        print('   halves '+' '.join(f'{100*x:.0f}' for x in v)+f' | {oh[0][4:] if oh else ""} '+' '.join(f'{100*(F["half"][2][i][q]+F["half"][3][j][q]):.0f}' for q in range(len(oh))))
        if 'quarter' in F:
            GT,BT,GO,BO,pt,po=F['quarter'];print('   quarters '+' '.join(f'{100*x:.0f}' for x in GT[i]+BT[j])+' | oos '+' '.join(f'{100*x:.0f}' for x in GO[i]+BO[j]))
        row=dict(rank=n,objective=round(ob,4),**({f'v4_{k}':round(val,4) for k,val in x4.items()} if x4 else {}),post_tax_cagr=round(cg,4),positive_halves=round(pos*len(tr)),worst_half=round(float(v.min()),4),
                 range=round(float(v.max()-v.min()),4),oos_2026H1=round(float(o),4))
        for s,r,w,d,t in (('green',G[i],wg[i],dg[i],ng[i]),('red',B[j],wb[j],db[j],nb[j])):
            row.update({f'{s}_{k}':r[k] for k in ('structure','level','config','profit','stop')});row.update({f'{s}_worst_trade':w,f'{s}_train_max_dd':d,f'{s}_trades':int(t)})
        row.update({h:round(float(x),4) for h,x in zip(tr,v)})
        if 'quarter' in F:
            GT,BT,GO,BO,pt,po=F['quarter'];row.update({f'train_{p}':round(float(x),4) for p,x in zip(pt,GT[i]+BT[j])});row.update({f'oos_{p}':round(float(x),4) for p,x in zip(po,GO[i]+BO[j])})
        rows.append(row)
    with open(a.out,'w',newline='') as fh:
        w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
    k=len(P);bh=sum(VG[i]+VB[j] for i,j,*_ in P)/k;bo=sum(F['half'][2][i]+F['half'][3][j] for i,j,*_ in P)/k
    print(f'\nbasket: the {k} pairs together, 1/{k} of the capital each (each leg at 1/{k} size: worst trade '
          f'{100*max(-wg[i] for i,*_ in P)/k:.1f}% / {100*max(-wb[j] for _,j,*_ in P)/k:.1f}% of capital)')
    print('   halves '+' '.join(f'{100*x:.0f}' for x in bh)+(' | '+oh[0][4:]+' '+' '.join(f'{100*x:.0f}' for x in bo) if len(oh) else '')
          +f' | post-tax CAGR {100*float(ws.post_tax_cagr(bh,per.train,keep,yrs,a.tax)):.1f}%, worst half {100*bh.min():.1f}%')
    if 'quarter' in F:
        GT,BT,GO,BO,pt,po=F['quarter'];bq=sum(GT[i]+BT[j] for i,j,*_ in P)/k;bqo=sum(GO[i]+BO[j] for i,j,*_ in P)/k;c=floor_cols(pt,a)
        print('   quarters '+' '.join(f'{100*x:.0f}' for x in bq)+' | oos '+' '.join(f'{100*x:.0f}' for x in bqo)
              +f' | worst full quarter {100*bq[c].min():.1f}%, negative {int((bq[c]<0).sum())}/{int(c.sum())}')
    if a.loyo:
        print('\nleave-one-year-out: the same pick on the other years; the picked pairs in the held-out year (each pair, %)')
        held=[];bheld=[];stitch=np.full(len(per.train),np.nan)
        for y in per.years:
            k2,y2=ws.fold(per,y);P2=pick(G,B,VG,VB,okg,okb,per,k2,y2,a,a.top,F,held=y)
            hq=[q for q,p in enumerate(per.train) if p[:4]==y]
            if not P2:print(f'  {y}: no pair qualifies');continue
            res=[[VG[i][q]+VB[j][q] for q in hq] for i,j,*_ in P2];held+=[x for r in res for x in r];bk=np.mean(res,0);bheld+=list(bk);stitch[hq]=bk
            print(f"  {y}: "+'  '.join('/'.join(f'{100*x:.0f}' for x in r) for r in res)+'   | basket '+'/'.join(f'{100*x:.0f}' for x in bk))
        if held:print(f'  held-out pair-halves: {len(held)}, negative {sum(x<0 for x in held)}, mean {100*np.mean(held):.1f}%, worst {100*min(held):.1f}%')
        if bheld:print(f'  held-out basket halves: {len(bheld)}, negative {sum(x<0 for x in bheld)}, mean {100*np.mean(bheld):.1f}%, worst {100*min(bheld):.1f}%')
        if np.isfinite(stitch).all():
            print(f'  held-out basket stitched (each year from the pick made without it): {100*stitch.sum()/yrs:.1f}%/yr pre-tax simple, '
                  f'post-tax CAGR {100*float(ws.post_tax_cagr(stitch,per.train,keep,yrs,a.tax)):.1f}% (tax {a.tax:.0%}, loss carry-forward, compounded yearly)')
    print(f'-> {a.out}')

if __name__=='__main__':main()
