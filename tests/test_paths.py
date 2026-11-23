"""Path engine and exit grid tests: where the old engine and the new one model the same thing, they must agree exactly.
Synthetic only; none of this is evidence of profitability."""
import sys,unittest,tempfile,sqlite3,math,datetime as dt,random,collections,json
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
import numpy as np
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.zone import paths as zp
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zru
from nifty_iv.zone.legacy import report as zr
from nifty_iv.core.contract_specs import LotMaster
from tests.test_zone import Market,sessions,walk

def pcfg(**kw):
    a=SimpleNamespace(slippage=0.,brokerage=0.,lots=1,max_participation=.1,min_short_days=4,long_target=60,contract_master=None,max_exit_lag_bars=9,forced_exit_haircut=.02,slippage_points=0.)
    a.__dict__.update(kw);return zp.config(a)

def grid_path(p):
    L=len(p['rows']['cash_total']);return dict(p,rows=zp.unpack(zp.pack(p['rows']),L))

class Paths(Market):
    def paths(self,m,bars,final,entries={0:['bull']},structures=('naked',),cfg=None,offsets=(0,)):
        sim=zp.PathSim(m,bars,final,cfg or pcfg());out=[];sim.run(entries,structures,offsets,sink=out.extend,progress=False);return out,sim
    def test_naked_to_expiry_matches_old_engine(self):
        m=self.build();bars=self.bars_for(self.days);old=self.go(m,bars,['G']*len(bars))[0][0];p=self.paths(m,bars,['G']*len(bars))[0][0]
        self.assertEqual(p['reason'],'expiry');self.assertAlmostEqual(p['end_pnl'],old['pnl_inr'],6)
    def test_zone_flip_matches_old_engine(self):
        m=self.build();bars=self.bars_for(self.days);final=['G']*5+['R']*(len(bars)-5)
        old=self.go(m,bars,final)[0][0];p=self.paths(m,bars,final)[0][0]
        self.assertEqual((p['reason'],p['end_bar']),('zone_flip',5));self.assertAlmostEqual(p['end_pnl'],old['pnl_inr'],6)
        self.assertEqual(p['rows']['exit_bar'][-1],old['exit_bar'])
    def test_calendar_roll_chain_matches_old_engine(self):
        days=sessions('2024-01-02',12);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',));bars=self.bars_for(days);final=['G']*75+['R']*(len(bars)-75)
        tr,_=self.go(m,bars,final,structures=('calendar',));p=self.paths(m,bars,final,structures=('calendar',),cfg=pcfg(roll_entry_after_exit=False))[0][0]
        self.assertEqual((p['pairs'],p['reason']),(2,'zone_flip'));self.assertEqual(sum(p['rows']['roll']),1)
        self.assertAlmostEqual(p['end_pnl'],sum(t['pnl_inr'] for t in tr),6)   # same minute-sharing rule as the old engine
        q=self.paths(m,bars,final,structures=('calendar',))[0][0];self.assertNotEqual(p['end_pnl'],q['end_pnl'])   # default: new pair after the last exit minute
    def test_spilled_roll_matches_old_engine(self):
        days=sessions('2024-01-02',12);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',))
        m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day='2024-01-11' AND minute>='15:15'")
        bars=self.bars_for(days);final=['G']*85+['R']*(len(bars)-85)
        tr,_=self.go(m,bars,final,structures=('calendar',));sim=zp.PathSim(m,bars,final,pcfg(roll_entry_after_exit=False));out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False);p=out[0]
        self.assertEqual(sim.counts['roll_exit_spilled'],1);self.assertEqual(p['pairs'],2);self.assertEqual(len(tr),2)
        self.assertEqual(tr[1]['entry_day'],'2024-01-12');self.assertAlmostEqual(p['end_pnl'],sum(t['pnl_inr'] for t in tr),6)
    def test_data_end_is_open_not_unresolved(self):
        days=sessions('2024-01-02',4);m=self.build(days=days);bars=self.bars_for(days);p=self.paths(m,bars,['G']*len(bars))[0][0]
        self.assertEqual((p['status'],p['reason']),('open_at_data_end','open_at_data_end'));self.assertTrue(math.isfinite(p['end_pnl']))
    def test_long_sold_after_short_matches_old_engine(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '12:15' AND '12:24'",(self.days[0],))
        bars=self.bars_for(self.days);final=['G']*3+['R']*(len(bars)-3)
        old=self.go(m,bars,final,structures=('calendar',))[0][0];p=self.paths(m,bars,final,structures=('calendar',))[0][0]
        self.assertAlmostEqual(p['end_pnl'],old['pnl_inr'],6)
    def test_lag_cap_matches_old_engine(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND (day>? OR minute>'11:00')",(self.days[0],))
        bars=self.bars_for(self.days);final=['G']*3+['R']*(len(bars)-3)
        old=self.go(m,bars,final,structures=('calendar',),cfg=self.cfg(max_exit_lag_bars=2))[0][0]
        p=self.paths(m,bars,final,structures=('calendar',),cfg=pcfg(max_exit_lag_bars=2))[0][0]
        self.assertTrue(old['forced']);self.assertEqual(p['rows']['exit_forced'][-1],1);self.assertAlmostEqual(p['end_pnl'],old['pnl_inr'],6)
    def test_entry_sliced_across_minutes(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        m=self.edit('UPDATE opt_1m SET volume=?',(lot*10,))           # 10% of a minute = one lot
        bars=self.bars_for(self.days);final=['G']*len(bars)
        p=self.paths(m,bars,final,cfg=pcfg(lots=3))[0][0];self.assertEqual((p['status'],p['units']),('closed',3*lot))
        old=self.go(m,bars,final,cfg=self.cfg(lots=3))[0][0];self.assertEqual(old['status'],'no_fill')   # old engine needed 3 lots in ONE minute
        p=self.paths(m,bars,final,cfg=pcfg(lots=46))[0][0];self.assertEqual((p['status'],p['reason']),('no_fill','no_liquid_entry'))   # V4.9.7: full size or nothing (45 of 46 in the one window)
        p=self.paths(m,bars,final,cfg=pcfg(lots=46,entry_bars=3))[0][0];self.assertEqual((p['status'],p['units']),('closed',46*lot))   # a second window completes it
        self.assertEqual(p['first_bar'],2);self.assertNotEqual(p['reason'],'partial_entry_unwound')                                  # rows start after the completing window
    def test_entry_price_is_average_of_slices(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty;m=self.edit('UPDATE opt_1m SET volume=?',(lot*10,))
        m=self.edit("UPDATE opt_1m SET open=open+2,high=high+2,low=low+2,close=close+2 WHERE day=? AND minute='10:01'",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=2))
        pair,_=sim.open_pair(0,'bull','naked',0);c=pair['held'][0][0]
        p0=m.quote(self.days[0],'10:00',*c)[3];p1=m.quote(self.days[0],'10:01',*c)[3];self.assertGreater(p1,p0+1);self.assertAlmostEqual(pair['px'][c],(p0+p1)/2,6)
    def test_unresolved_when_settlement_missing(self):
        m=self.build();bars=self.bars_for(self.days);m.twap.clear();m.last.clear()
        p=self.paths(m,bars,['G']*len(bars))[0][0];self.assertEqual(p['status'],'unresolved')

class Offline(Market):
    def test_take_profit_read_off_path_matches_old_engine(self):
        days=sessions('2024-01-02',8);lv=lambda d,mm:20000.+(150. if d>=days[2] else 0.)
        m=self.build(days=days,level=lv);bars=self.bars_for(days,lv);final=['G']*len(bars)
        old={t['tp']:t for t in self.go(m,bars,final,tps=(None,.5))[0]}
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        db=self.paths_db(bars,final,out);B=zg.Bars(db);o=zg.outcomes(grid_path(out[0]),B,['none','tp50'],['none'],[])
        u=out[0]['units'];self.assertAlmostEqual(o['pnl'][1,0]*u,old[.5]['pnl_inr'],1);self.assertAlmostEqual(o['pnl'][0,0]*u,old[None]['pnl_inr'],1)
    def paths_db(self,bars,final,out):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)]);return db
    def atr_case(self):
        days=sessions('2024-01-02',10);lv=lambda d,mm:20000.-(100. if d>=days[4] else 0.)
        m=self.build(days=days,level=lv,weekly='2024-01-18',far='2024-03-28')
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+20,b.close-20,b.close) for b in self.bars_for(days,lv)];return m,bars
    def test_atr_stop_read_off_path_matches_old_engine(self):
        m,bars=self.atr_case();final=['G']*len(bars)
        old=zb.Sim(m,bars,final,self.cfg(atr_mult=1.)).run({20:['bull']},structures=('naked',),offsets=(0,),tps=(None,),stops=(15,),progress=False)[0]
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({20:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        o=zg.outcomes(grid_path(out[0]),zg.Bars(self.paths_db(bars,final,out)),['none'],['none','atr15x1','atr15x5'],[])
        self.assertEqual(old['reason'],'atr_stop');self.assertAlmostEqual(o['pnl'][0,1]*out[0]['units'],old['pnl_inr'],6)
        self.assertEqual(o['J'][0,2],len(out[0]['rows']['cash_total'])-1)        # wide stop never fires
    def test_boundary_value_matches_old_engine(self):
        m=self.build();bars=self.bars_for(self.days);final=['G']*len(bars)
        old=self.go(m,bars,final,boundaries=(self.days[3],))[0][0]
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        o=zg.outcomes(grid_path(out[0]),zg.Bars(self.paths_db(bars,final,out)),['none'],['none'],[dt.date.fromisoformat(self.days[3]).toordinal()])
        self.assertAlmostEqual(float(o['vh'][0,0,0]),old['marks_at'][self.days[3]],3)
    def test_trailing_and_pct_and_target(self):
        B=zru.BarArrays.from_close([100.,101,103,102,99,105],atr={15:np.full(6,1.),30:np.full(6,np.nan),45:np.full(6,1.),20:np.full(6,1.),60:np.full(6,1.)})
        rows={k:np.zeros(5) for k in zp.FLOATS};rows['value']=np.full(5,np.nan);rows.update({k:np.zeros(5,np.int64) for k in zp.INTS})
        p=dict(signal_bar=0,rows=rows)
        J=zg.first_hits(['trail15x2','trail15x5','pct1','target15x2','atr30x1','target15x10'],p,B,1)
        self.assertEqual(list(J),[3,4,4,1,4,4])   # trail: 99<103-2 at row 3; pct1: 99 is not below 99; ATR(30) not ready: never
    def test_premium_rules_need_printed_legs(self):
        B=zru.BarArrays.from_close(np.full(6,100.),atr=1.)
        rows={k:np.zeros(5) for k in zp.FLOATS};rows.update({k:np.zeros(5,np.int64) for k in zp.INTS})
        rows['base'][:]=100;rows['cash_pair'][:]=100;rows['value']=np.array([-90.,np.nan,-40,-260,0]);rows['roll'][2]=1
        J=zg.first_hits(['tp50','loss50','loss100'],dict(signal_bar=0,rows=rows),B,1)
        self.assertEqual(list(J),[4,3,3])   # +60 at row 2 is a roll bar (no take-profit there); row 1 has no print; -160 at row 3 hits both loss stops

class Selection(unittest.TestCase):
    def test_sequence_one_position_at_a_time_per_rule(self):
        XB=np.array([[5,1],[9,9]]);VH=np.array([[[1.,2.],[3.,-4.]]]);ed=['2024-02-01','2024-02-02']
        n,s,w,ss=zg.sequence([0,3],{0:0,3:1},XB,VH,ed,[('w','2024-01-01','2025-01-01')],{'2025-01-01':0},2)
        self.assertEqual(n[0].tolist(),[1,2]);self.assertEqual(s[0].tolist(),[1.,-2.]);self.assertEqual(w[0].tolist(),[1,1])
    def test_all_configs_at_once_equals_one_by_one(self):
        rnd=np.random.default_rng(5);T=60;R=12;XB=[];VH=[];ed=[];idx={}
        for t in range(T):
            if rnd.random()<.2:continue
            idx[t]=len(XB);XB.append(t+rnd.integers(0,12,R));VH.append(rnd.normal(size=(2,R)));ed.append(f'202{3+t//30}-0{1+t%9}-10')
        XB=np.array(XB);VH=np.array(VH).transpose(1,0,2)
        wins=[('a','2023-01-01','2024-01-01'),('b','2023-01-01','2025-01-01')];hi={'2024-01-01':0,'2025-01-01':1}
        sig={c:sorted(rnd.choice(T,15,replace=False).tolist()) for c in range(7)};by=collections.defaultdict(list)
        for c,v in sig.items():
            for t in v:by[t].append(c)
        n,s,w,ss=zg.sequence_all({t:np.array(v) for t,v in by.items()},idx,XB,VH,ed,wins,hi,7,R)
        for c in range(7):
            n1,s1,w1,ss1=zg.sequence(sig[c],idx,XB,VH,ed,wins,hi,R)
            self.assertTrue(np.array_equal(n[:,c],n1));self.assertTrue(np.allclose(s[:,c],s1));self.assertTrue(np.array_equal(w[:,c],w1));self.assertTrue(np.allclose(ss[:,c],ss1))
    def test_vectorised_tp_rule_matches_report(self):
        rnd=random.Random(3)
        for _ in range(200):
            P=5;n=[rnd.choice([0,5,40,60]) for _ in range(P)];tot=[rnd.uniform(-50,100) for _ in range(P)];wins=[rnd.randint(0,x) if x else 0 for x in n]
            G=dict(n=np.array(n).reshape(1,1,P,1),total=np.array(tot).reshape(1,1,P,1),wins=np.array(wins).reshape(1,1,P,1),sumsq=np.zeros((1,1,P,1)))
            pick,nn,tt,ww,_,elig=zg.choose(G,0,30,.75,np.array([True]),np.array([True]))
            by={i:dict(n=n[i],total=tot[i],win=wins[i]/n[i] if n[i] else None) for i in range(P)}
            ref=zr.choose_tp(by,.75,30)
            if ref is zr.NO_TP_QUALIFIES:self.assertFalse(elig[0,0]);continue
            self.assertTrue(elig[0,0]);self.assertEqual(int(pick[0,0]),ref)
    def test_rule_labels_parse(self):
        for st in ('naked','calendar','futures_zone','futures_expiry'):
            for labs in (zg.profit_rules(st),zg.stop_rules(st)):
                self.assertEqual(len(labs),len(set(labs)))
                for lab in labs:zg.parse(lab)
        self.assertEqual((len(zg.profit_rules('naked')),len(zg.stop_rules('naked'))),(50,378))   # V4.9.9: +10 mloss
        # every V4.7.x rule is still in the grid
        for lab in ['tp10','tp75','target15x4','atr30x1','atr45x3','trail15x3','strail30x2.5','pct0.5','loss25','loss200']:self.assertIn(lab,zg.profit_rules('naked')+zg.stop_rules('naked'))
        self.assertEqual(zg.parse('atr30x1.5'),('atr',30,1.5));self.assertEqual(zg.parse('tp25'),('tp',.25));self.assertEqual(zg.parse('loss150'),('loss',1.5))

class Futures(unittest.TestCase):
    def test_futures_path_matches_benchmark(self):
        from nifty_iv.zone.legacy import futures as zf
        days=sessions('2024-01-02',10);mins=walk(days,drift=.3);bars=zs.aggregate(mins,45);final=['G']*30+['R']*(len(bars)-30)
        listing={d:{'2024-01-11','2024-01-18'} for d in days}
        c=SimpleNamespace(brokerage=0.,gst=.18,hedge_bps=0.,hedge_statutory=False,slippage_points=0.,lots=1,atr_mult=1.,min_short_days=4,contract_master=None)
        ref=[x for x in zf.Futures(bars,final,mins,listing,c).run({2:['bull']},('futures_zone',),(None,)) if x['status']=='closed'][0]
        cfg=pcfg();cfg.hedge_statutory=False
        sim=zp.PathSim(zb.PlanMarket(listing),bars,final,cfg,mins);out=[];sim.run({2:['bull']},('futures_zone',),(0,),sink=out.extend,progress=False)
        self.assertEqual(out[0]['end_bar'],30);self.assertAlmostEqual(out[0]['end_pnl'],ref['pnl_inr'],6)

class EndToEnd(Market):
    def test_grid_files_and_walk_forward_shape(self):
        days=sessions('2024-01-02',12);lv=lambda d,mm:20000.+(80. if d>=days[3] else 0.)
        m=self.build(days=days,level=lv,extra=('2024-01-18',));bars=self.bars_for(days,lv);final=['G']*40+['R']*(len(bars)-40)
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+10,b.close-10,b.close) for b in bars]
        sig={'zone_only_tw2':{'bull':[0,10],'bear':[45]},'N3_k0.25_m8_j0.25_tw2':{'bull':[0,20],'bear':[]}}
        p=Path(tempfile.mkdtemp())/'p.sqlite';db=sqlite3.connect(p);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)])
        db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,s,t) for c,v in sig.items() for s in v for t in v[s]])
        sim=zp.PathSim(m,bars,final,pcfg());sim.run(zb.entry_map(bars,sig,'2024-01-01','2024-12-31'),('naked','calendar'),(0,100),sink=zp.writer(db),progress=False);db.commit();db.close()
        out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False);res=zg.report(out,min_trades=1,paths_db=p)
        z=np.load(out/'grid_bull_naked_+0__none.npz');self.assertEqual(z['n'].shape,(2,len(z['windows']),50,1));self.assertEqual(z['total'].dtype,np.float64);self.assertEqual(z['sumsq'].dtype,np.float64)
        ci=list(z['configs']).index('N3_k0.25_m8_j0.25_tw2');wi=list(z['windows']).index('train_full')
        self.assertEqual(int(z['n'][ci,wi,0,0]),1)   # signal 20 falls inside the trade from signal 0 (zone holds to bar 40)
        fam=[f for f in res['families'] if f['side']=='bull' and f['structure']=='naked' and f['stops']=='any' and f['configs']=='all'][0]
        self.assertIsNotNone(fam['train_best']);d=fam['train_best_detail'];self.assertEqual(d['n'],fam['train_best']['n']);self.assertAlmostEqual(d['total'],fam['train_best']['total'],0)
        self.assertEqual(res['buy_and_hold']['train_full']['index_start'],20000.)
        self.assertTrue((out/'baselines.csv').exists());self.assertTrue(any(r['config'].startswith('zone_only') for r in res['baselines']))


