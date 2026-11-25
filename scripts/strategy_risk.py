"""Worst open trade loss and daily train equity of every strategy in a candidates.csv, for all train years and with each train year's
entries left out, recomputed with the wide search's engine (Engine.risk, as the wide search's leave-one-year-out). For
scripts/pair_picker.py --rank v4: a pair's drawdown is the drawdown of its two legs' summed daily equity (green and red positions do
not overlap, both at full size), and no held-out year's trades enter that year's pick.

    python scripts/strategy_risk.py --candidates runs/wide_search_v3/candidates.csv --paths runs/zone_paths_levels.sqlite \
        --holidays config/nse_holidays.txt --out runs/wide_search_v3/risk.npz

Same windows as the wide search (--train-start, --split, --end, --capital). Writes --out (npz): keys (strategy keys, '|'-joined),
years (train years), days (train day ordinals), worst (n, 1 + years) = worst open trade loss (share of capital, <= 0; column 0 all
years, then each year left out), equity (n, 1 + years, days) float32 = daily mark-to-market P&L / capital. Checks column 0 against
the candidates' worst_trade and train_max_dd and reports differences."""
import argparse,csv,json,sqlite3,sys,time
import numpy as np
from nifty_iv.zone import grid as zg, rules as zr
from nifty_iv.diagnostics import trade_structure as ts, wide_search as ws

OPTIONS='naked,naked_m1,naked_m2,calendar,vert_w1,vert_w2,longopt,backspread_w1,backspread_w2,married,covered,collar_w1,collar_w2'
def key_text(r):return '|'.join(str(x) for x in ws.strategy_key(r))

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--candidates',required=True);ap.add_argument('--paths',required=True);ap.add_argument('--holidays')
    ap.add_argument('--gap-policy',default='busy');ap.add_argument('--capital',type=float,default=2.5e6)
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--structures',default=OPTIONS,help='strategies to compute (default: the option structures)');ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);t0=time.time();sts=set(a.structures.split(','))
    print('strategy_risk '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    todo=[];seen=set()
    for r in csv.DictReader(open(a.candidates)):
        k=key_text(r)
        if r['structure'] in sts and k not in seen:seen.add(k);todo.append(r)
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta={}
    hol=zr.read_holidays(a.holidays) if a.holidays else [];bars=zr.BarArrays.from_db(db,hol);db.close()
    scale=1/float(meta.get('target_fraction') or 1.)
    per=ws.Periods(a.train_start,a.split,a.end,'half')
    per.days=np.array(sorted({o for o in bars.ord if per.olo<=o<per.ohi}),np.int64);per.ntd=int((per.days<per.omid).sum())
    folds=[None]+per.years;keys=[];W=[];EQ=[];diff=[0,0];checked=0
    print(f'{len(todo)} strategies, {per.ntd} train days, folds: all + {", ".join(per.years)} left out; scale {scale:g}',flush=True)
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for n,r in enumerate(todo,1):
            sd=ts.StrategyData(D,ws.strategy_key(r),[],a.end,scale,features=False);bt=sd.b
            if bt is None:continue
            E=ws.Engine(sd,per,a.capital);E.attach_risk(bt);b=[sd.ref];w=[];eq=[]
            for y in folds:
                wy,Ey=E.risk(b,y);w.append(wy);eq.append(np.asarray(Ey[:per.ntd],np.float32))
            if 'worst_trade' in r and r['worst_trade']!='':
                checked+=1;diff[0]+=int(abs(w[0]-float(r['worst_trade']))>1e-6)
                diff[1]+=int('train_max_dd' in r and abs(float(ws.drawdown(eq[0].astype(float)))-float(r['train_max_dd']))>1e-5)
            keys.append(key_text(r));W.append(w);EQ.append(np.stack(eq))
            del E,sd,bt
            if n%100==0:print(f'  {n}/{len(todo)} ({time.time()-t0:.0f}s)',flush=True)
    if not keys:sys.exit('no strategy computed')
    np.savez_compressed(a.out,keys=np.array(keys),years=np.array(per.years),days=per.days[:per.ntd],worst=np.array(W,float),equity=np.stack(EQ))
    print(f'check against {a.candidates}: {checked} strategies, worst trade differs {diff[0]}, train drawdown differs {diff[1]} (must be 0)')
    print(f'-> {a.out} ({len(keys)} strategies, {time.time()-t0:.0f}s)')

if __name__=='__main__':main()
