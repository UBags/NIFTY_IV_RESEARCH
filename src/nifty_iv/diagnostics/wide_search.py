#!/usr/bin/env python3
"""Wide search for a stable account: every strategy the grid can test, judged by half-year (or quarter) stability of the whole
account, not by each strategy alone (V4.9.13, user: "redo the wider search").

Strategy = side x structure x strike level x signal configuration x profit rule x stop rule, for every group passing the fill-rate
gate, layered policies included. Rupees at full size (per-unit P&L x units x --scale, default 1 / the paths file's target
fraction), as a share of --capital; P&L after slippage and charges at the paths file's fills, exits at 45-minute bar closes, no
interest, no tax. A leg's P&L is booked on its exit day (trades still open at --end are valued to it), exactly as
nifty-diag-trade-structure books it.

Stage 1  candidates. Each group, all configurations x all rules at once (the grid's one-position-at-a-time sequencing and data-gap
         occupancy): the train period returns (entries and exits in [--train-start, --split)), the out-of-sample ones (exits in
         [--split, --end)), train trades and the worst open loss of a train trade (worst bar-close mark up to the exit, or the final
         result, x units). Each candidate is scored by --rank on the train periods, once on all of them and once with each train
         year left out (those years' entries, periods and trades removed); candidates whose worst trade exceeds --max-trade-loss
         even at the smallest --weights size fail; with --robust the score is the median over the configuration and its
         neighbouring swing-filter configurations (same rule). Groups with fewer than --min-trades filled train entries are
         skipped (calendars: no data before late 2024). Per configuration the --per-config best rules under every scoring are kept
         (identical trade streams once); then per scoring the --pool-per-family best of every side x structure.
Stage 2  account. The pool is recomputed with the exact engine (the stage-1 figures must agree to the last bit; checked). Greedy
         forward selection of (strategy, size) pairs, size from --weights (share of full size), maximising the account objective
         of nifty-diag-trade-structure on the train periods: worst period, minus 2 x any shortfall of the yearly return below
         --min-annual, minus 0.001 x the spread; subject to peak margin exposure (sum of size x position open, bar by bar, train
         period) <= --max-exposure, every strategy's size x worst trade loss <= --max-trade-loss (user: 9.9%; 3% until V4.9.15), and the account's
         equity drawdown (daily mark-to-market: realised legs plus open positions at each day's last bar close) <=
         --max-dd (user: 20%). Then single replacements / resizes / removals until no improvement. Up to --max-strategies.
Stage 3  exit baskets (--baskets K > 1): each chosen strategy's position split into K slices, each with its own exit pair from
         CANDIDATE_PROFITS x CANDIDATE_STOPS; same objective and limits (a position counts until its last slice exits; its worst
         open loss is bounded by the slices' average worst loss).
Checks   leave-one-year-out over the whole procedure: for each train year, the pool, the account and the baskets are chosen with
         that year removed, then that year's entries are scored. Out-of-sample periods (2025-26 was examined before: a check, not
         a clean test) are reported for every stage.

Outputs in --out: stage1/ (one file per group: resumable), candidates.csv (the pool), account.csv (selection steps),
periods.csv (period returns of each stage), loyo.csv, final.json. Searching this many strategies overfits: trust the
leave-one-year-out and out-of-sample columns, not the in-sample objective. Replay the final account through
nifty-diag-trade-ledger (conservative fills, intrabar stop) and nifty-diag-reinvest before trusting the rupees."""
import argparse,collections,csv,datetime as dt,hashlib,json,math,os,re,sqlite3,statistics as stt,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.diagnostics import trade_structure as ts
from nifty_iv.core import cli

_prange=zg._prange
_jit=zg._nb.njit if zg.HAVE_NUMBA else (lambda f:f)
_pjit=zg._nb.njit(parallel=True) if zg.HAVE_NUMBA else (lambda f:f)
RANKS=('composite','lcb','sharpe','worst')

# ------------------------------------------------------------------ periods
def period_start(label):
    """First day (ISO) of a half-year / quarter label as made by trade_structure.period_of."""
    y=int(label[:4]);k=int(label[5:])
    return dt.date(y,1 if label[4]=='H' and k==1 else 7 if label[4]=='H' else 3*(k-1)+1,1).toordinal()

class Periods:
    """Train labels on [lo, mid), out-of-sample labels on [mid, hi), as trade_structure.all_periods makes them."""
    def __init__(self,lo,mid,hi,kind):
        self.lo,self.mid,self.hi,self.kind=lo,mid,hi,kind;o=lambda d:dt.date.fromisoformat(d).toordinal()
        self.olo,self.omid,self.ohi=o(lo),o(mid),o(hi)
        self.train=ts.all_periods(lo,mid,kind);self.oos=ts.all_periods(mid,hi,kind)
        self.tstart=np.array([period_start(p) for p in self.train],np.int64);self.ostart=np.array([period_start(p) for p in self.oos],np.int64)
        self.years=sorted({p[:4] for p in self.train})
    def held_window(self,y):
        """(labels, leg window lo, hi) of a held-out train year, as trade_structure.leave_one_year_out scores it."""
        ylo=max(self.lo,f'{y}-01-01');yhi=min(self.hi,f'{int(y)+1}-01-01');lab=ts.all_periods(ylo,yhi,self.kind)
        return lab,dt.date.fromisoformat(ylo).toordinal(),dt.date.fromisoformat(yhi).toordinal()

