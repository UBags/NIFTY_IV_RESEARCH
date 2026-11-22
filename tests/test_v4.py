"""V4 regression tests. Synthetic only; none of this is evidence of profitability."""
import sys,unittest,tempfile,sqlite3,math,random,decimal
from pathlib import Path
from types import SimpleNamespace
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import os as _os;_os.environ.setdefault('NIFTY_HOLDOUT_LOG',str(Path(tempfile.mkdtemp())/'log.jsonl'))
import numpy as np
from tests.test_v3 import fixture,config
from calendar_research import (delay_mode,campaign_economics,session_time_iv,attribute_leg,trend_fields,execute_fills,Data,simulate,fill_flow,exercise_stt_rate,settlement_reference,
    synchronized_fills,quote_fill)
from candidate_core import generate
from enrich_cache import vendor_concordance
from prepare_research_cache import num,whole,DAILY_SQL,session_quality,month_windows,finalize,FINAL_INDEXES,extraction_config,DDL
import hashlib
import subprocess,json,lineage
from audit_contract_coverage import audit_day
from quant_v3 import yang_zhang_rv,black_forward
from quant_fast_v3 import invert_and_greeks
from calendar_research import parse_event_line,sessions_between,fly_width,scheduled_exit_day,path_digest,month_ranges,contract_digest,mtm_digest,holiday_audit
from rank_strategies import prior_inspections,record_holdout_inspection,ratio,SIGNALS,freeze_mismatch,fixed_holdout_days,freeze_payload,done_by,fast_window,portfolio,GroupMatrix,choose,spa_matrix,basis_weights,nested_folds,persistent_nested_portfolio,fold_window_stats,circular_shift_control
from settlements_v3 import SettlementMaster
import datetime as dt,json,os
from unittest.mock import patch
from stats_v4 import spa_test,bootstrap_summary

def daily(i,o,h,l,c,complete=1,**kw):
    return dict(day=f'2026-01-{i:02d}',open=o,high=h,low=l,close=c,complete_session=complete,**kw)

class Base(unittest.TestCase):
    def make(self,**kw):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'s.sqlite';fixture(path,**kw)
        d=Data(str(path));self.addCleanup(d.close);self.path=path
        return d
    def cand(self,d,c):
        return next(iter(generate(d,c,Counter())))
    def clear(self,d):
        d.db.commit()
        for f in (d.opt,d.surface,d.chain,d.greek,d.forward,d.spot):f.cache_clear()
    def drop(self,d,c,ts,expiry,strike,cp='PE'):
        d.db.execute('DELETE FROM opt WHERE provider=? AND ts=? AND expiry=? AND strike=? AND cp=?',(c.provider,ts,expiry,strike,cp))

class YangZhang(unittest.TestCase):
    def rows(self):
        return [daily(1,100,101,99,100),daily(2,100.5,102,100,101),daily(3,100.8,101.5,99.5,100),
                daily(4,100.2,103,100,102),daily(5,101.5,102.5,100.5,101),daily(6,101.2,104,101,103)]
    def test_hand_computed(self):
        r=self.rows();o=[];c=[];u=[];d=[]
        for a,b in zip(r,r[1:]):
            o.append(math.log(b['open']/a['close']));c.append(math.log(b['close']/b['open']))
            u.append(math.log(b['high']/b['open']));d.append(math.log(b['low']/b['open']))
        k=.34/(1.34+6/4);self.assertAlmostEqual(k,.11971830985,places=9)
        rs=sum(x*(x-z)+y*(y-z) for x,y,z in zip(u,d,c))/5
        total=np.var(o,ddof=1)+k*np.var(c,ddof=1)+(1-k)*rs
        got=yang_zhang_rv(r,5)
        self.assertAlmostEqual(got['rv'],math.sqrt(252*total),places=12)
        self.assertAlmostEqual(got['gap_share'],np.var(o,ddof=1)/total,places=12)
        self.assertIsNone(got['rv1m_range_proxy'])
    def test_gap_never_spans_incomplete_session(self):
        r=self.rows()+[daily(7,103,104,102,103,complete=0),daily(8,50,51,49,50)]
        self.assertEqual(yang_zhang_rv(r,5)['rv'],yang_zhang_rv(self.rows(),5)['rv'])
    def test_no_lookahead(self):
        r=self.rows();v=yang_zhang_rv(r[:5],4);r[5]['high']=9999
        self.assertEqual(v,yang_zhang_rv(r[:5],4))

class Matrix(unittest.TestCase):
    def test_agrees_with_reference_replay_including_stress_and_gaps(self):
        rng=random.Random(5);days=[f'2026-{m:02d}-{d:02d}' for m in (1,2,3) for d in range(1,29)]
        for _ in range(60):
            rows=[];marks={}
            for i in range(rng.randint(3,25)):
                a=rng.randrange(len(days)-3);b=min(len(days)-1,a+rng.randint(0,30));tid=f't{i}';path={};v=0.
                for j in range(a,b+1):
                    v+=rng.gauss(0,500)
                    if j in (a,b) or rng.random()>.15:path[days[j]]=dict(base=v,lower=v-99,upper=v+77,capital=rng.uniform(1,9)*1e4)
                path[days[b]]=dict(base=v,lower=v,upper=v,capital=0.)
                marks[tid]=path;rows.append(dict(id=tid,day=days[a],exit_day=days[b],pnl_inr=v,entry_capital=rng.uniform(1,5)*1e4))
            cap=rng.choice([None,120000.]);mo=rng.randint(1,4);gm=GroupMatrix(rows,marks,days)
            for stress in ('base','lower','upper'):
                ref=portfolio(rows,marks,days,mo,cap,stress);got=gm.evaluate(choose(rows,mo,cap),cap,stress)
                for f in ('n','days','mark_missing','capital_breaches','selected'):self.assertEqual(ref[f],got[f],f)
                for f in ('net','max_dd','capital_scenario_peak','equity_end','entry_capital_peak','worst_daily_increment','win_pct','time_in_market'):
                    self.assertAlmostEqual(ref[f],got[f],places=6,msg=f)
                if ref['sharpe'] is None:self.assertIsNone(got['sharpe'])
                else:self.assertAlmostEqual(ref['sharpe'],got['sharpe'],places=8)
    def test_drawdown_counts_initial_loss(self):
        days=['2026-01-01','2026-01-02','2026-01-03']
        rows=[dict(id='a',day=days[0],exit_day=days[2],pnl_inr=-900,entry_capital=1)]
        marks={'a':{days[0]:dict(base=-500,lower=-500,upper=-500,capital=1),days[1]:dict(base=-800,lower=-800,upper=-800,capital=1),days[2]:dict(base=-900,lower=-900,upper=-900,capital=1)}}
        self.assertEqual(GroupMatrix(rows,marks,days).evaluate(rows)['max_dd'],900)
    def test_stressed_net_uses_stressed_end(self):
        days=['2026-01-01','2026-01-02']
        rows=[dict(id='a',day=days[0],exit_day=days[1],pnl_inr=-50,entry_capital=1)]
        marks={'a':{days[0]:dict(base=0,lower=-10,upper=10,capital=1),days[1]:dict(base=-50,lower=-50,upper=70,capital=1)}}
        self.assertEqual(portfolio(rows,marks,days,1,None,'upper')['net'],70)
        self.assertEqual(GroupMatrix(rows,marks,days).evaluate(rows,None,'upper')['net'],70)

class Solver(unittest.TestCase):
    def data(self):
        r=np.random.default_rng(1);n=4000;f=r.uniform(15000,27000,n);k=f*np.exp(r.uniform(-.2,.2,n));t=r.uniform(1/365,.33,n);v=r.uniform(.06,.9,n)
        cp=np.where(r.random(n)>.5,'CE','PE');rate=r.uniform(.04,.09,n)
        return f,k,t,np.array([black_forward(*x) for x in zip(f,k,t,v,cp,rate)]),cp,rate,v
    def test_accuracy_where_vega_supports_it(self):
        f,k,t,p,cp,rate,v=self.data();iv,*_,rep=invert_and_greeks(f,k,t,p,cp,rate)
        m=np.isfinite(iv);self.assertGreater(m.sum(),3500)
        self.assertLess(np.max(np.abs(np.array([black_forward(*x) for x in zip(f[m],k[m],t[m],iv[m],cp[m],rate[m])])-p[m])),1e-5)
        self.assertEqual(rep['fallback'],0)
    def test_unconverged_quotes_are_counted_and_still_solved(self):
        f,k,t,p,cp,rate,v=self.data();iv,*_,rep=invert_and_greeks(f,k,t,p,cp,rate,max_iter=1)
        self.assertGreater(rep['fallback'],0)
        full,*_=invert_and_greeks(f,k,t,p,cp,rate);m=np.isfinite(iv)&np.isfinite(full)
        self.assertLess(np.max(np.abs(iv[m]-full[m])),1e-4)
    def test_invalid_prices_rejected(self):
        iv,*_,rep=invert_and_greeks(np.array([25000.]),np.array([25000.]),np.array([.02]),np.array([25000.]),np.array(['PE']),np.array([.06]))
        self.assertTrue(math.isnan(iv[0]));self.assertEqual(rep['fallback'],0)

class Stats(unittest.TestCase):
    def test_spa_matrix_is_days_by_models(self):
        self.assertEqual(spa_matrix([np.zeros(150) for _ in range(40)]).shape,(150,40))
    def test_spa_null_vs_edge(self):
        r=np.random.default_rng(3);null=r.normal(0,1,(400,50))
        self.assertGreater(spa_test(null,500)['p_consistent'],.05)
        edge=null.copy();edge[:,7]+=.45
        self.assertLess(spa_test(edge,500)['p_consistent'],.02)
    def test_irrelevant_bad_models_do_not_raise_p(self):
        r=np.random.default_rng(4);good=r.normal(.12,1,(400,5));bad=r.normal(-3,1,(400,200))
        with_bad=spa_test(np.hstack([good,bad]),500);alone=spa_test(good,500)
        self.assertLessEqual(with_bad['p_consistent'],alone['p_consistent']+.03)
        self.assertLessEqual(with_bad['p_consistent'],with_bad['p_upper'])
    def test_bootstrap_widens_with_dependence(self):
        r=np.random.default_rng(9);x=np.zeros(500);e=r.normal(0,1,500)
        for i in range(1,500):x[i]=.7*x[i-1]+e[i]
        short=bootstrap_summary(x,800,1);long_=bootstrap_summary(x,800,20)
        self.assertGreater(long_['max_dd_p95'],short['max_dd_p95'])

class Execution(Base):
    def test_synchronized_entry_shares_a_minute(self):
        d=self.make();c=config();e=self.cand(d,c)
        self.drop(d,c,e['day']+' 09:46:00',e['long_expiry'],e['strike']);self.clear(d)
        o,err=simulate(d,c,e,'weekly','none','put');self.assertIsNone(err)
        self.assertEqual(set(o[0]['entry_fill_minutes']),{'09:46','09:47'})
        c2=config(fill_sync='synchronized');o,err=simulate(d,c2,e,'weekly','none','put');self.assertIsNone(err)
        self.assertEqual(set(o[0]['entry_fill_minutes']),{'09:47'})
    def test_no_common_minute_rejected_not_synthesised(self):
        d=self.make();c=config(fill_sync='synchronized');e=self.cand(d,c)
        for i,m in enumerate(('09:46','09:47','09:48','09:49','09:50')):
            self.drop(d,c,f"{e['day']} {m}:00",e['long_expiry'] if i%2==0 else e['short_expiry'],e['strike'])
        self.clear(d)
        self.assertEqual(simulate(d,c,e,'weekly','none','put'),(None,'no_synchronized_entry_minute'))
    def test_intrabar_never_cheaper_than_parametric_and_respects_range(self):
        c=SimpleNamespace(slippage=.5,slippage_mode='intrabar_range',intrabar_lambda=.5,intrabar_min=.05,stt=0.,exchange_bps=0.,gst=0.,stamp=0.,brokerage=0.)
        _,_,one_print=fill_flow(100.,1,'2026-01-07',c,'2026-01-13',1,100.,100.)
        self.assertGreater(one_print,.5)
        _,_,wide=fill_flow(100.,-1,'2026-01-07',c,'2026-01-13',400,120.,90.)
        self.assertAlmostEqual(wide,5.05)
    def test_vendor_discordance_flags_and_rejects(self):
        d=self.make();c=config(exclude_vendor_discordant=True);e=self.cand(d,c)
        rows=list(d.db.execute('SELECT * FROM opt WHERE provider=?',(c.provider,)))
        d.db.executemany('INSERT INTO opt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[('thetrademarkk',*tuple(r)[1:]) for r in rows])
        self.assertEqual(vendor_concordance(d.db),(len(rows),0))
        d.db.execute("UPDATE opt SET close=close+50 WHERE provider='thetrademarkk' AND ts=? AND expiry=? AND strike=? AND cp='PE'",(e['day']+' 09:45:00',e['long_expiry'],e['strike']))
        self.assertEqual(vendor_concordance(d.db),(len(rows),1))
        d.discordant.cache_clear();self.clear(d)
        self.assertEqual(simulate(d,c,e,'weekly','none','put'),(None,'vendor_discordant_entry'))
    def test_gamma_addon_delta_enters_initial_hedge(self):
        d=self.make();c=config();e=self.cand(d,c)
        base=simulate(d,c,e,'weekly','delta_entry_proxy','put')[0][0]
        gam=simulate(d,c,e,'weekly','gamma_reduced_delta_proxy','put')[0][0]
        self.assertGreater(gam['entry_delta_proxy'],base['entry_delta_proxy'])

class Settlement(Base):
    def test_exercise_stt_buyer_only_by_date(self):
        self.assertEqual(exercise_stt_rate('2026-03-31',config()),.00125)
        self.assertEqual(exercise_stt_rate('2026-04-01',config()),.0015)
    def ext(self,d,day,twap,n):
        d.daily[day]['twap_1500_1529']=twap;d.daily[day]['twap_minutes']=n
    def test_twap_stays_labelled_proxy(self):
        d=self.make();c=config(settlement_proxy='twap_1500_1529');e=self.cand(d,c);x=e['short_expiry'];self.ext(d,x,24900.,30)
        self.assertEqual(settlement_reference(d,c,x,Counter()),(24900.,'settlement_proxy_TWAP_NOT_NSE_OFFICIAL','SPOT_1500_1529_TWAP_PROXY',False))
    def test_twap_needs_enough_minutes(self):
        d=self.make();c=config(settlement_proxy='twap_1500_1529');e=self.cand(d,c);x=e['short_expiry'];self.ext(d,x,24900.,20);n=Counter()
        self.assertEqual(settlement_reference(d,c,x,n)[1],'settlement_proxy_NOT_NSE_OFFICIAL');self.assertEqual(n['settlement_twap_unavailable'],1)
    def test_cas_window_does_not_use_twap(self):
        d=self.make();c=config(settlement_proxy='twap_1500_1529');x='2026-09-01';d.daily[x]=dict(twap_1500_1529=1.,twap_minutes=30);n=Counter()
        self.assertIsNone(settlement_reference(d,c,x,n));self.assertEqual(n['settlement_proxy_cas_window'],1)
    def test_expiry_session_missing_from_days_still_settles(self):
        d=self.make();c=config();e=self.cand(d,c);d.valuation_days=[x for x in d.valuation_days if x!=e['short_expiry']]
        o,err=simulate(d,c,e,'weekly','none','put');self.assertIsNone(err);r=o[0]
        self.assertNotEqual(r['status'],'unresolved')
        self.assertEqual(r['missing_marks'].get('settled_after_missing_expiry_session'),1)
    def test_itm_long_at_expiry_pays_stt_short_does_not(self):
        d=self.make();c=config(exercise_stt=.01);e=self.cand(d,c)
        for row in (e['short_expiry'],):
            d.db.execute("DELETE FROM opt WHERE ts=? AND strike=?",(row+' 15:16:00',e['strike']))
        self.clear(d)
        d.spot.cache_clear()
        d.db.execute('UPDATE spot SET close=24000 WHERE ts=?',(e['short_expiry']+' 15:29:00',));d.db.commit();d.spot.cache_clear()
        o,_=simulate(d,c,e,'weekly','none','put');short=[x for x in o[0]['ledger'] if x['event'].startswith('settlement') and x['units']<0][0]
        self.assertGreater(short['payoff'],0);self.assertEqual(short['exercise_stt'],0.)

    def test_itm_long_at_expiry_is_charged_on_intrinsic(self):
        d=self.make();c=config(exercise_stt=.01);e=self.cand(d,c)
        d.db.execute('UPDATE spot SET close=25800 WHERE ts=?',(e['short_expiry']+' 15:29:00',));d.db.commit();d.spot.cache_clear()
        o,err=simulate(d,c,e,'weekly','gamma_reduced_delta_proxy','put');self.assertIsNone(err)
        leg=[x for x in o[0]['ledger'] if x['event'].startswith('settlement') and x['units']>0 and x['cp']=='CE'][0]
        self.assertEqual(leg['payoff'],800.);self.assertAlmostEqual(leg['exercise_stt'],leg['units']*800*.01)
        self.assertAlmostEqual(leg['cash_flow'],leg['units']*800*.99)

class Corrections(Base):
    """Acceptance tests T01-T07 from the independent review."""
    def rising(self,**kw):
        d=self.make(bull=True);c=config(fill_sync='synchronized',recenter_threshold=.001,**kw);return d,c,self.cand(d,c)
    def t01_blank(self,d,e,minutes):
        for m in minutes:
            d.db.execute("DELETE FROM opt WHERE ts=? AND strike=26000 AND cp='PE' AND expiry=?",(f'2026-01-14 {m}:00',e['long_expiry']))
        self.clear(d)
    def test_t01_recenter_legs_share_one_minute(self):
        d,c,e=self.rising();self.t01_blank(d,e,('09:46','09:47'))
        r=simulate(d,c,e,'rolling_recenter','none','put')[0][0]
        day=[x for x in r['ledger'] if x['day']=='2026-01-14' and x.get('time')]
        self.assertEqual({x['event'] for x in day},{'recenter_old_long_exit','recenter_new_long_open','roll_short_sale'})
        self.assertEqual({x['time'] for x in day},{'09:48'})
    def test_t01_no_common_minute_means_no_recenter(self):
        d,c,e=self.rising();self.t01_blank(d,e,('09:46','09:47','09:48','09:49','09:50'))
        r=simulate(d,c,e,'rolling_recenter','none','put')[0][0]
        day=[x['event'] for x in r['ledger'] if x['day']=='2026-01-14']
        self.assertNotIn('recenter_new_long_open',day)
        self.assertGreaterEqual(r['missing_marks'].get('recenter_no_common_minute',0),1)
    def test_t02_long_not_sold_while_short_is_open(self):
        d=self.make();c=config(iv_convergence=99.,fill_sync='synchronized');e=self.cand(d,c)
        self.drop(d,c,e['day']+' 11:31:00',e['short_expiry'],e['strike']);self.clear(d)
        o,err=simulate(d,c,e,'iv_converge','none','put');self.assertIsNone(err);r,marks=o
        self.assertNotIn('PARTIAL_EXIT_NAKED_SHORT',marks[0]['quality'])
        self.assertFalse([x for x in r['ledger'] if x['day']==e['day'] and x['event']=='stop_exit' and x['time']=='11:31'])   # protection stays while the short is open
        self.assertEqual(r['partial_exit_days']['naked_short'],0)
    def test_t02_long_residual_labelled(self):
        d=self.make(missing_exit=False);c=config();e=self.cand(d,c)
        self.drop(d,c,e['short_expiry']+' 15:16:00',e['long_expiry'],e['strike']);self.clear(d)
        r,marks=simulate(d,c,e,'weekly','none','put')[0]
        self.assertTrue(any('PARTIAL_EXIT_LONG_ONLY' in m['quality'] for m in marks));self.assertGreaterEqual(r['partial_exit_days']['long_only'],1)
    def gated(self,mode):
        d=self.make();c=config(roll_signal_policy=mode,roll_iv_min=1.,roll_iv_max=9.);e=self.cand(d,c)
        real=d.greek;low=sorted(x for x in d.days if x>e['short_expiry'] and dt.date.fromisoformat(x).weekday() in c.weekdays)[0]
        def fake(provider,day,minute,ex,k,cp,*a):
            g=real(provider,day,minute,ex,k,cp,*a)
            if g and day==low and ex!=e['long_expiry']:g=dict(g,iv=g['iv']*.1)
            return g
        d.greek=fake
        return simulate(d,c,e,'rolling_static','none','put')[0][0],low
    def test_t03_roll_policies(self):
        un,low=self.gated('unconditional');sells=[x for x in un['ledger'] if x['event']=='roll_short_sale']
        self.assertEqual(sells[0]['day'],low);self.assertTrue(all('iv_ratio' in x for x in un['roll_decisions']))
        wait,_=self.gated('wait_until_eligible');w=[x for x in wait['ledger'] if x['event']=='roll_short_sale']
        self.assertEqual(wait['roll_decisions'][0]['decision'],'skip');self.assertGreater(w[0]['day'],low)
        req,_=self.gated('require_ratio');q=[x for x in req['ledger'] if x['event']=='roll_short_sale']
        self.assertGreater(q[0]['day'],req['roll_decisions'][0]['short_expiry'])
        self.assertGreater(q[0]['day'],w[0]['day'])
    def test_t07_verified_settlement_overrides_only_its_expiry(self):
        d=self.make();e=self.cand(d,config());x=e['short_expiry']
        p=Path(self.temp.name)/'m.csv';p.write_text(f'expiry,settlement_price,evidence_url\n{x},24000,EV\n')
        c=config(settlement_master=SettlementMaster(p),settlement_proxy='twap_1500_1529');n=Counter()
        self.assertEqual(settlement_reference(d,c,x,n),(24000.,'verified_official_settlement','EV',True))
        other=[y for y in d.days if y>x][0]
        self.assertFalse(settlement_reference(d,c,other,n)[3])

class Nested(unittest.TestCase):
    days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(90)]
    def trade(self,tid,a,b,slope,seed):
        r=random.Random(seed);v=0.;p={}
        for j in range(a,b+1):
            v+=slope+r.gauss(0,1);p[self.days[j]]=dict(base=v,lower=v,upper=v,capital=1000.)
        return dict(id=tid,day=self.days[a],exit_day=self.days[b],pnl_inr=v,entry_capital=1000.,lot_units=65),p
    def test_t04_future_only_winner_cannot_be_selected(self):
        rows=[];marks={};rules={('s',0.,1.):[],('s',1.,2.):[]}
        for i,a in enumerate(range(0,84,6)):
            early=a<45
            for rule,slope in ((('s',0.,1.),2. if early else -2.),(('s',1.,2.),-2. if early else 20.)):
                t,p=self.trade(f'{rule[1]}-{i}',a,a+5,slope,i);rows.append(t);marks[t['id']]=p;rules[rule].append(t)
        rows.sort(key=lambda x:(x['day'],x['id']));m=GroupMatrix(rows,marks,self.days)
        out=nested_folds(rules,m,[(self.days[48],self.days[66]),(self.days[67],self.days[80])],0,self.days[89],2,None,'base',None,5)
        self.assertEqual(out[0]['rule'],('s',0.,1.))     # chosen before the regime flip
        self.assertLess(out[0]['entry_cohort_lifecycle_net'],0.)                # and the loss is reported, not hidden
    def test_t05_per_unit_removes_lot_eras(self):
        days=self.days[:40];rows=[];marks={};flat=[];fm={}
        for i,(a,lot) in enumerate(((0,75),(10,25),(20,65),(30,75))):
            t,p=self.trade(f'x{i}',a,a+8,.3,i);base=dict(t);fm[t['id']]=p;flat.append(dict(base,lot_units=1))
            t=dict(t,pnl_inr=t['pnl_inr']*lot,lot_units=lot);q={d:dict(base=v['base']*lot,lower=v['lower']*lot,upper=v['upper']*lot,capital=1000.) for d,v in p.items()}
            rows.append(t);marks[t['id']]=q
        unit=GroupMatrix(rows,marks,days).evaluate(rows,None,'base',basis_weights(rows,'per_unit'))
        same=GroupMatrix(flat,fm,days).evaluate(flat)
        raw=GroupMatrix(rows,marks,days).evaluate(rows)
        self.assertAlmostEqual(unit['sharpe'],same['sharpe'],places=9);self.assertNotAlmostEqual(raw['sharpe'],same['sharpe'],places=3)

