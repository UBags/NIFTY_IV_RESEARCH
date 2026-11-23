#!/usr/bin/env python3
"""V3 exact complete-grid portfolio MTM with purged expanding walk-forward.

All eligible threshold/config combinations get full daily equity (no cheap P&L
prescreen). Final holdout is evaluated ONLY after training selections are frozen.
DSR is a diagnostic with disclosed approximate effective-trial count.
"""
from __future__ import annotations
import argparse,datetime as dt,sqlite3,math,json,csv,statistics,hashlib,os,shutil
from collections import defaultdict,Counter
from itertools import combinations
from pathlib import Path
from bisect import bisect_left,bisect_right
import numpy as np
from scipy.special import ndtr,ndtri
from nifty_iv.ivcal.stats import bootstrap_summary,spa_test
from nifty_iv.core import lineage as lineage_mod
from nifty_iv.ivcal.candidate_core import CAUSAL_FILL_MODELS,STRESS_FILL_MODELS
from nifty_iv.core import cli

TENORS=(5,15,30,45,60,75,90,120)
STRATEGY_FIELDS=('mode','round_step','offset','long_tenor','hedge','policy','structure','dte_bucket','long_strike_offset')
SIGNALS=('iv_ratio','forward_iv_ratio','iv_to_rv','iv_to_rv_parkinson','skew_slope',
  'iv_to_rv_yz','iv_to_rv_1m_range_proxy','iv_to_rv_1m_closeclose','gap_share_5d','bf25_short','var_carry_daily','iv_ratio_biz','iv_to_rv_yz_biz','spot_to_sma50','spot_to_sma100','spot_to_sma200','trend_21d','pct_below_52w_high') + tuple(f'iv{a}/iv{b}' for a,b in combinations(TENORS,2))+('annual_variance_curvature_5_30_90','total_variance_curvature_5_30_90','fwd_iv_to_rv_yz')

# Features with no natural scale near 1: thresholds come from training quantiles, not the ratio grid.
QUANTILE_SIGNALS={'annual_variance_curvature_5_30_90','total_variance_curvature_5_30_90','skew_slope','gap_share_5d','bf25_short','var_carry_daily','spot_to_sma50','spot_to_sma100','spot_to_sma200','trend_21d','pct_below_52w_high'}
OPEN_END=1e18   # finite so it survives JSON and JSONB
OPEN_AT_CUTOFF='9999-12-31'   # exit_day of a trade still open when the training sample ends


def quantile_bounds(values,qlo,qhi):
    """Numeric (low,high) for a quantile interval of these values; the tails stay open."""
    if not values:return None
    low=-OPEN_END if qlo<=0 else float(np.quantile(values,qlo))
    high=OPEN_END if qhi>=1 else float(np.quantile(values,qhi))
    return (low,high) if low<high else None


def plateau(rule,rule_sharpe,lows,highs,quantiles):
    """Training Sharpe of the grid cells one step away. A lone peak among weak neighbours looks like a fitted cliff."""
    signal,a,b=rule
    axis_a,axis_b=([0.]+sorted(quantiles),sorted(quantiles)+[1.]) if signal in QUANTILE_SIGNALS else (lows,highs)
    if a not in axis_a or b not in axis_b:return None
    i,j=axis_a.index(a),axis_b.index(b);cells=[]
    if i>0:cells.append((axis_a[i-1],b))
    if i+1<len(axis_a):cells.append((axis_a[i+1],b))
    if j>0:cells.append((a,axis_b[j-1]))
    if j+1<len(axis_b):cells.append((a,axis_b[j+1]))
    vals=[rule_sharpe[(signal,x,y)] for x,y in cells if x<y and rule_sharpe.get((signal,x,y)) is not None]
    return dict(neighbours_scored=len(vals),median_sharpe=float(np.median(vals)) if vals else None,min_sharpe=min(vals) if vals else None,positive_share=sum(v>0 for v in vals)/len(vals) if vals else None)


def signal_grid(signal,values,lows,highs,quantiles):
    """[(rule key, low, high)]. Quantile keys hold probabilities, so each fold can resolve them from its own past."""
    if signal not in QUANTILE_SIGNALS:return [((signal,l,h),l,h) for l in lows for h in highs if h>l]
    edges=[0.]+sorted(quantiles)+[1.];finite=[v for v in values if v is not None and math.isfinite(v)];out=[]
    for i,qlo in enumerate(edges):
        for qhi in edges[i+1:]:
            if (qlo,qhi)==(0.,1.):continue   # the unfiltered baseline
            b=quantile_bounds(finite,qlo,qhi)
            if b:out.append(((signal,qlo,qhi),b[0],b[1]))
    return out


def term_curvature(row):
    """Annualized variance: (30-day level minus its 5-to-90 interpolation) / 30-day level. A front-week bump against a parallel shift."""
    a,b,c=row.get('iv5'),row.get('iv30'),row.get('iv90')
    if not a or not b or not c:return None
    interp=a*a+(c*c-a*a)*(30-5)/(90-5)
    return (b*b-interp)/(b*b)


def total_variance_curvature(row):
    """Total variance T*IV^2: same interpolation on the quantity that adds over time. Can take the opposite sign to term_curvature."""
    a,b,c=row.get('iv5'),row.get('iv30'),row.get('iv90')
    if not a or not b or not c:return None
    w5,w30,w90=5*a*a,30*b*b,90*c*c
    return (w30-(w5+(w90-w5)*(30-5)/(90-5)))/w30


def fwd_iv_to_rv_yz(row):
    """Forward IV over Yang-Zhang RV: (short IV / RV) / (short IV / forward IV)."""
    x,y=row.get('iv_to_rv_yz'),row.get('forward_iv_ratio')
    return x/y if x is not None and y else None


DERIVED_SIGNALS={'annual_variance_curvature_5_30_90':term_curvature,'total_variance_curvature_5_30_90':total_variance_curvature,'fwd_iv_to_rv_yz':fwd_iv_to_rv_yz}


def ratio(row,signal):
    if signal in DERIVED_SIGNALS:return DERIVED_SIGNALS[signal](row)
    if '/' in signal:
        a,b=signal.split('/');x=row.get(a);y=row.get(b)
        return x/y if x is not None and y is not None and y>0 else None
    return row.get(signal)


EXIT_FIELD=['scheduled_exit_day']   # which date decides that a trade finished inside a sample; set from --train-censor


def done_by(row):
    return row.get(EXIT_FIELD[0],row['exit_day'])


def choose(rows,max_open,max_capital=None,presorted=False):
    chosen=[];active=[]
    for row in (rows if presorted else sorted(rows,key=lambda x:(x['day'],x['id']))):
        active=[r for r in active if r['exit_day']>=row['day']]
        if len(active)>=max_open:continue
        # Entry-time risk estimate, not future maximum. This is a capital SCENARIO.
        if max_capital is not None and sum(x['entry_capital'] for x in active)+row['entry_capital']>max_capital:continue
        chosen.append(row);active.append(row)
    return chosen


def max_drawdown(equity):
    high=0.;draw=0.
    for val in equity:
        high=max(high,val);draw=max(draw,high-val)
    return draw


def portfolio(rows,marks,session_days,max_open,max_capital,mark_stress='base'):
    """Exact selected-path daily mark aggregation, preserving flat sessions."""
    chosen=choose(rows,max_open,max_capital)
    if not chosen:return dict(n=0,net=0,sharpe=None,max_dd=None,days=len(session_days),selected=[],
                               capital_scenario_peak=0,mark_missing=0,capital_breaches=0)
    pnl={x['id']:marks.get(x['id'],{}) for x in chosen}
    dates=list(session_days)   # the caller's fixed window, flat days at both ends included
    equity=[];cap_peak=0.;capital_breaches=0;missing=0
    for date in dates:
        total=0.;cap=0.
        for tr in chosen:
            if tr['day']>date:continue
            if date>tr['exit_day']:
                total+=tr['pnl_inr']
            else:
                m=pnl[tr['id']].get(date)
                if m is None:
                    missing+=1
                    # Mark omitted by data outage: carry LAST AVAILABLE mark (never future).
                    older=[x for x in pnl[tr['id']] if x<date]
                    m=pnl[tr['id']][max(older)] if older else None
                if m is None:raise ValueError('No entry-day MTM for '+tr['id'])
                total+=m[mark_stress]
                cap+=m['capital']
        equity.append(total);cap_peak=max(cap_peak,cap)
        if max_capital is not None and cap>max_capital:capital_breaches+=1
    increments=[equity[0]]+[equity[i]-equity[i-1] for i in range(1,len(equity))]
    sharpe=None
    if len(increments)>=5 and statistics.stdev(increments)>0:
        sharpe=statistics.mean(increments)/statistics.stdev(increments)*math.sqrt(252)
    active=[v for d,v in zip(dates,increments) if any(z['day']<=d<=z['exit_day'] for z in chosen)]
    active_sharpe=None
    if len(active)>=5 and statistics.stdev(active)>0:
        active_sharpe=statistics.mean(active)/statistics.stdev(active)*math.sqrt(252)
    # Stress scenarios must report the stressed end value, not the base realized sum.
    net=sum(x['pnl_inr'] for x in chosen) if mark_stress=='base' else equity[-1]
    return dict(n=len(chosen),net=net,sharpe=sharpe,max_dd=max_drawdown(equity),
                win_pct=100*sum(x['pnl_inr']>0 for x in chosen)/len(chosen),
                days=len(dates),active_days=len(active),time_in_market=len(active)/len(dates),active_day_sharpe=active_sharpe,
                selected=[x['id'] for x in chosen],capital_scenario_peak=cap_peak,
                capital_breaches=capital_breaches,mark_missing=missing,
                worst_daily_increment=min(increments),daily_increments=increments,
                equity_end=equity[-1],entry_capital_peak=max(
                  (sum(z['entry_capital'] for z in chosen if z['day']<=d<=z['exit_day']) for d in dates),default=0))


