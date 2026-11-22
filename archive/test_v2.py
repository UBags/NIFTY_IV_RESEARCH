import sys,unittest,tempfile,sqlite3,datetime as dt,math,random,os
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,str(Path(__file__).parents[1]))
from quant_v2 import black_forward,implied_forward,implied_vol,implied_vol_batch,forward_variance
from contract_specs_v2 import rule_lot_size
from calendar_research import Data,generate,simulate
from rank_strategies import metrics
from prepare_research_cache import DDL,TIMES,insert_rows
from collections import Counter


def fixture(path):
 db=sqlite3.connect(path);db.executescript(DDL)
 db.executemany('INSERT INTO meta VALUES(?,?)',[
  ('version','2'),('research_eligible','1'),('cache_complete','1'),
  ('minutes',','.join(TIMES)),('source_fingerprint','synthetic'),('enrichment_complete','0')])
 start=dt.date(2026,1,7);end=dt.date(2026,3,31);long=dt.date(2026,4,28)
 spot_rows=[];opt_rows=[];i=0
 for j in range((end-start).days+1):
  day=start+dt.timedelta(days=j)
  if day.weekday()>=5:continue
  s=25000.;date=day.isoformat()
  for minute in TIMES:
   ts=f'{date} {minute}:00'
   spot_rows.append((ts,date,s,s,0,1,i));i+=1
   when=dt.datetime.combine(day,dt.time.fromisoformat(minute))
   exps=[long]
   for n in range(0,24):
    ex=day+dt.timedelta(days=n)
    if ex.weekday()==1 and ex<=long and ex not in exps:exps.append(ex)
    if len(exps)>=4:break
   for ex in exps:
    exp=ex.isoformat();t=(dt.datetime.combine(ex,dt.time(15,30))-when).total_seconds()/(365.25*86400)
    if t<=0:continue
    sig=.14 if ex==long else .15
    for k in [24500.,25000.,25500.]:
     for cp in ['CE','PE']:
      value=black_forward(s,k,t,sig,cp,.06)
      if value<.00001:value=.00001
      opt_rows.append(('rissin_upstox',ts,date,exp,k,cp,value,value,value,value,100,1000,2,i));i+=1
 db.executemany('INSERT INTO spot VALUES(?,?,?,?,?,?,?)',spot_rows)
 db.executemany('INSERT INTO opt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',opt_rows)
 db.executescript('CREATE INDEX optday ON opt(provider,ts,expiry,strike,cp);')
 db.commit();db.close()


def config(**kw):
 c=dict(provider='rissin_upstox',start='2026-01-07',end='2026-01-30',
   weekdays={2,3,4},max_short_dte=8,round_steps=[100,500],strike_mode='points',
   strike_values=[0.],long_tenors=[90,120],tenor_tolerance=20,
   rate=.06,min_signal_volume=1,min_pair_volume=1,max_parity_bps=40,
   min_fill_volume=1,min_exit_volume=1,min_mark_volume=0,
   slippage=.5,brokerage=20.,gst=.18,exchange_bps=3.5,stt=None,stamp=.00003,
   hedge_bps=2.,delta_band=.1,max_gamma_lots=3,iv_convergence=1.,min_debit=1.,
   lot_master=SimpleNamespace(get=lambda d,e:rule_lot_size(d,e)))
 c.update(kw);return SimpleNamespace(**c)