class Coverage(unittest.TestCase):
    """V4.8.5: zone_coverage.py classifies each recorded gap against the options file."""
    def test_classes(self):
        import io,contextlib,csv;from nifty_iv.zone import coverage as zc;from nifty_iv.zone import contracts as zk
        d=Path(tempfile.mkdtemp());o=sqlite3.connect(d/'o.sqlite');o.executescript(zk.DDL)
        o.executemany('INSERT INTO listing VALUES(?,?,?)',[('2024-01-02','a','2024-01-04'),('2024-01-02','a','2024-03-28'),('2024-01-03','b','2024-01-04')])
        o.executemany('INSERT INTO targets VALUES(?,?,?,?)',[('2024-01-02','2024-03-28',21000.,'PE'),('2024-01-02','2024-03-28',20900.,'PE')])
        o.execute("INSERT INTO opt_1m VALUES('a','2024-01-02','09:15','2024-03-28',20900.,'PE',1,1,1,1,10)")
        o.execute("INSERT INTO opt_1m VALUES('a','2024-01-05','09:15','2024-03-28',21000.,'PE',1,1,1,1,10)");o.commit();o.close()
        p=sqlite3.connect(d/'p.sqlite');p.executescript(zp.DDL)
        G=('event','leg','day','signal_bar','side','structure','offset','cp','strike','atm','expiry','dte','roll')
        p.executemany(f'INSERT INTO gaps VALUES({",".join("?"*13)})',[
            ('not_extracted','long','2024-01-02',0,'bull','calendar',400.,'PE',21000.,20600.,'2024-03-28',86,0),     # planned, source empty that day
            ('not_extracted','long','2024-01-02',0,'bull','calendar',300.,'PE',21100.,20800.,'2024-03-28',86,1),     # never planned
            ('not_extracted','long','2024-01-02',0,'bull','calendar',0.,'PE',20000.,20000.,'2024-02-29',58,0),       # expiry not listed
            ('no_long_expiry','long','2024-01-03',1,'bear','calendar',0.,'CE',20000.,20000.,None,1,0)]);p.commit();p.close()
        with contextlib.redirect_stdout(io.StringIO()):zc.main(['--paths',str(d/'p.sqlite'),'--options',str(d/'o.sqlite'),'--out',str(d/'c')])
        rows=list(csv.DictReader(open(d/'c'/'coverage_gaps.csv')))
        self.assertEqual([r['cls'] for r in rows],['planned_no_source','not_planned','expiry_not_listed','no_far_expiry_listed'])
        self.assertEqual((rows[0]['moneyness'],rows[0]['nearest_extracted_strike_gap'],rows[0]['other_days_extracted']),('ITM400','100.0','1'))
        L={(r['year'],r['provider']):r['days_with_listed_expiry_over_30d'] for r in csv.DictReader(open(d/'c'/'coverage_listing.csv'))}
        self.assertEqual((L[('2024','any')],L[('2024','a')],L[('2024','b')]),('1','1','0'))

if __name__=='__main__':unittest.main()


class ReviewV470(Market):
    """Regression tests for the four defects reproduced in the V4.7.0 review (A: spilled roll, B: futures with no exit print,
    C: float32 statistics, D: trailing anchor) and for temporal prefix invariance."""
    def spilled(self,scale_after=None):
        days=sessions('2024-01-02',12);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',))
        m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day='2024-01-11' AND minute>='15:15'")
        if scale_after:m=self.edit("UPDATE opt_1m SET open=open*1.7,high=high*1.7,low=low*1.7,close=close*1.7,volume=volume*3 WHERE day>=?",(scale_after,))
        bars=self.bars_for(days);final=['G']*85+['R']*(len(bars)-85)
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False);return m,bars,final,out[0]
    def test_A_spilled_roll_rows_hold_only_cash_already_received(self):
        m,bars,final,p=self.spilled();R=p['rows'];j=next(i for i,x in enumerate(R['roll']) if x);k=p['first_bar']+j
        self.assertEqual((bars[k].day,bars[k].end),('2024-01-11',915))
        # the wait row at 15:30 has the short buy-back (filled 15:15-15:29) but not the long sold next morning
        self.assertLess(R['cash_total'][j+1],R['cash_total'][j+1]+R['exit_cash'][j+1])
        self.assertGreater(R['exit_bar'][j+1],0);self.assertGreaterEqual(R['exit_bar'][j+1],k+1)
        old=zb.Sim(m,bars,final,self.cfg(),boundaries=('2024-01-12',)).run({0:['bull']},structures=('calendar',),offsets=(0,),tps=(None,),progress=False)
        first=[t for t in old if t['roll']==0][0]
        o=zg.outcomes(grid_path(p),zg.Bars(Offline.paths_db(self,bars,final,[p])),['none'],['none'],[dt.date.fromisoformat('2024-01-12').toordinal()])
        self.assertAlmostEqual(float(o['vh'][0,0,0]),first['marks_at']['2024-01-12'],6)   # as-of value equals the old engine's boundary mark
    def test_A01_prefix_invariance(self):
        cut='2024-01-12';_,bars,_,a=self.spilled();_,_,_,b=self.spilled(scale_after=cut)
        n=sum(1 for i in range(len(a['rows']['mark'])) if bars[a['first_bar']+i].day<cut);self.assertGreater(n,60)
        for k in ('cash_total','cash_pair','base','value','mark','roll','forced_before'):
            np.testing.assert_array_equal(np.asarray(a['rows'][k][:n]),np.asarray(b['rows'][k][:n]),err_msg=k)
        self.assertNotAlmostEqual(a['end_pnl'],b['end_pnl'],2)    # later data does change the outcome
    def test_B_futures_exit_waits_for_a_print(self):
        days=sessions('2024-01-02',2);m=self.build(days=days);bars=self.bars_for(days);final=['G']*2+['R']*(len(bars)-2)
        mins={days[0]:[(600,20000.,20000.,20000.,20000.)]}
        s=zp.PathSim(m,bars,final,pcfg(),mins);s.futures_path(0,'bull','futures_zone');p=s.done[0]
        self.assertEqual(p['status'],'open_at_data_end');self.assertTrue(np.isnan(p['rows']['exit_cash'][-1]))
        mins={days[0]:[(600,20000.,20000.,20000.,20000.)],days[1]:[(700,19900.,19900.,19900.,19900.)]}   # first print after the flip: next day 11:40
        c=pcfg();c.hedge_statutory=False;s=zp.PathSim(m,bars,final,c,mins);s.futures_path(0,'bull','futures_zone');p=s.done[0]
        self.assertEqual((p['status'],p['reason']),('closed','zone_flip'));xb=p['rows']['exit_bar'][-1]
        self.assertEqual((bars[xb].day,bars[xb].end),(days[1],690));self.assertAlmostEqual(p['end_pnl'],(19900-20000)*p['units'],6)
    def test_C_statistics_survive_low_dispersion(self):
        x=np.array([100.+(i%2)*.001 for i in range(1000)]);self.assertTrue(np.isfinite(zg.tstat(len(x),float(x.sum()),float(np.square(x).sum()))))
    def test_D_trailing_stop_starts_at_the_fill(self):
        B=zru.BarArrays.from_close([120.,101.,102.,103.],atr=1.)
        R={k:np.zeros(3) for k in zp.FLOATS};R.update({k:np.zeros(3,dtype=np.int64) for k in zp.INTS});R['value'][:]=np.nan
        J=zg.first_hits(['trail15x2','strail15x2'],dict(signal_bar=0,rows=R,entry_index=100.),B,1)
        self.assertEqual(list(J),[2,0])    # from the fill: never more than 2 below the best close; from the signal: at once
    def test_paths_record_index_at_fill(self):
        days=sessions('2024-01-02',8);lv=lambda d,mm:20000.+(50. if mm>='10:05' else 0.)
        m=self.build(days=days,level=lv);bars=self.bars_for(days,lv)
        mins={d:[(mi,)+(lv(d,zs.hhmm(mi)),)*4 for mi in range(zs.OPEN,zs.CLOSE)] for d in days}
        sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(),mins);out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        self.assertEqual(out[0]['entry_index'],20000.)          # filled in the 10:00 minute, before the move


class LotTransition(unittest.TestCase):
    def test_futures_entry_with_unknown_lot_is_rejected_not_crashed(self):
        days=sessions('2021-06-21',6);mins=walk(days,drift=.3);bars=zs.aggregate(mins,45);final=['G']*len(bars)
        listing={d:{'2021-07-01','2021-07-08'} for d in days}            # weekly expiries in the 2021 lot transition
        self.assertIsNone(LotMaster(None).get(days[0],'2021-07-08').qty)
        sim=zp.PathSim(zb.PlanMarket(listing),bars,final,pcfg(),mins);out=[]
        sim.run({2:['bull']},('futures_zone','futures_expiry'),(0,),sink=out.extend,progress=False)
        self.assertEqual({(p['status'],p['reason']) for p in out},{('no_fill','lot_unknown')})