def annualized(increments):
    x=np.asarray(increments,dtype=float)
    sd=x.std(ddof=1) if len(x)>1 else 0.
    return float(x.mean()/sd*math.sqrt(252)) if len(x)>=5 and sd>1e-12 else None


class GroupMatrix:
    """Dense daily-mark matrices for one strategy group; replaces per-subset Python replay.

    Equity LEVEL per trade per session: 0 before entry, mark (or last earlier mark)
    while open, realized P&L after exit. Same admission and flat-day semantics as portfolio().
    """
    def __init__(self,rows,marks,days):
        self.days=list(days);self.index={r['id']:i for i,r in enumerate(rows)}
        M,D=len(rows),len(self.days)
        self.level={s:np.zeros((M,D)) for s in ('base','lower','upper')}
        self.capital=np.zeros((M,D));self.active=np.zeros((M,D),dtype=bool)
        self.missing=np.zeros((M,D),dtype=np.int32);self.broken=np.zeros(M,dtype=bool)
        self.pnl=np.array([r['pnl_inr'] for r in rows],dtype=float)
        self.entry_capital=np.array([r['entry_capital'] for r in rows],dtype=float)
        for i,r in enumerate(rows):
            path=marks.get(r['id'],{});last=None
            first=bisect_left(self.days,r['day']);end=bisect_right(self.days,r['exit_day'])
            for d in sorted(x for x in path if x<r['day']):last=path[d]
            for j in range(first,end):
                m=path.get(self.days[j])
                if m is None:
                    self.missing[i,j]=1;m=last
                    if m is None:self.broken[i]=True;continue
                last=m
                self.level['base'][i,j]=m['base'];self.level['lower'][i,j]=m['lower'];self.level['upper'][i,j]=m['upper']
                self.capital[i,j]=m['capital'];self.active[i,j]=True
            for s in self.level:self.level[s][i,end:]=self.pnl[i]

    def evaluate(self,chosen,max_capital=None,mark_stress='base',scale=None,window=None):
        """chosen comes from choose(); output mirrors portfolio().

        scale: per-row weights aligned to the constructor's rows (see basis_weights).
        window: (first,last-exclusive) session indices, default every session. The caller fixes it,
        so a rule that rarely trades is measured over the same calendar as one that always does.
        sharpe is calendar-time; active_day_sharpe covers only days with a position open.
        """
        if not chosen:return dict(n=0,net=0,sharpe=None,max_dd=None,days=len(self.days),selected=[],active_days=0,time_in_market=0.,
                                  active_day_sharpe=None,window_mtm=0.,capital_scenario_peak=0,mark_missing=0,capital_breaches=0)
        ids=np.array([self.index[x['id']] for x in chosen])
        if self.broken[ids].any():raise ValueError('No entry-day MTM for a selected trade')
        s,e=window or (0,len(self.days))
        w=np.ones(len(ids)) if scale is None else scale[ids]
        equity=(self.level[mark_stress][ids][:,s:e]*w[:,None]).sum(axis=0)
        capital=self.capital[ids][:,s:e].sum(axis=0)
        scaled_capital=(self.capital[ids][:,s:e]*w[:,None]).sum(axis=0)
        before=float((self.level[mark_stress][ids][:,s-1]*w).sum()) if s>0 else 0.   # P&L carried into the window
        increments=np.diff(equity,prepend=before)
        sharpe=annualized(increments)
        open_days=self.active[ids][:,s:e].any(axis=0)
        peak=np.maximum(np.maximum.accumulate(equity),before)
        pnl=self.level['base'][ids][:,-1]*w   # value at this matrix's last session: a trade still open then is its mark, never its later outcome
        net=float(pnl.sum()) if mark_stress=='base' else float(equity[-1])
        return dict(n=len(chosen),net=net,sharpe=sharpe,max_dd=float((peak-equity).max()),
            win_pct=100*float((pnl>0).sum())/len(chosen),days=len(equity),active_days=int(open_days.sum()),
            time_in_market=float(open_days.mean()),active_day_sharpe=annualized(increments[open_days]),
            window_mtm=float(equity[-1]-before),selected=[x['id'] for x in chosen],
            capital_scenario_peak=float(scaled_capital.max()),
            capital_breaches=int((capital>max_capital).sum()) if max_capital is not None else 0,
            mark_missing=int(self.missing[ids][:,s:e].sum()),worst_daily_increment=float(increments.min()),
            daily_increments=increments.tolist(),equity_end=float(equity[-1]),
            entry_capital_peak=float((self.active[ids][:,s:e]*self.entry_capital[ids][:,None]).sum(axis=0).max()))

    def series(self,chosen,mark_stress='base',scale=None):
        """Portfolio equity, open-position flag and scaled capital over every session: the shared input of fast_window."""
        if not chosen:
            flat=np.zeros(len(self.days));return flat,np.zeros(len(self.days),dtype=bool),flat
        ids=np.array([self.index[x['id']] for x in chosen])
        if self.broken[ids].any():raise ValueError('No entry-day MTM for a selected trade')
        w=np.ones(len(ids)) if scale is None else scale[ids]
        return (self.level[mark_stress][ids].T@w,self.active[ids].any(axis=0),self.capital[ids].T@w)

    def daily_pnl(self,chosen,mark_stress='base',scale=None):
        """Daily MTM change over every session in the matrix."""
        ids=np.array([self.index[x['id']] for x in chosen])
        w=np.ones(len(ids)) if scale is None else scale[ids]
        return np.diff((self.level[mark_stress][ids]*w[:,None]).sum(axis=0),prepend=0.)

    def daily_return(self,chosen,mark_stress='base',scale=None,denominator=None):
        """Daily MTM over ALL sessions as a return on mean deployed scenario capital; None if never deployed."""
        if not chosen:return None
        ids=np.array([self.index[x['id']] for x in chosen])
        w=np.ones(len(ids)) if scale is None else scale[ids]
        capital=(self.capital[ids]*w[:,None]).sum(axis=0)
        deployed=capital[capital>0]
        if not len(deployed):return None
        return self.daily_pnl(chosen,mark_stress,scale)/float(denominator if denominator else deployed.mean())

    def mean_deployed(self,chosen,scale=None):
        ids=np.array([self.index[x['id']] for x in chosen])
        w=np.ones(len(ids)) if scale is None else scale[ids]
        capital=(self.capital[ids]*w[:,None]).sum(axis=0)
        return float(capital[capital>0].mean()) if (capital>0).any() else None


def fast_window(equity,open_days,s,e):
    """Window metrics matching GroupMatrix.evaluate, for the hot loops: no selected-id copies per fold."""
    before=float(equity[s-1]) if s>0 else 0.
    seg=equity[s:e]
    if not len(seg):return dict(window_mtm=0.,sharpe=None,active_day_sharpe=None,time_in_market=0.,max_dd=0.)
    inc=np.empty(len(seg));inc[0]=seg[0]-before;inc[1:]=seg[1:]-seg[:-1]
    peak=np.maximum(np.maximum.accumulate(seg),before);on=open_days[s:e]
    return dict(window_mtm=float(seg[-1]-before),sharpe=annualized(inc),active_day_sharpe=annualized(inc[on]),time_in_market=float(on.mean()),max_dd=float((peak-seg).max()))


