"""Pick green + red pairs from a wide-search candidates.csv, with a different strategy in every pair.

    python scripts/pair_picker.py --candidates runs/wide_search_v3/candidates.csv --out runs/wide_search_v3/pairs.csv

A pair = one bull + one bear strategy, both at full size; their half-year returns are added (green and red positions do not overlap
in time, so both can use the whole capital). A pair qualifies when
  - each leg's worst open trade loss  <= --max-trade-loss       (default 0.099 of capital)
  - each leg's train account drawdown <= --max-dd               (default 0.20; the pair's own drawdown needs the paths file)
  - each leg has >= --min-trades train trades                   (default 20)
  - pair post-tax CAGR (35% tax, loss carry-forward) >= --cagr-floor (default 0.20)
  - share of train half-years > 0 >= --min-positive             (default 0.85, i.e. 8 of 9)
  - half-year range (max - min) <= --range-cap                  (default 0.40)
Qualifying pairs are ranked all-positive first (unless --no-prefer-all-positive), then by --rank (objective: the wide search's
composite with the same --cagr-floor and --range-cap; or cagr; or worst-half). Pairs are taken greedily: a pair is skipped if either
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

def pick(G,B,VG,VB,okg,okb,per,keep,yrs,a,top):
    """Greedy diverse pick on the halves in keep. Returns [(i, j, cagr, obj, positive share)]."""
    C=dict(tax=a.tax,target=a.cagr_floor,ramp_from=None,range_floor=a.range_floor,range_cap=a.range_cap)
    gi=np.flatnonzero(okg);bi=np.flatnonzero(okb)
    if not len(gi) or not len(bi):return []
    V=VG[gi][:,None,:]+VB[bi][None,:,:]
    cg=ws.post_tax_cagr(V,per.train,keep,yrs,a.tax);ob=ws.composite(V,per.train,keep,yrs,C)
    Vk=V[...,keep];pos=(Vk>0).mean(-1);rng=Vk.max(-1)-Vk.min(-1);wh=Vk.min(-1)
    m=(cg>=a.cagr_floor)&(pos>=a.min_positive-1e-9)&(rng<=a.range_cap)
    if not m.any():return []
    sc={'objective':ob,'cagr':cg,'worst-half':wh}[a.rank]
    I,J=np.nonzero(m);tier=(pos[I,J]>=1-1e-9).astype(float) if a.prefer_all_positive else np.zeros(len(I))
    o=np.lexsort((-sc[I,J],-tier));usedg=set();usedb=set();out=[]
    for t in o:
        i,j=gi[I[t]],bi[J[t]];kg,kb=key(G[i],a.distinct),key(B[j],a.distinct)
        if kg in usedg or kb in usedb:continue
        usedg.add(kg);usedb.add(kb);out.append((i,j,float(cg[I[t],J[t]]),float(ob[I[t],J[t]]),float(pos[I[t],J[t]])))
        if len(out)==top:break
    return out

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--structures',default=OPTIONS,help='structures allowed in either leg (default: the option structures)')
    ap.add_argument('--max-trade-loss',type=float,default=.099);ap.add_argument('--max-dd',type=float,default=.20)
    ap.add_argument('--min-trades',type=int,default=20);ap.add_argument('--cagr-floor',type=float,default=.20)
    ap.add_argument('--min-positive',type=float,default=.85);ap.add_argument('--range-cap',type=float,default=.40)
    ap.add_argument('--range-floor',type=float,default=.05);ap.add_argument('--tax',type=float,default=.35)
    ap.add_argument('--rank',choices=('objective','cagr','worst-half'),default='objective')
    ap.add_argument('--no-prefer-all-positive',dest='prefer_all_positive',action='store_false')
    ap.add_argument('--distinct',choices=('signal','level','rule'),default='signal');ap.add_argument('--top',type=int,default=5)
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--loyo',action='store_true',help='also run the pick with each year left out and show the held-out returns')
    a=ap.parse_args(argv)
    print('pair_picker '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    G,B,tr=load(a.candidates,set(a.structures.split(',')))
    per=ws.Periods(a.train_start,a.split,a.end,'half')
    if [f'train_{p}' for p in per.train]!=tr:sys.exit(f'candidates halves {tr} do not match --train-start/--split ({per.train})')
    VG,wg,dg,ng,og=arr(G,tr);VB,wb,db,nb,obb=arr(B,tr)
    okg=(wg>=-a.max_trade_loss)&(dg<=a.max_dd)&(ng>=a.min_trades);okb=(wb>=-a.max_trade_loss)&(db<=a.max_dd)&(nb>=a.min_trades)
    print(f'distinct strategies: green {len(G)} ({int(okg.sum())} within the per-leg limits), red {len(B)} ({int(okb.sum())})')
    keep,yrs=ws.fold(per);P=pick(G,B,VG,VB,okg,okb,per,keep,yrs,a,a.top)
    if not P:sys.exit('no pair meets the criteria; relax --cagr-floor / --min-positive / --max-trade-loss / --max-dd')
    nm=lambda r:f"{r['side']} {r['structure']} {int(float(r['level'])):+d} {r['config']} {r['profit']}/{r['stop']}"
    rows=[]
    print(f'\n{len(P)} pairs, distinct by {a.distinct}; halves {tr[0][6:]}..{tr[-1][6:]}, 2026H1 out of sample')
    for n,(i,j,cg,ob,pos) in enumerate(P,1):
        v=VG[i]+VB[j];o=og[i]+obb[j]
        print(f"{n}. obj {ob:5.2f} | post-tax CAGR {100*cg:5.1f}% | positive {round(pos*len(tr))}/{len(tr)} | worst half {100*v.min():5.1f}% range {100*(v.max()-v.min()):4.1f}% | 2026H1 {100*o:6.1f}%")
        print(f"   G {nm(G[i])}  [worst trade {100*wg[i]:.1f}%, train DD {100*dg[i]:.1f}%, {int(ng[i])} trades]")
        print(f"   R {nm(B[j])}  [worst trade {100*wb[j]:.1f}%, train DD {100*db[j]:.1f}%, {int(nb[j])} trades]")
        print('   halves '+' '.join(f'{100*x:.0f}' for x in v))
        row=dict(rank=n,objective=round(ob,4),post_tax_cagr=round(cg,4),positive_halves=round(pos*len(tr)),worst_half=round(float(v.min()),4),
                 range=round(float(v.max()-v.min()),4),oos_2026H1=round(float(o),4))
        for s,r,w,d,t in (('green',G[i],wg[i],dg[i],ng[i]),('red',B[j],wb[j],db[j],nb[j])):
            row.update({f'{s}_{k}':r[k] for k in ('structure','level','config','profit','stop')});row.update({f'{s}_worst_trade':w,f'{s}_train_max_dd':d,f'{s}_trades':int(t)})
        row.update({h:round(float(x),4) for h,x in zip(tr,v)});rows.append(row)
    with open(a.out,'w',newline='') as fh:
        w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
    if a.loyo:
        print('\nleave-one-year-out: the same pick on the other years; the picked pairs in the held-out year (each pair, %)')
        held=[]
        for y in per.years:
            k2,y2=ws.fold(per,y);P2=pick(G,B,VG,VB,okg,okb,per,k2,y2,a,a.top)
            hq=[q for q,p in enumerate(per.train) if p[:4]==y]
            if not P2:print(f'  {y}: no pair qualifies');continue
            res=[[VG[i][q]+VB[j][q] for q in hq] for i,j,*_ in P2];held+=[x for r in res for x in r]
            print(f"  {y}: "+'  '.join('/'.join(f'{100*x:.0f}' for x in r) for r in res))
        if held:print(f'  held-out pair-halves: {len(held)}, negative {sum(x<0 for x in held)}, mean {100*np.mean(held):.1f}%, worst {100*min(held):.1f}%')
    print(f'-> {a.out}')

if __name__=='__main__':main()