class Estimators(unittest.TestCase):
    def test_t06_closeclose_is_separate_from_range_proxy(self):
        rows=[daily(i+1,100+i,102+i,99+i,101+i,parkinson_1m_var=.001,closeclose_1m_var=.002,closeclose_1m_n=374) for i in range(7)]
        g=yang_zhang_rv(rows,5);o=[math.log(rows[i]['open']/rows[i-1]['close']) for i in range(2,7)]
        self.assertAlmostEqual(g['rv1m_closeclose'],math.sqrt(252*np.mean(np.square(o)+.002)),places=12)
        self.assertAlmostEqual(g['rv1m_range_proxy'],math.sqrt(252*np.mean(np.square(o)+.001)),places=12)
    def test_t06_incomplete_minutes_refused(self):
        rows=[daily(i+1,100+i,102+i,99+i,101+i,closeclose_1m_var=.002,closeclose_1m_n=374) for i in range(7)]
        rows[-1]['closeclose_1m_n']=200
        self.assertIsNone(yang_zhang_rv(rows,5)['rv1m_closeclose'])

class FixedWindows(unittest.TestCase):
    """V4.1-F01/F02/F03 acceptance, built on the reviewer's two counterexamples."""
    days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(25)]
    def book(self,extra=()):
        spec=[('warmup',0,5),('fold_A',8,13),('fold_B',10,15)]+list(extra);rows=[];marks={}
        for tid,i,j in spec:
            rows.append(dict(id=tid,day=self.days[i],exit_day=self.days[j],pnl_inr=100.,entry_capital=100.,lot_units=65,status='closed_traded'))
            marks[tid]={self.days[k]:dict(base=(k-i+1)**2*100/(j-i+1)**2,lower=(k-i+1)**2*100/(j-i+1)**2,upper=(k-i+1)**2*100/(j-i+1)**2,capital=100.) for k in range(i,j+1)}
        return rows,marks
    def nest(self,rows,marks,cap=None,max_open=1):
        g=GroupMatrix(rows,marks,self.days)
        out=nested_folds({('iv_ratio',1,2):rows},g,[(self.days[8],self.days[9]),(self.days[10],self.days[11])],0,self.days[20],max_open,cap,'base',None,1)
        return g,out,persistent_nested_portfolio(out,g,max_open,cap,'base',None)
    def overlap(self,rows,ids):
        by={r['id']:r for r in rows}
        return [d for d in self.days if sum(by[i]['day']<=d<=by[i]['exit_day'] for i in ids)>1]
    def test_f01_open_position_blocks_next_fold_slot(self):
        rows,marks=self.book();_,out,p=self.nest(rows,marks)
        self.assertEqual([x['id'] for f in out for x in f['chosen']],['fold_A','fold_B'])   # folds alone still pick both
        self.assertEqual(p['admitted'],['fold_A']);self.assertEqual(self.overlap(rows,p['admitted']),[])
        self.assertEqual(p['folds'][1]['carried_in'],1);self.assertEqual(p['folds'][1]['entries'],0)
    def test_f01_capital_cap_persists(self):
        rows,marks=self.book();_,_,p=self.nest(rows,marks,cap=150.,max_open=5)
        self.assertEqual(p['admitted'],['fold_A'])
        _,_,free=self.nest(rows,marks,cap=250.,max_open=5);self.assertEqual(free['admitted'],['fold_A','fold_B'])
    def test_f01_unresolved_position_keeps_its_slot(self):
        rows,marks=self.book();rows[1]['status']='unresolved';_,_,p=self.nest(rows,marks)
        self.assertEqual(p['admitted'],['fold_A'])
    def test_f02_flat_sessions_change_calendar_stats_only(self):
        rows,marks=self.book();r=rows[1]
        short=[d for d in self.days if d>=r['day'] and d<=r['exit_day']]
        narrow=GroupMatrix([r],{r['id']:marks[r['id']]},short).evaluate([r])
        wide=GroupMatrix([r],{r['id']:marks[r['id']]},self.days).evaluate([r])
        self.assertAlmostEqual(narrow['sharpe'],25.4558441,places=5);self.assertAlmostEqual(wide['sharpe'],7.3183307,places=5)
        self.assertEqual((narrow['net'],narrow['n']),(wide['net'],wide['n']))
        self.assertAlmostEqual(narrow['active_day_sharpe'],wide['active_day_sharpe'],places=9)
        self.assertEqual(wide['time_in_market'],6/25);self.assertEqual(wide['days'],25)
    def test_f02_reference_replay_agrees(self):
        rows,marks=self.book();r=rows[1]
        m=portfolio([r],marks,self.days,1,None);self.assertAlmostEqual(m['sharpe'],7.3183307,places=5);self.assertEqual(m['active_days'],6)
    def test_f03_fold_mtm_clipped_and_sums_to_lifecycle(self):
        rows,marks=self.book();r=rows[1];g=GroupMatrix([r],{r['id']:marks[r['id']]},self.days)
        a=fold_window_stats(g,[r],self.days[8],self.days[9],None,'base',None)
        b=fold_window_stats(g,[r],self.days[10],self.days[20],None,'base',None)
        self.assertAlmostEqual(a['window_mtm'],4*100/36,places=9);self.assertAlmostEqual(a['window_mtm']+b['window_mtm'],100.,places=9)
        self.assertEqual((b['entries'],b['carried_in']),(0,1))