BASES=('raw','per_unit','entry_capital_normalized','risk_normalized_score')
def basis_weights(rows,basis):
    """raw = cash; per_unit = per index unit, removing lot-size eras; entry_capital_normalized = per rupee of entry
    scenario capital. These rescale finished trades for comparison; none of them is an executed sizing rule."""
    if basis=='raw':return np.ones(len(rows))
    if basis=='per_unit':return np.array([1./r['lot_units'] for r in rows],dtype=float)
    if basis=='entry_capital_normalized':return np.array([1./r['entry_capital'] for r in rows],dtype=float)
    if basis=='risk_normalized_score':return np.array([1./(r['lot_units']*r['entry_risk_pts']) for r in rows],dtype=float)   # one expected short-leg move of risk, known at entry. Counterfactual reweighting of billed trades: no integer lots, charges or capital checks follow it
    raise ValueError(basis)


def inner_score(m):
    return m['sharpe']-.25*m['max_dd']/max(1e-12,m['capital_scenario_peak'])


def nested_folds(rules,matrix,folds,embargo_days,cutoff,max_open,max_capital,stress,scale,min_past,quantile_rules=frozenset()):
    """Select one threshold rule per fold from earlier, fully exited, embargoed trades; freeze; score the fold.

    rules maps (signal,low,high) to chronological qualifying rows. A fold with no eligible
    rule, or whose frozen rule trades nothing, stays in the record as flat.
    """
    out=[];rules={k:sorted(v,key=lambda x:(x['day'],x['id'])) for k,v in rules.items()}
    for begin,end in folds:
        purge=(dt.date.fromisoformat(begin)-dt.timedelta(days=embargo_days)).isoformat()
        best=None;seen=set()
        for rule in sorted(rules):
            past=[x for x in rules[rule] if x['day']<purge and done_by(x)<purge]
            if rule in quantile_rules:   # thresholds from this fold's own past, never from later trades
                bounds=quantile_bounds([ratio(x,rule[0]) for x in past],rule[1],rule[2])
                if not bounds:continue
                past=[x for x in past if bounds[0]<=ratio(x,rule[0])<bounds[1]]
            past_end=bisect_left(matrix.days,purge)
            if len(past)<min_past:continue
            chosen=choose(past,max_open,max_capital,presorted=True)
            mask=tuple(x['id'] for x in chosen)
            if len(chosen)<min_past or mask in seen:continue
            seen.add(mask)
            equity,on,capital=matrix.series(chosen,stress,scale)
            m=fast_window(equity,on,0,past_end)
            if m['sharpe'] is None:continue
            m['capital_scenario_peak']=float(capital[:past_end].max()) if past_end else 0.
            s=inner_score(m)
            if best is None or s>best[0]:best=(s,rule)
        if best is None:
            out.append(dict(begin=begin,end=end,rule=None,inner_score=None,n=0,entry_cohort_lifecycle_net=0.,chosen=[],candidates=[]));continue
        rule=best[1]
        candidates=[x for x in rules[rule] if begin<=x['day']<=end and done_by(x)<cutoff]
        resolved=None
        if rule in quantile_rules:
            resolved=quantile_bounds([ratio(x,rule[0]) for x in rules[rule] if x['day']<purge and done_by(x)<purge],rule[1],rule[2])
            candidates=[x for x in candidates if resolved[0]<=ratio(x,rule[0])<resolved[1]]
        trades=choose(candidates,max_open,max_capital)
        lifecycle=matrix.evaluate(trades,max_capital,stress,scale)['net'] if trades else 0.
        out.append(dict(begin=begin,end=end,rule=rule,resolved_bounds=resolved,inner_score=best[0],n=len(trades),entry_cohort_lifecycle_net=lifecycle,chosen=trades,candidates=candidates))
    return out


def fold_window_stats(matrix,chosen,begin,end,max_capital,stress,scale,series=None):
    """One portfolio's MTM clipped to a fold's sessions, including trades carried in from earlier."""
    window=(bisect_left(matrix.days,begin),bisect_right(matrix.days,end))
    equity,on,_=series or matrix.series(chosen,stress,scale)
    m=fast_window(equity,on,*window)
    return dict(begin=begin,end=end,entries=sum(begin<=x['day']<=end for x in chosen),
        carried_in=sum(x['day']<begin<=x['exit_day'] for x in chosen),window_mtm=m['window_mtm'],sharpe=m['sharpe'],
        active_day_sharpe=m['active_day_sharpe'],time_in_market=m['time_in_market'],max_dd=m['max_dd'])


def persistent_nested_portfolio(fold_results,matrix,max_open,max_capital,stress,scale):
    """Walk the folds with one portfolio: a position opened in one fold occupies its slot and capital in the next.

    Each fold's frozen rule supplies candidates; choose() admits them chronologically across all folds.
    """
    if not fold_results:return None
    pool=[x for f in fold_results for x in f['candidates']]
    admitted=choose(pool,max_open,max_capital)
    first=bisect_left(matrix.days,fold_results[0]['begin'])
    m=matrix.evaluate(admitted,max_capital,stress,scale,window=(first,len(matrix.days)))
    return dict(trades=len(admitted),candidates=len(pool),window_mtm=m['window_mtm'],sharpe=m['sharpe'],active_day_sharpe=m['active_day_sharpe'],
        time_in_market=m['time_in_market'],max_dd=m['max_dd'],days=m['days'],capital_breaches=m['capital_breaches'],admitted=[x['id'] for x in admitted],
        folds=[fold_window_stats(matrix,admitted,f['begin'],f['end'],max_capital,stress,scale) for f in fold_results])


