"""V4.9.14 multi-leg structures (user 2026-11-24): option verticals, futures + options, long options, ratio backspread; futures carry.
Synthetic only; none of this is evidence of profitability."""
import sys,unittest,tempfile,sqlite3,math,json,subprocess
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
import numpy as np
from nifty_iv.zone import signals as zs, backtest as zb, paths as zp, rules as zr, contracts as zc
from nifty_iv.core import strategy_spec as ss
from tests.test_zone import Market,sessions
from tests.test_paths import pcfg

ROOT=Path(__file__).resolve().parents[1]


class SpecV4914(unittest.TestCase):
    def test_legs_margins_and_expiry_policies(self):
        L=lambda st,side,lv=1:ss.combo_legs(st,side,23456.,lv)            # ATM 23500, step 200
        self.assertEqual(L('vert_w1','bull'),[('PE',23300.,-1),('PE',23100.,1)]);self.assertEqual(L('vert_w2','bear'),[('CE',23700.,-1),('CE',24100.,1)])
        self.assertEqual(L('married','bull'),[('FUT',0.,1),('PE',23300.,1)]);self.assertEqual(L('married','bear'),[('FUT',0.,-1),('CE',23700.,1)])
        self.assertEqual(L('covered','bull'),[('FUT',0.,1),('CE',23700.,-1)]);self.assertEqual(L('covered','bear'),[('FUT',0.,-1),('PE',23300.,-1)])
        self.assertEqual(L('collar_w1','bull'),[('FUT',0.,1),('PE',23300.,1),('CE',23700.,-1)]);self.assertEqual(L('collar_w2','bear'),[('FUT',0.,-1),('CE',23700.,1),('PE',23100.,-1)])
        self.assertEqual(L('longopt','bull'),[('CE',23700.,1)]);self.assertEqual(L('longopt','bear'),[('PE',23300.,1)])
        self.assertEqual(L('backspread_w1','bull',0),[('CE',23500.,-1),('CE',23700.,2)]);self.assertEqual(ss.COMBOS['backspread_w1']['sides'],('bull',))
        self.assertEqual([(ss.margin_share(s),ss.lots_factor(s)) for s in ('vert_w1','married','covered','collar_w1','longopt','backspread_w2','naked')],
                         [(.6,1.6),(.6,1.6),(1.,1.),(1.1,.9),(1.,1.),(1.1,.9),(1.,1.)])
        ex=['2024-01-04','2024-01-11','2024-01-25','2024-02-29']
        self.assertEqual(ss.select_short_expiry('vert_w1','2024-01-02',ex),('2024-01-11',None))           # weekly >= 4 days, as naked
        self.assertEqual(ss.select_short_expiry('married','2024-01-02',ex),('2024-01-25',None))          # current monthly >= 21 days
        self.assertEqual(ss.select_short_expiry('married','2024-01-08',ex),(None,'policy_ineligible'))
        self.assertEqual(ss.structure_class('vert_w1'),'credit');self.assertEqual(ss.structure_class('longopt'),'debit');self.assertEqual(ss.structure_class('collar_w1'),'combo')
        self.assertTrue(ss.expires('married') and ss.expires('naked') and not ss.expires('futures_zone'))
    def test_rule_families(self):
        self.assertIn('tp50',zr.profit_rules('vert_w1'));self.assertIn('mult2',zr.stop_rules('vert_w1'));self.assertIn('loss100',zr.stop_rules('vert_w1'))
        self.assertIn('tp50',zr.profit_rules('longopt'));self.assertIn('dloss50',zr.stop_rules('longopt'));self.assertNotIn('loss100',zr.stop_rules('longopt'))
        for st in ('married','covered','collar_w1','backspread_w1'):
            self.assertFalse(any(x.startswith(('tp','loss','dloss','mult')) and not x.startswith('tpct') for x in zr.profit_rules(st)+zr.stop_rules(st)),st)
            self.assertIn('exp1',zr.stop_rules(st))


