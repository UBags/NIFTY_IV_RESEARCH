import sys,unittest,tempfile,sqlite3,datetime as dt,math
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
from nifty_iv.data.research_cache import DDL,TIMES
from nifty_iv.ivcal.quant_v3 import parity_ols,trailing_rv,black_forward
from nifty_iv.core.contract_specs import rule_lot_size
from nifty_iv.ivcal.calendar_research import Data,simulate,estimate_mark
from nifty_iv.ivcal.candidate_core import generate
from nifty_iv.ivcal.rank_strategies import portfolio,fold_ranges,choose
from collections import Counter


def fixture(path,missing_mark=False,missing_exit=False,bull=False, long=None,end=None):
    db=sqlite3.connect(path);db.executescript(DDL)
    db.executemany('INSERT INTO meta VALUES(?,?)',[
        ('version','3'),('research_eligible','1'),('cache_complete','1'),
        ('minutes',','.join(TIMES)),('source_fingerprint','synthetic'),('enrichment_complete','0')])
    start=dt.date(2026,1,7);end=end or dt.date(2026,4,29);long=long or dt.date(2026,4,28)
    ix=0;spots=[];bars=[];daily=[]
    day=start
    while day<=end:
        if day.weekday()<5:
            date=day.isoformat();j=(day-start).days
            s=25000+((0.005*j*25000) if bull else 0)
            daily.append((date,s,s+100,s-100,s,375,1,1))
            expiries=[long]
            for w in range(0,3):
                exp=day+dt.timedelta(days=(1-day.weekday())%7+7*w)
                if exp<=long and exp not in expiries:expiries.append(exp)
            for minute in TIMES:
                ts=f'{date} {minute}:00';spots.append((ts,date,s,s,0,1,ix));ix+=1
                for exp in expiries:
                    t=(dt.datetime.combine(exp,dt.time(15,30))-dt.datetime.combine(day,dt.time.fromisoformat(minute))).total_seconds()/(365.25*86400)
                    if t<=0:continue
                    for k in [24000.,24500.,25000.,25500.,26000.]:
                        for cp in ['PE','CE']:
                            p=black_forward(s,k,t,.17 if exp==long else .20,cp,.06)
                            p=max(.05,p)
                            if missing_mark and date=='2026-01-09' and minute=='15:29' and exp==long and k==25000. and cp=='PE':continue
                            if missing_exit and date=='2026-01-13' and minute=='15:16' and exp==long and k==25000. and cp=='PE':continue
                            bars.append(('rissin_upstox',ts,date,exp.isoformat(),k,cp,p,p,p,p,100,500,2,ix));ix+=1
        day+=dt.timedelta(days=1)
    db.executemany('INSERT INTO spot_daily VALUES(?,?,?,?,?,?,?,?)',daily)
    db.executemany('INSERT INTO spot VALUES(?,?,?,?,?,?,?)',spots)
    db.executemany('INSERT INTO opt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',bars)
    db.commit();db.close()


def config(**kw):
    values=dict(provider='rissin_upstox',start='2026-01-07',end='2026-01-10',
         weekdays={2,3,4},max_short_dte=8,round_steps=[500],strike_mode='points',
         strike_values=[0.],long_tenors=[120],tenor_tolerance=25,option_type='PE',
         rate=.06,min_signal_volume=1,min_pair_volume=1,max_parity_bps=40,
         min_fill_volume=1,min_exit_volume=1,min_mark_volume=0,
         slippage=.1,slippage_mode='flat',brokerage=0.,gst=0.,exchange_bps=0.,stt=0.,stamp=0.,
         hedge_bps=0.,delta_band=.1,max_gamma_lots=3,iv_convergence=1.,min_debit=1.,
         max_mark_age_days=5,max_interp_gap=1000.,mark_stress=.2,roll_step=500,
         max_diagonal_gap=500,recenter_threshold=.02,double_z=.75,capital_scenario_pct=.15,
         lot_master=SimpleNamespace(get=lambda d,e:rule_lot_size(d,e)))
    values.update(kw);return SimpleNamespace(**values)