class ReviewV472(Market):
    """V4.7.2 review: zone flip during a pending roll exit; resume integrity; report lineage; partial calendar entries."""
    def flip_case(self,bump=0.):
        days=sessions('2024-01-02',12);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',))
        m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND ((day='2024-01-11' AND minute>='15:15') OR (day='2024-01-12' AND minute<'10:00'))")   # long sells 10:00+ on the 12th
        if bump:m=self.edit("UPDATE opt_1m SET open=open+?,high=high+?,low=low+?,close=close+? WHERE expiry='2024-03-28' AND day='2024-01-12'",(bump,)*4)
        bars=self.bars_for(days);flip=next(i for i,b in enumerate(bars) if b.day=='2024-01-11' and b.end==930);final=['G']*flip+['R']*(len(bars)-flip)
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False);return bars,out[0],sim
    def test_zone_flip_during_pending_roll_waits_for_the_last_fill(self):
        bars,p,sim=self.flip_case();self.assertEqual(sim.counts['roll_exit_spilled'],1);self.assertEqual(p['reason'],'zone_flip')
        flip=next(i for i,b in enumerate(bars) if b.day=='2024-01-11' and b.end==930)
        self.assertEqual(p['end_fill_day'],'2024-01-12')
        self.assertEqual(p['end_bar'],flip+1)                     # the exit completes in the minutes after bar flip+1 (10:00 on the 12th), not at the flip
        self.assertEqual(int(p['rows']['exit_bar'][-1]),flip+1)   # and that bar holds the one-position lock
        self.assertEqual(p['pairs'],1)                            # no new pair after the flip
    def test_flip_case_prefix_invariance(self):
        bars,a,_=self.flip_case();_,b,_=self.flip_case(bump=100.)
        n=sum(1 for i in range(len(a['rows']['mark'])) if bars[a['first_bar']+i].day<'2024-01-12')
        for k in ('cash_total','mark','exit_bar','roll'):np.testing.assert_array_equal(np.asarray(a['rows'][k][:n]),np.asarray(b['rows'][k][:n]),err_msg=k)
        self.assertNotAlmostEqual(a['end_pnl'],b['end_pnl'],1)                          # the outcome depends on the 12th, and is only known on the 12th
    def grid_db(self):
        days=sessions('2024-01-02',12);lv=lambda d,mm:20000.+(80. if d>=days[3] else 0.)
        m=self.build(days=days,level=lv,extra=('2024-01-18',));bars=self.bars_for(days,lv);final=['G']*40+['R']*(len(bars)-40)
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+10,b.close-10,b.close) for b in bars]
        sig={'zone_only_tw2':{'bull':[0,10],'bear':[]},'N3_k0.25_m8_j0.25_tw2':{'bull':[0,20],'bear':[]}}
        p=Path(tempfile.mkdtemp())/'p.sqlite';db=sqlite3.connect(p);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)])
        db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,s,t) for c,v in sig.items() for s in v for t in v[s]])
        zp.PathSim(m,bars,final,pcfg()).run(zb.entry_map(bars,sig,'2024-01-01','2024-12-31'),('naked',),(0,),sink=zp.writer(db),progress=False);db.commit();db.close();return p
    def test_resume_recomputes_a_corrupted_group_file(self):
        p=self.grid_db();out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False)
        f=out/'grid_bull_naked_+0__atr.npz';side=zg.read_json(str(f)+'.json')
        with open(f,'ab') as fh:fh.write(b'corruption')
        import io,contextlib;buf=io.StringIO()
        with contextlib.redirect_stdout(buf):zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False,resume=True)
        self.assertIn('recomputing',buf.getvalue());self.assertEqual(zg.digest(f),zg.read_json(str(f)+'.json')['file_sha256'])
    def test_report_refuses_mismatched_inputs(self):
        p=self.grid_db();q=self.grid_db();out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False)
        con=sqlite3.connect(q);con.execute("UPDATE bars SET close=close+1 WHERE idx=0");con.commit();con.close()
        with self.assertRaises(SystemExit):zg.report(out,min_trades=1,paths_db=q)
        with open(out/'grid_bull_naked_+0__none.npz','ab') as fh:fh.write(b'x')
        with self.assertRaises(SystemExit):zg.report(out,min_trades=1)
    def test_calendar_with_a_silent_long_is_not_entered(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '10:00' AND '10:44'",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        self.assertEqual((out[0]['status'],out[0]['reason']),('no_fill','no_liquid_entry'))   # V4.8.5: the short is never sold alone
        self.assertEqual(sim.counts['no_liquid_entry_limited_by_long'],1);self.assertEqual(sim.counts['partial_entry'],0)
    def test_new_pair_fills_after_the_exits_last_minute(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        m=self.edit("UPDATE opt_1m SET volume=0 WHERE day=? AND minute BETWEEN '10:01' AND '10:44'",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg())
        self.assertIsNotNone(sim.open_pair(0,'bull','naked',0)[0])                     # 10:00 is liquid
        self.assertEqual(sim.open_pair(0,'bull','naked',0,start=(self.days[0],46))[1],'no_liquid_entry')   # from 10:01 on nothing trades
        self.assertIsNotNone(sim.open_pair(0,'bull','naked',0,start=(self.days[1],46))[0])                # a start on another day does not constrain this window
    def test_vendor_tie_is_deterministic(self):
        m=self.build(vendor2=(5.,100000));d=zp.load_day(m.db,self.days[0]);k=('2024-01-11',20000.,'PE')
        self.assertAlmostEqual(d.px[k][45],m.quote(self.days[0],'10:00','2024-01-11',20000.,'PE')[0],6)   # equal volume: rissin_upstox (first by name) in both readers

class NewRules(unittest.TestCase):
    def path(self,n,**rows):
        R={k:np.zeros(n) for k in zp.FLOATS};R.update({k:np.zeros(n,np.int64) for k in zp.INTS});R['value'][:]=np.nan
        for k,v in rows.items():R[k]=np.asarray(v,R[k].dtype)
        return dict(signal_bar=0,rows=R,entry_index=100.)
    def hits(self,rules,close,sign=1,days=None,end=None,**rows):
        B=zru.BarArrays.from_close(close,atr=1.,day=days,end=end);return list(zg.first_hits(rules,self.path(len(close)-1,**rows),B,sign))
    def test_breakeven_and_giveback(self):
        c=[100,100.5,102,101,100.05,99]   # best +2 (>= 1.5 ATR), then back to entry
        # rows: 100.5 102 101 100.05 99 -> move from entry .5 2 1 .05 -1; best move reaches 2 at row 1
        self.assertEqual(self.hits(['be1.5c0','be1.5c0.25','be3c0','gb1.5x0.5'],c),[4,3,4,2])   # be: falls to 0 at row 4 / to .25 at row 3; gb: gives back half (1) at row 2
    def test_time_tod_weekend(self):
        days=['2024-01-04']*2+['2024-01-05']*3;end=[600,645,870,915,930]+[0]   # Thu, Fri (Sat-Sun gap follows)
        c=[100]*6;days=['2024-01-04']+days;end=[555]+end[:5]
        self.assertEqual(self.hits(['time1','tod1515','wkend'],c,days=days,end=end),[2,3,3])
    def test_points_epct_chandelier_donchian(self):
        c=[100,103,104,99.5,96,95];h=self.hits(['pts3','epct3','chand6x4','donch6c0'],c)
        self.assertEqual(h[0],3);self.assertEqual(h[1],3)          # 96 is 4 points / 4% below the entry at 100
        self.assertEqual(h[2],2);self.assertEqual(h[3],2)          # chand: 99.5 < best 104 - 4 ATR; donch: 99.5 < the lowest of the earlier bars (100)
    def test_mult_needs_short_price(self):
        P=self.path(3,value=[-50.,-100.,-210.],base=[40,40,40],cash_pair=[45,45,45]);P['entry_short_px']=50.;P['units']=1
        B=zru.BarArrays.from_close([100,100,100,100],atr=1.)
        self.assertEqual(list(zg.first_hits(['mult2','mult4'],P,B,1)),[1,2])
    def test_stability_prefers_a_stable_neighbourhood(self):
        # 3 configs in a line (neighbours), 1 profit, 3 stops in a line; cell (0,0,0) high but its neighbours lose
        C,P,S=3,1,3;n=np.full((C,P,S),20);T=np.array([[[500,-300,-300]],[[-300,100,100]],[[90,100,110]]],float)
        CN=np.array([[1,-1],[0,2],[1,-1]]);PN=np.full((1,1),-1);SN=np.array([[1,-1],[0,2],[1,-1]])
        score,share=zg.stability(n,T,15,CN,PN,SN)
        self.assertTrue(np.isnan(score[0,0,1]))          # losing cells are not scored
        self.assertLess(score[0,0,0],0)                   # 500 with neighbours -300, -300: lower quartile -300
        self.assertEqual(score[2,0,1],90.)                # 100 with neighbours 100, 90, 110
        self.assertEqual(score[2,0,2],100.)               # 110 with neighbours 100, 100
        ci,pi,si=np.unravel_index(np.nanargmax(np.where(score>0,score,np.nan)),score.shape);self.assertEqual((ci,si),(2,2))


class ReviewV480(Market):
    """V4.8.0 review: partial quantities, dated re-entry, algorithm binding, stability edges, cutoff mid-exit, fill stress, carry."""
    def fake(self,q):
        """q: per-minute tradable units of each leg (3 minutes), price 100."""
        return SimpleNamespace(window=lambda k:('2024-01-03',45,48),counts=collections.Counter(),
            caps=lambda day,i0,i1,c,lot,buy:(np.array(q[c],float),np.full(3,100.),np.array(q[c],float)*10),
            book=lambda acc,c,sign,lot:-float(acc[1])*100.*sign,index_at=lambda *a:20000.)
    def test_legs_fill_together_minute_by_minute(self):
        sh=('2024-01-11',20000.,'PE');lg=('2024-03-28',20000.,'PE');legs=[(sh,-100),(lg,100)]
        for q,status,n,need in (({sh:[0,0,0],lg:[0,0,0]},'none',0,50),({sh:[50,0,0],lg:[0,50,0]},'none',0,50),   # never in the same minute: nothing
                           ({sh:[50,50,0],lg:[50,0,50]},'ok',50,50),({sh:[100,0,0],lg:[50,50,50]},'ok',50,50),
                           ({sh:[50,50,0],lg:[50,0,50]},'none',0,None),({sh:[100,0,0],lg:[50,50,50]},'none',0,None),   # V4.9.7 default: full size or nothing
                           ({sh:[50,50,50],lg:[100,100,100]},'ok',100,None),({sh:[100,100,0],lg:[100,0,0]},'ok',100,None)):
            r=zp.PathSim.enter(self.fake(q),0,legs,50,need=need);self.assertEqual(r['status'],status,q)
            if n:
                self.assertEqual(dict(r['filled']),{sh:-n,lg:n},q);self.assertEqual(r['units'],n)       # equal quantities: never a naked leg
                self.assertAlmostEqual(r['cash'],0.)                                                    # sold n at 100, bought n at 100
        r=zp.PathSim.enter(self.fake({sh:[50,50,0],lg:[50,0,50]}),0,legs,50,need=100);self.assertEqual(r['status'],'none')   # a roll needs the full size
    def test_thin_long_gives_a_smaller_matched_calendar(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        m=self.edit("UPDATE opt_1m SET volume=? WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '10:00' AND '10:44'",(lot*10,self.days[0]))   # 1 lot a minute for the long
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=50));out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        self.assertEqual((out[0]['status'],out[0]['reason']),('no_fill','no_liquid_entry'))                    # V4.9.7: 45 of 50 lots in the one window: nothing
        sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=50,entry_bars=3));out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        p=out[0];self.assertNotEqual(p['reason'],'partial_entry_unwound');self.assertEqual(p['units'],50*lot)   # the next window completes the full 50
        self.assertEqual(sim.counts['entry_reduced'],0);self.assertEqual(sim.counts['entry_completed_in_window_2'],1)
    def test_start_is_dated(self):
        sim=zp.PathSim(None,self.bars_for(sessions('2024-01-02',3)),['G']*27,pcfg())
        self.assertIsNone(sim.after(None));self.assertEqual(sim.after(('2024-01-02',365)),('2024-01-02',366))
    def test_resume_and_report_bound_to_the_algorithm(self):
        p=ReviewV472.grid_db(self);out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False)
        old=zg.GRID_ALGORITHM
        try:
            zg.GRID_ALGORITHM='changed'
            with self.assertRaises(SystemExit):zg.report(out,min_trades=1)
            import io,contextlib;buf=io.StringIO()
            with contextlib.redirect_stdout(buf):zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False,resume=True)
            self.assertIn('recomputing',buf.getvalue());zg.report(out,min_trades=1)       # rebuilt under the new algorithm: accepted
        finally:zg.GRID_ALGORITHM=old
    def test_stability_scores_grid_edges(self):
        cfg=[f'zone_only_tw{x}' for x in range(2,7)];n=np.full((5,1,1),20);tot=np.array([1000.,900.,800.,700.,600.]).reshape(5,1,1)
        sc,_,cov=zg.stability(n,tot,15,zru.config_neighbours(cfg),zru.neighbours(['none']),zru.neighbours(['none']),return_coverage=True)
        self.assertEqual(list(sc[:,0,0]),[900.,800.,700.,600.,600.])                     # tw2: min(1000, 900); tw6: min(600, 700)
        self.assertEqual(list(cov[:,0,0]),[1.]*5)
    def test_cutoff_inside_a_stop_exit_matches_old_engine(self):
        days=sessions('2023-12-20',10);drop=lambda d,mm:20000.-(100. if (d,mm)>=('2023-12-29','12:00') else 0.)
        m=self.build(days=days,level=drop,weekly='2024-01-04',far='2024-03-28')
        m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day='2023-12-29' AND minute>='12:00'")   # long cannot be sold until 2024-01-01
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+20,b.close-20,b.close) for b in self.bars_for(days,drop)];final=['G']*len(bars)
        old=zb.Sim(m,bars,final,self.cfg(atr_mult=1.),boundaries=('2024-01-01',)).run({20:['bull']},structures=('calendar',),offsets=(0,),tps=(None,),stops=(15,),progress=False)[0]
        self.assertEqual((old['reason'],old['exit_day']),('atr_stop','2024-01-01'))
        sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({20:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        h=dt.date.fromisoformat('2024-01-01').toordinal();B=zg.Bars(Offline.paths_db(self,bars,final,out))
        o=zg.outcomes(grid_path(out[0]),B,['none'],['none','atr15x1'],[h],structure='calendar')
        self.assertAlmostEqual(float(o['vh'][0,0,1]),old['marks_at']['2024-01-01'],6)    # short bought back on the 29th, long at its last close
        self.assertNotAlmostEqual(float(o['vh'][0,0,1]),float(o['vh'][0,0,0]),3)          # differs from the natural path's mark
    def test_adverse_fill_prices_cost_more(self):
        m=self.build();m=self.edit('UPDATE opt_1m SET high=close*1.1,low=close*0.9');bars=self.bars_for(self.days)
        base=Paths.paths(self,m,bars,['G']*len(bars))[0][0];stress=Paths.paths(self,m,bars,['G']*len(bars),cfg=pcfg(fill_price='adverse'))[0][0]
        self.assertLess(stress['end_pnl'],base['end_pnl'])
    def test_assumed_carry_sign(self):
        tr=[dict(unit=100.,index=20000.,sign=1.,entry_day='2024-01-01'),dict(unit=50.,index=20000.,sign=-1.,entry_day='2024-02-01')]
        c=zg._carry(tr,[100.,50.],[365,73],.05);self.assertAlmostEqual(c['assumed_carry_points'],1000.-200.,6)   # long pays a year, short receives 73 days
        self.assertAlmostEqual(c['total_net_of_assumed_carry'],150.-800.,6)

class Layered(Market):
    def test_layered_policy_fires_at_the_earliest_part(self):
        c=[100,101,103,104,101.5,99,95,96]
        P=NewRules.path(self,len(c)-1);B=zru.BarArrays.from_close(c,atr=1.)
        parts=zg.first_hits(['atr15x3','trail15x2','time3'],P,B,1)
        lay=zg.first_hits(['c:atr15x3|trail15x2|time3','c:atr15x3|none|none','c:none|trail15x2|none'],P,B,1)
        self.assertEqual(int(lay[0]),int(parts.min()));self.assertEqual(int(lay[1]),int(parts[0]));self.assertEqual(int(lay[2]),int(parts[1]))
    def test_layered_family_in_grid_and_report(self):
        p=ReviewV472.grid_db(self);out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False)
        files=[e['file'] for e in zg.read_json(out/'grid_index.json')['groups']];self.assertTrue(any('__compound_' in f for f in files))
        z=np.load(out/'grid_bull_naked_+0__compound_time.npz');self.assertEqual(list(z['profits']),zru.compound_profits('naked'))
        res=zg.report(out,min_trades=1);self.assertTrue(any(f['stops']=='compound' for f in res['families']))


class ReviewV481(Market):
    """V4.8.1 review: dated fill cash, as-of state at any window end, unpriceable remainders, indicator code in the digest,
    stability estimator, calendar weekends, prefix invariance across a year end."""
    def standin(self,fills,close=20.):
        s=zp.PathSim.__new__(zp.PathSim);days=['2023-12-26','2023-12-27','2023-12-28','2023-12-29','2024-01-02']
        s.bars=[SimpleNamespace(day=d,end=930) for d in days];s.cfg=SimpleNamespace(max_exit_lag_bars=99,forced_exit_haircut=0.)
        s.memo={};s.counts=collections.Counter();s.lc={};s.day_end={d:i for i,d in enumerate(days)};s.session_days=sorted(days)
        s.window=lambda t:((s.bars[t+1].day,0,45) if t+1<len(s.bars) else None)
        s.slice=lambda day,i0,i1,c,want,lot,buy=True:fills.get(day,(0,0.,0.,None))
        s.book=lambda acc,c,sign,lot:acc[0]*(-1 if sign==1 else 1)          # zero costs: sale proceeds = notional
        s.last_close=(lambda c,k,end:close(end)) if callable(close) else (lambda c,k,end:close)
        s.settle_px=lambda c:None;return s
    def test_cash_is_booked_on_the_day_it_is_received(self):
        s=self.standin({'2023-12-29':(50,500.,100.,10),'2024-01-02':(50,5000.,100.,10)})
        r=s.exit(((('2024-03-28',20000.,'PE'),100),),2,50);sched=dict(r[5]);days,cash,value,n,f=r[8]
        self.assertEqual(sched,{3:500.,4:5000.});self.assertEqual(n,2)
        self.assertEqual(cash[:2],(500.,5500.));self.assertEqual(value[0],50*20.)       # end of the 29th: Rs 500 in, 50 units still open at 20
    def test_window_end_uses_the_last_exit_day_before_it(self):
        fills={'2023-12-27':(30,300.,1.,1),'2023-12-29':(30,600.,1.,1),'2024-01-02':(40,4000.,1.,1)}
        s=self.standin(fills,close=lambda end:10.*(end+1));r=s.exit(((('2024-03-28',20000.,'PE'),100),),0,10);days,cash,value,n,f=r[8]
        self.assertEqual((n,f),(4,3));self.assertEqual(cash[:4],(300.,300.,900.,4900.))   # sessions 27, 28, 29 Dec, 2 Jan; fills on 3 of them
        self.assertEqual(value[:3],(70*20.,70*30.,40*40.))                                # 28th: no fill, 70 still open, repriced at its close
        # a path row with these slots, valued at 1 January: cash to the 29th plus the 40 units at the 29th's close
        L=1;R={k:np.zeros(L) for k in zp.FLOATS};R.update({k:np.zeros(L,np.int64) for k in zp.INTS})
        R['exit_cash'][:]=4900.;R['mark'][:]=777.;R['exit_day'][:]=dt.date(2024,1,2).toordinal();R['exit_sessions'][:]=n;R['value'][:]=np.nan
        for q in range(zp.SLOTS):R[f'exit_slot_day{q}'][:]=days[q];R[f'exit_slot_cash{q}'][:]=cash[q];R[f'exit_slot_value{q}'][:]=value[q]
        B=zru.BarArrays.from_close([100.,100.],atr=1.,day=['2023-12-26','2023-12-27'],end=[930,930])
        b=zru.Batch([dict(side='bull',signal_bar=0,rows=R,units=1,status='closed',end_pnl=4900.,entry_day='2023-12-27')],B,'calendar')
        self.assertEqual(float(b.row_value(dt.date(2024,1,1).toordinal())[0,0]),900.+1600.)
        self.assertEqual(float(b.row_value(dt.date(2023,12,28).toordinal())[0,0]),300.+1400.)
    def test_window_end_uses_the_last_session_before_it_even_without_fills(self):
        # the V4.8.2 review case: sell 50 on 27 Dec, nothing on 30-31 Dec, rest on 2 Jan; value at 1 Jan uses the 31 Dec close
        def run(dec31):
            s=self.standin({'2023-12-27':(50,500.,1.,1),'2024-01-02':(50,5000.,1.,1)},close=lambda end:{1:20.,2:40.,3:dec31,4:100.}.get(end,10.))
            s.bars=[SimpleNamespace(day=d,end=930) for d in ['2023-12-26','2023-12-27','2023-12-28','2023-12-29','2024-01-02']]
            r=s.exit(((('2024-03-28',20000.,'PE'),100),),0,10);days,cash,value,n,f=r[8]
            L=1;R={k:np.zeros(L) for k in zp.FLOATS};R.update({k:np.zeros(L,np.int64) for k in zp.INTS})
            R['exit_cash'][:]=r[0];R['exit_day'][:]=dt.date(2024,1,2).toordinal();R['exit_sessions'][:]=n;R['value'][:]=np.nan
            for q in range(zp.SLOTS):R[f'exit_slot_day{q}'][:]=days[q];R[f'exit_slot_cash{q}'][:]=cash[q];R[f'exit_slot_value{q}'][:]=value[q]
            B=zru.BarArrays.from_close([100.,100.],atr=1.,day=['2023-12-26','2023-12-27'],end=[930,930])
            b=zru.Batch([dict(side='bull',signal_bar=0,rows=R,units=1,status='closed',end_pnl=r[0],entry_day='2023-12-27')],B,'calendar')
            return float(b.row_value(dt.date(2024,1,1).toordinal())[0,0]),n,f
        a=run(60.);b=run(120.)
        self.assertEqual((a[1],a[2]),(4,2))                             # 4 sessions (27, 28, 29 Dec, 2 Jan), fills on 2 of them
        self.assertEqual(a[0],500.+50*60.);self.assertEqual(b[0],500.+50*120.)
    def test_unpriceable_remainder_is_nan_not_zero(self):
        s=self.standin({'2023-12-29':(50,500.,100.,10),'2024-01-02':(50,5000.,100.,10)},close=None);s.last_close=lambda c,k,end:None   # completes on the 2nd
        r=s.exit(((('2024-03-28',20000.,'PE'),100),),2,50);self.assertTrue(math.isnan(r[8][2][0]));self.assertGreaterEqual(s.counts['exit_session_slot_unpriced'],1)
    def test_digest_covers_indicator_code(self):
        base=zg.algorithm_digest();orig=zs.atr
        try:zs.atr=lambda bars,p:np.full(len(bars),123.);self.assertNotEqual(zg.algorithm_digest(),base)
        finally:zs.atr=orig
        self.assertEqual(zg.algorithm_digest(),base)
    def test_stability_estimator_is_the_documented_conservative_quartile(self):
        CN=np.array([[1,2,3],[0,-1,-1],[0,-1,-1],[0,-1,-1]]);n=np.full((4,1,1),20);T=np.array([100.,200.,300.,400.]).reshape(4,1,1)
        sc,_=zg.stability(n,T,15,CN,np.full((1,1),-1),np.full((1,1),-1))
        self.assertEqual(sc[0,0,0],100.)        # values 100..400: floor(3/4)=0 -> the smallest, by design (np.percentile would give 175)
    def test_weekend_is_a_calendar_fact(self):
        B=zru.BarArrays.from_close([100.]*4,atr=1.,day=['2024-01-01','2024-01-03','2024-01-04','2024-01-08'],end=[930]*4)
        self.assertEqual(list(B.weekend),[False]*4)                  # no holiday list: Fridays only; data gaps never make a weekend
        B.set_holidays(['2024-01-05']);self.assertEqual(list(B.weekend),[False,False,True,False])   # Friday a holiday: Thursday is the last session
        G=zru.BarArrays.from_close([100.]*2,atr=1.,day=['2024-01-01','2024-01-08'],end=[930]*2)
        self.assertEqual(list(G.weekend),[False,False])              # Monday -> Monday feed gap (the V4.8.2 review case)
    def test_year_end_value_ignores_quotes_from_the_new_year(self):
        def run(bump):
            days=sessions('2023-12-20',10);drop=lambda d,mm:20000.-(100. if (d,mm)>=('2023-12-29','12:00') else 0.)
            m=self.build(days=days,level=drop,weekly='2024-01-04',far='2024-03-28')
            m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day='2023-12-29' AND minute>='12:00'")
            if bump:m=self.edit("UPDATE opt_1m SET open=open*2,high=high*2,low=low*2,close=close*2 WHERE day>='2024-01-01'")
            bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+20,b.close-20,b.close) for b in self.bars_for(days,drop)];final=['G']*len(bars)
            sim=zp.PathSim(m,bars,final,pcfg());out=[];sim.run({20:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
            o=zg.outcomes(grid_path(out[0]),zg.Bars(Offline.paths_db(self,bars,final,out)),['none'],['none','atr15x1'],[dt.date(2024,1,1).toordinal()],structure='calendar')
            return float(o['vh'][0,0,1]),float(o['pnl'][0,1])
        a=run(False);b=run(True)
        self.assertEqual(a[0],b[0]);self.assertNotEqual(a[1],b[1])     # value at 1 Jan unchanged; the final P&L does depend on January


class DataGapLegs(Market):
    def test_missing_long_contract_is_a_data_gap_not_a_partial_fill(self):
        m=self.build();m=self.edit("DELETE FROM opt_1m WHERE expiry='2024-03-28' AND day=?",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        self.assertEqual((out[0]['status'],out[0]['reason']),('no_fill','not_extracted'))
        self.assertEqual(sim.counts['partial_entry'],0);self.assertEqual(sim.counts['not_extracted_one_leg_long'],1)
    def test_extracted_but_silent_long_is_illiquid_not_a_gap(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day=?",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        self.assertEqual(out[0]['reason'],'no_liquid_entry');self.assertEqual(sim.gaps,[])
    def test_gap_is_recorded_with_its_contract(self):
        m=self.build();m=self.edit("DELETE FROM opt_1m WHERE expiry='2024-03-28' AND day=?",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg());sim.run({0:['bull']},('calendar',),(0,),sink=lambda r:None,progress=False)
        g=sim.gaps[0];self.assertEqual((g['event'],g['leg'],g['expiry'],g['cp'],g['roll']),('not_extracted','long','2024-03-28','PE',0))
        self.assertEqual(g['strike']-g['atm'],0.);self.assertGreater(g['dte'],30)
    def test_roll_reopen_keeps_the_path_size(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=2))
        self.assertIsNone(sim.open_pair(0,'bull','naked',0,size=3*lot-1)[0])                      # not a whole number of lots
        pair,_=sim.open_pair(0,'bull','naked',0,size=lot);self.assertEqual(pair['units'],lot)


class Portfolio(unittest.TestCase):
    """V4.9: green + red portfolio on rupee returns, yearly floors and combined daily drawdown."""
    def setUp(self):
        from nifty_iv.zone import portfolio as zpf;self.zpf=zpf
        self.days=[d for d in (dt.date(2022,12,1)+dt.timedelta(i) for i in range(500)) if d.weekday()<5]
        self.sd=[d.isoformat() for d in self.days];self.so=np.array([d.toordinal() for d in self.days])
    def tr(self,e0,e1,unit,units=750.,marks=None,sign=1.,index=20000.):
        e0,e1=self.sd[e0],self.sd[e1];mo=[dt.date.fromisoformat(e0).toordinal()+k for k in range(len(marks or []))]
        return dict(entry_day=e0,exit_day=e1,unit=unit,units=units,marks=np.array(marks or [],float),mark_ord=np.array(mo,np.int64),sign=sign,index=index)
    def test_equity_marks_then_realises(self):
        E,op,bad=self.zpf.equity([self.tr(2,5,10.,marks=[1,-4,float('nan')]),self.tr(5,8,-2.)],self.so)
        self.assertEqual(list(E[:10]),[0,0,750,-3000,-3000,7500,7500,7500,6000,6000])      # NaN mark carried forward; exit realises the final P&L
        self.assertEqual(bad,0);self.assertTrue(op[2:5].all() and not op[0] and op[5:8].all() and not op[8])
    def test_equity_uses_each_trades_units_and_skips_unvalued(self):
        E,_,bad=self.zpf.equity([self.tr(1,2,10.,units=375.),self.tr(3,4,float('nan')),self.tr(5,6,10.,units=1500.)],self.so)
        self.assertEqual((E[-1],bad),(10*375+10*1500,1))
    def test_open_at_window_end_takes_the_window_end_value(self):
        E,op,_=self.zpf.equity([self.tr(len(self.sd)-3,len(self.sd)-1,7.,marks=[1,2])],self.so[:-1])
        self.assertEqual(E[-1],7*750)
    def test_futures_carry_charged_pro_rata(self):
        t=self.tr(0,10,100.,units=1.,marks=[0.]);E,_,_=self.zpf.equity([t],self.so,'futures',.0365)
        days=(self.days[10]-self.days[0]).days;self.assertAlmostEqual(E[10],100-20000*.0365/365*days,9)
        Es,_,_=self.zpf.equity([dict(t,sign=-1.)],self.so,'futures',.0365);self.assertAlmostEqual(Es[10],100+20000*.0365/365*days,9)
    def test_year_slices_annualise_part_years(self):
        ys=self.zpf.year_slices(self.sd,'2022-12-01','2024-01-01')
        self.assertEqual([(y,d) for y,*_,d in ys],[(2022,31),(2023,365)]);self.assertAlmostEqual(ys[0][3],365/31)
    def test_pair_metrics_match_brute_force(self):
        rnd=np.random.default_rng(3);T=len(self.sd);G=np.cumsum(rnd.normal(500,20000,(5,T)),1);R=np.cumsum(rnd.normal(300,15000,(4,T)),1)
        lo,hi='2022-12-01','2024-01-01';ys=self.zpf.year_slices(self.sd,lo,hi);i0=ys[0][1];i1=ys[-1][2];span=sum(y[4] for y in ys)/365
        Y,tot,worst,dd=self.zpf.pair_metrics(G,R,i0,i1,ys,2.5e6,span,chunk=2,min_days=90)
        for a in range(5):
            for b in range(4):
                e=G[a]+R[b];s=np.r_[0,e[i0:i1+1]];self.assertAlmostEqual(dd[a,b],(np.maximum.accumulate(s)-s).max(),6)
                y23=[i for i,d in enumerate(self.sd) if d[:4]=='2023'];p23=(e[y23[-1]]-e[y23[0]-1])/2.5e6
                self.assertAlmostEqual(Y[a,b,1],p23,9);self.assertAlmostEqual(worst[a,b],p23,9)          # the 31-day 2022 stub is not judged
                self.assertAlmostEqual(Y[a,b,0],e[ys[0][2]]/2.5e6,9)                                    # nor annualised
                self.assertAlmostEqual(tot[a,b],e[i1]/2.5e6/span,9)
    def test_ranking_and_gates(self):
        tot=np.array([[.30,.25]]);worst=np.array([[.18,.20]]);dd=np.array([[200000.,100000.]])
        k1,k2=self.zpf.rank_key('dd',tot,worst,dd);self.assertEqual(int(np.lexsort((k2.ravel(),k1.ravel()))[0]),1)
        k1,k2=self.zpf.rank_key('calmar',tot,worst,dd);self.assertEqual(int(np.lexsort((k2.ravel(),k1.ravel()))[0]),1)
        k1,k2=self.zpf.rank_key('worst_year',tot,worst,dd);self.assertEqual(int(np.lexsort((k2.ravel(),k1.ravel()))[0]),1)


class PortfolioV493(unittest.TestCase):
    """Review cases V4.9.1-V4.9.2 (AT01-AT14, AT17, AT20 of the V4.9.2 review) on the V4.9.3 portfolio."""
    def setUp(self):
        from nifty_iv.zone import portfolio as zpf;self.zpf=zpf
        self.days=[d for d in (dt.date(2022,12,1)+dt.timedelta(i) for i in range(800)) if d.weekday()<5]
        self.sd=[d.isoformat() for d in self.days];self.so=np.array([d.toordinal() for d in self.days]);self.close=np.full(len(self.so),20000.)
    def tr(self,e0,e1,unit,units=650.,marks=None,sign=1.,index=20000.,lot=65,sb=None):
        d0,d1=self.sd[e0],self.sd[e1];mo=[dt.date.fromisoformat(d0).toordinal()+k for k in range(len(marks or []))]
        return dict(entry_day=d0,exit_day=d1,unit=unit,units=units,marks=np.array(marks or [],float),mark_ord=np.array(mo,np.int64),sign=sign,index=index,lot=lot,signal_bar=sb if sb is not None else 4*e0-1)
    # bars: 4 per session; a trade entering session e0 is signalled at bar 4*e0-1 (previous session's last bar) and fills at bar 4*e0
    def ev(self,x,e0,exit_bar):return ('trade',4*e0-1,exit_bar,x)
    def _acc(self,book,capital=2.5e6,buffer=0.,margin=None,max_lots=None,bar_close=None,sizing='exchange',tu=None):
        bars=SimpleNamespace(day=[d for d in self.sd for _ in range(4)],close=bar_close if bar_close is not None else np.full(4*len(self.sd),20000.))
        class D_:
            def __init__(s):s.bars=bars
            def signal_outcomes(s,sel,value_to):return book[sel['config']]
        sel_of=lambda k:dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5])
        return self.zpf.Account(D_(),self.sd,self.so,self.close,'2025-01-01',capital,buffer,90,margin or {},self.zpf.Sizing(sizing,650.,10,tu),sel_of,max_lots)
    key=staticmethod(lambda c,side='bull':(side,'naked',0.,c,'none','none'))
    def legs(self,*spec):return [(2024,n,self.key(c,'bull' if n=='green' else 'bear'),k,'2024-01-01','2025-01-01') for n,c,k in spec]
    def jan(self):return [i for i,d in enumerate(self.sd) if d.startswith('2024-01')]
    # ---- carry, sizing, pieces ----
    def test_T01_carry_on_futures_zone_and_futures_expiry(self):
        t=self.tr(0,10,100.,units=1.,marks=[0.]);days=(self.days[10]-self.days[0]).days
        for st in ('futures_zone','futures_expiry'):
            self.assertAlmostEqual(self.zpf.replay([t],self.so,st,.055)['E'][10],100-20000*.055/365*days,9)
            self.assertAlmostEqual(self.zpf.replay([dict(t,sign=-1.)],self.so,st,.055)['E'][10],100+20000*.055/365*days,9)
        self.assertEqual(self.zpf.replay([t],self.so,'naked',.055)['E'][10],100.)
    def test_AT03_exchange_lots_are_whole_contracts_of_the_day(self):
        tu=self.zpf.Sizing('exchange',650.,10,650.)                               # paths built with --target-units 650
        self.assertEqual([tu.units(dict(units=650.,lot=25),k) for k in (10,5,1)],[650.,325.,50.])             # 26 / 13 / 2 lots of 25
        self.assertEqual([tu.units(dict(units=650.,lot=50),k) for k in (10,5)],[650.,300.])                   # 13 / 6 lots of 50
        self.assertEqual([tu.units(dict(units=675.,lot=75),k) for k in (10,1)],[675.,0.])                     # 9 lots of 75; 1 of 10 is < 1 lot
        lo=self.zpf.Sizing('exchange',650.,10,None)                               # paths built with --lots 10
        self.assertEqual([lo.units(dict(units=250.,lot=25),k) for k in (10,1)],[250.,25.])
        x=dict(units=260.,lot=65);self.assertEqual([lo.units(x,k) for k in (1,2,4,5,10)],[65.,130.,260.,260.,260.])   # 4 of 10 matched: never more than matched
        for sz in (tu,lo):
            for lot in (25,50,65,75):
                for k in range(11):self.assertEqual(sz.units(dict(units=10*lot,lot=lot),k)%lot,0)
        self.assertAlmostEqual(self.zpf.Sizing('normalized',650.,10).units(dict(units=500.,lot=50),5),325.)
    def test_same_day_trade_holds_margin_and_lots(self):
        p=self.zpf.prepare([self.tr(5,5,10.,marks=[1.])],self.so,'naked',0.,self.close,.1);E,op,M,Lo=self.zpf.combine(p,10,self.zpf.Sizing('exchange',650.,10))
        self.assertEqual(M[5],.1*650*20000);self.assertEqual(Lo[5],10.);self.assertFalse(op[5]);self.assertEqual(E[5],6500.)
        p=self.zpf.prepare([self.tr(2,5,1.,marks=[1]),self.tr(5,7,1.,marks=[1])],self.so,'naked',0.,self.close,.1);_,_,M,Lo=self.zpf.combine(p,10,self.zpf.Sizing('exchange',650.,10))
        self.assertEqual(Lo[5],10.);self.assertEqual(M[5],.1*650*20000)                       # sequential trades of one strategy: not added
        with self.assertRaises(ValueError):self.zpf.prepare([self.tr(2,6,1.,marks=[1]),self.tr(4,8,1.,marks=[1])],self.so)
    def test_AT10_full_leap_year_counts_one(self):
        self.assertEqual(self.zpf.year_factor(2024,366),1.);self.assertEqual(self.zpf.year_factor(2023,365),1.);self.assertAlmostEqual(self.zpf.year_factor(2024,31),365/31)
        ys=self.zpf.year_slices(self.sd,'2023-01-01','2025-01-01');self.assertEqual([y[3] for y in ys],[1.,1.])
    def test_T06_no_judged_year_is_not_floored(self):
        G=np.cumsum(np.full((1,len(self.so)),-100.),1);R=np.zeros((1,len(self.so)))
        ys=self.zpf.year_slices(self.sd,'2022-12-01','2022-12-31');Y,tot,worst,dd=self.zpf.pair_metrics(G,R,ys[0][1],ys[-1][2],ys,2.5e6,ys[0][4]/365);self.assertTrue(np.isnan(worst[0,0]))
    # ---- training search ----
    def _folds(self,curves,margins=None,lots_open=None,sizes=(0,10)):
        ys=self.zpf.year_slices(self.sd,'2022-12-01','2025-01-01');T=len(self.so);margins=margins if margins is not None else [np.zeros(T)]*len(curves);lots_open=lots_open if lots_open is not None else [np.full(T,10.)]*len(curves)
        return {k:self.zpf.Fold([(c*k/10,np.ones(T,bool),m*k/10,l*k/10) for c,m,l in zip(curves,margins,lots_open)],ys[0][1],ys[-1][2],ys,90,T) for k in sizes},ys
    def test_combo_eval_matches_brute_force_including_funds_and_lots(self):
        rnd=np.random.default_rng(5);T=len(self.so);G=np.cumsum(rnd.normal(3000,20000,(3,T)),1);R=np.cumsum(rnd.normal(-500,15000,(2,T)),1);MG=rnd.uniform(5e5,2e6,(3,T));MR=rnd.uniform(5e5,2e6,(2,T))
        LG=rnd.integers(0,11,(3,T)).astype(float);LR=rnd.integers(0,11,(2,T)).astype(float)
        FG,ys=self._folds(list(G),list(MG),list(LG));FR,_=self._folds(list(R),list(MR),list(LR));i0,i1=ys[0][1],ys[-1][2];span=sum(y[4] for y in ys)/365
        g=np.array([0,1,2,3]);r=np.array([1,2,0,1]);tot,Y,worst,dd,mg,ex,lo=self.zpf.combo_eval(FG[10],FR[10],g,r,2.5e6,span,.1)
        Gz,Rz,Mz,Nz,Lz,Kz=(np.vstack([a,np.zeros(T)]) for a in (G,R,MG,MR,LG,LR))
        for k in range(4):
            e=Gz[g[k]]+Rz[r[k]];base=e[i0-1] if i0 else 0.;s=np.r_[0,e[i0:i1+1]-base];m=(Mz[g[k]]+Nz[r[k]])[i0:i1+1]
            self.assertAlmostEqual(dd[k],(np.maximum.accumulate(s)-s).max(),3);self.assertAlmostEqual(ex[k],(m-.9*(2.5e6+s[:-1])).max(),3)
            self.assertEqual(lo[k],(Lz[g[k]]+Kz[r[k]])[i0:i1+1].max())
    def test_AT01_training_rejects_combined_lots_above_cap(self):
        T=len(self.so);up=np.linspace(0,3e6,T);F,ys=self._folds([up]);span=sum(y[4] for y in ys)/365
        F,ys=self._folds([up],sizes=(0,5,10));rows,passed,cnt=self.zpf.search(F,F,[0,5,10],2.5e6,span,.01,-1.,1e12,'dd',50,0.,10.)
        self.assertTrue(passed);self.assertTrue(all(r['la']+r['lb']<=10 for r in rows));self.assertEqual(cnt['pass_lot_cap_of_target_and_floor'],5)   # (5,0) (10,0) (0,5) (0,10) (5,5)
    def test_A2_margin_judged_against_funds_after_losses(self):
        T=len(self.so);E=np.zeros(T);E[300:]=-300000.;M=np.zeros(T);M[301:]=2.4e6
        FG,ys=self._folds([E],[M]);span=sum(y[4] for y in ys)/365;*_,mg,ex,lo=self.zpf.combo_eval(FG[10],FG[0],[0],[1],2.5e6,span,0.);self.assertLess(mg[0],2.5e6);self.assertGreater(ex[0],0)
    def test_A3_no_false_none_passed_from_decoys(self):
        T=len(self.so);calm=np.linspace(0,.16*2.5e6*len(self.so)/252,T);wild=np.r_[np.linspace(0,3e7,T//2),np.linspace(3e7,0,T-T//2)]+np.linspace(0,1.2*2.5e6*3,T)
        F,ys=self._folds([wild]*50+[calm]);span=sum(y[4] for y in ys)/365;rows,passed,cnt=self.zpf.search(F,F,[0,10],2.5e6,span,.10,-1.,5e5,'dd',3)
        self.assertTrue(passed);self.assertEqual(rows[0]['g'] if rows[0]['la'] else rows[0]['r'],50);self.assertEqual(cnt['evaluated_exactly'],cnt['pass_target_and_floor'])
    def test_dispersion_rank_prefers_steady_years(self):
        tot=np.array([.22,.22]);worst=np.array([.21,.17]);dd=np.array([2e5,1e5]);Y=np.array([[.21,.22,.22,.23],[.17,.18,.18,.355]])
        k1,k2=self.zpf.rank_key('dispersion',tot,worst,dd,Y,[0,1,2,3]);self.assertEqual(int(np.lexsort((k2,k1))[0]),0)
    def test_A8_leg_lots_above_simulated_size_rejected(self):
        with self.assertRaises(ValueError):self.zpf.parse_lots('0,11,20',10)
    # ---- executable account ----
    def test_AT01_AT02_combined_lot_cap_in_the_account(self):
        j=self.jan();X=self.tr(j[2],j[10],1.,marks=[1.]*8,sb=4*j[2]-1);Y=self.tr(j[4],j[12],1.,marks=[1.]*8,sb=4*j[4]-1);Z=self.tr(j[11],j[15],1.,marks=[1.]*4,sb=4*j[11]-1)
        acc=self._acc({'X':[self.ev(X,j[2],4*j[10])],'Y':[self.ev(Y,j[4],4*j[12])],'Z':[self.ev(Z,j[11],4*j[15])]},max_lots=10.)
        A,M,opn,orders,Lo=acc.run(self.legs(('green','X',10),('red','Y',10)),0.)
        self.assertEqual([o['decision'] for o in orders],['taken','rejected: open lot cap reached']);self.assertLessEqual(Lo.max(),10.)
        A,M,opn,orders,Lo=acc.run(self.legs(('green','X',10),('red','Z',10)),0.)          # sequential: the cap is reused
        self.assertEqual([o['lots_taken'] for o in orders],[10,10])
    def test_AT04_AT05_intraday_overlap_reserves_margin_and_sequential_reuses_it(self):
        j=self.jan();d=j[5]
        G=self.tr(d,d,1.,marks=[1.],sb=4*d-1);R=self.tr(d,d,1.,marks=[1.],sb=4*d)            # green fills bar 4d, exits bar 4d+2; red fills bar 4d+1 (overlap)
        acc=self._acc({'G':[('trade',4*d-1,4*d+2,G)],'R':[('trade',4*d,4*d+3,R)]},buffer=.1,margin={'naked':.12})
        A,M,opn,orders,Lo=acc.run(self.legs(('green','G',10),('red','R',10)),0.)
        self.assertEqual(orders[1]['decision'],'cut to fit available funds');self.assertEqual(orders[1]['lots_taken'],4);self.assertGreater(M[d],0)   # (22.5 L - 15.6 L) / 1.56 L
        acc=self._acc({'G':[('trade',4*d-1,4*d,G)],'R':[('trade',4*d+1,4*d+3,R)]},buffer=.1,margin={'naked':.12})   # green out at bar 4d, red in at 4d+2
        A,M,opn,orders,Lo=acc.run(self.legs(('green','G',10),('red','R',10)),0.)
        self.assertEqual([o['lots_taken'] for o in orders],[10,10]);self.assertAlmostEqual(M[d],.12*650*20000,0)           # same day, not at once: the peak is one position
    def test_AT06_eventual_close_does_not_change_admission(self):
        j=self.jan();d=j[5];G=self.tr(d,d+3,1.,marks=[1.]*3);R=self.tr(d,d+3,1.,marks=[1.]*3)
        res=[]
        for later in (10000.,20000.,40000.):
            bc=np.full(4*len(self.sd),20000.);bc[4*d:4*d+4]=later                           # the entry day's bars after the decision
            acc=self._acc({'G':[('trade',4*d-1,4*d+8,G)],'R':[('trade',4*d-1,4*d+8,R)]},buffer=.1,margin={'naked':.12},bar_close=bc)
            res.append([o['lots_taken'] for o in acc.run(self.legs(('green','G',10),('red','R',10)),0.)[3]])
        self.assertEqual(res[0],res[1]);self.assertEqual(res[1],res[2])
    def test_AT07_later_margin_rise_uses_the_buffer_and_is_reported(self):
        j=self.jan();d=j[5];X=self.tr(d,d+10,1.,marks=[1.]*10);bc=np.full(4*len(self.sd),20000.);bc[4*(d+3):]=23000.
        acc=self._acc({'X':[('trade',4*d-1,4*(d+10),X)]},buffer=.1,margin={'naked':.17},bar_close=bc)      # 650 x 20,000 x 17% = 22.1 L at entry, 25.4 L later
        wf,daily,orders,s=self.zpf.walk_forward(acc,{2024:([dict(_g=self.key('X'),_r=None,green_lots=10,red_lots=0,passed=True)],True,'pass')},'cash',1,.17,0.,(0.,))
        self.assertEqual(orders[0]['lots_taken'],10);self.assertGreater(s['buffer_breach_sessions'],0);self.assertGreater(s['funds_short_sessions'],0);self.assertFalse(wf[0]['deployable'])
    def test_T03_T04_flat_adoption_and_continuous_drawdown(self):
        dec=[i for i,d in enumerate(self.sd) if d.startswith('2023-12')];j=self.jan()
        A=self.tr(dec[-6],dec[-1],-200000/650.,marks=[0.]*5+[-200000/650.]);Bpre=self.tr(dec[-10],j[3],+999.,marks=[999.]*5);B=self.tr(j[0],j[8],-200000/650.,marks=[-100000/650.]*3+[-200000/650.]*5)
        acc=self._acc({'A':[self.ev(A,dec[-6],4*dec[-1])],'B':[self.ev(Bpre,dec[-10],4*j[3]),self.ev(B,j[0],4*j[8])]})
        row=lambda c:dict(_g=self.key(c),_r=None,green_lots=10,red_lots=0,passed=True)
        wf,daily,orders,s=self.zpf.walk_forward(acc,{2023:([row('A')],True,'pass'),2024:([row('B')],True,'pass')},'cash',1,.17,0.,(0.,))
        self.assertAlmostEqual(s['daily_close_drawdown_continuous'],400000,-1);self.assertAlmostEqual(s['largest_in_year_drawdown'],200000,-1)
        self.assertAlmostEqual(wf[1]['account_return'],-200000/2.5e6,6)                      # full leap year: factor 1 (V4.9.3); Bpre not credited
    def test_A1_carried_position_and_new_choice_cannot_exceed_funds(self):
        dec=[i for i,d in enumerate(self.sd) if d.startswith('2023-12')];j=self.jan()
        X=self.tr(dec[-5],j[10],1.,marks=[1.]*15);Y=self.tr(j[2],j[20],1.,marks=[1.]*15)
        row=lambda c:dict(_g=self.key(c),_r=None,green_lots=10,red_lots=0,passed=True);picks={2023:([row('X')],True,'pass'),2024:([row('Y')],True,'pass')}
        acc=self._acc({'X':[self.ev(X,dec[-5],4*j[10])],'Y':[self.ev(Y,j[2],4*j[20])]},margin={'naked':.18})
        wf,daily,orders,s=self.zpf.walk_forward(acc,picks,'cash',1,.17,0.,(0.,))
        self.assertEqual([o['decision'] for o in orders],['taken','rejected: assumed margin does not fit available funds']);self.assertLessEqual(s['assumed_margin_peak'],2.5e6)
        acc=self._acc({'X':[self.ev(X,dec[-5],4*j[10])],'Y':[self.ev(Y,j[2],4*j[20])]},margin={'naked':.18},max_lots=10.)
        self.assertEqual(self.zpf.walk_forward(acc,picks,'cash',1,.17,0.,(0.,))[2][1]['decision'],'rejected: assumed margin does not fit available funds')
    def test_AT13_AT14_every_year_reported_and_carried_exposure_is_not_cash(self):
        dec=[i for i,d in enumerate(self.sd) if d.startswith('2023-12')];j=self.jan();X=self.tr(dec[-5],j[10],1.,marks=[1.]*15)
        row=lambda c,p=True:dict(_g=self.key(c),_r=None,green_lots=10,red_lots=0,passed=p)
        wf,_,_,s=self.zpf.walk_forward(self._acc({'X':[self.ev(X,dec[-5],4*j[10])]}),{2023:([row('X')],True,'pass'),2024:([],False,'no_candidates')},'cash',1,.17,0.,(0.,))
        self.assertEqual([w['test_year'] for w in wf],[2023,2024]);self.assertEqual(wf[1]['decision'],'no new entries: no_candidates');self.assertFalse(wf[1]['flat_cash_year']);self.assertEqual(s['no_new_entry_years'],1)
    def test_AT11_cagr_reported_beside_simple_return(self):
        T=len(self.so);A=np.zeros(T)
        for y in (2023,2024):A[[i for i,d in enumerate(self.sd) if d>=f'{y}-12-29'][0]:]+=550000.
        end=(self.days[-1]+dt.timedelta(1)).isoformat();acc=SimpleNamespace(days=self.sd,capital=2.5e6,buffer=.1,end=end);i0=[i for i,d in enumerate(self.sd) if d>='2023-01-01'][0]
        s,*_=self.zpf.account_summary(acc,A,np.zeros(T),np.zeros(T),{},[],[2023,2024],i0)
        span=(dt.date.fromisoformat(end)-dt.date(2023,1,1)).days;self.assertAlmostEqual(s['cagr_fixed_size_no_withdrawals'],(3.6e6/2.5e6)**(365/span)-1,6)
        self.assertAlmostEqual(s['overall_simple'],1.1e6/2.5e6/(span/365),6)
        self.assertLess(s['cagr_fixed_size_no_withdrawals'],.22)
    def test_AT12_training_pass_is_not_an_oos_pass(self):
        s=dict(years=[2022,2023,2024],overall_simple=.10,worst=.02,daily_close_drawdown_continuous=1e5,funds_short_sessions=0,every_year_deployable=True)
        v=self.zpf.verdict(s,[],.22,.17,2.5e5);self.assertEqual(v['oos_verdict'],'FAIL')
        self.assertEqual(self.zpf.verdict(dict(s,overall_simple=.25,worst=.18),[],.22,.17,2.5e5)['oos_verdict'],'PASS')
        self.assertEqual(self.zpf.verdict(dict(s,years=[2024]),[],.22,.17,2.5e5)['oos_verdict'],'INSUFFICIENT EVIDENCE')
    def test_AT20_identity_distinguishes_trades_with_equal_daily_pnl(self):
        T=5;arr=(np.zeros(T),np.zeros(T,bool),np.zeros(T),np.zeros(T))
        c=lambda sb:dict(prep=dict(pieces=[(0,1,np.zeros(1),1,0.,None,dict(signal_bar=sb,entry_day='2024-01-02',exit_day='2024-01-03',units=650.,lot=65,unit=0.),(0,2))]),at={10:arr})
        self.assertNotEqual(self.zpf._identity(c(5)),self.zpf._identity(c(6)));self.assertEqual(self.zpf._identity(c(5)),self.zpf._identity(c(5)))
    def test_gap_holds_leg_in_account(self):
        j=self.jan();Y=self.tr(j[3],j[6],5.,marks=[5.]*3)
        acc=self._acc({'Y':[('gap',4*j[1]-1,4*j[7],self.sd[j[1]]),('trade',4*j[3]-1,4*j[6],Y)]})
        wf,_,orders,_=self.zpf.walk_forward(acc,{2024:([dict(_g=self.key('Y'),_r=None,green_lots=10,red_lots=0,passed=True)],True,'pass')},'cash',1,.17,0.,(0.,))
        self.assertEqual(wf[0]['account_return'],0.);self.assertTrue(orders[0]['decision'].startswith('no fill (data gap'))

class GridV491(Market):
    def _grid(self):
        days=sessions('2024-01-02',12);lv=lambda d,mm:20000.+(80. if d>=days[3] else 0.)
        m=self.build(days=days,level=lv,extra=('2024-01-18',));bars=self.bars_for(days,lv);final=['G']*40+['R']*(len(bars)-40)
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+10,b.close-10,b.close) for b in bars]
        sig={'zone_only_tw2':{'bull':[0,10],'bear':[45]}}
        p=Path(tempfile.mkdtemp())/'p.sqlite';db=sqlite3.connect(p);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)])
        db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,s,t) for c,v in sig.items() for s in v for t in v[s]])
        sim=zp.PathSim(m,bars,final,pcfg());sim.run(zb.entry_map(bars,sig,'2024-01-01','2024-12-31'),('naked',),(0,),sink=zp.writer(db),progress=False);db.commit();db.close()
        return p,days,bars
    def test_T05_flat_start_ignores_signals_before_adoption(self):
        p,days,bars=self._grid();sel=dict(side='bull',structure='naked',offset=0.,config='zone_only_tw2',profit='none',stop='none')
        with zg.Detail(p) as D:
            lo=bars[10].day                                                       # signal 0's trade (to the zone flip at bar 40) still holds here
            self.assertEqual(len(D.trades(sel,lo,'2025-01-01')),0)                 # grid convention: signal 10 locked out
            t=D.trades(sel,lo,'2025-01-01',flat=True);self.assertEqual([x['signal_bar'] for x in t],[10])
            self.assertEqual(t[0]['lot'],LotMaster(None).get(t[0]['entry_day'],'2024-01-11').qty);self.assertGreaterEqual(t[0]['marks_n'],t[0]['stale'])
    def test_T08_portfolio_refuses_a_modified_grid_file(self):
        from nifty_iv.zone import portfolio as zpf
        p,_,_=self._grid();out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False,compound=False)
        f=sorted(out.glob('grid_*.npz'))[0];b=bytearray(f.read_bytes());b[-30]^=1;f.write_bytes(bytes(b))
        with self.assertRaises(SystemExit):zpf.main(['--grid',str(out),'--paths',str(p),'--out',str(out/'pf')])