class EngineV4914(Market):
    def setUp(self):
        self.days=sessions('2024-01-02',19)
    def run_(self,structures,final=None,side='bull',lots=3,carry=0.,offsets=(0,),level=None,t=0):
        m=self.build(days=self.days,level=level,weekly='2024-01-11',extra=('2024-01-18','2024-01-25'));bars=self.bars_for(self.days,level or (lambda d,e:20000.))
        final=final or ['G' if side=='bull' else 'R']*len(bars);out=[]
        sim=zp.PathSim(m,bars,final,pcfg(lots=lots,carry=carry),zs.load_minutes(self.sp))
        sim.run({t:[side]},tuple(structures),offsets,sink=out.extend,progress=False);return out,sim,m
    def test_future_arrays_are_spot_with_carry(self):
        out,sim,m=self.run_(('married',),carry=.1)
        d,k=sim.dk(self.days[0],('2024-01-25',0.,'FUT'))
        self.assertAlmostEqual(float(d.buy[k][0]),20000.*(1+.1*23/365),9);self.assertEqual(float(d.vol[k][0]),1e12)
        self.assertTrue(sim.extracted(self.days[0],('2024-01-25',0.,'FUT')))
    def test_married_put_settles_both_legs_at_expiry(self):
        for carry in (0.,.1):
            out,sim,m=self.run_(('married',),carry=carry);p=out[0]
            self.assertEqual((p['status'],p['reason'],p['short_expiry']),('closed','expiry','2024-01-25'))
            s=m.settlement('2024-01-25')[0];u=p['units'];self.assertEqual(u%50,0)
            ct=p['rows']['cash_total'][-1]                                             # entry cash (no rolls)
            self.assertAlmostEqual(p['end_pnl']-ct,u*s+u*max(0.,20000.-s),6)           # future at the spot reference + the put's value
        o0,_,_=self.run_(('married',),carry=0.);o1,_,_=self.run_(('married',),carry=.1)
        u=o0[0]['units'];F=sim.fut_price(20000.,o0[0]['entry_day'],'2024-01-25')
        self.assertAlmostEqual((o1[0]['end_pnl']-o0[0]['end_pnl'])/(u*20000.*.1*22/365),-1,1)   # carry costs about S x carry x days / 365
    def test_vertical_is_a_credit_with_net_premium_base_and_margin_factor(self):
        out,sim,m=self.run_(('vert_w1',),lots=3);p=out[0];r=p['rows']
        self.assertEqual(p['units'],50*4)                                             # floor(3 x 1.6) lots
        self.assertGreater(r['base'][0],0);self.assertAlmostEqual(r['base'][0],r['cash_total'][0],9)
        self.assertEqual(p['reason'],'expiry');self.assertEqual(p['short_expiry'],'2024-01-11')
        self.assertGreater(p['entry_short_px'],0)                                      # short put premium - long put premium
    def test_backspread_ratio_and_covered_bear(self):
        out,sim,m=self.run_(('backspread_w1',),lots=3);p=out[0];self.assertEqual(p['units'],50*2)   # floor(3 x 0.9)
        o2,s2,_=self.run_(('backspread_w1',),lots=3,final=['G']*6+['R']*200)        # zone flip: the exit closes 1 short + 2 longs
        q=o2[0];self.assertEqual(q['reason'],'zone_flip');self.assertTrue(math.isfinite(q['end_pnl']))
        self.assertEqual(self.run_(('backspread_w1',),side='bear')[0],[])           # bull only
        out,sim,m=self.run_(('covered',),side='bear');p=out[0];self.assertEqual(p['structure'],'covered');self.assertEqual(p['reason'],'expiry')
        s=m.settlement('2024-01-25')[0];u=p['units']
        self.assertAlmostEqual(p['end_pnl']-p['rows']['cash_total'][-1],-u*s-u*max(0.,20000.-s),6)   # short future + short put at settlement
    def test_long_option_is_a_debit_and_collar_exits_on_zone_flip(self):
        out,sim,m=self.run_(('longopt',));p=out[0];r=p['rows']
        self.assertLess(r['cash_total'][0],0);self.assertAlmostEqual(r['base'][0],-r['cash_total'][0],9)
        out,sim,m=self.run_(('collar_w2',),final=['G']*10+['R']*300);p=out[0]
        self.assertEqual(p['reason'],'zone_flip');self.assertGreater(p['rows']['base'][0],0)          # margin base
        self.assertAlmostEqual(p['rows']['base'][0],p['units']*ss.margin_per_unit(p['entry_day'])*1.1,6)
    def test_expiry_payoffs_through_the_strikes(self):
        up=lambda d,m:20000.+(300. if d>=self.days[5] else 0.);dn=lambda d,m:20000.-(300. if d>=self.days[5] else 0.)
        out,sim,m=self.run_(('backspread_w1',),level=up);p=out[0];s=m.settlement('2024-01-25')[0];u=p['units']
        self.assertAlmostEqual(p['end_pnl']-p['rows']['cash_total'][-1],-u*max(0.,s-20000.)+2*u*max(0.,s-20100.),6)   # 1 short, 2 long calls
        self.assertGreater(s,20100.)
        out,sim,m=self.run_(('vert_w1',),level=dn);p=out[0];s=m.settlement('2024-01-11')[0];u=p['units']
        self.assertAlmostEqual(p['end_pnl']-p['rows']['cash_total'][-1],-u*max(0.,20000.-s)+u*max(0.,19900.-s),6)      # loss capped at the width
        self.assertLess(s,19900.)
    def test_batch_rules_run_and_mloss_uses_the_margin_share(self):
        out,sim,m=self.run_(('vert_w1',),offsets=(0,100));db=sqlite3.connect(':memory:');db.executescript(zp.DDL)
        bars=self.bars_for(self.days)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,'G') for i,b in enumerate(bars)])
        zp.writer(db)(out)
        from nifty_iv.zone import grid as zg
        ps=[p for p in zg.load_paths(db,'bull','vert_w1',0.).values() if p];b=zr.Batch(ps,zr.BarArrays.from_db(db),'vert_w1')
        self.assertAlmostEqual(float(b.mpu[0]),ss.margin_per_unit(ps[0]['entry_day'])*.6,9)
        F=zr.evaluate(zr.stop_rules('vert_w1')+zr.profit_rules('vert_w1'),b);self.assertEqual(F.shape[1],len(ps))


