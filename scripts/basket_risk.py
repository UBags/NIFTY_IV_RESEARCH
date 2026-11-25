"""Real combined risk of baskets of pairs from scripts/pair_rank.py (or pair_picker.py) output, from the paths file: daily
mark-to-market drawdown, worst combined open loss, worst single trade, worst day / month, peak margin exposure and overlap.

    python scripts/basket_risk.py --pairs runs/wide_search_v4/poolv3_2000/pair_rank_top100.csv \\
        --paths runs/zone_paths_v4914.sqlite --holidays config/nse_holidays.txt --basket 1,19 --basket 1,15,18,19

A basket of n pairs holds every leg at 1/n of full size (a pair alone: both legs at full size, as pair_rank / pair_picker count it).
Each leg is recomputed with the wide search's exact engine (same fills, exits at 45-minute bar closes, valued to --end), and:
  equity (daily mark-to-market, share of capital)  realised legs + open positions at each day's last bar close, as the wide
                                                    search's --max-dd uses it; train window = entries before --split
  max drawdown                                      train days / out-of-sample days (from the last train value) / whole span
  worst combined open loss                          the most negative sum of the basket's OPEN positions at a day's close
  worst single trade                                worst bar-close mark of one trade x its leg's size (as pair_rank's worst trade)
  peak exposure                                     largest sum over legs of size x [position open] on any 45-minute bar (1.0 = one
                                                    full-size position's margin)
Halves are checked against the pairs file (they must agree)."""
import argparse,collections,csv,datetime as dt,json,sqlite3,sys,time
import numpy as np
from nifty_iv.zone import grid as zg,rules as zr
from nifty_iv.diagnostics import wide_search as ws,trade_structure as ts

def parse_leg(s,side):
    p=s.split();prof,stop=p[3].split('/',1);return (side,p[0],float(p[1]),p[2],prof,stop)

def leg_series(E,b,per):
    """(daily equity, daily open-position marks, worst trade train, worst trade oos, per-trade (entry ord, exit ord, worst))."""
    _,_,_,_,oi,ov=E.run(b,per.olo,per.ohi,per.olo,per.olo,per.tstart[:1]);days=per.days
    Q=np.zeros(len(days));O=np.zeros(len(days));wt=wo=0.;trades=[]
    for i in oi[ov]:
        r=b[0];j=E.J[r,i];o=E.row_ord[i,:j+1];m=E.mf[i,:j+1];a=np.searchsorted(days,o[0]);x=np.searchsorted(days,E.XD[r,i])
        if x>a:mk=m[np.searchsorted(o,days[a:x],'right')-1]*E.u[i];Q[a:x]+=mk;O[a:x]+=mk
        Q[x:]+=E.PU[r,i];w=float(E.W[r,i]);trades.append((int(E.eord[i]),int(E.XD[r,i]),w))
        if E.eord[i]<per.omid:wt=min(wt,w)
        else:wo=min(wo,w)
    return Q/E.cap,O/E.cap,wt,wo,trades

