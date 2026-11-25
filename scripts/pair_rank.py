"""Rank EVERY green x red pair of a pool by the user's alternate score (no limits, no eliminations, a strategy may appear in any
number of pairs), with leave-one-year-out held-out figures.

    python scripts/stage1_pool.py --run runs/wide_search_v4 --paths runs/zone_paths_v4914.sqlite --holidays config/nse_holidays.txt \\
        --select v3 --top 2000 --out runs/wide_search_v4/poolv3_2000
    python scripts/pair_rank.py --candidates runs/wide_search_v4/poolv3_2000/candidates.csv \\
        --quarters runs/wide_search_v4/poolv3_2000/quarters.csv --top 100 --out runs/wide_search_v4/poolv3_2000/pair_rank_top100.csv

Pair = bull leg + bear leg, both at full size, period returns added (as scripts/pair_picker.py). On the train periods:
  score = 10 / max(half range, 0.05) - 30 x [half range > 0.4] - 30 x (negative halves) - 10 x (negative quarters)
          + 15 x [CAGR > s1] + 15 x [CAGR > s2] + 15 x [CAGR > s3]        (--steps s1,s2,s3; default 0.35,0.35,0.25 as the user wrote it)
          + 25 x (max(CAGR - 0.20, 0) + max(CAGR - 0.25, 0) + max(CAGR - 0.30, 0) + max(CAGR - 0.35, 0))
CAGR = post-tax CAGR of the pair's train halves (35% tax, loss carry-forward, as pair_picker / the wide search); half range =
best - worst train half; negative halves / quarters counted over every train half (9) / quarter (18), partial first ones included.
Held-out: the whole ranking is redone with each train year left out (its halves and quarters removed, CAGR over the remaining
years); for each rank r the pair at rank r of that ranking is scored on the left-out year; stitching those years gives the held-out
post-tax CAGR AT RANK r (an honest figure for "take the r-th pair"), and of the top-N basket (1/N each). A fixed pair has no held-out
CAGR of its own: its returns are the same whether or not a year was used to pick it. Also shown: in how many of the 5 left-out
rankings the pair is itself in the top N."""
import argparse,csv,sys,time
import numpy as np
from nifty_iv.diagnostics import wide_search as ws

def components(V,Q,cg,steps):
    """Score parts (dict of arrays) from half returns V (..., halves), quarter returns Q (..., quarters) and post-tax CAGR cg."""
    rh=V.max(-1)-V.min(-1);nh=(V<0).sum(-1);nq=(Q<0).sum(-1)
    p=dict(stability=10/np.maximum(rh,.05),range_penalty=-30.*(rh>.4),neg_halves=-30.*nh,neg_quarters=-10.*nq,
           cagr_steps=sum(15.*(cg>s) for s in steps),cagr_slope=25*sum(np.maximum(cg-t,0.) for t in (.2,.25,.3,.35)))
    return p,rh,nh,nq

def score(V,Q,cg,steps):
    p,*_=components(V,Q,cg,steps);return sum(p.values())

