#!/usr/bin/env python3
"""Of the trades whose open loss reached each drawdown level, what share ended in profit?

Population: every stored path (signal x side x structure x strike level) entered BEFORE the holdout (2025-01-01), held to its
natural end (zone change or expiry; no stop, no target), at the size it was simulated (V4.9.7: 40% of the margin-table lots).
Drawdown = the worst mark-to-market P&L at a 45-minute bar close during the trade, in rupees (intraday lows are not seen,
so true drawdowns are somewhat deeper). Ended in profit = final P&L after costs > 0. Paths of all signal configurations are
pooled and not sequenced (one path per signal bar). Groups below the fill-rate gate are skipped unless --all."""
import argparse,csv,json,sqlite3,sys
from pathlib import Path
import numpy as np
import zone_paths as zp

LEVELS=[25_000*i for i in range(1,15)]       # 25K .. 350K

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',default='zone_paths_levels.sqlite');ap.add_argument('--grid',default='zone_grid_v497',help='grid folder: its fill rates decide which groups are testable')
    ap.add_argument('--before',default='2025-01-01',help='entries before this date only (never the holdout)')
    ap.add_argument('--min-fill-rate',type=float,default=.75);ap.add_argument('--all',action='store_true',help='include groups below the fill-rate gate')
    ap.add_argument('--out',default='dd_recovery.csv')
    a=ap.parse_args(argv)
    rate={}
    gi=Path(a.grid)/'grid_index.json'
    if gi.exists():
        for e in json.loads(gi.read_text())['groups']:rate[(e['side'],e['structure'],float(e['offset']))]=e.get('fill_rate')
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);out=[]
    groups=db.execute("SELECT DISTINCT side,structure,offset FROM paths WHERE status='closed' ORDER BY 2,1,3").fetchall()
    for side,st,off in groups:
        r=rate.get((side,st,float(off)))
        if not a.all and r is not None and r<a.min_fill_rate:continue
        mae=[];fin=[]
        for fb,eb,pnl,blob in db.execute("SELECT first_bar,end_bar,end_pnl,data FROM paths WHERE side=? AND structure=? AND offset=? AND status='closed' AND entry_day<? AND end_pnl IS NOT NULL",(side,st,off,a.before)):
            m=zp.unpack(blob,eb-fb+1)['mark'];m=m[np.isfinite(m)]
            mae.append(min(0.,float(m.min())) if len(m) else 0.);fin.append(float(pnl))
        if not fin:continue
        mae=np.array(mae);fin=np.array(fin);row=dict(side=side,structure=st,level=int(off),fill_rate=r,trades=len(fin),profitable=round(float((fin>0).mean()),3),median_pnl=round(float(np.median(fin))))
        for L in LEVELS:
            hit=mae<=-L;n=int(hit.sum())
            row[f'n_{L//1000}k']=n;row[f'pct_{L//1000}k']=round(100*float((fin[hit]>0).mean()),1) if n else None
        out.append(row)
    db.close()
    if not out:sys.exit('no trades found')
    with open(a.out,'w',newline='') as f:w=csv.DictWriter(f,list(out[0]));w.writeheader();w.writerows(out)
    print(f'Share of trades (entered before {a.before}, held to their natural end) that ended in profit, among those whose open loss reached each level.')
    print('Cells: % ended in profit (number of trades that reached the level). Bar-close marks: intraday drawdowns are deeper.\n')
    hdr=f"{'group':24}{'trades':>7}{'win%':>6}"+''.join(f'{str(L//1000)+"K":>11}' for L in LEVELS);print(hdr)
    for r in out:
        cells=''.join(f"{(str(r[f'pct_{L//1000}k'])+'('+str(r[f'n_{L//1000}k'])+')') if r[f'n_{L//1000}k'] else '-':>11}" for L in LEVELS)
        print(f"{r['side']+' '+r['structure']+' '+format(r['level'],'+d'):24}{r['trades']:>7}{100*r['profitable']:>6.0f}"+cells)
    print(f'\n-> {a.out}')

if __name__=='__main__':main()
