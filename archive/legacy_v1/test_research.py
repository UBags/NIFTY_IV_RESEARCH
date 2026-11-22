#!/usr/bin/env python3
"""Synthetic end-to-end tests. NO REAL market profitability claims."""
import datetime as dt
import gzip
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from scipy.stats import norm

import prepare_research_cache as prep
import calendar_research as eng
import rank_strategies as ranking

ROOT=Path(__file__).resolve().parent


def fake_cache(path):
 db=sqlite3.connect(path)
 db.executescript(prep.SCHEMA)
 db.executemany('INSERT INTO meta VALUES(?,?)', [('cache_complete','1'),('range_start','2026-04-01'),('range_end','2026-04-07'),('minutes','09:45,09:46,15:29')])
 dates=['2026-04-01','2026-04-02','2026-04-06','2026-04-07']
 spot=25000.
 sid=1
 for day in dates:
  for tm in ['09:45','09:46','15:29']:
   ts=f'{day} {tm}:00'
   db.execute('INSERT INTO spot(ts,day,open,close,volume,source_id) VALUES(?,?,?,?,?,?)',
       (ts,day,spot,spot,0,sid))
   for exp in ['2026-04-07','2026-04-14','2026-05-05','2026-06-30']:
    if exp<day:continue
    t=eng.texp(exp,eng.at(day,tm))
    for k in [24500.,24900.,25000.,25100.,25500.]:
     for cp in ('PE','CE'):
      v=.25 if exp=='2026-04-07' else .18
      px=max(.10,eng.bs(spot,k,t,v,cp,.06,.015))
      db.execute('''INSERT INTO opt(provider,ts,day,expiry,strike,cp,
         open,high,low,close,volume,oi,source_id,source_row)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
         ('rissin_upstox',ts,day,exp,k,cp,px,px,px,px,1000,5000,sid,0))
 db.commit();db.close()

class StrategyTests(unittest.TestCase):
 def test_iv_inverts_black_scholes(self):
  s=25000;k=25000;t=20/365.25;r=.06;q=.015
  for cp in ('PE','CE'):
   x=eng.bs(s,k,t,.23,cp,r,q)
   got=eng.implied_vol(s,k,t,x,cp,r,q)
   self.assertAlmostEqual(got,.23,places=5)

 def test_full_weekly_and_gamma(self):
  with tempfile.TemporaryDirectory() as d:
   cache=Path(d)/'market.sqlite';fake_cache(cache)
   data=eng.Data(str(cache))
   cfg=SimpleNamespace(provider='rissin_upstox',rate=.06,dividend=.015,
       min_fill_volume=1,min_exit_volume=1,min_signal_volume=1,min_mark_volume=1,
       slippage=.5,brokerage=20,gst=.18,stt=.0015,stamp=.00003,
       exchange_bps=3.5,lot_size=65,hedge_bps=2.0,max_gamma_lots=3,
       delta_band=.1,min_debit=.01,iv_convergence=1.0,
       weekdays={2,3,4},start='2026-04-01',end='2026-04-07',
       round_steps=[100],offsets=[0],long_tenors=[90],
       tenor_tolerance=4,max_short_dte=8)
   r=list(eng.candidates(data,cfg))
   self.assertGreater(len(r),0)
   cand=r[0][0]
   self.assertEqual(cand['day'],'2026-04-01')
   self.assertEqual(cand['short_expiry'],'2026-04-07')
   self.assertEqual(cand['long_expiry'],'2026-06-30')
   self.assertIn('iv_5',cand)
   plain,err=eng.simulate(data,cfg,cand,'none','weekly')
   self.assertIsNone(err);self.assertEqual(plain['exit_day'],'2026-04-07')
   self.assertTrue(math.isfinite(plain['pnl_points']))
   dlt,err=eng.simulate(data,cfg,cand,'delta_daily_proxy','weekly')
   self.assertIsNone(err);self.assertTrue(math.isfinite(dlt['pnl_points']))
   gamma,err=eng.simulate(data,cfg,cand,'gamma_integer_delta_daily_proxy','weekly')
   self.assertIsNone(err);self.assertGreater(gamma['gamma_call_lots'],0)
   short,err=eng.simulate(data,cfg,cand,'none','p25_s50')
   self.assertIsNone(err)
   carry,err=eng.simulate_carry30(data,cfg,cand)
   self.assertIsNotNone(err) # no future 30DTE available in synthetic data
   self.assertEqual(err,'missing_carry_exit_day')
   data.close()

 def test_optimize_portfolio_overlap(self):
  rows=[{'day':'2026-04-01','exit_day':'2026-04-07','pnl_points':10,'entry_debit_points':100},
        {'day':'2026-04-02','exit_day':'2026-04-07','pnl_points':20,'entry_debit_points':100},
        {'day':'2026-04-06','exit_day':'2026-04-07','pnl_points':30,'entry_debit_points':100}]
  st=ranking.portfolio_stats(rows,2,{})
  self.assertEqual(st['count'],2)
  self.assertEqual(st['total_points'],30.)

if __name__=='__main__':unittest.main(verbosity=2)
