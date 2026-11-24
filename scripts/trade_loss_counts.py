"""Per-trade worst open loss of wide-search candidates: how many trades breach each threshold, per strategy.

    python scripts/trade_loss_counts.py --candidates runs/wide_search_v3/candidates.csv --paths runs/zone_paths_levels.sqlite \
        --holidays config/nse_holidays.txt --out runs/wide_search_v3/trade_losses.csv

Worst open loss of a trade = min(worst 45-minute bar-close mark up to its exit, its final result, 0) x units / --capital, at full
size (the wide search's and strategy_scan's definition; intraday lows are not seen, so true losses are somewhat deeper). Trades
entered in [--train-start, --end) under the strategy's own sequencing. --structures limits the strategies (default: the options).
Writes one row per strategy (trades, worst, trades beyond each threshold, by entry year) and prints the summary."""
import argparse,collections,csv,json,sqlite3,sys,time
import numpy as np
from nifty_iv.zone import grid as zg, rules as zr
from nifty_iv.diagnostics import trade_structure as ts, wide_search as ws

THR=(.03,.05,.075,.099,.10,.125)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--paths',required=True);ap.add_argument('--holidays')
    ap.add_argument('--gap-policy',default='busy');ap.add_argument('--capital',type=float,default=2.5e6)
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--entry-end',help='count only trades entered before this date (default --end; trades still run to --end)')
    ap.add_argument('--structures',default='naked,naked_m1,naked_m2,calendar,vert_w1,vert_w2,longopt,backspread_w1,backspread_w2,married,covered,collar_w1,collar_w2')
    ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);t0=time.time();sts=set(a.structures.split(','))
    rows=[r for r in csv.DictReader(open(a.candidates)) if r['structure'] in sts]
    seen=set();keys=[]
    for r in rows:
        k=(r['side'],r['structure'],float(r['level']),r['config'],r['profit'],r['stop'])
        if k not in seen:seen.add(k);keys.append(k)
    keys.sort(key=lambda k:(k[0],k[1],k[2]))
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta={}
    db.close();scale=1/float(meta.get('target_fraction') or 1.);hol=zr.read_holidays(a.holidays) if a.holidays else []
    per=ws.Periods(a.train_start,a.split,a.end,'half');out=[]
    ehi=per.ohi if not a.entry_end else ws.Periods(a.train_start,a.entry_end,a.end,'half').omid
    print(f'{len(keys)} strategies ({", ".join(sorted(sts & {k[1] for k in keys}))}), scale {scale:g}',flush=True)
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for n,k in enumerate(keys,1):
            sd=ts.StrategyData(D,k,[],a.end,scale,features=False);bt=sd.b
            if bt is None:continue
            E=ws.Engine(sd,per,a.capital);E.attach_risk(bt);r=sd.ref
            _,_,_,_,oi,ov=E.run([r],per.olo,ehi,per.olo,per.olo,per.tstart[:1]);ix=oi[ov]
            w=E.W[r,ix];yr=E.eyear[ix]
            row=dict(side=k[0],structure=k[1],level=int(k[2]),config=k[3],profit=k[4],stop=k[5],trades=len(ix),worst=round(float(w.min(initial=0.)),5))
            for T in THR:
                row[f'beyond_{100*T:g}pct']=int((w<-T).sum())
                row[f'beyond_{100*T:g}pct_years']=' '.join(f'{y}:{c}' for y,c in sorted(collections.Counter(yr[w<-T].tolist()).items()))
            out.append(row)
            if n%50==0:print(f'  {n}/{len(keys)} ({time.time()-t0:.0f}s)',flush=True)
    with open(a.out,'w',newline='') as fh:
        wr=csv.DictWriter(fh,list(out[0]));wr.writeheader();wr.writerows(out)
    print(f'\n{len(out)} strategies; trades entered {a.train_start}..{a.entry_end or a.end}; worst open loss at full size, share of Rs {a.capital:,.0f}')
    print(f"{'threshold':>10} {'strategies breaching':>21} {'trades breaching per breaching strategy: median / max':>56} {'share of their trades':>22}")
    for T in THR:
        b=[x for x in out if x[f'beyond_{100*T:g}pct']>0];c=[x[f'beyond_{100*T:g}pct'] for x in b]
        sh=sum(c)/max(1,sum(x['trades'] for x in b))
        print(f"{100*T:9.1f}% {len(b):>10} of {len(out):<8} {(f'{int(np.median(c))} / {max(c)}' if c else '-'):>40} {100*sh:>21.1f}%")
    print(f'-> {a.out} ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
