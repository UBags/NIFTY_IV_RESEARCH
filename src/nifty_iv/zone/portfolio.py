#!/usr/bin/env python3
"""Green + red portfolio selection on rupee returns, yearly floors, combined drawdown and available funds (V4.9.2).

A portfolio is one green (bull) strategy and one red (bear) strategy, each at its own size of whole lots (0..the paths file's
--lots; 0 = that leg not traded), run on one account. Each strategy is a grid combination (structure, strike offset, signal
configuration, profit rule, stop rule). The account is judged as a whole:

  size              a leg of k lots trades min(k, lots the historical path matched) whole lots on each trade: the paths were
                    simulated at --lots, and a trade whose market matched only m lots there fills min(k, m) at k (per-unit P&L
                    from that path; exits of smaller orders are, if anything, cheaper). --sizing constant (default): a lot is
                    --ref-units / --lots units (65) in every year, so a point is worth the same whatever the exchange lot size
                    was (50 / 25 / 75 / 65); --sizing actual: the exchange lot of the day.
  margin            per open position and session: rate x |units| x index close (--margin, defaults futures 12%, naked short
                    12%, calendar 4% of notional; ASSUMED, not SPAN). Called assumed_margin everywhere.
  available funds   capital + the account's mark-to-market P&L at the previous session close. Margin must stay within
                    (1 - --margin-buffer) x available funds: a training gate for each combination, and an order-admission rule
                    in the walk-forward account (an order is cut to the lots that fit, or rejected; rejections are logged).
  return per year   that calendar year's rupee P&L (mark-to-market at the year's last close) / capital, annualised for a part
                    year of at least --min-year-days; shorter part years are neither annualised nor held to the floor. Simple.
  drawdown          daily_close_drawdown: largest fall of the combined equity at session closes from its running peak (stale
                    marks carried forward, share reported). Not an intraday guarantee.
  gates             overall >= --target, every judged year >= --floor (no judged year: fails), drawdown <= --max-dd, assumed
                    margin within available funds at every session. Ranking among passing (pair, sizes): --rank dd (default),
                    worst_year or calmar; ties by higher overall return. Every survivor of the target and floor screens is
                    evaluated exactly (no truncation).

V4.9.7: --capital-basis allocated (default): each traded leg must meet --allocation (5/12) of the account's target, floor (default
0.70 x target) and drawdown limit; a green + red pair twice that (10/12). A nominal share: a simplification to tune to actual margin use.
A leg trades its full entry size (the paths' ceil(--target-fraction x margin-table lots)) or not at all; every full training year
must be positive with the worst >= --band-floor x the mean full year; a walk-forward test year needs --min-train-years full years of
training before it (none with data from Sept 2021 and the holdout from 2025: the holdout is the forward test); grid groups below
--min-fill-rate are not candidates.

Walk-forward is a single chronological EXECUTABLE account. For test year Y the pair and sizes are chosen on [--start, Y) only.
The choice is adopted flat on 1 January (signals before then ignored), takes entries during Y only, and positions still open
at the year end run to their own exits (overlapping the next year's choice). Every entry, of every year's legs, goes through the
order-admission rule against the account's own funds and margin at that session. Every planned test year is reported: a year
with no passing pair (or no candidates) takes no new entries (--if-none cash; 'closest' trades the nearest failing pair,
research only, never deployable) while positions carried from the year before continue; a year counts as flat cash only if
the account held nothing in it. A year is deployable only if its choice passed every training gate and its funds were never
short. Training curves follow the grid's convention (one continuous strategy from --start), so pools agree with the grid.

Candidates: for each side and training window the union of the top --pool combinations by training total, worst training year
and the grid's stability score, each with >= --min-trades trades (walk-forward windows: --wf-min-trades, default
max(10, min-trades // 2), as the grid). Rule labels with identical trades are one candidate. Data gaps follow the grid's
--gap-policy (read from the grid index).

Robustness and uncertainty: share of one-step neighbour swaps that still pass (identical neighbours reported separately); the
test-year results of the top --wf-top distinct passing pairs per year; the account at several futures carry rates.

Futures are spot-proxy: an assumed carry (--carry, 5.5%/yr) is charged pro rata on futures legs (long pays, short receives).

Outputs (in --out): portfolio_walk_forward.csv, portfolio_account_daily.csv, portfolio_orders.csv, portfolio_pairs_in_sample.csv,
portfolio_gate_counts.csv, portfolio_report.json."""
import argparse,collections,csv,datetime as dt,json,math,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.core import strategy_spec as ss
from nifty_iv.core import cli

CAPITAL=2_500_000.
MARGIN={'futures_zone':.12,'futures_expiry':.12,'naked':.12,'naked_m1':.12,'naked_m2':.12,'calendar':.04}   # --margin-model rates (assumed shares of notional)
HOLIDAYS=frozenset()            # exchange holidays of the current run (set by run / the holdout): session completeness at period ends

class MarginModel:
    """Margin per unit of a position. table (default, V4.9.6): strategy_spec margin table, upper end of the range, per unit of the
    period's lot, on the day (x --calendar-margin-factor for calendars, default 1: no spread benefit assumed). rates: share of
    notional per structure (MARGIN / --margin) x index close."""
    def __init__(self,model='table',rates=None,calendar_factor=1.,defaults=True):
        self.model=model;self.rates=dict(MARGIN if defaults else {},**(rates or {}));self.cal=calendar_factor;self._d={}
    def factor(self,structure):return self.cal if structure=='calendar' else 1.
    def per_unit(self,structure,days,closes):
        """Array of rupees per unit for each (day, index close)."""
        if self.model=='table':
            key=(days[0] if len(days) else None,days[-1] if len(days) else None,len(days))
            if key not in self._d:self._d[key]=np.array([ss.margin_per_unit(d) for d in days])
            return self._d[key]*self.factor(structure)
        return self.rates.get(structure,0.)*np.asarray(closes,float)
    def active(self,structure):return self.model=='table' or self.rates.get(structure,0.)>0

# ---------------- candidate pools from the grid files ----------------
_P={}
def _year_factor(start,split):
    """Calendar years covered inside [start, split) and the annualisation factor of each (year_factor)."""
    a=dt.date.fromisoformat(start);b=dt.date.fromisoformat(split);out={}
    for y in range(a.year,b.year+1):
        lo=max(a,dt.date(y,1,1));hi=min(b,dt.date(y+1,1,1));d=(hi-lo).days
        if d>0:out[y]=year_factor(y,d)
    return out

def _pool_file(e):
    """Top candidates of one grid file for every (training window, criterion)."""
    c=_P;K=c['pool'];out=[]
    with np.load(c['grid']/e['file']) as z:Z={k:z[k] for k in ('n','total','configs','profits','stops','windows')}
    configs=list(Z['configs']);P=list(Z['profits']);S=list(Z['stops']);wn=list(Z['windows'])
    CN=zr.config_neighbours(configs);PN=zr.neighbours(P);SN=zr.compound_neighbours(S) if e['kind'].startswith('compound') else zr.neighbours(S)
    for fold,(wname,years,mt) in c['folds'].items():
        if wname not in wn:continue
        wi=wn.index(wname);N=Z['n'][:,wi].astype(np.int64);T=Z['total'][:,wi]
        ok=N>=mt
        if not ok.any():continue
        yw=lambda y:f'test_{y}' if f'test_{y}' in wn else f'train_{y+1}'          # the first (part) year has no test window: train_<next year> is that year alone
        yr=[Z['total'][:,wn.index(yw(y))]*c['factor'][y] for y in years if yw(y) in wn]
        worst=np.min(np.stack(yr),0) if yr else T
        stab,_=zg.stability(N,T,mt,CN,PN,SN)
        for crit,score in (('total',T),('worst_year',worst),('stable',stab)):
            sc=np.where(ok&np.isfinite(score),score,-np.inf).ravel();k=min(K,int(np.isfinite(sc).sum()))
            if not k:continue
            for f in np.argpartition(-sc,k-1)[:k]:
                ci,pi,si=np.unravel_index(int(f),N.shape)
                out.append((fold,crit,float(sc[f]),(e['side'],e['structure'],float(e['offset']),str(configs[ci]),str(P[pi]),str(S[si])),e['file']))
    return out

def pools(grid,folds,factor,pool,workers=1,min_fill_rate=0.):
    """{(fold, side): {candidate key: file}} with the top `pool` per criterion across all files. folds: {fold: (window, years, min trades)}."""
    idx=zg.read_json(grid/'grid_index.json');_P.clear();_P.update(grid=grid,folds=folds,factor=factor,pool=pool)
    best=collections.defaultdict(list)
    for part in zg.pool_map(_pool_file,[e for e in idx['groups'] if zg.testable(e,min_fill_rate)],workers):
        for fold,crit,score,key,f in part:best[fold,key[0],crit].append((score,key,f))
    out=collections.defaultdict(dict)
    for (fold,side,crit),lst in best.items():
        lst.sort(key=lambda x:(-x[0],x[1]))
        for _,key,f in lst[:pool]:out[fold,side][key]=f
    return out,idx

# ---------------- trades -> per-lot pieces -> daily equity, margin and open lots at k lots ----------------
class Sizing:
    """Units a trade holds at a leg size of k (of K reference lots), and how many reference lots that is.
    exchange (default, executable): whole exchange lots of the trade's own contract. The path was simulated at a target of
    tl exchange lots (paths built with --target-units: round(target / lot), else --lots) and matched m <= tl of them; a leg of k
    holds n = min(m, floor(k x tl / K)) lots = n x lot units. Never more than the simulated, matched quantity.
    normalized (research proxy, never deployable): ref_units x min(k / K, m / tl) units, the same notional in every year.
    A reference lot is ref_units / K units (65): the unit of --max-open-lots."""
    def __init__(self,sizing='exchange',ref_units=650.,K=10,target_units=None,cap_unit='reference',target_margin=None,target_fraction=1.):
        self.sizing=sizing;self.ref=float(ref_units);self.K=int(K or 10);self.tu=target_units;self.tm=target_margin;self.tf=float(target_fraction or 1.);self.stats=collections.Counter();self.ref_lot=self.ref/self.K;self.cap_unit=cap_unit
    def target(self,x):
        """Exchange lots the path was simulated at: --target-margin (ceil(--target-fraction x margin-table lots) on the entry day, V4.9.7),
        --target-units, or --lots."""
        lot=x.get('lot')
        if not lot:return self.K
        if self.tm and x.get('entry_day'):return ss.entry_lots(x['entry_day'],lot,self.tm,self.tf)
        return max(1,int(round(self.tu/lot))) if self.tu else self.K
    def matched(self,x):
        lot=x.get('lot')
        if not lot:self.stats['lot_unknown']+=1;return self.K
        return max(0,min(self.target(x),int(round(float(x['units'])/lot))))
    def lots(self,x,k):
        """Exchange lots held at leg size k."""
        return min(self.matched(x),int(math.floor(k*self.target(x)/self.K+1e-9)))
    def units(self,x,k):
        if self.sizing=='normalized':return self.ref*min(k/self.K,self.matched(x)/self.target(x))
        return float(self.lots(x,k)*(x.get('lot') or self.ref_lot))
    def unit_per_lot(self,x):return float(x.get('lot') or self.ref_lot) if self.sizing=='exchange' else self.ref_lot
    def cap_lots(self,x,u):
        """Lots counted against --max-open-lots for u units of trade x. reference: the share of the path's full target size x K
        (a full-size position is K whatever the rounding: 9 lots of 75 for 650 units counts 10); exchange: actual exchange lots."""
        lot=x.get('lot')
        if self.cap_unit=='exchange':return u/lot if lot else u/self.ref_lot
        if self.sizing=='exchange' and lot:return self.K*(u/lot)/self.target(x)
        return u/self.ref_lot

