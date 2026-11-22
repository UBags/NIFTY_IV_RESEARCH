#!/usr/bin/env python3
"""Synthetic IV numerical comparison; does not forecast real-dataset runtime."""
from time import perf_counter
import numpy as np
from quant_v3 import black_forward,implied_vol
from quant_fast_v3 import invert_and_greeks

def main():
    r=np.random.default_rng(142)
    n=2000;f=r.uniform(15000,27000,n);k=f*np.exp(r.uniform(-.06,.06,n))
    t=r.uniform(3/365,115/365,n);v=r.uniform(.08,.40,n)
    cp=np.where(r.random(n)>.5,'CE','PE');rate=r.uniform(.04,.09,n)
    prices=np.array([black_forward(a,b,c,d,e,z) for a,b,c,d,e,z in zip(f,k,t,v,cp,rate)])
    started=perf_counter();baseline=[implied_vol(*x) for x in zip(f,k,t,prices,cp,rate)];scalar=perf_counter()-started
    started=perf_counter();ivs,delta,gamma,vega,stats=invert_and_greeks(f,k,t,prices,cp,rate);fast=perf_counter()-started
    comparable=np.isfinite(ivs)&np.array([x is not None for x in baseline])
    max_difference=max(abs(ivs[i]-baseline[i]) for i in np.flatnonzero(comparable))
    print(dict(synthetic_quotes=n,valid=int(comparable.sum()),scalar_seconds=round(scalar,3),
        vector_seconds=round(fast,3),speedup=round(scalar/max(fast,1e-9),2),
        max_iv_difference=max_difference,fallback=stats['fallback']))
    assert max_difference<1e-6
    # Deep wings, sub-day expiries, exchange tick rounding: where an unbracketed solver falls back.
    n=20000;f=r.uniform(15000,27000,n);k=f*np.exp(r.uniform(-.25,.25,n));t=r.uniform(.5/365,115/365,n);v=r.uniform(.05,1.2,n)
    cp=np.where(r.random(n)>.5,'CE','PE');rate=r.uniform(.04,.09,n)
    prices=np.maximum(.05,np.round(np.array([black_forward(a,b,c,d,e,z) for a,b,c,d,e,z in zip(f,k,t,v,cp,rate)])/.05)*.05)
    started=perf_counter();ivs,*_,stats=invert_and_greeks(f,k,t,prices,cp,rate);fast=perf_counter()-started
    print(dict(wing_quotes=n,**stats,seconds=round(fast,3)))
    assert stats['fallback']<=n*.001

if __name__=='__main__':main()
