#!/usr/bin/env python3
"""Trade structuring: exit baskets, entry scoring and sizing, several strategies in one account, judged on period stability.

Goal (user, V4.9.12): fewer negative quarters / half-years and a lower spread of period returns, at an acceptable average return.

1. Exit baskets. Each strategy keeps its own entries (its signal configuration, side, structure, level). Its position is split into
   --baskets equal slices; each slice exits by its own (profit rule, stop rule) pair. A new signal is taken only when every slice
   of the previous trade has exited (one position's margin at most). Each slice's P&L is booked on its own exit day. The pairs come
   from a fixed candidate list (CANDIDATE_PROFITS x CANDIDATE_STOPS, valid for the structure; --profits / --stops override) and are
   chosen by search: greedy forward selection (a pair may fill several slices), then single-slice swaps until no improvement,
   strategies optimised in turn (coordinate rounds) on the COMBINED account.
2. Objective, on entries in [--train-start, --split) only: maximise the worst --period return (half or quarter), subject to an
   average yearly return >= --min-annual (share of --capital at full size), shortfall penalised; ties: lower spread of period
   returns. The reference (each strategy's own exit, in every slice) is reported alongside. Periods from --split on are reported
   as out of sample (2025-26 was examined before: see docs/CODEMAP_V4_9_12.md).
3. Entry scoring and sizing (--sizing), applied to the chosen structure's trades:
     meta   meta-labelling (Lopez de Prado): a secondary model scores each entry at signal time; walk-forward by calendar year
            (trained only on trades that EXITED before the year starts, all strategies pooled, at least --min-train trades), an
            L2 logistic regression on standardised features predicts P(trade profitable). Its training-score terciles map a trade
            to low / medium / high and to --size-map lots (default 4,8,12 of 12). Years without enough history trade medium.
     vol    volatility management (Moreira-Muir): size = the size-map step nearest to median(trailing ATR15/close) / (ATR15/close
            at the signal), capped to [low, high].
     none   full size.
   Features (all known at the signal bar's close; the entry premium is the fill, known within the entry window): ATR15/close,
   its percentile over the trailing 2,000 bars, bars since the zone turned this colour, side-signed index return over 9 and 45
   bars, entry premium / ATR15, calendar days to expiry, bar end minute, weekday, result of the strategy's last CLOSED trade.
   Reported: out-of-sample P&L per tercile (does the score rank trades?), and the account with sizing vs at constant size with the
   same average exposure (sizing that only lowers exposure is not an improvement).
4. Every strategy's period returns, their correlation (are they diversifying?), and per configuration: average yearly return,
   period spread, worst period, negative periods, Sharpe of period returns, closed-trade drawdown.

Rupees: per-unit P&L from the paths file (after slippage and charges at (O+H+L+C)/4 fills and bar-close exits; no interest) x the
path's units x --scale (default 1 / the paths' --target-fraction: full size). Replay the final structure through
nifty-diag-trade-ledger (conservative fills, intrabar stop, interest) before trusting the rupees. Searching many structures on
the same data overfits: prefer the out-of-sample columns and structures whose neighbours also work."""
import argparse,collections,csv,datetime as dt,json,math,sqlite3,statistics as stt,sys
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.zone import signals as zs
from nifty_iv.core import cli

CANDIDATE_PROFITS=('none','ptime3','ptime5','ptime7','tp50','tp75','tp90','target15x1','target15x2','tpct1')
CANDIDATE_STOPS=('none','mloss1.5','mloss3','mloss5','atr15x1','atr15x2','eatr30x1.5','pct0.5','trail15x1.5','gb1x0.5','be1c0','time3','time5','exp1')
FEATURES=('atr_rel','atr_pct','zone_age','mom9','mom45','prem_atr','dte','tod','dow','prev_result')

def period_of(day,kind):
    y,m=int(day[:4]),int(day[5:7])
    return f'{y}H{1 if m<=6 else 2}' if kind=='half' else f'{y}Q{(m-1)//3+1}'