class V3Tests(unittest.TestCase):
    def make(self,**kw):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'synthetic.sqlite';fixture(path,**kw)
        d=Data(str(path));self.addCleanup(d.close)
        return d

    def candidate(self,d,c=None):
        c=c or config()
        entries=list(generate(d,c,Counter()))
        self.assertGreater(len(entries),0,'No generated candidate')
        return entries[0]

    def test_forward_regression(self):
        spot=25000;f=25070;t=80/365.;v=.21;r=.065
        quotes={}
        for k in (24600,24800,25000,25200,25400):
            for cp in ('PE','CE'):
                quotes[float(k),cp]=dict(close=black_forward(f,k,t,v,cp,r),volume=200)
        q=parity_ols(quotes,spot,t,.06,max_residual_bps=1.)
        self.assertEqual(q.quality,'fit_ok');self.assertAlmostEqual(q.forward,f,places=5)
        self.assertAlmostEqual(q.rate,r,places=7)

    def test_no_lookahead_in_trailing_rv(self):
        rows=[dict(day=f'2026-01-{i:02d}',open=100+i,high=102+i,low=99+i,close=101+i,complete_session=1) for i in range(1,9)]
        v1=trailing_rv(rows[:7],'parkinson',5)
        rows[7]['high']=10000
        self.assertEqual(v1,trailing_rv(rows[:7],'parkinson',5))

    def test_missing_mark_does_not_delete_trade(self):
        d=self.make(missing_mark=True);c=config();cand=self.candidate(d,c)
        outcome,error=simulate(d,c,cand,'carry30','none','put')
        self.assertIsNone(error);r,marks=outcome
        self.assertGreater(len(marks),4)
        self.assertTrue(any('interpolated' in m['quality'] or 'model' in m['quality']
                            or 'conservative' in m['quality'] for m in marks))
        self.assertNotEqual(r['status'],'unresolved')

    def test_settlement_proxy_on_missing_liquid_close(self):
        d=self.make();c=config();cand=self.candidate(d,c)
        # Remove short expiry buyback candle at 15:16 for entry contract
        d.db.execute('DELETE FROM opt WHERE provider=? AND ts=? AND expiry=? AND strike=? AND cp=?',
            (c.provider,cand['short_expiry']+' 15:16:00',cand['short_expiry'],cand['strike'],'PE'))
        d.db.commit();d.opt.cache_clear();d.surface.cache_clear()
        outcome,error=simulate(d,c,cand,'weekly','none','put')
        self.assertIsNone(error)
        result,marks=outcome
        self.assertEqual(result['status'],'proxy_settled')
        self.assertTrue(any(x['event'].startswith('settlement_proxy') for x in result['ledger']))

    def test_rolling_and_static(self):
        d=self.make();c=config();cand=self.candidate(d,c)
        for policy in ('weekly','rolling_static','rolling_diagonal','rolling_recenter'):
            outcome,error=simulate(d,c,cand,policy,'none','put')
            self.assertIsNone(error,policy+': '+str(error))
            result,marks=outcome
            self.assertGreater(len(marks),0)
            self.assertTrue(result['short_sales']>=1)
        self.assertGreater(simulate(d,c,cand,'rolling_static','none','put')[0][0]['short_sales'],1)

    def test_call_and_double(self):
        d=self.make();c=config();cand=self.candidate(d,c)
        for structure in ('put','call','double'):
            outcome,error=simulate(d,c,cand,'weekly','none',structure)
            self.assertIsNone(error,structure+': '+str(error))
            self.assertGreater(outcome[0]['executed_fills'],0)

    def test_portfolio_includes_flat_days_and_mtm(self):
        dates=['2026-01-07','2026-01-08','2026-01-09','2026-01-12','2026-01-13']
        tr=[dict(id='A',day=dates[0],exit_day=dates[-1],pnl_inr=200,entry_capital=1000)]
        marks={'A':{dates[0]:dict(base=-100,lower=-120,upper=-80,capital=1000),
             dates[1]:dict(base=-900,lower=-1000,upper=-800,capital=1000),
             dates[2]:dict(base=0,lower=-100,upper=100,capital=1000),
             dates[-1]:dict(base=200,lower=200,upper=200,capital=0)}}
        m=portfolio(tr,marks,dates,max_open=1,max_capital=None)
        self.assertEqual(m['n'],1);self.assertEqual(m['days'],5)
        self.assertGreaterEqual(m['max_dd'],800)
        self.assertEqual(m['mark_missing'],1)

    def test_purge_after_campaign_end(self):
        days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(80)]
        self.assertEqual(len(fold_ranges(days,5)),5)
        rows=[dict(id=str(x),day=days[0],exit_day=days[70],entry_capital=1000) for x in range(3)]
        self.assertEqual(len(choose(rows,2)),2)


    def test_unresolved_remains_in_ledger(self):
        d=self.make();c=config();cand=self.candidate(d,c)
        d.valuation_days=[x for x in d.valuation_days if x<='2026-01-09']
        outcome,error=simulate(d,c,cand,'weekly','none','put')
        self.assertIsNone(error)
        r,marks=outcome
        self.assertEqual(r['status'],'unresolved')
        self.assertIsNotNone(r['pnl_inr'])
        self.assertTrue('HORIZON_CONSERVATIVE' in marks[-1]['quality'])
        self.assertGreater(len(r['ledger']),0)

    def test_verified_settlement_override(self):
        from nifty_iv.ivcal.settlements import SettlementMaster
        d=self.make();c=config();cand=self.candidate(d,c)
        d.db.execute('DELETE FROM opt WHERE provider=? AND ts=? AND expiry=? AND strike=? AND cp=?',
            (c.provider,cand['short_expiry']+' 15:16:00',cand['short_expiry'],cand['strike'],'PE'))
        d.db.commit();d.opt.cache_clear();d.surface.cache_clear()
        path=Path(self.temp.name)/'settlements.csv'
        path.write_text('expiry,settlement_price,evidence_url\n'+cand['short_expiry']+',24900,TEST_OFFICIAL_FIXTURE\n')
        c.settlement_master=SettlementMaster(path)
        outcome,error=simulate(d,c,cand,'weekly','none','put')
        self.assertIsNone(error)
        result,marks=outcome
        self.assertEqual(result['status'],'closed_verified_settlement')
        self.assertTrue(any(x['event']=='verified_official_settlement' for x in result['ledger']))

    def test_gamma_option_reduction_is_not_exact_neutral(self):
        d=self.make();c=config();cand=self.candidate(d,c)
        outcome,error=simulate(d,c,cand,'weekly','gamma_reduced_delta_proxy','put')
        self.assertIsNone(error)
        r,marks=outcome
        self.assertGreater(r['gamma_reduction_pct'],0.)
        self.assertEqual(r['execution_class'],'theoretical_delta')
        self.assertIsNotNone(r['entry_gamma_proxy'])

    def test_solver_reconstructs_and_invalid_bounds(self):
        import numpy as np
        from nifty_iv.ivcal.quant_fast_v3 import invert_and_greeks
        f=np.array([25000.,25000.,25000.,25000.]);k=np.array([25000.,23000.,26000.,25000.])
        t=np.array([7,45,90,7])/365.25
        cp=np.array(['PE','CE','PE','PE']);r=np.array([.06]*4);v=np.array([.2,.35,.12,.2])
        px=np.array([black_forward(ff,kk,tt,vol,c,rate) for ff,kk,tt,vol,c,rate in zip(f,k,t,v,cp,r)])
        px[3]=25000  # violates Black put upper bound
        iv,de,ga,ve,stats=invert_and_greeks(f,k,t,px,cp,r)
        self.assertTrue(np.allclose(iv[:3],v[:3],atol=1e-7))
        self.assertTrue(math.isnan(iv[3]))

if __name__=='__main__':unittest.main()