def circular_shift_control(rows,values,grid,matrix,max_open,max_capital,stress,scale,min_train,max_shifts):
    """Is the best threshold in this signal better than the same search on a time-shifted signal?

    Shifting keeps the signal's own persistence and every trade path, and breaks only their alignment.
    Returns a p-value that accounts for the threshold search inside one signal and group.
    """
    n=len(rows)
    days=sorted({r['day'] for r in rows});day_at={d:i for i,d in enumerate(days)};by_day=[[] for _ in days];rank_in_day=[]
    for r,v in zip(rows,values):
        rank_in_day.append(len(by_day[day_at[r['day']]]));by_day[day_at[r['day']]].append(v)
    def shifted(step):   # every trade on a day moves together, by whole candidate days
        return [by_day[(day_at[r['day']]+step)%len(days)][k%len(by_day[(day_at[r['day']]+step)%len(days)])] for r,k in zip(rows,rank_in_day)]
    def best(vals):
        top=None;seen=set()
        for low,high in grid:
            chosen=choose([r for r,v in zip(rows,vals) if v is not None and math.isfinite(v) and low<=v<high],max_open,max_capital)
            mask=tuple(x['id'] for x in chosen)
            if len(chosen)<min_train or mask in seen:continue
            seen.add(mask);s=matrix.evaluate(chosen,max_capital,stress,scale)['sharpe']
            if s is not None and (top is None or s>top):top=s
        return top
    actual=best(values)
    if actual is None or len(days)<20:return None
    D=len(days)
    shifts=range(1,D) if D-1<=max_shifts else [D//2] if max_shifts==1 else [1+round(i*(D-2)/(max_shifts-1)) for i in range(max_shifts)]
    null=[b for s in shifts if (b:=best(shifted(s))) is not None]
    return dict(best_sharpe=actual,shifts=len(null),p_value=(1+sum(b>=actual for b in null))/(1+len(null)),note='Serial alignment test; small shift counts give coarse p-values') if null else None


def spa_matrix(models):
    """Per-model daily vectors to the (days,models) shape spa_test requires."""
    return np.column_stack(models)


def load_group_marks(db,group):
    marks=defaultdict(dict)
    for i in range(0,len(group),350):
        ids=[r['id'] for r in group[i:i+350]]
        if not ids:continue
        for tid,d,v,lo,hi,cap in db.execute(
            'SELECT trade_id,day,pnl_inr,lower,upper,capital_scenario FROM marks WHERE trade_id IN ('+
            ','.join('?'*len(ids))+')',ids):
            marks[tid][d]=dict(base=v,lower=lo,upper=hi,capital=cap)
    return marks


def effective_dsr(daily_increments,annualized_sharpe,n_eff,sr_dispersion_annualized):
    """Diagnostic-only approximate DSR; N_eff inferred from distinct trade masks.

    Not a rigorous multiple-testing correction for correlated searches.
    """
    if annualized_sharpe is None or len(daily_increments)<20 or n_eff<2:return None
    mean=statistics.mean(daily_increments);sd=statistics.stdev(daily_increments)
    if sd==0:return None
    n=len(daily_increments);z=[(v-mean)/sd for v in daily_increments]
    skew=sum(v**3 for v in z)/n;kurt=sum(v**4 for v in z)/n
    sr=annualized_sharpe/math.sqrt(252)
    sd_sr=max(1e-8,sr_dispersion_annualized/math.sqrt(252))
    gamma=0.5772156649
    n_eff=max(2,n_eff)
    expected=sd_sr*((1-gamma)*float(ndtri(1-1/n_eff))+gamma*float(ndtri(1-1/(n_eff*math.e))))
    denom=1-skew*sr+max(0,(kurt-1))*sr*sr/4
    if denom<=0:return None
    return float(ndtr((sr-expected)*math.sqrt(n-1)/math.sqrt(denom)))


def regimes(daily):
    out={};completed=[]
    for row in daily:
        d=row['day']
        if len(completed)>=21:
            trend=completed[-1]['close']/completed[-21]['close']-1
            recent=[math.log(completed[i]['close']/completed[i-1]['close']) for i in range(len(completed)-19,len(completed))]
            rv=statistics.stdev(recent)*math.sqrt(252)
            out[d]=('up' if trend>.02 else 'down' if trend<-.02 else 'sideways')+'_'+('highRV' if rv>.2 else 'lowRV')
        else:out[d]='warmup'
        if row['complete_session']:completed.append(row)
    return out


def fold_ranges(days,n_folds):
    """Validation folds occur chronologically after a minimum training warm-up."""
    if len(days)<n_folds*8:return []
    start=max(int(len(days)*.35),15);tail=days[start:]
    size=max(1,len(tail)//n_folds)
    return [(tail[i*size],tail[(i+1)*size-1] if i<n_folds-1 else tail[-1])
            for i in range(n_folds) if i*size<len(tail)]


def compact(m):
    return {k:(round(v,5) if isinstance(v,float) else v) for k,v in m.items() if k not in ('selected','daily_increments')}


def valuation_sessions(daily):
    """Every day with market data. A partial session can still carry the worst mark of an open trade."""
    return [x['day'] for x in daily if x['minute_bars']>=1]


def validate_args(a):
    """Settings that crash later or silently mean nothing."""
    bad=[]
    for name,low in (('max_open',1),('folds',2),('embargo_days',0),('min_train',1),('min_holdout',1),('min_positive_folds',0),('top',1),('max_capital',0),('shift_control_max',0),('bootstrap_reps',0),('bootstrap_block',1),('spa_max_models',1)):
        if getattr(a,name)<low:bad.append(f'--{name.replace("_","-")} must be at least {low}')
    if any(float(x)<=0 for x in a.capital_multipliers.split(',')):bad.append('--capital-multipliers must be positive')
    return bad


def prune_finalists(finalists,cap):
    """Keep the best `cap` in score order. Stable, so ties resolve as a full sort would."""
    finalists.sort(key=lambda x:(x['score'],x['train']['n']),reverse=True);del finalists[cap:]


def record_holdout_inspection(trades_path,split,cutoff,lineage_block,digest=None):
    """Written before any holdout number exists, so an interrupted run still counts. Returns how many came before."""
    digest=digest or lineage_mod.trades_digest(trades_path);prior=prior_inspections(trades_path,split,digest);path=audit_log_path();path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:f.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),split=split,holdout_entry_cutoff=cutoff,release=lineage_mod.RELEASE,trades=str(trades_path),trades_sha256=digest,run_id=lineage_block.get('run_id')))+'\n')
    return prior


def train_rows_of(group,ids):
    keep=set(ids);return [r for r in group if r['id'] in keep]


def fixed_holdout_days(sessions,split):
    """One calendar for every strategy: the split to the data end. Trades, chosen or not, cannot move it."""
    return [d for d in sessions if d>=split]


def freeze_payload(shortlist,trades_sha256,args,identity=None):
    """What train_only committed to. final_holdout must recompute exactly this before any holdout number exists."""
    rules=[[x['key'],x['signal'],x['rule_key'],[round(float(x['lower']),10),round(float(x['upper']),10)]] for x in shortlist]
    pinned={k:v for k,v in sorted(args.items()) if k not in ('out','trades','cache','research_mode','freeze_file','allow_reinspection')}
    body=dict(trades_sha256=trades_sha256,args=pinned,rules=rules,identity=identity or {})
    return dict(body,digest=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest())


def run_identity(trade_meta,sessions):
    """Everything besides trades and settings that could change a holdout number: code, market data, enrichment, tariff, calendars, sessions."""
    keep=('source_fingerprint','market_content_digest','enrichment_run_id','tariff_version','contract_master_sha256','settlement_master_sha256','special_sessions_sha256','holiday_calendar_sha256','event_calendar_sha256')
    return dict(release=lineage_mod.RELEASE,code_sha256=lineage_mod.code_digest(),sessions_sha256=hashlib.sha256('|'.join(sessions).encode()).hexdigest(),**{k:trade_meta.get(k) or '' for k in keep})


def freeze_mismatch(frozen,current):
    """Names of what moved since the freeze; empty means the holdout may be opened."""
    bad=[f'identity.{k}' for k in sorted(set(frozen.get('identity',{}))|set(current['identity'])) if frozen.get('identity',{}).get(k)!=current['identity'].get(k)]
    plain=lambda x:json.loads(json.dumps(x,default=str))
    bad+=[k for k in ('trades_sha256','args','rules') if plain(frozen.get(k))!=plain(current[k])]
    if not bad and frozen.get('digest')!=current['digest']:bad.append('digest')
    return bad


def audit_log_path():
    """One log per user, keyed by trade content, so copying the file does not reset it. Inspection tracking, not cryptographic sealing."""
    return Path(os.environ.get('NIFTY_HOLDOUT_LOG') or Path.home()/'.nifty_research'/'holdout_inspections.jsonl')


def read_audit_log():
    path=audit_log_path()
    if not path.exists():return []
    try:return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except json.JSONDecodeError:raise SystemExit(f'Holdout log {path} is corrupt. Repair it before any holdout run.')


def prior_inspections(trades_path,split,digest=None):
    digest=digest or lineage_mod.trades_digest(trades_path)
    return sum(x.get('split')==split and x.get('trades_sha256')==digest for x in read_audit_log())


def resolve_holdout_cutoff(value,split,sessions,rows):
    """Last holdout entry day. 'auto' depends only on the data end and the longest tenor, never on trade outcomes."""
    if value in ('none','per_trade'):return None if value=='none' else 'per_trade'
    cutoff=value
    if value in ('auto','global_longest'):cutoff=(dt.date.fromisoformat(max(sessions))-dt.timedelta(days=max(r['target_tenor'] for r in rows))).isoformat()
    if cutoff<split:raise SystemExit(f"Holdout entry cutoff {cutoff} is before --split {split}, so the holdout would hold no entries. Move the split earlier, drop the longest tenor, or pass --holdout-entry-cutoff none.")
    return cutoff