# ------------------------------------------------------------------ data per strategy
class StrategyData:
    """A strategy's signals in time order, and for every candidate pair and filled path: per-unit P&L, exit bar, exit day."""
    def __init__(self,D,key,pairs,end,scale,features=True):
        side,st,off,cf,pr,sp=key;self.key=key;self.name='|'.join(map(str,key));self.side=side
        b,index=D.group(side,st,float(off));self.b=b;self.index=index;self.gaps=dict(D.gaps);self.scale=scale
        valid_p=set(zr.profit_rules(st));valid_s=set(zr.stop_rules(st))
        pairs=[(p,s) for p,s in pairs if p in valid_p and s in valid_s]
        if (pr,sp) not in pairs:pairs=[(pr,sp)]+pairs                              # the strategy's own exit is always a candidate
        self.pairs=pairs;self.ref=pairs.index((pr,sp));self.signals=list(D.signals[cf,side])
        h=dt.date.fromisoformat(end).toordinal();self.end=end
        if b is None:self.P=np.zeros((len(pairs),0));self.XB=self.XD=np.zeros((len(pairs),0),np.int64);self.feat={};return
        P=sorted({p for p,_ in pairs});S=sorted({s for _,s in pairs})
        o=zr.outcomes(b,P,S,[h]);pi={p:i for i,p in enumerate(P)};si={s:i for i,s in enumerate(S)}
        self.P=np.stack([o['vh'][0,pi[p],si[s]] for p,s in pairs])                  # per-unit P&L valued at end (pairs, paths)
        self.XB=np.stack([o['xb'][pi[p],si[s]] for p,s in pairs]).astype(np.int64)
        xd=np.stack([o['xd'][pi[p],si[s]] for p,s in pairs]).astype(np.int64);self.XD=np.minimum(xd,h)
        self.units=b.units*scale;self.entry=[p['entry_day'] for p in b.paths]
        self.feat=signal_features(D,b,index,side,self.signals) if features else {}

def signal_features(D,b,index,side,signals):
    """{signal bar: feature vector} for filled signals (FEATURES order, except prev_result filled in during simulation)."""
    bars=D.bars;close=bars.close;atr=bars.atr[15];rel=atr/close;n=len(close)
    zone=[z for (z,) in D.db.execute('SELECT zone FROM bars ORDER BY idx')];col='G' if side=='bull' else 'R'
    age=np.zeros(n);run=0
    for k in range(n):run=run+1 if zone[k]==col else 0;age[k]=run
    S=1. if side=='bull' else -1.;out={}
    for t in signals:
        i=index.get(t)
        if i is None:continue
        p=b.paths[i];lo=max(0,t-2000);w=rel[lo:t+1];w=w[np.isfinite(w)]
        pct=float((w<=rel[t]).mean()) if len(w) and math.isfinite(rel[t]) else .5
        m9=S*(close[t]/close[max(0,t-9)]-1);m45=S*(close[t]/close[max(0,t-45)]-1)
        prem=(p.get('entry_short_px') or math.nan)/atr[t] if atr[t] and math.isfinite(atr[t]) else math.nan
        ex=p.get('short_expiry');dte=(dt.date.fromisoformat(ex)-dt.date.fromisoformat(p['entry_day'])).days if ex else math.nan
        out[t]=np.array([rel[t],pct,age[t],m9,m45,prem,dte,float(bars.end[t]),float(dt.date.fromisoformat(bars.day[t]).weekday()),0.])
    return out