def dd_info(Q,days):
    """(max drawdown, peak day, trough day, recovery day or None) of an equity curve starting at 0."""
    if not len(Q):return 0.,None,None,None
    E=np.concatenate([[0.],Q]);pk=np.maximum.accumulate(E);d=pk-E;t=int(d.argmax())
    if d[t]<=0:return 0.,None,None,None
    p=int(np.flatnonzero(E[:t+1]==pk[t])[-1]);rec=np.flatnonzero(E[t:]>=pk[t]);r=t+int(rec[0]) if len(rec) else None
    day=lambda k:dt.date.fromordinal(int(days[max(k-1,0)])).isoformat()
    return float(d[t]),day(p),day(t),(day(r) if r is not None else None)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pairs',required=True,help='pair_rank.py / pair_picker.py output (rank, green/red legs)')
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays');ap.add_argument('--gap-policy',default='busy')
    ap.add_argument('--basket',action='append',help='ranks in the pairs file, comma separated (repeatable; default 1,19 and 1,15,18,19)')
    ap.add_argument('--capital',type=float,default=2.5e6);ap.add_argument('--scale',type=float,help="default 1 / the paths file's target fraction")
    ap.add_argument('--train-start',default='2021-09-01');ap.add_argument('--split',default='2026-01-01');ap.add_argument('--end',default='2026-07-01')
    ap.add_argument('--tax',type=float,default=.35);ap.add_argument('--json',help='also write the figures here')
    a=ap.parse_args(argv);t0=time.time();baskets=[[int(x) for x in s.split(',')] for s in (a.basket or ['1,19','1,15,18,19'])]
    P={int(r['rank']):r for r in csv.DictReader(open(a.pairs))}
    def legs(r):
        if 'green' in r:return parse_leg(r['green'],'bull'),parse_leg(r['red'],'bear')                     # pair_rank.py
        return tuple((s,r[f'{c}_structure'],float(r[f'{c}_level']),r[f'{c}_config'],r[f'{c}_profit'],r[f'{c}_stop']) for c,s in (('green','bull'),('red','bear')))
    miss=[k for b in baskets for k in b if k not in P]
    if miss:sys.exit(f'ranks {miss} are not in {a.pairs}')
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta={}
    hol=zr.read_holidays(a.holidays) if a.holidays else [];bars=zr.BarArrays.from_db(db,hol);db.close()
    scale=a.scale or 1/float(meta.get('target_fraction') or 1.);per=ws.Periods(a.train_start,a.split,a.end,'half')
    per.days=np.array(sorted({o for o in bars.ord if per.olo<=o<per.ohi}),np.int64);per.ntd=int((per.days<per.omid).sum())
    nbars=len(bars.day);ntrain=int(sum(1 for d in bars.day if d<a.split))
    need=sorted({l for b in baskets for k in b for l in legs(P[k])},key=str);L={}
    print(f'{len(need)} legs, scale {scale:g}, capital Rs {a.capital:,.0f}; days {len(per.days)} ({per.ntd} train)',flush=True)
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for key in need:
            sd=ts.StrategyData(D,key,[],a.end,scale,features=False);bt=sd.b
            if bt is None:sys.exit(f'leg not in the paths file: {key}')
            E=ws.Engine(sd,per,a.capital);b=[sd.ref];vt,nt,ot,ox,_,_=E.train(b);vo,no,qt,qx,_,_=E.oos(b);E.attach_risk(bt)
            Q,O,wt,wo,tr=leg_series(E,b,per)
            L[key]=dict(train=np.asarray(vt),oos=np.asarray(vo),n=int(nt),Q=Q,O=O,wt=wt,wo=wo,trades=tr,occ=ws.occupancy(qt,qx,nbars))
            del E,sd,bt
            print(f"  {key[0]} {key[1]} {int(key[2]):+d} {key[3]} {key[4]}/{key[5]}: {nt} train trades, worst trade {100*wt:.1f}% (oos {100*wo:.1f}%), "
                  f"train DD alone {100*ws.drawdown(Q[:per.ntd]):.1f}%",flush=True)
    keep,yrs=ws.fold(per);mdays=np.array([dt.date.fromordinal(int(o)).strftime('%Y-%m') for o in per.days])
    def report(name,members):
        """members: [(leg key, weight)]"""
        Q=sum(w*L[k]['Q'] for k,w in members);O=sum(w*L[k]['O'] for k,w in members);vt=sum(w*L[k]['train'] for k,w in members)
        vo=sum(w*L[k]['oos'] for k,w in members);X=sum(w*L[k]['occ'] for k,w in members);n=per.ntd
        dtr=dd_info(Q[:n],per.days[:n]);doo=dd_info(Q[n:]-(Q[n-1] if n else 0.),per.days[n:]);dall=dd_info(Q,per.days)
        dQ=np.diff(np.concatenate([[0.],Q]));wd=int(dQ.argmin())
        me={};last=0.
        for m in sorted(set(mdays)):k=np.flatnonzero(mdays==m)[-1];me[m]=float(Q[k]-last);last=float(Q[k])
        wm=min(me,key=me.get);bm=max(me,key=me.get)
        wtr=min(w*L[k]['wt'] for k,w in members);woo=min(w*L[k]['wo'] for k,w in members)
        openk=np.stack([(np.abs(L[k]['O'])>0) for k,_ in members]).sum(0) if len(members)>1 else np.zeros(len(per.days))
        cg=float(ws.post_tax_cagr(vt,per.train,keep,yrs,a.tax))
        print(f"\n=== {name}")
        for k,w in members:print(f"    {w:.3g} x {k[0]} {k[1]} {int(k[2]):+d} {k[3]} {k[4]}/{k[5]}")
        print(f"  halves (train)        {' '.join(f'{100*x:.0f}' for x in vt)} | {'+'.join(per.oos) or 'out of sample'} {100*vo.sum():.1f}%   post-tax CAGR {100*cg:.1f}%")
        print(f"  max drawdown          train {100*dtr[0]:.1f}% ({dtr[1]} -> {dtr[2]}, recovered {dtr[3] or 'not by the split'})")
        print(f"                        out of sample {100*doo[0]:.1f}% ({doo[1]} -> {doo[2]}, recovered {doo[3] or 'not by '+a.end})")
        print(f"                        whole span {100*dall[0]:.1f}% ({dall[1]} -> {dall[2]})")
        print(f"  worst combined open   train {100*O[:n].min(initial=0):.1f}% on {dt.date.fromordinal(int(per.days[int(O[:n].argmin())])).isoformat() if n else '-'}; "
              f"out of sample {100*O[n:].min(initial=0):.1f}%" + (f" on {dt.date.fromordinal(int(per.days[n+int(O[n:].argmin())])).isoformat()}" if n<len(O) else ''))
        print(f"  worst single trade    train {100*wtr:.1f}%, out of sample {100*woo:.1f}% (bar-close mark x leg size)")
        print(f"  worst day             {100*dQ[wd]:.1f}% on {dt.date.fromordinal(int(per.days[wd])).isoformat()}; worst month {100*me[wm]:.1f}% ({wm}), best month {100*me[bm]:.1f}% ({bm}); "
              f"negative months {sum(v<0 for v in me.values())}/{len(me)}")
        print(f"  peak exposure         train {X[:ntrain].max(initial=0):.2f}, out of sample {X[ntrain:].max(initial=0):.2f} (1 = one full-size position)"
              + (f"; days with 2+ legs open {100*(openk>=2).mean():.0f}%, 3+ {100*(openk>=3).mean():.0f}%" if len(members)>1 else ''))
        return dict(name=name,legs=[[list(k),w] for k,w in members],train_halves=list(map(float,vt)),oos=float(vo.sum()),post_tax_cagr=cg,
                    max_dd_train=dtr[0],max_dd_oos=doo[0],max_dd_all=dall[0],dd_train_dates=dtr[1:],dd_oos_dates=doo[1:],
                    worst_open_train=float(O[:n].min(initial=0)),worst_open_oos=float(O[n:].min(initial=0)),worst_trade_train=wtr,worst_trade_oos=woo,
                    worst_day=float(dQ[wd]),worst_month=[wm,me[wm]],negative_months=sum(v<0 for v in me.values()),months=len(me),
                    peak_exposure_train=float(X[:ntrain].max(initial=0)),peak_exposure_oos=float(X[ntrain:].max(initial=0)))
    out=[];bad=[]
    for k in sorted({k for b in baskets for k in b}):
        g,r=legs(P[k]);res=report(f'pair #{k} alone (both legs at full size)',[(g,1.),(r,1.)]);out.append(res)
        H=[c for c in P[k] if len(c)==6 and c[4]=='H'] or [c for c in P[k] if c.startswith('train_20')]
        if H and max(abs(float(P[k][c])-x) for c,x in zip(H,res['train_halves']))>1e-3:bad.append(k)
    for b in baskets:
        w=collections.defaultdict(float)
        for k in b:
            for l in legs(P[k]):w[l]+=1/len(b)
        out.append(report(f"basket of pairs {', '.join('#'+str(k) for k in b)} (1/{len(b)} each)",list(w.items())))
    if bad:print(f'\nWARNING: halves differ from {a.pairs} for pairs {bad} (other --split / --scale / paths file?)')
    else:print(f'\nhalves agree with {a.pairs} for every pair')
    if a.json:open(a.json,'w').write(json.dumps(out,indent=1))
    print(f'({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