def prepare(trades,sess_ord,structure='naked',carry=0.,sess_close=None,margin_rate=0.,margin_unit=None):
    """Each valued trade as per-unit pieces on the session axis: (i0, hi, mark segment per unit, realised session or None,
    final per unit, margin per unit per session, trade, held sessions). A position is held from its entry session to its exit
    session inclusive (a same-day trade holds margin and lots on that day). Trades without a value are counted, not used."""
    T=len(sess_ord);out=[];bad=0;stale=0;nm=0;fut=structure.startswith('futures');busy_to=-1
    for x in trades:
        if not math.isfinite(x['unit']):bad+=1;continue
        e0=dt.date.fromisoformat(x['entry_day']).toordinal();e1=dt.date.fromisoformat(x['exit_day']).toordinal()
        c=x.get('sign',1.)*x.get('index',0.)*carry/365. if fut and carry else 0.     # assumed carry, points per unit per day
        i0=int(np.searchsorted(sess_ord,e0));i1=int(np.searchsorted(sess_ord,e1));hi=min(i1,T)
        mo=np.asarray(x['mark_ord'],np.int64);mv=np.asarray(x['marks'],float);fin=np.isfinite(mv)
        si=np.searchsorted(sess_ord,mo[fin]);mv=mv[fin]-c*(mo[fin]-e0)
        stale+=int(x.get('stale',0));nm+=int(x.get('marks_n',len(mo)))
        n=max(hi-i0,0);seg=np.zeros(0)
        if n:
            seg=np.full(n,np.nan);last=np.r_[si[1:]!=si[:-1],True] if len(si) else np.zeros(0,bool)   # marks in time order: the session's last close
            k=si[last];v=mv[last];inb=(k>=i0)&(k<hi);seg[k[inb]-i0]=v[inb]
            ok=np.isfinite(seg);fill=np.where(ok,np.arange(n),-1);np.maximum.accumulate(fill,out=fill)
            seg=np.where(fill>=0,seg[np.maximum(fill,0)],0.)
        if i0<busy_to and n:raise ValueError('overlapping trades inside one strategy')
        busy_to=max(busy_to,hi)
        if i1>=T and x.get('hypothetical') is False:raise ValueError(f"trade entered {x['entry_day']} exits {x['exit_day']}, after the window, without a window-end value (pass trades valued at the horizon)")
        final=x['unit']-c*(e1-e0);held=(i0,min(i1,T-1)+1) if i0<T else (i0,i0)            # sessions holding margin / lots
        if margin_unit is not None:mrg=np.asarray(margin_unit)[held[0]:held[1]]               # rupees per unit per session (margin table or rate x close)
        else:mrg=margin_rate*sess_close[held[0]:held[1]] if margin_rate and sess_close is not None else None
        out.append((i0,hi,seg,i1 if i1<T else None,final,mrg,x,held))
    return dict(pieces=out,bad=bad,stale=stale,marks=nm,n=len(trades),T=T)

def combine(prep,k,sz,pieces=None):
    """Daily rupee equity, open mask (overnight holding), assumed margin and open reference lots of the prepared trades at leg
    size k. Within one strategy trades are sequential, so a session's margin and lots are the largest of the trades it held."""
    T=prep['T'];O=np.zeros(T);open_=np.zeros(T,bool);realised=np.zeros(T);M=np.zeros(T);Lo=np.zeros(T)
    for i0,hi,seg,ir,final,mrg,x,held in (prep['pieces'] if pieces is None else pieces):
        u=sz.units(x,k)
        if not u:continue
        if len(seg):O[i0:hi]=seg*u;open_[i0:hi]=True
        a,b=held
        if mrg is not None and b>a:np.maximum(M[a:b],mrg*u,out=M[a:b])
        if b>a:np.maximum(Lo[a:b],sz.cap_lots(x,u),out=Lo[a:b])
        if ir is not None:realised[ir]+=final*u
        elif T:O[T-1]=final*u;open_[T-1]=True                                   # open at the window end: its window-end value
    return np.cumsum(realised)+O,open_,M,Lo

def replay(trades,sess_ord,structure='naked',carry=0.,size=None,sess_close=None,margin_rate=0.):
    """V4.9.1 interface (one size function for all trades): dict(E, open, M, bad, stale, marks, n)."""
    p=prepare(trades,sess_ord,structure,carry,sess_close,margin_rate)
    class _S:
        ref_lot=1.
        def units(s,x,k):return size(x) if size else x['units']
        def cap_lots(s,x,u):return u
    E,op,M,_=combine(p,1,_S());return dict(E=E,open=op,M=M,bad=p['bad'],stale=p['stale'],marks=p['marks'],n=p['n'])

def equity(trades,sess_ord,structure='naked',carry=0.):
    """V4.9.0 interface: (equity, open mask, trades without a value) at the units each trade held."""
    r=replay(trades,sess_ord,structure,carry);return r['E'],r['open'],r['bad']

def year_factor(y,days):
    """Annualisation of a calendar year covered for `days` days: a full year (365 or 366 days) counts 1; a part year 365 / days."""
    full=366 if (y%4==0 and (y%100!=0 or y%400==0)) else 365
    return 1. if days>=full else 365./max(days,1)

HORIZON_SLACK=10
def horizon_end(sess_days,hi,holidays=None):
    """End (exclusive) of a period meant to run to hi: hi itself when every weekday after the last session and before hi is an
    exchange holiday (HOLIDAYS of the run; without a list, no weekday may be missing), so a full year ending on Friday 29 December
    is a full year; otherwise the day after the last session (the data stops early, and the period is that much shorter)."""
    h=dt.date.fromisoformat(hi)
    if not sess_days:return h
    hol=HOLIDAYS if holidays is None else frozenset(holidays);last=dt.date.fromisoformat(sess_days[-1])
    if last>=h:return h
    if (h-last).days>HORIZON_SLACK:return last+dt.timedelta(1)
    miss=[last+dt.timedelta(i) for i in range(1,(h-last).days) if (last+dt.timedelta(i)).weekday()<5 and (last+dt.timedelta(i)).isoformat() not in hol]
    return last+dt.timedelta(1) if miss else h

def is_full_year(y,days):return days>=(366 if (y%4==0 and (y%100!=0 or y%400==0)) else 365)

def year_slices(sess_days,lo,hi,factor=None):
    """[(year, i_start, i_end, annualisation, calendar days covered)]: sessions of each calendar year inside [lo, hi). A part
    year (the window starts or ends inside it, or the data does) is annualised over the calendar days it covers: 365 / days."""
    out=[];inside=[i for i,d in enumerate(sess_days) if lo<=d<hi]
    if not inside:return out
    end=horizon_end(sess_days,hi);lo_d=dt.date.fromisoformat(lo)
    for y in sorted({sess_days[i][:4] for i in inside}):
        idx=[i for i in inside if sess_days[i][:4]==y];y=int(y)
        days=(min(end,dt.date(y+1,1,1))-max(lo_d,dt.date(y,1,1))).days
        out.append((y,idx[0],idx[-1],year_factor(y,days),days))
    return out

def pair_metrics(G,R,i0,i1,ys,capital,span_years,chunk=64,min_days=90):
    """All green x red pairs at full size over sessions [i0, i1]: yearly returns (nG, nR, Y), overall return, worst judged
    year (NaN when no year covers min_days: not judged, V4.9.1), max drawdown."""
    base=lambda A,i:A[:,i-1] if i>i0 else np.zeros(len(A))
    fy=[f if d>=min_days else 1. for *_,f,d in ys];yG=np.stack([(G[:,b]-base(G,a))*f for (_,a,b,*_),f in zip(ys,fy)],1);yR=np.stack([(R[:,b]-base(R,a))*f for (_,a,b,*_),f in zip(ys,fy)],1)
    Y=(yG[:,None,:]+yR[None,:,:])/capital
    tot=((G[:,i1]-base(G,i0))[:,None]+(R[:,i1]-base(R,i0))[None,:])/capital/span_years
    dd=np.empty((len(G),len(R)))
    g0=base(G,i0);r0=base(R,i0)
    for a in range(0,len(G),chunk):
        S=(G[a:a+chunk,None,i0:i1+1]-g0[a:a+chunk,None,None])+(R[None,:,i0:i1+1]-r0[None,:,None])
        S=np.concatenate([np.zeros(S.shape[:2]+(1,)),S],2)
        dd[a:a+chunk]=(np.maximum.accumulate(S,2)-S).max(2)
    judged=[j for j,y in enumerate(ys) if y[4]>=min_days]
    worst=Y[:,:,judged].min(2) if judged else np.full(tot.shape,np.nan)
    return Y,tot,worst,dd

def _dd_rows(S):
    """Max drawdown of each row of S (rows x sessions), equity starting at 0 before the first session."""
    S=np.concatenate([np.zeros((len(S),1),S.dtype),S],1);return (np.maximum.accumulate(S,1)-S).max(1)

class Fold:
    """Training-window arrays of a side's candidate curves at one leg size; row n (the last) is the empty leg (zeros)."""
    def __init__(self,curves,i0,i1,ys,min_days,T):
        E=np.vstack([c[0] for c in curves]+[np.zeros(T)]);M=np.vstack([c[2] for c in curves]+[np.zeros(T)]);Lo=np.vstack([c[3] for c in curves]+[np.zeros(T)])
        b=E[:,i0-1] if i0>0 else np.zeros(len(E));self.n=len(curves)
        self.judged=[j for j,y in enumerate(ys) if y[4]>=min_days];self.full=[j for j,y in enumerate(ys) if is_full_year(y[0],y[4])];fy=[y[3] if y[4]>=min_days else 1. for y in ys]
        self.y=np.stack([(E[:,bb]-(E[:,aa-1] if aa>i0 else b))*f for (_,aa,bb,*_),f in zip(ys,fy)],1)
        self.t=E[:,i1]-b;self.D=(E[:,i0:i1+1]-b[:,None]);self.M=M[:,i0:i1+1];self.Lo=Lo[:,i0:i1+1]
        self.open=np.vstack([c[1] for c in curves]+[np.zeros(T,bool)])[:,i0:i1+1]

def combo_eval(FG,FR,g,r,capital,span,buffer=0.,max_margin=None):
    """Exact metrics of combinations (rows g of FG, r of FR, each already at its size): overall return, yearly returns, worst
    judged year (NaN if none), daily-close drawdown, peak assumed margin, the largest margin excess over (1 - buffer) x available
    funds (capital + P&L at the previous close; <= 0: the funds always sufficed) and the peak combined open reference lots.
    Margin and lots include positions opened and closed within a session; a green and a red position held on the same day are
    added even if they did not overlap within it (conservative)."""
    g=np.asarray(g,np.int64);r=np.asarray(r,np.int64);tot=(FG.t[g]+FR.t[r])/capital/span
    Y=(FG.y[g]+FR.y[r])/capital;worst=Y[:,FG.judged].min(1) if FG.judged else np.full(len(g),np.nan)
    dd=np.empty(len(g));mg=np.empty(len(g));ex=np.empty(len(g));lo=np.empty(len(g))
    for s in range(0,len(g),10000):
        sl=slice(s,s+10000);E=FG.D[g[sl]]+FR.D[r[sl]];M=FG.M[g[sl]]+FR.M[r[sl]]
        dd[sl]=_dd_rows(E);mg[sl]=M.max(1);lo[sl]=(FG.Lo[g[sl]]+FR.Lo[r[sl]]).max(1)
        prev=np.concatenate([np.zeros((len(E),1)),E[:,:-1]],1);lim=(1-buffer)*(capital+prev)
        if max_margin:lim=np.minimum(lim,max_margin)                                       # the margin budget (default: the capital)
        ex[sl]=(M-lim).max(1)
    return tot,Y,worst,dd,mg,ex,lo