# ------------------------------------------------------------------ simulation
def simulate(sd,basket,start='2000-01-01',end=None,exclude=None):
    """Trades of one strategy with basket = list of candidate indices (one per slice). Returns trades: dict(signal, entry, exit
    (last slice), legs [(exit day ISO, rupees)], pnl, features)."""
    end=end or sd.end;K=len(basket);busy=-1;out=[];last=None;closed=[]
    for t in sd.signals:
        if t<=busy:continue
        i=sd.index.get(t)
        if i is None:
            if t in sd.gaps:busy=sd.gaps[t]
            continue
        busy=int(max(sd.XB[r,i] for r in basket));e=sd.entry[i]
        if not start<=e<end or (exclude and e[:4]==exclude):continue             # excluded year: the slot is still held
        vals=[float(sd.P[r,i]) for r in basket]
        if not all(map(math.isfinite,vals)):continue                                 # unvalued (as in the scans): slot held, not counted
        legs=[(dt.date.fromordinal(int(sd.XD[r,i])).isoformat(),v*sd.units[i]/K) for r,v in zip(basket,vals)]
        f=sd.feat.get(t)
        if f is not None:
            f=f.copy();prior=[c for c in out if max(d for d,_ in c['legs'])<e]           # last trade fully closed before this entry
            f[-1]=0. if not prior else (1. if prior[-1]['pnl']>0 else -1.)
        out.append(dict(strategy=sd.name,side=sd.side,signal=t,entry=e,exit=max(d for d,_ in legs),legs=legs,pnl=sum(v for _,v in legs),features=f,mult=1.))
    return out

def period_returns(trades,kind,capital,lo='0000',hi='9999'):
    """{period: return on capital} from each leg's exit day x the trade's size multiplier, for exit days in [lo, hi)."""
    g=collections.defaultdict(float)
    for t in trades:
        for d,v in t['legs']:
            if lo<=d<hi:g[period_of(d,kind)]+=v*t['mult']/capital
    return dict(sorted(g.items()))

def all_periods(lo,hi,kind):
    """Every period label from day lo to day hi (ISO), so periods without trades count as 0."""
    out=[];d=dt.date.fromisoformat(lo);e=dt.date.fromisoformat(hi)
    while d<e:                                                                   # month by month: no partial period is skipped
        p=period_of(d.isoformat(),kind)
        if p not in out:out.append(p)
        d=(d.replace(day=1)+dt.timedelta(days=32)).replace(day=1)
    return out