class PortfolioV494(PortfolioV493):
    """V4.9.3 review: causal intraday funds, lot-cap units, CAGR-basis selection, elapsed-period overall return, evidence rule."""
    def test_AT493_01_02_same_day_realised_result_changes_funds(self):
        j=self.jan();d=j[5]
        G=self.tr(d,d,-1300000/650.,marks=[-1000000/650.],sb=4*d-1);R=self.tr(d,d+3,1.,marks=[1.]*3,sb=4*d+1)
        acc=self._acc({'G':[('trade',4*d-1,4*d,G)],'R':[('trade',4*d+1,4*d+12,R)]},buffer=.1,margin={'naked':.12})      # green fills bar 4d, last exit fill bar 4d+1; red decided at 4d+1
        A,M,opn,orders,Lo=acc.run(self.legs(('green','G',10),('red','R',10)),0.)
        self.assertEqual(orders[1]['funds_at_decision'],2500000-1300000)                                       # loss realised before the decision
        self.assertEqual(orders[1]['lots_taken'],int((.9*1.2e6)//(.12*65*20000)));self.assertEqual(orders[1]['decision'],'cut to fit available funds')
        G2=dict(G,unit=+400000/650.,marks=np.array([400000/650.]))
        acc=self._acc({'G':[('trade',4*d-1,4*d,G2)],'R':[('trade',4*d+1,4*d+12,R)]},buffer=.1,margin={'naked':.12})
        self.assertEqual(acc.run(self.legs(('green','G',10),('red','R',10)),0.)[3][1]['funds_at_decision'],2900000)
        G3=self.tr(d,d+2,-1300000/650.,marks=[-500000/650.]*12,sb=4*d-1)
        acc=self._acc({'G':[('trade',4*d-1,4*d+8,G3)],'R':[('trade',4*d+1,4*d+12,R)]},buffer=.1,margin={'naked':.12})
        self.assertEqual(acc.run(self.legs(('green','G',10),('red','R',10)),0.)[3][1]['funds_at_decision'],2000000)   # still open: marked at the decision bar
    def test_AT493_03_lot_cap_units(self):
        r=self.zpf.Sizing('exchange',650.,10,650.,'reference');e=self.zpf.Sizing('exchange',650.,10,650.,'exchange')
        for lot,full in ((25,26),(50,13),(65,10),(75,9)):
            x=dict(units=full*lot,lot=lot);u=r.units(x,10);self.assertEqual(u,full*lot);self.assertAlmostEqual(r.cap_lots(x,u),10.);self.assertAlmostEqual(e.cap_lots(x,u),full)
    def test_AT493_04_cagr_basis_controls_selection(self):
        T=len(self.so);ys=self.zpf.year_slices(self.sd,'2023-01-01','2025-01-01');span=sum(y[4] for y in ys)/365
        cur=lambda rate:np.r_[np.zeros(ys[0][1]),np.linspace(0,rate*2.5e6*span,T-ys[0][1])]
        a=cur(.235);b=cur(.31);b[ys[0][1]+50:ys[0][1]+52]-=50000.                                         # A: simple ~22.3% (CAGR ~20%), no drawdown; B: simple ~29% (CAGR ~26%), a dip
        F={k:self.zpf.Fold([(c*k/10,np.ones(T,bool),np.zeros(T),np.full(T,10.)*k/10) for c in (a,b)],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)}
        E={k:self.zpf.Fold([],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)}                                    # no red candidates
        rows,passed,_=self.zpf.search(F,E,[0,10],2.5e6,span,.22,.17,1e6,'dd',5);self.assertEqual(rows[0]['g'],0)        # simple basis: A wins on drawdown
        rows,passed,_=self.zpf.search(F,E,[0,10],2.5e6,span,.22,.17,1e6,'dd',5,basis='cagr')
        self.assertTrue(passed);self.assertEqual([r['g'] for r in rows],[1])                                           # cagr basis: A no longer qualifies
        self.assertAlmostEqual(self.zpf.simple_target(.22,4,'cagr'),(1.22**4-1)/4,12)
    def test_AT493_05_overall_is_period_pnl_over_elapsed_years(self):
        days=[d for d in (dt.date(2025,1,1)+dt.timedelta(i) for i in range(546)) if d.weekday()<5];sd=[d.isoformat() for d in days];T=len(sd)
        A=np.zeros(T);A[[i for i,d in enumerate(sd) if d>='2025-12-31'][0]:]+=.17*2.5e6;A[-1]+=.135*2.5e6           # 17% in 2025, 13.5% in H1 2026
        acc=SimpleNamespace(days=sd,capital=2.5e6,buffer=.1,end='2026-07-01');s,*_=self.zpf.account_summary(acc,A,np.zeros(T),np.zeros(T),{},[],[2025,2026],0,start_date='2025-01-01')
        self.assertAlmostEqual(s['overall_simple'],.305/(546/365),6);self.assertLess(s['overall_simple'],.22)
        v=self.zpf.verdict(dict(s,years=[2025,2026],worst=.17,funds_short_sessions=0,every_year_deployable=True),[],.22,.17,1e9);self.assertEqual(v['oos_verdict'],'FAIL')
    def test_AT493_07_one_judged_year_is_insufficient_unless_preregistered(self):
        s=dict(years=[2025],overall_simple=.30,worst=.30,daily_close_drawdown_continuous=0,funds_short_sessions=0,every_year_deployable=True)
        self.assertEqual(self.zpf.verdict(s,[],.22,.17,2.5e5)['oos_verdict'],'INSUFFICIENT EVIDENCE');self.assertEqual(self.zpf.verdict(s,[],.22,.17,2.5e5,min_full_years=1)['oos_verdict'],'PASS')
    def test_AT493_08_holdout_code_is_in_the_digest(self):
        f=Path(self.zpf.__file__).with_name('holdout.py');orig=f.read_bytes();d0=self.zpf.code_digest()
        try:f.write_bytes(orig+b'\n# probe\n');self.assertNotEqual(self.zpf.code_digest(),d0)
        finally:f.write_bytes(orig)
    def test_AT493_09_coverage_problems(self):
        from nifty_iv.zone import holdout as zh
        self.assertEqual(zh.coverage_problems(['2025-01-01','2025-01-02'],'2025-01-01','2025-01-03'),[])
        self.assertTrue(zh.coverage_problems(['2025-01-01','2025-02-01'],'2025-01-01','2025-02-02'))
        self.assertTrue(zh.coverage_problems(['2025-01-01'],'2025-01-01','2025-03-01'))

class PortfolioV495(PortfolioV493):
    """V4.9.4 review: executable qualification, holdout completeness, exposure snapshot, horizon end."""
    def test_A494_01_qualification_replays_the_executable_account(self):
        j=self.jan();d=j[5]
        G=self.tr(d,d,-1000000/650.,marks=[-1000000/650.],sb=4*d-1);R=self.tr(d,d+40,600000/650.,marks=[600000/650.]*40,sb=4*d+1)
        acc=self._acc({'G':[('trade',4*d-1,4*d,G)],'R':[('trade',4*d+1,4*d+200,R)]},buffer=.1,margin={'naked':.154})     # 650 x 20,000 x 15.4% = 20.0 L at full size
        row=dict(_g=self.key('G'),_r=self.key('R','bear'),green_lots=10,red_lots=10)
        q=self.zpf.qualify(acc,row,'2024-01-01','2025-01-01',2.5e6,-1.,-1.,1e9,'simple',90,0.)
        A,M,opn,orders,Lo=acc.run(self.legs(('green','G',10),('red','R',10)),0.)
        self.assertEqual(orders[1]['decision'],'cut to fit available funds');self.assertLess(orders[1]['lots_taken'],10)       # 13.5 L of funds for margin: 6 of 10 lots
        q2=self.zpf.qualify(acc,row,'2024-01-01','2025-01-01',2.5e6,(600000*.6-1000000)/2.5e6+.01,-1.,1e9,'simple',90,0.)
        self.assertFalse(q2['executable_pass']);self.assertEqual(q2['executable_orders_cut'],1)                              # at full size it would have passed; cut, it does not
    def test_A494_03_04_main_exposure_not_overwritten_by_alternatives(self):
        j=self.jan();X=self.tr(j[2],j[10],1.,marks=[1.]*8);Y=self.tr(j[2],j[10],1.,marks=[1.]*8)
        acc=self._acc({'X':[self.ev(X,j[2],4*j[10])],'Y':[self.ev(Y,j[2],4*j[10])]},max_lots=10.)
        rows=[dict(_g=self.key('X'),_r=None,green_lots=1,red_lots=0,passed=True),dict(_g=self.key('Y'),_r=None,green_lots=6,red_lots=0,passed=True)]
        for top in (1,2):
            wf,daily,orders,s=self.zpf.walk_forward(acc,{2024:(rows,True,'pass')},'cash',top,.17,0.,(0.,))
            self.assertEqual(s['exchange_lots_peak'],1.);self.assertEqual(max(r['exchange_lots'] for r in daily),1.)
    def test_A494_06_full_year_ending_on_friday_counts_365_days(self):
        days=[d for d in (dt.date(2023,1,1)+dt.timedelta(i) for i in range(365)) if d.weekday()<5];sd=[d.isoformat() for d in days];self.assertEqual(sd[-1],'2023-12-29')
        ys=self.zpf.year_slices(sd,'2023-01-01','2024-01-01');self.assertEqual((ys[0][3],ys[0][4]),(1.,365))
        T=len(sd);A=np.zeros(T);A[-1]=550000.;acc=SimpleNamespace(days=sd,capital=2.5e6,buffer=.1,end=self.zpf.horizon_end(sd,'2024-01-01').isoformat())
        s,*_=self.zpf.account_summary(acc,A,np.zeros(T),np.zeros(T),{},[],[2023],0,start_date='2023-01-01');self.assertAlmostEqual(s['overall_simple'],.22,9)
        self.assertEqual(self.zpf.horizon_end(sd[:200],'2024-01-01'),dt.date.fromisoformat(sd[199])+dt.timedelta(1))       # data stops early: no padding
    def test_A494_07_08_holdout_completeness(self):
        from nifty_iv.zone import holdout as zh
        wk=[(dt.date(2025,1,1)+dt.timedelta(i)).isoformat() for i in range(90) if (dt.date(2025,1,1)+dt.timedelta(i)).weekday()<5]
        self.assertTrue(zh.coverage_problems(wk[::2],'2025-01-01','2025-04-01',holidays=[]))                                # half the weekdays gone: caught
        self.assertTrue(zh.coverage_problems(wk[::2],'2025-01-01','2025-04-01'))                                           # weak check catches it too
        self.assertEqual(zh.coverage_problems([x for x in wk if x!='2025-01-14'],'2025-01-01','2025-04-01',holidays=['2025-01-14']),[])   # a holiday is not missing
        self.assertTrue(zh.coverage_problems([x for x in wk if x!='2025-01-15'],'2025-01-01','2025-04-01',holidays=['2025-01-14']))       # one missing session caught
        bpd={x:9 for x in wk};bpd[wk[10]]=3;self.assertTrue(zh.coverage_problems(wk,'2025-01-01','2025-04-01',holidays=[],bars_per_day=bpd))   # thin session caught

class HoldoutV493(GridV491):
    def test_AT17_holdout_runs_once_and_refuses_changes(self):
        from nifty_iv.zone import portfolio as zpf;from nifty_iv.zone import holdout as zh
        with self.assertRaises(ValueError):zg.run_grid('x','y','2023-01-01','2025-06-01')          # the API refuses a training split inside the holdout
        p,days,bars=self._grid();d=Path(tempfile.mkdtemp());old=zg.HOLDOUT_START;zg.HOLDOUT_START=days[0]
        try:
            end=(dt.date.fromisoformat(days[-1])+dt.timedelta(1)).isoformat()
            F=dict(selection=[dict(side='bull',structure='naked',offset=0.,config='zone_only_tw2',profit='none',stop='none',lots=1)],params=dict(capital=2.5e6,target=.22,floor=.17,max_dd=2.5e5,sizing='exchange',
                   ref_units=650.,margin={},buffer=.1,max_lots=10.,carry=.055,min_days=90,return_basis='simple',gap_policy='busy',holidays=[],holdout_start=days[0],holdout_end=end,holdout_min_full_years=1),
                   paths_sha256=zg.digest(p),code_sha256=zpf.code_digest())
            (d/'portfolio_freeze.json').write_text(json.dumps(F))
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'h0',progress=False)    # no holiday list: exact coverage cannot be checked
            rep=zh.run(d/'portfolio_freeze.json',p,d/'h1',progress=False,weak_coverage=True);self.assertEqual(rep['holdout']['coverage_check'],'weak (no holiday list)');self.assertFalse(rep['repeat']);self.assertEqual(rep['oos_verdict'],'INSUFFICIENT EVIDENCE')   # 12 sessions: no judged year
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'hx',to='2025-01-01',acknowledge_repeat=True,progress=False,weak_coverage=True)   # horizon is frozen
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'h2',progress=False,weak_coverage=True)       # second look refused
            self.assertTrue(zh.run(d/'portfolio_freeze.json',p,d/'h3',acknowledge_repeat=True,progress=False,weak_coverage=True)['repeat'])
            (d/'portfolio_freeze.json').write_text(json.dumps(dict(F,params=dict(F['params'],holdout_end='2024-03-01'))))
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'h6',progress=False,weak_coverage=True)       # AT493-09: sessions stop short of the frozen end
            (d/'portfolio_freeze.json').write_text(json.dumps(dict(F,paths_sha256='0'*64)))
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'h4',progress=False,weak_coverage=True)       # data changed
            (d/'portfolio_freeze.json').write_text(json.dumps(dict(F,code_sha256='0'*64)))
            with self.assertRaises(SystemExit):zh.run(d/'portfolio_freeze.json',p,d/'h5',progress=False,weak_coverage=True)       # code changed
        finally:zg.HOLDOUT_START=old