def vec_stats(v,labels,lo,hi,kind,exclude=None):
    """trade_structure.stats() fields used by the objective, from a period-return vector aligned with labels = all_periods(lo, hi)
    (identical arithmetic)."""
    keep=[float(x) for p,x in zip(labels,v) if not (exclude and p[:4]==exclude)]
    yrs=max((dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days,1)/365.25
    if exclude:yrs-=sum(1 for p in labels if p[:4]==exclude)/(2 if kind=='half' else 4)
    yrs=max(yrs,1/365.25);sd=float(stt.pstdev(keep)) if len(keep)>1 else 0.
    return dict(periods=len(keep),annual=float(sum(keep)/yrs),sd=sd,worst=float(min(keep)) if keep else 0.,negative=int(sum(x<0 for x in keep)))

def post_tax_cagr(v,labels,keep,yrs,tax):
    """Post-tax CAGR of period-return arrays (..., periods) on capital: each calendar year's net return (kept periods) taxed at
    `tax` after setting off losses carried forward from earlier years, the after-tax yearly returns compounded over `yrs` years
    (no interest, no reinvestment of FD income; -1 if the account is wiped out)."""
    v=np.asarray(v,float);years=sorted({p[:4] for p,k in zip(labels,keep) if k})
    R=[v[...,np.array([k and p[:4]==y for p,k in zip(labels,keep)])].sum(-1) for y in years]   # each year's net return (numpy sums, as before)
    if zg.HAVE_NUMBA and R and v.ndim>1:
        g=_tax_years(np.ascontiguousarray(np.stack(R).reshape(len(R),-1)),float(tax)).reshape(v.shape[:-1])
    else:
        cf=np.zeros(v.shape[:-1]);g=np.ones(v.shape[:-1])
        for r in R:used=np.where(r>0,np.minimum(cf,r),0.);cf=np.where(r<0,cf-r,cf-used);g=g*(1+r-tax*np.maximum(r-used,0.))
    with np.errstate(invalid='ignore'):return np.where(g>0,np.power(np.maximum(g,1e-300),1/yrs)-1,-1.)

@_jit
def _tax_years(R,tax):
    """post_tax_cagr's yearly loop element by element (the same IEEE operations in the same order as the numpy expressions;
    np.minimum / np.maximum NaN semantics): the after-tax growth factor of each column of R (years, n)."""
    Y,N=R.shape;out=np.empty(N)
    for i in range(N):
        cf=0.;g=1.
        for y in range(Y):
            r=R[y,i]
            if r>0:used=cf if (cf<=r or cf!=cf) else r
            else:used=0.
            cf=cf-r if r<0 else cf-used
            x=r-used;mx=x if (x>=0. or x!=x) else 0.
            g=g*((1+r)-tax*mx)
        out[i]=g
    return out

def composite(v,labels,keep,yrs,c):
    """User objective (2026-11-24): 0.4 / max(range of half-year returns, range_floor) counted only when post-tax CAGR > target,
    + 0.4 x 7 x [post-tax CAGR > target] (or a ramp from ramp_from to target if ramp_from is set), + 0.2 x 40 x post-tax CAGR,
    - 2.8 x [range > range_cap]. Arrays (..., periods)."""
    v=np.asarray(v,float);k=np.asarray(keep,bool);cg=post_tax_cagr(v,labels,k,yrs,c['tax'])
    vk=v if k.all() else v[...,k]
    rng=(vk.max(-1)-vk.min(-1)) if k.any() else np.zeros(v.shape[:-1])
    hit=cg>c['target']
    ramp=hit*1. if c.get('ramp_from') is None else np.clip((cg-c['ramp_from'])/(c['target']-c['ramp_from']),0.,1.)
    return .4/np.maximum(rng,c['range_floor'])*hit+.4*7*ramp+.2*40*cg-2.8*(rng>c.get('range_cap',np.inf))

def fold(per,exclude=None):
    """(kept-label mask, years) of the train periods without the excluded year, as vec_stats counts them."""
    keep=np.array([not (exclude and p[:4]==exclude) for p in per.train])
    yrs=max((dt.date.fromisoformat(per.mid)-dt.date.fromisoformat(per.lo)).days,1)/365.25
    if exclude:yrs-=(~keep).sum()/(2 if per.kind=='half' else 4)
    return keep,max(yrs,1/365.25)

def objective(v,per,min_annual,exclude=None):
    c=getattr(per,'composite',None)
    if c is None:return ts.objective(vec_stats(v,per.train,per.lo,per.mid,per.kind,exclude),min_annual)
    keep,yrs=fold(per,exclude);return float(composite(np.asarray(v,float),per.train,keep,yrs,c))

# ------------------------------------------------------------------ exact engine (trade_structure.simulate + period_returns)
@_jit
def _basket_kernel(sig,pix,gend,XB,PU,XD,eord,eyear,basket,elo,ehi,excl,only,dlo,dhi,starts,cap):
    """One strategy, one basket: legs of trades entered in [elo, ehi) (entry year != excl, == only if only >= 0) exiting in
    [dlo, dhi), summed per period (starts = first day of each period) in trade and slice order. Also the trades' signal bars and
    last exit bars (margin occupancy), path indices and whether valued, and the number of valued trades."""
    K=len(basket);v=np.zeros(len(starts));busy=-1;n=0;occ_t=np.empty(len(sig),np.int64);occ_x=np.empty(len(sig),np.int64);m=0
    occ_i=np.empty(len(sig),np.int64);occ_v=np.zeros(len(sig),np.bool_)
    for j in range(len(sig)):
        t=sig[j]
        if t<=busy:continue
        i=pix[j]
        if i<0:
            if gend[j]>=0:busy=gend[j]                # data gap: the slot is held, no value
            continue
        xb=XB[basket[0],i]
        for k in range(1,K):
            if XB[basket[k],i]>xb:xb=XB[basket[k],i]
        busy=xb;e=eord[i]
        if e<elo or e>=ehi or (excl>=0 and eyear[i]==excl) or (only>=0 and eyear[i]!=only):continue
        occ_t[m]=t;occ_x[m]=xb;occ_i[m]=i;m+=1
        ok=True
        for k in range(K):
            if not math.isfinite(PU[basket[k],i]):ok=False
        if not ok:continue                            # unvalued: slot held, not counted
        occ_v[m-1]=True;n+=1
        for k in range(K):
            r=basket[k];d=XD[r,i]
            if d>=dlo and d<dhi:
                p=0
                while p+1<len(starts) and starts[p+1]<=d:p+=1
                v[p]+=(PU[r,i]/K)/cap
    return v,n,occ_t[:m],occ_x[:m],occ_i[:m],occ_v[:m]

class Engine:
    """A strategy's arrays for the exact engine (from a trade_structure.StrategyData)."""
    def __init__(self,sd,per,capital):
        self.sd=sd;self.per=per;self.cap=float(capital);self.name=sd.name
        self.sig=np.array(sd.signals,np.int64)
        self.pix=np.array([sd.index.get(t,-1) for t in sd.signals],np.int64)
        self.gend=np.array([sd.gaps.get(t,-1) if sd.index.get(t) is None else -1 for t in sd.signals],np.int64)
        self.XB=np.ascontiguousarray(sd.XB,np.int64);self.XD=np.ascontiguousarray(sd.XD,np.int64)
        units=getattr(sd,'units',np.zeros(sd.P.shape[1]))
        self.PU=np.ascontiguousarray(sd.P*np.asarray(units,float)[None,:],np.float64)   # = float(P) x units, as simulate() multiplies
        ent=getattr(sd,'entry',[]);self.eord=np.array([dt.date.fromisoformat(e).toordinal() for e in ent],np.int64)
        self.eyear=np.array([int(e[:4]) for e in ent],np.int64)
    def run(self,basket,elo,ehi,dlo,dhi,starts,excl=None,only=None):
        b=np.asarray(basket,np.int64)
        return _basket_kernel(self.sig,self.pix,self.gend,self.XB,self.PU,self.XD,self.eord,self.eyear,b,elo,ehi,int(excl) if excl else -1,
                              int(only) if only else -1,dlo,dhi,starts,self.cap)
    def train(self,basket,excl=None):
        p=self.per;return self.run(basket,p.olo,p.omid,p.olo,p.omid,p.tstart,excl)
    def oos(self,basket):
        p=self.per;return self.run(basket,p.olo,p.ohi,p.omid,p.ohi,p.ostart)
    def held(self,basket,y):
        p=self.per;lab,a,b=p.held_window(y);st=np.array([period_start(x) for x in lab],np.int64)
        return self.run(basket,p.olo,p.ohi,a,b,st,only=y)[0],lab
    def attach_risk(self,b):
        """Per candidate pair and path: exit row J, worst open loss W (share of capital: min(worst bar-close mark up to the exit,
        final value, 0) x units / capital, as strategy_scan), and the path's bar-close marks (carried over unpriced rows)."""
        sd=self.sd;P=sorted({x for x,_ in sd.pairs});S=sorted({x for _,x in sd.pairs});FP=zr.evaluate(P,b);FS=zr.evaluate(S,b)
        self.J=np.stack([np.minimum(FP[P.index(x)],FS[S.index(y)]) for x,y in sd.pairs]);ar=np.arange(b.Np)[None,:]
        cm=worst_marks(b);u=np.asarray(sd.units,float)
        self.W=(np.minimum(np.minimum(cm[ar,self.J],sd.P),0.)*u[None,:])/self.cap
        ok=b.valid&np.isfinite(b.markpu);ix=np.maximum.accumulate(np.where(ok,np.arange(b.Lmax)[None,:],0),1)
        self.mf=np.where(np.cumsum(ok,1)>0,b.markpu[np.arange(b.Np)[:,None],ix],0.);self.row_ord=b.row_ord;self.u=u
        self.sd.b=None;return self
    def risk(self,basket,excl=None):
        """(worst open loss of a trade entered in the train window, upper bound for baskets: the slices' average; daily mark-to-market
        equity over per.days of the trades entered in [lo, end)), entry year != excl. Shares of capital."""
        p=self.per;K=len(basket);_,_,_,_,oi,ov=self.run(basket,p.olo,p.ohi,p.olo,p.olo,p.tstart[:1],excl)
        days=p.days;E=np.zeros(len(days));worst=0.
        for i in oi[ov]:
            if self.eord[i]<p.omid:
                w=0.
                for r in basket:w+=self.W[r,i]
                worst=min(worst,w/K)
            for r in basket:
                j=self.J[r,i];o=self.row_ord[i,:j+1];m=self.mf[i,:j+1];a=np.searchsorted(days,o[0]);x=np.searchsorted(days,self.XD[r,i])
                if x>a:E[a:x]+=m[np.searchsorted(o,days[a:x],'right')-1]*self.u[i]/K
                E[x:]+=self.PU[r,i]/K
        return worst,E/self.cap

def worst_marks(b):
    """Running worst bar-close mark per unit of every path (inf before the first priced row), as strategy_scan uses it."""
    return np.minimum.accumulate(np.where(b.valid&np.isfinite(b.markpu),b.markpu,np.inf),1)

def drawdown(E,axis=-1):
    """Largest fall of equity from its running peak (the start counted as 0), along axis."""
    E=np.asarray(E,float);z=np.zeros(E.shape[:-1]+(1,));pk=np.maximum.accumulate(np.concatenate([z,E],-1),-1)[...,1:]
    return (pk-E).max(-1,initial=0.)

def occupancy(occ_t,occ_x,nbars):
    """Bars with a position open (entry fills in the bar after the signal, held to the last exit bar), 0/1 per bar."""
    d=np.zeros(nbars+1,np.int32);a=np.minimum(occ_t+1,nbars);b=np.maximum(a,np.minimum(occ_x+1,nbars))
    np.add.at(d,a,1);np.add.at(d,b,-1);return (np.cumsum(d)[:nbars]>0).astype(np.float32)

# ------------------------------------------------------------------ stage 1: every configuration x rule of a group
@_pjit
def _scan_kernel(ts_,cptr,cbar,pix,gx,XB,V,WL,PT,PO,EY,C,R,NT,NO,NY):
    """Per configuration, one position at a time per rule (the grid's sequencing): train and out-of-sample period sums, train
    period sums by entry year (for leave-one-year-out screening) and valued trades entered in the train window (total and by
    entry year), and the worst open loss of those trades (WL, share of capital; total and by entry year)."""
    tr=np.zeros((C,R,NT));oo=np.zeros((C,R,NO));ey=np.zeros((C,R,NT,NY));n=np.zeros((C,R),np.int32);ny=np.zeros((C,R,NY),np.int32)
    wt=np.zeros((C,R));wy=np.zeros((C,R,NY))
    for c in _prange(C):
        busy=np.full(R,-1,np.int64)
        for a in range(cptr[c],cptr[c+1]):
            q=cbar[a];pi=pix[q];t=ts_[q]
            if pi<0:
                if gx[q]>=0:
                    for r in range(R):
                        if t>busy[r]:busy[r]=gx[q]
                continue
            y=EY[pi]
            for r in range(R):
                if t>busy[r]:
                    busy[r]=XB[pi,r];v=V[pi,r]
                    if math.isfinite(v):
                        if y>=0:                                 # valued trade entered in the train window (as the engine counts)
                            n[c,r]+=1;ny[c,r,y]+=1;w_=WL[pi,r]
                            if w_<wt[c,r]:wt[c,r]=w_
                            if w_<wy[c,r,y]:wy[c,r,y]=w_
                        p=PT[pi,r]
                        if p>=0:
                            tr[c,r,p]+=v
                            if y>=0:ey[c,r,p,y]+=v
                        p=PO[pi,r]
                        if p>=0:oo[c,r,p]+=v
    return tr,oo,ey,n,ny,wt,wy

def scorings(per):
    """Scoring 0 = all train years; scoring 1+i = train year per.years[i] left out: (excluded year, kept-label mask, years)."""
    days=max((dt.date.fromisoformat(per.mid)-dt.date.fromisoformat(per.lo)).days,1)/365.25;ppy=2 if per.kind=='half' else 4;out=[]
    for y in [None]+per.years:
        keep=np.array([not (y and p[:4]==y) for p in per.train]);yrs=days-(0 if y is None else (~keep).sum()/ppy)
        out.append((y,keep,max(yrs,1/365.25)))
    return out

def score(v,n,keep,yrs,rank,min_trades,min_annual,ppy,worst=None,max_loss=np.inf,labels=None,comp=None):
    """Screening score of single strategies (arrays (..., periods) of returns on capital at full size; -inf if too few trades or
    the yearly return is below min_annual or a trade's open loss exceeded max_loss). lcb: yearly return - one yearly standard
    deviation; sharpe: mean / sd; worst: the worst period."""
    V_=v;v=v[...,keep];annual=v.sum(-1)/yrs
    with np.errstate(divide='ignore',invalid='ignore'):
        if rank=='composite':s=composite(np.asarray(V_,float),labels,keep,yrs,comp)        # sd / mean unused: not computed
        else:
            sd=v.std(-1) if v.shape[-1] else annual*0;mean=v.mean(-1) if v.shape[-1] else annual*0
            s=annual-sd*math.sqrt(ppy) if rank=='lcb' else np.where(sd>0,mean/sd,np.where(mean>0,1e6,-1e6))*math.sqrt(ppy) if rank=='sharpe' else v.min(-1)-1e-3*sd
    ok=(n>=min_trades)&(annual>=min_annual)
    if worst is not None:ok&=worst>=-max_loss
    return np.where(ok,s,-np.inf)

_CFG=re.compile(r'N(\d+)_k([\d.]+)_m(\d+)_j([\d.]+)_tw(\d+)$')
def neighbours(configs):
    """Index array (configurations, 11): each configuration first, then those differing from it in one parameter by one grid step
    (N, k, m, j, tw of the swing filter; tw for the zone-only controls), padded with -1."""
    idx={c:i for i,c in enumerate(configs)};par={}
    for c in configs:
        m=_CFG.match(c)
        if m:par[c]=('N',)+tuple(float(x) for x in m.groups())
        elif c.startswith('zone_only_tw'):par[c]=('Z',float(c[len('zone_only_tw'):]))
    grids={}
    for v in par.values():
        for d in range(1,len(v)):grids.setdefault((v[0],d),set()).add(v[d])
    grids={k:sorted(x) for k,x in grids.items()};out=np.full((len(configs),11),-1,np.int64)
    by={v:c for c,v in par.items()}
    for c,i in idx.items():
        out[i,0]=i;v=par.get(c);k=1
        if v is None:continue
        for d in range(1,len(v)):
            g=grids[(v[0],d)];j=g.index(v[d])
            for jj in (j-1,j+1):
                if 0<=jj<len(g):
                    w=v[:d]+(g[jj],)+v[d+1:]
                    if w in by and k<11:out[i,k]=idx[by[w]];k+=1
    return out

@_pjit
def _robust_kernel(sc,nb):
    """robust() for every (configuration, rule): the same order statistic as np.sort along the neighbours (NaN after +inf)."""
    C,B=sc.shape;W=nb.shape[1];out=np.empty((C,B))
    for c in _prange(C):
        cnt=0
        for k in range(W):
            if nb[c,k]>=0:cnt+=1
        kk=cnt//2;buf=np.empty(W)
        for b in range(B):
            s=sc[c,b]
            if not math.isfinite(s):out[c,b]=-np.inf;continue
            n=0
            for k in range(W):
                j=nb[c,k]
                if j>=0:buf[n]=sc[j,b];n+=1
            for q in range(n,W):buf[q]=np.inf                                         # padding, as robust's np.inf
            for q in range(1,W):                                                       # insertion sort; NaN greatest (np.sort order)
                x=buf[q];i=q-1
                while i>=0 and (buf[i]>x or (buf[i]!=buf[i] and x==x)):buf[i+1]=buf[i];i-=1
                buf[i+1]=x
            out[c,b]=buf[kk]
    return out

def robust(sc,nb):
    """Median score over each configuration and its neighbours (the upper middle value for an even count, never an average with a
    failing one; unscored neighbours ignored, failing ones count as -inf); -inf where the configuration itself fails."""
    if zg.HAVE_NUMBA:return _robust_kernel(np.ascontiguousarray(sc,np.float64),np.ascontiguousarray(nb,np.int64))
    return _robust_numpy(sc,nb)

def _robust_numpy(sc,nb):
    g=np.sort(np.where(nb[:,:,None]>=0,sc[np.maximum(nb,0)],np.inf),1)                # (configurations, 11, rules); padding sorts last
    k=((nb>=0).sum(1)//2)[:,None,None]                                                 # upper middle of the present values
    m=np.take_along_axis(g,np.broadcast_to(k,(g.shape[0],1,g.shape[2])),1)[:,0,:]
    return np.where(np.isfinite(sc),m,-np.inf)

def _group_name(g):return f'{g[0]}_{g[1]}_{int(g[2]):+d}'

def scan_group(g):
    """Stage 1 for one group: the --per-config best rules of every configuration under every scoring. Runs in a worker."""
    c=zg._CTX;side,st,off=g;t0=time.time();per=c['per'];ph=collections.Counter();tq=time.perf_counter()
    def lap(k):
        nonlocal tq;t=time.perf_counter();ph[k]+=t-tq;tq=t
    if zg._nb is not None:zg._nb.set_num_threads(max(1,min(zg._nb.config.NUMBA_NUM_THREADS,(os.cpu_count() or 1)//max(1,c['workers']))))
    db=sqlite3.connect(f'file:{c["paths"]}?mode=ro',uri=True);stat=zg.fill_stats(db,side,st,off)
    if not zg.testable(stat,c['min_fill_rate']):db.close();return dict(group=g,skipped=f"fill rate {stat.get('fill_rate')}",rows=[])
    paths=zg.load_paths(db,side,st,off);filled=[p for p in paths.values() if p]       # as zone_grid.Detail.group (the exact engine's source)
    gaps=zg.gap_occupancy(db,side,st,off,c['gap_policy']);db.close()
    if not filled:return dict(group=g,skipped='no trades',rows=[])
    ntr=sum(per.lo<=p['entry_day']<per.mid for p in filled)
    if ntr<c['min_trades']:return dict(group=g,skipped=f'{ntr} filled entries in the train window',rows=[])
    lap('load');b=zr.Batch(filled,c['bars'],st);h=per.ohi;ar=np.arange(b.Np)[None,:];cm=worst_marks(b);lap('batch')
    configs=c['configs'];nb=neighbours(configs) if c['robust'] else None;by_bar=collections.defaultdict(list)
    # configurations with the same signal list have identical results: the kernel and the scores run once per distinct list (ui:
    # the configurations computed; cinv[ci]: the distinct list of configuration ci); robust() and the selection stay per configuration
    uid={};ui=[];cinv=np.empty(len(configs),np.int64)
    for ci,cf in enumerate(configs):
        k=tuple(c['signals'].get((cf,side),()))
        if k not in uid:uid[k]=len(ui);ui.append(ci)
        cinv[ci]=uid[k]
    for u,ci in enumerate(ui):
        for t in c['signals'].get((configs[ci],side),()):by_bar[t].append(u)
    ts_=np.array(sorted(by_bar),np.int64);sizes=np.array([len(by_bar[t]) for t in ts_],np.int64)
    cfg=np.concatenate([np.asarray(by_bar[t],np.int64) for t in ts_]) if len(ts_) else np.zeros(0,np.int64)
    bar_of=np.repeat(np.arange(len(ts_)),sizes);order=np.argsort(cfg,kind='stable');cbar=bar_of[order].astype(np.int64)
    C=len(configs);CU=len(ui);cptr=np.zeros(CU+1,np.int64);np.add.at(cptr,cfg+1,1);cptr=np.cumsum(cptr)
    idx={p['signal_bar']:i for i,p in enumerate(filled)};pix=np.array([idx.get(int(t),-1) for t in ts_],np.int64)
    gx=np.array([gaps.get(int(t),-1) if idx.get(int(t)) is None else -1 for t in ts_],np.int64)
    units=b.units*c['scale'];RV=b.row_value(h)
    eord=np.array([dt.date.fromisoformat(p['entry_day']).toordinal() for p in filled],np.int64)
    yi={int(y):i for i,y in enumerate(per.years)}
    EY=np.array([yi.get(int(p['entry_day'][:4]),-1) if per.olo<=eo<per.omid else -1 for p,eo in zip(filled,eord)],np.int64)
    ent_tr=(eord>=per.olo)&(eord<per.omid);ent_all=(eord>=per.olo)&(eord<per.ohi)
    S_=scorings(per);NS=len(S_);NT,NO,NY=len(per.train),len(per.oos),len(per.years);ppy=2 if per.kind=='half' else 4
    K=c['per_config'];best=[[[] for _ in range(C)] for _ in range(NS)];thr=np.full((NS,C),-np.inf)
    P=zr.profit_rules(st);chunks=zg.stop_chunks(zr.stop_rules(st)+(zr.compound_rules(st) if c['compound'] else []))
    lap('setup');PC=[x for x in zr.compound_profits(st) if x in P];pc_idx=np.array([P.index(x) for x in PC]);FP=zr.evaluate(P,b);rules_seen=0;lap('evaluate')
    block=max(16,int(c['mem']//max(1,C*(NT*(NY+1)+NO+2*NY+3)*8*(2 if c['robust'] else 1))))   # unchanged: the block size can decide ties in the selection
    loss=c['max_trade_loss']/c['min_weight']                                          # a candidate may enter at the smallest size
    for kind,S in chunks.items():
        Pk=PC if kind.startswith('compound') else P;FPk=FP[pc_idx] if kind.startswith('compound') else FP
        lap('setup');FS=zr.evaluate(S,b);lap('evaluate');J=np.minimum(FPk[:,None,:],FS[None,:,:]).reshape(len(Pk)*len(S),b.Np);rules_seen+=len(J)
        first,inv=zg._dedup_rows(J);J=J[first];lap('dedup')
        XB=np.ascontiguousarray(b.xb[ar,J].T,np.int64);Pv=RV[ar,J].T;XD=np.minimum(b.xd[ar,J],h).T
        V=np.ascontiguousarray((Pv*units[:,None])/c['capital'])                      # = (P x units / 1) / capital, as the engine
        WL=np.ascontiguousarray((np.minimum(np.minimum(cm[ar,J].T,Pv),0.)*units[:,None])/c['capital'])   # worst open loss, as Engine.W
        PT=np.where(ent_tr[:,None]&(XD>=per.olo)&(XD<per.omid),np.searchsorted(per.tstart,XD,'right')-1,-1).astype(np.int64)
        PO=np.where(ent_all[:,None]&(XD>=per.omid)&(XD<per.ohi),np.searchsorted(per.ostart,XD,'right')-1,-1).astype(np.int64)
        lap('gather')
        for b0 in range(0,len(first),block):
            sl=slice(b0,min(b0+block,len(first)));B=sl.stop-sl.start
            tr,oo,ey,n,ny,wt,wy=_scan_kernel(ts_,cptr,cbar,pix,gx,np.ascontiguousarray(XB[:,sl]),np.ascontiguousarray(V[:,sl]),np.ascontiguousarray(WL[:,sl]),
                                             np.ascontiguousarray(PT[:,sl]),np.ascontiguousarray(PO[:,sl]),EY,CU,B,NT,NO,NY)
            lap('kernel');scu=np.empty((NS,CU,B));sc=np.empty((NS,C,B))
            if NS>1:                                                                  # once per block, not once per left-out year
                eys=ey.sum(-1);z=np.zeros(wy.shape[:-1]+(1,))
                pre=np.minimum.accumulate(np.concatenate([z,wy],-1),-1)               # pre[..., j] = min(0, wy[..., :j])
                suf=np.minimum.accumulate(np.concatenate([wy,z],-1)[...,::-1],-1)[...,::-1]   # suf[..., j] = min(0, wy[..., j:])
            for si,(y,keep,yrs) in enumerate(S_):
                if y is None:v,nn,ww=tr,n,wt
                else:j=per.years.index(y);v=eys-ey[...,j];nn=n-ny[...,j];ww=np.minimum(pre[...,j],suf[...,j+1])   # = np.delete(wy, j).min(initial=0)
                scu[si]=score(v,nn,keep,yrs,c['rank'],c['min_trades'],c['cand_min_annual'],ppy,ww,loss,per.train,c.get('comp'))
                sc[si]=scu[si][cinv]                                                    # every configuration's scores
                if nb is not None:sc[si]=robust(sc[si],nb)
            lap("score");ph["unique_rules"]+=B
            for si in range(NS):
                cc,uu=np.nonzero(sc[si]>thr[si][:,None])
                for ci in np.unique(cc):
                    us=uu[cc==ci];us=us[np.argsort(-sc[si,ci,us],kind='stable')][:4*K];L=best[si][ci]
                    for u in us:
                        s_=float(sc[si,ci,u])
                        if len(L)>=K and s_<=L[-1][0]:break
                        cu=cinv[ci];key=tr[cu,u].tobytes()+oo[cu,u].tobytes()
                        if any(k==key for _,k,_ in L):continue
                        r=int(first[b0+u]);rec=dict(config=configs[ci],profit=Pk[r//len(S)],stop=S[r%len(S)],trades=int(n[cu,u]),worst_trade=float(wt[cu,u]),
                                                    train=tr[cu,u].tolist(),oos=oo[cu,u].tolist(),scores=[float(x) for x in sc[:,ci,u]])
                        L.append((s_,key,rec));L.sort(key=lambda x:-x[0]);del L[K:]
                    if len(L)>=K:thr[si,ci]=L[-1][0]
            lap('select')
    rows={}
    for si in range(NS):
        for L in best[si]:
            for _,key,rec in L:rows.setdefault((rec['config'],key),rec)
    out=[dict(side=side,structure=st,level=int(off),**r) for r in rows.values()]
    return dict(group=g,rows=out,seconds=round(time.time()-t0,1),fill_rate=stat.get('fill_rate'),paths=len(filled),rules=rules_seen,configs=C,distinct_signal_lists=CU,
                phases={k:round(v,2) for k,v in ph.items() if k!='unique_rules'},unique_rules=int(ph['unique_rules']))

# ------------------------------------------------------------------ stage 2: the account
def strategy_key(r):return (r['side'],r['structure'],float(r['level']),r['config'],r['profit'],r['stop'])
def key_name(k):return f'{k[0]} {k[1]} {int(k[2]):+d} {k[3]} {k[4]}/{k[5]}'

def account_vec(chosen,V):
    a=np.zeros(next(iter(V.values())).shape if V else 0)
    for c,w in chosen:a=a+w*V[c]
    return a

def build_account(cands,V,OCC,per,a,excl=None,log=None,name=str,WORST=None,EQ=None,dmask=None):
    """Greedy forward selection of (candidate, weight), then single replacements / resizes / removals. V: {candidate: train vector
    (excl applied)}; OCC: {candidate: 0/1 open position over the train bars}; WORST: {candidate: worst open loss of a trade at full
    size}; EQ: {candidate: daily mark-to-market equity}, account drawdown measured on the days in dmask. Feasible: peak exposure <=
    --max-exposure, weight x worst trade loss <= --max-trade-loss, account drawdown <= --max-dd. Returns (chosen, objective, steps)."""
    W=a.weights;obj=lambda v:objective(v,per,a.min_annual,excl);ids=list(cands)
    if not ids:return [],obj(np.zeros(len(per.train))),[]
    M=np.stack([OCC[c] for c in ids]);risk=EQ is not None
    if risk:Q=np.stack([EQ[c][dmask] for c in ids]);WS=np.array([WORST[c] for c in ids])
    chosen=[];steps=[]
    def state(ch):
        A=account_vec(ch,V);E=sum((w*OCC[c] for c,w in ch),np.zeros(M.shape[1],np.float32))
        Qa=sum((w*EQ[c][dmask] for c,w in ch),np.zeros(int(dmask.sum()))) if risk else None
        return A,E,Qa
    def feasible(ch):
        A,E,Qa=state(ch)
        if E.max(initial=0)>a.max_exposure+1e-9:return False
        if risk and (any(w*WORST[c]<-a.max_trade_loss-1e-12 for c,w in ch) or drawdown(Qa)>a.max_dd+1e-12):return False
        return True
    def trials(ch,skip):
        A,E,Qa=state(ch);best=(-np.inf,None)
        for w in W:
            ok=(E[None,:]+w*M).max(1)<=a.max_exposure+1e-9
            if risk:
                ok&=w*WS>=-a.max_trade_loss-1e-12
                if ok.any():ok[ok]=drawdown(Qa[None,:]+w*Q[ok])<=a.max_dd+1e-12
            for j in np.flatnonzero(ok):
                cid=ids[j]
                if cid in skip:continue
                v=obj(A+w*V[cid])
                if v>best[0]:best=(v,(cid,w))
        return best
    cur=obj(np.zeros(len(per.train)))
    while len(chosen)<a.max_strategies:
        v,pick=trials(chosen,{c for c,_ in chosen})
        if pick is None or v<=cur+1e-12:break
        chosen.append(pick);cur=v;steps.append(dict(step=len(steps)+1,action='add',strategy=pick[0],weight=pick[1],objective=v))
        if log:log(f'    + {name(pick[0])} x{pick[1]:g}: objective {v:.4f}')
    for rnd in range(a.rounds):                                                     # single replacements / resizes / removals
        improved=False
        for j in range(len(chosen)):
            rest=chosen[:j]+chosen[j+1:];v,pick=trials(rest,{c for c,_ in rest});old=chosen[j]
            drop=obj(account_vec(rest,V)) if feasible(rest) else -np.inf
            if drop>cur+1e-12 and (pick is None or drop>=v):
                chosen=rest;cur=drop;improved=True;steps.append(dict(step=len(steps)+1,action='drop',strategy=old[0],weight=old[1],objective=drop))
                if log:log(f'    - {name(old[0])} x{old[1]:g}: objective {drop:.4f}')
                break
            if pick is not None and v>cur+1e-12:
                chosen=rest+[pick];cur=v;improved=True
                steps.append(dict(step=len(steps)+1,action='replace' if pick[0]!=old[0] else 'resize',strategy=pick[0],weight=pick[1],objective=v,replaced=old[0]))
                if log:log(f'    ~ {name(old[0])} x{old[1]:g} -> {name(pick[0])} x{pick[1]:g}: objective {v:.4f}')
                break
        if not improved:break
    return chosen,cur,steps

# ------------------------------------------------------------------ stage 3: exit baskets on the chosen strategies
def basket_search(items,K,per,a,excl=None,log=None,dmask=None):
    """items: [(Engine with candidate pairs and risk arrays attached, weight)]; per strategy a list of K candidate indices maximising
    the account objective (trade_structure.search's procedure: greedy forward fill, then single-slice swaps, strategies in turn),
    under the exposure, trade-loss (slices' average) and account-drawdown limits."""
    cache={};nb=a.ntrain;risk=all(hasattr(E,'W') for E,_ in items)
    if risk and dmask is None:dmask=per.days<per.omid
    def run(i,b):
        k=(i,tuple(sorted(b)),excl)
        if k not in cache:
            E=items[i][0];v,_,ot,ox,_,_=E.train(list(k[1]),excl)
            wq=E.risk(list(k[1]),excl) if risk else (0.,None);cache[k]=(v,occupancy(ot,ox,a.nbars)[:nb],wq[0],wq[1])
        return cache[k]
    def score(bk):
        A=np.zeros(len(per.train));X=np.zeros(nb,np.float32);Q=np.zeros(len(per.days)) if risk else None
        for i,(E,w) in enumerate(items):
            v,o,wst,q=run(i,bk[i]);A=A+w*v;X=X+w*o
            if risk:
                if w*wst<-a.max_trade_loss-1e-12:return -np.inf
                Q=Q+w*q
        if X.max(initial=0)>a.max_exposure+1e-9 or (risk and drawdown(Q[dmask])>a.max_dd+1e-12):return -np.inf
        return objective(A,per,a.min_annual,excl)
    bk=[[E.sd.ref]*K for E,_ in items];best=score(bk)
    for rnd in range(a.rounds):
        improved=False
        for i,(E,w) in enumerate(items):
            R=len(E.sd.pairs)
            if rnd==0:
                cur=[]
                for k in range(K):
                    cands=[]
                    for r in range(R):
                        trial=cur+[r];full=trial+[trial[-1]]*(K-len(trial));cands.append((score(bk[:i]+[full]+bk[i+1:]),r))
                    cur.append(max(cands)[1])
                v=score(bk[:i]+[cur]+bk[i+1:])
                if v>best:bk[i]=cur;best=v;improved=True
            while True:
                top=(best,None)
                for k in range(K):
                    for r in range(R):
                        if r==bk[i][k]:continue
                        trial=list(bk[i]);trial[k]=r;v=score(bk[:i]+[trial]+bk[i+1:])
                        if v>top[0]+1e-12:top=(v,trial)
                if top[1] is None:break
                best,bk[i]=top[0],top[1];improved=True
            if log:log(f'    round {rnd+1}, {key_name(E.sd.key)} x{w:g}: {[E.sd.pairs[r] for r in bk[i]]} objective {best:.4f}')
        if not improved:break
    return bk,best

# ------------------------------------------------------------------ driver
def _imap(fn,xs,w):
    if w<=1 or len(xs)<=1:
        for x in xs:yield fn(x)
        return
    import multiprocessing as mp
    with mp.get_context('fork').Pool(min(w,len(xs)),maxtasksperchild=2) as pool:yield from pool.imap_unordered(fn,xs,chunksize=1)

def _signature(a,paths,scale,hol,gp):
    """Settings and code a stage-1 group file depends on (a file with another signature is recomputed)."""
    import nifty_iv;st=Path(paths).stat();h=hashlib.sha256()
    for f in (__file__,zr.__file__,zg.__file__):h.update(Path(f).read_bytes())
    return dict(paths=str(Path(paths).resolve()),size=st.st_size,mtime_ns=st.st_mtime_ns,holidays=hashlib.sha256('|'.join(hol).encode()).hexdigest(),gap_policy=gp,
                period=a.period,train_start=a.train_start,split=a.split,end=a.end,capital=a.capital,scale=scale,rank=a.rank,min_trades=a.min_trades,
                cand_min_annual=a.cand_min_annual,per_config=a.per_config,compound=not a.no_compound,min_fill_rate=a.min_fill_rate,code=h.hexdigest(),
                max_trade_loss=a.max_trade_loss,min_weight=min(a.weights),robust=a.robust,comp=_comp(a))

def _comp(a):
    return dict(tax=a.tax,target=a.cagr_target,ramp_from=a.ramp_from,range_floor=a.range_floor,range_cap=a.range_cap) if a.objective=='composite' else None

def _stats_line(v,labels,lo,hi,kind,tax=.35):
    s=vec_stats(v,labels,lo,hi,kind);yrs=max((dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days,1)/365.25;v=np.asarray(v,float)
    cg=float(post_tax_cagr(v,labels,np.ones(len(labels),bool),yrs,tax)) if len(v) else 0.;rg=float(v.max()-v.min()) if len(v) else 0.
    return f"{100*s['annual']:6.1f}%/yr  post-tax CAGR {100*cg:5.1f}%  range {100*rg:5.1f}%  sd {100*s['sd']:5.1f}%  worst {100*s['worst']:6.1f}%  negative {s['negative']}/{s['periods']}"

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True,help='paths file (nifty-zone-paths)')
    ap.add_argument('--holidays',help='exchange holiday list (weekend exits), as the grid uses');ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--structures',help='limit to these structures (comma separated)')
    ap.add_argument('--groups',help='limit to these groups: side:structure:level, comma separated (e.g. bull:naked:-1,bear:vert_w1:2; profiling)');ap.add_argument('--no-compound',action='store_true',help='skip the layered policies')
    ap.add_argument('--min-fill-rate',type=float,default=zg.MIN_FILL_RATE,help='fill-rate gate per group (default %(default)s)')
    ap.add_argument('--capital',type=float,default=2.5e6,help='base of every return (default 25 lakh)')
    ap.add_argument('--scale',type=float,help="rupee multiplier (default 1 / the paths file's --target-fraction: full size)")
    ap.add_argument('--train-start',default='2021-09-01',help='first entry day of the search period');ap.add_argument('--split',default='2026-01-01',help='entries before this day are searched on (user: 2025 included; default %(default)s)')
    ap.add_argument('--objective',choices=('composite','floor'),default='composite',help="composite (user): 0.4/max(half-year range, --range-floor) if post-tax CAGR > --cagr-target, + 2.8 if post-tax CAGR > --cagr-target, + 8 x post-tax CAGR, - 2.8 if the range > --range-cap; floor: worst period with --min-annual shortfall penalty (default %(default)s)")
    ap.add_argument('--tax',type=float,default=.35,help='tax on each year\'s net gain after loss set-off (default %(default)s)')
    ap.add_argument('--cagr-target',type=float,default=.20,help='post-tax CAGR target (default %(default)s)');ap.add_argument('--ramp-from',type=float,help='optional: replace the 0 / 1 target term by a ramp from this post-tax CAGR to --cagr-target (default: off, user: binary)')
    ap.add_argument('--range-floor',type=float,default=.05,help='smallest half-year range the spread term divides by (default %(default)s)')
    ap.add_argument('--range-cap',type=float,default=.40,help='half-year range (best - worst) above which the objective loses 2.8 (default %(default)s)')
    ap.add_argument('--end',default='2026-07-01',help='entries before this day; trades still open are valued to it')
    ap.add_argument('--period',choices=('half','quarter'),default='half',help='period of the objective and the screening scores')
    ap.add_argument('--rank',choices=RANKS,default='composite',help='stage-1 screening score: composite = the --objective composite on the single strategy at full size (default); lcb = yearly return - one yearly sd; sharpe; worst = worst period')
    ap.add_argument('--min-trades',type=int,default=8,help='stage 1: train trades a candidate needs (default %(default)s)')
    ap.add_argument('--cand-min-annual',type=float,default=.08,help='stage 1: yearly train return a candidate needs at full size (default %(default)s)')
    ap.add_argument('--per-config',type=int,default=2,help='stage 1: best rules kept per configuration and scoring (default %(default)s)')
    ap.add_argument('--robust',action=argparse.BooleanOptionalAction,default=True,help='stage 1: rank each configuration by the median score of it and its neighbouring configurations (one swing-filter parameter one step away), same rule (default on)')
    ap.add_argument('--pool-per-family',type=int,default=40,help='stage 2: best candidates per side x structure, per scoring (default %(default)s)')
    ap.add_argument('--max-trade-loss',type=float,default=.099,help='worst open loss of any trade x its size, share of --capital (user rule, was 0.03 until V4.9.15; default %(default)s); stage 1 keeps candidates within it at the smallest --weights size')
    ap.add_argument('--max-dd',type=float,default=.20,help='account equity drawdown (daily mark-to-market, train period), share of --capital (default %(default)s)')
    ap.add_argument('--min-annual',type=float,default=.35,help='account objective: yearly return required (shortfall penalised), share of --capital (default %(default)s)')
    ap.add_argument('--weights',default='0.25,0.5,0.75,1',help='position sizes a strategy may take, share of full size (default %(default)s)')
    ap.add_argument('--max-strategies',type=int,default=10,help='strategies in the account at most (default %(default)s)')
    ap.add_argument('--max-exposure',type=float,default=1.,help='peak sum of size x position open over the train bars (1 = one full-size margin; default %(default)s)')
    ap.add_argument('--baskets',type=int,default=5,help='stage 3: slices per position, each with its own exit (0 or 1 = skip; default %(default)s)')
    ap.add_argument('--profits',help='stage 3 candidate profit rules (default trade_structure.CANDIDATE_PROFITS)');ap.add_argument('--stops',help='stage 3 candidate stop rules')
    ap.add_argument('--rounds',type=int,default=3,help='refinement rounds of stages 2 and 3 (default %(default)s)')
    ap.add_argument('--loyo',action=argparse.BooleanOptionalAction,default=True,help='leave-one-year-out check of the whole procedure (default on)')
    ap.add_argument('--stage1-only',action='store_true',help='stop after stage 1 (rerun without it to continue from the stored groups)')
    ap.add_argument('--mem',type=float,default=4e8,help='stage-1 working memory per worker for the per-rule sums, bytes (default %(default)g)')
    ap.add_argument('--workers',type=int,default=4,help='stage-1 groups in parallel (default %(default)s)');ap.add_argument('--out',default='wide_search',help='output folder')
    a=ap.parse_args(argv);t0=time.time();out=Path(a.out);(out/'stage1').mkdir(parents=True,exist_ok=True)
    a.weights=sorted({float(x) for x in a.weights.split(',')})
    if not a.weights or min(a.weights)<=0:sys.exit('--weights: positive numbers')
    hol=zr.read_holidays(a.holidays) if a.holidays else [];gp=a.gap_policy;per=Periods(a.train_start,a.split,a.end,a.period)
    if a.split>zg.HOLDOUT_START:print(f'note: --split {a.split} is after {zg.HOLDOUT_START}: the out-of-sample periods are shorter')
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True)
    try:meta_args=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
    except Exception:meta_args={}
    scale=a.scale or 1/float(meta_args.get('target_fraction') or 1.);bars=zr.BarArrays.from_db(db,hol)
    signals=collections.defaultdict(list)
    for cf,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[cf,side].append(t)
    groups=db.execute('SELECT DISTINCT side,structure,offset FROM paths ORDER BY 2,1,3').fetchall();db.close()
    if a.structures:groups=[g for g in groups if g[1] in a.structures.split(',')]
    if a.groups:
        want={(x.split(':')[0],x.split(':')[1],float(x.split(':')[2])) for x in a.groups.split(',') if x}
        groups=[g for g in groups if (g[0],g[1],float(g[2])) in want]
        if not groups:sys.exit(f'--groups: none of {sorted(want)} is in the paths file')
    sig=_signature(a,a.paths,scale,hol,gp);configs=sorted({cf for cf,_ in signals})
    zg._CTX.clear();zg._CTX.update(paths=a.paths,bars=bars,signals=dict(signals),configs=configs,per=per,scale=scale,capital=a.capital,rank=a.rank,
        min_trades=a.min_trades,cand_min_annual=a.cand_min_annual,per_config=a.per_config,compound=not a.no_compound,min_fill_rate=a.min_fill_rate,
        gap_policy=gp,workers=a.workers,mem=a.mem,robust=a.robust,max_trade_loss=a.max_trade_loss,min_weight=min(a.weights),comp=_comp(a))
    if a.rank=='composite' and a.objective!='composite':sys.exit('--rank composite needs --objective composite')
    per.composite=_comp(a)
    per.days=np.array(sorted({o for o in bars.ord if per.olo<=o<per.ohi}),np.int64);per.ntd=int((per.days<per.omid).sum())
    per.dyear=np.array([dt.date.fromordinal(int(o)).year for o in per.days])
    print(f'{len(groups)} groups, {len(configs)} configurations; train periods {per.train[0]}..{per.train[-1]} ({len(per.train)}), out of sample {per.oos[0] if per.oos else "-"}..{per.oos[-1] if per.oos else "-"}; '
          f'scale {scale:g}, capital Rs {a.capital:,.0f}',flush=True)
    # ---- stage 1 (resumable, one file per group)
    import gzip
    fn=lambda g:out/'stage1'/f'{_group_name(g)}.json.gz'
    def load(g):
        try:
            with gzip.open(fn(g),'rt') as fh:d=json.load(fh)
            return d['result'] if d.get('signature')==sig else None
        except (OSError,ValueError,KeyError):return None
    todo=[g for g in groups if load(g) is None]
    print(f'stage 1: {len(groups)-len(todo)} groups already done, {len(todo)} to scan',flush=True)
    for i,r in enumerate(_imap(scan_group,todo,a.workers),1):
        r=dict(r,group=list(r['group']));tmp=fn(r['group']).with_suffix('.tmp')
        with gzip.open(tmp,'wt') as fh:json.dump(dict(signature=sig,result=r),fh)
        os.replace(tmp,fn(r['group']));g=r['group']
        msg=r.get('skipped') or f"{len(r['rows'])} candidates from {r['configs']} configurations x {r['rules']} rules ({r['seconds']}s)"
        print(f'  {i}/{len(todo)} {g[0]} {g[1]} {int(g[2]):+d}: {msg}; total {time.time()-t0:.0f}s',flush=True)
    res=[load(g) for g in groups];rows=[x for r in res if r for x in r['rows']]
    skipped=[f"{r['group'][0]} {r['group'][1]} {int(r['group'][2]):+d} ({r['skipped']})" for r in res if r and r.get('skipped')]
    if skipped:print('skipped: '+'; '.join(skipped))
    print(f'stage 1: {len(rows):,} candidates kept ({time.time()-t0:.0f}s)',flush=True)
    if a.stage1_only or not rows:return
    # ---- pool per scoring: the best of every side x structure
    S_=scorings(per);pools=[];fam=collections.defaultdict(list)
    for i,r in enumerate(rows):fam[(r['side'],r['structure'])].append(i)
    for si in range(len(S_)):
        P=[]
        for f,ids in sorted(fam.items()):
            seen=set();n_=0
            for i in sorted(ids,key=lambda i:-rows[i]['scores'][si]):
                if n_>=a.pool_per_family or not math.isfinite(rows[i]['scores'][si]):break
                k=(tuple(rows[i]['train']),tuple(rows[i]['oos']),rows[i]['trades'])
                if k in seen:continue
                seen.add(k);P.append(i);n_+=1
        pools.append(P)
    union=sorted({i for P in pools for i in P},key=lambda i:(rows[i]['side'],rows[i]['structure'],rows[i]['level']))
    cnt=collections.Counter((rows[i]['side'],rows[i]['structure']) for i in pools[0])
    print(f'pool: {len(pools[0])} candidates on all train years ('+', '.join(f'{s_} {t}: {n}' for (s_,t),n in sorted(cnt.items()))+f'), {len(union)} across the {len(S_)} scorings; recomputing them exactly ...',flush=True)
    # ---- exact engine on the pool, with risk figures per fold
    a.nbars=len(bars.day);a.ntrain=int(sum(1 for d in bars.day if d<a.split))
    tmask=per.days<per.omid;dm={None:tmask,**{y:tmask&(per.dyear!=int(y)) for y in per.years}}
    EX={};mism=0
    with zg.Detail(a.paths,hol,gp) as D:
        for i in union:
            r=rows[i];sd=ts.StrategyData(D,strategy_key(r),[],a.end,scale,features=False);E=Engine(sd,per,a.capital);bt=sd.b;b=[sd.ref]
            vt,nt,ot,ox,_,_=E.train(b);vo,no,qt,qx,_,_=E.oos(b);E.attach_risk(bt)
            rk={y:E.risk(b,y) for y in [None]+per.years}
            if list(vt)!=list(r['train']) or list(vo)!=list(r['oos']) or nt!=r['trades'] or rk[None][0]!=r['worst_trade']:mism+=1
            occ=occupancy(ot,ox,a.nbars);occ_all=occupancy(qt,qx,a.nbars)
            EX[i]=dict(train=vt,oos=vo,n=nt,occ=occ[:a.ntrain],occ_oos=occ_all[a.ntrain:],excl={y:E.train(b,y)[0] for y in per.years},held={y:E.held(b,y)[0] for y in per.years},
                       worst={y:w for y,(w,_) in rk.items()},eq={y:q for y,(_,q) in rk.items()})
            del E,sd,bt
    print(f'  {len(union)} recomputed; stage-1 figures differ from the exact engine for {mism} ({time.time()-t0:.0f}s)',flush=True)
    if mism:print('  WARNING: stage-1 and exact figures should be identical; the screening may be off for those candidates')
    OCC={i:EX[i]['occ'] for i in union}
    fold=lambda y:dict(WORST={i:EX[i]['worst'][y] for i in union},EQ={i:EX[i]['eq'][y] for i in union},dmask=dm[y])
    # ---- stage 2
    tgt=f'2.8 x ramp({a.ramp_from:.0%}..{a.cagr_target:.0%})' if a.ramp_from is not None else f'2.8 if post-tax CAGR > {a.cagr_target:.0%}'
    od=(f'0.4/max(range,{a.range_floor:g}) if post-tax CAGR > {a.cagr_target:.0%} + {tgt} + 8 x post-tax CAGR - 2.8 if range > {a.range_cap:.0%}, tax {a.tax:.0%}' if per.composite else f'worst {a.period}, yearly return >= {a.min_annual:.0%} of capital')
    print(f'\nstage 2: account on all train years (objective: {od}; sizes {a.weights}; peak exposure <= {a.max_exposure:g}; '
          f'trade loss x size <= {a.max_trade_loss:.1%}; account drawdown <= {a.max_dd:.0%})',flush=True)
    nm=lambda c:key_name(strategy_key(rows[c]))
    chosen,obj,steps=build_account(pools[0],{i:EX[i]['train'] for i in union},OCC,per,a,None,log=lambda m:print(m,flush=True),name=nm,**fold(None))
    loyo=[]
    if a.loyo:
        print('\nleave-one-year-out: pool, account (and baskets) chosen without the year (its trades and days left out of the risk limits too), then the year scored',flush=True)
        for si,(y,_,_) in enumerate(S_[1:],1):
            ch,_,_=build_account(pools[si],{i:EX[i]['excl'][y] for i in union},OCC,per,a,y,**fold(y))
            lab=per.held_window(y)[0];held=account_vec(ch,{i:EX[i]['held'][y] for i in union}) if ch else np.zeros(len(lab))
            loyo.append(dict(year=y,labels=lab,single=[float(x) for x in held],strategies=[[key_name(strategy_key(rows[c])),w] for c,w in ch],chosen=ch))
            print(f"  {y}: {len(ch)} strategies, held-out {per.kind}s {[f'{100*x:.1f}%' for x in held]}",flush=True)
    # ---- stage 3
    K=a.baskets;pairs=[(p,s_) for p in (a.profits.split(',') if a.profits else ts.CANDIDATE_PROFITS) for s_ in (a.stops.split(',') if a.stops else ts.CANDIDATE_STOPS)]
    bk=None;items=[]
    def make_items(ch,D):
        out=[]
        for c,w in ch:
            sd=ts.StrategyData(D,strategy_key(rows[c]),pairs,a.end,scale,features=False);bt=sd.b;out.append((Engine(sd,per,a.capital).attach_risk(bt),w))
        return out
    if K>1 and chosen:
        print(f'\nstage 3: exit baskets, {K} slices per position, {len(pairs)} candidate exit pairs',flush=True)
        with zg.Detail(a.paths,hol,gp) as D:
            items=make_items(chosen,D);bk,bobj=basket_search(items,K,per,a,None,log=lambda m:print(m,flush=True),dmask=dm[None])
            if a.loyo:
                for L in loyo:
                    if not L['chosen']:L['basket']=[0.]*len(L['labels']);continue
                    it=make_items(L['chosen'],D);b_=basket_search(it,K,per,a,L['year'],dmask=dm[L['year']])[0]
                    h=np.zeros(len(L['labels']))
                    for (E,w),bb in zip(it,b_):h=h+w*E.held(bb,L['year'])[0]
                    L['basket']=[float(x) for x in h];print(f"  held-out {L['year']} with baskets: {[f'{100*x:.1f}%' for x in h]}",flush=True)
    # ---- report
    def acc(ch,which):return account_vec(ch,{i:EX[i][which] for i in union}) if ch else np.zeros(len(per.train if which=='train' else per.oos))
    def peak(ch,which):return float(sum((w*EX[c][which] for c,w in ch),np.zeros(len(EX[ch[0][0]][which]) if ch else 0,np.float32)).max(initial=0))
    def dds(Q):return drawdown(Q[:per.ntd]),(drawdown(Q[per.ntd:]-(Q[per.ntd-1] if per.ntd else 0.)) if per.ntd<len(Q) else 0.)
    tv,ov=acc(chosen,'train'),acc(chosen,'oos');res_rows=[('single exits',tv,ov,dds(sum((w*EX[c]['eq'][None] for c,w in chosen),np.zeros(len(per.days)))))]
    if bk:
        tb=np.zeros(len(per.train));ob=np.zeros(len(per.oos));Qb=np.zeros(len(per.days))
        for (E,w),b_ in zip(items,bk):tb=tb+w*E.train(b_)[0];ob=ob+w*E.oos(b_)[0];Qb=Qb+w*E.risk(b_)[1]
        res_rows.append((f'exit baskets ({K})',tb,ob,dds(Qb)))
    print(f'\nchosen account ({len(chosen)} strategies; peak exposure train {peak(chosen,"occ"):.2f}, out of sample {peak(chosen,"occ_oos"):.2f}):')
    for j,(c,w) in enumerate(chosen):
        r=rows[c];e=EX[c];print(f'  {w:>4g} x {key_name(strategy_key(r))}: worst trade {100*w*e["worst"][None]:.1f}% (full size {100*e["worst"][None]:.1f}%); train {_stats_line(e["train"],per.train,per.lo,per.mid,per.kind)} | oos {100*vec_stats(e["oos"],per.oos,per.mid,per.hi,per.kind)["annual"]:.1f}%/yr'
                                + (f'  baskets: {[items[j][0].sd.pairs[x] for x in bk[j]]}' if bk else ''))
    print(f'\nreturns on Rs {a.capital:,.0f}, pre-tax, before interest; train = entries {per.lo}..{per.mid}, out of sample = exits {per.mid}..{per.hi}')
    for name,t_,o_,(dt_,do_) in res_rows:
        print(f'  {name:<18} train {_stats_line(t_,per.train,per.lo,per.mid,per.kind)}  max DD {100*dt_:.1f}% | oos {_stats_line(o_,per.oos,per.mid,per.hi,per.kind) if per.oos else "-"}  max DD {100*do_:.1f}%')
        print('    '+'  '.join(f'{p} {100*x:.1f}%' for p,x in zip(per.train+per.oos,list(t_)+list(o_))))
    if loyo:
        hs=[x for L in loyo for x in L['single']];print(f"  leave-one-year-out held-out {per.kind}s, single exits: negative {sum(x<0 for x in hs)}/{len(hs)}, worst {100*min(hs):.1f}%, mean {100*stt.mean(hs):.1f}%")
        if bk:hb=[x for L in loyo for x in L.get('basket',[])];print(f"  leave-one-year-out held-out {per.kind}s, baskets: negative {sum(x<0 for x in hb)}/{len(hb)}, worst {100*min(hb):.1f}%, mean {100*stt.mean(hb):.1f}%") if hb else None
    # ---- files
    inpool={i:[si for si,P in enumerate(pools) if i in P] for i in union};labs=['all']+[f'without_{y}' for y in per.years]
    with open(out/'candidates.csv','w',newline='') as fh:
        w=csv.writer(fh);w.writerow(['side','structure','level','config','profit','stop','trades','worst_trade','train_max_dd','pools']+[f'score_{l}' for l in labs]+[f'train_{p}' for p in per.train]+[f'oos_{p}' for p in per.oos])
        for i in union:
            r=rows[i];w.writerow([r['side'],r['structure'],r['level'],r['config'],r['profit'],r['stop'],r['trades'],round(EX[i]['worst'][None],6),round(float(drawdown(EX[i]['eq'][None][:per.ntd])),6),' '.join(labs[s] for s in inpool[i])]+
                                 [round(x,6) for x in r['scores']]+[round(float(x),6) for x in EX[i]['train']]+[round(float(x),6) for x in EX[i]['oos']])
    with open(out/'account.csv','w',newline='') as fh:
        w=csv.writer(fh);w.writerow(['step','action','strategy','weight','objective','replaced'])
        for s in steps:w.writerow([s['step'],s['action'],key_name(strategy_key(rows[s['strategy']])),s['weight'],round(s['objective'],6),key_name(strategy_key(rows[s['replaced']])) if s.get('replaced') is not None else ''])
    with open(out/'periods.csv','w',newline='') as fh:
        w=csv.writer(fh);w.writerow(['period','set']+[x[0] for x in res_rows])
        for j,p in enumerate(per.train):w.writerow([p,'train']+[round(float(x[1][j]),6) for x in res_rows])
        for j,p in enumerate(per.oos):w.writerow([p,'out_of_sample']+[round(float(x[2][j]),6) for x in res_rows])
    with open(out/'loyo.csv','w',newline='') as fh:
        w=csv.writer(fh);w.writerow(['year','period','single_exits','baskets','strategies'])
        for L in loyo:
            for j,p in enumerate(L['labels']):w.writerow([L['year'],p,round(L['single'][j],6),round(L['basket'][j],6) if 'basket' in L else '','; '.join(f'{x} x{v:g}' for x,v in L['strategies'])])
    final=dict(settings={k:v for k,v in vars(a).items() if k not in ('nbars','ntrain')},scale=scale,objective_train=obj,
               strategies=[dict(strategy=key_name(strategy_key(rows[c])),key=list(strategy_key(rows[c])),weight=w,worst_trade_full_size=EX[c]['worst'][None],baskets=[list(items[j][0].sd.pairs[x]) for x in bk[j]] if bk else None) for j,(c,w) in enumerate(chosen)],
               periods={x[0]:dict(train=dict(zip(per.train,map(float,x[1]))),oos=dict(zip(per.oos,map(float,x[2]))),max_dd_train=float(x[3][0]),max_dd_oos=float(x[3][1])) for x in res_rows},
               loyo=[{k:v for k,v in L.items() if k!='chosen'} for L in loyo])
    (out/'final.json').write_text(json.dumps(final,indent=1,default=str))
    print(f'-> {out}/ (candidates.csv, account.csv, periods.csv, loyo.csv, final.json) ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