ALLOCATION=5/12   # V4.9.7 (user): nominal capital share of one strategy (5 of 12 lots in 2026); a SIMPLIFICATION, to be tuned to actual margin use
def leg_share(la,lb,allocation=None):
    """Share of the account's targets (return, floor, drawdown) a combination must meet: allocation x legs traded (0 < la, 0 < lb);
    None = account basis (1, as before V4.9.7). Works on scalars and arrays."""
    if allocation is None:return 1.
    return allocation*((np.asarray(la)>0).astype(float)+(np.asarray(lb)>0).astype(float))

def simple_target(target,span,basis='simple'):
    """The overall simple return (total P&L / capital / years) equivalent to the target on the chosen basis. cagr: fixed size,
    profits kept, so CAGR >= target  <=>  total / capital >= (1 + target)^years - 1 (exact, monotonic)."""
    return target if basis=='simple' else ((1+target)**span-1)/span

def _band_short(Yf,band):
    """Shortfall from the year band of full-year returns Yf (..., years): 10 if a year is <= 0, else how far the worst year is below
    band x the mean, as a share of that (0 inside the band)."""
    mn=Yf.min(-1);need=band*Yf.mean(-1)
    return np.where(mn<=0,10.,np.maximum(need-mn,0)/np.maximum(need,1e-9))

def search(FG,FR,leg_lots,capital,span,target,floor,max_dd,rank,top,buffer=0.,max_lots=None,closest_keep=2000,basis='simple',max_margin=None,band=0.,allocation=None):
    """Every (green, red, green lots, red lots); FG / FR: {lots: Fold}. Target and floor screened for all combinations, then every
    survivor evaluated exactly (drawdown, funds, combined open lots <= max_lots) in chunks; nothing is truncated.
    Returns (rows best first, passed, counts)."""
    cnt=collections.Counter();keep=collections.defaultdict(list);close=[];cap=max_lots if max_lots else np.inf;judged=None;target0,floor0,dd0=target,floor,max_dd
    F=('g','r','la','lb','tot','Y','worst','dd','mg','ex','lo')
    for la in leg_lots:
        for lb in leg_lots:
            if not la and not lb:continue
            f=float(leg_share(la,lb,allocation));target=simple_target(target0*f,span,basis);floor=floor0*f;max_dd=dd0*f     # V4.9.7: allocated basis
            A=FG[la] if la else FG[0];B=FR[lb] if lb else FR[0];judged=A.judged
            gi=np.arange(A.n) if la else np.array([A.n]);ri=np.arange(B.n) if lb else np.array([B.n])
            if not len(gi) or not len(ri):continue
            tot=(A.t[gi][:,None]+B.t[ri][None,:])/capital/span
            if A.judged:worst=((A.y[gi][:,None,:]+B.y[ri][None,:,:])[:,:,A.judged]/capital).min(2)
            else:worst=np.full(tot.shape,np.nan);cnt['no_judged_year']+=tot.size
            p1=tot>=target;p2=worst>=floor;ok=p1&p2
            if band and A.full:                                                   # V4.9.7: every full year positive, the worst >= band x the mean full year
                Yf=(A.y[gi][:,None,A.full]+B.y[ri][None,:,A.full])/capital;mn=Yf.min(2);pb=(mn>0)&(mn>=band*Yf.mean(2)-1e-12)
                cnt['pass_band']+=int(pb.sum());ok&=pb
            cnt['combinations']+=tot.size;cnt['pass_target']+=int(p1.sum());cnt['pass_floor']+=int(p2.sum());cnt['pass_target_and_floor']+=int(ok.sum())
            ga,rb=np.nonzero(ok)
            for s in range(0,len(ga),200000):                                     # exact evaluation of every survivor
                g=gi[ga[s:s+200000]];r=ri[rb[s:s+200000]];res=combo_eval(A,B,g,r,capital,span,buffer,max_margin);t,Y,w,dd,mg,ex,lo=res
                p3=dd<=max_dd;p4=ex<=0;p5=lo<=cap+1e-9;cnt['evaluated_exactly']+=len(g);cnt['pass_drawdown_of_target_and_floor']+=int(p3.sum())
                cnt['pass_funds_of_target_and_floor']+=int(p4.sum());cnt['pass_lot_cap_of_target_and_floor']+=int(p5.sum())
                q=p3&p4&p5;cnt['pass_all']+=int(q.sum())
                if q.any():
                    for k_,v in zip(F,(g[q],r[q],np.full(q.sum(),la),np.full(q.sum(),lb))+tuple(x[q] for x in res)):keep[k_].append(v)
            sf=np.maximum(target-tot,0)/target+np.where(np.isfinite(worst),np.maximum(floor-worst,0)/max(floor,1e-9),10.)
            if band and A.full:sf=sf+_band_short((A.y[gi][:,None,A.full]+B.y[ri][None,:,A.full])/capital,band)   # V4.9.9: the closest list respects the band too
            k=min(closest_keep,sf.size);f=np.argpartition(sf.ravel(),k-1)[:k];x,y=np.unravel_index(f,sf.shape)
            close.append((sf.ravel()[f],gi[x],ri[y],np.full(k,la),np.full(k,lb)))
    cnt.setdefault('pass_all',0);cnt.setdefault('evaluated_exactly',0)
    if keep:
        sel={k_:np.concatenate(v) for k_,v in keep.items()};k1,k2=rank_key(rank,sel['tot'],sel['worst'],sel['dd'],sel['Y'],judged)
        order=np.lexsort((sel['lb'],sel['la'],sel['r'],sel['g'],k2,k1))
        n_r=max(B.n for B in FR.values())+1;pair=sel['g'].astype(np.int64)*n_r+sel['r'];_,first=np.unique(pair[order],return_index=True)   # each pair once, at its best sizes
        order=order[np.sort(first)];order=order[:top] if top else order;cnt['distinct_pairs_passing']=len(first)
        return [_row(sel,i,True) for i in order],True,cnt
    if not close:return [],False,cnt
    sf,g,r,la,lb=(np.concatenate(x) for x in zip(*close));k=np.argsort(sf,kind='stable')[:closest_keep];g,r,la,lb=g[k],r[k],la[k],lb[k]
    m=collections.defaultdict(list)
    for a_ in np.unique(la):
        for b_ in np.unique(lb[la==a_]):
            s=(la==a_)&(lb==b_);A=FG[a_] if a_ else FG[0];B=FR[b_] if b_ else FR[0];res=combo_eval(A,B,g[s],r[s],capital,span,buffer,max_margin)
            for k_,v in zip(F,(g[s],r[s],la[s],lb[s])+res):m[k_].append(v)
    m={k_:np.concatenate(v) for k_,v in m.items()};f=leg_share(m['la'],m['lb'],allocation)*np.ones(len(m['la']));target=np.array([simple_target(target0*x,span,basis) for x in f]);floor=floor0*f;max_dd=dd0*f
    w=np.where(np.isfinite(m['worst']),np.maximum(floor-m['worst'],0)/np.maximum(floor,1e-9),10.)
    fy=next(iter(FG.values())).full
    if band and fy:w=w+_band_short(m['Y'][:,fy],band)
    short=np.maximum(target-m['tot'],0)/target+w+np.maximum(m['dd']-max_dd,0)/max_dd+np.maximum(m['ex'],0)/capital+(np.maximum(m['lo']-cap,0)/cap if np.isfinite(cap) else 0.)
    order=np.lexsort((m['lb'],m['la'],m['r'],m['g'],short))[:top]
    return [_row(m,i,False) for i in order],False,cnt

def _row(m,i,passed):return dict(g=int(m['g'][i]),r=int(m['r'][i]),la=int(m['la'][i]),lb=int(m['lb'][i]),tot=float(m['tot'][i]),Y=m['Y'][i],worst=float(m['worst'][i]),dd=float(m['dd'][i]),
                                 mg=float(m['mg'][i]),ex=float(m['ex'][i]),lo=float(m['lo'][i]),passed=passed)

def rank_key(rank,tot,worst,dd,Y=None,judged=None):
    """Primary and secondary sort keys (ascending). dispersion: standard deviation of the judged yearly returns, then drawdown."""
    if rank=='dispersion':
        sd=np.nanstd(Y[:,judged],1) if Y is not None and judged and len(judged)>1 else np.zeros(len(tot))
        return sd,dd
    if rank=='worst_year':return -worst,-tot
    if rank=='calmar':return -(tot/np.maximum(dd,1.)),-tot
    return dd,-tot

