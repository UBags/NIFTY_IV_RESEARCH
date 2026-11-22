"""Memory-bounded vector IV / Greeks: bracketed Newton-Halley, honest scalar fallback.

Solves the out-of-the-money equivalent, seeded at the Black price inflection point
(Manaster-Koehler). Every solution is checked by reconstructing the original price;
anything the vector loop leaves unconverged goes to Brent and is COUNTED.
"""
from __future__ import annotations
import numpy as np
from scipy.special import ndtr
from scipy.optimize import brentq

INV_ROOT2PI=0.39894228040143267794
VOL_LO=1e-4
VOL_HI=5.0

def black_vector(f,k,t,vol,call,rate):
    w=np.maximum(vol,1e-10)*np.sqrt(t)
    d1=(np.log(f/k)+.5*w*w)/w
    d2=d1-w
    return np.exp(-rate*t)*np.where(call,f*ndtr(d1)-k*ndtr(d2),k*ndtr(-d2)-f*ndtr(-d1))


def _bracketed_solve(ff,kk,tt,target,q,sigma,max_iter,tol):
    """target is the undiscounted OTM price; q=+1 call, -1 put. Returns sigma, converged, iterations."""
    n=len(ff);lo=np.full(n,VOL_LO);hi=np.full(n,VOL_HI)
    active=np.arange(n);done=np.zeros(n,dtype=bool);iterations=0
    log_fk=np.log(ff/kk);root_t=np.sqrt(tt)
    while len(active) and iterations<max_iter:
        iterations+=1
        s=sigma[active];rt=root_t[active];w=s*rt
        d1=(log_fk[active]+.5*w*w)/w;d2=d1-w;sign=q[active]
        error=sign*(ff[active]*ndtr(sign*d1)-kk[active]*ndtr(sign*d2))-target[active]
        ok=np.abs(error)<=tol
        done[active[ok]]=True
        keep=~ok
        active=active[keep]
        if not len(active):break
        s=s[keep];rt=rt[keep];d1=d1[keep];d2=d2[keep];error=error[keep]
        lo[active]=np.where(error<0,np.maximum(lo[active],s),lo[active])
        hi[active]=np.where(error>0,np.minimum(hi[active],s),hi[active])
        vega=ff[active]*rt*INV_ROOT2PI*np.exp(-.5*d1*d1)
        with np.errstate(divide='ignore',invalid='ignore',over='ignore'):
            damp=np.clip(1-error*d1*d2/(2*s*vega),.3,3.)
            proposed=s-error/(vega*damp)
        inside=np.isfinite(proposed)&(proposed>lo[active])&(proposed<hi[active])
        sigma[active]=np.where(inside,proposed,.5*(lo[active]+hi[active]))
    return sigma,done,iterations


def invert_and_greeks(f,k,t,p,cp,rate,spot=None,max_iter=60,rtol_price=1e-5):
    """Vectorized IV + delta/gamma/vega, NaN when invalid.

    Input arrays must fit available memory; caller chunks at <=100k rows.
    Greeks are derivatives w.r.t. model forward/spot proxy, NOT futures fills.
    report['fallback'] counts quotes actually sent to Brent.
    """
    f,k,t,p,rate=np.broadcast_arrays(*(np.asarray(x,dtype='float64') for x in (f,k,t,p,rate)))
    cp=np.broadcast_to(np.asarray(cp),f.shape)
    shape=f.shape
    call=cp=='CE'
    with np.errstate(invalid='ignore',over='ignore'):
        disc=np.exp(-rate*t)
        low=disc*np.where(call,np.maximum(f-k,0),np.maximum(k-f,0))
        high=disc*np.where(call,f,k)
        valid=(np.isfinite(f)&np.isfinite(k)&np.isfinite(t)&np.isfinite(p)&np.isfinite(rate)&
            (f>0)&(k>0)&(t>1e-8)&(p>low+1e-5)&(p<high-1e-5))
    iv=np.full(shape,np.nan);delta=np.full(shape,np.nan);gamma=np.full(shape,np.nan);vega=np.full(shape,np.nan)
    inds=np.flatnonzero(valid.ravel())
    if len(inds)==0:return iv,delta,gamma,vega,dict(valid=0,fallback=0,fail=0,iterations=0)
    ff=f.ravel()[inds];kk=k.ravel()[inds];tt=t.ravel()[inds];pp=p.ravel()[inds]
    rr=rate.ravel()[inds];cc=call.ravel()[inds];df=np.exp(-rr*tt);ll=low.ravel()[inds]
    st=np.sqrt(tt);lfk=np.log(ff/kk)
    q=np.where(kk>=ff,1.,-1.)          # the OTM side, by parity carries the same vol
    otm=(pp-ll)/df
    # A quote outside the solver's vol range cannot be inverted; settle that up front.
    at_lo=black_vector(ff,kk,tt,VOL_LO,q>0,0.);at_hi=black_vector(ff,kk,tt,VOL_HI,q>0,0.)
    reachable=(otm>=at_lo)&(otm<=at_hi)
    inflect=np.sqrt(2*np.abs(lfk)/tt)
    atm_seed=np.sqrt(2*np.pi/tt)*otm/(.5*(ff+kk))
    sigma=np.clip(np.where(np.abs(lfk)<.002,atm_seed,inflect),1e-3,4.)
    solved=np.zeros(len(ff),dtype=bool);iterations=0
    if reachable.any():
        sub=np.flatnonzero(reachable)
        s,done,iterations=_bracketed_solve(ff[sub],kk[sub],tt[sub],otm[sub],q[sub],sigma[sub].copy(),max_iter,rtol_price*.001)
        sigma[sub]=s;solved[sub]=done
    sigma[~reachable]=np.nan
    fail=reachable&(~solved|(np.abs(black_vector(ff,kk,tt,sigma,cc,rr)-pp)>rtol_price))
    nfallback=int(fail.sum())
    for i in np.flatnonzero(fail):
        fn=lambda x:float(black_vector(ff[i],kk[i],tt[i],x,cc[i],rr[i])-pp[i])
        try:sigma[i]=brentq(fn,VOL_LO,VOL_HI,xtol=1e-11)
        except ValueError:sigma[i]=np.nan
    good=np.isfinite(sigma)
    good&=np.abs(black_vector(ff,kk,tt,np.where(good,sigma,1.),cc,rr)-pp)<=rtol_price
    sigma[~good]=np.nan
    w=np.maximum(sigma,1e-10)*st;d1=(lfk+.5*w*w)/w
    pdf=INV_ROOT2PI*np.exp(-.5*d1*d1)
    div=df*(ndtr(d1)-(~cc))
    gam=df*pdf/(ff*w)
    vg=df*ff*st*pdf
    if spot is not None:
        spotarr=np.broadcast_to(np.asarray(spot,dtype=float),shape).ravel()[inds]
        fac=np.divide(ff,spotarr,out=np.full_like(ff,np.nan),where=spotarr>0)
        div*=fac;gam*=fac*fac
    iv.ravel()[inds]=sigma;delta.ravel()[inds]=np.where(good,div,np.nan)
    gamma.ravel()[inds]=np.where(good,gam,np.nan);vega.ravel()[inds]=np.where(good,vg,np.nan)
    return iv,delta,gamma,vega,dict(valid=int(len(inds)),fallback=nfallback,fail=int((~good).sum()),iterations=iterations)
