"""Quarter (or half-year) returns of every strategy in a wide-search candidates.csv, recomputed from the paths file with the wide
search's own engine, for scripts/pair_picker.py --min-quarter.

    python scripts/strategy_periods.py --candidates runs/wide_search_v3/candidates.csv --paths runs/zone_paths_levels.sqlite \
        --holidays config/nse_holidays.txt --out runs/wide_search_v3/quarters.csv

Same windows as the wide search (--train-start, --split, --end, --capital; the defaults are the wide search's). Columns: the
strategy key, trades, then train_<period> and oos_<period> returns (share of --capital, full size). When candidates.csv
was made with the same --period (half by default) its train_ / oos_ columns must equal these: the script checks and reports."""
import argparse,csv,json,sqlite3,sys,time
import numpy as np
from nifty_iv.zone import grid as zg, rules as zr
from nifty_iv.diagnostics import trade_structure as ts, wide_search as ws

OPTIONS='naked,naked_m1,naked_m2,calendar,vert_w1,vert_w2,longopt,backspread_w1,backspread_w2,married,covered,collar_w1,collar_w2'

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--paths',required=True);ap.add_argument('--holidays')
    ap.add_argument('--gap-policy',default='busy');ap.add_argument('--capital',type=float,default=2.5e6)
    ap.add_argument('--period',choices=('quarter','half'),default='quarter')
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--structures',default=OPTIONS,help='strategies to compute (default: the option structures)');ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);t0=time.time();sts=set(a.structures.split(','))
    print('strategy_periods '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    rows=[r for r in csv.DictReader(open(a.candidates)) if r['structure'] in sts];seen=set();todo=[]
    for r in rows:
        k=ws.strategy_key(r)
        if k not in seen:seen.add(k);todo.append(r)
    todo.sort(key=lambda r:(r['side'],r['structure'],float(r['level'])))
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta={}
    db.close();scale=1/float(meta.get('target_fraction') or 1.);hol=zr.read_holidays(a.holidays) if a.holidays else []
    per=ws.Periods(a.train_start,a.split,a.end,a.period);out=[];diff=0;checked=0
    check=bool(todo) and all(f'train_{p}' in todo[0] for p in per.train) and all(f'oos_{p}' in todo[0] for p in per.oos)
    print(f'{len(todo)} strategies, {len(per.train)} train + {len(per.oos)} out-of-sample {a.period}s, scale {scale:g}',flush=True)
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for n,r in enumerate(todo,1):
            sd=ts.StrategyData(D,ws.strategy_key(r),[],a.end,scale,features=False)
            if sd.b is None:continue
            E=ws.Engine(sd,per,a.capital);b=[sd.ref];vt,nt,*_=E.train(b);vo,*_=E.oos(b)
            row={k:r[k] for k in ('side','structure','level','config','profit','stop')};row['trades']=int(nt)
            row.update({f'train_{p}':round(float(x),6) for p,x in zip(per.train,vt)});row.update({f'oos_{p}':round(float(x),6) for p,x in zip(per.oos,vo)})
            if check:
                checked+=1;ref=[float(r[f'train_{p}']) for p in per.train]+[float(r[f'oos_{p}']) for p in per.oos]
                diff+=int(max(abs(x-y) for x,y in zip(ref,list(vt)+list(vo)))>2e-6)
            out.append(row)
            if n%50==0:print(f'  {n}/{len(todo)} ({time.time()-t0:.0f}s)',flush=True)
    if not out:sys.exit('no strategy computed')
    with open(a.out,'w',newline='') as fh:
        w=csv.DictWriter(fh,list(out[0]));w.writeheader();w.writerows(out)
    if check:print(f'check against candidates.csv: {checked} strategies, {diff} differ')
    print(f'-> {a.out} ({len(out)} strategies, {time.time()-t0:.0f}s)')

if __name__=='__main__':main()
