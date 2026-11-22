#!/usr/bin/env python3
"""Size and power of spa_test on overlapping-campaign P&L (T10). Synthetic; no market data."""
import argparse,numpy as np
from nifty_iv.ivcal.stats import spa_test

def campaigns(T,K,hold,rng):
    """Population-mean-zero daily differences from staggered multi-day positions sharing a factor.

    Do not demean the sample here: that forces the test statistic to zero and fakes perfect size.
    """
    common=rng.standard_t(4,T+hold)/np.sqrt(2)
    x=np.zeros((T,K))
    for k in range(K):
        shock=.5*common+.5*rng.standard_t(4,T+hold)/np.sqrt(2)
        x[:,k]=np.convolve(shock,np.ones(hold)/hold,mode='valid')[:T]
    return x

def trade_book(T,K,hold,rng):
    """Model-minus-baseline daily MTM built from discrete overlapping campaigns, population mean zero.

    Each campaign has its own drift over its life and shares a market factor while open; models
    hold different random subsets of one trade pool. This is the dependence V4 actually produces.
    """
    starts=np.arange(0,T,max(1,hold//3));market=rng.standard_t(4,T)/np.sqrt(2)
    vol=np.exp(np.convolve(rng.normal(0,.25,T+20),np.ones(20)/np.sqrt(20),mode='valid')[:T])
    paths=np.zeros((T,len(starts)))
    for i,a in enumerate(starts):
        b=min(T,a+hold);paths[a:b,i]=vol[a:b]*(.6*market[a:b]*rng.choice((-1,1))+.8*rng.normal(0,1,b-a)+.3*rng.normal(0,1))
    base=rng.random(len(starts))<.5
    out=np.zeros((T,K))
    for k in range(K):
        pick=rng.random(len(starts))<.5
        out[:,k]=paths[:,pick].sum(axis=1)-paths[:,base].sum(axis=1)
    return out

def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--runs',type=int,default=400);a.add_argument('--reps',type=int,default=400)
    a.add_argument('--days',type=int,default=300);a.add_argument('--models',type=int,default=30)
    a=a.parse_args()
    print('process        hold block size@5% size@1% power@5%(+0.25sd/day)')
    for name,fn in (('trade_book',trade_book),('ma_increments',campaigns)):
        for hold in (5,15,30):
            for block in (5,10,20,40):
                c5=c1=pw=0
                for i in range(a.runs):
                    r=spa_test(fn(a.days,a.models,hold,np.random.default_rng(1000+i)),a.reps,block,seed=i)
                    c5+=r['p_consistent']<.05;c1+=r['p_consistent']<.01
                    if i%4==0:
                        x=fn(a.days,a.models,hold,np.random.default_rng(5000+i));x[:,0]+=.25*x.std()
                        pw+=spa_test(x,a.reps,block,seed=i)['p_consistent']<.05
                print(f'{name:14s} {hold:4d} {block:5d} {c5/a.runs:6.3f} {c1/a.runs:6.3f} {pw/(a.runs/4):8.3f}',flush=True)

if __name__=='__main__':main()
