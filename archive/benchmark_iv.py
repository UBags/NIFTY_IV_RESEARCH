#!/usr/bin/env python3
"""Measure actual local IV solver speed and reconstruction accuracy (not promises)."""
import argparse,math,time
import numpy as np
from quant_v2 import black_forward,implied_vol,implied_vol_batch

def main():
 p=argparse.ArgumentParser();p.add_argument('--samples',type=int,default=3000);a=p.parse_args()
 rng=np.random.default_rng(20261108)
 f=rng.uniform(12000,30000,a.samples);k=f*rng.uniform(.94,1.06,a.samples)
 t=rng.uniform(4/365,120/365,a.samples);v=rng.uniform(.1,.4,a.samples)
 cp=np.where(rng.random(a.samples)<.5,'PE','CE')
 premium=np.array([black_forward(*q) for q in zip(f,k,t,v,cp)])
 before=time.perf_counter();vector=implied_vol_batch(f,k,t,premium,cp);vec_time=time.perf_counter()-before
 before=time.perf_counter();scalar=np.array([implied_vol(*q) for q in zip(f,k,t,premium,cp)]);scalar_time=time.perf_counter()-before
 mask=np.isfinite(vector)&np.isfinite(scalar)
 error=np.max(abs(vector[mask]-scalar[mask])) if mask.any() else float('nan')
 print('samples',a.samples,'valid_count',int(mask.sum()),'IV_max_difference',error)
 print('scalar_sec',round(scalar_time,5),'batch_sec',round(vec_time,5),
       'observed_speedup_x',round(scalar_time/vec_time,3) if vec_time else None)
 print('NOT transferable without benchmarking your own dataset, contract tails, and hardware')
if __name__=='__main__':main()
