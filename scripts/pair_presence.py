"""Where do the pairs picked from one pool stand in another pool?

    python scripts/pair_presence.py --pairs runs/wide_search_v3/pairs_t15_h5_q-0.025_half.csv \
        --candidates runs/wide_search_v3/pool3000/candidates.csv --quarters runs/wide_search_v3/pool3000/quarters.csv \
        --min-half 0.05 --min-quarter -0.025 --include-oos half

For each picked pair: is each leg in the other pool (and its rank there by the stage-1 score on all train years, per side); does
the pair pass the other pool's criteria (same pair_picker arguments); how many qualifying pairs there rank above it by the
objective; and, if it was not picked there, which already-picked pair took its green or red signal (--distinct). All other
arguments are pair_picker's and must match the run being compared."""
import csv,importlib.util,sys
from pathlib import Path
import numpy as np
spec=importlib.util.spec_from_file_location('pair_picker',Path(__file__).with_name('pair_picker.py'));pp=importlib.util.module_from_spec(spec);spec.loader.exec_module(pp)
ws=pp.ws

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if '--pairs' not in argv:sys.exit('--pairs FILE (a pair_picker output) is required')
    i=argv.index('--pairs');pairs_file=argv[i+1];del argv[i:i+2]
    if '--out' not in argv:argv+=['--out','/dev/null']
    cap={}
    orig=pp.pick
    def spy(*x,**k):                                                         # capture the main pick and its inputs
        r=orig(*x,**k)
        if k.get('held') is None and 'main' not in cap:cap['main']=(x,k,r)
        return r
    pp.pick=spy
    import contextlib,io
    with contextlib.redirect_stdout(io.StringIO()):
        try:pp.main(argv+['--top','5'] if '--top' not in argv else argv)
        except SystemExit:pass
    if 'main' not in cap:sys.exit('pair_picker did not run its pick (check the arguments)')
    x,k,P=cap['main'];G,B,VG,VB,okg,okb,per,keep,yrs,a,top=x[:11];F=x[11] if len(x)>11 else k.get('F')
    key=lambda s,r:(s,r['structure'],float(r['level']),r['config'],r['profit'],r['stop'])
    gi={key('bull',r):n for n,r in enumerate(G)};bi={key('bear',r):n for n,r in enumerate(B)}
    rank=lambda S,n:1+sum(float(r.get('score_all',0) or 0)>float(S[n].get('score_all',0) or 0) for r in S)
    C=dict(tax=a.tax,target=a.cagr_floor,ramp_from=None,range_floor=a.range_floor,range_cap=a.range_cap)
    ga=np.flatnonzero(okg);ba=np.flatnonzero(okb);V=VG[ga][:,None,:]+VB[ba][None,:,:]
    cg=ws.post_tax_cagr(V,per.train,keep,yrs,a.tax);ob=ws.composite(V,per.train,keep,yrs,C);Vk=V[...,keep]
    m=(cg>=a.cagr_floor)&((Vk>0).mean(-1)>=a.min_positive-1e-9)&(Vk.max(-1)-Vk.min(-1)<=a.range_cap)
    if a.min_half is not None or a.min_quarter is not None:m&=pp.floors_ok(ga,ba,F,a,None)
    picked=[(G[i],B[j]) for i,j,*_ in P]
    print(f'other pool: green {len(G)} ({len(ga)} within the leg limits), red {len(B)} ({len(ba)}); qualifying pairs {int(m.sum()):,}; its picks:')
    for n,(g,b) in enumerate(picked,1):print(f'  {n}. obj {P[n-1][3]:.2f}  G {pp.key(g,"rule")[1:]}  R {pp.key(b,"rule")[1:]}')
    print()
    for r in csv.DictReader(open(pairs_file)):
        g=('bull',r['green_structure'],float(r['green_level']),r['green_config'],r['green_profit'],r['green_stop'])
        b=('bear',r['red_structure'],float(r['red_level']),r['red_config'],r['red_profit'],r['red_stop'])
        print(f"pair {r['rank']} (obj {float(r['objective']):.2f}): G {g[1]} {int(g[2]):+d} {g[3]} {g[4]}/{g[5]} | R {b[1]} {int(b[2]):+d} {b[3]} {b[4]}/{b[5]}")
        ig,ib=gi.get(g),bi.get(b)
        for nm,ix,S,ok in (('green',ig,G,okg),('red',ib,B,okb)):
            print(f'   {nm}: '+('NOT in the other pool' if ix is None else f'in the pool, stage-1 rank {rank(S,ix)} of {len(S)}'+('' if ok[ix] else ', outside the leg limits')))
        if ig is None or ib is None or not okg[ig] or not okb[ib]:continue
        x,y=int(np.searchsorted(ga,ig)),int(np.searchsorted(ba,ib))
        if not m[x,y]:print('   pair fails the criteria there');continue
        above=int((m&(ob>ob[x,y])).sum());print(f'   qualifies; {above:,} qualifying pairs rank above it (objective {ob[x,y]:.2f})')
        if any(pp.key(G[i],'rule')==pp.key(G[ig],'rule') and pp.key(B[j],'rule')==pp.key(B[ib],'rule') for i,j,*_ in P):print('   picked there too');continue
        bl=[n for n,(i,j,*_) in enumerate(P,1) if ob[int(np.searchsorted(ga,i)),int(np.searchsorted(ba,j))]>=ob[x,y]
            and (pp.key(G[i],a.distinct)==pp.key(G[ig],a.distinct) or pp.key(B[j],a.distinct)==pp.key(B[ib],a.distinct))]
        print('   not picked: '+(f'its {a.distinct} is taken by pick {bl[0]}' if bl else f'{min(above,len(P))}+ higher pairs filled the {len(P)} places first'))

if __name__=='__main__':main()
