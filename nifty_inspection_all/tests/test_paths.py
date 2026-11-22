"""Path engine and exit grid tests: where the old engine and the new one model the same thing, they must agree exactly.
Synthetic only; none of this is evidence of profitability."""
import sys,unittest,tempfile,sqlite3,math,datetime as dt,random,collections
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import zone_signals as zs
import zone_backtest as zb
import zone_paths as zp
import zone_grid as zg
import zone_rules as zru
import zone_report as zr
from contract_specs_v2 import LotMaster
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
        p=self.paths(m,bars,final,cfg=pcfg(lots=46))[0][0];self.assertEqual((p['status'],p['reason']),('closed','partial_entry_unwound'))   # 45 of 46 lots traded: held, then sold back
        self.assertEqual(p['rows']['exit_cash'][0]!=0,True)
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
        self.assertEqual((len(zg.profit_rules('naked')),len(zg.stop_rules('naked'))),(50,368))
        # every V4.7.x rule is still in the grid
        for lab in ['tp10','tp75','target15x4','atr30x1','atr45x3','trail15x3','strail30x2.5','pct0.5','loss25','loss200']:self.assertIn(lab,zg.profit_rules('naked')+zg.stop_rules('naked'))
        self.assertEqual(zg.parse('atr30x1.5'),('atr',30,1.5));self.assertEqual(zg.parse('tp25'),('tp',.25));self.assertEqual(zg.parse('loss150'),('loss',1.5))

class Futures(unittest.TestCase):
    def test_futures_path_matches_benchmark(self):
        import zone_futures as zf
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
    def test_partial_calendar_entry_is_held_and_unwound(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '10:00' AND '10:44'",(self.days[0],))
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg());out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        p=out[0];self.assertEqual(p['reason'],'partial_entry_unwound');self.assertEqual((sim.counts['partial_entry_short_full'],sim.counts['partial_entry_long_none']),(1,1))
        self.assertEqual(len(p['rows']['mark']),1);self.assertTrue(math.isnan(p['rows']['base'][0]))   # no take-profit or premium stop on it
        self.assertLess(p['end_pnl'],0)                                                                    # a round trip on one leg costs money
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
        return SimpleNamespace(window=lambda k:('2024-01-03',45,90),counts=collections.Counter(),
            slice=lambda day,i0,i1,c,want,lot,buy=True:(q[c],float(q[c])*100.,q[c],45),
            book=lambda acc,c,sign,lot:-float(acc[1])*100.*sign,index_at=lambda *a:20000.)
    def test_every_executed_quantity_is_recorded(self):
        sh=('2024-01-11',20000.,'PE');lg=('2024-03-28',20000.,'PE');legs=[(sh,-100),(lg,100)]
        for q,status,filled in (({sh:0,lg:0},'none',None),({sh:50,lg:50},'partial',{sh:-50,lg:50}),({sh:100,lg:50},'partial',{sh:-100,lg:50}),
                                ({sh:50,lg:100},'partial',{sh:-50,lg:100}),({sh:100,lg:100},'ok',{sh:-100,lg:100})):
            r=zp.PathSim.enter(self.fake(q),0,legs,50);self.assertEqual(r['status'],status,q)
            if filled:
                self.assertEqual(dict(r['filled']),filled,q)
                self.assertAlmostEqual(r['cash'],sum(-u*100. for u in filled.values()))   # cash for exactly the executed units
    def test_partial_leg_in_a_real_market_is_held_and_unwound(self):
        m=self.build();lot=LotMaster(None).get(self.days[0],'2024-01-11').qty
        m=self.edit("UPDATE opt_1m SET volume=? WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '10:00' AND '10:44'",(lot*10,self.days[0]))   # 1 lot a minute for the long
        bars=self.bars_for(self.days);sim=zp.PathSim(m,bars,['G']*len(bars),pcfg(lots=50));out=[];sim.run({0:['bull']},('calendar',),(0,),sink=out.extend,progress=False)
        p=out[0];self.assertEqual(p['reason'],'partial_entry_unwound')
        self.assertEqual((sim.counts['partial_entry_short_full'],sim.counts['partial_entry_long_part']),(1,1))   # short 50 lots, long 45 of 50
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