def stats(trades,kind,capital,lo,hi,exclude=None):
    """Account statistics of trades exiting in [lo, hi)."""
    per=period_returns(trades,kind,capital,lo,hi);labels=[p for p in all_periods(lo,hi,kind) if not (exclude and p[:4]==exclude)];v=[per.get(p,0.) for p in labels]
    yrs=max((dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days,1)/365.25
    if exclude:yrs-=sum(1 for p in all_periods(lo,hi,kind) if p[:4]==exclude)/(2 if kind=='half' else 4)
    yrs=max(yrs,1/365.25);per_year=len(labels)/yrs
    eq=pk=dd=0.
    for t in sorted(trades,key=lambda t:t['exit']):
        if lo<=t['exit']<hi:eq+=t['pnl']*t['mult'];pk=max(pk,eq);dd=max(dd,pk-eq)
    sd=float(stt.pstdev(v)) if len(v)>1 else 0.;mean=float(stt.mean(v)) if v else 0.
    n=[t for t in trades if lo<=t['exit']<hi]
    v=[float(x) for x in v]
    return dict(periods=len(v),annual=float(sum(v)/yrs),sd=sd,worst=float(min(v)) if v else 0.,negative=int(sum(x<0 for x in v)),sharpe=mean/sd*math.sqrt(per_year) if sd else None,
                max_dd=dd/capital,trades=len(n),win=(sum(t['pnl']>0 for t in n)/len(n)) if n else None,by_period=dict(zip(labels,v)))

# ------------------------------------------------------------------ search
def objective(st_,min_annual):
    """Higher is better: worst period, penalised by any shortfall of the yearly return; tie-break on lower spread."""
    return st_['worst']-2.*max(0.,min_annual-st_['annual'])-1e-3*st_['sd']

def search(data,K,kind,capital,lo,hi,min_annual,rounds=3,log=None,exclude=None):
    """Basket per strategy (list of K candidate indices), maximising objective() of the combined account on [lo, hi)."""
    bk={s.name:[s.ref]*K for s in data};cache={}
    def trades_of(s,b):
        k=(s.name,tuple(sorted(b)))
        if k not in cache:cache[k]=simulate(s,list(b),lo,hi,exclude)
        return cache[k]
    def score(bks):
        tr=[t for s in data for t in trades_of(s,bks[s.name])];return objective(stats(tr,kind,capital,lo,hi,exclude),min_annual)
    best=score(bk)
    for rnd in range(rounds):
        improved=False
        for s in data:
            R=len(s.pairs)
            if rnd==0:                                                              # greedy forward fill of the K slices
                cur=[]
                for k in range(K):
                    cands=[]
                    for r in range(R):
                        trial=cur+[r];full=trial+[trial[-1]]*(K-len(trial))         # unfilled slices copy the last pick
                        cands.append((score(dict(bk,**{s.name:full})),r))
                    cur.append(max(cands)[1])
                v=score(dict(bk,**{s.name:cur}))
                if v>best:bk[s.name]=cur;best=v;improved=True
            while True:                                                             # single-slice swaps
                top=(best,None)
                for k in range(K):
                    for r in range(R):
                        if r==bk[s.name][k]:continue
                        trial=list(bk[s.name]);trial[k]=r;v=score(dict(bk,**{s.name:trial}))
                        if v>top[0]+1e-12:top=(v,trial)
                if top[1] is None:break
                best,bk[s.name]=top[0],top[1];improved=True
            if log:log(f'  round {rnd+1}, {s.name}: {[s.pairs[r] for r in bk[s.name]]} objective {best:.4f}')
        if not improved:break
    return bk,best

def leave_one_year_out(data,K,kind,capital,lo,hi,min_annual,rounds=2,log=None):
    """For each calendar year in [lo, hi): choose baskets on the other years, then score the held-out year. Returns rows per year
    (held-out periods of the chosen structure and of the reference): an estimate of how much the search's gain is fitted noise."""
    rows=[];ref={s.name:[s.ref]*K for s in data}
    for y in sorted({p[:4] for p in all_periods(lo,hi,kind)}):
        bk,_=search(data,K,kind,capital,lo,hi,min_annual,rounds,exclude=y)
        ylo=max(lo,f'{y}-01-01');yhi=min(hi,f'{int(y)+1}-01-01')
        def held(b):
            tr=[t for s in data for t in simulate(s,b[s.name],lo,hi) if t['entry'][:4]==y]
            per=period_returns(tr,kind,capital,ylo,f'{int(y)+1}-03-01');return [per.get(p,0.) for p in all_periods(ylo,yhi,kind)]
        r=dict(year=y,chosen=[float(x) for x in held(bk)],reference=[float(x) for x in held(ref)],baskets={s_.name:[s_.pairs[i] for i in bk[s_.name]] for s_ in data});rows.append(r)
        if log:log(f'  held-out {y}: chosen {[round(100*x,1) for x in r["chosen"]]}  reference {[round(100*x,1) for x in r["reference"]]}')
    return rows

# ------------------------------------------------------------------ sizing
def logistic_fit(X,y,lam=1.,iters=500,lr=.1):
    w=np.zeros(X.shape[1]);b0=0.
    for _ in range(iters):
        z=X@w+b0;p=1/(1+np.exp(-np.clip(z,-30,30)));g=p-y
        w-=lr*(X.T@g/len(y)+lam*w/len(y));b0-=lr*g.mean()
    return w,b0

def size_meta(trades,size_map,min_train=30,lam=1.):
    """Walk-forward meta-labelling by calendar year; sets t['mult'] (size / full) and t['score_tier'] (low / medium / high / none)."""
    full=max(size_map);steps=[x/full for x in size_map];tiers=('low','medium','high')
    usable=[t for t in trades if t['features'] is not None]
    for t in trades:t['mult']=steps[1];t['score_tier']='none'
    report=[]
    for y in sorted({t['entry'][:4] for t in usable}):
        test=[t for t in usable if t['entry'][:4]==y];train=[t for t in usable if t['exit']<f'{y}-01-01']
        if len(train)<min_train:report.append(dict(year=y,train=len(train),test=len(test),fitted=False));continue
        X=np.array([t['features'] for t in train],float);mu=np.nanmean(X,0);sd=np.nanstd(X,0);sd[sd==0]=1.
        Z=np.nan_to_num((X-mu)/sd);yv=np.array([t['pnl']>0 for t in train],float);w,b0=logistic_fit(Z,yv,lam)
        s_tr=Z@w+b0;q1,q2=np.quantile(s_tr,[1/3,2/3])
        for t in test:
            z=np.nan_to_num((np.asarray(t['features'],float)-mu)/sd)@w+b0;k=0 if z<=q1 else 1 if z<=q2 else 2
            t['mult']=steps[k];t['score_tier']=tiers[k];t['score']=float(z)
        report.append(dict(year=y,train=len(train),test=len(test),fitted=True,weights={f:round(float(v),3) for f,v in zip(FEATURES,w)}))
    return report

def size_vol(trades,size_map,data):
    full=max(size_map);steps=np.array([x/full for x in size_map]);feat_i=FEATURES.index('atr_rel')
    med={}
    for s in data:
        rel=s.b.bars.atr[15]/s.b.bars.close if s.b is not None else None;med[s.name]=rel
    for t in trades:
        t['score_tier']='vol'
        if t['features'] is None:t['mult']=steps[1];continue
        rel=med[t['strategy']];lo=max(0,t['signal']-2000);w=rel[lo:t['signal']+1] if rel is not None else np.zeros(0);w=w[np.isfinite(w)]
        now=t['features'][feat_i]
        if not len(w) or not (math.isfinite(now) and now>0):t['mult']=steps[1];continue        # no volatility history yet: medium
        x=float(np.median(w))/now;t['mult']=float(steps[np.argmin(abs(steps-np.clip(x,steps[0],steps[-1])))])

def tier_report(trades,lo,hi):
    out={}
    for tier in ('low','medium','high'):
        g=[t for t in trades if t.get('score_tier')==tier and lo<=t['entry']<hi]
        if g:out[tier]=dict(trades=len(g),mean_pnl=round(stt.mean(t['pnl'] for t in g)),win=round(sum(t['pnl']>0 for t in g)/len(g),3))
    return out

# ------------------------------------------------------------------ main
def parse_strategy(s):
    p=[x.strip() for x in s.split(',')]
    if len(p)!=6:raise SystemExit(f'--strategy needs side,structure,level,config,profit,stop: {s}')
    return (p[0],p[1],float(p[2]),p[3],p[4],p[5])

def fmt_row(name,s_in,s_out):
    pc=lambda v:'-' if v is None else f'{100*v:6.1f}%'
    nm=lambda v:'-' if v is None else f'{v:5.2f}'
    return (f"{name:<34}{pc(s_in['annual'])}{pc(s_in['sd'])}{pc(s_in['worst'])}{s_in['negative']:>4}/{s_in['periods']:<3}{nm(s_in['sharpe']):>6}{pc(s_in['max_dd'])}"
            f"  |{pc(s_out['annual'])}{pc(s_out['sd'])}{pc(s_out['worst'])}{s_out['negative']:>4}/{s_out['periods']:<3}{nm(s_out['sharpe']):>6}{pc(s_out['max_dd'])}")

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays');ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--strategy',action='append',required=True,help='side,structure,level,config,profit,stop (repeat): its entries, and its own exit as the reference')
    ap.add_argument('--baskets',type=int,default=5,help='equal slices of each position, each with its own exit (1 = a single exit, searched)')
    ap.add_argument('--period',choices=('half','quarter'),default='half',help='period the objective and the stability statistics use')
    ap.add_argument('--min-annual',type=float,default=.35,help='average yearly return required in the search, share of --capital at full size, pre-tax, before interest')
    ap.add_argument('--capital',type=float,default=2.5e6);ap.add_argument('--scale',type=float,help='rupee multiplier (default 1 / the paths file\'s --target-fraction: full size)')
    ap.add_argument('--train-start',default='2021-09-01',help='first entry day of the search period (the grid\'s training start)')
    ap.add_argument('--split',default='2025-01-01',help='entries before this day are searched on; from it on: out of sample')
    ap.add_argument('--end',default='2026-07-01',help='entries before this day; trades still open are valued to it')
    ap.add_argument('--profits',help='comma-separated candidate profit rules (default: a fixed list across families)')
    ap.add_argument('--stops',help='comma-separated candidate stop rules (default: a fixed list across families)')
    ap.add_argument('--rounds',type=int,default=3,help='coordinate rounds over the strategies')
    ap.add_argument('--sizing',choices=('none','meta','vol','all'),default='all',help='entry sizing to evaluate on the chosen structure: meta-labelling, volatility management, or all')
    ap.add_argument('--size-map',default='4,8,12',help='lots for low, medium, high (the largest = full size)')
    ap.add_argument('--min-train',type=int,default=30,help='closed trades needed before the meta model sizes a year')
    ap.add_argument('--loyo',action=argparse.BooleanOptionalAction,default=True,help='leave-one-year-out check of the search (choose on the other years, score the held-out one)')
    ap.add_argument('--out',default='trade_structure')
    a=ap.parse_args(argv);hol=zr.read_holidays(a.holidays) if a.holidays else []
    size_map=[float(x) for x in a.size_map.split(',')]
    if len(size_map)!=3 or not 0<size_map[0]<=size_map[1]<=size_map[2]:sys.exit('--size-map needs three increasing positive numbers')
    profits=a.profits.split(',') if a.profits else CANDIDATE_PROFITS;stops=a.stops.split(',') if a.stops else CANDIDATE_STOPS
    pairs=[(p,s) for p in profits for s in stops]
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta_args=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta_args={}
    db.close();scale=a.scale or 1/float(meta_args.get('target_fraction') or 1.)
    data=[]
    with zg.Detail(a.paths,hol,a.gap_policy) as D:
        for s in a.strategy:
            sd=StrategyData(D,parse_strategy(s),pairs,a.end,scale);data.append(sd)
            print(f'{sd.name}: {len(sd.signals)} signals, {sd.P.shape[1]} filled paths, {len(sd.pairs)} candidate exits',flush=True)
    K=a.baskets;lo,mid,hi=a.train_start,a.split,a.end;kind=a.period
    print(f'search: {K} slices, objective = worst {kind} on entries {lo}..{mid}, average >= {a.min_annual:.0%} a year of Rs {a.capital:,.0f} (scale {scale:g})',flush=True)
    ref={s.name:[s.ref]*K for s in data}
    best,obj=search(data,K,kind,a.capital,lo,mid,a.min_annual,a.rounds,log=lambda m:print(m,flush=True))
    loyo=None
    if a.loyo:
        print('\nleave-one-year-out (choose on the other in-sample years, score the held-out year), % of capital per period:',flush=True)
        loyo=leave_one_year_out(data,K,kind,a.capital,lo,mid,a.min_annual,max(1,a.rounds-1),log=lambda m:print(m,flush=True))
        ch=[x for r in loyo for x in r['chosen']];rf=[x for r in loyo for x in r['reference']]
        if ch:print(f"  held-out periods: chosen negative {sum(x<0 for x in ch)}/{len(ch)}, sd {100*stt.pstdev(ch):.1f}%, worst {100*min(ch):.1f}% | "
                    f"reference negative {sum(x<0 for x in rf)}/{len(rf)}, sd {100*stt.pstdev(rf):.1f}%, worst {100*min(rf):.1f}%")
    def run(bks):return [t for s in data for t in simulate(s,bks[s.name],lo,hi)]
    rows=[];hdr=(f"{'':<34}{'in-sample: yr ret':>9}{'sd':>7}{'worst':>7}{'neg':>8}{'Shrp':>6}{'DD':>7}  |{'out of sample: yr ret':>9}{'sd':>7}{'worst':>7}{'neg':>8}{'Shrp':>6}{'DD':>7}")
    configs=[('reference (own exits)',run(ref)),(f'exit baskets ({K} slices)',run(best))]
    T=configs[1][1]
    if a.sizing in ('meta','all'):
        Tm=[dict(t) for t in T];rep=size_meta(Tm,size_map,a.min_train);avg=stt.mean(t['mult'] for t in Tm)
        configs.append(('baskets + meta sizing',Tm));Tc=[dict(t,mult=avg) for t in T];configs.append((f'baskets, constant {avg:.2f}x (same exposure)',Tc))
    if a.sizing in ('vol','all'):
        Tv=[dict(t) for t in T];size_vol(Tv,size_map,data);avg_v=stt.mean(t['mult'] for t in Tv)
        configs.append(('baskets + volatility sizing',Tv));configs.append((f'baskets, constant {avg_v:.2f}x (same exposure)',[dict(t,mult=avg_v) for t in T]))
    print(f'\nchosen exits per strategy (slices of 1/{K}):')
    for s in data:
        c=collections.Counter(best[s.name]);print(f'  {s.name}: '+', '.join(f'{n} x {s.pairs[r][0]}/{s.pairs[r][1]}' for r,n in c.most_common()))
    print(f'\nreturns as a share of Rs {a.capital:,.0f}; periods = {kind}; in-sample = entries {lo}..{mid}, out of sample = {mid}..{hi}')
    print(hdr)
    out_rows=[]
    for name,tr in configs:
        si=stats(tr,kind,a.capital,lo,mid);so=stats(tr,kind,a.capital,mid,hi);print(fmt_row(name,si,so))
        out_rows.append(dict(config=name,**{f'in_{k}':v for k,v in si.items() if k!='by_period'},**{f'oos_{k}':v for k,v in so.items() if k!='by_period'}))
    per_rows=[];labels=all_periods(lo,hi,kind)
    print(f'\n{kind} returns (% of capital):\n{"":<34}'+''.join(f'{p:>8}' for p in labels))
    for name,tr in configs:
        pr=period_returns(tr,kind,a.capital,lo,hi);print(f'{name:<34}'+''.join(f'{100*pr.get(p,0.):8.1f}' for p in labels));per_rows.append(dict(config=name,**{p:round(pr.get(p,0.),5) for p in labels}))
    # strategies separately and their correlation
    if len(data)>1:
        series=[[period_returns([t for t in T if t['strategy']==s.name],kind,a.capital,lo,hi).get(p,0.) for p in labels] for s in data]
        C=np.corrcoef(np.array(series)) if all(np.std(x)>0 for x in series) else None
        print('\ncorrelation of the strategies\' period returns (chosen exits):')
        for s,row in zip(data,C if C is not None else []):print(f'  {s.name[:40]:<42}'+' '.join(f'{v:5.2f}' for v in row))
    if a.sizing in ('meta','all'):
        print('\nmeta-labelling, out of sample (walk-forward) per tier:',json.dumps(tier_report(Tm,'0000','9999')))
        for r in rep:print('  ',r)
    keys=list(dict.fromkeys(k for r in out_rows for k in r))
    with open(f'{a.out}_summary.csv','w',newline='') as f:w=csv.DictWriter(f,keys);w.writeheader();w.writerows(out_rows)
    with open(f'{a.out}_periods.csv','w',newline='') as f:w=csv.DictWriter(f,list(per_rows[0]));w.writeheader();w.writerows(per_rows)
    tk=['config','strategy','signal','entry','exit','pnl','mult','score_tier']
    with open(f'{a.out}_trades.csv','w',newline='') as f:
        w=csv.writer(f);w.writerow(tk+list(FEATURES))
        for name,tr in configs:
            for t in tr:w.writerow([name]+[t.get(k) if k!='config' else name for k in tk[1:]]+(list(np.round(t['features'],5)) if t['features'] is not None else []))
    json.dump(dict(baskets={s.name:[s.pairs[r] for r in best[s.name]] for s in data},loyo=loyo),open(f'{a.out}_baskets.json','w'),indent=1,default=float)
    print(f'\n-> {a.out}_summary.csv, {a.out}_periods.csv, {a.out}_trades.csv, {a.out}_baskets.json')

if __name__=='__main__':main()
