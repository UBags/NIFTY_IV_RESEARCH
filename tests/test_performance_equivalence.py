"""Randomized parity checks for performance-only changes in paths and rules (contributed by ChatGPT, adapted to V4.9.13).

The reference implementations below deliberately retain the pre-optimization
calculations, independently of the optimized functions under test.
"""
import collections
import math
import unittest
from types import SimpleNamespace

import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src")]
import numpy as np
from nifty_iv.zone import paths as zp
from nifty_iv.zone import rules as zr


def old_slice(sim, day, i0, i1, c, want, lot, buy=True):
    """V4.9.12 PathSim.slice before DayArrays capacity caching."""
    d=sim.arrays(day);k=sim.key(c);px=(d.buy if buy else d.sell).get(k)
    if px is None or i0>=i1 or want<=0:return 0,0.,0.,None
    vol=d.vol[k][i0:i1];cap=np.floor(vol*sim.cfg.max_participation)//lot*lot;cs=np.cumsum(cap)
    total=int(cs[-1])
    if total<=0:return 0,0.,0.,None
    if total<want:j=int(np.flatnonzero(cap)[-1]);taken=cap
    else:
        j=int(np.searchsorted(cs,want));taken=cap[:j+1].copy();taken[j]=want-(cs[j-1] if j else 0.)
    n=int(taken.sum());p=px[i0:i0+len(taken)]
    return n,float(taken@p),float(vol[:len(taken)][taken>0].sum()),i0+j


def random_batch(rng, npaths=32, length=60):
    lengths=rng.integers(1,length+1,size=npaths,dtype=np.int64)
    k=np.minimum(rng.integers(0,20,(npaths,1))+np.arange(length)[None,:],length+25)
    s=rng.choice([-1.,1.],size=npaths)
    e=rng.uniform(100,300,size=npaths)
    close=rng.uniform(100,300,size=(npaths,length))
    close[rng.random(close.shape)<.04]=np.nan
    atr=rng.uniform(.1,5,size=(npaths,length))
    atr[rng.random(atr.shape)<.08]=np.nan
    arrlen=length+26
    low={L:rng.uniform(50,200,arrlen) for L in zr.LOOKBACKS}
    high={L:rng.uniform(200,350,arrlen) for L in zr.LOOKBACKS}
    for L in zr.LOOKBACKS:
        low[L][rng.integers(0,arrlen,3)]=np.nan
        high[L][rng.integers(0,arrlen,3)]=np.nan
    bars=SimpleNamespace(prev_low=low,prev_high=high)
    b=SimpleNamespace(Np=npaths,Lmax=length,L=lengths,valid=np.arange(length)[None,:]<lengths[:,None],
                      sign=s,e0=e,c=close,K=k,bars=bars,atr_now=lambda p:atr)
    return b


def old_rolling_hits(labels,b):
    """Original per-rule chandelier/Donchian calculations (including NaN and ties)."""
    S=b.sign[:,None];out=[]
    for label in labels:
        kind,L,p=zr.parse(label)
        if kind=='chand':
            a=np.nan_to_num(b.atr_now(15),nan=np.inf)
            pad=np.concatenate([np.repeat((b.sign*b.e0)[:,None],L-1,1),S*b.c],1)
            roll=np.lib.stride_tricks.sliding_window_view(pad,L,axis=1).max(-1)
            m=(S*b.c)<roll-p*a
        elif kind=='donch':
            a=np.nan_to_num(b.atr_now(15),nan=np.inf)
            lo=b.bars.prev_low[L][b.K];hi=b.bars.prev_high[L][b.K]
            cush=p*a if p else 0.
            m=np.where(S>0,b.c<lo-cush,b.c>hi+cush)
        else:raise AssertionError(label)
        m=m&b.valid
        out.append(np.where(m.any(-1),m.argmax(-1),b.L-1))
    return np.array(out,dtype=np.int64)


class PerformanceEquivalence(unittest.TestCase):
    def test_cached_capacities_match_old_slice_random(self):
        rng=np.random.default_rng(20261123)
        key=('2024-01-25',24000.,'PE');day='2024-01-02'
        d=zp.DayArrays.__new__(zp.DayArrays)

        d.vol={key:rng.uniform(0,9000,zp.NMIN)}
        d.vol[key][::11]=0.
        d.buy={key:rng.uniform(1,200,zp.NMIN)}
        d.sell={key:rng.uniform(1,200,zp.NMIN)}
        d.px=d.buy
        sim=zp.PathSim.__new__(zp.PathSim)
        sim.days=collections.OrderedDict([(day,d)]);sim.cache_days=1
        sim.cfg=SimpleNamespace(max_participation=.20)
        for _ in range(450):
            i0=int(rng.integers(0,zp.NMIN));i1=min(zp.NMIN,i0+int(rng.integers(0,100)))
            lot=int(rng.choice([25,50,65,75]));want=int(rng.integers(0,150))*lot
            sim.cfg.max_participation=float(rng.choice([.1,.2,.25,.33]))
            buy=bool(rng.integers(2))
            ref=old_slice(sim,day,i0,i1,key,want,lot,buy)
            new=sim.slice(day,i0,i1,key,want,lot,buy)
            self.assertEqual(new,ref)  # exact float equality, not allclose
            if i0>=i1:
                self.assertIsNone(sim.caps(day,i0,i1,key,lot,buy))
                continue
            got=sim.caps(day,i0,i1,key,lot,buy)[0]
            old=np.floor(d.vol[key][i0:i1]*sim.cfg.max_participation)//lot*lot
            np.testing.assert_array_equal(got,old)
            if len(got):
                got[0]=-1234 # returned caps are still independent writable arrays
                np.testing.assert_array_equal(sim.caps(day,i0,i1,key,lot,buy)[0],old)
        # The cache is a property of the evictable trading-day object, not global state.
        self.assertLessEqual(len(d.cap),4*4)

    def test_rolling_stops_equal_old_random_with_padding_and_nan(self):
        rng=np.random.default_rng(20261123)
        labels=[f'chand{L}x{m:g}' for L in zr.LOOKBACKS for m in (1.,1.5,2.,2.5,3.,4.)]
        labels += [f'donch{L}c{m:g}' for L in zr.LOOKBACKS for m in (0.,.25,.5,1.)]
        for _ in range(12):
            b=random_batch(rng,npaths=int(rng.integers(2,35)),length=int(rng.integers(10,80)))
            np.testing.assert_array_equal(zr.evaluate(labels,b),old_rolling_hits(labels,b))
            order=rng.permutation(len(labels))
            np.testing.assert_array_equal(zr.evaluate([labels[i] for i in order],b),old_rolling_hits([labels[i] for i in order],b))


if __name__=='__main__':unittest.main()
