"""Dependence-aware inference on daily MTM: stationary bootstrap and Hansen's SPA test.

Works on training-window daily P&L only; nothing here reads the holdout.
"""
from __future__ import annotations
import math
import numpy as np

def stationary_indices(n,reps,mean_block,rng):
    """Politis-Romano (1994) resampling indices, shape (reps,n)."""
    p=1./max(mean_block,1.)
    start=rng.integers(0,n,size=(reps,n))
    extend=rng.random((reps,n))>=p
    idx=start.copy()
    for t in range(1,n):
        idx[:,t]=np.where(extend[:,t],(idx[:,t-1]+1)%n,start[:,t])
    return idx


def bootstrap_summary(daily,reps=2000,mean_block=10,seed=20240):
    """5th-pct annualized Sharpe and 95th-pct max drawdown of a daily MTM increment series."""
    x=np.asarray(daily,dtype=float)
    if len(x)<20 or reps<100:return None
    idx=stationary_indices(len(x),reps,mean_block,np.random.default_rng(seed))
    sample=x[idx]
    sd=sample.std(axis=1,ddof=1)
    sharpe=np.divide(sample.mean(axis=1),sd,out=np.zeros(reps),where=sd>1e-12)*math.sqrt(252)
    equity=sample.cumsum(axis=1)
    peak=np.maximum(np.maximum.accumulate(equity,axis=1),0.)   # matches rank_strategies.max_drawdown: peak starts at 0
    drawdown=(peak-equity).max(axis=1)
    return dict(sharpe_p05=float(np.quantile(sharpe,.05)),sharpe_median=float(np.median(sharpe)),
                max_dd_p95=float(np.quantile(drawdown,.95)),reps=reps,mean_block=mean_block)


def batch_means_sd(x,length):
    """Long-run sd of sqrt(T)*mean for each model from non-overlapping blocks. x is (...,T,K)."""
    T=x.shape[-2];n=T//length
    blocks=x[...,:n*length,:].reshape(*x.shape[:-2],n,length,x.shape[-1]).mean(axis=-2)
    return np.sqrt(length*blocks.var(axis=-2,ddof=1))


def spa_test(differences,reps=2000,mean_block=10,seed=20241,memory_mb=256):
    """Hansen (2005) test: H0 = no model beats the benchmark. differences is (T,K), positive = model better.

    Bootstrap-t: each replicate is studentized by its own batch-means variance, because a fixed sample
    variance over-rejected badly on overlapping campaigns (see nifty-iv-calibrate-spa and CHANGES_V4.md).
    A small p-value says the best model's excess survives the search; it does not say which model will persist.
    """
    d=np.asarray(differences,dtype=float)
    if d.ndim!=2 or d.shape[0]<20 or d.shape[1]<1 or reps<100:return None
    T=d.shape[0];length=max(2,int(round(mean_block)))
    if T//length<2:return None
    omega=batch_means_sd(d,length);usable=omega>1e-12
    if not usable.any():return None
    d=d[:,usable];omega=omega[usable];dbar=d.mean(axis=0)
    root=math.sqrt(T);stat=max(float((root*dbar/omega).max()),0.)
    threshold=-omega*math.sqrt(2*math.log(math.log(T))/T)
    centre_consistent=np.where(dbar>=threshold,dbar,0.)
    idx=stationary_indices(T,reps,mean_block,np.random.default_rng(seed))
    chunk=max(1,int(memory_mb*2**20/(T*d.shape[1]*8)))
    hits_consistent=hits_upper=0
    for i in range(0,reps,chunk):
        sample=d[idx[i:i+chunk]];m=sample.mean(axis=1)
        own=batch_means_sd(sample,length);own=np.where(own>1e-12,own,omega)
        hits_consistent+=int(((root*(m-centre_consistent)/own).max(axis=1).clip(min=0)>=stat).sum())
        hits_upper+=int(((root*(m-dbar)/own).max(axis=1).clip(min=0)>=stat).sum())
    best=int(np.argmax(root*dbar/omega))
    return dict(p_consistent=hits_consistent/reps,p_upper=hits_upper/reps,statistic=stat,
                models=int(usable.sum()),days=T,best_mean_excess=float(dbar[best]),reps=reps,mean_block=mean_block,studentization='bootstrap_t_batch_means')