# ---------------- main flow ----------------
def run(grid,paths_db,out,capital=CAPITAL,target=.22,floor=.17,max_dd=250_000.,rank='dd',pool=150,min_trades=15,carry=.055,top=50,
        holidays=(),workers=1,progress=True,min_days=90,sizing='exchange',ref_units=650.,margin=None,leg_lots=None,if_none='cash',wf_top=10,
        wf_min_trades=None,carry_scenarios=(0.,.03,.055,.08),buffer=0.,max_lots=None,return_basis='simple',cap_unit='reference',min_full_years=2,
        holdout_end='2026-07-01',holdout_min_full_years=1,qualify_max=0,margin_model='table',calendar_margin_factor=1.,max_margin=None,
        min_train_years=3,band=.70,min_fill_rate=.75,allocation=ALLOCATION):
    global HOLIDAYS
    grid=Path(grid);out=Path(out);out.mkdir(parents=True,exist_ok=True);t0=time.time();HOLIDAYS=frozenset(holidays);max_margin=max_margin or capital
    mm=MarginModel(margin_model,margin,calendar_margin_factor)
    gi=zg.read_json(grid/'grid_index.json');start,split=gi['start'],gi['split'];factor=_year_factor(start,split);gap_policy=gi.get('gap_policy','free')
    wins=[w[0] for w in gi['windows']];grid_years=sorted(int(w[5:]) for w in wins if w.startswith('test_'))
    wmt=wf_min_trades if wf_min_trades is not None else max(10,min_trades//2)
    full_before=lambda y:[t for t in range(int(start[:4]),y) if f'{t}-01-01'>=start]          # complete calendar years of training before y
    test_years=[y for y in grid_years if len(full_before(y))>=min_train_years]                  # V4.9.7: a test year needs min_train_years full years before it
    folds={f'test_{y}':(f'train_{y}',sorted({t for t in grid_years+[int(start[:4])] if t<y}),wmt) for y in test_years}
    folds['in_sample']=('train_full',sorted(set(test_years+[int(start[:4])])),min_trades)
    P,_=pools(grid,{k:v for k,v in folds.items() if v[0] in wins},factor,pool,workers,min_fill_rate)
    if progress:print('pools: '+', '.join(f'{f} {s} {len(v)}' for (f,s),v in sorted(P.items()))+f' ({time.time()-t0:.0f}s)',flush=True)
    D=zg.Detail(paths_db,holidays,gap_policy);L=D.target_lots or 10;tu=D.target_units;tm=D.target_margin;tf=D.target_fraction
    leg_lots=sorted(set(leg_lots if leg_lots is not None else ((0,L) if tm else range(L+1))))   # V4.9.7: with margin-table entries a leg trades its full entry size or not at all
    sess_days=sorted({d for d in D.bars.day if start<=d<split});sess_ord=np.array([dt.date.fromisoformat(d).toordinal() for d in sess_days]);T=len(sess_days)
    last_close={};[last_close.__setitem__(d,c) for d,c in zip(D.bars.day,D.bars.close)];sess_close=np.array([last_close[d] for d in sess_days])
    sz=Sizing(sizing,ref_units,L,tu,cap_unit,tm,tf)
    def sel_of(key):side,st,off,cfg,prof,stop=key;return dict(side=side,structure=st,offset=off,config=cfg,profit=prof,stop=stop)
    curves={};files={}
    def curve(key):
        """Training curve of a candidate (screen): prepared trades plus its daily equity / open / margin at every searched size."""
        if key not in curves:
            p=prepare(D.trades(sel_of(key),start,split),sess_ord,key[1],carry,margin_unit=mm.per_unit(key[1],sess_days,sess_close))
            curves[key]=dict(prep=p,at={k:combine(p,k,sz) for k in leg_lots if k},bad=p['bad'],n=p['n'],stale=p['stale'],marks=p['marks'],
                             reduced=sum(1 for *_,x,_h in p['pieces'] if sz.matched(x)<sz.target(x)))
        return curves[key]
    allkeys=sorted({k for v in P.values() for k in v},key=lambda k:(k[1],k[2],k[0],k[3:]))
    for (f,s),v in P.items():files.update(v)
    for i,k in enumerate(allkeys):
        curve(k)
        if progress and (i+1)%500==0:print(f'  replayed {i+1}/{len(allkeys)} ({time.time()-t0:.0f}s)',flush=True)
    if progress:print(f'replayed {len(allkeys)} strategies ({time.time()-t0:.0f}s)',flush=True)
    dropped={s:sum(1 for k in allkeys if k[0]==s and curves[k]['bad']) for s in ('bull','bear')}
    zero=(np.zeros(T),np.zeros(T,bool),np.zeros(T),np.zeros(T))
    gates=[];picks={};ins=[];report=dict(start=start,split=split,planned_test_years=test_years,min_train_years=min_train_years,band_floor=band,min_fill_rate=min_fill_rate,paths_target_fraction=tf,
        capital_basis='account' if allocation is None else 'allocated',allocation_per_leg=allocation,
        allocated_note=None if allocation is None else f'target, floor and drawdown limit are those of the account x {allocation:.4f} per leg traded (nominal share; a simplification to be tuned to actual margin use); returns are reported on the full capital',
        walk_forward_note=None if test_years else f'no walk-forward year: each needs {min_train_years} full calendar years of training before it; the in-sample choice goes to the holdout (nifty-zone-holdout), the forward test',min_year_days=min_days,capital=capital,target=target,floor=floor,max_drawdown=max_dd,rank=rank,pool=pool,
        min_trades=min_trades,wf_min_trades=wmt,carry=carry,sizing=sizing,paths_target_margin=tm,paths_target_units=tu,simulated_lots=L,leg_lots=leg_lots,max_open_lots=max_lots,lot_cap_unit=cap_unit,
        return_basis=return_basis,min_full_years=min_full_years,margin_model=margin_model,margin_budget=max_margin,calendar_margin_factor=calendar_margin_factor,assumed_margin_rates=mm.rates if margin_model=='rates' else None,
        margin_buffer=buffer,if_none=if_none,gap_policy=gap_policy,candidates_dropped_for_unvalued_trades=dropped,folds={},
        conventions=dict(returns='simple rupee P&L / capital per calendar year, part years >= min_year_days annualised, not compounded; overall = P&L over the elapsed period / capital / years',
            canonical='every ranked, printed and frozen figure comes from the executable account (orders admitted at each decision against funds and the margin budget); screen_* columns are the vectorised pre-screen',
            size='a leg of k (of K) holds min(lots matched on the path, floor(k x path target lots / K)) whole exchange lots; with paths built at --target-margin, k / K is the share of the margin budget',
            margin='table: upper end of the strategy_spec margin table per unit on the day; rates: share of notional x index close',
            training_curves='screen: one continuous strategy from start (the grid convention); qualification: executable account adopted flat at start',
            drawdown='daily close mark-to-market; not intraday',stale_marks='last known leg close carried forward; share of bar closes without a fresh print reported'))
    for fold,(wname,years,mt) in folds.items():
        y=int(fold[5:]) if fold.startswith('test_') else None
        g,dg=_distinct([k for k in P.get((fold,'bull'),{}) if not curves[k]['bad']],curves);r,dr=_distinct([k for k in P.get((fold,'bear'),{}) if not curves[k]['bad']],curves)
        lo=start;hi=f'{y}-01-01' if y else split
        ys=year_slices(sess_days,lo,hi)
        if (not g and not r) or not ys or wname not in wins:
            why='no_training_sessions' if not ys or wname not in wins else 'no_candidates'
            report['folds'][fold]=dict(note=why);gates.append(dict(fold=fold,note=why,green_candidates=len(g),red_candidates=len(r)))
            if y:picks[y]=([],False,why)
            continue
        i0=ys[0][1];i1=ys[-1][2];span=sum(d for *_,d in ys)/365.
        FG={k:Fold([curves[c]['at'][k] if k else zero for c in g],i0,i1,ys,min_days,T) for k in set(leg_lots)|{0}}
        FR={k:Fold([curves[c]['at'][k] if k else zero for c in r],i0,i1,ys,min_days,T) for k in set(leg_lots)|{0}}
        res,passed,cnt=search(FG,FR,leg_lots,capital,span,target,floor,max_dd,rank,None,buffer,max_lots,basis=return_basis,max_margin=max_margin,band=band,allocation=allocation)   # every screen-passing pair
        rows=[_out(fold,x,g,r,curves,ys,min_days,FG,FR) for x in res]
        if passed:                                                                 # executable account = source of truth (V4.9.6)
            tacc=Account(D,sess_days[:i1+1],sess_ord[:i1+1],sess_close[:i1+1],hi,capital,buffer,min_days,mm,sz,sel_of,max_lots,max_margin)
            qual=[];checked=0;limit=qualify_max or len(rows)
            for row,x in zip(rows,res):
                if checked>=limit:break
                checked+=1;f=float(leg_share(row['green_lots'],row['red_lots'],allocation))
                row.update(qualify(tacc,row,start,hi,capital,target*f,floor*f,max_dd*f,return_basis,min_days,carry,band),allocated_share=round(f,6))
                if row['executable_pass']:qual.append((row,x))
                if progress and checked%2000==0:print(f'  {fold}: executable replay {checked:,} of {len(rows):,} ({len(qual):,} pass, {time.time()-t0:.0f}s)',flush=True)
            cnt.update(executable_checked=checked,executable_pass=len(qual),screen_pairs=len(rows),search_complete=checked>=len(rows))
            if qual:
                k1,k2=_exec_rank(rank,[q[0] for q in qual])
                order=sorted(range(len(qual)),key=lambda i:(k1[i],k2[i],i))           # executable metrics decide; ties keep screen order
                qual=[qual[i] for i in order];rows,res=[q[0] for q in qual],[q[1] for q in qual]
            else:passed=False
        gates.append(dict(fold=fold,green_candidates=len(g),red_candidates=len(r),green_identical_dropped=dg,red_identical_dropped=dr,**cnt))
        if rows and passed:
            f=float(leg_share(rows[0]['green_lots'],rows[0]['red_lots'],allocation))
            rows[0].update(_robust(rows[0],res[0],FG,FR,curves,curve,files,ys,i0,i1,capital,span,simple_target(target*f,span,return_basis),floor*f,max_dd*f,grid,min_days,T,buffer,max_lots,max_margin))
        why='pass' if passed else ('no_pair_passed' if not cnt.get('executable_checked') else 'no_executable_pass' if cnt.get('search_complete') else 'search_incomplete')
        if y:picks[y]=(rows,passed,why)
        else:ins=rows[:top]
        report['folds'][fold]=dict(green_candidates=len(g),red_candidates=len(r),**cnt,result=why,best={k:v for k,v in rows[0].items() if not k.startswith('_')} if rows else None)
        if progress:print(f'{fold}: {cnt["combinations"]:,} combinations, {cnt.get("screen_pairs",0):,} screen pairs, {cnt.get("executable_pass",0):,} executable passes of {cnt.get("executable_checked",0):,} replayed ({time.time()-t0:.0f}s)',flush=True)
    for y in test_years:picks.setdefault(y,([],False,'no_training_sessions'))
    acc=Account(D,sess_days,sess_ord,sess_close,split,capital,buffer,min_days,mm,sz,sel_of,max_lots,max_margin)
    wf,daily,orders,summary=walk_forward(acc,picks,if_none,wf_top,floor,carry,carry_scenarios)
    if sizing=='normalized':                                                       # a research proxy: quantities need not be whole exchange lots
        for w in wf:w['deployable']=False
        summary['every_year_deployable']=False;summary['deployable_years']=0;summary['note']='normalized sizing: research proxy, not executable; '+summary.get('note','')
    fw=max([float(leg_share(w.get('green_lots',0),w.get('red_lots',0),allocation)) for w in wf if w.get('traded')] or [1.])   # the most-allocated year's legs decide (conservative)
    summary.update(verdict(summary,wf,target*fw,floor*fw,max_dd*fw,return_basis,min_full_years),verdict_share=round(fw,6))
    if ins and ins[0].get('executable_pass'):_freeze(out,ins[0],grid,gi,paths_db,dict(capital=capital,target=target,floor=floor,max_dd=max_dd,sizing=sizing,ref_units=ref_units,margin=margin,margin_model=margin_model,
        calendar_margin_factor=calendar_margin_factor,max_margin=max_margin,buffer=buffer,max_lots=max_lots,cap_unit=cap_unit,carry=carry,min_days=min_days,return_basis=return_basis,gap_policy=gap_policy,
        holidays=list(holidays),holdout_start=zg.HOLDOUT_START,holdout_end=holdout_end,holdout_min_full_years=holdout_min_full_years,band=band,min_train_years=min_train_years,allocation=allocation,
        min_fill_rate=min_fill_rate,target_fraction=tf))
    D.close();report['walk_forward']=summary;report['sizing_lot_unknown']=sz.stats.get('lot_unknown',0)
    _csv(out/'portfolio_walk_forward.csv',wf);_csv(out/'portfolio_pairs_in_sample.csv',ins);_csv(out/'portfolio_gate_counts.csv',gates);_csv(out/'portfolio_account_daily.csv',daily);_csv(out/'portfolio_orders.csv',orders)
    report['gate_counts']=gates;zg.write_json(report,out/'portfolio_report.json',indent=1,default=str)
    return report,wf,ins,gates

def _exec_rank(rank,rows):
    """Sort keys (ascending) of executable-qualified rows by the ranking on their EXECUTABLE account metrics."""
    dd=[r['executable_drawdown'] for r in rows];ov=[-(r['executable_overall'] or -1e9) for r in rows]
    if rank=='worst_year':return [-(r['executable_worst_year'] if r['executable_worst_year'] is not None else -1e9) for r in rows],ov
    if rank=='calmar':return [-((r['executable_overall'] or 0)/max(r['executable_drawdown'],1.)) for r in rows],ov
    if rank=='dispersion':return [r['executable_dispersion'] if r['executable_dispersion'] is not None else 0. for r in rows],dd
    return dd,ov

def _identity(c):
    """Stable digest of a candidate's trading: every trade's signal bar, entry / exit day, units and value, and its daily
    equity / margin / lots at every size. Equal digests = the same trades, not merely the same daily P&L."""
    import hashlib
    h=hashlib.sha256()
    for *_,x,held in c['prep']['pieces']:h.update(repr((x['signal_bar'],x['entry_day'],x['exit_day'],x['units'],x['lot'],x['unit'])).encode())
    for k,a in sorted(c['at'].items()):
        for arr in a:h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()

def _distinct(keys,curves):
    """One candidate per distinct trading history (different rule labels with identical trades, e.g. a wide stop that never
    fires, or a layered policy whose only active part is its mandatory exit; compared by _identity). Returns (kept, dropped count)."""
    seen={};out=[]
    for k in sorted(keys,key=lambda k:(k[1],k[2],k[3:])):
        c=curves[k];h=_identity(c)
        if h in seen:curves[seen[h]]['aliases']=curves[seen[h]].get('aliases',1)+1;continue
        seen[h]=k;out.append(k);c.setdefault('aliases',1)
    return out,len(keys)-len(out)

def _leg(name,key,lots,curves):
    if key is None or not lots:return {f'{name}_lots':0,f'{name}_structure':'none'}
    side,st,off,cfg,prof,stop=key;c=curves[key]
    return {f'{name}_lots':lots,f'{name}_structure':st,f'{name}_offset':off,f'{name}_config':cfg,f'{name}_profit':prof,f'{name}_stop':stop,f'{name}_trades':c['n'],
            f'{name}_trades_matched_below_simulated_lots':c['reduced'],f'{name}_stale_share':round(c['stale']/c['marks'],3) if c['marks'] else None,f'{name}_identical_rules':c.get('aliases',1)}

def _out(fold,x,g,r,curves,ys,min_days,FG,FR):
    """A candidate row. Canonical columns (overall, worst_year, daily_close_drawdown, assumed_margin_peak, year_*) hold the
    screen values until qualify() replaces them with the executable account's; screen_* keep the screen values."""
    gk=g[x['g']] if x['g']<len(g) else None;rk=r[x['r']] if x['r']<len(r) else None
    ov=int((FG[x['la']].open[x['g']]&FR[x['lb']].open[x['r']]).sum()) if x['la'] and x['lb'] else 0
    sp=sum(y[4] for y in ys)/365.;worst=None if math.isnan(x['worst']) else round(x['worst'],6)
    yrs={f'year_{y}'+('' if ys[j][4]>=min_days else f'_part_{ys[j][4]}d_not_annualised'):round(float(x['Y'][j]),6) for j,(y,*_) in enumerate(ys)}
    return dict(fold=fold,passed=x['passed'],metrics_basis='screen',overall=round(x['tot'],6),worst_year=worst,daily_close_drawdown=round(x['dd']),assumed_margin_peak=round(x['mg']),**yrs,
                screen_overall=round(x['tot'],6),screen_overall_cagr=round(float((1+x['tot']*sp)**(1/sp)-1),6) if 1+x['tot']*sp>0 else None,screen_worst_year=worst,screen_daily_close_drawdown=round(x['dd']),
                screen_assumed_margin_peak=round(x['mg']),screen_margin_excess_over_limit=round(x['ex']),screen_open_cap_lots_peak=round(x['lo'],3),
                overlap_sessions=ov,**_leg('green',gk,x['la'],curves),**_leg('red',rk,x['lb'],curves),_g=gk,_r=rk)

# ---------------- executable walk-forward account ----------------
class Account:
    """One chronological account. legs: [(label year, side name, key, size k, entry from, entry to)]. Every signal of every leg,
    in time order: a leg takes it if the leg is free (one position per leg; a data gap holds the leg as the grid does); the order
    is then admitted at the signal bar's close, using only what is known then:
      in use   = positions open at the order's fill bar (entry bar <= fill bar <= exit bar, intraday included): their assumed
                 margin at the signal bar's index close, and their reference lots
      funds    = capital + the account's P&L at the previous session close
      lots     = min(exchange lots the leg size wants, lots whose margin fits (1 - buffer) x funds - margin in use,
                 lots keeping the combined open reference lots <= max_lots)
    Fewer than one lot: rejected (logged); the leg stays free for its next signal."""
    def __init__(self,D,sess_days,sess_ord,sess_close,split,capital,buffer,min_days,margin,sz,sel_of,max_lots=None,max_margin=None):
        self.D=D;self.days=sess_days;self.ord=sess_ord;self.close=sess_close;self.split=split;self.capital=capital;self.buffer=buffer;self.min_days=min_days
        self.mm=margin if isinstance(margin,MarginModel) else MarginModel('rates',margin,defaults=False)   # V4.9.6: a MarginModel (a dict: exactly those rates)
        self.sz=sz;self.sel_of=sel_of;self.max_lots=max_lots;self.max_margin=max_margin or capital;self._ev={};self._prep={};self._mu={}
        self.bar_close=np.asarray(D.bars.close,float);self.bar_day=list(D.bars.day)
        self.end=horizon_end(sess_days,split).isoformat() if sess_days else split      # the intended horizon when the data reaches it
        ix={d:i for i,d in enumerate(sess_days)};self.bar_sess=np.array([ix.get(d,-1) for d in self.bar_day],np.int64)
    def mu(self,structure,bars=False):
        """Margin per unit, per bar close (bars=True) or per session close, for a structure."""
        k=(structure,bars)
        if k not in self._mu:self._mu[k]=self.mm.per_unit(structure,self.bar_day,self.bar_close) if bars else self.mm.per_unit(structure,self.days,self.close)
        return self._mu[k]
    def events(self,key,lo,hi):
        """Signals of one leg adopted flat at lo with entries in [lo, hi) (cached)."""
        if (key,lo,hi) not in self._ev:
            ev=[]
            for kind,t,until,x in self.D.signal_outcomes(self.sel_of(key),self.end):
                day=x if kind=='gap' else x['entry_day']
                if lo<=day<hi:ev.append((kind,t,until,x))
            self._ev[key,lo,hi]=ev
        return self._ev[key,lo,hi]
    def run(self,legs,carry):
        """Returns (daily equity A, daily peak assumed margin M, open masks by (year, side), order ledger, daily peak cap lots,
        daily peak exchange lots, daily peak gross contracts)."""
        T=len(self.days);A=np.zeros(T);opn={};orders=[];busy={};live=[];realised=0.
        nb=len(self.bar_close);Mb=np.zeros(nb);Lb=np.zeros(nb);Xb=np.zeros(nb);Cb=np.zeros(nb)       # per bar close: margin, cap lots, exchange lots, gross contracts
        bar_ord=np.array([dt.date.fromisoformat(d).toordinal() for d in self.bar_day],np.int64) if nb else np.zeros(0,np.int64)
        evs=sorted(((t,li,kind,until,x) for li,(y,name,key,k,lo,hi) in enumerate(legs) for kind,t,until,x in self.events(key,lo,hi)),key=lambda e:(e[0],e[1]))
        def mark(h,t):
            """Open trade h marked at bar t's close (its last finite mark at or before t, precomputed; carry accrued): rupees."""
            j=t-h['fb'];m=h['carried']
            if j<0 or not len(m):return 0.
            return (m[min(j,len(m)-1)]-h['c']*(bar_ord[min(t,nb-1)]-h['e0']))*h['u']
        for t,li,kind,until,x in evs:
            y,name,key,k,lo,hi=legs[li]
            if t<=busy.get(li,-1):continue
            if kind=='gap':busy[li]=until;orders.append(dict(year=y,leg=name,signal_bar=t,day=x,decision='no fill (data gap, or entry not completed in its windows): leg held, outcome unknown'));continue
            pk=(key,t,carry)
            if pk not in self._prep:self._prep[pk]=prepare([x],self.ord,key[1],carry,margin_unit=self.mu(key[1]))
            p=self._prep[pk];mub=self.mu(key[1],True)
            if not p['pieces']:busy[li]=until;orders.append(dict(year=y,leg=name,signal_bar=t,day=x['entry_day'],decision='trade without a value: leg held, not counted'));continue
            if p['pieces'][0][0]>=T:continue
            fill=t+1;px=self.bar_close[t]                                            # decision at the signal bar's close; fills in the next bar
            done=[h for h in live if h['until']+1<=t];live=[h for h in live if h['until']+1>t]   # an exit's last fill is in the bar after its exit bar
            realised+=sum(h['final'] for h in done)
            used=sum(h['mub'][t]*h['u'] for h in live);used_lots=sum(h['cap'] for h in live)            # margin of open positions at this bar's close
            funds=self.capital+realised+sum(mark(h,t) for h in live)                # time-causal: realised P&L so far + open positions at this close
            ex=self.sz.sizing=='exchange';upl=self.sz.unit_per_lot(x);per_lot=mub[t]*upl;cap_per_lot=self.sz.cap_lots(x,upl)
            limit=min(self.max_margin,(1-self.buffer)*funds)                         # the margin budget, never more than the funds allow
            want=self.sz.lots(x,k) if ex else self.sz.units(x,k)/upl                 # exchange: whole exchange lots; normalized (proxy): fractional reference lots
            rnd=(lambda v:int(max(0.,math.floor(v+1e-9)))) if ex else (lambda v:max(0.,v))
            fit_m=want if per_lot<=0 else rnd((limit-used)/per_lot)
            fit_l=want if not self.max_lots else rnd((self.max_lots-used_lots)/cap_per_lot)
            lots=min(want,fit_m,fit_l)
            rec=dict(year=y,leg=name,signal_bar=t,day=x['entry_day'],index_at_decision=round(float(px),2),margin_limit=round(limit),lots_wanted=want,lots_matched_on_path=self.sz.matched(x),lots_taken=max(lots,0),
                     funds_at_decision=round(funds),margin_in_use=round(used),margin_per_lot=round(per_lot),open_cap_lots=round(used_lots,3))
            if (lots<1 if ex else lots<=0):
                rec['decision']='rejected: '+('leg size or path match below one exchange lot of this contract' if (want<1 if ex else want<=0) else 'assumed margin does not fit available funds' if fit_m<=fit_l else 'open lot cap reached');orders.append(rec);continue
            rec['decision']='taken' if lots==want else 'cut to fit '+('available funds' if fit_m<=fit_l else 'the open lot cap');orders.append(rec)
            u=lots*upl
            class _S:
                ref_lot=self.sz.ref_lot
                def units(s_,x_,k_):return u
                def cap_lots(s_,x_,u_):return 0.
            E,op,_,_=combine(p,1,_S());A+=E;opn.setdefault((y,name),np.zeros(T,int))[:]+=op;busy[li]=until
            fut=key[1].startswith('futures');c=x.get('sign',1.)*x.get('index',0.)*carry/365. if fut and carry else 0.
            exl=u/(x.get('lot') or upl);legs_n=2 if key[1]=='calendar' else 1
            mk=np.asarray(x['marks'],float);ix=np.where(np.isfinite(mk),np.arange(len(mk)),-1);np.maximum.accumulate(ix,out=ix) if len(ix) else None
            h=dict(fill=fill,until=until,u=u,mub=mub,cap=self.sz.cap_lots(x,u),final=(p['pieces'][0][4]*u),sb=t,fb=int(x.get('first_bar',t+1)),carried=np.where(ix>=0,mk[np.maximum(ix,0)],0.) if len(mk) else mk,c=c,
                   e0=dt.date.fromisoformat(x['entry_day']).toordinal())
            live.append(h);b1=min(until+1,nb-1)+1                                    # held through the bar of its last exit fill
            if b1>fill:Mb[fill:b1]+=mub[fill:b1]*u;Lb[fill:b1]+=h['cap'];Xb[fill:b1]+=exl;Cb[fill:b1]+=exl*legs_n
        M=np.zeros(T);Lo=np.zeros(T);Xl=np.zeros(T);Gc=np.zeros(T);ok=self.bar_sess>=0                 # each session: the largest bar-close value of the day
        for arr,src in ((M,Mb),(Lo,Lb),(Xl,Xb),(Gc,Cb)):np.maximum.at(arr,self.bar_sess[ok],src[ok])
        self.last_exchange_lots=Xl;self.last_gross_contracts=Gc
        return A,M,opn,orders,Lo
    def year(self,A,y):
        ys=year_slices(self.days,f'{y}-01-01',min(f'{y+1}-01-01',self.end))
        if not ys:return None,None,None
        _,a,b,f,days=ys[0];e0=A[a-1] if a>0 else 0.;return (A[b]-e0)/self.capital*(f if days>=self.min_days else 1.),days,(a,b)

def qualify(acc,row,lo,hi,capital,target,floor,max_dd,basis,min_days,carry,band=0.):
    """Executable qualification of a training candidate: replay it through the chronological account on [lo, hi) (adopted flat at
    lo, every order admitted against funds at the decision and the margin budget) and apply the gates to that account. On return
    the row's canonical metrics are the executable ones (metrics_basis 'executable')."""
    legs=[('train',n,kk,ll,lo,hi) for n,kk,ll in (('green',row['_g'],row['green_lots']),('red',row['_r'],row['red_lots'])) if ll and kk is not None]
    A,M,opn,orders,Lo=acc.run(legs,carry);T=len(acc.days)
    ys=year_slices(acc.days,lo,hi)
    if not ys:return dict(executable_pass=False,executable_note='no sessions')
    yr=[];full=[];i0=ys[0][1];yrs={}
    for y,a,b,f,days in ys:
        e0=A[a-1] if a>0 else 0.;v=(A[b]-e0)/capital*(f if days>=min_days else 1.)
        yrs[f'year_{y}'+('' if days>=min_days else f'_part_{days}d_not_annualised')]=round(float(v),6)
        if days>=min_days:yr.append(v)
        if is_full_year(y,days):full.append(v)
    s,*_=account_summary(acc,A,M,Lo,opn,orders,[y for y,*_ in ys],i0,start_date=lo,xl=np.zeros(T),gc=np.zeros(T))
    overall=s['overall_simple'] if basis=='simple' else s['cagr_fixed_size_no_withdrawals'];worst=min(yr) if yr else float('nan')
    in_band=not band or not full or (min(full)>0 and min(full)>=band*float(np.mean(full))-1e-12)
    ok=overall is not None and overall>=target and worst>=floor and in_band and s['daily_close_drawdown_continuous']<=max_dd and not s['funds_short_sessions']
    out=dict(executable_pass=bool(ok),executable_overall=None if overall is None else round(float(overall),6),executable_worst_year=None if not yr else round(float(worst),6),
             executable_dispersion=round(float(np.std(yr)),6) if len(yr)>1 else None,executable_in_band=bool(in_band),executable_drawdown=s['daily_close_drawdown_continuous'],executable_assumed_margin_peak=s['assumed_margin_peak'],
             executable_orders_cut=s['orders_cut'],executable_orders_rejected=s['orders_rejected'],executable_funds_short_sessions=s['funds_short_sessions'])
    out.update(metrics_basis='executable',overall=out['executable_overall'],worst_year=out['executable_worst_year'],daily_close_drawdown=out['executable_drawdown'],
               assumed_margin_peak=out['executable_assumed_margin_peak'],**yrs)
    return out

def _year_legs(y,row,split):
    lo=f'{y}-01-01';hi=min(f'{y+1}-01-01',split)
    return [(y,n,kk,ll,lo,hi) for n,kk,ll in (('green',row['_g'],row['green_lots']),('red',row['_r'],row['red_lots'])) if ll and kk is not None]

def account_summary(acc,A,M,Lo,opn,orders,years,i0,start_date=None,xl=None,gc=None):
    """Daily series and whole-account figures of an account run from session i0."""
    T=len(acc.days);base=A[i0-1] if i0>0 else 0.;S=np.r_[0.,A[i0:]-base];run_dd=np.maximum.accumulate(S)-S
    G=sum((v for (y,n),v in opn.items() if n=='green'),np.zeros(T,int));R=sum((v for (y,n),v in opn.items() if n=='red'),np.zeros(T,int))
    funds=acc.capital+A;short=M>funds;breach=M>(1-acc.buffer)*funds                 # short: a margin call; breach: the buffer is used
    over=M>getattr(acc,'max_margin',None) if getattr(acc,'max_margin',None) else np.zeros(T,bool)   # above the margin budget (prices moved after admission)
    a0=dt.date.fromisoformat(start_date) if start_date else (dt.date(int(years[0]),1,1) if years and str(years[0]).isdigit() else dt.date.fromisoformat(acc.days[i0]) if T>i0 else None)
    days=(dt.date.fromisoformat(acc.end)-a0).days if T>i0 and a0 else 0                    # calendar days from the period start to the day after the last session
    w0=acc.capital+base;w1=acc.capital+A[-1] if T else w0
    cagr=(w1/w0)**(365./days)-1 if days>0 and w0>0 and w1>0 else None
    s=dict(overall_simple=round(float((A[-1]-base)/acc.capital/(days/365.)),6) if days>0 else None,elapsed_years=round(days/365.,4),
           daily_close_drawdown_continuous=round(float(run_dd.max())),assumed_margin_peak=round(float(M[i0:].max())) if T>i0 else 0,open_cap_lots_peak=round(float(Lo[i0:].max()),3) if T>i0 else 0,
           exchange_lots_peak=round(float(xl[i0:].max()),2) if xl is not None and T>i0 else None,gross_contracts_peak=round(float(gc[i0:].max()),2) if gc is not None and T>i0 else None,
           funds_short_sessions=int(short[i0:].sum()),buffer_breach_sessions=int(breach[i0:].sum()),margin_over_budget_sessions=int(over[i0:].sum()),
           sessions_both_sides_open=int(((G>0)&(R>0))[i0:].sum()),sessions_two_positions_one_side=int(((G>1)|(R>1))[i0:].sum()),
           orders_rejected=sum(o['decision'].startswith('rejected') for o in orders),orders_cut=sum(o['decision'].startswith('cut') for o in orders),
           cagr_fixed_size_no_withdrawals=None if cagr is None else round(float(cagr),6),terminal_wealth=round(float(w1)))
    daily=[dict(day=d,equity=round(float(A[i]-base)),funds=round(float(funds[i])),assumed_margin=round(float(M[i])),open_cap_lots=round(float(Lo[i]),3),
                exchange_lots=round(float(xl[i]),2) if xl is not None else None,gross_contracts=round(float(gc[i]),2) if gc is not None else None,
                green_positions=int(G[i]),red_positions=int(R[i]),buffer_breach=bool(breach[i]),funds_short=bool(short[i])) for i,d in enumerate(acc.days) if i>=i0]
    return s,daily,G,R,short,breach

def walk_forward(acc,picks,if_none,wf_top,floor,carry,carry_scenarios):
    years=sorted(picks);legs=[];wf=[]
    for y in years:
        rows,passed,why=picks[y];row=rows[0] if rows else None;trade=row is not None and (passed or if_none=='closest')
        rec=dict(test_year=y,training_result=why,passed_in_training=bool(passed),traded=trade,
                 decision='new entries: pair passed every training gate' if passed else ('new entries: closest failing pair (research only)' if trade else
                          ('no new entries: no pair passed in training' if why=='no_pair_passed' else 'no new entries: no training pair passed in the executable account' if why=='no_executable_pass' else f'no new entries: {why}')))
        if trade:legs+=_year_legs(y,row,acc.split);rec.update({k:v for k,v in row.items() if not k.startswith('_') and k!='fold'})
        wf.append(rec)
    A,M,opn,orders,Lo=acc.run(legs,carry);T=len(acc.days)
    xl=np.array(getattr(acc,'last_exchange_lots',np.zeros(T)));gc=np.array(getattr(acc,'last_gross_contracts',np.zeros(T)))   # snapshot before any other run
    for rec in wf:                                                                # the top passing choices, each alone, in their test year
        rows,passed,_=picks[rec['test_year']]
        if passed and len(rows)>1:
            alt=[acc.year(acc.run(_year_legs(rec['test_year'],x,acc.split),carry)[0],rec['test_year'])[0] for x in rows[:wf_top]]
            alt=[v for v in alt if v is not None]
            if alt:rec.update(top_k=len(alt),top_k_median_return=round(float(np.median(alt)),6),top_k_worst_return=round(float(min(alt)),6),top_k_share_meeting_floor=round(float(np.mean([v>=floor for v in alt])),3))
    if not years:return wf,[],orders,{}
    first=year_slices(acc.days,f'{years[0]}-01-01',acc.split)
    if not first:
        for rec in wf:rec.update(account_return=None,result='unavailable: no sessions')
        return wf,[],orders,{}
    i0=first[0][1];s,daily,G,R,short,breach=account_summary(acc,A,M,Lo,opn,orders,years,i0,xl=xl,gc=gc)
    for rec in wf:
        r_,days,ab=acc.year(A,rec['test_year'])
        if r_ is None:rec.update(account_return=None,result='unavailable: no sessions');continue
        a,b=ab;e0=A[a-1] if a>0 else 0.;sy=np.r_[0.,A[a:b+1]-e0];n=[o for o in orders if o['year']==rec['test_year']]
        held=bool((M[a:b+1]>0).any() or (G[a:b+1]>0).any() or (R[a:b+1]>0).any())
        rec.update(account_return=round(float(r_),6),test_days=days,test_annualised=bool(days>=acc.min_days),full_calendar_year=is_full_year(rec['test_year'],days),account_daily_close_drawdown_in_year=round(float((np.maximum.accumulate(sy)-sy).max())),
                   assumed_margin_peak_in_year=round(float(M[a:b+1].max())),open_cap_lots_peak_in_year=round(float(Lo[a:b+1].max()),3),funds_short_sessions=int(short[a:b+1].sum()),
                   buffer_breach_sessions=int(breach[a:b+1].sum()),orders_taken=sum(o['decision']=='taken' or o['decision'].startswith('cut') for o in n),
                   orders_cut=sum(o['decision'].startswith('cut') for o in n),orders_rejected=sum(o['decision'].startswith('rejected') for o in n),
                   flat_cash_year=not held,deployable=bool(rec['passed_in_training'] and not short[a:b+1].any()))
    full=[w for w in wf if w.get('account_return') is not None and w.get('test_annualised')];rets=[w['account_return'] for w in full]
    cs={}
    for cr in carry_scenarios:
        Ac=acc.run(legs,cr)[0];cs[str(cr)]={str(w['test_year']):round(float(acc.year(Ac,w['test_year'])[0]),6) for w in full}
    summary=dict(planned_years=years,reported_years=[w['test_year'] for w in wf],years=[w['test_year'] for w in full],full_years=[w['test_year'] for w in wf if w.get('full_calendar_year')],returns=[float(v) for v in rets],
                 mean_of_yearly_returns=round(float(np.mean(rets)),6) if rets else None,worst=round(float(min(rets)),6) if rets else None,
                 return_dispersion=round(float(np.std(rets)),6) if len(rets)>1 else None,
                 largest_in_year_drawdown=max([w.get('account_daily_close_drawdown_in_year',0) for w in wf],default=0),**s,
                 deployable_years=sum(bool(w.get('deployable')) for w in wf),no_new_entry_years=sum(not w['traded'] for w in wf),flat_cash_years=sum(bool(w.get('flat_cash_year')) for w in wf),
                 every_year_deployable=all(w.get('deployable') for w in wf),carry_scenarios=cs,
                 note='single executable account: orders admitted at the signal close against funds, margin and the lot cap; continuous daily-close drawdown across year ends; mean / worst over full test years only')
    return wf,daily,orders,summary

def verdict(s,wf,target,floor,max_dd,basis='simple',min_full_years=2):
    """Historical out-of-sample verdict of the stitched account (not of training): PASS only if every objective holds and there
    are at least min_full_years COMPLETE calendar years (else INSUFFICIENT EVIDENCE); judged part years still face the floor. Overall = account P&L over the whole elapsed period /
    capital / elapsed years (simple), or the account's CAGR at fixed size with profits kept (cagr) - never a mean of yearly figures."""
    nf=len(s.get('full_years',s.get('years',[]))) if s else 0                           # complete calendar years (a part year never counts)
    if not s or nf<min_full_years or not s.get('years'):return dict(oos_verdict='INSUFFICIENT EVIDENCE',oos_verdict_reasons=[f"{nf} complete calendar year(s) < {min_full_years} required"],oos_overall_basis=basis)
    overall=s.get('overall_simple') if basis=='simple' else s.get('cagr_fixed_size_no_withdrawals')
    why=[]
    if overall is None or overall<target:why.append(f'overall ({basis}) {overall} < {target}')
    if s['worst']<floor:why.append(f"worst year {s['worst']} < {floor}")
    if s['daily_close_drawdown_continuous']>max_dd:why.append(f"continuous drawdown {s['daily_close_drawdown_continuous']} > {max_dd}")
    if s['funds_short_sessions']:why.append(f"funds short of assumed margin in {s['funds_short_sessions']} sessions")
    if not s['every_year_deployable']:why.append('a test year was not deployable (no passing pair, or funds short)')
    return dict(oos_verdict='PASS' if not why else 'FAIL',oos_verdict_reasons=why,oos_overall_basis=basis)

def _sha(path):
    import hashlib;return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def code_digest():
    """Digest of the code that turns a frozen selection into an account result."""
    import hashlib;from nifty_iv.zone import rules as zone_rules,paths as zone_paths,holdout,signals;from nifty_iv.core import contract_specs,strategy_spec;from nifty_iv.ivcal import calendar_research
    h=hashlib.sha256()
    for m in (__file__,zg.__file__,zone_rules.__file__,zone_paths.__file__,holdout.__file__,contract_specs.__file__,signals.__file__,calendar_research.__file__,strategy_spec.__file__):
        h.update(Path(m).read_bytes())                                                  # V4.9.4: the evaluator, contract sizes and costs too
    h.update(np.__version__.encode());return h.hexdigest()

def _freeze(out,row,grid,gi,paths_db,params):
    """portfolio_freeze.json: the in-sample choice (fitted to all training years) with everything needed to replay it once on the
    holdout (nifty-zone-holdout) and to detect any change of code, data or assumptions."""
    if not row.get('passed') or not row.get('executable_pass'):return
    legs=[dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5],lots=l) for k,l in ((row['_g'],row['green_lots']),(row['_r'],row['red_lots'])) if k is not None and l]
    f=dict(created=dt.datetime.now().isoformat(timespec='seconds'),selection=legs,training=dict(start=gi['start'],split=gi['split']),params=params,
           paths_sha256=gi['paths_sha256'],grid_rules_sha256=gi['rules_sha256'],grid_index_sha256=_sha(Path(grid)/'grid_index.json'),code_sha256=code_digest(),
           in_sample={k:v for k,v in row.items() if not k.startswith('_')})
    zg.write_json(f,Path(out)/'portfolio_freeze.json',indent=1,default=str)

