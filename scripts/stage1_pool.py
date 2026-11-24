"""A wider pool for scripts/pair_picker.py: the top --top strategies per side from a wide search's stage-1 files (every strategy
stage 1 kept, not only the --pool-per-family pool in candidates.csv), recomputed exactly with half-year and quarter returns and
the train account drawdown.

    python scripts/stage1_pool.py --run runs/wide_search_v3 --paths runs/zone_paths_levels.sqlite --holidays config/nse_holidays.txt \
        --top 1000 --out runs/wide_search_v3/pool1000
    python scripts/pair_picker.py --candidates runs/wide_search_v3/pool1000/candidates.csv \
        --quarters runs/wide_search_v3/pool1000/quarters.csv --funnel --loyo --out runs/wide_search_v3/pool1000/pairs.csv

Stage 1 keeps, per signal configuration, its best --per-config exits under each of the wide search's scorings, so its files hold
far more strategies than the pool. Kept here: --structures (default: the options), train trades >= --min-trades, worst open trade
loss within --max-trade-loss, a finite stage-1 score; identical results once; then the --top best per side by the stage-1 score on
all train years (--top 0: all of them). Note: stage 1 already dropped strategies whose worst trade exceeded its own limit
(--max-trade-loss / smallest --weights of that run).
Each kept strategy is recomputed with the wide search's engine (same windows, read from the run's stage-1 signature when present):
halves and quarters, train and out of sample, trades, worst trade, train account drawdown (daily mark-to-market, as the wide search).
The stage-1 figures are checked against the recomputed ones (they must agree). Writes candidates.csv (pair_picker's columns) and
quarters.csv (strategy_periods' columns) in --out."""
import argparse,collections,csv,glob,gzip,json,os,sqlite3,sys,time
import datetime as dt
import numpy as np
from nifty_iv.zone import grid as zg, rules as zr
from nifty_iv.diagnostics import trade_structure as ts, wide_search as ws