def why(parts,rh,nh,nq,cg):
    """One line: what lifted this pair."""
    out=[]
    if parts['stability']>=50:out.append(f'very steady halves (range {100*rh:.0f}%, +{parts["stability"]:.0f})')
    elif parts['stability']>=30:out.append(f'steady halves (range {100*rh:.0f}%, +{parts["stability"]:.0f})')
    if nh==0 and nq==0:out.append('no negative half or quarter')
    elif nh==0:out.append(f'no negative half ({nq} negative quarter{"s"*(nq!=1)})')
    gain=parts['cagr_steps']+parts['cagr_slope']
    if gain>=40:out.append(f'high post-tax CAGR {100*cg:.0f}% (+{gain:.0f})')
    elif gain>0:out.append(f'post-tax CAGR {100*cg:.0f}% (+{gain:.0f})')
    if parts['range_penalty']<0:out.append('range above 40% (-30)')
    return '; '.join(out) or f'least penalised (CAGR {100*cg:.0f}%, range {100*rh:.0f}%)'

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True,help='candidates.csv of the pool (stage1_pool.py / the wide search)')
    ap.add_argument('--quarters',required=True,help='quarters.csv of the same pool');ap.add_argument('--out',required=True)
    ap.add_argument('--top',type=int,default=100);ap.add_argument('--steps',default='0.35,0.35,0.25',help='the three 15-point CAGR thresholds')
    ap.add_argument('--tax',type=float,default=.35);ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01')
    ap.add_argument('--end',default='2026-07-01');ap.add_argument('--chunk',type=int,default=200000,help='pairs per block (memory)')
    a=ap.parse_args(argv);t0=time.time();steps=[float(x) for x in a.steps.split(',')]
    if len(steps)!=3:sys.exit('--steps: three thresholds')
    per=ws.Periods(a.train_start,a.split,a.end,'half');pq=ws.Periods(a.train_start,a.split,a.end,'quarter')
    C=list(csv.DictReader(open(a.candidates)));Q={ws.strategy_key(r):r for r in csv.DictReader(open(a.quarters))}
    th=[f'train_{p}' for p in per.train];oh=[f'oos_{p}' for p in per.oos];tq=[f'train_{p}' for p in pq.train];oq=[f'oos_{p}' for p in pq.oos]
    if not C or any(k not in C[0] for k in th+oh):sys.exit(f'{a.candidates} lacks the halves {th+oh}')
    miss=sum(ws.strategy_key(r) not in Q for r in C)
    if miss:print(f'note: {miss} strategies have no quarter figures and are left out (make quarters.csv from the same pool)')
    C=[r for r in C if ws.strategy_key(r) in Q]
    if Q and any(k not in next(iter(Q.values())) for k in tq+oq):sys.exit(f'{a.quarters} lacks the quarters {tq+oq}')
    G=[r for r in C if r['side']=='bull'];B=[r for r in C if r['side']=='bear']
    if not G or not B:sys.exit('need bull and bear strategies')
    H=lambda S,c:np.array([[float(r[k]) for k in c] for r in S]).reshape(len(S),len(c))
    QH=lambda S,c:np.array([[float(Q[ws.strategy_key(r)][k]) for k in c] for r in S]).reshape(len(S),len(c))
    GH,BH,GO,BO=H(G,th),H(B,th),H(G,oh),H(B,oh);GQ,BQ,GQO,BQO=QH(G,tq),QH(B,tq),QH(G,oq),QH(B,oq)
    nG,nB=len(G),len(B);N=nG*nB;yrs_all=ws.fold(per)[1]
    folds=[(None,)+ws.fold(per)]+[(y,)+ws.fold(per,y) for y in per.years]
    qkeep={y:np.array([not (y and p[:4]==y) for p in pq.train]) for y,_,_ in folds}
    print(f'{nG} green x {nB} red = {N:,} pairs; train halves {per.train[0]}..{per.train[-1]}, quarters {pq.train[0]}..{pq.train[-1]}; '
          f'score steps {steps}; {len(folds)-1} left-out rankings',flush=True)
    S=np.empty((len(folds),N))
    for lo in range(0,N,a.chunk):
        idx=np.arange(lo,min(N,lo+a.chunk));i,j=idx//nB,idx%nB;V=GH[i]+BH[j];Qr=GQ[i]+BQ[j]
        for f,(y,keep,yrs) in enumerate(folds):
            cg=ws.post_tax_cagr(V,per.train,keep,yrs,a.tax);S[f,idx]=score(V[:,keep],Qr[:,qkeep[y]],cg,steps)
        if (lo//a.chunk)%10==0:print(f'  {min(N,lo+a.chunk):,}/{N:,} pairs scored ({time.time()-t0:.0f}s)',flush=True)
    def top(f,K):
        k=min(K,N);c=np.argpartition(-S[f],k-1)[:k];return c[np.lexsort((c,-S[f][c]))]                 # ties: lower pair index first
    K=a.top;T=top(0,K);tops={y:top(f,K) for f,(y,_,_) in enumerate(folds) if y}
    # held-out per rank: the rank-r pair of each left-out ranking, scored on its left-out year
    stitch=np.full((K,len(per.train)),np.nan)
    for y,P in tops.items():
        hq=[q for q,p in enumerate(per.train) if p[:4]==y]
        for r,pidx in enumerate(P):stitch[r,hq]=(GH[pidx//nB]+BH[pidx%nB])[hq]
    keep0,yrs0=ws.fold(per);ok=np.isfinite(stitch).all(1)
    hcg=np.where(ok,ws.post_tax_cagr(np.nan_to_num(stitch),per.train,keep0,yrs0,a.tax),np.nan)
    infold={int(p):sum(int(p) in set(map(int,P)) for P in tops.values()) for p in T}
    nm=lambda r:f"{r['structure']} {int(float(r['level'])):+d} {r['config']} {r['profit']}/{r['stop']}"
    rows=[]
    for r,pidx in enumerate(T):
        i,j=pidx//nB,pidx%nB;V=GH[i]+BH[j];Qr=GQ[i]+BQ[j];cg=float(ws.post_tax_cagr(V,per.train,keep0,yrs0,a.tax))
        parts,rh,nh,nq=components(V,Qr,cg,steps);parts={k:float(v) for k,v in parts.items()}
        rows.append(dict(rank=r+1,score=round(float(S[0,pidx]),3),post_tax_cagr=round(cg,4),neg_halves=int(nh),neg_quarters=int(nq),
                         half_range=round(float(rh),4),quarter_range=round(float(Qr.max()-Qr.min()),4),
                         oos_2026H1=round(float((GO[i]+BO[j]).sum()),4),oos_2026Q1=round(float(GQO[i][0]+BQO[j][0]),4) if len(oq) else '',
                         oos_2026Q2=round(float(GQO[i][1]+BQO[j][1]),4) if len(oq)>1 else '',
                         heldout_cagr_at_rank=round(float(hcg[r]),4) if np.isfinite(hcg[r]) else '',in_top_left_out=f'{infold[int(pidx)]}/5',
                         why=why(parts,float(rh),int(nh),int(nq),cg),green=nm(G[i]),red=nm(B[j]),
                         green_worst_trade=G[i].get('worst_trade',''),red_worst_trade=B[j].get('worst_trade',''),
                         green_trades=G[i].get('trades',''),red_trades=B[j].get('trades',''),
                         **{f'part_{k}':round(v,2) for k,v in parts.items()},**{h[6:]:round(float(x),4) for h,x in zip(th,V)},
                         **{q[6:]:round(float(x),4) for q,x in zip(tq,Qr)}))
    with open(a.out,'w',newline='') as fh:
        w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
    print(f"\ntop {K} of {N:,} pairs by the alternate score (train {per.train[0]}..{per.train[-1]}; 2026 never used)")
    print(f"{'#':>3} {'score':>6} {'CAGR':>5} {'-H':>2} {'-Q':>2} {'Hrng':>5} {'Qrng':>5} {'26H1':>6} {'HO@r':>6} {'inLO':>4}  green | red  ::  why")
    for x in rows:
        ho=f"{100*x['heldout_cagr_at_rank']:5.1f}%" if x['heldout_cagr_at_rank']!='' else '    - '
        print(f"{x['rank']:3d} {x['score']:6.1f} {100*x['post_tax_cagr']:4.0f}% {x['neg_halves']:2d} {x['neg_quarters']:2d} {100*x['half_range']:4.0f}% "
              f"{100*x['quarter_range']:4.0f}% {100*x['oos_2026H1']:5.1f}% {ho} {x['in_top_left_out']:>4}  {x['green']} | {x['red']}  ::  {x['why']}")
    b=np.nanmean(stitch,0)
    print(f"\nheld-out (5 left-out rankings): top-{K} basket stitched post-tax CAGR {100*float(ws.post_tax_cagr(b,per.train,keep0,yrs0,a.tax)):.1f}%, "
          f"halves {' '.join(f'{100*v:.0f}' for v in b)}; per-rank held-out CAGR median {100*np.nanmedian(hcg):.1f}%, negative at "
          f"{int((hcg<0).sum())} of {K} ranks; held-out halves negative {int((stitch<0).sum())} of {int(np.isfinite(stitch).sum())}")
    for lo,hi in ((0,10),(10,25),(25,50),(50,100)):
        if hi>K:break
        sb=np.nanmean(stitch[lo:hi],0);ob=np.mean([x['oos_2026H1'] for x in rows[lo:hi]])
        print(f'  ranks {lo+1:3d}-{hi:<3d}: held-out basket post-tax CAGR {100*float(ws.post_tax_cagr(sb,per.train,keep0,yrs0,a.tax)):5.1f}%, 2026H1 basket {100*ob:5.1f}%')
    import collections
    print('legs in the top '+str(K)+': green '+str(dict(collections.Counter(x['green'].split()[0] for x in rows)))+'; red '+str(dict(collections.Counter(x['red'].split()[0] for x in rows))))
    print(f'distinct legs in the top {K}: green {len({x["green"] for x in rows})}, red {len({x["red"] for x in rows})}')
    print(f'-> {a.out} ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