def _neighbour_keys(key,grid,file):
    """One-step neighbours of a strategy inside its grid file (configuration, profit rule, stop rule)."""
    with np.load(grid/file) as z:configs=[str(x) for x in z['configs']];P=[str(x) for x in z['profits']];S=[str(x) for x in z['stops']]
    side,st,off,cfg,prof,stop=key;ci,pi,si=configs.index(cfg),P.index(prof),S.index(stop)
    CN=zr.config_neighbours(configs);PN=zr.neighbours(P);SN=zr.compound_neighbours(S) if stop.startswith('c:') else zr.neighbours(S)
    return [(side,st,off,configs[j],prof,stop) for j in CN[ci] if j>=0]+[(side,st,off,cfg,P[j],stop) for j in PN[pi] if j>=0]+[(side,st,off,cfg,prof,S[j]) for j in SN[si] if j>=0]

def _robust(row,x,FG,FR,curves,curve,files,ys,i0,i1,capital,span,target,floor,max_dd,grid,min_days,T,buffer,max_lots=None,max_margin=None):
    """SCREEN robustness (not replayed through the account): swap each traded leg for its one-step neighbours (other leg and sizes
    fixed): share of those that pass every screen gate (in-sample),
    and the share of neighbours whose trades are identical to the chosen leg's (those pass trivially and say nothing)."""
    res=[];same=[]
    for mine,is_green in ((row['_g'],True),(row['_r'],False)):
        k=x['la'] if is_green else x['lb']
        if mine is None or not k:continue
        f=files.get(mine)
        if f is None:continue
        me=curves[mine]['at'][k][0]
        for nk in _neighbour_keys(mine,grid,f):
            c=curve(nk)
            if c['bad']:res.append(False);same.append(False);continue
            same.append(bool(np.array_equal(c['at'][k][0],me)))
            Fn=Fold([c['at'][k]],i0,i1,ys,min_days,T)
            tot,Y,worst,dd,mg,ex,lo=combo_eval(Fn,FR[x['lb']],[0],[x['r']],capital,span,buffer,max_margin) if is_green else combo_eval(FG[x['la']],Fn,[x['g']],[0],capital,span,buffer,max_margin)
            res.append(bool(tot[0]>=target and worst[0]>=floor and dd[0]<=max_dd and ex[0]<=0 and (not max_lots or lo[0]<=max_lots+1e-9)))
    distinct=[p for p,q in zip(res,same) if not q]
    return dict(screen_neighbour_swaps=len(res),screen_neighbour_pass_share=round(sum(res)/len(res),3) if res else None,screen_neighbour_identical_share=round(sum(same)/len(same),3) if same else None,
                screen_neighbour_pass_share_distinct=round(sum(distinct)/len(distinct),3) if distinct else None)