class SequenceParity(unittest.TestCase):
    def test_numba_kernel_matches_numpy(self):
        rnd=np.random.default_rng(11);C,R,Np=23,17,40;bars_=np.sort(rnd.choice(np.arange(500),Np,replace=False));idx={int(t):i for i,t in enumerate(bars_)}
        XB=bars_[:,None]+rnd.integers(1,60,(Np,R));wins=zg.windows('2021-09-01','2025-01-01');his=sorted({w[2] for w in wins});hix={h:i for i,h in enumerate(his)}
        VH=rnd.normal(0,50,(len(his),Np,R));VH[rnd.random(VH.shape)<.05]=np.nan;ed=[f'{2021+int(t)*4//500}-0{1+int(t)%9}-15' for t in bars_]
        by=collections.defaultdict(list)
        for c in range(C):
            for t in rnd.choice(np.r_[bars_,[3,7]],15,replace=False):by[int(t)].append(c)
        by={t:np.array(v) for t,v in by.items()}
        VH[rnd.random(VH.shape)<.02]=np.inf;VH[rnd.random(VH.shape)<.02]=-np.inf;VH[rnd.random(VH.shape)<.01]=1e150   # V4.9.2: infinities are not values
        for gaps in (None,{3:120,7:40}):
            a=zg._sequence_all_numpy(by,idx,XB,VH,ed,wins,hix,C,R,gaps);b=zg.sequence_all(by,idx,XB,VH,ed,wins,hix,C,R,gaps)
            for x,y in zip(a,b):self.assertTrue(np.array_equal(x,y))
    def test_gap_holds_the_slot(self):
        wins=zg.windows('2021-09-01','2025-01-01');his=sorted({w[2] for w in wins});hix={h:i for i,h in enumerate(his)}
        XB=np.array([[20]]);VH=np.full((len(his),1,1),5.);by={0:np.array([0]),5:np.array([0])};idx={5:0};ed=['2022-03-01']
        for f in (zg._sequence_all_numpy,zg.sequence_all):
            self.assertEqual(int(f(by,idx,XB,VH,ed,wins,hix,1,1)[0].sum()),4)                 # free: the trade at 5 counts (4 windows hold its entry)
            self.assertEqual(int(f(by,idx,XB,VH,ed,wins,hix,1,1,{0:10})[0].sum()),0)          # busy: the gap at 0 holds the slot to bar 10
    def test_gap_occupancy_to_zone_change(self):
        db=sqlite3.connect(':memory:');db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars(idx,day,zone) VALUES(?,?,?)',[(i,'2024-01-02',z) for i,z in enumerate('GGGRRG')])
        db.executemany("INSERT INTO paths(side,structure,offset,signal_bar,status,reason) VALUES('bull','calendar',0,?,'no_fill',?)",[(0,'no_long_expiry'),(1,'no_liquid_entry'),(5,'not_extracted')])
        self.assertEqual(zg.gap_occupancy(db,'bull','calendar',0,'busy'),{0:4,5:10**9});self.assertEqual(zg.gap_occupancy(db,'bull','calendar',0,'free'),{})