class FuturesCarryV4914(Market):
    def test_futures_expiry_entry_and_exit_carry(self):
        days=sessions('2024-01-02',19);m=self.build(days=days,extra=('2024-01-18','2024-01-25'));bars=self.bars_for(days)
        def go(c):
            out=[];zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=1,carry=c),zs.load_minutes(self.sp)).run({0:['bull']},('futures_expiry','futures_zone'),(0,),sink=out.extend,progress=False)
            return {p['structure']:p for p in out}
        a,b=go(0.),go(.1)
        e=a['futures_expiry'];u=e['units'];dte=zb.days_between(e['entry_day'],e['short_expiry'])
        self.assertAlmostEqual((b['futures_expiry']['rows']['cash_total'][0]-e['rows']['cash_total'][0])/(-u*20000.*.1*dte/365),1,3)   # pays the carry at entry
        self.assertAlmostEqual(b['futures_expiry']['end_pnl']-a['futures_expiry']['end_pnl'],-u*20000.*.1*dte/365,-1)                   # converges at expiry
        z0,z1=a['futures_zone'],b['futures_zone'];held=zb.days_between(z0['entry_day'],days[-1])
        self.assertLess(z1['rows']['mark'][-1],z0['rows']['mark'][-1])                 # carry drag while held


class PlanV4914(Market):
    def test_planner_lists_option_legs_until_the_zone_changes_or_expiry(self):
        days=sessions('2024-01-02',19);self.build(days=days,extra=('2024-01-18','2024-01-25'));bars=self.bars_for(days)
        final=['G']*30+['R']*(len(bars)-30);listing={d:{'2024-01-04','2024-01-11','2024-01-18','2024-01-25'} for d in days}
        a=SimpleNamespace(offset_mode='points',offsets='0',from_date='2024-01-01',to_date='2024-12-31',buffer_sessions=1,min_short_days=4)
        out,counts=zc.plan_combo_targets(bars,final,{'c':{'bull':[0],'bear':[]}},listing,a,['married','covered','vert_w1','backspread_w1'])
        self.assertIn((days[0],'2024-01-25',20000.,'PE'),out);self.assertIn((days[0],'2024-01-25',20000.,'CE'),out)      # married put, covered call
        self.assertIn((days[0],'2024-01-11',19900.,'PE'),out);self.assertIn((days[0],'2024-01-25',20100.,'CE'),out)      # vertical long leg, backspread longs
        self.assertFalse(any(cp=='FUT' for *_,cp in out))
        flip=bars[30].day;last=days[days.index(flip)+1+1]
        self.assertTrue(all(d<=last for d,*_ in out));self.assertIn((last,'2024-01-25',20000.,'PE'),out)


class MergeV4914(Market):
    def _paths(self,structures,carry=0.):
        days=sessions('2024-01-02',12);m=self.build(days=days,extra=('2024-01-18','2024-01-25'));bars=self.bars_for(days)
        p=Path(tempfile.mkdtemp())/'p.sqlite';db=sqlite3.connect(p);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,'G') for i,b in enumerate(bars)])
        db.executemany('INSERT INTO signals VALUES(?,?,?)',[('c','bull',0)]);db.execute("INSERT INTO meta VALUES('args',?)",(json.dumps(dict(lots=1,structures=','.join(structures))),))
        zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=1,carry=carry),zs.load_minutes(self.sp)).run({0:['bull']},structures,(0,),sink=zp.writer(db),progress=False);db.commit();db.close();return p
    def test_merge_replaces_structures_and_checks_inputs(self):
        a=self._paths(('naked','futures_expiry'));b=self._paths(('futures_expiry','married'),carry=.05);out=Path(tempfile.mkdtemp())/'m.sqlite'
        r=subprocess.run([sys.executable,str(ROOT/'scripts/merge_paths.py'),'--base',str(a),'--add',str(b),'--out',str(out)],capture_output=True,text=True,env={'PYTHONPATH':str(ROOT/'src')})
        self.assertEqual(r.returncode,0,r.stderr);db=sqlite3.connect(out)
        self.assertEqual(sorted(db.execute('SELECT structure,COUNT(*) FROM paths GROUP BY 1')),[('futures_expiry',1),('married',1),('naked',1)])
        fe=db.execute("SELECT end_pnl FROM paths WHERE structure='futures_expiry'").fetchone()[0]
        self.assertEqual(fe,sqlite3.connect(b).execute("SELECT end_pnl FROM paths WHERE structure='futures_expiry'").fetchone()[0])   # the added (carry) version
        c=sqlite3.connect(b);c.execute('UPDATE bars SET close=close+1 WHERE idx=0');c.commit();c.close()
        r=subprocess.run([sys.executable,str(ROOT/'scripts/merge_paths.py'),'--base',str(a),'--add',str(b),'--out',str(out)+'2'],capture_output=True,text=True,env={'PYTHONPATH':str(ROOT/'src')})
        self.assertNotEqual(r.returncode,0);self.assertIn('bars tables differ',r.stderr+r.stdout)


if __name__=='__main__':unittest.main()