def _csv(path,rows):
    keys=[];[keys.append(k) for r in rows for k in r if k not in keys and not k.startswith('_')]
    with open(path,'w',newline='') as f:w=csv.DictWriter(f,keys,extrasaction='ignore');w.writeheader();w.writerows(rows)

def parse_margin(text):
    out={}
    for part in (text or '').split(','):
        if not part.strip():continue
        k,v=part.split('=');k=k.strip();v=float(v)
        if not 0<=v<=1:raise ValueError('--margin rates are shares of notional (0..1)')
        if k=='futures':out['futures_zone']=out['futures_expiry']=v
        elif k in MARGIN:out[k]=v
        else:raise ValueError(f'unknown structure in --margin: {k}')
    return out

def parse_lots(text,lots):
    """Leg sizes to search: whole lots, 0..the lots the paths were simulated at (larger sizes were never simulated)."""
    if not text:return None
    out=set()
    for part in text.split(','):
        if '-' in part:a,b=part.split('-');out.update(range(int(a),int(b)+1))
        else:out.add(int(part))
    if min(out)<0:raise ValueError('--leg-lots must be >= 0')
    if max(out)>lots:raise ValueError(f'--leg-lots above {lots}: the paths were simulated at {lots} lots (rebuild them with a larger --lots)')
    if not any(out):raise ValueError('--leg-lots needs a size above 0')
    return sorted(out)

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--grid',required=True,help='nifty-zone-grid output directory');ap.add_argument('--paths',required=True,help='the zone_paths.sqlite the grid was built from')
    ap.add_argument('--out',default='portfolio');ap.add_argument('--capital',type=float,default=CAPITAL)
    ap.add_argument('--target',type=float,default=.22,help='overall yearly return on capital (pre-tax)');ap.add_argument('--floor',type=float,help='minimum return in every judged calendar year (default 0.70 x --target, V4.9.7)')
    ap.add_argument('--return-basis',choices=('simple','cagr'),default='simple',help='what --target means, in training selection, the out-of-sample verdict and the holdout: simple = P&L over the period / capital / years (default); cagr = account CAGR at fixed size, profits kept. Both are reported')
    ap.add_argument('--min-full-years',type=int,default=2,help='judged (full or >= --min-year-days) test years required for a PASS / FAIL verdict, else INSUFFICIENT EVIDENCE')
    ap.add_argument('--holdout-end',default='2026-07-01',help='holdout end (exclusive), frozen for nifty-zone-holdout: 2026-07-01 = through June 2026; 2026-08-01 = July included (if the data has it)')
    ap.add_argument('--holdout-min-full-years',type=int,default=1,help='judged years the holdout needs for PASS / FAIL (frozen; Jan 2025 - Jun 2026 has one full year and a part year)')
    ap.add_argument('--max-dd',type=float,default=250_000.,help='largest allowed fall of combined daily-close equity from its peak, rupees')
    ap.add_argument('--rank',choices=('dd','worst_year','calmar','dispersion'),default='dd',help='dispersion: smallest standard deviation of the judged yearly returns, then drawdown')
    ap.add_argument('--pool',type=int,default=150,help='candidates per side, window and criterion')
    ap.add_argument('--min-trades',type=int,default=15);ap.add_argument('--wf-min-trades',type=int,help='trade minimum in walk-forward training windows (default max(10, min-trades // 2), as the grid)')
    ap.add_argument('--carry',type=float,default=.055);ap.add_argument('--carry-scenarios',default='0,.03,.055,.08');ap.add_argument('--top',type=int,default=50)
    ap.add_argument('--min-year-days',type=int,default=90,help='a part year shorter than this is not annualised and not held to --floor')
    ap.add_argument('--sizing',choices=('exchange','normalized'),default='exchange',help='exchange: whole exchange lots, never above the simulated matched quantity (executable); normalized: same notional every year (research proxy, never deployable)')
    ap.add_argument('--ref-units',type=float,default=650.,help='units of --lots reference lots (650 = 10 lots of 65): the unit of --max-open-lots and of normalized sizing')
    ap.add_argument('--max-open-lots',type=float,default=0.,help='optional extra cap on combined open lots, intraday included (default 0 = none: the margin budget decides the lots)')
    ap.add_argument('--lot-cap-unit',choices=('reference','exchange'),default='reference',help='reference: a full-size position (--target-units) counts --lots whatever the exchange lot (650 units = 10); exchange: actual exchange lots (26 at lot 25). Gross contracts are reported either way')
    ap.add_argument('--margin-model',choices=('table','rates'),default='table',help='table (default): upper-end margin per lot from the strategy_spec margin table on the day; rates: --margin shares of notional')
    ap.add_argument('--max-margin',type=float,help='margin budget: total margin of open positions (both sides, intraday included) may not exceed it (default: --capital, 25 lakh)')
    ap.add_argument('--calendar-margin-factor',type=float,default=1.,help='table model: calendar margin as a share of the table (default 1 = no spread benefit assumed)')
    ap.add_argument('--margin',default='',help='rates model only: assumed margin as a share of notional per structure, e.g. futures=.12,naked=.12,calendar=.04 (defaults shown)')
    ap.add_argument('--margin-buffer',type=float,default=0.,help='share of available funds kept free of margin at admission (default 0: margin may use the budget and the funds in full); later use is reported')
    ap.add_argument('--leg-lots',help='leg sizes in reference lots to search, e.g. 0-10 or 0,5,10 (default 0..the paths file --lots); 0 = leg not traded')
    ap.add_argument('--if-none',choices=('cash','closest'),default='cash',help='test year whose training has no passing pair: no new entries, or trade the closest failing pair (research only)')
    ap.add_argument('--wf-top',type=int,default=10,help='top distinct passing pairs whose test-year results are reported per year')
    ap.add_argument('--qualify-max',type=int,default=0,help='cap on screen-passing pairs replayed through the executable account (default 0 = all; a cap that stops early is reported as search_incomplete, never as no pass)')
    ap.add_argument('--capital-basis',choices=('allocated','account'),default='allocated',help='allocated (V4.9.7 default): each traded leg must meet --allocation of the account target, floor and drawdown limit (a pair: twice that); account: the pair meets them in full (V4.9.6)')
    ap.add_argument('--allocation',default='5/12',help='nominal capital share of one strategy for --capital-basis allocated (default 5/12: 5 of 12 lots); a fraction or a ratio')
    ap.add_argument('--min-train-years',type=int,default=3,help='full calendar years of training a walk-forward test year needs before it (V4.9.7 default 3)')
    ap.add_argument('--band-floor',type=float,default=.70,help='every full training year positive and the worst >= this share of the mean full year (V4.9.7 default 0.70; 0 = off)')
    ap.add_argument('--min-fill-rate',type=float,default=.75,help='grid groups whose entries completed less often than this are not candidates (V4.9.7 default 0.75)')
    ap.add_argument('--holidays');ap.add_argument('--workers',type=int,default=1)
    a=ap.parse_args(argv)
    if a.capital<=0 or a.max_dd<=0 or a.target<=0:sys.exit('--capital, --max-dd and --target must be positive')
    if a.floor is None:a.floor=round(.70*a.target,6)
    try:
        n,_,d=a.allocation.partition('/');alloc=(float(n)/float(d) if d else float(n)) if a.capital_basis=='allocated' else None
    except Exception:sys.exit('--allocation must be a number or a ratio such as 5/12')
    if alloc is not None and not 0<alloc<=1:sys.exit('--allocation must be in (0, 1]')
    if a.floor>a.target:sys.exit('--floor above --target: every year would have to beat the overall target')
    if a.pool<1 or a.top<1 or a.wf_top<1 or a.min_trades<1 or a.qualify_max<0:sys.exit('--pool, --top, --wf-top and --min-trades must be >= 1, --qualify-max >= 0')
    if a.calendar_margin_factor<=0 or (a.max_margin is not None and a.max_margin<=0):sys.exit('--calendar-margin-factor and --max-margin must be positive')
    if not 1<=a.min_year_days<=366:sys.exit('--min-year-days must be 1..366')
    if a.min_full_years<1 or a.holdout_min_full_years<1:sys.exit('--min-full-years and --holdout-min-full-years must be >= 1')
    if a.holdout_end<=zg.HOLDOUT_START:sys.exit(f'--holdout-end must be after {zg.HOLDOUT_START}')
    if a.ref_units<=0 or a.max_open_lots<0:sys.exit('--ref-units must be positive and --max-open-lots >= 0')
    if not 0<=a.margin_buffer<=.9:sys.exit('--margin-buffer must be 0..0.9')
    if a.min_train_years<1 or not 0<=a.band_floor<=1 or not 0<=a.min_fill_rate<=1:sys.exit('--min-train-years >= 1, --band-floor and --min-fill-rate in [0, 1]')
    try:margin=parse_margin(a.margin);cs=tuple(float(x) for x in a.carry_scenarios.split(',') if x.strip())
    except ValueError as e:sys.exit(str(e))
    idx=zg.verify(a.grid,a.paths)                                                  # every grid file's hash, the computation digest and the paths file
    hol=zr.read_holidays(a.holidays) if a.holidays else []
    if sorted(hol)!=sorted(idx.get('holidays',[])):sys.exit('--holidays must be the list the grid was built with')
    with zg.Detail(a.paths) as D:lots=D.target_lots or 10;tu=D.target_units;tm=D.target_margin
    if not tm and a.margin_model=='table':print('note: the paths were not built with --target-margin: a full leg is not the full margin budget; rebuild the paths with nifty-zone-paths --target-margin 2500000 so each entry is sized by the margin table')
    try:leg_lots=parse_lots(a.leg_lots,lots)
    except ValueError as e:sys.exit(str(e))
    rep,wf,ins,gates=run(a.grid,a.paths,a.out,a.capital,a.target,a.floor,a.max_dd,a.rank,a.pool,a.min_trades,a.carry,a.top,hol,a.workers,min_days=a.min_year_days,
                         sizing=a.sizing,ref_units=a.ref_units,margin=margin,leg_lots=leg_lots,if_none=a.if_none,wf_top=a.wf_top,wf_min_trades=a.wf_min_trades,carry_scenarios=cs,buffer=a.margin_buffer,
                         max_lots=a.max_open_lots or None,return_basis=a.return_basis,cap_unit=a.lot_cap_unit,min_full_years=a.min_full_years,holdout_end=a.holdout_end,holdout_min_full_years=a.holdout_min_full_years,qualify_max=a.qualify_max,
                         margin_model=a.margin_model,calendar_margin_factor=a.calendar_margin_factor,max_margin=a.max_margin,
                         min_train_years=a.min_train_years,band=a.band_floor,min_fill_rate=a.min_fill_rate,allocation=alloc)
    print('\nCombinations passing each gate (training data of each window; every target-and-floor survivor evaluated exactly):')
    for g in gates:
        if 'note' in g:print(f"  {g['fold']:10} {g['note']}");continue
        print(f"  {g['fold']:10} {g.get('combinations',0):>12,}  target {g.get('pass_target',0):>10,}  floor {g.get('pass_floor',0):>10,}  both {g.get('pass_target_and_floor',0):>9,}  "
              f"drawdown {g.get('pass_drawdown_of_target_and_floor',0):>8,}  funds {g.get('pass_funds_of_target_and_floor',0):>8,}  lot cap {g.get('pass_lot_cap_of_target_and_floor',0):>8,}  all {g.get('pass_all',0):>7,}"
              f"  executable {g.get('executable_pass','-')} of {g.get('executable_checked','-')} replayed{'' if g.get('search_complete',True) else ' (INCOMPLETE)'}")
    print('\nWalk-forward executable account (choice made on earlier years only, adopted flat on 1 January, orders admitted at the signal close):')
    if rep.get('allocated_note'):print('  '+rep['allocated_note'])
    if rep.get('walk_forward_note'):print('  '+rep['walk_forward_note'])
    for w in wf:
        r=w.get('account_return');leg=lambda n:'none' if not w.get(f'{n}_lots') else f"{w[n+'_lots']}L {w[n+'_structure']} {w[n+'_offset']:+.0f} {w[n+'_config']} {w[n+'_profit']} {w[n+'_stop']}"
        print(f"  {w['test_year']}: {'n/a' if r is None else f'{r:.1%}'}{'' if w.get('test_annualised',True) else ' (part year, not annualised)'}  {w['decision']}"+
              (f"{' (carried positions only)' if not w['traded'] and not w.get('flat_cash_year',True) else ''}")+
              (f" | green {leg('green')} | red {leg('red')}" if w['traded'] else '')+
              (f"  [orders cut {w.get('orders_cut',0)}, rejected {w.get('orders_rejected',0)}; buffer used {w.get('buffer_breach_sessions',0)} sessions; deployable {w.get('deployable')}]")+
              (f"  [top {w['top_k']}: median {w['top_k_median_return']:.1%}, worst {w['top_k_worst_return']:.1%}]" if w.get('top_k') else ''))
    s=rep.get('walk_forward') or {}
    if s.get('returns'):
        c=s.get('cagr_fixed_size_no_withdrawals')
        print(f"  overall {s['overall_simple']:.1%} a year over {s['elapsed_years']:.2f} years (CAGR at fixed size, profits kept: {'n/a' if c is None else format(c,'.1%')}; mean of yearly figures {s['mean_of_yearly_returns']:.1%}), worst year {s['worst']:.1%}, dispersion {s.get('return_dispersion')}; "
              f"daily-close drawdown Rs {s['daily_close_drawdown_continuous']:,} continuous (largest within a year Rs {s['largest_in_year_drawdown']:,}); assumed margin peak Rs {s['assumed_margin_peak']:,}, "
              f"open lots peak {s['open_cap_lots_peak']} ({a.lot_cap_unit}; exchange lots {s.get('exchange_lots_peak')}, gross contracts {s.get('gross_contracts_peak')}); funds short {s['funds_short_sessions']}, buffer used {s['buffer_breach_sessions']} sessions; {s['no_new_entry_years']} no-new-entry, {s['flat_cash_years']} flat-cash year(s)")
        print('  futures carry sensitivity (yearly account returns): '+'; '.join(f"{float(k):.1%}: "+', '.join(f'{y} {v:.1%}' for y,v in d.items()) for k,d in s['carry_scenarios'].items()))
    if s.get('oos_verdict'):print(f"  OUT-OF-SAMPLE VERDICT: {s['oos_verdict']}"+(f" ({'; '.join(s['oos_verdict_reasons'])})" if s.get('oos_verdict_reasons') else ''))
    if ins:
        b=ins[0];sh=b.get('allocated_share');print(f"\nIn-sample best ({'passes, executable replay too' if b.get('executable_pass') else 'passes the screen only' if b['passed'] else 'closest; none pass'}), fitted to {rep['start']}..{rep['split']}: overall {b['overall']:.1%} of capital"+(f" ({b['overall']/sh:.1%} of its allocated {sh:.3f}; needs {a.target:.1%})" if sh and b.get('overall') is not None else '')+f", worst year {'n/a' if b['worst_year'] is None else format(b['worst_year'],'.1%')}, "
                       f"drawdown Rs {b['daily_close_drawdown']:,}, assumed margin peak Rs {b['assumed_margin_peak']:,} (metrics: {b.get('metrics_basis')}), green {b['green_lots']}, red {b['red_lots']}, "
                       f"screen neighbour pass share {b.get('screen_neighbour_pass_share')} (distinct: {b.get('screen_neighbour_pass_share_distinct')}, identical: {b.get('screen_neighbour_identical_share')})"+
                       (f"\n  frozen for the holdout: {a.out}/portfolio_freeze.json (run nifty-zone-holdout once)" if b.get('executable_pass') else ''))
    print(f'-> {a.out}/portfolio_walk_forward.csv, portfolio_account_daily.csv, portfolio_orders.csv, portfolio_pairs_in_sample.csv, portfolio_gate_counts.csv, portfolio_report.json')

if __name__=='__main__':main()