class TenorCoverage(unittest.TestCase):
    def test_h1_h2_from_listing(self):
        from nifty_iv.zone import coverage as zc
        listing={'2024-01-02':{'2024-01-04','2024-01-25','2024-02-29'},'2024-01-08':{'2024-01-11','2024-01-25'},'2024-01-15':{'2024-01-18','2024-01-25','2024-02-29'}}
        r={x['year']:x for x in zc.tenor_coverage(listing)}['2024']
        self.assertEqual((r['days'],r['h1_policy_eligible'],r['h1_listed'],r['h2_listed']),(3,1,1,2))   # 2 Jan: 23 days to 25 Jan; 8 Jan: 17 days; Feb monthly listed on 2 and 15 Jan


class StrategySpecV496(unittest.TestCase):
    """User decisions of 2026-11-12: margin table (upper end), side-aware ITM / OTM levels with an index-level step, monthly expiry policies."""
    def test_margin_table_upper_end_and_lots_in_budget(self):
        from nifty_iv.core import strategy_spec as ss
        self.assertEqual(ss.margin_per_lot('2022-06-01',50),115000.);self.assertEqual(ss.margin_per_lot('2024-06-03',25),80000.)
        self.assertAlmostEqual(ss.margin_per_lot('2025-03-03',75),250000.);self.assertAlmostEqual(ss.margin_per_lot('2026-02-02',65),200000.)
        self.assertEqual([ss.max_lots(d,l,2.5e6) for d,l in (('2022-06-01',50),('2024-06-03',25),('2025-03-03',75),('2026-02-02',65))],[21,31,10,12])
        self.assertAlmostEqual(ss.margin_per_lot('2024-12-10',25),25*250000/75)     # a lot-25 contract in the lot-75 period: per unit
        with self.assertRaises(ValueError):ss.margin_row('2003-12-31')
    def test_levels_are_side_aware_with_index_step(self):
        from nifty_iv.core import strategy_spec as ss
        self.assertEqual([ss.distance_step(x) for x in (4999,5000,11999,12000)],[50,100,100,200])
        self.assertEqual([ss.strike_for('bull',20040,l) for l in (5,1,0,-1,-5)],[19000.,19800.,20000.,20200.,21000.])   # puts: OTM below, ITM above
        self.assertEqual([ss.strike_for('bear',20040,l) for l in (5,1,0,-1,-5)],[21000.,20200.,20000.,19800.,19000.])   # calls: OTM above, ITM below
        self.assertEqual([ss.strike_for('bull',8030,l) for l in (1,-2)],[7900.,8200.]);self.assertEqual(ss.strike_for('bear',4030,3),4200.)   # steps 100 and 50 (ATM to 50)
        self.assertEqual(ss.LEVELS,tuple(range(-5,6)))
    def test_expiry_policies(self):
        from nifty_iv.core import strategy_spec as ss
        exps=['2024-01-04','2024-01-11','2024-01-18','2024-01-25','2024-02-29','2024-03-28']
        self.assertEqual(ss.select_short_expiry('naked','2024-01-02',exps),('2024-01-04',None) if ss.days_between('2024-01-02','2024-01-04')>=4 else ('2024-01-11',None))
        self.assertEqual(ss.select_short_expiry('naked_m1','2024-01-02',exps),('2024-01-25',None))                       # 23 days >= 21
        self.assertEqual(ss.select_short_expiry('naked_m1','2024-01-05',exps),(None,'policy_ineligible'))              # 20 days: skipped, never next month
        self.assertEqual(ss.select_short_expiry('naked_m1','2024-01-04',exps),('2024-01-25',None))                      # exactly 21 days
        self.assertEqual(ss.select_short_expiry('naked_m2','2024-01-30',exps),('2024-02-29',None))                      # February monthly, not a weekly
        self.assertEqual(ss.select_short_expiry('naked_m2','2024-01-02',['2024-01-25']),(None,'no_short_expiry'))       # not listed: a data gap
        self.assertNotIn('policy_ineligible',zg.GAP_REASONS)                                                             # a deliberate skip never holds the slot

class PathsV496(Market):
    def test_levels_and_monthly_policies_in_the_path_engine(self):
        m=self.build(extra=('2024-01-25','2024-02-29'));bars=self.bars_for(self.days);cfg=pcfg(offset_mode='levels',target_margin=2.5e6)
        sim=zp.PathSim(m,bars,['G']*len(bars),cfg)
        pair,_=sim.open_pair(0,'bull','naked',-1);self.assertEqual(pair['held'][0][0][1],20200.)                          # ITM put
        pair,_=sim.open_pair(0,'bull','naked_m1',1);self.assertEqual((pair['held'][0][0][0],pair['held'][0][0][1]),('2024-01-25',19800.))
        pair,_=sim.open_pair(0,'bull','naked_m2',0);self.assertEqual(pair['held'][0][0][0],'2024-02-29')
        lot=LotMaster(None).get(self.days[0],'2024-01-25').qty;self.assertEqual(sim.lots_for(lot,self.days[0]),__import__('nifty_iv.core.strategy_spec',fromlist=['_']).max_lots(self.days[0],lot,2.5e6))

class PortfolioV496(PortfolioV493):
    """V4.9.5 review: exhaustive executable qualification and executable ranking, complete-year evidence, coverage, contract checks."""
    def test_A495_01_search_returns_every_screen_pass(self):
        T=len(self.so);curves=[np.linspace(0,(.3+i*1e-4)*2.5e6*2,T) for i in range(205)]
        F,ys=self._folds(curves);E={k:self.zpf.Fold([],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)};span=sum(y[4] for y in ys)/365
        rows,passed,cnt=self.zpf.search(F,E,[0,10],2.5e6,span,.05,-1.,1e12,'dd',None);self.assertEqual(len(rows),205);self.assertEqual(cnt['distinct_pairs_passing'],205)
    def test_A495_02_executable_metrics_decide_the_rank(self):
        a=dict(executable_drawdown=230000,executable_overall=.25,executable_worst_year=.2,executable_dispersion=.02);b=dict(executable_drawdown=160000,executable_overall=.23,executable_worst_year=.18,executable_dispersion=.01)
        k1,k2=self.zpf._exec_rank('dd',[a,b]);self.assertEqual(min(range(2),key=lambda i:(k1[i],k2[i])),1)
        k1,k2=self.zpf._exec_rank('worst_year',[a,b]);self.assertEqual(min(range(2),key=lambda i:(k1[i],k2[i])),0)
    def test_A495_04_part_year_is_not_a_full_year(self):
        s=dict(years=[2026],full_years=[],overall_simple=.25,worst=.2,daily_close_drawdown_continuous=1e4,funds_short_sessions=0,every_year_deployable=True)
        self.assertEqual(self.zpf.verdict(s,[],.22,.17,2.5e5,min_full_years=1)['oos_verdict'],'INSUFFICIENT EVIDENCE')
        self.assertEqual(self.zpf.verdict(dict(s,years=[2025,2026],full_years=[2025]),[],.22,.17,2.5e5,min_full_years=1)['oos_verdict'],'PASS')
        self.assertTrue(self.zpf.is_full_year(2024,366));self.assertFalse(self.zpf.is_full_year(2024,365));self.assertFalse(self.zpf.is_full_year(2026,95))
    def test_A495_05_06_expected_bars_per_session(self):
        from nifty_iv.zone import holdout as zh
        wk=[(dt.date(2025,1,1)+dt.timedelta(i)).isoformat() for i in range(31) if (dt.date(2025,1,1)+dt.timedelta(i)).weekday()<5]
        full={d:set(zh.EXPECTED_BAR_ENDS) for d in wk};three={d:set(zh.EXPECTED_BAR_ENDS[:3]) for d in wk}
        self.assertEqual(zh.coverage_problems(wk,'2025-01-01','2025-02-01',holidays=[],bar_ends=full),[])
        self.assertTrue(zh.coverage_problems(wk,'2025-01-01','2025-02-01',holidays=[],bar_ends=three))                  # every session equally truncated: caught
        one=dict(full);one[wk[4]]=set(zh.EXPECTED_BAR_ENDS[:-1]);self.assertTrue(zh.coverage_problems(wk,'2025-01-01','2025-02-01',holidays=[],bar_ends=one))
        self.assertEqual(zh.coverage_problems(wk,'2025-01-01','2025-02-01',holidays=[],bar_ends=one,short_sessions=[wk[4]]),[])   # a declared short session
        self.assertEqual(len(zh.EXPECTED_BAR_ENDS),9)
    def test_A495_07_missing_final_week_is_not_imputed(self):
        days=[d.isoformat() for d in (dt.date(2023,1,1)+dt.timedelta(i) for i in range(365)) if d.weekday()<5]
        self.assertEqual(self.zpf.horizon_end(days,'2024-01-01',holidays=()),dt.date(2024,1,1))
        cut=[d for d in days if d<'2023-12-26']                                                                          # last week missing
        self.assertEqual(self.zpf.horizon_end(cut,'2024-01-01',holidays=()),dt.date(2023,12,26))   # data ends Mon 25 Dec: the period ends there
        self.assertEqual(self.zpf.horizon_end(cut,'2024-01-01',holidays=['2023-12-26','2023-12-27','2023-12-28','2023-12-29']),dt.date(2024,1,1))
    def test_A495_12_trade_beyond_window_needs_a_window_value(self):
        t=self.tr(len(self.sd)-3,len(self.sd)-1,7.,marks=[1,2]);so=self.so[:-1]
        with self.assertRaises(ValueError):self.zpf.prepare([dict(t,hypothetical=False)],so)
        self.assertEqual(self.zpf.combine(self.zpf.prepare([dict(t,hypothetical=True)],so),10,self.zpf.Sizing('exchange',650.,10))[0][-1],7*650)
    def test_margin_table_model(self):
        mm=self.zpf.MarginModel('table');d=['2022-06-01','2025-03-03'];v=mm.per_unit('naked',d,[0,0])
        self.assertAlmostEqual(v[0],2300.);self.assertAlmostEqual(v[1],250000/75);self.assertAlmostEqual(self.zpf.MarginModel('table',calendar_factor=.5).per_unit('calendar',d,[0,0])[0],1150.)