def main():
    p=cli.Parser(description=__doc__)
    p.add_argument('--trades',default='research_trades_v4.sqlite');p.add_argument('--cache',default='research_cache_v4.sqlite')
    p.add_argument('--allow-conditional-fill-models',action='store_true')
    p.add_argument('--split',default='2026-01-01');p.add_argument('--embargo-days',type=int,default=35)
    p.add_argument('--folds',type=int,default=5);p.add_argument('--max-open',type=int,default=3)
    p.add_argument('--max-capital',type=float,default=0.,help='Capital SCENARIO INR; 0=unlimited')
    p.add_argument('--signals',default='iv_ratio,forward_iv_ratio,iv_to_rv')
    p.add_argument('--lower-ratios',default='.8,1,1.1,1.2,1.3,1.5')
    p.add_argument('--upper-ratios',default='1.2,1.5,2,999')
    p.add_argument('--quantiles',default='.25,.5,.75',help='Training-only cut points for features without a ratio scale')
    p.add_argument('--min-train',type=int,default=20);p.add_argument('--min-holdout',type=int,default=5)
    p.add_argument('--min-positive-folds',type=int,default=0)
    p.add_argument('--strict-completed-only',action='store_true',help='DIAGNOSTIC only: excluding unresolved can introduce bias')
    p.add_argument('--include-theoretical-hedges',action='store_true')
    p.add_argument('--score-basis',choices=BASES,default='per_unit',help='Basis for Sharpe, score, SPA and nested selection; raw INR mixes lot-size eras')
    p.add_argument('--holdout-entry-cutoff',default='per_trade',help="per_trade (default): a trade is in the holdout when its own scheduled exit fits inside the data. global_longest: the V4.4 rule, data end minus the longest tenor of any strategy. A date fixes the last entry. 'none' disables")
    p.add_argument('--research-mode',choices=('train_only','final_holdout','open'),default='train_only',help='train_only: no holdout number is computed; writes selection_freeze.json. final_holdout: one evaluation of exactly the frozen selection. open: the V4.5.0 report, every run sees the holdout')
    p.add_argument('--freeze-file',default=None,help='selection_freeze.json from a train_only run; required with final_holdout')
    p.add_argument('--allow-reinspection',action='store_true',help='final_holdout again on an already-inspected split; the report says so')
    p.add_argument('--train-censor',choices=('scheduled','realized'),default='scheduled',help='scheduled: a trade counts when its planned exit precedes the cutoff, known at entry. realized: the V4.4 rule, which keeps early exits and drops slow trades')
    p.add_argument('--bucket-scheme',choices=('exact_sessions','legacy'),default='exact_sessions',help='exact_sessions: one strategy group per sessions-to-expiry, so a Wed-to-Thu 1-session short and a Fri-to-Tue 2-session short never share a group across the 2025-09-01 expiry move. legacy: the 1-2/3-5/6-8 buckets')
    p.add_argument('--capital-multipliers',default='1,1.5,2',help='Scenario-capital stress for holdout return on capital')
    p.add_argument('--shift-control-max',type=int,default=200,help='Circular-shift negative control per finalist signal; 0 disables')
    p.add_argument('--bootstrap-reps',type=int,default=2000,help='0 disables bootstrap and SPA')
    p.add_argument('--bootstrap-block',type=float,default=10.)
    p.add_argument('--spa-max-models',type=int,default=20000,help='Global SPA is skipped, not truncated, above this')
    p.add_argument('--top',type=int,default=25);p.add_argument('--mark-ranking',choices=('base','lower'),default='base');p.add_argument('--out',default='strategy_rankings_v4')
    a=p.parse_args()
    problems=validate_args(a)
    if problems:p.error('; '.join(problems))
    if a.research_mode=='final_holdout' and not a.freeze_file:p.error('final_holdout needs --freeze-file')
    valid=set(SIGNALS)
    signals=list(SIGNALS) if a.signals=='all' else [x.strip() for x in a.signals.split(',')]
    if set(signals)-valid:p.error('Unknown signals')
    if a.max_open<1 or a.folds<2:p.error('Invalid max-open/folds')
    lo_grid=[float(v) for v in a.lower_ratios.split(',')]
    hi_grid=[float(v) for v in a.upper_ratios.split(',')]
    quantile_cuts=[float(v) for v in a.quantiles.split(',')]
    if any(not 0<q<1 for q in quantile_cuts):p.error('quantiles must be strictly between 0 and 1')
    coverage={s:dict(rows=0,null=0,rules=0,thin_rules=0,unique_masks=0,values=[]) for s in signals}
    multipliers=[float(v) for v in a.capital_multipliers.split(',')]
    split=dt.date.fromisoformat(a.split)
    cutoff=(split-dt.timedelta(days=a.embargo_days)).isoformat()
    conn=sqlite3.connect(a.trades);conn.row_factory=sqlite3.Row
    cache=sqlite3.connect(a.cache);cache.row_factory=sqlite3.Row
    try:
        trade_meta=dict(conn.execute('SELECT name,value FROM meta'))
        if trade_meta.get('complete')!='1':raise SystemExit('Incomplete trade database')
        if trade_meta.get('engine')!='4.5.4':raise SystemExit('Trade database predates this release: rerun nifty-iv-calendar-research')
        fill_model=json.loads(trade_meta.get('config') or '{}').get('fill_model')
        if fill_model not in CAUSAL_FILL_MODELS+STRESS_FILL_MODELS and not a.allow_conditional_fill_models:raise SystemExit(f'{fill_model} conditions fills on later volume; ranking refused. Pass --allow-conditional-fill-models for a diagnostic.')
        try:lineage_mod.check_inputs(dict(cache.execute('SELECT name,value FROM meta')),trade_meta)
        except ValueError as e:raise SystemExit(f'Refusing to rank: {e}')
        daily=[dict(r) for r in cache.execute('SELECT * FROM spot_daily ORDER BY day')]
        sessions=valuation_sessions(daily)
        regime=regimes(daily)
        allowed="status IN ('closed_traded','closed_verified_settlement','proxy_settled'"+("" if a.strict_completed_only else ",'unresolved'")+")"
        if not a.include_theoretical_hedges:allowed+=" AND execution_class<>'theoretical_delta'"
        needed='''id,day,exit_day,status,provider,strike_mode,strike_step,strike_value,
        economic_id,target_tenor,hedge,exit_policy,structure,short_dte_bucket,iv_ratio,forward_iv_ratio,
        iv5,iv15,iv30,iv45,iv60,iv75,iv90,iv120,rv5_close,rv5_parkinson,
        iv_to_rv,iv_to_rv_parkinson,skew_slope,rv5_yz,iv_to_rv_yz,gap_share_5d,rv5_1m_range_proxy,iv_to_rv_1m_range_proxy,
        rv5_1m_closeclose,iv_to_rv_1m_closeclose,lot_units,long_strike,long_strike_offset,iv_ratio_biz,iv_to_rv_yz_biz,spot_to_sma50,spot_to_sma100,spot_to_sma200,trend_21d,pct_below_52w_high,bf25_short,var_carry_daily,pnl_inr,initial_debit_inr,
        entry_capital_scenario,execution_class'''
        needed+=',scheduled_exit_day,short_sessions,entry_weekday,short_expiry_weekday,entry_risk_pts,events_in_short,path_id,contract_id,mtm_id,tm1_outcome'
        EXIT_FIELD[0]='exit_day' if a.train_censor=='realized' else 'scheduled_exit_day'
        rows=[dict(x) for x in conn.execute('SELECT '+needed+' FROM trades WHERE '+allowed+' ORDER BY day,id')]
        boundary=[r for r in rows if r['day']<cutoff<=r['exit_day']]
        if boundary:   # still open at the cutoff: from here on the sample knows only its last mark before the cutoff
            last={}
            for i in range(0,len(boundary),350):
                ids=[r['id'] for r in boundary[i:i+350]]
                last.update({t:v for t,v,_ in conn.execute('SELECT trade_id,pnl_inr,MAX(day) FROM marks WHERE day<? AND trade_id IN ('+','.join('?'*len(ids))+') GROUP BY trade_id',[cutoff]+ids)})
            for r in boundary:
                r.update(pnl_inr=float(last.get(r['id'],0.)),exit_day=OPEN_AT_CUTOFF,status='open_at_cutoff',path_id='open:'+r['id'],mtm_id='open:'+r['id'],contract_id='open:'+r['id'],tm1_outcome=None)
        train_boundary=dict(open_at_cutoff_trades=len(boundary),as_of_mark_inr=sum(r['pnl_inr'] for r in boundary),in_training_sample=sum(r['scheduled_exit_day']<cutoff for r in boundary),note='Marks before the cutoff only; each later outcome is withheld from training')
        if a.research_mode=='train_only':rows=[r for r in rows if r['day']<cutoff]   # sealed means no later row reaches this report, not only no holdout metric
        all_by_group=defaultdict(list)
        for row in rows:
            row['entry_capital']=float(row['entry_capital_scenario'])
            key=(row['strike_mode'],row['strike_step'],row['strike_value'],row['target_tenor'],
                 row['hedge'],row['exit_policy'],row['structure'],row['short_dte_bucket'] if a.bucket_scheme=='legacy' else str(row['short_sessions']),row['long_strike_offset'])
            all_by_group[key].append(row)
        print('Eligible trade simulations:',len(rows),'groups:',len(all_by_group),flush=True)
        if not rows:raise SystemExit('No eligible trades')
        # One fixed calendar for every rule: the simulated span, not when a given rule happened to trade.
        span_start=min(r['day'] for r in rows);span_end=max((r['exit_day'] for r in rows if r['exit_day']<OPEN_AT_CUTOFF),default=cutoff)
        train_days=[d for d in sessions if span_start<=d<cutoff]
        hold_days=fixed_holdout_days(sessions,a.split) if a.holdout_entry_cutoff=='per_trade' else [d for d in sessions if a.split<=d<=span_end]
        if not train_days or (not hold_days and a.research_mode!='train_only'):raise SystemExit('Split outside available completed market data')
        a.holdout_entry_cutoff=resolve_holdout_cutoff(a.holdout_entry_cutoff,a.split,sessions,rows)
        nested_by_group={};nested_portfolios={};min_past=max(5,a.min_train//4)
        all_mask_sharpes=[];finalists=[];raw_grid=0;maskset=set();summary=Counter();spa_models=[];spa_groups=[];spa_unique=[];spa_seen=set();spa_total=0;finalist_total=0;finalist_cap=max(a.top*40,2000);spa_memo={};shift_memo={}
        family={}
        folds=fold_ranges(train_days,a.folds)
        if not folds:raise SystemExit('Insufficient training sessions for requested folds')
        for gno,(key,group) in enumerate(all_by_group.items(),1):
            trainable=[x for x in group if x['day']<cutoff and done_by(x)<cutoff]
            if len(trainable)<min_past:continue
            marks=load_group_marks(conn,group)
            matrix=GroupMatrix(trainable,marks,train_days)
            weights=basis_weights(trainable,a.score_basis)
            baseline_chosen=choose(trainable,a.max_open,a.max_capital or None)
            budget=matrix.mean_deployed(baseline_chosen,weights)   # one capital budget for baseline and every filter
            baseline_return=matrix.daily_return(baseline_chosen,a.mark_ranking,weights,budget)
            if baseline_chosen:   # equal-weight book of every strike and tenor, no filter, no selection: the bar every filtered rule must clear
                fam=family.setdefault((key[4],key[5],key[6]),dict(pnl=np.zeros(len(train_days)),groups=0,trades=0,keys=[]))
                fam['pnl']+=matrix.daily_pnl(baseline_chosen,a.mark_ranking,weights);fam['groups']+=1;fam['trades']+=len(baseline_chosen);fam['keys'].append(key)
            selections_seen=set();rules={};quantile_rules=set();rule_sharpe={};mask_sharpe={};group_first=len(finalists)
            for signal in signals:
                values=[ratio(x,signal) for x in trainable]
                finite_rows=[x for x,v in zip(trainable,values) if v is not None and math.isfinite(v)]
                cov=coverage[signal];cov['rows']+=len(values);cov['null']+=len(values)-len(finite_rows);cov['values']+=[ratio(x,signal) for x in finite_rows]
                if len(cov['values'])>200000:cov['values']=cov['values'][::2]   # diagnostic quantiles only; keeps memory bounded
                for rule_key,low,high in signal_grid(signal,values,lo_grid,hi_grid,quantile_cuts):
                    qualifying=[x for x,v in zip(trainable,values) if v is not None and math.isfinite(v) and low<=v<high]
                    cov['rules']+=1
                    if signal in QUANTILE_SIGNALS:rules[rule_key]=finite_rows;quantile_rules.add(rule_key)
                    else:rules[rule_key]=qualifying
                    if len(qualifying)<a.min_train:cov['thin_rules']+=1;continue
                    chosen=choose(qualifying,a.max_open,a.max_capital or None,presorted=True)   # rows arrive ordered by day,id
                    mask=tuple(x['id'] for x in chosen)
                    if mask in selections_seen:
                        summary['duplicate_masks']+=1;rule_sharpe[rule_key]=mask_sharpe.get(mask);continue
                    cov['unique_masks']+=1
                    selections_seen.add(mask);summary['unique_masks_encountered']+=1
                    if len(chosen)<a.min_train:continue
                    raw_grid+=1;economic_mask=hashlib.sha256('|'.join(sorted(x['mtm_id'] for x in chosen)).encode()).hexdigest();maskset.add(economic_mask)   # identical daily marks score identically, so they are one trial whichever policy label produced them
                    train=matrix.evaluate(chosen,a.max_capital or None,a.mark_ranking)
                    scored=train if a.score_basis=='raw' else matrix.evaluate(chosen,a.max_capital or None,a.mark_ranking,weights)
                    rule_sharpe[rule_key]=mask_sharpe[mask]=scored['sharpe']
                    if scored['sharpe'] is not None:all_mask_sharpes.append(scored['sharpe'])
                    if a.bootstrap_reps and baseline_return is not None:
                        model_return=matrix.daily_return(chosen,a.mark_ranking,weights,budget)
                        if model_return is not None:
                            spa_total+=1   # the family is always counted; memory stops growing at the cap
                            if len(spa_models)<a.spa_max_models:
                                spa_models.append(np.asarray(model_return-baseline_return,dtype='float32'));spa_groups.append(key);spa_unique.append(economic_mask not in spa_seen);spa_seen.add(economic_mask)
                    # Fixed rule, one portfolio, each fold's MTM clipped to that fold's sessions.
                    path=matrix.series(chosen,a.mark_ranking,weights)
                    fold_results=[fold_window_stats(matrix,chosen,begin,end,a.max_capital or None,a.mark_ranking,weights,path) for begin,end in folds]
                    if not any(f['entries']>=3 for f in fold_results):continue
                    sharpes=[f['sharpe'] or 0. for f in fold_results]   # a flat fold earned nothing; it is not skipped
                    positives=sum(x['window_mtm']>0 for x in fold_results)
                    if positives<a.min_positive_folds:continue
                    mean_sr=statistics.mean(sharpes)
                    sd_sr=statistics.stdev(sharpes) if len(sharpes)>1 else 1.
                    score=mean_sr-.75*sd_sr-.25*scored['max_dd']/max(1e-12,scored['capital_scenario_peak'])
                    # All candidates scored by full daily MTM; no gross-PnL prescreen.
                    finalist_total+=1
                    finalists.append(dict(key=key,signal=signal,lower=low,upper=high,rule_key=rule_key,
                        score=score,train=train,scored=scored,folds=[compact(f) for f in fold_results],
                        fold_positive=positives,fold_count=len(fold_results),
                        params=(key,signal,low,high)))
            for finalist in finalists[group_first:]:finalist['plateau']=plateau(finalist['rule_key'],rule_sharpe,lo_grid,hi_grid,quantile_cuts)
            if len(finalists)>2*finalist_cap:prune_finalists(finalists,finalist_cap)
            nested=nested_folds(rules,matrix,folds,a.embargo_days,cutoff,a.max_open,a.max_capital or None,a.mark_ranking,weights,min_past,quantile_rules)
            nested_by_group[key]=nested
            nested_portfolios[key]=persistent_nested_portfolio(nested,matrix,a.max_open,a.max_capital or None,a.mark_ranking,weights)
            if gno%25==0:print('Full-MTM groups:',gno,'of',len(all_by_group),flush=True)
        finalists.sort(key=lambda x:(x['score'],x['train']['n']),reverse=True)
        results=[];effective_count=max(2,len(maskset))
        sharpe_dispersion=statistics.stdev(all_mask_sharpes) if len(all_mask_sharpes)>1 else 1.   # whole search, not survivors
        # What the whole selection procedure earned: one portfolio, each fold's rule chosen from its past only.
        picks=[]
        for fi,(begin,end) in enumerate(folds):
            live=[(nested_by_group[k][fi]['inner_score'],k) for k in sorted(nested_by_group,key=str) if nested_by_group[k][fi]['rule'] is not None]
            if not live:picks.append(dict(begin=begin,end=end,group=None,rule=None,inner_score=None,n=0,entry_cohort_lifecycle_net=0.,candidates=[]));continue
            score,k=max(live,key=lambda z:z[0]);rec=nested_by_group[k][fi]
            picks.append(dict(rec,group=dict(zip(STRATEGY_FIELDS,k)),inner_score=score))
        pool=[x for p in picks for x in p['candidates']]
        nested_global=dict(basis=a.score_basis,executable_portfolio_state='persistent_across_folds',groups_searched=len(nested_by_group),portfolio=None,
            selections=[{f:p.get(f) for f in ('begin','end','group','rule','resolved_bounds','inner_score','n','entry_cohort_lifecycle_net')} for p in picks],
            note='Each fold selects strategy and thresholds from earlier fully exited, embargoed trades. Slots and capital persist across folds. entry_cohort_lifecycle_net is per-fold-entry diagnostics; do not sum it')
        if pool:
            gm=GroupMatrix(pool,load_group_marks(conn,pool),train_days)
            nested_global['portfolio']=persistent_nested_portfolio(picks,gm,a.max_open,a.max_capital or None,a.mark_ranking,basis_weights(pool,a.score_basis))
        spa_global=None
        if spa_models:
            if spa_total<=a.spa_max_models:
                spa_global=spa_test(spa_matrix([m for m,u in zip(spa_models,spa_unique) if u]),a.bootstrap_reps,a.bootstrap_block)
            else:spa_global=dict(models_counted=spa_total,skipped=f'{spa_total} models exceed --spa-max-models; a truncated universe would bias the test')
        sealed=a.research_mode=='train_only'
        in_holdout=lambda r:not sealed and r['day']>=a.split and (a.holdout_entry_cutoff is None or (r['scheduled_exit_day']<=sessions[-1] if a.holdout_entry_cutoff=='per_trade' else r['day']<=a.holdout_entry_cutoff))
        ensembles=[]
        for fam_key,fam in family.items():
            x=fam['pnl']/fam['groups'];held=[r for k in fam['keys'] for r in all_by_group[k] if in_holdout(r)]
            hold=None
            if held:
                days_h=hold_days;total=np.zeros(len(days_h));count=0
                for k in fam['keys']:
                    rows_h=[r for r in all_by_group[k] if in_holdout(r)]
                    picked=choose(rows_h,a.max_open,a.max_capital or None)
                    if picked:
                        gm=GroupMatrix(rows_h,load_group_marks(conn,rows_h),days_h);total+=gm.daily_pnl(picked,a.mark_ranking,basis_weights(rows_h,a.score_basis));count+=1
                if count:y=total/count;hold=dict(strategy_groups=count,days=len(days_h),sharpe=annualized(y),net=float(y.sum()),max_dd=float((np.maximum(np.maximum.accumulate(np.cumsum(y)),0.)-np.cumsum(y)).max()))
            ensembles.append(dict(hedge=fam_key[0],policy=fam_key[1],structure=fam_key[2],strategy_groups=fam['groups'],trades=fam['trades'],basis=a.score_basis,train=dict(days=len(x),sharpe=annualized(x),net=float(x.sum())),holdout=hold))
        ensembles.sort(key=lambda z:(z['train']['sharpe'] is not None,z['train']['sharpe'] or 0),reverse=True)
        economic_of={r['id']:r['mtm_id'] for r in rows};shortlist=[];by_trades={}
        for x in finalists:   # one result per distinct set of training trades; other parameter labels are listed on it
            trades=tuple(sorted(economic_of[i] for i in x['train']['selected']))
            if trades in by_trades:by_trades[trades]['aliases'].append(dict(strategy=dict(zip(STRATEGY_FIELDS,x['key'])),signal=x['signal'],lower=x['lower'],upper=x['upper']))
            elif len(shortlist)<a.top:by_trades[trades]=x;x['aliases']=[];shortlist.append(x)
        trades_sha=lineage_mod.trades_digest(a.trades);freeze=freeze_payload(shortlist,trades_sha,vars(a),run_identity(trade_meta,sessions))
        if a.research_mode=='final_holdout':
            frozen=json.loads(Path(a.freeze_file).read_text())
            moved=freeze_mismatch(frozen,freeze)
            if moved:raise SystemExit('Selection differs from --freeze-file on: '+', '.join(moved)+'. No holdout number was computed. Re-freeze with train_only only if this holdout is still uninspected.')
            seen_before=prior_inspections(a.trades,a.split,trades_sha)
            if seen_before and not a.allow_reinspection:raise SystemExit(f'This holdout split has been inspected {seen_before} time(s). It is no longer untouched. --allow-reinspection overrides, and the report records it.')
        prior_views=None if sealed else record_holdout_inspection(a.trades,a.split,a.holdout_entry_cutoff,{},trades_sha)
        for x in shortlist:
            key,signal,low,high=x['params'];group=all_by_group[key]
            vrows=[r for r in group if in_holdout(r) and (v:=ratio(r,signal)) is not None and math.isfinite(v) and low<=v<high]
            marks=load_group_marks(conn,vrows)
            group_hold=[row for row in group if in_holdout(row)];hd=hold_days
            test=portfolio(vrows,marks,hd,a.max_open,a.max_capital or None,a.mark_ranking)
            hold_stress_lower=portfolio(vrows,marks,hd,a.max_open,a.max_capital or None,'lower')
            hold_stress_upper=portfolio(vrows,marks,hd,a.max_open,a.max_capital or None,'upper')
            base_train=[row for row in group if row['day']<cutoff and done_by(row)<cutoff]
            baseline_marks=load_group_marks(conn,base_train)
            base_train_mtm=portfolio(base_train,baseline_marks,train_days,a.max_open,a.max_capital or None,a.mark_ranking)
            base_holdout_mtm=portfolio(group_hold,load_group_marks(conn,group_hold),hd,a.max_open,a.max_capital or None,a.mark_ranking)
            if key not in spa_memo:
                within=[m for m,g in zip(spa_models,spa_groups) if g==key]
                spa_memo[key]=spa_test(spa_matrix(within),a.bootstrap_reps,a.bootstrap_block) if within and spa_total<=a.spa_max_models else None
            spa_within=spa_memo[key]
            if a.shift_control_max and (key,signal) not in shift_memo:
                control_matrix=GroupMatrix(base_train,baseline_marks,train_days)
                shift_memo[key,signal]=circular_shift_control(base_train,[ratio(r,signal) for r in base_train],[(l,h) for _,l,h in signal_grid(signal,[ratio(r,signal) for r in base_train],lo_grid,hi_grid,quantile_cuts)],
                    control_matrix,a.max_open,a.max_capital or None,a.mark_ranking,basis_weights(base_train,a.score_basis),a.min_train,a.shift_control_max)
            boot=bootstrap_summary(x['scored']['daily_increments'],a.bootstrap_reps,a.bootstrap_block) if a.bootstrap_reps else None
            fully=[r for r in vrows if r['status']!='unresolved']
            cohorts=dict(all_opened=compact(test),fully_observed=compact(portfolio(fully,marks,hd,a.max_open,a.max_capital or None,a.mark_ranking)) if fully else None,
                unresolved_in_all_opened=sum(r['status']=='unresolved' for r in choose(vrows,a.max_open,a.max_capital or None)))
            views={}
            if vrows:
                hm=GroupMatrix(vrows,marks,hd);picked=choose(vrows,a.max_open,a.max_capital or None)
            tm=GroupMatrix([r for r in base_train],baseline_marks,train_days);tp=[r for r in base_train if r['id'] in set(x['train']['selected'])]
            for b in BASES:
                t=tm.evaluate(tp,a.max_capital or None,a.mark_ranking,basis_weights(base_train,b))
                h=hm.evaluate(picked,a.max_capital or None,a.mark_ranking,basis_weights(vrows,b)) if vrows and picked else None
                views[b]=dict(train=dict(net=t['net'],sharpe=t['sharpe'],max_dd=t['max_dd']),holdout=dict(net=h['net'],sharpe=h['sharpe'],max_dd=h['max_dd']) if h else None)
            capital_stress={f'{m}x':(test['net']/(m*test['capital_scenario_peak']) if test['capital_scenario_peak'] else None) for m in multipliers}
            train=x['train'];dsr=effective_dsr(x['scored']['daily_increments'],x['scored']['sharpe'],effective_count,sharpe_dispersion)
            selected_train=set(train['selected']);selected_hold=set(test['selected'])
            completed_cohort=[r for r in vrows if r['id'] in selected_hold]
            cohort_followup=None
            if completed_cohort:
                last=max(max(r['exit_day'] for r in completed_cohort),completed_cohort[0]['day']);cd=[d for d in hd if d<=last]
                cohort_followup=compact(portfolio(completed_cohort,marks,cd,a.max_open,a.max_capital or None,a.mark_ranking));cohort_followup['note']='Selected trades only, ending at their last exit. A lifecycle view; not comparable across strategies'
            outcomes=lambda ids:dict(Counter(r['tm1_outcome'] for r in group if r['id'] in ids and r.get('tm1_outcome')))
            regime_pnl=defaultdict(lambda:dict(n=0,pnl=0.))
            for r in group:
                if r['id'] in selected_train or r['id'] in selected_hold:
                    sample='train' if r['id'] in selected_train else 'holdout'
                    tag=sample+'_'+regime.get(r['day'],'unknown')
                    regime_pnl[tag]['n']+=1;regime_pnl[tag]['pnl']+=r['pnl_inr']   # a trade open at the cutoff carries its as-of mark
            results.append(dict(strategy=dict(zip(STRATEGY_FIELDS,key)),
                signal=signal,lower=low,upper=high,score=x['score'],threshold_basis='training_quantile' if signal in QUANTILE_SIGNALS else 'fixed_grid',
                quantile_rule=list(x['rule_key'][1:]) if signal in QUANTILE_SIGNALS else None,plateau=x['plateau'],parameter_aliases=x['aliases'],
                deflated_sharpe_diagnostic=dsr,effective_trials_approx=effective_count,
                bootstrap=boot,spa_within_group=spa_within,circular_shift_control=shift_memo.get((key,signal)),
                training=compact(train),fold_positive=x['fold_positive'],fold_count=x['fold_count'],
                fixed_rule_fold_diagnostic=x['folds'],nested_selections=[{f:n.get(f) for f in ('begin','end','rule','resolved_bounds','inner_score','n','entry_cohort_lifecycle_net')} for n in nested_by_group.get(key,[])],
                nested_portfolio=nested_portfolios.get(key),
                holdout_status='sealed' if sealed else 'reported',holdout_cohort_followup=cohort_followup,tm1_execution=dict(train=outcomes(selected_train),holdout=outcomes(selected_hold)) if key[5] in ('tm1','p25_s50_tm1') else None,
                basis_views=views,holdout_cohorts=cohorts,return_on_scenario_capital_stress=capital_stress,
                score_basis=a.score_basis,unfiltered_train_baseline=compact(base_train_mtm),
                unfiltered_holdout_baseline=compact(base_holdout_mtm),holdout=compact(test),
                holdout_stress_lower=compact(hold_stress_lower),holdout_stress_upper=compact(hold_stress_upper),holdout_sufficient=test['n']>=a.min_holdout,
                regimes=dict(regime_pnl),expiry_regime_mix=dict(train_before_2025_09=sum(r['day']<'2025-09-01' for r in train_rows_of(group,train['selected'])),train_from_2025_09=sum(r['day']>='2025-09-01' for r in train_rows_of(group,train['selected'])),holdout_n=test['n'],post_move_train_support='sufficient' if sum(r['day']>='2025-09-01' for r in train_rows_of(group,train['selected']))>=a.min_train else 'insufficient')))
    finally:conn.close();cache.close()
    final_dest=Path(a.out);dest=final_dest.with_name(final_dest.name+f'.tmp{os.getpid()}');dest.mkdir(parents=True)   # the report set appears whole or not at all
    for cov in coverage.values():
        vals=cov.pop('values');cov['null_rate']=cov['null']/cov['rows'] if cov['rows'] else None
        cov['training_quantiles']={str(q):float(np.quantile(vals,q)) for q in (.05,.25,.5,.75,.95)} if vals else None
    payload=dict(engine='V4',train_boundary=train_boundary,holdout_policy='fresh_account_at_split: holdout admits entries from the split onward with empty slots; training positions are not carried in',unfiltered_family_books=ensembles,unfiltered_family_books_note='Every strike and tenor group, no signal filter, each group admitted under --max-open and --max-capital, then averaged. A capital-constrained admission book, not all opportunities held at once',signal_coverage=coverage,release=lineage_mod.RELEASE,lineage=lineage_mod.build(a.trades,trade_meta,vars(a)),metrics_basis=dict(training='raw_inr',holdout='raw_inr',score=a.score_basis,dsr=a.score_basis,bootstrap=a.score_basis,nested=a.score_basis),
        complete_grid_mtm=True,spa_global=spa_global,nested_walk_forward=nested_global,
        score_basis=a.score_basis,holdout_entry_cutoff=a.holdout_entry_cutoff,
        labels=dict(capital='scenario_capital (not NSE SPAN/ELM or broker margin)',fills='OHLC_fill_proxy (no bid/ask, queue or impact data); next_bar_open is the only model whose gate precedes its fill; next_bar_open_adverse is an ex-post stress; the rest are conditional diagnostics',status='research_only',
            scale_bases='per_unit, entry_capital_normalized and risk_normalized_score rescale finished, billed trades. None is executed position sizing: lots, charges, participation and capital are not recomputed',
            sharpe='calendar-time over a fixed window, flat days included; active_day_sharpe is reported separately'),split=a.split,train_end_exclusive=cutoff,
        embargo_days=a.embargo_days,folds=folds,
        evaluated_unique_full_mtm_masks=raw_grid,approx_effective_trials=effective_count,
        total_eligible_trades=len(rows),statuses=dict(Counter(r['status'] for r in rows)),
        strict_completed_only_diagnostic=a.strict_completed_only,n_finalists=finalist_total,search_counts=dict(rule_cells=sum(c['rules'] for c in coverage.values()),rule_cells_below_min_train=sum(c['thin_rules'] for c in coverage.values()),unique_masks_encountered=summary['unique_masks_encountered'],unique_masks_scored=raw_grid,economically_unique_masks_scored=len(maskset),groups_searched=len(nested_by_group),same_day_competing_groups=sum(len(g)!=len({r['day'] for r in g}) for g in all_by_group.values()),distinct_contract_paths=len({r['contract_id'] for r in rows}),distinct_cash_flow_paths=len({r['path_id'] for r in rows}),distinct_mtm_paths=len({r['mtm_id'] for r in rows}),finalists_after_fold_gates=finalist_total),
        warnings=['OHLC prices are not bid/ask executable prices','NSE settlement is PROXY unless independently verified; proxy trades included',
            'Capital charge is scenario, not NSE SPAN; intra-trade shortfalls flagged, not liquidated',
            'Delta-hedged paths use NONTRADEABLE spot proxy when enabled',
            'DSR uses heuristic N_eff and is not proof of persistent alpha',
            'Missing valuations modelled; unresolved receive conservative horizon marks, NOT realized returns',
            'SPA p-values are approximate on overlapping campaigns: studentized bootstrap calibration gives 5-21% false positives at nominal 5% for block 10, 3-10% for block 20 (nifty-iv-calibrate-spa). Use them to discount, never to confirm',
            'holdout_stress_lower/upper stress open-position marks along the path; closed trades end at realized P&L, so use --fill-model next_minute_adverse_range for execution stress',
            *([] if a.holdout_entry_cutoff else ['No --holdout-entry-cutoff was predeclared: late long-dated entries may be censored']),
            *([] if trade_meta.get('holiday_calendar_sha256') or a.bucket_scheme!='exact_sessions' else ['Exact-session groups were counted without --holiday-calendar: a week with a holiday falls in the wrong group']),
            'fixed_rule_fold_diagnostic uses rules chosen on the full training window; use nested_walk_forward.portfolio for selection-free out-of-sample evidence'],
        results=results)
    payload['research_mode']=a.research_mode
    if a.research_mode=='train_only':
        payload['warnings'].append('Holdout sealed: no holdout metric in this report is an observation. Run --research-mode final_holdout once, with selection_freeze.json')
    payload['holdout_inspection_number']=None if prior_views is None else prior_views+1
    if prior_views:payload['warnings'].append(f'This holdout split has been reported {prior_views} time(s) before; results are no longer untouched out-of-sample evidence')
    payload['lineage']['report_sha256']=lineage_mod.report_digest(json.loads(json.dumps(payload,default=str)))
    if a.research_mode=='train_only':(dest/'selection_freeze.json').write_text(json.dumps(freeze,indent=2,default=str))
    (dest/'rankings.json').write_text(json.dumps(payload,indent=2,default=str))
    with (dest/'rankings.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['rank','strategy','signal','lo','hi','quantile_rule','plateau_median_sharpe','folds_positive',
            'train_n','train_net_raw_inr','train_dd_raw_inr','train_sharpe_raw_inr','holdout_n','holdout_net_raw_inr','holdout_dd_raw_inr','holdout_sharpe_raw_inr','dsr_score_basis','shift_control_p','nested_portfolio_mtm_basis','train_active_day_sharpe','train_time_in_market','boot_sharpe_p05','boot_dd_p95','spa_p_within_group'])
        for i,x in enumerate(results,1):
            tr=x['training'];ho=x['holdout']
            writer.writerow([i,json.dumps(x['strategy']),x['signal'],x['lower'],x['upper'],json.dumps(x['quantile_rule']),(x['plateau'] or {}).get('median_sharpe'),x['fold_positive'],
                tr['n'],tr['net'],tr['max_dd'],tr['sharpe'],ho['n'],ho['net'],ho['max_dd'],ho['sharpe'],x['deflated_sharpe_diagnostic'],(x.get('circular_shift_control') or {}).get('p_value'),(x['nested_portfolio'] or {}).get('window_mtm'),(x['training'].get('active_day_sharpe')),(x['training'].get('time_in_market')),
                (x['bootstrap'] or {}).get('sharpe_p05'),(x['bootstrap'] or {}).get('max_dd_p95'),(x['spa_within_group'] or {}).get('p_consistent')])
    json.loads((dest/'rankings.json').read_text())
    retired=final_dest.with_name(final_dest.name+f'.old{os.getpid()}')
    if final_dest.exists():final_dest.rename(retired)
    dest.rename(final_dest);shutil.rmtree(retired,ignore_errors=True)
    print('V4 full-grid rankings:',final_dest,'scored:',raw_grid,'shortlisted:',len(results))

if __name__=='__main__':main()