OPTIONS='naked,naked_m1,naked_m2,calendar,vert_w1,vert_w2,longopt,backspread_w1,backspread_w2,married,covered,collar_w1,collar_w2'

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run',required=True,help='wide-search --out directory (holds stage1/)');ap.add_argument('--paths',required=True)
    ap.add_argument('--holidays');ap.add_argument('--gap-policy',default='busy');ap.add_argument('--capital',type=float,default=2.5e6)
    ap.add_argument('--train-start',default=None,help='default: from the run, else 2021-09-01');ap.add_argument('--split',default=None,help='default: from the run, else 2026-01-01')
    ap.add_argument('--end',default=None,help='default: from the run, else 2026-07-01')
    ap.add_argument('--structures',default=OPTIONS);ap.add_argument('--min-trades',type=int,default=15)
    ap.add_argument('--max-trade-loss',type=float,default=.099);ap.add_argument('--top',type=int,default=1000,help='best per side (0 = all)')
    ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);t0=time.time();sts=set(a.structures.split(','));out=os.path.abspath(a.out);os.makedirs(out,exist_ok=True)
    files=sorted(glob.glob(os.path.join(a.run,'stage1','*.json.gz')))
    if not files:sys.exit(f'no stage-1 files in {a.run}/stage1')
    rows=[];sig=None
    for f in files:
        with gzip.open(f,'rt') as fh:d=json.load(fh)
        sig=sig or d.get('signature');rows+=[r for r in d['result'].get('rows',[]) if r['structure'] in sts]
    sig=sig or {}
    for k,dflt in (('train_start','2021-09-01'),('split','2026-01-01'),('end','2026-07-01')):
        if getattr(a,k) is None:setattr(a,k,sig.get(k,dflt))
    rp=sig.get('period','half')                                             # stage-1 vectors are in the run's period (checked against it)
    if sig.get('capital') not in (None,a.capital):print(f"note: the run used capital {sig['capital']}, here {a.capital}")
    print('stage1_pool '+' '.join(f'--{k.replace("_","-")} {v}' for k,v in vars(a).items()),flush=True)
    print(f'{len(files)} stage-1 files, {len(rows):,} option strategies',flush=True)
    keep=collections.defaultdict(list);seen=set()
    for r in rows:
        if r['trades']<a.min_trades or r['worst_trade']<-a.max_trade_loss or not np.isfinite(r['scores'][0]):continue
        k=(r['side'],tuple(r['train']),tuple(r['oos']),r['trades'],r['worst_trade'])
        if k in seen:continue
        seen.add(k);keep[r['side']].append(r)
    for s in keep:keep[s].sort(key=lambda r:-r['scores'][0])
    print('within the limits (distinct results): '+', '.join(f'{s} {len(v):,}' for s,v in sorted(keep.items())),flush=True)
    todo=[r for s in sorted(keep) for r in (keep[s][:a.top] if a.top else keep[s])]
    todo.sort(key=lambda r:(r['side'],r['structure'],r['level']))
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta={}
    hol=zr.read_holidays(a.holidays) if a.holidays else [];bars=zr.BarArrays.from_db(db,hol);db.close()
    scale=float(sig.get('scale') or 1/float(meta.get('target_fraction') or 1.))
    ph=ws.Periods(a.train_start,a.split,a.end,'half');pq=ws.Periods(a.train_start,a.split,a.end,'quarter')
    ph.days=np.array(sorted({o for o in bars.ord if ph.olo<=o<ph.ohi}),np.int64);ph.ntd=int((ph.days<ph.omid).sum())
    print(f'recomputing {len(todo):,} strategies (scale {scale:g}) ...',flush=True)
    C=[];Q=[];mism=0
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for n,r in enumerate(todo,1):
            key=(r['side'],r['structure'],float(r['level']),r['config'],r['profit'],r['stop'])
            sd=ts.StrategyData(D,key,[],a.end,scale,features=False);bt=sd.b
            if bt is None:continue
            b=[sd.ref];Eh=ws.Engine(sd,ph,a.capital);Eq=ws.Engine(sd,pq,a.capital)
            vt,nt,*_=Eh.train(b);vo,*_=Eh.oos(b);qt,*_=Eq.train(b);qo,*_=Eq.oos(b)
            Eh.attach_risk(bt);w,E=Eh.risk(b);dd=float(ws.drawdown(E[:ph.ntd]))
            ref=list(vt)+list(vo) if rp=='half' else list(qt)+list(qo)
            if len(ref)!=len(r['train'])+len(r['oos']) or max(abs(x-y) for x,y in zip(ref,r['train']+r['oos']))>1e-9 or int(nt)!=r['trades'] or abs(w-r['worst_trade'])>1e-9:mism+=1
            base=dict(side=r['side'],structure=r['structure'],level=r['level'],config=r['config'],profit=r['profit'],stop=r['stop'],trades=int(nt))
            C.append(dict(base,worst_trade=round(float(w),6),train_max_dd=round(dd,6),score_all=round(float(r['scores'][0]),6),
                          **{f'train_{p}':round(float(x),6) for p,x in zip(ph.train,vt)},**{f'oos_{p}':round(float(x),6) for p,x in zip(ph.oos,vo)}))
            Q.append(dict(base,**{f'train_{p}':round(float(x),6) for p,x in zip(pq.train,qt)},**{f'oos_{p}':round(float(x),6) for p,x in zip(pq.oos,qo)}))
            del Eh,Eq,sd,bt
            if n%200==0:print(f'  {n:,}/{len(todo):,} ({time.time()-t0:.0f}s)',flush=True)
    if not C:sys.exit('nothing recomputed')
    for name,R in (('candidates.csv',C),('quarters.csv',Q)):
        with open(os.path.join(out,name),'w',newline='') as fh:
            w_=csv.DictWriter(fh,list(R[0]));w_.writeheader();w_.writerows(R)
    cnt=collections.Counter(r['side'] for r in C)
    print(f'stage-1 vs recomputed: {mism} differ (must be 0)')
    print(f'-> {out}/candidates.csv, quarters.csv ({", ".join(f"{s} {n}" for s,n in sorted(cnt.items()))}; {time.time()-t0:.0f}s)')

if __name__=='__main__':main()