class StrategySpecV497(unittest.TestCase):
    """V4.9.7 (user): one entry = ceil(40% of the margin-table maximum lots)."""
    def test_entry_lots_is_forty_percent_rounded_up(self):
        from nifty_iv.core import strategy_spec as ss
        for day,lot,mx,el in (('2026-03-02',65,12,5),('2025-03-03',75,10,4),('2022-06-01',50,21,9),('2024-06-03',25,31,13)):
            self.assertEqual(ss.max_lots(day,lot,2.5e6),mx);self.assertEqual(ss.entry_lots(day,lot,2.5e6),el);self.assertEqual(ss.entry_lots(day,lot,2.5e6,1.),mx)
        self.assertEqual(ss.entry_lots('2025-03-03',75,1e5),1)                                   # never below one lot
    def test_paths_and_portfolio_use_the_same_rule(self):
        from nifty_iv.zone import portfolio as zpf;from nifty_iv.core import strategy_spec as ss
        sim=zp.PathSim.__new__(zp.PathSim);sim.cfg=pcfg(target_margin=2.5e6,target_fraction=.4)
        sz=zpf.Sizing('exchange',650.,10,None,'reference',2.5e6,.4)
        for day,lot in (('2026-03-02',65),('2025-03-03',75),('2022-06-01',50)):
            self.assertEqual(sim.lots_for(lot,day),sz.target(dict(lot=lot,entry_day=day)));self.assertEqual(sz.target(dict(lot=lot,entry_day=day)),ss.entry_lots(day,lot,2.5e6))

class PathsV497(Market):
    """V4.9.7 (user): entries may use 3 windows, 20% participation, full size or nothing; rows start after the completing window."""
    def _sim(self,final,lots,bars_n=3):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        m=self.edit('UPDATE opt_1m SET volume=?',(lot*10,))                                     # 10% participation: one lot a minute, 45 a window
        bars=self.bars_for(self.days);fin=final(len(bars))
        return zp.PathSim(m,bars,fin,pcfg(lots=lots,entry_bars=bars_n)),bars,lot
    def test_entry_completes_in_the_third_window_and_rows_start_after_it(self):
        sim,bars,lot=self._sim(lambda n:['G']*n,100);out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        p=out[0];self.assertEqual((p['status'],p['units'],p['first_bar']),('closed',100*lot,3))
        self.assertEqual(len(p['rows']['cash_total']),p['end_bar']-p['first_bar']+1);self.assertEqual(sim.counts['entry_completed_in_window_3'],1)
    def test_short_after_the_last_window_is_nothing_and_holds_the_slot_to_it(self):
        sim,bars,lot=self._sim(lambda n:['G']*n,200);out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        self.assertEqual((out[0]['status'],out[0]['reason'],out[0]['end_bar']),('no_fill','no_liquid_entry',2))   # 135 of 200 lots in three windows
    def test_zone_change_during_the_entry_cancels_it(self):
        sim,bars,lot=self._sim(lambda n:['G','R']+['G']*(n-2),100);out=[];sim.run({0:['bull']},('naked',),(0,),sink=out.extend,progress=False)
        self.assertEqual((out[0]['status'],out[0]['reason'],out[0]['end_bar']),('no_fill','zone_flip_before_fill',1))
    def test_grid_reads_first_bar_occupancy_and_fill_stats(self):
        db=sqlite3.connect(':memory:');db.executescript(zp.DDL)
        for lots,final in ((100,lambda n:['G']*n),):
            sim,bars,lot=self._sim(final,lots)
            db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,'G') for i,b in enumerate(bars)])
            sim.run({0:['bull']},('naked',),(0,),sink=zp.writer(db),progress=False)
        sim2,_,_=self._sim(lambda n:['G']*n,200);sim2.run({5:['bull']},('naked',),(0,),sink=zp.writer(db),progress=False)
        paths=zg.load_paths(db,'bull','naked',0.);self.assertEqual(paths[0]['first_bar'],3);self.assertIsNone(paths[5])
        B=zru.Batch([paths[0]],zru.BarArrays.from_db(db),'naked');self.assertEqual(int(B.K[0,0]),3)               # row 0 is bar 3
        self.assertEqual(zg.gap_occupancy(db,'bull','naked',0.,'free'),{5:7})                                     # attempt windows of bars 5..7
        st=zg.fill_stats(db,'bull','naked',0.);self.assertEqual((st['filled'],st['entry_not_completed'],st['fill_rate']),(1,1,.5))
        self.assertFalse(zg.testable(st));self.assertTrue(zg.testable(dict(fill_rate=.8)));self.assertTrue(zg.testable({}))

class GridV497(unittest.TestCase):
    """V4.9.7 (user): >= 3 full training years before a walk-forward year; band (every full year positive, worst >= 70% of the mean)."""
    def test_plan_windows(self):
        W=zg.plan_windows(['2022','2023','2024'],'2025-01-01',15)
        self.assertEqual((W['full_years'],W['wf_years'],W['loyo_years']),(['2022','2023','2024'],[],['2022','2023','2024']))
        self.assertEqual([w for w,_ in W['train']],['train_full','loyo_2022','loyo_2023','loyo_2024']);self.assertEqual(W['band']['loyo_2023'],['2022','2024'])
        W=zg.plan_windows(['2022','2023','2024','2025'],'2026-01-01',15);self.assertEqual(W['wf_years'],['2025']);self.assertEqual(W['band']['train_2025'],['2022','2023','2024'])
        W=zg.plan_windows(['2023','2024'],'2025-01-01',15);self.assertEqual((W['wf_years'],W['loyo_years']),([],[]))
    def test_band_mask_and_loyo_sums(self):
        wn=['train_full','test_2022','test_2023','test_2024'];t=np.zeros((4,4,1,1))
        t[0,1:,0,0]=(10,10,10);t[1,1:,0,0]=(10,10,5.9);t[2,1:,0,0]=(10,10,6.2);t[3,1:,0,0]=(20,-1,25)   # worst / mean: 1, 0.683, 0.705, a negative year
        Z=dict(total=t.transpose(0,1,2,3),n=np.ones((4,4,1,1),np.int16),wins=np.ones((4,4,1,1),np.int16),sumsq=np.ones((4,4,1,1)))
        B=zg.band_mask(Z,wn,['2022','2023','2024'],.70)[:,0,0];self.assertEqual(list(B),[True,False,True,False])
        N,T,_,_=zg.window_arrays(Z,wn,'loyo_2023',['2022','2023','2024']);self.assertEqual((int(N[0,0,0]),float(T[3,0,0])),(2,45.))

class PortfolioV497(PortfolioV493):
    def test_band_gate_in_the_search(self):
        T=len(self.so);idx=np.arange(T);y23=np.array([d[:4]=='2023' for d in self.sd]);y24=np.array([d[:4]=='2024' for d in self.sd])
        step=lambda a,b:np.cumsum(np.where(y23,a,0.)+np.where(y24,b,0.))
        even=step(.20*2.5e6/y23.sum(),.20*2.5e6/y24.sum());skew=step(.40*2.5e6/y23.sum(),.05*2.5e6/y24.sum())
        F,ys=self._folds([even,skew]);E={k:self.zpf.Fold([],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)};span=sum(y[4] for y in ys)/365
        _,_,c0=self.zpf.search(F,E,[0,10],2.5e6,span,.05,-1.,1e12,'dd',None);self.assertEqual(c0['pass_all'],2)
        rows,_,c1=self.zpf.search(F,E,[0,10],2.5e6,span,.05,-1.,1e12,'dd',None,band=.7);self.assertEqual((c1['pass_all'],c1['pass_band']),(1,1))
        self.assertEqual(rows[0]['g'],0)                                                                # the even curve survives
    def test_account_marks_from_the_first_row(self):
        j=self.jan();X=self.tr(j[3],j[6],5.,marks=[1.,2.,3.]);X['first_bar']=4*j[3]+2
        acc=self._acc({'X':[('trade',4*j[3]-1,4*j[6],X)]})
        wf,daily,orders,s=self.zpf.walk_forward(acc,{2024:([dict(_g=self.key('X'),_r=None,green_lots=10,red_lots=0,passed=True)],True,'pass')},'cash',1,.17,0.,(0.,))
        self.assertEqual(orders[0]['decision'],'taken')

class CoverageV497(unittest.TestCase):
    def test_moneyness_from_strike_and_atm(self):
        from nifty_iv.zone import coverage as zc
        self.assertEqual(zc.moneyness('PE',20200-20000),'ITM200');self.assertEqual(zc.moneyness('PE',19800-20000),'OTM200')
        self.assertEqual(zc.moneyness('CE',19800-20000),'ITM200');self.assertEqual(zc.moneyness('CE',0),'ATM')

class SelectionDryRunV497(unittest.TestCase):
    """Dry run of the V4.9.7 selection on a synthetic grid directory with three full years: band, leave-one-year-out, fill gate."""
    def _grid_dir(self):
        from nifty_iv.zone import rules as zr
        out=Path(tempfile.mkdtemp());start,split='2021-09-01','2025-01-01';wins=zg.windows(start,split);wn=[w[0] for w in wins]
        configs=[f'N3_k{k}_m8_j0.5_tw2' for k in ('0.25','0.5','0.75','1.0','1.25')];P=zr.profit_rules('naked');S=zg.stop_chunks(zr.stop_rules('naked'))['pts']
        C,W,NP,NS=len(configs),len(wn),len(P),len(S);rnd=np.random.default_rng(7)
        def arrays(yearly):
            tot=np.zeros((C,W,NP,NS));n=np.zeros((C,W,NP,NS),np.int16)
            for y,v in yearly.items():
                tot[:,wn.index(f'test_{y}')]=v;n[:,wn.index(f'test_{y}')]=10
                for w in wn:
                    if w.startswith('train_') and w!='train_full' and int(w[6:])>int(y):tot[:,wn.index(w)]+=v;n[:,wn.index(w)]+=10
            tot[:,wn.index('train_full')]=sum(yearly.values());n[:,wn.index('train_full')]=10*len(yearly)
            return tot,n
        base={y:rnd.uniform(-50,50,(C,NP,NS)) for y in ('2022','2023','2024')}
        base['2022'][2,0,3]=base['2023'][2,0,3]=base['2024'][2,0,3]=600.                     # steady: 600 a year (inside the band)
        base['2022'][1,0,3],base['2023'][1,0,3],base['2024'][1,0,3]=2400.,200.,200.          # larger total, one good year (outside the band)
        groups=[]
        for off,rate in ((0.,.95),(-3.,.5)):
            tot,n=arrays(base);name=f'grid_bull_naked_{int(off):+d}__pts.npz'
            zg.save_npz(out/name,n=n,total=tot,wins=(n//2).astype(np.int16),sumsq=tot**2/10,configs=np.array(configs),profits=np.array(P),stops=np.array(S),windows=np.array(wn))
            groups.append(dict(file=name,side='bull',structure='naked',offset=off,kind='pts',paths_sha256='x',start=start,split=split,file_sha256=zg.digest(out/name),fill_rate=rate))
        zg.write_json(dict(paths_db='none',paths_sha256='x',rules_sha256=zg.algorithm_digest((),'busy'),start=start,split=split,windows=wins,holidays=[],gap_policy='busy',groups=groups),out/'grid_index.json')
        return out
    def test_band_loyo_and_gate(self):
        out=self._grid_dir();res=zg.report(out,min_trades=5)
        sel=res['selection'];self.assertEqual((sel['full_years'],sel['walk_forward_years'],sel['leave_one_year_out_years']),(['2022','2023','2024'],[],['2022','2023','2024']))
        self.assertIn('no walk-forward year',sel['walk_forward_note'])
        self.assertEqual([(g['offset'],g['fill_rate']) for g in res['untestable_groups']],[(-3.,.5)])
        fam=[f for f in res['families'] if f['side']=='bull' and f['stops']=='pts' and f['configs']=='all' and f['mode']=='total'][0]
        b=fam['train_best'];self.assertEqual((b['config'],b['offset']),('N3_k0.75_m8_j0.5_tw2',0.))          # the steady one, not the larger one-year one
        self.assertEqual(b['years_in_sample'],{'2022':600.,'2023':600.,'2024':600.})
        self.assertEqual([x['year'] for x in fam['leave_one_year_out']],['2022','2023','2024']);self.assertEqual(fam['walk_forward'],[])
        self.assertEqual(fam['leave_one_year_out_pooled']['total'],1800.)
        self.assertTrue(all(f['train_best'] is None or f['train_best']['offset']==0. for f in res['families']))   # the untestable group is never picked
        import io,contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:zg.print_summary(res)
        self.assertNotIn('np.str_',buf.getvalue());self.assertIn('Untestable',buf.getvalue())

class PortfolioRunV497(GridV491):
    def test_run_without_three_training_years_has_no_walk_forward(self):
        import io,contextlib;from nifty_iv.zone import portfolio as zpf
        p,_,_=self._grid();out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False,compound=False)
        with contextlib.redirect_stdout(io.StringIO()):rep,wf,ins,gates=zpf.run(out,p,out/'pf',target=.0001,floor=-10.,max_dd=1e12,progress=False,min_trades=1,wf_min_trades=1)
        self.assertEqual(rep['planned_test_years'],[]);self.assertIn('no walk-forward year',rep['walk_forward_note']);self.assertEqual(wf,[])
        self.assertEqual(rep['walk_forward']['oos_verdict'],'INSUFFICIENT EVIDENCE')

class AllocatedBasisV497(PortfolioV493):
    """V4.9.7 (user): each traded leg meets 5/12 of the account target, floor and drawdown limit; a pair 10/12."""
    def test_leg_share(self):
        self.assertAlmostEqual(self.zpf.leg_share(10,0,5/12),5/12);self.assertAlmostEqual(self.zpf.leg_share(10,10,5/12),10/12);self.assertEqual(self.zpf.leg_share(10,10,None),1.)
    def test_targets_scale_with_the_legs_traded(self):
        T=len(self.so);years=sum(y[4] for y in self.zpf.year_slices(self.sd,'2022-12-01','2025-01-01'))/365
        leg=np.linspace(0,.10*2.5e6*years,T)                                                              # 10% of the capital a year, steadily
        F,ys=self._folds([leg]);span=sum(y[4] for y in ys)/365
        run=lambda G,R,sizes,al:self.zpf.search(G,R,sizes,2.5e6,span,.22,.154,2.5e5,'dd',None,allocation=al)[2]['pass_all']
        E={k:self.zpf.Fold([],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)}
        self.assertEqual(run(F,E,[0,10],None),0)                                                          # account basis: 10% < 22%
        self.assertEqual(run(F,E,[0,10],5/12),1)                                                          # one leg: 10% >= 5/12 x 22% = 9.17%
        self.assertEqual(run(F,F,[10],5/12),1)                                                            # a pair: 20% >= 10/12 x 22% = 18.3%
        self.assertEqual(run(F,F,[10],.5),0)                                                              # a pair at 1/2 each needs 22%
        dd=np.where(np.arange(T)==T//2,-1.2e5,0.)+leg                                                     # a 1.2 lakh dip: above 5/12 x 2.5 lakh
        Fd,_=self._folds([dd]);self.assertEqual(run(Fd,E,[0,10],5/12),0);self.assertEqual(run(Fd,E,[0,10],None)+run(Fd,E,[0,10],.5),0)

class CliDryRunV497(Market):
    """V4.9.7: the command-line entry points end to end on a synthetic extraction (zone_paths -> zone_grid -> zone_portfolio)."""
    def test_paths_grid_portfolio_mains(self):
        import io,contextlib;from nifty_iv.zone import portfolio as zpf
        self.build();d=Path(self.tmp.name);old=sys.argv
        try:
            sys.argv=['zone_paths.py','--options',str(self.op),'--spot',str(self.sp),'--structures','naked,futures_zone','--target-margin','2500000','--out',str(d/'p.sqlite')]
            with contextlib.redirect_stdout(io.StringIO()) as buf:zp.main()
            self.assertIn('paths',buf.getvalue());db=sqlite3.connect(d/'p.sqlite');args=json.loads(db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0]);db.close()
            self.assertEqual((args['entry_bars'],args['max_participation'],args['target_fraction']),(3,.2,.4))
            sys.argv=['zone_grid.py','--paths',str(d/'p.sqlite'),'--out',str(d/'g'),'--start','2023-06-01','--min-trades','1','--no-compound']
            with contextlib.redirect_stdout(io.StringIO()) as buf:zg.main()
            self.assertIn('Selection band',buf.getvalue());self.assertTrue((d/'g'/'grid_report.json').exists())
            with contextlib.redirect_stdout(io.StringIO()) as buf:zpf.main(['--grid',str(d/'g'),'--paths',str(d/'p.sqlite'),'--out',str(d/'pf'),'--min-trades','1','--wf-min-trades','1'])
            self.assertIn('OUT-OF-SAMPLE VERDICT',buf.getvalue());self.assertTrue((d/'pf'/'portfolio_report.json').exists())
        finally:sys.argv=old

class MarginStopV498(Market):
    """V4.9.8: mloss X = the trade's mark-to-market loss reaches X% of its margin (units x table margin per unit on the entry day)."""
    def _db(self):
        m=self.build();bars=self.bars_for(self.days);db=sqlite3.connect(':memory:');db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,'G') for i,b in enumerate(bars)])
        zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=1)).run({0:['bull']},('naked',),(0,),sink=zp.writer(db),progress=False);return db
    def test_mloss_fires_at_the_first_close_beyond_the_margin_share(self):
        from nifty_iv.core import strategy_spec as ss
        db=self._db();fb,eb,units,day,blob=db.execute('SELECT first_bar,end_bar,units,entry_day,data FROM paths').fetchone();n=eb-fb+1
        r={k:np.array(v) for k,v in zp.unpack(blob,n).items()};mg=units*ss.margin_per_unit(day)
        mark=np.zeros(n);mark[2]=-.03*mg;mark[4]=-.052*mg;mark[5]=-.08*mg;r['mark']=mark
        db.execute('UPDATE paths SET data=?',(zp.pack(r),))
        b=zru.Batch([zg.load_paths(db,'bull','naked',0.)[0]],zru.BarArrays.from_db(db),'naked')
        F=zru.evaluate(['mloss2.5','mloss5','mloss7.5','mloss10'],b)[:,0]
        self.assertEqual(list(F),[2,4,5,n-1])                                          # 10% never reached: natural end
        self.assertIn('mloss5',zru.stop_rules('futures_zone'));self.assertIn('c:mloss5|none|none',zru.compound_rules('naked'))
    def test_cutoff_script_uses_the_stored_exit_fill(self):
        from nifty_iv.core import strategy_spec as ss;from nifty_iv.diagnostics import dd_cutoffs as dc
        db=self._db();fb,eb,units,day,blob,pnl=db.execute('SELECT first_bar,end_bar,units,entry_day,data,end_pnl FROM paths').fetchone();n=eb-fb+1
        r={k:np.array(v) for k,v in zp.unpack(blob,n).items()};mg=units*ss.margin_per_unit(day)
        mark=np.zeros(n);mark[3]=-.06*mg;r['mark']=mark;r['cash_total']=np.full(n,100.);r['exit_cash']=np.full(n,-.07*mg);r['exit_bar']=np.arange(fb,fb+n)+1
        db.execute('UPDATE paths SET data=?',(zp.pack(r),))
        tr=dc.load(db,'bull','naked',0.,'2000-01-01','2100-01-01',(5.,7.5));v=tr[0]['v']
        self.assertAlmostEqual(v[5.][0],100-.07*mg);self.assertEqual(v[5.][1],fb+4);self.assertAlmostEqual(v[None][0],pnl);self.assertEqual(v[7.5],v[None])   # 7.5%: never reached
        P=dc.per_trade(tr,5.);self.assertEqual((P['trades'],P['stopped']),(1,1))
    def test_one_at_a_time_takes_more_trades_when_a_stop_frees_the_slot(self):
        from nifty_iv.diagnostics import dd_cutoffs as dc
        tr=[dict(sb=0,day='2023-01-02',v={None:(10.,20),5.:(-5.,4)}),dict(sb=6,day='2023-01-03',v={None:(7.,30),5.:(7.,30)}),dict(sb=25,day='2023-01-05',v={None:(-3.,40),5.:(-3.,40)})]
        self.assertEqual(dc.sequenced(tr,None),(2,7.,3.));self.assertEqual(dc.sequenced(tr,5.),(2,2.,5.))
    def test_cutoff_script_command_line(self):
        import io,contextlib;from nifty_iv.diagnostics import dd_cutoffs as dc
        db=self._db();db.commit();f=Path(self.tmp.name)/'c.sqlite';d=sqlite3.connect(f);db.backup(d);d.close()
        with contextlib.redirect_stdout(io.StringIO()) as buf:dc.main(['--paths',str(f),'--cutoffs','bull,naked,0=5;bear,naked,0=5','--optimise','--out',str(Path(self.tmp.name)/'c.csv')])
        out=buf.getvalue();self.assertIn('ONE AT A TIME',out);self.assertIn('optimised',out);self.assertIn('no trades',out)   # bear: none in this file