class TestMath(unittest.TestCase):
 def test_parity_and_inversion(self):
  f=25035.;t=30/365.25;k=25000.;v=.21
  c=black_forward(f,k,t,v,'CE');p=black_forward(f,k,t,v,'PE')
  est=implied_forward({(k,'CE'):{'close':c,'volume':10},(k,'PE'):{'close':p,'volume':10},
       (24950.,'CE'):{'close':black_forward(f,24950,t,v,'CE'),'volume':10},
       (24950.,'PE'):{'close':black_forward(f,24950,t,v,'PE'),'volume':10}},
       f,t,max_dispersion_bps=1)
  self.assertEqual(est.quality,'multi_pair');self.assertAlmostEqual(est.forward,f,places=7)
  self.assertAlmostEqual(implied_vol(f,k,t,p,'PE'),v,places=7)
  self.assertAlmostEqual(implied_vol_batch(np.array([f]),np.array([k]),np.array([t]),np.array([p]),np.array(['PE']))[0],v,places=5)
  self.assertIsNone(implied_vol(f,k,t,0.,'PE'))
  self.assertIsNone(forward_variance(.4,.1,90/365,120/365))
 def test_reject_parity_conflict(self):
  f=25000.;t=60/365.25;vol=.15
  quotes={}
  for k in (24900.,25000.):
   for cp in ('CE','PE'):
    quotes[k,cp]=dict(close=black_forward(f,k,t,vol,cp),volume=100)
  quotes[24900.,'PE']['close']+=125.0
  est=implied_forward(quotes,f,t,max_dispersion_bps=5.)
  self.assertEqual(est.quality,'parity_inconsistent')
  self.assertIsNone(est.forward)

 def test_dedup_retains_raw_provenance(self):
  with tempfile.TemporaryDirectory() as folder:
   db=sqlite3.connect(str(Path(folder)/'raw.sqlite'));db.executescript(DDL)
   base=('rissin_upstox','2026-01-07 09:45:00','2026-01-07','2026-01-13',25000.,'PE',150.,150.,150.,150.)
   insert_rows(db,[base+(2,20,10,1),base+(40,20,11,1)],'option')
   self.assertEqual(db.execute('SELECT COUNT(*) FROM opt_raw').fetchone()[0],2)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM opt').fetchone()[0],1)
   self.assertEqual(db.execute('SELECT source_id FROM opt').fetchone()[0],11)
   db.close()

 def test_lots(self):
  self.assertEqual(rule_lot_size('2025-12-11','2025-12-23').qty,75)
  self.assertEqual(rule_lot_size('2025-12-11','2026-01-06').qty,65)  # initial rule estimate; verify contract master
  self.assertEqual(rule_lot_size('2025-12-11','2026-03-31').qty,75)
  self.assertEqual(rule_lot_size('2025-12-31','2026-03-31').qty,65)
  self.assertEqual(rule_lot_size('2026-01-07','2026-01-13').qty,65)
  self.assertEqual(rule_lot_size('2026-01-07','2026-04-28').qty,65)

class TestSimulator(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory()
  cls.path=str(Path(cls.tmp.name)/'fixture.sqlite')
  fixture(cls.path)
  cls.data=Data(cls.path)
 @classmethod
 def tearDownClass(cls):cls.data.close();cls.tmp.cleanup()
 def test_generate_weekly_and_rolling(self):
  cfg=config()
  candidates=list(generate(self.data,cfg,Counter()))
  self.assertTrue(candidates)
  c=next(x for x in candidates if x['day']=='2026-01-07' and x['strike']==25000 and x['long_expiry']=='2026-04-28')
  out,marks=simulate(self.data,cfg,c,'weekly','none')
  self.assertEqual(out['short_sales'],1)
  self.assertEqual(marks[-1][0],out['exit_day'])
  self.assertAlmostEqual(out['pnl_inr'],marks[-1][1],places=6)
  self.assertGreater(len(marks),1)
  out2,marks2=simulate(self.data,cfg,c,'rolling','none')
  self.assertGreater(out2['short_sales'],2)
  self.assertGreater(len(out2['fills']),5)
  self.assertAlmostEqual(out2['option_cash_flow_inr']+out2['proxy_hedge_net_and_cost_inr'],out2['pnl_inr'],places=7)
  self.assertLessEqual((dt.date.fromisoformat(c['long_expiry'])-dt.date.fromisoformat(out2['exit_day'])).days,30)
  self.assertAlmostEqual(out2['pnl_inr'],marks2[-1][1],places=6)
 def test_hedge_modes_do_not_claim_exact_gamma(self):
  cfg=config()
  c=next(x for x in generate(self.data,cfg,Counter()) if x['day']=='2026-01-07' and x['strike']==25000 and x['long_expiry']=='2026-04-28')
  for hedge in ('delta_entry_proxy','delta_daily_proxy','gamma_reduced_delta_proxy'):
   out,marks=simulate(self.data,cfg,c,'weekly',hedge)
   self.assertTrue(out['proxy_hedge'])
   self.assertAlmostEqual(marks[-1][1],out['pnl_inr'],places=7)
  self.assertGreater(out['gamma_call_units'],0)
  self.assertIsNotNone(out['gamma_reduction_pct'])

 def test_portfolio_drawdown_sees_open_loss(self):
  tr=[dict(id='x',day='2026-01-07',exit_day='2026-01-09',pnl_inr=200.,risk_proxy_inr=1000.)]
  marks={'x':{'2026-01-07':-700.,'2026-01-08':-900.,'2026-01-09':200.}}
  result=metrics(tr,marks,1)
  self.assertEqual(result['mtm_dd_inr'],900.)
  self.assertEqual(result['total_inr'],200.)

if __name__=='__main__':unittest.main()