ROOT=Path(__file__).resolve().parents[1]
class V43(Base):
    """V4.2 deep-review acceptance tests (T57-T67 where runnable without PostgreSQL)."""
    def audit_cfg(self,**kw):
        v=dict(provider='rissin_upstox',max_short_dte=8,long_tenors=[120],tenor_tolerance=25,option_type='PE',min_signal_volume=1,min_fill_volume=1,
            min_pair_volume=1,max_parity_bps=40,rate=.06,fill_sync='synchronized',fill_model='next_minute',long_strike_offset=0.,lot_master=config().lot_master);v.update(kw);return SimpleNamespace(**v)
    def only(self,d,c,e,expiry,keep):
        for m in ('09:46','09:47','09:48','09:49','09:50'):
            if m not in keep:self.drop(d,c,f"{e['day']} {m}:00",expiry,e['strike'])
        self.clear(d)
    def test_t57_fill_never_from_the_qualifying_minute(self):
        d=self.make();c=config(fill_model='next_minute',fill_sync='synchronized');e=self.cand(d,c)
        o,err=simulate(d,c,e,'weekly','none','put');self.assertIsNone(err)
        first=[x for x in o[0]['ledger'] if x['event'].startswith('initial_')]
        self.assertTrue(all(x['observed_bar_minute']=='09:46' and x['time']=='09:47' for x in first))
    def test_t57_single_print_cannot_fill_under_delay(self):
        d=self.make();c=config(fill_model='next_minute',fill_sync='synchronized');e=self.cand(d,c)
        self.only(d,c,e,e['long_expiry'],('09:46',))
        self.assertIsNone(simulate(d,c,e,'weekly','none','put')[0])
        self.assertIsNotNone(simulate(d,config(fill_sync='synchronized'),e,'weekly','none','put')[0])
    def test_stress_monotonic(self):
        d=self.make();e=self.cand(d,config())
        d.db.execute("UPDATE opt SET high=open+3,low=MAX(.05,open-3)");self.clear(d)
        pnl=lambda m:simulate(d,config(fill_model=m,fill_sync='synchronized'),e,'weekly','none','put')[0][0]['pnl_inr']
        self.assertLess(pnl('next_minute_adverse_range'),pnl('next_minute'))
    def test_t58_audit_refuses_unsynchronized_legs(self):
        d=self.make();c=self.audit_cfg();e=self.cand(d,config())
        self.only(d,config(),e,e['short_expiry'],('09:46',));self.only(d,config(),e,e['long_expiry'],('09:48',))
        _,reached,_=next(audit_day(d,c,e['day']))
        self.assertIn('any_independent_liquid_print',reached);self.assertNotIn('any_synchronous_liquid_print',reached);self.assertNotIn('admitted',reached)
    def test_audit_reconciles_with_simulator(self):
        d=self.make();sc=config(fill_model='next_minute',fill_sync='synchronized',start='2026-01-07',end='2026-02-20');a=self.audit_cfg()
        sims={(x['day'],x['strike'],x['long_expiry']) for x in generate(d,sc,Counter()) if simulate(d,sc,x,'weekly','none','put')[0]}
        self.assertGreater(len(sims),5)
        adm={(day,r['strike'],r['long_expiry']) for day in d.days for _,reached,r in audit_day(d,a,day) if 'admitted' in reached}
        self.assertLessEqual(sims,adm)
    def enrich(self,path,vol):
        p=subprocess.run([sys.executable,str(ROOT/'enrich_cache.py'),'--cache',str(path),'--min-pair-volume',str(vol),'--batch-rows','100'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
    def test_t59_reenrichment_leaves_no_stale_greeks(self):
        d=self.make();self.enrich(self.path,1)
        db=sqlite3.connect(self.path);before=db.execute('SELECT COUNT(*) FROM greeks_cache WHERE iv IS NOT NULL').fetchone()[0];self.assertGreater(before,0);db.close()
        self.enrich(self.path,100000)
        db=sqlite3.connect(self.path);self.assertEqual(db.execute('SELECT COUNT(*) FROM greeks_cache').fetchone()[0],0);db.close()
    def test_t60_interrupted_enrichment_reads_incomplete(self):
        d=self.make();self.enrich(self.path,1);db=sqlite3.connect(self.path)
        db.execute("UPDATE meta SET value='0' WHERE name='enrichment_complete'");db.commit();db.close()
        d2=Data(str(self.path));self.addCleanup(d2.close);self.assertFalse(d2.enriched)
    def test_t61_cached_and_live_greeks_match(self):
        d=self.make();e=self.cand(d,config());self.enrich(self.path,1)
        live=d.greek('rissin_upstox',e['day'],'09:45',e['long_expiry'],e['strike'],'PE',.06,1,40)
        d2=Data(str(self.path));self.addCleanup(d2.close)
        cached=d2.greek('rissin_upstox',e['day'],'09:45',e['long_expiry'],e['strike'],'PE',.06,1,40)
        self.assertAlmostEqual(live['iv'],cached['iv'],places=6)
    def test_t62_session_classes(self):
        self.assertEqual(session_quality(350,370),'partial');self.assertEqual(session_quality(372,370),'near_full');self.assertEqual(session_quality(375,370),'full')
    def test_t65_stop_includes_checkpoint_hedge_mark(self):
        d=self.make();c=config(fill_sync='synchronized');e=self.cand(d,c)
        base=simulate(d,c,e,'p50_s100','delta_entry_proxy','put')[0][0]
        self.assertNotEqual(base['exit_reason'],'checkpoint_p50_s100')
        d.db.execute('UPDATE spot SET close=close*3 WHERE ts=?',(e['day']+' 11:30:00',));d.db.commit();d.spot.cache_clear()
        moved=simulate(d,c,e,'p50_s100','delta_entry_proxy','put')[0][0]
        self.assertEqual((moved['exit_reason'],moved['exit_day']),('checkpoint_p50_s100',e['day']))
    def test_t66_ledger_cash_reconciles_to_pnl(self):
        d=self.make()
        for policy in ('weekly','p25_s50','carry30','rolling_static','rolling_diagonal','rolling_recenter'):
            c=config(fill_sync='synchronized',fill_model='next_minute',recenter_threshold=.001);e=self.cand(d,c)
            r=simulate(d,c,e,policy,'none','put')[0][0]
            if r['status']=='unresolved':continue
            self.assertAlmostEqual(sum(x['cash_flow'] for x in r['ledger']),r['pnl_inr'],places=6,msg=policy)
    def test_t67_publication_refuses_changed_trades(self):
        t=Path(tempfile.mkdtemp())/'t.sqlite';db=sqlite3.connect(t);db.execute('CREATE TABLE trades(id,day,exit_day,status,pnl_inr)');db.execute("INSERT INTO trades VALUES('a','d','e','closed',1.0)");db.commit()
        meta=dict(source_fingerprint='s',enrichment_run_id='r');L=lineage.build(t,meta,{})
        lineage.verify(L,meta,t,meta,allow_code_change=True)
        db.execute("UPDATE trades SET pnl_inr=2.0");db.commit();db.close()
        with self.assertRaises(ValueError):lineage.verify(L,meta,t,meta,allow_code_change=True)
        with self.assertRaises(ValueError):lineage.verify(None,meta,t,meta)

class Blueprint(unittest.TestCase):
    def test_parity_prunes_a_single_bad_strike(self):
        from quant_v3 import parity_ols
        q={}
        for k in (24600,24800,25000,25200,25400,25600):
            for cp in ('PE','CE'):q[float(k),cp]=dict(close=black_forward(25070,k,80/365,.2,cp,.065),volume=200)
        q[25600.,'CE']['close']+=250
        r=parity_ols(q,25000,80/365,.06,max_residual_bps=40.)
        self.assertEqual(r.quality,'fit_ok');self.assertAlmostEqual(r.forward,25070,places=2);self.assertEqual(r.matched_pairs,5)
    def test_march_quarterly_keeps_old_lot_if_expiry_moves(self):
        from contract_specs_v2 import rule_lot_size
        self.assertEqual(rule_lot_size('2025-12-15','2026-03-30').qty,75)
    def test_month_windows_cover_range(self):
        w=list(month_windows(dt.date(2025,1,15),dt.date(2025,3,2)))
        self.assertEqual(w,[(dt.date(2025,1,15),dt.date(2025,1,31)),(dt.date(2025,2,1),dt.date(2025,2,28)),(dt.date(2025,3,1),dt.date(2025,3,2))])

class V44Integrity(unittest.TestCase):
    """Reviewer reproductions R1 and R4 and acceptance tests AT01-AT07, kept as permanent regressions."""
    def book(self):
        t=Path(tempfile.mkdtemp())/'t.sqlite';db=sqlite3.connect(t)
        db.executescript("""CREATE TABLE trades(id,day,exit_day,status,pnl_inr);CREATE TABLE marks(trade_id,day,pnl_inr);CREATE TABLE fills(trade_id,seq,details);CREATE TABLE meta(name,value);
          INSERT INTO trades VALUES('t','d','e','closed_traded',100);INSERT INTO marks VALUES('t','d',-500);INSERT INTO fills VALUES('t',0,'50');INSERT INTO meta VALUES('config','{}');""")
        db.commit();db.close();m=dict(source_fingerprint='s',enrichment_run_id='r');return t,m,lineage.build(t,m,{})
    def mutate(self,sql):
        t,m,L=self.book();lineage.verify(L,m,t,m)
        db=sqlite3.connect(t);db.execute(sql);db.commit();db.close()
        with self.assertRaises(ValueError):lineage.verify(L,m,t,m)
    def test_at01_marks(self):self.mutate('UPDATE marks SET pnl_inr=-100000')
    def test_at02_fills(self):self.mutate("UPDATE fills SET details='5000'")
    def test_config_change(self):self.mutate("UPDATE meta SET value='changed'")
    def test_at03_report_metric(self):
        t,m,L=self.book();p={'results':[{'net':1.}]};L['report_sha256']=lineage.report_digest(p);q=dict(p,lineage=L)
        lineage.verify(L,m,t,m,payload=q)
        with self.assertRaises(ValueError):lineage.verify(L,m,t,m,payload=dict(q,results=[{'net':2.}]))
    def test_at04_master_contents(self):
        t,m,L=self.book();c=t.with_name('lots.csv');c.write_text('a\n');tm=dict(m,contract_master_sha256=lineage.file_digest(c))
        lineage.verify(L,m,t,tm,config=dict(contract_master=str(c)))
        c.write_text('b\n')
        with self.assertRaises(ValueError):lineage.verify(L,m,t,tm,config=dict(contract_master=str(c)))
    def test_at05_ranking_refuses_other_cache(self):
        ok=dict(source_fingerprint='s',enrichment_run_id='r',research_eligible='1',enrichment_complete='1')
        lineage.check_inputs(ok,dict(source_fingerprint='s',enrichment_run_id='r'))
        for bad in (dict(ok,source_fingerprint='x'),dict(ok,enrichment_run_id='x'),dict(ok,enrichment_complete='0'),dict(ok,research_eligible='0')):
            with self.assertRaises(ValueError):lineage.check_inputs(bad,dict(source_fingerprint='s',enrichment_run_id='r'))
    def stage(self):
        db=sqlite3.connect(':memory:');db.executescript(DDL)
        r=('p','2026-01-01 09:45:00','2026-01-01','2026-01-06',25000.,'PE',1.,1.,1.,1.,1,1,1,1)
        db.execute('INSERT INTO opt_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',r);db.execute('INSERT INTO opt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',r);db.commit();return db
    def test_at06_finalize_resumes_after_each_step(self):
        for done in range(len(FINAL_INDEXES)+1):
            db=self.stage()
            for sql in FINAL_INDEXES[:done]:db.execute(sql)
            finalize(db);finalize(db)
            self.assertEqual(db.execute('SELECT source_rows,canonical_rows FROM duplicate_stats').fetchall(),[(1,1)])
    def test_at07_resume_fingerprint_covers_settings(self):
        base=dict(min_session_minutes=370,allow_partial=False,daily_warmup_days=420);ns=lambda **k:SimpleNamespace(**{**base,**k})
        f=lambda a:extraction_config('d','2025-01-01','2025-02-01','09:45',a)
        self.assertEqual(f(ns()),f(ns()))
        for change in (dict(min_session_minutes=350),dict(allow_partial=True),dict(daily_warmup_days=0)):self.assertNotEqual(f(ns()),f(ns(**change)))

class V44Clock(Base):
    """R2, R3 and AT09-AT14: one event clock for every order."""
    cfg=staticmethod(lambda **k:config(fill_model='next_minute',fill_sync='synchronized',**k))
    def test_r2_at11_lone_1516_bar_cannot_fill(self):
        d=self.make();c=self.cfg();e=self.cand(d,c)
        self.drop(d,c,e['short_expiry']+' 15:17:00',e['short_expiry'],e['strike']);self.clear(d)
        r=simulate(d,c,e,'weekly','none','put')[0][0]
        self.assertFalse([x for x in r['ledger'] if x['event']=='preexpiry_buyback'])
        self.assertTrue(any(x['event'].startswith('settlement_proxy') for x in r['ledger']))
    def test_exit_fill_is_after_qualifying_minute(self):
        d=self.make();c=self.cfg();e=self.cand(d,c);r=simulate(d,c,e,'weekly','none','put')[0][0]
        b=[x for x in r['ledger'] if x['event']=='preexpiry_buyback'][0]
        self.assertEqual((b['signal_time'],b['observed_bar_minute'],b['fill_time']),('15:15','15:16','15:17'))
    def test_r3_at09_pre_entry_spot_cannot_move_hedge(self):
        for hedge in ('delta_entry_proxy','delta_daily_proxy','gamma_reduced_delta_proxy'):
            d=self.make();c=self.cfg();e=self.cand(d,c);before=simulate(d,c,e,'weekly',hedge,'put')[0][0]
            self.assertEqual(set(before['entry_fill_minutes']),{'09:47'})
            d.db.execute('UPDATE spot SET open=open+1000,close=close+1000 WHERE ts=?',(e['day']+' 09:46:00',));d.db.commit();d.spot.cache_clear()
            self.assertAlmostEqual(simulate(d,c,e,'weekly',hedge,'put')[0][0]['pnl_inr'],before['pnl_inr'],places=6,msg=hedge)
    def test_hedge_path_rejected_when_spot_missing_at_fill(self):
        d=self.make();c=self.cfg();e=self.cand(d,c)
        d.db.execute('DELETE FROM spot WHERE ts=?',(e['day']+' 09:47:00',));d.db.commit();d.spot.cache_clear()
        self.assertEqual(simulate(d,c,e,'weekly','delta_entry_proxy','put'),(None,'missing_hedge_spot_at_fill'))
        self.assertIsNotNone(simulate(d,c,e,'weekly','none','put')[0])
    def test_at10_gamma_leg_on_same_clock(self):
        d=self.make();c=self.cfg();e=self.cand(d,c);r=simulate(d,c,e,'weekly','gamma_reduced_delta_proxy','put')[0][0]
        self.assertEqual({x['time'] for x in r['ledger'] if x['event'] in ('initial_long','initial_short','gamma_reduction_option_buy')},{'09:47'})
        self.drop(d,c,e['day']+' 09:47:00',e['short_expiry'],e['strike'],'CE');self.drop(d,c,e['day']+' 09:48:00',e['short_expiry'],e['strike'],'CE');self.clear(d)
        r=simulate(d,c,e,'weekly','gamma_reduced_delta_proxy','put')[0][0]
        self.assertEqual({x['time'] for x in r['ledger'] if x['event'] in ('initial_long','initial_short','gamma_reduction_option_buy')},{'09:49'})
    def test_at12_times_nondecreasing(self):
        d=self.make();c=self.cfg(recenter_threshold=.001,max_short_long_width=2000.);e=self.cand(d,c)
        for policy in ('weekly','p25_s50','rolling_static','rolling_recenter','atm_weekly_roll'):
            for x in simulate(d,c,e,policy,'none','put')[0][0]['ledger']:
                if x.get('fill_time'):self.assertLessEqual(x['signal_time'],x['fill_time'],policy);self.assertEqual(x['price_evidence_time'],x['fill_time'])
    def test_at14_zero_volume_never_fills(self):
        d=self.make();c=self.cfg();e=self.cand(d,c)
        d.db.execute("UPDATE opt SET volume=0 WHERE expiry=? AND strike=?",(e['long_expiry'],e['strike']));self.clear(d)
        self.assertIsNone(simulate(d,c,e,'weekly','none','put')[0])
    def test_same_day_checkpoint_retry(self):
        d=self.make();c=config(iv_convergence=99.,fill_sync='synchronized');e=self.cand(d,c)
        self.drop(d,c,e['day']+' 11:31:00',e['short_expiry'],e['strike']);self.clear(d)
        r=simulate(d,c,e,'iv_converge','none','put')[0][0]
        self.assertEqual({(x['day'],x['time']) for x in r['ledger'] if x['event']=='pending_exit_fill'},{(e['day'],'13:31')})
    def test_stops_measure_actual_cost_under_adverse_fills(self):
        d=self.make();e=self.cand(d,config());d.db.execute("UPDATE opt SET high=open+20,low=MAX(.05,open-20)");self.clear(d)
        r=simulate(d,config(fill_model='next_minute_adverse_range',fill_sync='synchronized',slippage=.1),e,'p50_s100','none','put')[0][0]
        raw=(simulate(d,config(fill_sync='synchronized',fill_model='next_minute'),e,'p50_s100','none','put')[0][0]['initial_debit_inr'])
        self.assertGreater(r['initial_debit_inr'],raw)

class V44Guidebook(Base):
    """Optional diagonal, campaign and clock research paths. Synthetic; says nothing about profit."""
    def dcfg(self,**k):return config(fill_model='next_minute',fill_sync='synchronized',long_strike_offsets=[0.,-500.],max_short_long_width=1000.,**k)
    def pair(self,d,c):
        rows=[x for x in generate(d,c,Counter()) if x['day']==next(iter(generate(d,c,Counter())))['day']]
        return {x['long_strike_offset']:x for x in rows}
    def test_diagonal_legs_use_different_strikes(self):
        d=self.make();c=self.dcfg();p=self.pair(d,c);self.assertEqual(set(p),{0.,-500.})
        r=simulate(d,c,p[-500.],'weekly','none','put')[0][0]
        legs={x['event']:x['strike'] for x in r['ledger'] if x['event'].startswith('initial_')}
        self.assertEqual(legs['initial_short']-legs['initial_long'],500.)
        self.assertIsNone(r['forward_iv_ratio'])
        self.assertAlmostEqual(r['campaign']['strike_width_inr'],500.*r['lot_units'])
        self.assertLess(r['initial_debit_inr'],simulate(d,c,p[0.],'weekly','none','put')[0][0]['initial_debit_inr'])
    def test_calendar_row_unchanged_by_zero_offset(self):
        d=self.make();c=config();e=self.cand(d,c);self.assertEqual((e['long_strike'],e['long_strike_offset']),(e['strike'],0.))
    def test_campaign_gross_versus_net(self):
        L=[dict(event='initial_long',day='a',expiry='L',strike=90,cp='PE',units=1,cash_flow=-100.),
           dict(event='initial_short',day='a',expiry='W1',strike=100,cp='PE',units=-1,cash_flow=60.),
           dict(event='preexpiry_buyback',day='b',expiry='W1',strike=100,cp='PE',units=1,cash_flow=-50.),
           dict(event='roll_short_sale',day='c',expiry='W2',strike=100,cp='PE',units=-1,cash_flow=60.),
           dict(event='settlement_proxy_NOT_NSE_OFFICIAL',day='d',expiry='W2',strike=100,cp='PE',units=-1,cash_flow=0.)]
        c=campaign_economics(L,100.,10.)
        self.assertEqual((c['cycles'],c['winning_cycles'],c['gross_short_credit'],c['net_short_pnl']),(2,2,120.,70.))
        self.assertEqual((c['gross_payback_cycle'],c['net_payback_cycle'],c['gross_risk_free_cycle'],c['net_risk_free_cycle']),(2,None,2,None))
    def test_atm_roll_sells_above_long_and_caps_width(self):
        d=self.make(bull=True);c=self.dcfg(roll_step=500);p=self.pair(d,c);r=simulate(d,c,p[-500.],'atm_weekly_roll','none','put')[0][0]
        sells=[x for x in r['ledger'] if x['event']=='roll_short_sale'];self.assertTrue(sells)
        long_k=r['long_strike'];self.assertTrue(all(long_k<x['strike']<=long_k+1000. for x in sells))
    def test_atm_roll_skips_when_spot_below_long(self):
        d=self.make();c=self.dcfg(roll_step=500);p=self.pair(d,c)
        e=p[-500.];roll_day=[x for x in d.days if x>e['short_expiry'] and dt.date.fromisoformat(x).weekday() in c.weekdays][0]
        d.db.execute('UPDATE spot SET close=24000 WHERE ts=?',(roll_day+' 09:45:00',));d.db.commit();d.spot.cache_clear()
        r=simulate(d,c,e,'atm_weekly_roll','none','put')[0][0]
        self.assertGreaterEqual(r['missing_marks'].get('atm_short_below_long_skipped',0),1)
        self.assertFalse([x for x in r['ledger'] if x['event']=='roll_short_sale' and x['day']==roll_day])
    def test_expiry_day_close_roll_timing(self):
        d=self.make();c=self.dcfg(roll_step=500,roll_timing='expiry_day_close');p=self.pair(d,c);r=simulate(d,c,p[-500.],'atm_weekly_roll','none','put')[0][0]
        first=[x for x in r['ledger'] if x['event']=='roll_short_sale'][0]
        self.assertEqual((first['day'],first['signal_time'],first['fill_time']),(p[-500.]['short_expiry'],'15:15','15:17'))
    def test_attribution_is_additive(self):
        d=self.make();c=self.dcfg();p=self.pair(d,c);r=simulate(d,c,p[-500.],'rolling_static','none','put')[0][0]
        self.assertAlmostEqual(sum(r['pnl_attribution'].values()),r['pnl_inr'],places=6)
    def test_attribute_leg_recovers_theta_only(self):
        f,k,t0,iv,r=25000.,25000.,.2,.2,.06;t1=t0-1/365.25
        p0=black_forward(f,k,t0,iv,'PE',r);p1=black_forward(f,k,t1,iv,'PE',r)
        a=attribute_leg(dict(price=p0,iv=iv,f=f,t=t0,r=r),p1,f,iv,t1,k,'PE',1)
        self.assertAlmostEqual(a['theta'],p1-p0,places=9);self.assertAlmostEqual(a['delta']+a['gamma']+a['vega']+a['residual'],0.,places=9)
    def test_session_clock_by_hand(self):
        # Fri 09:45 to Tue 15:30: Mon and Tue are full sessions after Friday's remaining 345/375.
        got=session_time_iv(.2,'2026-01-09','2026-01-13',[])
        tcal=(4+(15.5-9.75)/24)/365.25;tb=(2+345/375)/252
        self.assertAlmostEqual(got,.2*math.sqrt(tcal/tb),places=12)
        self.assertLess(session_time_iv(.2,'2026-01-09','2026-01-13',[np.datetime64('2026-01-12')]),999)
    def test_trend_uses_only_prior_sessions(self):
        rows=[dict(close=100.+i) for i in range(300)];a=trend_fields(rows)
        self.assertAlmostEqual(a['spot_to_sma50'],399/(sum(100.+i for i in range(250,300))/50));self.assertAlmostEqual(a['pct_below_52w_high'],0.)
        self.assertIsNone(trend_fields(rows[:10]).get('spot_to_sma50'))
    def test_audit_reconciles_for_diagonal(self):
        d=self.make();sc=config(fill_model='next_minute',fill_sync='synchronized',start='2026-01-07',end='2026-02-20',long_strike_offsets=[-500.])
        a=SimpleNamespace(provider='rissin_upstox',max_short_dte=8,long_tenors=[120],tenor_tolerance=25,option_type='PE',min_signal_volume=1,min_fill_volume=1,min_pair_volume=1,max_parity_bps=40,rate=.06,fill_sync='synchronized',fill_model='next_minute',long_strike_offset=-500.,lot_master=config().lot_master)
        sims={(x['day'],x['long_expiry']) for x in generate(d,sc,Counter()) if simulate(d,sc,x,'weekly','none','put')[0]}
        self.assertGreater(len(sims),5)
        self.assertLessEqual(sims,{(day,r['long_expiry']) for day in d.days for _,got,r in audit_day(d,a,day) if 'admitted' in got})

class ShiftControl(unittest.TestCase):
    def setup(self,planted):
        rng=random.Random(3);days=[(dt.date(2025,1,1)+dt.timedelta(days=i)).isoformat() for i in range(420)]
        rows=[];marks={};vals=[];s=0.
        for i in range(0,400,4):
            a=rng.random()*2-1;s=.8*s+rng.gauss(0,.6)
            v=1.+s/4;edge=(3. if v>=1.05 else -3.) if planted else 0.;tid=f't{i:04d}';p={};cum=0.
            for j in range(i,i+4):cum+=edge/4+rng.gauss(0,1);p[days[j]]=dict(base=cum,lower=cum,upper=cum,capital=1000.)
            rows.append(dict(id=tid,day=days[i],exit_day=days[i+3],pnl_inr=cum,entry_capital=1000.,lot_units=65));marks[tid]=p;vals.append(v)
        g=GroupMatrix(rows,marks,days)
        return circular_shift_control(rows,vals,[(l,9.) for l in (.8,.9,1.,1.05,1.1)],g,1,None,'base',None,5,99)
    def test_planted_edge_found(self):self.assertLess(self.setup(True)['p_value'],.05)
    def test_no_edge_not_found(self):self.assertGreater(self.setup(False)['p_value'],.05)

class V441(Base):
    """V4.4 review: F01 hedge lifetime, F02 session eligibility, F03 information timing."""
    def hedged(self,**k):return config(fill_sync='synchronized',fill_model='next_minute',iv_convergence=99.,hedge_bps=0.,**k)
    def test_a1_a5_spot_after_exit_cannot_move_hedge(self):
        d=self.make();c=self.hedged();e=self.cand(d,c)
        for hedge in ('delta_entry_proxy','delta_daily_proxy','gamma_reduced_delta_proxy'):
            before=simulate(d,c,e,'iv_converge',hedge,'put')[0]
            d.db.execute('UPDATE spot SET close=close+1000 WHERE ts=?',(e['day']+' 15:29:00',));d.db.commit();d.spot.cache_clear()
            after=simulate(d,c,e,'iv_converge',hedge,'put')[0]
            self.assertAlmostEqual(after[0]['pnl_inr'],before[0]['pnl_inr'],places=6,msg=hedge)
            self.assertAlmostEqual(after[1][-1]['pnl'],before[1][-1]['pnl'],places=6,msg=hedge)
            d.db.execute('UPDATE spot SET close=close-1000 WHERE ts=?',(e['day']+' 15:29:00',));d.db.commit();d.spot.cache_clear()
    def test_a2_hedge_closes_on_option_clock(self):
        d=self.make();c=self.hedged();e=self.cand(d,c);r=simulate(d,c,e,'iv_converge','delta_entry_proxy','put')[0][0]
        close=[x for x in r['ledger'] if x['event']=='index_delta_proxy_close'];self.assertEqual(len(close),1)
        last=max(x['fill_time'] for x in r['ledger'] if x.get('mode')=='liquid_bar_proxy');self.assertEqual(close[0]['fill_time'],last)
    def test_a4_spot_before_hedge_close_matters(self):
        d=self.make();c=self.hedged();e=self.cand(d,c);a=simulate(d,c,e,'iv_converge','delta_entry_proxy','put')[0][0]['pnl_inr']
        d.db.execute('UPDATE spot SET open=open+1000 WHERE ts=?',(e['day']+' 11:32:00',));d.db.commit();d.spot.cache_clear()
        self.assertNotAlmostEqual(simulate(d,c,e,'iv_converge','delta_entry_proxy','put')[0][0]['pnl_inr'],a,places=2)
    def test_a3_partial_exit_keeps_hedge_until_last_leg(self):
        d=self.make();c=config(fill_sync='synchronized',fill_model='next_minute',hedge_bps=0.);e=self.cand(d,c)
        self.drop(d,c,e['short_expiry']+' 15:16:00',e['long_expiry'],e['strike']);self.clear(d)
        r=simulate(d,c,e,'weekly','delta_entry_proxy','put')[0][0]
        close=[x for x in r['ledger'] if x['event']=='index_delta_proxy_close'][0]
        self.assertGreater(close['day'],e['short_expiry'])
    def test_hedged_equals_cash_at_end(self):
        d=self.make();c=self.hedged();e=self.cand(d,c);r,m=simulate(d,c,e,'iv_converge','delta_entry_proxy','put')[0]
        self.assertAlmostEqual(m[-1]['pnl'],r['pnl_inr'],places=6)
    def partial(self,d,day):
        d.db.execute('UPDATE spot_daily SET minute_bars=40,complete_session=0 WHERE day=?',(day,));d.db.commit()
        d2=Data(str(self.path));self.addCleanup(d2.close);return d2
    def test_a6_entry_cannot_depend_on_how_the_day_later_ended(self):
        d=self.make();c=config();e=self.cand(d,c);d2=self.partial(d,e['day'])
        rows=[x for x in generate(d2,c,Counter()) if x['day']==e['day']];self.assertTrue(rows)
        self.assertEqual(simulate(d2,c,e,'weekly','none','put')[0][0]['missing_marks']['entry_day_later_incomplete'],1)
    def test_a6b_published_special_sessions_and_weekends_block_entries(self):
        d=self.make();c=config();e=self.cand(d,c);self.assertFalse(d.entry_session_eligible('2026-01-10'))
        d.special_sessions={e['day']};self.assertFalse([x for x in generate(d,c,Counter()) if x['day']==e['day']])
        self.assertTrue([x for x in generate(d,config(allow_special_sessions=True),Counter()) if x['day']==e['day']])
    def test_a7_audit_agrees_on_special_session(self):
        d=self.make();e=self.cand(d,config());d.special_sessions={e['day']}
        a=SimpleNamespace(provider='rissin_upstox',max_short_dte=8,long_tenors=[120],tenor_tolerance=25,option_type='PE',min_signal_volume=1,min_fill_volume=1,min_pair_volume=1,max_parity_bps=40,rate=.06,fill_sync='synchronized',fill_model='next_minute',long_strike_offset=0.,lot_master=config().lot_master)
        self.assertEqual(next(audit_day(d,a,e['day']))[1],[])
    def test_a8_open_position_still_valued_on_partial_day(self):
        d=self.make();c=config();e=self.cand(d,c);mid=[x for x in d.days if e['day']<x<e['short_expiry']][0];d2=self.partial(d,mid)
        r,marks=simulate(d2,c,e,'weekly','none','put')[0];self.assertIn(mid,[m['day'] for m in marks])
    def test_a9_trend_ignores_incomplete_sessions(self):
        rows=[dict(close=100.+i,complete_session=1) for i in range(60)]
        self.assertEqual(trend_fields(rows),trend_fields(rows+[dict(close=9999.,complete_session=0)]))
    def test_a10_a11_causal_fill_ignores_later_minutes(self):
        c=lambda:config(fill_sync='synchronized',fill_model='qualified_minute_close')
        d=self.make();e=self.cand(d,c());self.assertEqual(delay_mode(c()),'close')
        a=[x for x in simulate(d,c(),e,'weekly','none','put')[0][0]['ledger'] if x['event']=='initial_long'][0]
        self.assertEqual((a['fill_time'],a['price_field'],a['executable_claim']),('09:46','close',False))
        for m in ('09:47','09:48','09:49','09:50'):self.drop(d,c(),f"{e['day']} {m}:00",e['long_expiry'],e['strike'])
        self.clear(d)
        b=[x for x in simulate(d,c(),e,'weekly','none','put')[0][0]['ledger'] if x['event']=='initial_long'][0]
        self.assertEqual(a['reference_price'],b['reference_price'])
    def test_causal_price_is_the_close(self):
        d=self.make();c=config(fill_sync='synchronized',fill_model='qualified_minute_close');e=self.cand(d,c)
        d.db.execute("UPDATE opt SET open=open+7 WHERE expiry=? AND strike=?",(e['long_expiry'],e['strike']));self.clear(d)
        q=d.opt(c.provider,e['day'],'09:46',e['long_expiry'],e['strike'],'PE')
        self.assertEqual([x for x in simulate(d,c,e,'weekly','none','put')[0][0]['ledger'] if x['event']=='initial_long'][0]['reference_price'],q['close'])
    def test_compare_fill_models(self):
        from compare_fill_models import compare
        t=Path(tempfile.mkdtemp())
        for name,p in (('a',10.),('b',4.)):
            db=sqlite3.connect(t/f'{name}.sqlite');db.executescript('CREATE TABLE trades(id,economic_id,pnl_inr,lot_units,status);CREATE TABLE fills(trade_id,seq,details);CREATE TABLE marks(trade_id,day,pnl_inr);')
            db.execute('INSERT INTO trades VALUES(?,?,?,?,?)',('x','e',p,1,'closed_traded'))
            db.executemany('INSERT INTO fills VALUES(?,?,?)',[('x',i,'{"mode":"liquid_bar_proxy","units":1}') for i in range(4)]);db.commit();db.close()
        r=compare([('a',t/'a.sqlite'),('b',t/'b.sqlite')])['results'][1]
        self.assertEqual((r['paired_mean_diff_per_unit'],r['breakeven_extra_slippage_pts_per_traded_unit']),(-6.,1.))
    def test_auto_cutoff_uses_data_end_not_trade_exits(self):
        from rank_strategies import resolve_holdout_cutoff as f
        rows=[dict(target_tenor=120,exit_day='2026-02-01')]
        self.assertEqual(f('auto','2026-01-01',['2026-01-02','2026-07-01'],rows),'2026-03-03')
        self.assertIsNone(f('none','2026-01-01',['2026-07-01'],rows))
    def test_cutoff_before_split_is_refused(self):
        from rank_strategies import resolve_holdout_cutoff as f
        with self.assertRaises(SystemExit):f('auto','2026-06-01',['2026-07-01'],[dict(target_tenor=120)])
        with self.assertRaises(SystemExit):f('2026-01-15','2026-02-01',['2026-07-01'],[dict(target_tenor=120)])
    def test_ranking_refuses_changed_market_content(self):
        ok=dict(source_fingerprint='s',enrichment_run_id='r',research_eligible='1',enrichment_complete='1',market_content_digest='a')
        with self.assertRaises(ValueError):lineage.check_inputs(ok,dict(source_fingerprint='s',enrichment_run_id='r',market_content_digest='b'))

class V442(Base):
    """Costs, volumes and information timing, each enforced on every fill path."""
    close=dict(fill_sync='synchronized',fill_model='qualified_minute_close')
    def corrupted(self,cut,day_corrupt=None,delete=False):
        target=Path(self.temp.name)/f'x{abs(hash((cut,delete)))}.sqlite';db=sqlite3.connect(self.path);out=sqlite3.connect(target);db.backup(out);db.close()
        if delete:
            out.execute('DELETE FROM opt WHERE ts>?',(cut,));out.execute('DELETE FROM spot WHERE ts>?',(cut,))
        else:
            out.execute('UPDATE opt SET open=open*1.37,high=high*1.37,low=low*1.37,close=close*1.37,volume=volume+7 WHERE ts>?',(cut,))
            out.execute('UPDATE spot SET open=open*.93,close=close*.93 WHERE ts>?',(cut,))
        day=cut[:10]
        if delete:out.execute('DELETE FROM spot_daily WHERE day>?',(day,))
        else:out.execute('UPDATE spot_daily SET close=close*.9 WHERE day>?',(day,))
        if day_corrupt:out.execute('UPDATE spot_daily SET close=close*2,minute_bars=40,complete_session=0 WHERE day=?',(day,))
        out.commit();out.close();x=Data(str(target));self.addCleanup(x.close);return x
    @staticmethod
    def prefix(result,cut):
        day,minute=cut[:10],cut[11:16]
        return [x for x in result[0]['ledger'] if (x.get('day'),x.get('time') or '15:29')<=(day,minute)]
    def test_signals_ignore_everything_after_0945(self):
        d=self.make();c=config(end='2026-02-20');full={(x['day'],x['strike'],x['long_expiry']):x for x in generate(d,c,Counter())}
        days=sorted({k[0] for k in full});self.assertGreater(len(days),5)
        for day in days[::3]:
            x=self.corrupted(day+' 09:45:00',day_corrupt=True,delete=True)
            for row in generate(x,c,Counter()):
                if row['day']==day:self.assertEqual(row,full[(day,row['strike'],row['long_expiry'])])
    def test_ledger_prefix_ignores_later_data_on_every_policy(self):
        d=self.make(bull=True);cuts=[]
        grid=[(p,'none') for p in ('weekly','carry30','p25_s50','p50_s100','iv_converge','rolling_static','rolling_diagonal','rolling_recenter','atm_weekly_roll','tm1','p25_s50_tm1','rolling_tm1')]+[('weekly','delta_entry_proxy'),('iv_converge','delta_daily_proxy')]
        c=config(iv_convergence=.9,recenter_threshold=.01,max_short_long_width=2000.,**self.close);e=self.cand(d,c)
        checked=0
        for cut in (e['day']+' 09:47:00',e['day']+' 11:31:00',e['day']+' 15:16:00','2026-01-08 09:46:00','2026-01-13 13:31:00','2026-01-14 09:46:00','2026-01-20 15:16:00'):
            x=self.corrupted(cut)
            for policy,hedge in grid:
                a=simulate(d,c,e,policy,hedge,'put')[0];b=simulate(x,c,e,policy,hedge,'put')[0]
                if a is None or b is None:continue
                self.assertEqual(self.prefix(a,cut),self.prefix(b,cut),(cut,policy,hedge));checked+=1
        self.assertGreater(checked,50)
    def test_prefix_harness_detects_a_changed_past(self):
        d=self.make();c=config(**self.close);e=self.cand(d,c);a=simulate(d,c,e,'weekly','none','put')[0];cut=e['day']+' 09:47:00'
        d.db.execute('UPDATE opt SET close=close+3 WHERE ts=?',(e['day']+' 09:46:00',));self.clear(d)
        self.assertNotEqual(self.prefix(a,cut),self.prefix(simulate(d,c,e,'weekly','none','put')[0],cut))
    def test_fill_bar_cannot_veto_the_order(self):
        d=self.make();c=config(**self.close);e=self.cand(d,c)
        d.db.execute('UPDATE opt SET close=.05 WHERE ts=? AND expiry=? AND strike=?',(e['day']+' 09:46:00',e['long_expiry'],e['strike']));self.clear(d)
        self.assertIsNotNone(simulate(d,c,e,'weekly','none','put')[0])
    def test_recenter_obeys_fill_model_and_volume(self):
        d=self.make(bull=True);c=config(fill_model='qualified_minute_close',fill_sync='independent',recenter_threshold=.001);e=self.cand(d,c)
        ev=[x for x in simulate(d,c,e,'rolling_recenter','none','put')[0][0]['ledger'] if x['event'] in ('recenter_old_long_exit','recenter_new_long_open') and x['day']=='2026-01-14']
        self.assertEqual([x['price_field'] for x in ev],['close','close'])
        c2=config(fill_model='qualified_minute_close',fill_sync='independent',recenter_threshold=.001,min_fill_volume=100,min_exit_volume=101)
        self.assertFalse([x for x in simulate(d,c2,e,'rolling_recenter','none','put')[0][0]['ledger'] if x['event']=='recenter_old_long_exit'])
    def test_participation_gate(self):
        from calendar_research import required_volume as f
        self.assertEqual(f(SimpleNamespace(max_participation=.1,volume_unit='contracts'),65,65,1),10)
        self.assertEqual(f(SimpleNamespace(max_participation=.1,volume_unit='units'),65,65,1),650)
        self.assertEqual(f(SimpleNamespace(max_participation=None),65,65,3),3)
        d=self.make();e=self.cand(d,config())
        self.assertIsNotNone(simulate(d,config(max_participation=.1,volume_unit='contracts',**self.close),e,'weekly','none','put')[0])
        out,why=simulate(d,config(max_participation=.1,volume_unit='units',**self.close),e,'weekly','none','put')
        self.assertIsNone(out);self.assertEqual(why,'missing_liquid_entry_leg')
    def test_exit_needs_participation_volume_too(self):
        d=self.make();c=config(max_participation=.1,volume_unit='contracts',**self.close);e=self.cand(d,c)
        d.db.execute("UPDATE opt SET volume=5 WHERE expiry=? AND ts LIKE ?",(e['long_expiry'],e['short_expiry']+' 15:1%'));self.clear(d)
        r=simulate(d,c,e,'weekly','none','put')[0][0]
        self.assertFalse([x for x in r['ledger'] if x['event']=='end_long_exit' and x['day']==e['short_expiry']])
    def test_slippage_same_for_either_volume_unit(self):
        d=self.make();c=lambda u:config(slippage=.5,slippage_mode='tenor_volume',volume_unit=u,**self.close);e=self.cand(d,c('contracts'))
        a=simulate(d,c('contracts'),e,'weekly','none','put')[0][0]['pnl_inr']
        d.db.execute('UPDATE opt SET volume=volume*?',(e['qty'],));self.clear(d)
        self.assertAlmostEqual(simulate(d,c('units'),e,'weekly','none','put')[0][0]['pnl_inr'],a,places=6)
    def test_volume_unit_detection(self):
        from calendar_research import detect_volume_unit as f
        d=self.make();lot=lambda day,ex:65
        self.assertEqual(f(d.db,'rissin_upstox',lot),'contracts')
        d.db.execute('UPDATE opt SET volume=volume*65');self.assertEqual(f(d.db,'rissin_upstox',lot),'units')
    def costs(self,**k):
        return SimpleNamespace(**{**dict(slippage=.5,slippage_mode='flat',brokerage=20.,gst=.18,exchange_bps=None,sebi_bps=.01,stt=None,stamp=.00003,fill_model=''),**k})
    def test_sell_costs_by_hand(self):
        p=65*99.5;levy=p*((.0003553+.000001)*1.18+.0015);amount,actual,slip=fill_flow(100.,-65,'2026-05-01',self.costs(),'2026-05-07',100)
        self.assertAlmostEqual(amount,p-levy-23.6,places=8);self.assertEqual((actual,slip),(99.5,.5))
    def test_buy_costs_by_hand(self):
        p=65*100.5;levy=p*((.0003553+.000001)*1.18+.00003)
        self.assertAlmostEqual(fill_flow(100.,65,'2026-05-01',self.costs(),'2026-05-07',100)[0],-p-levy-23.6,places=8)
    def test_stt_and_exchange_schedule(self):
        from calendar_research import exchange_rate as ex
        c=self.costs();self.assertEqual((ex('2024-09-30',c),ex('2024-10-01',c)),(.000495,.0003553))
        self.assertEqual(ex('2026-05-01',self.costs(exchange_bps=3.)),.0003)
        sell=lambda day:fill_flow(100.,-65,day,self.costs(brokerage=0.,exchange_bps=0.,sebi_bps=0.),'2026-05-07',100)[0]
        self.assertAlmostEqual(sell('2024-09-30'),65*99.5*(1-.000625),places=8);self.assertAlmostEqual(sell('2024-10-01'),65*99.5*(1-.001),places=8)
        self.assertAlmostEqual(sell('2026-03-31'),65*99.5*(1-.001),places=8);self.assertAlmostEqual(sell('2026-04-01'),65*99.5*(1-.0015),places=8)
    def test_hedge_costs_by_side(self):
        from calendar_research import hedge_cost as f
        c=SimpleNamespace(hedge_bps=1.,hedge_statutory=True,gst=.18,brokerage=20.);n=65*25000.
        base=n*.0001+n*(.0000183+.000001)*1.18+23.6
        self.assertAlmostEqual(f(65,25000.,'2026-05-01',c),base+n*.00002,places=6);self.assertAlmostEqual(f(-65,25000.,'2026-05-01',c),base+n*.0005,places=6)
        self.assertAlmostEqual(f(-65,25000.,'2026-03-31',c),base+n*.0002,places=6);self.assertEqual(f(0,25000.,'2026-05-01',c),0.)
    def test_exercise_fee_on_itm_settlement(self):
        d=self.make();c=config(exercise_fee=20.,gst=.18,**self.close);e=self.cand(d,c)
        for m in ('15:16','15:17'):self.drop(d,c,f"{e['short_expiry']} {m}:00",e['short_expiry'],e['strike'])
        d.db.execute('UPDATE spot SET close=close-1000 WHERE ts=?',(e['short_expiry']+' 15:29:00',));self.clear(d)
        ev=[x for x in simulate(d,c,e,'weekly','none','put')[0][0]['ledger'] if 'settlement' in x['event'] and x['units']<0][0]
        self.assertAlmostEqual(ev['exercise_fee'],23.6);self.assertAlmostEqual(ev['cash_flow'],ev['units']*ev['payoff']-23.6)
    def test_every_cash_movement_is_in_the_ledger(self):
        d=self.make();c=config(slippage=.5,brokerage=20.,gst=.18,exchange_bps=3.,stt=.001,stamp=.00003,**self.close);e=self.cand(d,c)
        r=simulate(d,c,e,'weekly','none','put')[0][0]
        self.assertAlmostEqual(sum(x['cash_flow'] for x in r['ledger'] if 'cash_flow' in x),r['pnl_inr'],places=6)
    def test_unobservable_stop_is_reported(self):
        d=self.make();c=config(iv_convergence=99.,**self.close);e=self.cand(d,c)
        for m in ('11:30','13:30','15:15'):self.drop(d,c,f"{e['day']} {m}:00",e['long_expiry'],e['strike'])
        self.clear(d);r=simulate(d,c,e,'iv_converge','none','put')[0][0]
        self.assertEqual(r['missing_marks']['stop_unobservable_checkpoints'],3)
        self.assertEqual(len([x for x in r['ledger'] if x['event']=='stop_unobservable']),3)
    def test_partial_day_stays_in_the_risk_calendar(self):
        from rank_strategies import valuation_sessions as f
        self.assertEqual(f([dict(day='a',minute_bars=375,complete_session=1),dict(day='b',minute_bars=40,complete_session=0),dict(day='c',minute_bars=0,complete_session=0)]),['a','b'])
    def test_conditional_fill_model_refused_by_cli(self):
        root=Path(__file__).resolve().parents[1]
        r=subprocess.run([sys.executable,str(root/'calendar_research.py'),'--fill-model','next_minute'],capture_output=True,text=True)
        self.assertEqual(r.returncode,2);self.assertIn('conditions the fill',r.stderr)

class V443(Base):
    """Acceptance tests named in the V4.4.2 review."""
    corrupted=V442.corrupted;prefix=staticmethod(V442.prefix)
    causal=dict(fill_sync='synchronized',fill_model='next_bar_open')
    tariff=dict(slippage=0.,slippage_mode='flat',fill_model='',brokerage=20.,gst=.18,exchange_bps=None,sebi_bps=.01,stt=None,stamp=.00003)
    def test_cost01_order_modes_by_hand(self):
        from calendar_research import charged_orders as f
        n=lambda m,lots,**k:f(65*lots,65,SimpleNamespace(brokerage_order_mode=m,**k))
        for lots in (1,2,3,10):
            self.assertEqual(n('one_order_per_leg',lots),1);self.assertEqual(n('per_lot_orders',lots),lots)
        self.assertEqual([n('lots_per_order',x,lots_per_order=2) for x in (1,2,3,10)],[1,1,2,5]);self.assertEqual(n('fixed_orders',3,fixed_orders=2),2)
    def test_cost01_brokerage_moves_taxes_do_not(self):
        c=lambda m:SimpleNamespace(brokerage_order_mode=m,**self.tariff)
        one=fill_flow(100.,195,'2026-05-05',c('one_order_per_leg'),'2026-06-30',1000,lot=65)[0];per=fill_flow(100.,195,'2026-05-05',c('per_lot_orders'),'2026-06-30',1000,lot=65)[0]
        self.assertAlmostEqual(one-per,47.2,places=8)
    def test_cost02_line_items_rebuild_the_cash_flow(self):
        from calendar_research import fill_detail
        cash,actual,_,x=fill_detail(100.,-130,'2026-05-05',SimpleNamespace(brokerage_order_mode='per_lot_orders',**self.tariff),'2026-06-30',1000,lot=65)
        p=130*actual;self.assertAlmostEqual(x['stt'],p*.0015);self.assertEqual(x['stamp'],0.);self.assertEqual(x['brokerage'],40.);self.assertAlmostEqual(x['gst'],(40+x['exchange']+x['sebi'])*.18)
        self.assertAlmostEqual(cash,p-sum(x[k] for k in ('brokerage','exchange','sebi','gst','stt','stamp')),places=8)
    def test_cost02_lots_scale_whole_trade(self):
        d=self.make();kw=dict(brokerage=20.,gst=.18,brokerage_order_mode='per_lot_orders',**self.causal);e=self.cand(d,config(**kw))
        one=simulate(d,config(lots=1,**kw),e,'weekly','none','put')[0][0];three=simulate(d,config(lots=3,**kw),e,'weekly','none','put')[0][0]
        self.assertTrue(all(x['charges']['charged_orders']==3 for x in three['ledger'] if 'charges' in x))
        self.assertEqual(three['lot_units'],3*one['lot_units'])
        self.assertAlmostEqual(three['pnl_inr'],3*one['pnl_inr'],places=6)   # flat brokerage per lot is linear, unlike one order per leg
        b=simulate(d,config(lots=3,brokerage_order_mode='one_order_per_leg',**{k:v for k,v in kw.items() if k!='brokerage_order_mode'}),e,'weekly','none','put')[0][0]
        self.assertAlmostEqual(b['pnl_inr']-three['pnl_inr'],one['executed_fills']*2*23.6,places=6)
    def test_cost03_rate_boundaries(self):
        from calendar_research import exchange_rate as ex,hedge_cost
        c=SimpleNamespace(**self.tariff);self.assertEqual(ex('2024-09-30',c),.000495);self.assertEqual(ex('2024-10-01',c),.0003553);self.assertEqual(ex('2026-02-28',c),ex('2026-03-01',c))
        h=SimpleNamespace(hedge_bps=0.,hedge_statutory=True,gst=0.,brokerage=0.);n=65*25000.
        self.assertAlmostEqual(hedge_cost(65,25000.,'2024-09-30',h)-hedge_cost(65,25000.,'2024-10-01',h),n*.0000015,places=6)
    def test_cost03_futures_brokerage_is_capped(self):
        from calendar_research import hedge_cost
        h=SimpleNamespace(hedge_bps=0.,hedge_statutory=True,gst=0.,brokerage=20.)
        tiny=hedge_cost(.1,25000.,'2026-05-01',h)-.1*25000.*(.0000183+.000001+.00002);self.assertAlmostEqual(tiny,.75,places=6)
    def test_cost04_invalid_settings_refused(self):
        from calendar_research import validate_config as v
        ok=dict(slippage=.5,brokerage=20.,hedge_bps=2.,sebi_bps=.01,exercise_fee=20.,gst=.18,stamp=.00003,mark_stress=.2,stt=None,exercise_stt=None,exchange_bps=None,max_participation=.1,lots=1,lots_per_order=1,fixed_orders=1,start='2025-01-01',end='2025-02-01')
        self.assertEqual(v(SimpleNamespace(**ok)),[])
        for k,x in (('slippage',-.1),('brokerage',-1.),('gst',-.1),('gst',1.5),('stamp',-1e-5),('stt',-.1),('exchange_bps',-1.),('sebi_bps',-.1),('exercise_fee',-1.),('hedge_bps',-1.),('max_participation',-.1),('max_participation',1.1),('lots',0),('lots_per_order',0),('start','2026-01-01')):
            self.assertTrue(v(SimpleNamespace(**{**ok,k:x})),(k,x))
        with self.assertRaises(ValueError):
            from calendar_research import required_volume
            required_volume(SimpleNamespace(max_participation=-.1,volume_unit='units'),65,65,1)
    @staticmethod
    def bars(v47=50,c47=90.,v46=50):
        class D:
            def opt(self,provider,day,minute,expiry,strike,cp):
                v={'09:46':v46,'09:47':v47}.get(minute,0)
                return dict(open=100. if minute=='09:46' else 91.,close=99. if minute=='09:46' else c47,high=101.,low=89.,volume=v)
        return D()
    def test_time01_gate_is_finished_minute_fill_is_next_open(self):
        args=('p','2026-01-14','2026-02-24',25000.,'PE');m=('09:46','09:47')
        base=quote_fill(self.bars(),*args,minutes=m,minimum=10,delayed='next_open')
        self.assertEqual(base[:2],(91.,'09:47'));self.assertEqual(base[7:10],('09:46','next_bar_open_after_eligible_minute','open'))
        self.assertEqual(quote_fill(self.bars(v47=1,c47=500.),*args,minutes=m,minimum=10,delayed='next_open')[:2],(91.,'09:47'))   # the fill bar's later volume and close cannot gate it
        self.assertEqual(quote_fill(self.bars(v47=0),*args,minutes=m,minimum=10,delayed='next_open')[:2],(91.,'09:47'))   # later volume cannot veto an opening-price fill
        self.assertIsNone(quote_fill(self.bars(v46=5),*args,minutes=m,minimum=10,delayed='next_open'))   # the finished minute was too thin
    def test_time01_synchronized_needs_every_leg(self):
        d=self.make();c=config(**self.causal);e=self.cand(d,c);legs=[(e['long_expiry'],e['strike'],'PE'),(e['short_expiry'],e['strike'],'PE')]
        got=synchronized_fills(d,c.provider,e['day'],legs,minimum=1,delayed='next_open');self.assertEqual({x[1] for x in got},{'09:47'});self.assertEqual({x[7] for x in got},{'09:46'})
    def test_time02_labels(self):
        d=self.make();e=self.cand(d,config())
        for model,label in (('next_bar_open','causal_ohlc_proxy'),('next_bar_open_adverse','ex_post_adverse_stress'),('qualified_minute_close','conditional_ohlc_proxy'),('next_minute','conditional_ohlc_proxy')):
            r=simulate(d,config(fill_sync='synchronized',fill_model=model),e,'weekly','none','put')[0][0]
            self.assertEqual({x['evidence_class'] for x in r['ledger'] if 'charges' in x},{label});self.assertFalse(any(x.get('executable_claim') for x in r['ledger']))
    def test_time02_close_model_refused_by_cli(self):
        root=Path(__file__).resolve().parents[1]
        r=subprocess.run([sys.executable,str(root/'calendar_research.py'),'--fill-model','qualified_minute_close'],capture_output=True,text=True);self.assertEqual(r.returncode,2)
    def test_time02_adverse_never_beats_open(self):
        d=self.make();e=self.cand(d,config())
        d.db.execute('UPDATE opt SET high=open+2,low=MAX(open-2,.05)');self.clear(d)
        a=simulate(d,config(**self.causal),e,'weekly','none','put')[0][0]['pnl_inr'];b=simulate(d,config(fill_sync='synchronized',fill_model='next_bar_open_adverse'),e,'weekly','none','put')[0][0]['pnl_inr']
        self.assertLess(b,a)
    def test_time03_every_minute_boundary(self):
        from prepare_research_cache import TIMES
        d=self.make(bull=True);c=config(iv_convergence=.9,recenter_threshold=.01,max_short_long_width=2000.,**self.causal);e=self.cand(d,c);checked=0
        for day in (e['day'],'2026-01-08',e['short_expiry'],'2026-01-14'):
            for m in [x for x in TIMES if x!='15:29']:
                cut=f'{day} {m}:00';x=self.corrupted(cut);x.db.execute('UPDATE opt SET close=close*1.37,high=high*1.37,low=low*1.37,volume=volume+7 WHERE ts=?',(cut,));x.db.commit()
                for policy,hedge in (('weekly','none'),('iv_converge','none'),('rolling_recenter','none'),('atm_weekly_roll','none'),('weekly','delta_entry_proxy')):
                    a=simulate(d,c,e,policy,hedge,'put')[0];b=simulate(x,c,e,policy,hedge,'put')[0]
                    if a and b:
                        known=lambda r:[z for z in self.prefix(r,cut) if not (z['event']=='stop_unobservable' and z['day']==day and z['time']==m)]   # judged from that bar's close, so not yet known at its open
                        self.assertEqual(known(a),known(b),(cut,policy,hedge));checked+=1
        self.assertGreater(checked,200)
    def months(self,d,first,second):
        d.db.execute("UPDATE opt SET volume=? WHERE substr(ts,1,7)='2026-01'",(first,));d.db.execute("UPDATE opt SET volume=? WHERE substr(ts,1,7)='2026-02'",(second,));d.db.execute("DELETE FROM opt WHERE substr(ts,1,7)>'2026-02'");d.db.execute("DELETE FROM spot_daily WHERE substr(day,1,7)>'2026-02'")
    def test_vol01_every_month_must_agree(self):
        from calendar_research import detect_volume_unit as f,volume_unit_report as r
        lot=lambda day,ex:65
        d=self.make();self.months(d,130,195);self.assertEqual(f(d.db,'rissin_upstox',lot),'units')
        self.months(d,131,197);self.assertEqual(f(d.db,'rissin_upstox',lot),'contracts')
        self.months(d,131,195);self.assertIsNone(f(d.db,'rissin_upstox',lot));self.assertEqual({v['unit'] for v in r(d.db,'rissin_upstox',lot).values()},{'units','contracts'})
    def test_vol02_participation_follows_order_size(self):
        d=self.make();e=self.cand(d,config())   # bars carry 100 contracts
        self.assertIsNotNone(simulate(d,config(lots=10,max_participation=.1,volume_unit='contracts',**self.causal),e,'weekly','none','put')[0])
        self.assertIsNone(simulate(d,config(lots=11,max_participation=.1,volume_unit='contracts',**self.causal),e,'weekly','none','put')[0])
    def test_sig01_every_quantile_signal_has_rules(self):
        from rank_strategies import signal_grid,QUANTILE_SIGNALS,SIGNALS
        random.seed(3)
        self.assertLessEqual(QUANTILE_SIGNALS,set(SIGNALS))
        for sig in QUANTILE_SIGNALS:
            v=[random.gauss(.003,.002) for _ in range(200)]+[None]*10;g=signal_grid(sig,v,[.8,1],[1.5,999],[.25,.5,.75])
            self.assertEqual(len(g),9)
            self.assertTrue(all(sum(1 for x in v if x is not None and lo<=x<hi)>0 for _,lo,hi in g))
    def test_sig01_ratio_signals_keep_the_ratio_grid(self):
        from rank_strategies import signal_grid
        self.assertEqual(signal_grid('iv_ratio',[1.1],[1.],[1.5,999],[.5]),[(('iv_ratio',1.,1.5),1.,1.5),(('iv_ratio',1.,999),1.,999)])
    def test_sig01_fold_thresholds_use_only_its_past(self):
        from rank_strategies import nested_folds
        days=[f'2026-{m:02d}-{d:02d}' for m in (1,2,3,4) for d in (3,10,17,24)]
        def rows(future):
            out=[]
            for i,day in enumerate(days):
                out.append(dict(id=str(i),day=day,exit_day=day,skew_slope=(i if day<'2026-03' else future),pnl_inr=1.+i%3,entry_capital=1.,lot_units=65,status='closed_traded'))
            return out
        class M:
            def evaluate(self,chosen,*a,**k):return dict(sharpe=1.+len(chosen)/100,net=1.,max_dd=0.,capital_scenario_peak=1.,n=len(chosen))
            def series(self,chosen,*a,**k):return (np.cumsum(np.resize((1.,3.),len(days))),np.ones(len(days),dtype=bool),np.ones(len(days)))
        M.days=days;seen=[]
        for future in (1.,1000.):
            r=rows(future);rule=('skew_slope',.5,1.);out=nested_folds({rule:r},M(),[('2026-03-01','2026-03-31')],0,'2026-12-31',9,None,'base',None,2,{rule})
            seen.append(out[0]['resolved_bounds'])
        self.assertEqual(seen[0],seen[1]);self.assertIsNotNone(seen[0])
    def test_mkt01_one_spot_source(self):
        from prepare_research_cache import SPOT_SQL,DAILY_SQL,DAILY_RV_SQL
        path="f.relative_path='india-index-options-1m/index/NIFTY.parquet'"
        for sql in (SPOT_SQL,DAILY_SQL,DAILY_RV_SQL):self.assertIn(path,sql)
    def test_risk01_force_exit_stress(self):
        d=self.make();kw=dict(iv_convergence=.1,**self.causal);e=self.cand(d,config(**kw))
        for m in ('11:30','11:31','11:32'):self.drop(d,config(),f"{e['day']} {m}:00",e['long_expiry'],e['strike'])
        self.clear(d)
        wait=simulate(d,config(unobservable_stop_policy='wait',**kw),e,'iv_converge','none','put')[0][0];force=simulate(d,config(unobservable_stop_policy='force_exit',**kw),e,'iv_converge','none','put')[0][0]
        self.assertEqual(wait['exit_reason'],'weekly_exit');self.assertEqual(force['exit_reason'],'unobservable_stop_forced')
        self.assertEqual(force['missing_marks']['stop_unobservable_checkpoints'],1);self.assertLess(force['exit_day'],wait['exit_day'])
    def test_risk02_ledger_alone_rebuilds_pnl(self):
        d=self.make(bull=True);c=config(hedge_bps=2.,gst=.18,brokerage=20.,hedge_statutory=True,slippage=.5,**self.causal);e=self.cand(d,c)
        for hedge in ('none','delta_entry_proxy','delta_daily_proxy'):
            r,m=simulate(d,c,e,'weekly',hedge,'put')[0]
            self.assertAlmostEqual(sum(x.get('cash_flow',0.)+x.get('hedge_cash',0.) for x in r['ledger']),r['pnl_inr'],places=6,msg=hedge);self.assertAlmostEqual(m[-1]['pnl'],r['pnl_inr'],places=6,msg=hedge)

class V444(Base):
    """V4.4.3 reviews: acceptance fixtures named by ChatGPT, plus the Gemini items that held up."""
    corrupted=V442.corrupted;prefix=staticmethod(V442.prefix)
    causal=dict(fill_sync='synchronized',fill_model='next_bar_open')
    @staticmethod
    def quotes(v47):
        class D:
            def opt(self,provider,day,minute,expiry,strike,cp):
                base={'09:46':(99,100,100),'09:47':(103,104,v47),'09:48':(106,107,50)}
                if minute not in base:return None
                o,c,vol=base[minute];return dict(open=float(o),close=float(c),high=float(c+1),low=float(o-1),volume=vol,source_id=1,source_row=1)
        return D()
    def test_time04_later_volume_cannot_move_an_open_fill(self):
        out=[quote_fill(self.quotes(v),'p','2026-01-07','2026-01-13',25000.,'PE',delayed='next_open')[:5] for v in (0,1,50)]
        self.assertEqual(out[0],out[1]);self.assertEqual(out[1],out[2]);self.assertEqual(out[0][:2],(103.,'09:47'));self.assertEqual(out[0][2],100)
    def test_time05_synchronized_second_leg_zero_volume(self):
        out=[synchronized_fills(self.quotes(v),'p','2026-01-07',[('e',1.,'PE'),('f',2.,'PE')],delayed='next_open') for v in (0,50)]
        self.assertEqual([x[:5] for x in out[0]],[x[:5] for x in out[1]]);self.assertEqual(out[0][0][1],'09:47')
    def test_zero_volume_fill_is_flagged_not_hidden(self):
        d=self.make();c=config(**self.causal);e=self.cand(d,c)
        d.db.execute('UPDATE opt SET volume=0 WHERE ts=? AND expiry=?',(e['day']+' 09:47:00',e['short_expiry']));self.clear(d)
        r=simulate(d,c,e,'weekly','none','put')[0][0];first=[x for x in r['ledger'] if x['event']=='initial_short'][0]
        self.assertTrue(first['fill_bar_zero_volume']);self.assertEqual(first['liquidity_asof'],'09:46');self.assertEqual(r['missing_marks']['fills_on_zero_volume_bar'],1)
    def test_time06_only_the_stress_reads_the_fill_bar_range(self):
        d=self.make();e=self.cand(d,config())
        def run(model,mode='flat'):return simulate(d,config(fill_sync='synchronized',fill_model=model,slippage_mode=mode,intrabar_lambda=.5,intrabar_min=.05),e,'weekly','none','put')[0][0]['pnl_inr']
        a=run('next_bar_open');a2=run('next_bar_open','intrabar_range');b=run('next_bar_open_adverse')
        d.db.execute('UPDATE opt SET high=high+5,low=MAX(low-5,.05) WHERE ts=?',(e['day']+' 09:47:00',));self.clear(d)
        self.assertEqual(run('next_bar_open'),a);self.assertEqual(run('next_bar_open','intrabar_range'),a2);self.assertLess(run('next_bar_open_adverse'),b)
    def test_timestamps_are_monotonic_everywhere(self):
        d=self.make(bull=True);c=config(iv_convergence=.9,recenter_threshold=.01,max_short_long_width=2000.,**self.causal);e=self.cand(d,c);seen=0
        for policy in ('weekly','iv_converge','p25_s50','rolling_static','rolling_recenter','atm_weekly_roll'):
            for x in simulate(d,c,e,policy,'none','put')[0][0]['ledger']:
                if x.get('mode')=='liquid_bar_proxy':
                    self.assertLessEqual(x['signal_time'],x['observed_bar_minute'],x);self.assertLess(x['observed_bar_minute'],x['fill_time'],x);seen+=1
        self.assertGreater(seen,20)
    def test_risk03_missing_0945_keeps_the_mark(self):
        d=self.make();c=config();e=self.cand(d,c);d.db.execute("DELETE FROM spot WHERE ts='2026-01-09 09:45:00'");d.db.commit();d2=Data(str(self.path));self.addCleanup(d2.close)
        self.assertIn('2026-01-09',d2.valuation_days);self.assertNotIn('2026-01-09',d2.days)
        self.assertIn('2026-01-09',[m['day'] for m in simulate(d2,c,e,'carry30','none','put')[0][1]])
    def test_risk03_no_new_entry_without_0945(self):
        d=self.make();d.db.execute("DELETE FROM spot WHERE ts='2026-01-09 09:45:00'");d.db.commit();d2=Data(str(self.path));self.addCleanup(d2.close)
        self.assertFalse([x for x in generate(d2,config(end='2026-02-20'),Counter()) if x['day']=='2026-01-09'])
    def test_risk04_missing_eod_spot_uses_daily_close_and_flags_it(self):
        d=self.make();c=config();e=self.cand(d,c);d.db.execute("DELETE FROM spot WHERE ts IN ('2026-01-09 15:29:00','2026-01-09 15:16:00')");d.db.commit();d2=Data(str(self.path));self.addCleanup(d2.close)
        m=[x for x in simulate(d2,c,e,'carry30','none','put')[0][1] if x['day']=='2026-01-09'];self.assertEqual(len(m),1);self.assertIn('SPOT_EOD_DAILY_CLOSE',m[0]['quality'])
    def test_stats05_rank_calendar_equals_simulator_calendar(self):
        from rank_strategies import valuation_sessions
        d=self.make();self.assertEqual(valuation_sessions([dict(x) for x in d.daily.values()]),d.valuation_days)
    def test_cover03_audit_and_simulator_agree_on_size(self):
        d=self.make();
        for lots in (1,5,10,11):
            for cap in (.01,.02,.05,.1):
                c=config(lots=lots,max_participation=cap,volume_unit='contracts',long_strike_offset=0.,**self.causal);e=self.cand(d,c)
                got=next(t for t in audit_day(d,c,e['day']));sim=simulate(d,c,e,'weekly','none','put')[0]
                self.assertEqual('admitted' in got[1],sim is not None,(lots,cap))
    def test_cover03_debit_gate_matches(self):
        d=self.make();c=config(long_strike_offset=0.,min_debit=1e12,**self.causal);e=self.cand(d,config(**self.causal))
        self.assertNotIn('admitted',next(audit_day(d,c,e['day']))[1]);self.assertEqual(simulate(d,c,e,'weekly','none','put')[1],'nonpositive_debit')
    def test_cost05_three_lot_double_calendar_fee_count(self):
        d=self.make();c=config(lots=3,brokerage=20.,gst=.18,brokerage_order_mode='per_lot_orders',double_z=.75,**self.causal);e=self.cand(d,c)
        r=simulate(d,c,e,'weekly','none','double')[0][0];initial=[x for x in r['ledger'] if x['event'].startswith('initial_')]
        self.assertEqual(len(initial),4);self.assertEqual(sum(x['charges']['charged_orders'] for x in initial),12)
    def settled(self,c):
        d=self.make();e=self.cand(d,c)
        for m in ('15:16','15:17'):self.drop(d,c,f"{e['short_expiry']} {m}:00",e['short_expiry'],e['strike'])
        d.db.execute('UPDATE spot SET close=close-1000 WHERE ts=?',(e['short_expiry']+' 15:29:00',));self.clear(d)
        return [x for x in simulate(d,c,e,'weekly','none','put')[0][0]['ledger'] if 'settlement' in x['event'] and x['units']<0][0]
    def test_cost06_exercise_fee_basis(self):
        base=dict(exercise_fee=20.,gst=0.,lots=3,**self.causal)
        for basis,want in (('per_leg',20.),('per_contract',60.),('zero',0.)):self.assertAlmostEqual(self.settled(config(exercise_fee_basis=basis,**base))['exercise_fee'],want)
        self.assertAlmostEqual(self.settled(config(exercise_fee_basis='per_order',brokerage_order_mode='lots_per_order',lots_per_order=2,**base))['exercise_fee'],40.)
    def test_greek02_stop_basis_includes_every_entry_leg(self):
        d=self.make();c=config(brokerage=20.,gst=.18,hedge_bps=2.,max_gamma_lots=3,**self.causal);e=self.cand(d,c);r=simulate(d,c,e,'weekly','gamma_reduced_delta_proxy','put')
        if r[0] is None:self.skipTest(r[1])
        led=r[0][0]['ledger'];paid=-sum(x.get('cash_flow',0.) for x in led if x['day']==e['day'] and x['event'] in ('initial_long','initial_short','gamma_reduction_option_buy'))-sum(x['hedge_cash'] for x in led if x['event']=='index_delta_proxy_open')
        self.assertAlmostEqual(r[0][0]['initial_debit_inr'],max(paid,c.min_debit),places=6)
    def test_prov04_calendar_files_are_hashed(self):
        import lineage as L
        src=Path(__file__).resolve().parents[1]/'calendar_research.py';t=src.read_text()
        self.assertIn('special_sessions_sha256',t);self.assertIn('holiday_calendar_sha256',t)
        f=Path(tempfile.mkdtemp())/'s.txt';f.write_text('2026-01-09\n');a=L.file_digest(f);f.write_text('2026-01-10\n');self.assertNotEqual(a,L.file_digest(f))
    def test_recenter_is_all_or_none_without_a_short(self):
        d=self.make(bull=True);c=config(recenter_threshold=.001,fill_sync='independent',fill_model='next_bar_open');e=self.cand(d,c)
        d.db.execute("DELETE FROM opt WHERE day='2026-01-14' AND expiry<'2026-02-01' AND expiry>'2026-01-14' AND substr(ts,12,5)>='09:46' AND substr(ts,12,5)<='09:50'");self.clear(d)
        r=simulate(d,c,e,'rolling_recenter','none','put')[0][0]
        self.assertFalse([x for x in r['ledger'] if x['day']=='2026-01-14' and x['event'].startswith('recenter')])
    def test_recenter_records_why_it_could_not_happen(self):
        d=self.make(bull=True);c=config(recenter_threshold=.001,tenor_tolerance=0,**self.causal);e=self.cand(d,config(recenter_threshold=.001,**self.causal))
        r=simulate(d,c,e,'rolling_recenter','none','put')[0][0];self.assertGreater(r['missing_marks']['recenter_no_long_tenor'],0);self.assertGreater(r['missing_marks']['recenter_fell_back_to_plain_roll'],0)
    def test_signal_iv_is_the_simulator_iv(self):
        d=self.make();c=config();e=self.cand(d,c);g=d.greek(c.provider,e['day'],'09:45',e['short_expiry'],e['strike'],'PE',c.rate,c.min_pair_volume,c.max_parity_bps)
        self.assertEqual(e['iv_short'],g['iv'])
    def test_attribution_is_exact_on_a_joint_move(self):
        f,k,t0,iv,r=25000.,24500.,.1,.2,.06;f1,iv1,t1=24400.,.24,.1-1/365.25
        p0=black_forward(f,k,t0,iv,'PE',r);p1=black_forward(f1,k,t1,iv1,'PE',r);a=attribute_leg(dict(price=p0,iv=iv,f=f,t=t0,r=r),p1,f1,iv1,t1,k,'PE',1)
        self.assertAlmostEqual(a['residual'],0.,places=8);self.assertAlmostEqual(sum(a.values()),p1-p0,places=8);self.assertGreater(abs(a['vanna_volga']),.5)
    def test_derived_signals(self):
        from rank_strategies import ratio,SIGNALS,QUANTILE_SIGNALS
        self.assertAlmostEqual(ratio(dict(iv5=.2,iv30=.2,iv90=.2),'annual_variance_curvature_5_30_90'),0.);self.assertGreater(ratio(dict(iv5=.2,iv30=.3,iv90=.2),'annual_variance_curvature_5_30_90'),0)
        self.assertIsNone(ratio(dict(iv5=None,iv30=.3,iv90=.2),'annual_variance_curvature_5_30_90'));self.assertAlmostEqual(ratio(dict(iv_to_rv_yz=1.5,forward_iv_ratio=1.25),'fwd_iv_to_rv_yz'),1.2)
        self.assertIn('annual_variance_curvature_5_30_90',SIGNALS);self.assertIn('fwd_iv_to_rv_yz',SIGNALS);self.assertIn('annual_variance_curvature_5_30_90',QUANTILE_SIGNALS)
    def test_plateau_neighbours(self):
        from rank_strategies import plateau
        s={('iv_ratio',1.,1.5):2.,('iv_ratio',.8,1.5):1.,('iv_ratio',1.1,1.5):-1.,('iv_ratio',1.,2.):3.}
        got=plateau(('iv_ratio',1.,1.5),s,[.8,1.,1.1],[1.2,1.5,2.],[.5]);self.assertEqual(got['neighbours_scored'],3);self.assertEqual(got['median_sharpe'],1.);self.assertAlmostEqual(got['positive_share'],2/3)
        q=plateau(('skew_slope',.25,.75),{('skew_slope',0.,.75):1.},[],[],[.25,.5,.75]);self.assertEqual(q['neighbours_scored'],1)
    def test_shift_moves_whole_days(self):
        from rank_strategies import circular_shift_control as f
        days=[f'2026-{1+i//28:02d}-{1+i%28:02d}' for i in range(40)];rows=[dict(id=f'{d}{k}',day=d,exit_day=d) for d in days for k in 'ab'];seen=[]
        class M:
            def evaluate(self,chosen,*a,**k):seen.append({x['day'] for x in chosen});return dict(sharpe=float(len(chosen)))
        values=[float(i//2%2) for i in range(len(rows))]
        out=f(rows,values,[(.5,2.)],M(),99,None,'base',None,1,5);self.assertEqual(out['shifts'],5)
        for got in seen:self.assertEqual(len(got),20)   # both rows of a day stay together; no day is split
    def test_bad_exercise_basis_refused(self):
        from calendar_research import exercise_fee_count
        with self.assertRaises(ValueError):exercise_fee_count(65,65,SimpleNamespace(exercise_fee_basis='x'))

class V445(Base):
    """V4.4.4 review acceptance gates."""
    causal=dict(fill_sync='synchronized',fill_model='next_bar_open')
    def aliases(self,**k):
        return config(round_steps=[100,500],strike_values=[0.],end='2026-01-09',**k)
    def test_can01_one_simulation_and_identical_results(self):
        import calendar_research as C
        d=self.make();c=self.aliases();cands=[x for x in generate(d,c,Counter()) if x['day']=='2026-01-07']
        self.assertEqual({x['strike_step'] for x in cands},{100,500});self.assertEqual(len({x['strike'] for x in cands}),1)
        calls=[];real=C.simulate
        def counted(*a,**k):calls.append(1);return real(*a,**k)
        memo={}
        try:
            C.simulate=counted;got=[C.simulate_once(memo,d,c,x,'weekly','none','put') for x in cands]
        finally:C.simulate=real
        self.assertEqual(len(calls),1);self.assertEqual([g[2] for g in got],[False,True])
        for x,g in zip(cands,got):
            direct=simulate(d,c,x,'weekly','none','put')[0]
            self.assertEqual(g[0][0],direct[0]);self.assertEqual(g[0][1],direct[1])
    def test_can01_failures_are_shared_and_labels_kept(self):
        from calendar_research import simulate_once
        d=self.make();c=self.aliases(min_debit=1e12);cands=[x for x in generate(d,c,Counter()) if x['day']=='2026-01-07'];memo={}
        self.assertEqual([simulate_once(memo,d,c,x,'weekly','none','put')[1:] for x in cands],[('nonpositive_debit',False),('nonpositive_debit',True)])
    def test_can02_double_uses_actual_wings(self):
        from calendar_research import economic_key
        c=config(double_z=.75);base=dict(day='2026-01-07',short_expiry='2026-01-13',long_expiry='2026-04-28',qty=65,target_tenor=120,strike=25000.,short_f=25000.,iv_short=.2,long_strike=25000.)
        same=dict(base,strike=25100.);wide=dict(base,iv_short=.4)
        k=lambda x:economic_key(x,'weekly','none','double',c)
        self.assertEqual(k(base),k(same));self.assertNotEqual(k(base),k(wide));self.assertEqual(len(k(base)[5]),4)
    def test_can_key_keeps_what_changes_the_path(self):
        from calendar_research import economic_key
        c=config();a=dict(day='d',short_expiry='s',long_expiry='l',qty=65,target_tenor=90,strike=25000.,long_strike=25000.);b=dict(a,target_tenor=120)
        self.assertEqual(economic_key(a,'weekly','none','put',c),economic_key(b,'weekly','none','put',c))
        self.assertNotEqual(economic_key(a,'rolling_recenter','none','put',c),economic_key(b,'rolling_recenter','none','put',c))
        self.assertNotEqual(economic_key(a,'weekly','none','put',c),economic_key(dict(a,long_strike=24500.),'weekly','none','put',c))
        self.assertNotEqual(economic_key(a,'weekly','none','put',c),economic_key(a,'carry30','none','put',c))
    def lineage_fixture(self,meta_key,config_key):
        root=Path(tempfile.mkdtemp());t=root/'t.sqlite';sqlite3.connect(t).close();f=root/'f.txt';f.write_text('2026-01-15\n')
        meta={'source_fingerprint':'s','market_content_digest':'m','enrichment_run_id':'e',meta_key:lineage.file_digest(f)}
        cm=dict(source_fingerprint='s',market_content_digest='m',enrichment_run_id='e');cfg={config_key:str(f)}
        return lineage.build(str(t),meta,cfg),cm,str(t),meta,cfg,f
    def test_lin01_03_every_auxiliary_file_is_rechecked(self):
        for mk,ck in (('special_sessions_sha256','special_sessions'),('holiday_calendar_sha256','holiday_calendar'),('contract_master_sha256','contract_master'),('settlement_master_sha256','settlement_master_path')):
            rep,cm,t,meta,cfg,f=self.lineage_fixture(mk,ck);lineage.verify(rep,cm,t,meta,config=cfg)
            f.write_text('2026-01-16\n')
            with self.assertRaises(ValueError):lineage.verify(rep,cm,t,meta,config=cfg)
            f.write_text('2026-01-15\n');lineage.verify(rep,cm,t,meta,config=cfg);f.unlink()
            with self.assertRaises(ValueError):lineage.verify(rep,cm,t,meta,config=cfg)
    def test_lin03_no_file_recorded_needs_no_file(self):
        rep,cm,t,meta,cfg,f=self.lineage_fixture('special_sessions_sha256','special_sessions');meta=dict(meta,special_sessions_sha256='')
        lineage.verify(rep,cm,t,meta,config={})
    def test_cli01_single_shift_is_deterministic(self):
        from rank_strategies import circular_shift_control as f
        days=[f'2026-{1+i//28:02d}-{1+i%28:02d}' for i in range(24)];rows=[dict(id=d,day=d,exit_day=d) for d in days]
        class M:
            def evaluate(self,chosen,*a,**k):return dict(sharpe=float(len(chosen)))
        self.assertEqual(f(rows,[float(i) for i in range(24)],[(0.,20.)],M(),99,None,'base',None,1,1)['shifts'],1)
    def test_cli01_bad_ranking_arguments(self):
        from rank_strategies import validate_args as v
        ok=dict(max_open=3,folds=5,embargo_days=35,min_train=20,min_holdout=5,min_positive_folds=0,top=25,max_capital=0.,shift_control_max=200,bootstrap_reps=2000,bootstrap_block=10.,spa_max_models=20000,capital_multipliers='1,2')
        self.assertEqual(v(SimpleNamespace(**ok)),[])
        for k,x in (('shift_control_max',-1),('bootstrap_block',0.),('top',0),('folds',1),('capital_multipliers','1,0')):self.assertTrue(v(SimpleNamespace(**{**ok,k:x})),k)
    def test_fill02_traded_only_filter_is_labelled_and_applied(self):
        d=self.make();c=config(fill_sync='synchronized',fill_model='next_bar_open_traded_only');e=self.cand(d,config(**self.causal))
        d.db.execute('UPDATE opt SET volume=0 WHERE ts=? AND expiry=?',(e['day']+' 09:47:00',e['short_expiry']));self.clear(d)
        led=simulate(d,c,e,'weekly','none','put')[0][0]['ledger'];first=[x for x in led if x['event']=='initial_short'][0]
        self.assertEqual(first['fill_time'],'09:48');self.assertEqual(first['evidence_class'],'ex_post_traded_bar_filter')
        base=[x for x in simulate(d,config(**self.causal),e,'weekly','none','put')[0][0]['ledger'] if x['event']=='initial_short'][0];self.assertEqual(base['fill_time'],'09:47')
    def test_fill03_entry_delay(self):
        d=self.make();e=self.cand(d,config(**self.causal))
        for n,want in ((0,'09:47'),(1,'09:48'),(2,'09:49'),(3,'09:50')):
            r=simulate(d,config(entry_delay_minutes=n,**self.causal),e,'weekly','none','put')[0][0]
            self.assertEqual({x['fill_time'] for x in r['ledger'] if x['event'].startswith('initial_')},{want})
            self.assertEqual({x['fill_time'] for x in r['ledger'] if x['event']=='end_long_exit'}|{'15:17'},{'15:17'})
    def test_fill03_independent_delay_and_audit_agree(self):
        d=self.make();c=config(fill_sync='independent',fill_model='next_bar_open',entry_delay_minutes=2);e=self.cand(d,c)
        self.assertEqual({x[1] for x in execute_fills(d,c,e['day'],[(e['long_expiry'],e['strike'],'PE'),(e['short_expiry'],e['strike'],'PE')])},{'09:49'})
    def test_delay_setting_validated(self):
        from calendar_research import validate_config as v
        self.assertTrue(v(SimpleNamespace(entry_delay_minutes=4,fill_model='next_bar_open',max_participation=.1)));self.assertTrue(v(SimpleNamespace(entry_delay_minutes=1,fill_model='next_minute',max_participation=.1)))
    def test_compare_book_stats(self):
        from compare_fill_models import book_stats
        self.assertEqual(book_stats(dict(a=1.,b=-3.,c=1.))['max_drawdown'],3.)
    def test_prune_keeps_full_sort_order(self):
        from rank_strategies import prune_finalists
        items=[dict(score=s,train=dict(n=1),tag=i) for i,s in enumerate([1,3,3,2,3,0])];full=sorted(items,key=lambda x:(x['score'],x['train']['n']),reverse=True);some=list(items);prune_finalists(some,4)
        self.assertEqual([x['tag'] for x in some],[x['tag'] for x in full[:4]])
    def test_holdout_inspections_are_counted(self):
        from rank_strategies import record_holdout_inspection as f
        t=Path(tempfile.mkdtemp())/'t.sqlite';self.assertEqual([f(t,'2026-01-01',None,{}),f(t,'2026-01-01',None,{}),f(t,'2025-06-01',None,{})],[0,1,0])
    def test_attribution_buckets_cover_the_pnl(self):
        d=self.make();c=config(brokerage=20.,gst=.18,slippage=.5,exercise_fee=20.,**self.causal);e=self.cand(d,c);r=simulate(d,c,e,'weekly','delta_entry_proxy','put')[0][0];a=r['pnl_attribution']
        self.assertAlmostEqual(sum(a.values()),r['pnl_inr'],places=6);self.assertLess(a['transaction_charges'],0);self.assertLess(a['slippage'],0)
        self.assertAlmostEqual(a['hedge_net'],sum(x.get('hedge_cash',0.) for x in r['ledger']))
    def test_liq01_stage_names_say_what_they_are(self):
        import audit_contract_coverage as A
        self.assertIn('any_independent_liquid_print',A.STAGES);self.assertIn('any_synchronous_liquid_print',A.STAGES);self.assertNotIn('same_minute_both',A.STAGES)

class V450(Base):
    """Exits a session early, iron fly, event gate, parallel months, outcome-free sample boundaries."""
    fills=dict(fill_model='next_bar_open',fill_sync='synchronized',max_participation=0.,volume_unit='contracts')
    def run_policy(self,policy,structure='put',**kw):
        d=self.make();c=config(**self.fills,**kw);e=self.cand(d,c);return d,c,e,simulate(d,c,e,policy,'none',structure)
    def test_sessions_between_counts_only_open_days_and_holidays(self):
        self.assertEqual(sessions_between('2026-01-12','2026-01-13'),0);self.assertEqual(sessions_between('2026-01-09','2026-01-13'),1)
        self.assertEqual(sessions_between('2026-01-09','2026-01-13',[np.datetime64('2026-01-12')]),0)
    def test_tm1_is_flat_the_session_before_expiry(self):
        d,c,e,out=self.run_policy('tm1');r=out[0][0];self.assertIsNone(out[1])
        self.assertLess(r['exit_day'],e['short_expiry']);self.assertEqual(r['exit_reason'],'t_minus_1');self.assertEqual(sessions_between(r['exit_day'],e['short_expiry']),0)
        self.assertEqual({x['fill_time'] for x in r['ledger'] if x['event']=='tm1_exit'},{'15:17'})
    def test_tm1_never_exits_on_the_entry_day(self):
        d,c,e,out=self.run_policy('tm1');self.assertGreater(out[0][0]['exit_day'],e['day'])
    def test_p25_s50_tm1_still_honours_price_exits(self):
        d=self.make(bull=True);c=config(**self.fills);e=self.cand(d,c)
        a=simulate(d,c,e,'p25_s50','none','put')[0][0];b=simulate(d,c,e,'p25_s50_tm1','none','put')[0][0]
        if a['exit_reason']=='checkpoint_p25_s50':self.assertEqual(a['exit_day'],b['exit_day'])
        self.assertLessEqual(b['exit_day'],e['short_expiry'])
    def test_rolling_tm1_replaces_the_short_in_one_minute(self):
        d,c,e,out=self.run_policy('rolling_tm1');led=out[0][0]['ledger']
        buys=[x for x in led if x['event']=='tm1_short_buyback'];self.assertTrue(buys)
        for b in buys:
            sale=[x for x in led if x['event']=='roll_short_sale' and x['day']==b['day']];self.assertEqual(len(sale),1)
            self.assertEqual(sale[0]['fill_time'],b['fill_time']);self.assertGreater(sale[0]['expiry'],b['expiry'])
    def test_iron_fly_has_four_short_dated_legs_and_defined_risk(self):
        d=self.make();c=config(**self.fills,fly_wing_z=.78);e=self.cand(d,c);self.assertEqual(fly_width(e,c),500.)
        out,why=simulate(d,c,e,'weekly','none','iron_fly');self.assertIsNone(why);r,marks=out
        opens=[x for x in r['ledger'] if x['event'].startswith('initial')]
        self.assertEqual(sorted((x['cp'],x['strike'],x['units']>0) for x in opens),sorted([('PE',e['strike'],False),('CE',e['strike'],False),('PE',e['strike']-500,True),('CE',e['strike']+500,True)]))
        self.assertEqual({x['expiry'] for x in opens},{e['short_expiry']})
        self.assertEqual(r['defined_risk_capital_inr'],500*r['lot_units']+r['initial_debit_inr'])
        self.assertAlmostEqual(r['pnl_inr'],sum(x['cash_flow'] for x in r['ledger'] if 'cash_flow' in x))
        self.assertGreaterEqual(r['pnl_inr'],-500*r['lot_units']-1e4)
    def test_iron_fly_rejects_a_hedge_and_rolling(self):
        d=self.make();c=config(**self.fills);e=self.cand(d,c)
        with self.assertRaises(ValueError):simulate(d,c,e,'rolling_static','none','iron_fly')
        with self.assertRaises(ValueError):simulate(d,c,e,'weekly','delta_entry_proxy','iron_fly')
    def test_iron_fly_wing_is_at_least_one_strike(self):
        self.assertEqual(fly_width(dict(iv_short=.01,short_f=25000.,short_expiry='2026-01-13',day='2026-01-12'),config()),100.)
    def test_event_gate_reads_only_the_calendar(self):
        d=self.make();c=config(**self.fills);n=len(list(generate(d,c,Counter())))
        d.events=[('2026-01-13','2026-01-01 00:00','9999-12-31 00:00')];c2=config(event_policy='skip_event_weeks',**self.fills);s=Counter()
        self.assertEqual(list(generate(d,c2,s)),[]);self.assertEqual(s['event_policy_skip'],n)
        c3=config(event_policy='only_event_weeks',**self.fills);self.assertEqual(len(list(generate(d,c3,Counter()))),n)
    def test_scheduled_exit_is_known_at_entry(self):
        e=dict(short_expiry='2026-01-13',long_expiry='2026-04-28');c=config(max_short_dte=8)
        self.assertEqual(scheduled_exit_day(e,'p25_s50',c),'2026-01-13');self.assertEqual(scheduled_exit_day(e,'tm1',c),'2026-01-13')
        self.assertEqual(scheduled_exit_day(e,'rolling_static',c),'2026-04-06')
    def test_path_id_ignores_the_policy_label(self):
        d=self.make();c=config(**self.fills);e=self.cand(d,c)
        a=simulate(d,c,e,'weekly','none','put')[0][0];b=simulate(d,c,e,'p50_s100','none','put')[0][0]
        self.assertEqual(path_digest(a)==path_digest(b),[x['event'] for x in a['ledger']]==[x['event'] for x in b['ledger']] and a['pnl_inr']==b['pnl_inr'])
    def test_fast_window_matches_evaluate(self):
        rng=random.Random(9);days=[f'2026-{m:02d}-{d:02d}' for m in (1,2,3) for d in range(1,29)]
        for _ in range(40):
            rows=[];marks={}
            for i in range(rng.randint(2,15)):
                a=rng.randrange(len(days)-3);b=min(len(days)-1,a+rng.randint(0,20));tid=f't{i}';p={};v=0.
                for j in range(a,b+1):v+=rng.gauss(0,50);p[days[j]]=dict(base=v,lower=v-9,upper=v+7,capital=1e4)
                marks[tid]=p;rows.append(dict(id=tid,day=days[a],exit_day=days[b],pnl_inr=v,entry_capital=1e4,lot_units=65))
            gm=GroupMatrix(rows,marks,days);ch=choose(rows,3);w=basis_weights(rows,'per_unit');s=rng.randrange(40);e=rng.randrange(s+5,len(days)+1)
            m=gm.evaluate(ch,None,'base',w,window=(s,e));eq,on,_=gm.series(ch,'base',w);f=fast_window(eq,on,s,e)
            for k in ('window_mtm','max_dd','time_in_market'):self.assertAlmostEqual(m[k],f[k],places=9,msg=k)
            for k in ('sharpe','active_day_sharpe'):
                if m[k] is None:self.assertIsNone(f[k])
                else:self.assertAlmostEqual(m[k],f[k],places=9,msg=k)
    def test_sample_boundary_uses_the_planned_exit(self):
        self.assertEqual(done_by(dict(exit_day='2026-01-09',scheduled_exit_day='2026-01-13')),'2026-01-13')
        self.assertEqual(done_by(dict(exit_day='2026-01-09')),'2026-01-09')
    def test_risk_normalized_weight_is_inverse_units_times_risk(self):
        self.assertEqual(basis_weights([dict(lot_units=50,entry_risk_pts=200.)],'risk_normalized_score').tolist(),[1e-4])

class V451(Base):
    """Acceptance tests named in the independent V4.5.0 review."""
    fills=V450.fills
    def fly_cfg(self,**kw):return config(include_iron_fly=True,include_calendars=False,fly_wing_z=.78,**self.fills,**kw)
    def strip_far(self,d,e):
        d.db.execute('DELETE FROM opt WHERE expiry=?',(e['long_expiry'],));d.db.commit();self.clear(d);d.expiries_at.cache_clear()
    def test_iron01_fly_needs_no_far_quote(self):
        d=self.make();c=self.fly_cfg();e=self.cand(d,config(**self.fills));self.strip_far(d,e)
        self.assertEqual(list(generate(d,config(**self.fills),Counter())),[])
        rows=list(generate(d,c,Counter()));self.assertTrue(rows);self.assertTrue(all(r['is_fly'] and r['long_expiry'] is None for r in rows))
        out,why=simulate(d,c,rows[0],'weekly','none','iron_fly');self.assertIsNone(why);self.assertEqual(out[0]['exit_reason'],'weekly_exit')
    def test_iron02_structures_keep_separate_candidates(self):
        d=self.make();c=config(include_iron_fly=True,include_calendars=True,**self.fills);r=list(generate(d,c,Counter()))
        self.assertEqual(sum(bool(x.get('is_fly')) for x in r),sum(not x.get('is_fly') for x in r))
    def test_iron03_union_and_paired_are_distinct(self):
        from compare_structures import compare
        v=lambda u:dict(unit=u,capital=u/10,unresolved=False,rows=1,strike_modes=['points'])
        k=lambda d:('p',d,'x',25000.,'none')
        out=compare({k('d1'):v(1.),k('d2'):v(3.)},{k('d2'):v(5.),k('d3'):v(9.)})
        self.assertEqual((out['unmatched_a'],out['unmatched_b'],out['paired_all_opened']['n']),({'no_entry_that_day':1},{'no_entry_that_day':1},1));self.assertEqual(out['paired_all_opened']['a_minus_b_per_unit']['mean'],-2.)
    def t1cand(self,d,**kw):
        c=config(start='2026-01-12',end='2026-01-12',weekdays={0},**self.fills,**kw);return c,next(iter(generate(d,c,Counter())))
    def test_tm101_entry_on_last_session_is_rejected_by_name(self):
        d=self.make();c,e=self.t1cand(d)
        for policy in ('tm1','p25_s50_tm1','rolling_tm1'):self.assertEqual(simulate(d,c,e,policy,'none','put'),(None,'tm1_entry_on_last_session'))
        self.assertIsNotNone(simulate(d,c,e,'weekly','none','put')[0])
    def test_tm102_published_monday_holiday_makes_friday_t1(self):
        d=self.make();c=config(start='2026-01-09',end='2026-01-09',weekdays={4},**self.fills);e=next(iter(generate(d,c,Counter())))
        d.holidays=[np.datetime64('2026-01-12')];r=simulate(d,c,e,'tm1','none','put')
        self.assertEqual(r,(None,'tm1_entry_on_last_session'))
        c=config(**self.fills);e=self.cand(d,c);r=simulate(d,c,e,'tm1','none','put')[0][0]
        self.assertEqual(r['exit_day'],'2026-01-09');self.assertEqual(r['tm1_outcome'],'flat_at_t_minus_1')
    def test_tm103_failed_t1_close_is_reported_and_still_buys_back_on_expiry(self):
        d=self.make();c=config(**self.fills);e=self.cand(d,c);t1='2026-01-12'
        for m in ('15:16','15:17'):d.db.execute("DELETE FROM opt WHERE ts=? AND expiry=? AND strike=?",(f'{t1} {m}:00',e['short_expiry'],e['strike']))
        self.clear(d);r=simulate(d,c,e,'tm1','none','put')[0][0]
        self.assertEqual(r['tm1_outcome'],'partial_closed_later');self.assertEqual(r['exit_day'],e['short_expiry']);self.assertEqual(r['missing_marks']['tm1_outcome_partial_closed_later'],1)
        self.assertFalse([x for x in r['ledger'] if 'settlement' in x['event']])
    def test_tm103b_no_expiry_print_is_carried_to_settlement(self):
        d=self.make();c=config(**self.fills);e=self.cand(d,c);t1='2026-01-12';x=e['short_expiry']
        d.db.execute("DELETE FROM opt WHERE day IN (?,?) AND expiry=? AND strike=? AND ts>=?",(t1,x,x,e['strike'],t1+' 15:00:00'))
        d.db.execute("DELETE FROM opt WHERE day=? AND expiry=? AND strike=?",(x,x,e['strike']))
        self.clear(d);r=simulate(d,c,e,'tm1','none','put')[0][0];self.assertEqual(r['tm1_outcome'],'carried_to_settlement')
    def test_tm104_calendar_audit_flags_both_directions(self):
        daily={'2026-01-09':dict(minute_bars=375),'2026-01-13':dict(minute_bars=375)}
        self.assertEqual(holiday_audit(daily,[]),dict(listed_but_full_session=[],unlisted_weekday_missing=['2026-01-12']))
        self.assertEqual(holiday_audit(daily,['2026-01-12']),dict(listed_but_full_session=[],unlisted_weekday_missing=[]))
        self.assertEqual(holiday_audit(daily,['2026-01-09','2026-01-12'])['listed_but_full_session'],['2026-01-09'])
    def ledger(self):
        return dict(day='a',exit_day='b',status='closed_traded',pnl_inr=10.,ledger=[dict(event='initial_long',day='a',fill_time='09:47',expiry='x',strike=1.,cp='PE',units=65,cash_flow=-101.,charges={}),dict(event='end_long_exit',day='b',fill_time='15:17',expiry='x',strike=1.,cp='PE',units=-65,cash_flow=111.,charges={})])
    def test_path01_labels_do_not_change_identity(self):
        x=self.ledger();y=self.ledger();y['ledger'][1]['event']='tm1_exit';y['status']='proxy_settled'
        self.assertEqual(path_digest(x),path_digest(y));self.assertEqual(contract_digest(x),contract_digest(y))
    def test_path02_cash_flows_change_identity_contracts_do_not(self):
        x=self.ledger();z=self.ledger();z['ledger'][0]['cash_flow']-=100;z['ledger'][1]['cash_flow']+=100
        self.assertNotEqual(path_digest(x),path_digest(z));self.assertEqual(contract_digest(x),contract_digest(z))
    def test_path03_mtm_identity_sees_marks(self):
        m=[dict(day='a',pnl=1.,lower=0.,upper=2.,capital_scenario=9.)];n=[dict(m[0],pnl=1.5)]
        self.assertNotEqual(mtm_digest(m),mtm_digest(n));self.assertEqual(mtm_digest(m),mtm_digest([dict(m[0])]))
    def test_hold01_calendar_is_independent_of_trades(self):
        s=['2025-12-31','2026-01-01','2026-01-02'];self.assertEqual(fixed_holdout_days(s,'2026-01-01'),s[1:])
    def test_prev01_flat_sessions_count(self):
        from compare_fill_models import book_stats
        loss={'d1':-50.,'d2':200.,'d3':-80.,'d4':160.,'d5':50.};cal=[f'd{i}' for i in range(1,12)]
        self.assertEqual(book_stats(loss,cal),book_stats({**loss,**{f'd{i}':0. for i in range(6,12)}}))
    def test_prev02_breakeven_is_quantity_weighted(self):
        from compare_fill_models import breakeven_slippage
        self.assertEqual(breakeven_slippage(1300.,65.+65.+130.),5.)
    def test_prev03_freeze_binds_trades_settings_and_rules(self):
        short=[dict(key=('k',),signal='iv_ratio',rule_key=('iv_ratio',1.,2.),lower=1.,upper=2.)];a=freeze_payload(short,'h',dict(split='x',out='o1'))
        self.assertEqual(a['digest'],freeze_payload(short,'h',dict(split='x',out='o2'))['digest'])
        for changed in (freeze_payload(short,'h2',dict(split='x')),freeze_payload(short,'h',dict(split='y')),freeze_payload([],'h',dict(split='x'))):self.assertNotEqual(a['digest'],changed['digest'])
    def test_evt01_later_announcement_is_invisible(self):
        d=self.make();c=config(event_policy='skip_event_weeks',**self.fills);n=len(list(generate(d,config(**self.fills),Counter())))
        d.events=[('2026-01-13','2026-02-01 00:00','9999-12-31 00:00')];self.assertEqual(len(list(generate(d,c,Counter()))),n)

class V452(unittest.TestCase):
    """Acceptance tests named in the independent V4.5.1 review."""
    def db(self,rows):
        from compare_structures import side
        d=sqlite3.connect(':memory:');self.addCleanup(d.close)
        d.execute('CREATE TABLE trades(provider,day,structure,exit_policy,hedge,target_tenor,pnl_inr,lot_units,entry_capital_scenario,status,strike_mode,mtm_id,details)')
        for i,(st,pol,strike,pnl,status,*rest) in enumerate(rows):
            hedge,mode,day=(rest+['none','points','2026-01-07'][len(rest):])
            d.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('p',day,st,pol,hedge,0 if st=='iron_fly' else 90,pnl,65,1e4,status,mode,f'm{i}',json.dumps(dict(short_expiry='2026-01-13',strike=strike))))
        return side(d,'iron_fly:tm1'),side(d,'put:tm1')
    def test_comp01_unresolved_stays_and_is_labelled(self):
        from compare_structures import compare
        a,b=self.db([('iron_fly','tm1',25000.,70.,'closed_traded'),('iron_fly','tm1',25500.,-650.,'unresolved'),('put','tm1',25000.,40.,'closed_traded')])
        r=compare(a,b);self.assertEqual(r['universe_a']['all_opened']['per_unit']['n'],2);self.assertEqual(r['universe_a']['fully_observed_diagnostic']['per_unit']['n'],1);self.assertEqual(r['universe_a']['unresolved']['n'],1)
    def test_comp02_different_strikes_do_not_pair(self):
        from compare_structures import compare
        r=compare(*self.db([('iron_fly','tm1',25000.,70.,'closed_traded'),('put','tm1',25500.,-50.,'closed_traded')]))
        self.assertEqual(r['paired_all_opened']['n'],0);self.assertEqual(r['unmatched_a'],{'other_strike_or_expiry_that_day':1})
    def test_comp03_same_short_strike_pairs_across_structures(self):
        from compare_structures import compare
        r=compare(*self.db([('iron_fly','tm1',25000.,65.,'closed_traded'),('put','tm1',25000.,130.,'closed_traded')]));self.assertEqual(r['paired_all_opened']['a_minus_b_per_unit']['mean'],-1.)
    def test_comp04_hedged_rows_do_not_enter(self):
        a,b=self.db([('iron_fly','tm1',25000.,65.,'closed_traded'),('put','tm1',25000.,130.,'closed_traded','delta_entry_proxy')]);self.assertEqual(len(a),1);self.assertEqual(len(b),0)
    def test_event01_clock_time_decides(self):
        self.assertEqual(parse_event_line('2026-01-08,2026-01-07T16:00')[1],'2026-01-07 16:00');self.assertGreater(parse_event_line('2026-01-08,2026-01-07T16:00')[1],'2026-01-07 09:45')
        self.assertLessEqual(parse_event_line('2026-01-08,2026-01-07T08:00')[1],'2026-01-07 09:45');self.assertEqual(parse_event_line('2026-01-08,2026-01-07T04:15+00:00')[1],'2026-01-07 09:45')
    def test_event02_date_only_waits_and_vintages_close(self):
        self.assertEqual(parse_event_line('2026-01-08,2026-01-07')[1],'2026-01-08 00:00')
        self.assertEqual(parse_event_line('2026-01-08,2026-01-01T10:00,2026-01-05T10:00,rbi')[2],'2026-01-05 10:00')
        with self.assertRaises(ValueError):parse_event_line('2026-01-08')
    def test_event02_generator_honours_both_ends(self):
        path=Path(tempfile.mkdtemp())/'c.sqlite';fixture(path);d=Data(str(path));self.addCleanup(d.close)
        F=dict(fill_model='next_bar_open',fill_sync='synchronized',max_participation=0.,volume_unit='contracts');c=config(event_policy='skip_event_weeks',start='2026-01-07',end='2026-01-07',**F)
        for known,gone,blocked in (('2026-01-07 08:00','9999-12-31 00:00',True),('2026-01-07 16:00','9999-12-31 00:00',False),('2026-01-01 08:00','2026-01-05 08:00',False)):
            d.events=[('2026-01-08',known,gone)];self.assertEqual(list(generate(d,c,Counter()))==[],blocked,known)
    def test_fill01_window_ignores_other_runs(self):
        from compare_fill_models import compare
        t=Path(tempfile.mkdtemp());days=[f'2026-01-{i:02d}' for i in range(1,21)]
        def make(name,pairs):
            db=sqlite3.connect(t/name);db.executescript('CREATE TABLE trades(id,economic_id,pnl_inr,lot_units,status);CREATE TABLE fills(trade_id,seq,details);CREATE TABLE marks(trade_id,day,pnl_inr);')
            db.execute("INSERT INTO trades VALUES('x','e',1.,1,'closed_traded')");db.executemany('INSERT INTO marks VALUES(?,?,?)',[('x',a,b) for a,b in pairs]);db.commit();db.close();return t/name
        a=make('a',[(days[4],10.),(days[5],4.),(days[6],9.)]);b=make('b',[(days[0],1.),(days[19],2.)])
        solo=compare([('a',a)],days,days[2],days[12])['results'][0];both=compare([('a',a),('b',b)],days,days[2],days[12])['results'][0]
        self.assertEqual(solo['calendar_sharpe'],both['calendar_sharpe']);self.assertEqual(solo['sessions_scored'],11)
    def freeze(self,**identity):
        return freeze_payload([dict(key=('k',),signal='s',rule_key=('s',1.,2.),lower=1.,upper=2.)],'h',dict(split='x'),dict(dict(code_sha256='c',sessions_sha256='s',market_content_digest='m'),**identity))
    def test_freeze01_code_change_refuses(self):
        self.assertEqual(freeze_mismatch(self.freeze(),self.freeze()),[]);self.assertEqual(freeze_mismatch(self.freeze(),self.freeze(code_sha256='c2')),['identity.code_sha256'])
    def test_freeze02_cache_or_calendar_change_refuses(self):
        self.assertEqual(freeze_mismatch(self.freeze(),self.freeze(market_content_digest='m2')),['identity.market_content_digest'])
        self.assertEqual(freeze_mismatch(self.freeze(),self.freeze(sessions_sha256='s2')),['identity.sessions_sha256'])

class V453(unittest.TestCase):
    """Acceptance tests named in the V4.5.2 reviews."""
    def test_boundary01_net_stops_at_the_matrix(self):
        row=dict(id='t',day='2026-01-02',exit_day='2026-03-31',pnl_inr=1000.,entry_capital=1000.);days=['2026-01-02','2026-01-09']
        marks={'t':{d:dict(base=-7.,lower=-7.,upper=-7.,capital=1000.) for d in days}};x=GroupMatrix([row],marks,days).evaluate([row])
        self.assertEqual(x['net'],-7.);self.assertEqual(x['win_pct'],0.)
    def test_boundary05_train_only_report_ignores_every_later_outcome(self):
        import subprocess,shutil
        root=Path(tempfile.mkdtemp());cache=root/'c.sqlite';fixture(cache,long=dt.date(2026,6,30),end=dt.date(2026,6,30));here=Path(__file__).resolve().parents[1]
        run=lambda *a:subprocess.run([sys.executable,str(here/a[0]),*map(str,a[1:])],check=True,capture_output=True,cwd=root)
        run('enrich_cache.py','--cache',cache,'--batch-rows',8000)
        run('calendar_research.py','--cache',cache,'--out',root/'a.sqlite','--from-date','2026-01-07','--to-date','2026-04-15','--long-tenors','120','--tenor-tolerance','60','--round-steps','500','--strike-values','0','--policies','weekly,rolling_static','--structures','put','--max-candidates','75','--slippage-mode','flat')
        shutil.copy(root/'a.sqlite',root/'b.sqlite');cut='2026-03-16'
        db=sqlite3.connect(root/'b.sqlite')
        self.assertGreater(db.execute('SELECT COUNT(*) FROM trades WHERE day<? AND exit_day>=?',(cut,cut)).fetchone()[0],0)
        db.execute('UPDATE trades SET pnl_inr=pnl_inr+99999,status=?,mtm_id=mtm_id||?,path_id=path_id||?,contract_id=contract_id||? WHERE exit_day>=?',('proxy_settled','x','x','x',cut))
        db.execute('DELETE FROM trades WHERE id IN (SELECT id FROM trades WHERE day>=? LIMIT 5)',(cut,));db.execute('UPDATE marks SET pnl_inr=pnl_inr-5555 WHERE day>=?',(cut,));db.commit();db.close()
        args=lambda t,o:('rank_strategies.py','--cache',cache,'--trades',root/t,'--split',cut,'--embargo-days',0,'--folds',2,'--min-train',5,'--min-holdout',1,'--signals','iv_ratio','--lower-ratios','.1','--upper-ratios',3,'--bootstrap-reps',200,'--shift-control-max',5,'--out',root/o)
        run(*args('a.sqlite','ra'));run(*args('b.sqlite','rb'))
        A,B=[json.loads((root/x/'rankings.json').read_text()) for x in ('ra','rb')]
        for x in (A,B):x.pop('lineage')
        self.assertEqual(json.dumps(A,sort_keys=True),json.dumps(B,sort_keys=True));self.assertGreater(A['train_boundary']['open_at_cutoff_trades'],0)
        self.assertEqual(json.loads((root/'ra'/'selection_freeze.json').read_text())['rules'],json.loads((root/'rb'/'selection_freeze.json').read_text())['rules'])
        self.assertEqual(sorted(x.name for x in root.iterdir() if '.tmp' in x.name or '.old' in x.name),[])
    def test_holdout01_copy_keeps_the_count(self):
        root=Path(tempfile.mkdtemp());(root/'A').mkdir();(root/'B').mkdir();log=root/'log.jsonl'
        for n in 'AB':
            d=sqlite3.connect(root/n/'t.sqlite');d.execute('CREATE TABLE meta(name,value)');d.commit();d.close()
        with patch.dict(os.environ,dict(NIFTY_HOLDOUT_LOG=str(log))):
            self.assertEqual(record_holdout_inspection(root/'A'/'t.sqlite','2026-01-01','x',{}),0)
            self.assertEqual(prior_inspections(root/'B'/'t.sqlite','2026-01-01'),1);self.assertEqual(prior_inspections(root/'B'/'t.sqlite','2026-02-01'),0)
            log.write_text('{broken');
            with self.assertRaises(SystemExit):prior_inspections(root/'B'/'t.sqlite','2026-01-01')
    def test_signal01_both_curvatures_are_named_and_differ(self):
        v=dict(iv5=.4,iv30=.3,iv90=.25);self.assertAlmostEqual(ratio(v,'annual_variance_curvature_5_30_90'),-.459150,places=5);self.assertAlmostEqual(ratio(v,'total_variance_curvature_5_30_90'),.178105,places=5)
        self.assertNotIn('term_curvature_5_30_90',SIGNALS)
    def test_portfolio01_presorted_matches_sorted(self):
        r=[dict(id=x,day=d,exit_day=d,entry_capital=1,pnl_inr=0) for d in ('a','b','c') for x in 'pq'];self.assertEqual(choose(r,1,None,presorted=True),choose(list(reversed(r)),1))
    def test_freeze_pins_resolved_bounds(self):
        s=lambda u:[dict(key=('k',),signal='s',rule_key=('s',.25,.75),lower=1.,upper=u)];self.assertNotEqual(freeze_payload(s(2.),'h',{})['digest'],freeze_payload(s(3.),'h',{})['digest'])
    def test_fly_curve_needs_no_quote_at_its_own_strike(self):
        path=Path(tempfile.mkdtemp())/'c.sqlite';fixture(path);d=Data(str(path));self.addCleanup(d.close);c=config(include_iron_fly=True,include_calendars=False,fill_model='next_bar_open',max_participation=0.,volume_unit='contracts')
        e=next(iter(generate(d,c,Counter())));base=e['iv_60'] if e['iv_60'] is not None else e['iv_90']
        self.assertIsNotNone(base);k=e['strike']
        d.db.execute("DELETE FROM opt WHERE strike=? AND expiry>? AND ts LIKE ?",(k,e['short_expiry'],'% 09:45:00'));d.db.commit()
        self.assertEqual(sorted(x['strike'] for x in generate(d,c,Counter()))[0],k)
    def test_fly_var_carry_uses_only_iv30(self):
        path=Path(tempfile.mkdtemp())/'c.sqlite';fixture(path);d=Data(str(path));self.addCleanup(d.close);c=config(include_iron_fly=True,include_calendars=False,fly_wing_z=.78,fill_model='next_bar_open',fill_sync='synchronized',max_participation=0.,volume_unit='contracts')
        e=next(iter(generate(d,c,Counter())));r=simulate(d,c,dict(e,iv_30=None),'weekly','none','iron_fly')[0][0];self.assertIsNone(r.get('var_carry_daily'))
        r=simulate(d,c,dict(e,iv_30=.1),'weekly','none','iron_fly')[0][0];self.assertGreater(r['var_carry_daily'],0)

class V454(Base):
    def thin(self,d,c,e,keep_close=True):
        """Remove the long's exit-window bars from the short expiry on, optionally leaving the 15:29 prints."""
        clause=" AND substr(ts,12,5)<>'15:29'" if keep_close else ''
        d.db.execute("DELETE FROM opt WHERE provider=? AND expiry=? AND strike=? AND cp='PE' AND day>=?"+clause,(c.provider,e['long_expiry'],e['strike'],e['short_expiry']));self.clear(d)
    def run_it(self,**kw):
        d=self.make();c=config(fill_sync='synchronized',**kw);e=self.cand(d,c);self.thin(d,c,e)
        o,err=simulate(d,c,e,'weekly','none','put');self.assertIsNone(err);return o[0],e,d
    def test_off_by_default_keeps_waiting(self):
        r,e,_=self.run_it();self.assertFalse(r['forced_exit'])
        self.assertFalse([x for x in r['ledger'] if x['event']=='forced_exit_lag_cap'])
    def test_cap_zero_books_on_first_attempt_day(self):
        r,e,_=self.run_it(max_exit_lag=0);self.assertEqual(r['exit_reason'],'forced_exit_lag_cap')
        self.assertEqual(r['exit_day'],e['short_expiry']);self.assertEqual(r['status'],'closed_traded');self.assertEqual(r['exit_lag_sessions'],0)
    def test_cap_one_waits_one_session(self):
        r,e,_=self.run_it(max_exit_lag=1);x=[y for y in r['ledger'] if y['event']=='forced_exit_lag_cap'][0]
        self.assertGreater(x['day'],e['short_expiry']);self.assertEqual(r['exit_lag_sessions'],1);self.assertEqual(x['mode'],'model_mark');self.assertFalse(x['executable_claim'])
    def test_printed_close_takes_the_haircut(self):
        r,e,d=self.run_it(max_exit_lag=0,forced_exit_haircut=.05);x=[y for y in r['ledger'] if y['event']=='forced_exit_lag_cap'][0]
        close=d.opt(config().provider,x['day'],'15:29',e['long_expiry'],e['strike'],'PE')['close'];self.assertAlmostEqual(x['reference_price'],close*.95)
    def test_no_print_uses_lower_bound(self):
        d=self.make();c=config(fill_sync='synchronized',max_exit_lag=0,forced_exit_haircut=.05);e=self.cand(d,c);self.thin(d,c,e,keep_close=False)
        r=simulate(d,c,e,'weekly','none','put')[0][0];x=[y for y in r['ledger'] if y['event']=='forced_exit_lag_cap'][0]
        self.assertNotEqual(x['price_field'],'mark_traded_bar')
    def test_forced_exit_is_not_counted_as_a_fill(self):
        r,_,_=self.run_it(max_exit_lag=0);self.assertEqual(r['executed_fills'],sum(x.get('mode')=='liquid_bar_proxy' for x in r['ledger']));self.assertTrue(any(x.get('mode')=='model_mark' for x in r['ledger']))
    def test_clean_exit_has_no_drift_and_no_force(self):
        d=self.make();c=config(fill_sync='synchronized',max_exit_lag=0);e=self.cand(d,c);r=simulate(d,c,e,'weekly','none','put')[0][0]
        self.assertFalse(r['forced_exit']);self.assertAlmostEqual(r['post_schedule_drift_inr'],0.,places=6)
    def test_drift_is_pnl_less_planned_mark(self):
        r,_,_=self.run_it(max_exit_lag=2);self.assertAlmostEqual(r['pnl_inr']-r['scheduled_exit_mark_inr'],r['post_schedule_drift_inr'])
    def report_db(self,rows):
        path=Path(self.temp.name if hasattr(self,'temp') else tempfile.mkdtemp())/'t.sqlite';db=sqlite3.connect(path)
        db.executescript("CREATE TABLE trades(id,day,target_tenor,status,scheduled_exit_day,pnl_inr,lot_units,details);CREATE TABLE marks(trade_id,day,pnl_inr)")
        for i,(sched,exp,pnl,why,mark) in enumerate(rows):
            db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?)',(str(i),sched,60,'closed_traded',sched,pnl,65,json.dumps(dict(short_expiry=exp,exit_reason=why))));db.execute('INSERT INTO marks VALUES(?,?,?)',(str(i),sched,mark))
        db.commit();db.close();return path
    def test_report_stops_at_the_cutoff(self):
        from exit_lag_report import load,report
        base=[('2025-08-05','2025-08-07',650.,'weekly_exit',650.),('2025-08-12','2025-08-14',-650.,'weekly_exit',-650.)]
        a=report(load(self.report_db(base),'2025-11-27'))
        self.assertEqual(a,report(load(self.report_db(base+[('2026-02-03','2026-02-03',-9e9,'weekly_exit',-9e9)]),'2025-11-27')))
    def test_report_eras_use_short_expiry(self):
        from exit_lag_report import load,report
        b=report(load(self.report_db([('2025-08-28','2025-09-02',65.,'weekly_exit',65.)]),'2025-11-27'))['60']
        self.assertEqual(b['tuesday_era']['trades'],1);self.assertEqual(b['thursday_era']['trades'],0)
    def test_report_separates_drift(self):
        from exit_lag_report import load,report
        b=report(load(self.report_db([('2025-08-05','2025-08-07',130.,'late_liquid_exit',65.)]),'2025-11-27'))['60']['all']
        self.assertAlmostEqual(b['post_schedule_drift'],1.);self.assertEqual(b['lagged'],1)

class Plumbing(unittest.TestCase):
    def test_decimal_binds_to_sqlite(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE t(a REAL,b INTEGER)')
        db.execute('INSERT INTO t VALUES(?,?)',(num(decimal.Decimal('1.5')),whole(decimal.Decimal(7))))
        self.assertEqual(db.execute('SELECT * FROM t').fetchone(),(1.5,7));self.assertIsNone(num(None))
    def test_publish_on_conflict_names_a_target(self):
        src=(Path(__file__).resolve().parents[1]/'publish_results.py').read_text()
        self.assertNotIn('ON CONFLICT DO UPDATE',src)
    def test_daily_sql_keeps_single_source_file(self):
        self.assertIn("f.relative_path='india-index-options-1m/index/NIFTY.parquet'",DAILY_SQL)

if __name__=='__main__':unittest.main()