class DiagnoseV499(GridV491):
    def test_diagnose_runs_and_waiting_uses_the_marks(self):
        import io,contextlib;from nifty_iv.diagnostics import zone_diagnose as zd
        p,_,_=self._grid();db=sqlite3.connect(p)
        tr=zd.trades(db,'bull','naked',0.);self.assertTrue(tr)
        t=tr[0];self.assertAlmostEqual(zd.at(t,10**6),t['fin']);self.assertAlmostEqual(zd.at(t,1),float(t['mark'][0]) if np.isfinite(t['mark'][0]) else t['fin'])
        db.close()
        with contextlib.redirect_stdout(io.StringIO()) as buf:zd.main(['--paths',str(p),'--strategies','bull,naked,0;bear,naked,0'])
        out=buf.getvalue();self.assertIn('A. Market by half-year',out);self.assertIn('D. bull naked +0',out)

class PairExitsV499(GridV491):
    def test_pair_exit_search_runs_end_to_end(self):
        import io,contextlib;from nifty_iv.diagnostics import pair_exits as pe
        p,_,_=self._grid();out=Path(tempfile.mkdtemp());zg.run_grid(p,out,'2023-06-01','2025-01-01',progress=False,compound=False)
        G,n=pe.leg_candidates(out,zg.read_json(out/'grid_index.json'),('bull','naked',0.,'zone_only_tw2'),5,1,['2024'])
        self.assertTrue(G and n>=len(G));self.assertTrue(all(k[3]=='zone_only_tw2' for k in G))
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            pe.main(['--grid',str(out),'--paths',str(p),'--green','bull,naked,0,zone_only_tw2','--red','bear,naked,0,zone_only_tw2','--target','0.0001','--floor','-10','--band','0',
                     '--per-leg','5','--min-trades','1','--qualify','20','--top','3','--min-year-days','1','--out',str(out/'pe')])
        self.assertIn('Best',buf.getvalue());self.assertTrue((out/'pe'/'pair_exits.csv').exists())

class ClosestBandV499(PortfolioV493):
    def test_closest_list_prefers_pairs_inside_the_band(self):
        T=len(self.so);y23=np.array([d[:4]=='2023' for d in self.sd]);y24=np.array([d[:4]=='2024' for d in self.sd])
        step=lambda a,b:np.cumsum(np.where(y23,a,0.)+np.where(y24,b,0.))
        skew=step(.60*2.5e6/y23.sum(),.05*2.5e6/y24.sum());even=step(.28*2.5e6/y23.sum(),.28*2.5e6/y24.sum())   # skew: larger total, outside the band
        F,ys=self._folds([skew,even]);E={k:self.zpf.Fold([],ys[0][1],ys[-1][2],ys,90,T) for k in (0,10)};span=sum(y[4] for y in ys)/365
        rows,passed,_=self.zpf.search(F,E,[0,10],2.5e6,span,.30,-1.,1.,'dd',None,band=.7)                    # drawdown limit 1 rupee: nothing passes
        self.assertEqual(self.zpf.search(F,E,[0,10],2.5e6,span,.30,-1.,1.,'dd',None)[0][0]['g'],0)           # without the band the skewed one is closest
        self.assertFalse(passed);self.assertEqual(rows[0]['g'],1)                                            # the closest is the one inside the band

class StrategyScanV4910(GridV491):
    def test_scan_applies_the_three_requirements_and_matches_a_trade_by_trade_replay(self):
        import io,contextlib,csv as _csv;from nifty_iv.diagnostics import strategy_scan as ss_
        p,_,_=self._grid();out=Path(tempfile.mkdtemp())
        args=['--paths',str(p),'--judge-years','2024','--min-trades','1','--workers','1','--no-compound']
        with contextlib.redirect_stdout(io.StringIO()) as buf:ss_.main(args+['--min-mean','-1','--min-year','-1','--max-trade-dd','1','--out',str(out/'a.csv')])
        rows=list(_csv.DictReader(open(out/'a.csv')));self.assertTrue(rows);self.assertIn('distinct strategies meet',buf.getvalue())
        keys=[(r['side'],r['structure'],float(r['level']),r['config'],r['profit'],r['stop']) for r in rows[:25]]
        ref=ss_.evaluate(keys,str(p))
        for r,k in zip(rows[:25],keys):                                                   # the vectorised scan = the trade-by-trade replay
            self.assertAlmostEqual(float(r['ret_2024']),ref[k]['years'].get('2024',0.)/2.5e6,4);self.assertEqual(int(r['trades_2024']),ref[k]['trades'].get('2024',0))
            self.assertAlmostEqual(float(r['worst_trade_pct_judged']),ref[k]['worst'].get('2024',0.)/2.5e6,4)
        tight=min(float(r['worst_trade_pct_judged']) for r in rows)
        if tight<0:
            with contextlib.redirect_stdout(io.StringIO()):ss_.main(args+['--min-mean','-1','--min-year','-1','--max-trade-dd',str(-tight/2),'--out',str(out/'b.csv')])
            b=list(_csv.DictReader(open(out/'b.csv'))) if (out/'b.csv').exists() else []
            self.assertLess(len(b),len(rows))                                          # the per-trade limit removes the deepest
        with contextlib.redirect_stdout(io.StringIO()) as buf:ss_.main(args+['--min-mean','5','--min-year','5','--out',str(out/'c.csv')])
        self.assertIn('\n0 distinct strategies meet',buf.getvalue())
class StrategyScanMarksV4911(MarginStopV498):
    def test_worst_trade_uses_the_marks_up_to_the_exit(self):
        import io,contextlib,csv as _csv;from nifty_iv.diagnostics import strategy_scan as ss_;from nifty_iv.core import strategy_spec as ss
        db=self._db();db.execute("INSERT INTO signals VALUES('zone_only_tw2','bull',0)")
        fb,eb,units,blob=db.execute('SELECT first_bar,end_bar,units,data FROM paths').fetchone();n=eb-fb+1
        r={k:np.array(v) for k,v in zp.unpack(blob,n).items()};mark=np.zeros(n);mark[3]=-40000.;mark[6]=-90000.;r['mark']=mark
        db.execute('UPDATE paths SET data=?',(zp.pack(r),));db.commit()
        f=Path(self.tmp.name)/'m.sqlite';d=sqlite3.connect(f);db.backup(d);d.close()
        with contextlib.redirect_stdout(io.StringIO()):ss_.main(['--paths',str(f),'--judge-years','2024','--min-trades','1','--workers','1','--no-compound','--min-mean','-10','--min-year','-10','--max-trade-dd','1','--out',str(Path(self.tmp.name)/'s.csv')])
        rows={(x['profit'],x['stop']):x for x in _csv.DictReader(open(Path(self.tmp.name)/'s.csv'))}
        self.assertAlmostEqual(float(rows[('none','none')]['worst_trade_pct_judged']),min(-90000.,float(db.execute('SELECT end_pnl FROM paths').fetchone()[0]))/2.5e6,5)
        k=('bull','naked',0.,'zone_only_tw2','none','none');ref=ss_.evaluate([k],str(f))[k]
        self.assertAlmostEqual(float(rows[('none','none')]['worst_trade_pct_judged']),ref['worst']['2024']/2.5e6,5)
        hit=[x for (pr,stp),x in rows.items() if stp=='time1']
        if hit:self.assertGreater(float(hit[0]['worst_trade_pct_judged']),-90000/2.5e6)   # an early exit never sees the -90K bar

class EquityDDScanV4912(GridV491):
    """V4.9.12: single-strategy equity drawdown scan = trade-by-trade replay (closed P&L plus the open trade's bar-close marks)."""
    def test_kernel_drawdown_equals_brute_force_on_random_trades(self):
        from nifty_iv.diagnostics import equity_dd_scan as eq
        rnd=np.random.default_rng(3)
        for _ in range(200):
            T=int(rnd.integers(1,8));marks=[np.cumsum(rnd.normal(0,1,int(rnd.integers(1,12)))) for _ in range(T)];fin=[float(m[-1]+rnd.normal()) for m in marks]
            eqv=pk=dd=0.                                                                   # brute force: every mark, then the exit, of each trade in turn
            for m,f in zip(marks,fin):
                for v in list(m)+[f]:e=eqv+v;pk=max(pk,e);dd=max(dd,pk-e)
                eqv+=f
            LO=np.array([[min(m.min(),f,0.)] for m,f in zip(marks,fin)]);HI=np.array([[max(m.max(),f,0.)] for m,f in zip(marks,fin)])
            DI=np.array([[max(max(np.maximum.accumulate(np.maximum(m,0.))-m),max(m.max(),0.)-f,0.)] for m,f in zip(marks,fin)])
            ts=np.arange(T,dtype=np.int64);s,n,D=eq._dd_kernel_py(ts,np.array([0,T]),np.arange(T,dtype=np.int64),np.arange(T,dtype=np.int64),np.full(T,-1),ts.reshape(T,1),
                                                               np.array(fin).reshape(T,1),LO,HI,DI,np.zeros(T,np.int64),np.ones(T,np.bool_),1,1,1)
            self.assertAlmostEqual(D[0,0,0],dd,9);self.assertAlmostEqual(s[0,0,0],sum(fin),9)
    def test_scan_matches_replay(self):
        import io,contextlib,csv as _csv;from nifty_iv.diagnostics import equity_dd_scan as eq
        p,_,_=self._grid();out=Path(tempfile.mkdtemp())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            eq.main(['--paths',str(p),'--judge-years','2024','--min-trades','1','--workers','1','--no-compound','--min-year','-10','--caps','0.5,1','--out',str(out/'e.csv')])
        rows=list(_csv.DictReader(open(out/'e.csv')));self.assertTrue(rows);self.assertIn('GREEN (bull)',buf.getvalue())
        keys=[(r['side'],r['structure'],float(r['level']),r['config'],r['profit'],r['stop']) for r in rows[:40]]
        ref=eq.evaluate(keys,str(p),'2025-01-01')
        for r,k in zip(rows[:40],keys):
            self.assertAlmostEqual(float(r['ret_2024']),ref[k]['years'].get('2024',0.)/2.5e6,4)
            self.assertAlmostEqual(float(r['max_dd_rupees']),ref[k]['max_dd'],delta=1.)
        self.assertTrue(any(float(r['max_dd_rupees'])>0 for r in rows))
        with contextlib.redirect_stdout(io.StringIO()):
            eq.main(['--paths',str(p),'--judge-years','2024','--min-trades','1','--workers','1','--no-compound','--min-year','-10','--caps','1e-9','--out',str(out/'z.csv')])
        z=list(_csv.DictReader(open(out/'z.csv'))) if (out/'z.csv').exists() else []
        self.assertTrue(all(float(r['max_dd_rupees'])<=0.01 for r in z));self.assertLess(len(z),len(rows))

class EquityDDMarksV4912(MarginStopV498):
    def test_drawdown_sees_marks_inside_a_trade(self):
        import io,contextlib,csv as _csv;from nifty_iv.diagnostics import equity_dd_scan as eq
        db=self._db();db.execute("INSERT INTO signals VALUES('zone_only_tw2','bull',0)")
        fb,eb,units,blob=db.execute('SELECT first_bar,end_bar,units,data FROM paths').fetchone();n=eb-fb+1
        r={k:np.array(v) for k,v in zp.unpack(blob,n).items()};mark=np.zeros(n);mark[2]=30000.;mark[6]=-90000.;r['mark']=mark
        db.execute('UPDATE paths SET data=?',(zp.pack(r),));db.commit()
        f=Path(self.tmp.name)/'m.sqlite';d=sqlite3.connect(f);db.backup(d);d.close()
        with contextlib.redirect_stdout(io.StringIO()):eq.main(['--paths',str(f),'--judge-years','2024','--min-trades','1','--workers','1','--no-compound','--min-year','-10','--caps','1','--out',str(Path(self.tmp.name)/'e.csv')])
        rows={(x['profit'],x['stop']):x for x in _csv.DictReader(open(Path(self.tmp.name)/'e.csv'))}
        k=('bull','naked',0.,'zone_only_tw2','none','none');ref=eq.evaluate([k],str(f),'2025-01-01')[k]
        self.assertAlmostEqual(float(rows[('none','none')]['max_dd_rupees']),ref['max_dd'],delta=1.)
        self.assertGreaterEqual(float(rows[('none','none')]['max_dd_rupees']),120000-1)   # +30K high then a -90K mark: at least 120K from the peak

class TradeLedgerV4912(GridV491):
    """V4.9.12: the trade ledger replays each sequenced trade through the path engine; its net cash equals the paths file, and
    gross (after slippage) - charges = net for every trade."""
    def test_ledger_replay_matches_paths_and_adds_up(self):
        from nifty_iv.diagnostics import trade_ledger as tl
        p,_,_=self._grid()
        rows=tl.ledger(str(p),self.op,self.sp,[('bull','naked',0.,'zone_only_tw2','none','none'),('bull','naked',0.,'zone_only_tw2','none','time1'),
                                               ('bear','naked',0.,'zone_only_tw2','none','none')],end='2025-01-01',cfg=pcfg())
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(abs(r['check_rupees']),1.)
            self.assertLessEqual(abs(r['gross_pnl_rs']-r['charges_rs']-r['net_pnl_rs']),2)
            self.assertGreaterEqual(r['days_held'],1);self.assertTrue(r['entry_first_fill'])
            self.assertAlmostEqual(r['interest_rs'],r['margin_rs']*.10*r['days_held']/365,delta=1)
        y=tl.years(rows)
        for d in y:
            rs=[r for r in rows if r['strategy']==d['strategy'] and r['year']==d['year']]
            self.assertEqual(d['trades'],len(rs));self.assertEqual(d['net_after_interest_rs'],sum(r['net_after_interest_rs'] for r in rs))
