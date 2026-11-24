"""Quarter-by-quarter (and half-year) returns of the pairs in a pair_picker output file, and of their equal-weight basket.

    python scripts/pair_report.py --pairs runs/wide_search_v3/pairs.csv --candidates runs/wide_search_v3/candidates.csv \
        --quarters runs/wide_search_v3/quarters.csv [--csv runs/wide_search_v3/pairs_quarters.csv]

Returns are shares of the capital at full size for a pair (green leg + red leg), and 1/k each for the basket of the k pairs.
2021Q3 holds September only (the train window starts 2021-09-01); 2026Q1-Q2 are out of sample unless the pairs were picked with
--include-oos."""
import argparse,csv,sys
import numpy as np

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pairs',required=True);ap.add_argument('--candidates',required=True);ap.add_argument('--quarters',required=True)
    ap.add_argument('--csv',help='also write the table here');a=ap.parse_args(argv)
    key=lambda s,st,lv,cf,pr,sp:(s,st,float(lv),cf,pr,sp)
    Q=list(csv.DictReader(open(a.quarters)));qi={key(r['side'],r['structure'],r['level'],r['config'],r['profit'],r['stop']):r for r in Q}
    C={key(r['side'],r['structure'],r['level'],r['config'],r['profit'],r['stop']):r for r in csv.DictReader(open(a.candidates))}
    qc=[k for k in Q[0] if k.startswith('train_20') or k.startswith('oos_20')];hc=[k for k in next(iter(C.values())) if k.startswith('train_20') or k.startswith('oos_20')]
    lab=[k.split('_',1)[1] for k in qc];hl=[k.split('_',1)[1] for k in hc]
    rows=[];names=[]
    for p in csv.DictReader(open(a.pairs)):
        g=key('bull',p['green_structure'],p['green_level'],p['green_config'],p['green_profit'],p['green_stop'])
        b=key('bear',p['red_structure'],p['red_level'],p['red_config'],p['red_profit'],p['red_stop'])
        if g not in qi or b not in qi:sys.exit(f"pair {p['rank']}: a leg is not in {a.quarters} (make it with strategy_periods.py / stage1_pool.py from the same pool)")
        q=np.array([float(qi[g][c])+float(qi[b][c]) for c in qc]);h=np.array([float(C[g][c])+float(C[b][c]) for c in hc]) if g in C and b in C else None
        rows.append((q,h));names.append(f"{p['rank']}. G {g[1]} {int(g[2]):+d} {g[3]} {g[4]}/{g[5]}  |  R {b[1]} {int(b[2]):+d} {b[3]} {b[4]}/{b[5]}")
    if not rows:sys.exit('no pairs')
    k=len(rows);bq=sum(q for q,_ in rows)/k;full=[i for i,l in enumerate(lab) if l!='2021Q3']
    tr=[i for i in full if qc[i].startswith('train_')];oo=[i for i,c in enumerate(qc) if c.startswith('oos_')]
    w=max(12,max(len(l) for l in lab)+1);print(f'returns in % of capital; pair = both legs at full size, basket = 1/{k} each\n')
    for n in names:print(n)
    print('\n'+'quarter'.ljust(w)+''.join(f'{"pair "+str(i+1):>9}' for i in range(k))+f'{"basket":>9}')
    for i,l in enumerate(lab):
        if i==len(lab)-len(oo) and oo:print('-'*(w+9*(k+1))+'  out of sample')
        print(l.ljust(w)+''.join(f'{100*q[i]:9.1f}' for q,_ in rows)+f'{100*bq[i]:9.1f}'+('   (Sep only)' if l=='2021Q3' else ''))
    print('-'*(w+9*(k+1)))
    for nm,ix in (('train',tr),('oos',oo)):
        if not ix:continue
        print(f'{nm+" neg":<{w}}'+''.join(f'{int((q[ix]<0).sum()):>6}/{len(ix):<2}' for q,_ in rows)+f'{int((bq[ix]<0).sum()):>6}/{len(ix):<2}')
        print(f'{nm+" worst":<{w}}'+''.join(f'{100*q[ix].min():9.1f}' for q,_ in rows)+f'{100*bq[ix].min():9.1f}')
        print(f'{nm+" mean":<{w}}'+''.join(f'{100*q[ix].mean():9.1f}' for q,_ in rows)+f'{100*bq[ix].mean():9.1f}')
    if all(h is not None for _,h in rows):
        bh=sum(h for _,h in rows)/k;print('\n'+'half'.ljust(w)+' '+' '.join(hl))
        for i,(_,h) in enumerate(rows):print(f'pair {i+1}'.ljust(w)+' '+' '.join(f'{100*x:{len(l)}.0f}' for x,l in zip(h,hl)))
        print('basket'.ljust(w)+' '+' '.join(f'{100*x:{len(l)}.0f}' for x,l in zip(bh,hl)))
    if a.csv:
        with open(a.csv,'w',newline='') as fh:
            wr=csv.writer(fh);wr.writerow(['quarter']+[f'pair_{i+1}' for i in range(k)]+['basket'])
            for i,l in enumerate(lab):wr.writerow([l]+[round(float(q[i]),5) for q,_ in rows]+[round(float(bq[i]),5)])
        print(f'-> {a.csv}')

if __name__=='__main__':main()
