"""Forward-based NIFTY options mathematics, with numerical validity checks.

Prices use European Black (1976) on the expiry-specific forward. All underlying
index hedge Greeks are FORWARD/INDEX PROXIES, not executable futures Greeks.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from statistics import median
import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr, ndtri

SQ2PI = math.sqrt(2*math.pi)


def black_forward(f: float, k: float, t: float, sigma: float, cp: str, rate: float = .06) -> float:
    if min(f,k,t) <= 0 or sigma <= 0:
        return float('nan')
    sv = sigma*math.sqrt(t)
    d1 = (math.log(f/k)+0.5*sv*sv)/sv
    d2 = d1-sv
    disc = math.exp(-rate*t)
    if cp == 'CE': return disc*(f*ndtr(d1)-k*ndtr(d2))
    if cp == 'PE': return disc*(k*ndtr(-d2)-f*ndtr(-d1))
    raise ValueError('cp must be CE or PE')


def black_greeks(f,k,t,sigma,cp,rate=.06,spot=None):
    """Return forward-price delta/gamma/vega and optional spot-proxy delta/gamma."""
    if min(f,k,t,sigma)<=0: return None
    sv=sigma*math.sqrt(t)
    d1=(math.log(f/k)+0.5*sv*sv)/sv
    discount=math.exp(-rate*t)
    phi=math.exp(-.5*d1*d1)/SQ2PI
    delta=discount*(ndtr(d1)-(cp=='PE'))
    gamma=discount*phi/(f*sv)
    vega=discount*f*phi*math.sqrt(t)
    factor=f/spot if spot and spot>0 else 1.
    return dict(delta_f=delta,gamma_f=gamma,vega=vega,
                delta_proxy=delta*factor,gamma_proxy=gamma*factor*factor)


def _bounds(f,k,t,cp,rate):
    d=math.exp(-rate*t)
    return (d*max(f-k,0.),d*f) if cp=='CE' else (d*max(k-f,0.),d*k)


def implied_vol(f,k,t,price,cp,rate=.06,lower=.0001,upper=5.,tolerance=1e-8):
    if not all(math.isfinite(v) for v in (f,k,t,price,rate)) or min(f,k,t,price)<=0: return None
    lo,hi=_bounds(f,k,t,cp,rate)
    if price<=lo+max(1e-5,tolerance*.1) or price>=hi-1e-5: return None
    fn=lambda vol:black_forward(f,k,t,vol,cp,rate)-price
    if fn(lower)>0 or fn(upper)<0:return None
    try: return brentq(fn,lower,upper,xtol=1e-11,rtol=1e-13)
    except ValueError:return None


def implied_vol_batch(f,k,t,price,cp,rate=.06):
    """Safeguarded vector Halley, with Brent fallback; no fixed-iteration guarantee."""
    f,k,t,p=np.broadcast_arrays(np.asarray(f,dtype=float),np.asarray(k,dtype=float),
                                 np.asarray(t,dtype=float),np.asarray(price,dtype=float))
    codes=np.broadcast_to(np.asarray(cp),f.shape)
    out=np.full(f.shape,np.nan)
    disc=np.exp(-rate*t)
    intrinsic=np.where(codes=='CE',np.maximum(f-k,0),np.maximum(k-f,0))*disc
    cap=np.where(codes=='CE',f,k)*disc
    good=np.isfinite(f+k+t+p)&(f>0)&(k>0)&(t>0)&(p>intrinsic+1e-5)&(p<cap-1e-5)
    if not good.any():return out
    # Bounded starting point; robust bisection fallback for any failed Halley cells.
    idx=np.flatnonzero(good.ravel())
    ff=f.ravel()[idx]; kk=k.ravel()[idx]; tt=t.ravel()[idx]; pp=p.ravel()[idx]
    cc=codes.ravel()[idx]
    sig=np.clip(np.sqrt(2*np.pi/tt)*pp/(disc.ravel()[idx]*(ff+kk)),.03,2.0)
    ok=np.zeros(len(idx),dtype=bool)
    for _ in range(12):
        sv=sig*np.sqrt(tt)
        d1=(np.log(ff/kk)+.5*sv**2)/sv
        d2=d1-sv
        prices=np.where(cc=='CE',ff*ndtr(d1)-kk*ndtr(d2),kk*ndtr(-d2)-ff*ndtr(-d1))*np.exp(-rate*tt)
        error=prices-pp
        vega=np.exp(-rate*tt)*ff*np.exp(-.5*d1**2)/SQ2PI*np.sqrt(tt)
        volga_ratio=d1*d2/sig
        denominator=vega-.5*error*volga_ratio
        delta=np.divide(error,denominator,out=np.zeros_like(error),where=np.abs(denominator)>1e-10)
        next_sig=np.clip(sig-delta,.0001,5.)
        # Damping prevents divergent steps in tiny-vega tails.
        sig=np.where(np.abs(delta)<1.,next_sig,(sig+next_sig)/2)
        ok=np.abs(error)<1e-7
        if ok.all():break
    result=out.ravel()
    for j,flat_idx in enumerate(idx):
        if ok[j] and np.isfinite(sig[j]): result[flat_idx]=sig[j]
        else:
            v=implied_vol(float(ff[j]),float(kk[j]),float(tt[j]),float(pp[j]),str(cc[j]),rate)
            if v is not None:result[flat_idx]=v
    # Verify all returned solutions against actual prices, including last Halley update.
    for j,flat_idx in enumerate(idx):
        value=result[flat_idx]
        if np.isfinite(value) and abs(black_forward(float(ff[j]),float(kk[j]),float(tt[j]),float(value),str(cc[j]),rate)-float(pp[j]))>1e-5:
            result[flat_idx]=np.nan
    return out


@dataclass(frozen=True)
class ForwardEstimate:
    forward: float | None
    matched_pairs: int
    dispersion_bps: float | None
    quality: str


def implied_forward(quotes: dict, spot: float, t: float, rate=.06,
                    min_volume=1, max_moneyness=.035, max_dispersion_bps=40.):
    """quotes[(strike,cp)] -> bar dict; use ONLY same-timestamp liquid C/P closes.
    Robust median and deviation threshold, no look-ahead/inter-expiry mixing.
    """
    candidates=[]
    for (strike,cp),call in quotes.items():
        if cp!='CE' or abs(float(strike)/spot-1)>max_moneyness:continue
        put=quotes.get((strike,'PE'))
        if not put:continue
        if min((call.get('volume') or 0),(put.get('volume') or 0))<min_volume:continue
        if not all(math.isfinite(float(x)) and float(x)>0 for x in (call.get('close') or 0,put.get('close') or 0)):continue
        forward=float(strike)+(float(call['close'])-float(put['close']))*math.exp(rate*t)
        if forward>0:candidates.append(forward)
    if not candidates:return ForwardEstimate(None,0,None,'no_synchronous_call_put')
    f=median(candidates)
    disp=max(abs(v-f) for v in candidates)/f*10000.
    if disp>max_dispersion_bps:return ForwardEstimate(None,len(candidates),disp,'parity_inconsistent')
    return ForwardEstimate(f,len(candidates),disp,'single_pair' if len(candidates)==1 else 'multi_pair')


def forward_variance(sigma_short,sigma_long,t_short,t_long):
    if any(x is None for x in (sigma_short,sigma_long)) or not(0<t_short<t_long):return None
    x=(sigma_long**2*t_long-sigma_short**2*t_short)/(t_long-t_short)
    return math.sqrt(x) if x>0 else None
