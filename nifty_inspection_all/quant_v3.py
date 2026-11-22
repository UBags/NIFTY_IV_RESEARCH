"""Expiry-specific parity estimator and trailing realized volatility (research only)."""
from __future__ import annotations
from dataclasses import dataclass
from math import exp,log,sqrt,isfinite,pi
import numpy as np
from quant_v2 import black_forward, implied_vol, implied_vol_batch, black_greeks,forward_variance

@dataclass(frozen=True)
class Parity:
    forward: float | None
    discount: float | None
    rate: float | None
    matched_pairs: int
    dispersion_bps: float | None
    quality: str
    estimator: str


def parity_ols(quotes, spot, maturity, fallback_rate=.06, min_volume=1,
               max_residual_bps=40., min_strikes=3, rate_bounds=(.0,.15)):
    """Robust strike-by-strike parity regression C-P=a-b*K.

    Prices are asynchronous one-minute candles, not executable paired quotes.
    Rejects implausible fit rather than clamping estimated rates into validity.
    If regression impossible, uses discounted simple put/call parity with disclosed
    fallback rate, only if cross-strike consistency tests pass.
    """
    if not spot or maturity<=0:return Parity(None,None,None,0,None,'invalid_spot_or_maturity','none')
    pairs=[]
    for (k,cp),q in quotes.items():
        if cp!='CE':continue
        p=quotes.get((k,'PE'))
        if not p:continue
        c=q.get('close');put=p.get('close')
        if (c is None or put is None or not isfinite(c) or not isfinite(put)
            or c<=0 or put<=0 or (q.get('volume') or 0)<min_volume
            or (p.get('volume') or 0)<min_volume):continue
        pairs.append((float(k),float(c-put),sqrt(min(q['volume'],p['volume'])+1)))
    n=len(pairs)
    if n==0:return Parity(None,None,None,0,None,'no_matched_pairs','none')
    # Prefer liquid pairs in +/- 8% of spot to avoid deep-wing stale prints.
    pairs=[p for p in pairs if abs(p[0]/spot-1)<=.08]
    if not pairs:return Parity(None,None,None,0,None,'no_near_atm_pairs','none')
    n=len(pairs)
    if n>=min_strikes and max(x[0] for x in pairs)-min(x[0] for x in pairs)>=50:
        k=np.array([x[0] for x in pairs]);y=np.array([x[1] for x in pairs]);w=np.array([x[2] for x in pairs]);
        design=np.column_stack((np.ones(n),k-spot))
        mask=np.ones(n,dtype=bool)
        for _ in range(3):
            if mask.sum()<min_strikes:break
            coeff=np.linalg.lstsq(design[mask]*w[mask,None],y[mask]*w[mask],rcond=None)[0]
            residual=y-design@coeff
            new=abs(residual)<=max_residual_bps*spot/10000   # prune strikes the fit would otherwise fail on
            if new.sum()<min_strikes or np.array_equal(new,mask):break
            mask=new
        coeff=np.linalg.lstsq(design[mask]*w[mask,None],y[mask]*w[mask],rcond=None)[0]   # final fit matches final mask
        slope=coeff[1];discount=-slope
        if 0<discount<2:
            r=-log(discount)/maturity
            forward=spot+(coeff[0]/discount)
            residual=y[mask]-(design[mask]@coeff)
            disp=float(max(abs(residual)) / spot*10000) if len(residual) else None
            if (rate_bounds[0]<=r<=rate_bounds[1] and forward>0
                and disp is not None and disp<=max_residual_bps):
                return Parity(float(forward),float(discount),float(r),int(mask.sum()),
                              disp,'fit_ok','robust_strike_ols')
    discount=exp(-fallback_rate*maturity)
    forwards=np.array([k+(d/discount) for k,d,_ in pairs]);mid=float(np.median(forwards))
    disp=float(max(abs(forwards-mid))/spot*10000)
    if disp>max_residual_bps:return Parity(None,None,None,n,disp,'parity_inconsistent','fallback_rate')
    return Parity(mid,discount,fallback_rate,n,disp,'fallback_rate','matched_parity')


def trailing_rv(prior_daily, estimator='close', window=5):
    """Vol from COMPLETED daily OHLC only; daily rows chronological, no current day.

    Parkinson range estimator uses each day's log(H/L)^2; close-to-close uses
    unbiased stdev of log returns. Incomplete daily sessions are excluded.
    """
    rows=[x for x in prior_daily if x['complete_session'] and all(
        x.get(field) and x[field]>0 for field in ('open','high','low','close'))]
    if estimator=='parkinson':
        if len(rows)<window:return None
        values=[log(x['high']/x['low'])**2 for x in rows[-window:]]
        return sqrt(252*sum(values)/(4*window*log(2)))
    if estimator=='close':
        if len(rows)<window+1:return None
        closes=[x['close'] for x in rows[-window-1:]]
        returns=np.diff(np.log(closes))
        return float(np.std(returns,ddof=1)*sqrt(252)) if len(returns)>1 else None
    raise ValueError('Estimator must be close or parkinson; use yang_zhang_rv for Yang-Zhang')


def yang_zhang_rv(prior_daily, window=5):
    """Yang-Zhang (2000) from COMPLETED daily OHLC; prior_daily excludes the current day.

    An observation needs its immediately preceding row to be complete too, so an
    overnight return never spans a dropped session. Returns None when short of data.
    rv1m_range_proxy adds overnight squared returns to summed 1-minute Parkinson RANGE
    variance. rv1m_closeclose uses squared close-to-close minute returns instead and
    needs at least 365 consecutive-minute returns in every session. Either is None if data is short.
    """
    obs=[]
    for prev,row in zip(prior_daily,prior_daily[1:]):
        if not (prev.get('complete_session') and row.get('complete_session')):continue
        if not all(row.get(x) and row[x]>0 for x in ('open','high','low','close')) or not prev.get('close') or prev['close']<=0:continue
        obs.append((prev['close'],row))
    if len(obs)<window or window<2:return None
    obs=obs[-window:]
    o=np.array([log(r['open']/pc) for pc,r in obs])
    c=np.array([log(r['close']/r['open']) for _,r in obs])
    u=np.array([log(r['high']/r['open']) for _,r in obs])
    d=np.array([log(r['low']/r['open']) for _,r in obs])
    n=len(obs)
    k=.34/(1.34+(n+1)/(n-1))
    var_o=float(np.var(o,ddof=1));var_c=float(np.var(c,ddof=1));var_rs=float(np.mean(u*(u-c)+d*(d-c)))
    total=var_o+k*var_c+(1-k)*var_rs
    if total<=0:return None
    pk=[r.get('parkinson_1m_var') for _,r in obs]
    range_proxy=None
    if all(x is not None and x>=0 for x in pk):
        range_proxy=sqrt(252*float(np.mean(o*o+np.array(pk))))
    cc=[r.get('closeclose_1m_var') for _,r in obs]
    complete=all(x is not None and x>=0 and (r.get('closeclose_1m_n') or 0)>=365 for x,(_,r) in zip(cc,obs))
    closeclose=sqrt(252*float(np.mean(o*o+np.array(cc)))) if complete else None
    return dict(rv=sqrt(252*total),gap_share=var_o/total,k=k,rv1m_range_proxy=range_proxy,rv1m_closeclose=closeclose)
