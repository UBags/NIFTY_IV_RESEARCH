"""Zone + swing signal and option-backtest tests. Synthetic only; none of this is evidence of profitability."""
import sys,unittest,tempfile,sqlite3,math,json,datetime as dt,random,os
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
os.environ.setdefault('NIFTY_HOLDOUT_LOG',str(Path(tempfile.mkdtemp())/'log.jsonl'))
import numpy as np
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.zone.legacy import report as zr
from nifty_iv.data.research_cache import DDL
from nifty_iv.ivcal.quant_v3 import black_forward

def sessions(start,n):
    d=dt.date.fromisoformat(start);out=[]
    while len(out)<n:
        if d.weekday()<5:out.append(d.isoformat())
        d+=dt.timedelta(1)
    return out

def walk(days,seed=1,drift=0.,vol=6.,start=20000.):
    """Minute bars 09:15-15:29 for each day: a random walk with optional drift per minute."""
    rnd=random.Random(seed);p=start;out={}
    for d in days:
        rows=[]
        for m in range(zs.OPEN,zs.CLOSE):
            o=p;p=max(100.,p+drift+rnd.gauss(0,vol));rows.append((m,o,max(o,p)+1,min(o,p)-1,p))
        out[d]=rows
    return out

class Bars(unittest.TestCase):
    def test_45_minute_session_alignment(self):
        b=zs.aggregate(walk(['2024-01-02']),45)
        self.assertEqual([x.end for x in b],[600,645,690,735,780,825,870,915,930])
        self.assertEqual(b[-1].start,915);self.assertEqual(b[0].mark_minute,'09:59');self.assertEqual(b[-1].mark_minute,'15:29')
    def test_higher_bars_do_not_cross_sessions(self):
        b=zs.aggregate(walk(['2024-01-02','2024-01-03']),360)
        self.assertEqual([(x.day,x.start,x.end) for x in b],[('2024-01-02',555,915),('2024-01-02',915,930),('2024-01-03',555,915),('2024-01-03',915,930)])
    def test_bar_ohlc(self):
        m=walk(['2024-01-02']);b=zs.aggregate(m,45)[0];rows=[r for r in m['2024-01-02'] if r[0]<600]
        self.assertEqual((b.open,b.high,b.low,b.close),(rows[0][1],max(r[2] for r in rows),min(r[3] for r in rows),rows[-1][4]))
    def test_htf_value_arrives_only_when_higher_bar_closes(self):
        chart=zs.aggregate(walk(['2024-01-02']),45);h=zs.aggregate(walk(['2024-01-02']),90)
        v=zs.sample(chart,h,list(range(len(h))))
        self.assertTrue(math.isnan(v[0]));self.assertEqual(list(v[1:]),[0,0,1,1,2,2,3,4])

class Indicators(unittest.TestCase):
    def test_ema_seed_and_wma(self):
        e=zs.ema(np.array([10.,20.]),3);self.assertEqual(e[0],10.);self.assertAlmostEqual(e[1],15.)
        w=zs.wma(np.array([1.,2.,3.]),3);self.assertTrue(math.isnan(w[1]));self.assertAlmostEqual(w[2],(1+4+9)/6)
    def test_pine_round(self):self.assertEqual([zs.pine_round(math.sqrt(x)) for x in (6,7,16,24)],[2,3,4,5])
    def test_atr_is_rma_seeded_with_sma(self):
        b=[zs.Bar('d',0,0,10,12,9,11),zs.Bar('d',0,0,11,13,10,12),zs.Bar('d',0,0,12,15,11,14)]
        a=zs.atr(b,2);self.assertTrue(math.isnan(a[0]));self.assertAlmostEqual(a[1],3.);self.assertAlmostEqual(a[2],(3+4)/2)

class Zones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        days=sessions('2023-01-02',160)
        m=walk(days[:60],drift=.6);m.update(walk(days[60:110],seed=2,drift=-.7,start=m[days[59]][-1][4]));m.update(walk(days[110:],seed=3,drift=.5,start=m[days[109]][-1][4]))
        cls.minutes=m;cls.bars,cls.final,cls.aux=zs.zones(m)
    def test_trend_gives_both_colours(self):
        self.assertIn('G',self.final);self.assertIn('R',self.final)
    def test_persistence_needs_three_bars(self):
        pre=self.aux['pre']
        for i in range(3,len(pre)):
            if self.final[i]!=self.final[i-1] and self.final[i] is not None:self.assertEqual(pre[i-2:i+1],[self.final[i]]*3)
    def test_v14_equals_prefinal(self):
        _,f14,aux=zs.zones(self.minutes,fixed=False);self.assertEqual(f14,aux['pre'])
    def test_fixed_filter_changes_colour_less_often(self):
        _,f14,_=zs.zones(self.minutes,fixed=False)
        flips=lambda f:sum(f[i]!=f[i-1] for i in range(1,len(f)))
        self.assertLessEqual(flips(self.final),flips(f14))
    def test_no_lookahead(self):
        days=sorted(self.minutes);cut=days[120];m=dict(self.minutes)
        for d in days[121:]:m[d]=[(t,o*1.3,h*1.3,l*1.3,c*1.3) for t,o,h,l,c in m[d]]
        b2,f2,_=zs.zones(m);n=sum(1 for b in self.bars if b.day<=cut)
        self.assertEqual(self.final[:n],f2[:n])
        s1=zs.signal_grid(self.bars,self.final,dict(N=(3,),k=(.5,),m=(8,),j=(.25,),tw=(2,)));s2=zs.signal_grid(b2,f2,dict(N=(3,),k=(.5,),m=(8,),j=(.25,),tw=(2,)))
        for c in s1:
            for side in ('bull','bear'):self.assertEqual([t for t in s1[c][side] if t<n-8],[t for t in s2[c][side] if t<n-8])

def bar(i,lo,hi):return zs.Bar('2024-01-02',i,i+1,(lo+hi)/2,hi,lo,(lo+hi)/2)

class Swings(unittest.TestCase):
    def series(self,lows):return [bar(i,l,l+10) for i,l in enumerate(lows)]
    def test_pivot_tie_rule(self):
        self.assertEqual(zs.pivots([5,4,3,3,4,5,6],2,'low'),[2])
    def test_rising_valley_qualifies(self):
        lows=[100,95,90,95,100,105,110,115,108,102,97,102,107,112,117]
        b=self.series(lows);a=np.full(len(b),4.)
        ev=zs.swing_events(b,a,2,1.,8,1.)
        self.assertEqual(ev['low'],[(12,10,97)])   # 97-90=7 >= 4; highest high between 125-97=28 >= 4; gap 8
        self.assertEqual(zs.swing_events(b,a,2,1.,9,1.)['low'],[])   # spacing too short
        self.assertEqual(zs.swing_events(b,a,2,1.,8,2.)['low'],[])   # rise 7 < 8
        self.assertEqual(zs.swing_events(b,a,2,7.5,8,1.)['low'],[])  # swing 28 < 30
    def test_entry_waits_tw_and_needs_colour(self):
        lows=[100,95,90,95,100,105,110,115,108,102,97,102,107,112,117,118,119,120,121,122]
        b=self.series(lows);a=np.full(len(b),4.);ev=zs.swing_events(b,a,2,1.,8,1.)
        final=['G']*len(b)
        self.assertEqual(zs.entries(b,final,ev,2)['bull'],[14])
        final2=['R']*15+['G']*5
        self.assertEqual(zs.entries(b,final2,ev,2)['bull'],[15])   # the valley came first, the colour after
        self.assertEqual(zs.entries(b,['R']*len(b),ev,2)['bull'],[])
    def test_entry_cancelled_below_valley(self):
        lows=[100,95,90,95,100,105,110,115,108,102,97,102,107,112,90,90,90,90,90,90]
        b=self.series(lows);b[14]=zs.Bar('2024-01-02',14,15,95,100,90,95)
        a=np.full(len(b),4.);ev=zs.swing_events(b,a,2,1.,8,1.)
        self.assertEqual(zs.entries(b,['G']*len(b),ev,2)['bull'],[])
    def test_zone_only_control(self):
        f=[None,'R','R','G','G','G','G','R']
        self.assertEqual(zs.zone_only_entries(f,2)['bull'],[5]);self.assertEqual(zs.zone_only_entries(f,2)['bear'],[])

class Market(unittest.TestCase):
    """Options and spot files as zone_contracts.py and zone_spot.py write them: every minute, a few contracts."""
    def build(self,days=None,level=None,far='2024-03-28',weekly='2024-01-11',extra=(),vol=100000,vendor2=None):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        days=days or sessions('2024-01-02',8);level=level or (lambda d,m:20000.);self.days=days
        op=Path(self.tmp.name)/'o.sqlite';sp=Path(self.tmp.name)/'s.sqlite'
        from nifty_iv.zone import contracts as zc
        db=sqlite3.connect(op);db.executescript(zc.DDL);s=sqlite3.connect(sp)
        s.execute('CREATE TABLE spot_1m(day TEXT,minute TEXT,open REAL,high REAL,low REAL,close REAL,PRIMARY KEY(day,minute))')
        rows=[];listing=[]
        for d in days:
            for e in (weekly,far,*extra):
                if e>=d:listing.append((d,'rissin_upstox',e))
            for mi in range(zs.OPEN,zs.CLOSE):
                m=zs.hhmm(mi);x=level(d,m);s.execute('INSERT INTO spot_1m VALUES(?,?,?,?,?,?)',(d,m,x,x,x,x))
                for e in (weekly,far,*extra):
                    t=(dt.datetime.fromisoformat(e+' 15:30')-dt.datetime.fromisoformat(f'{d} {m}')).total_seconds()/(365.25*86400)
                    if t<=0:continue
                    for k in (19800.,19900.,20000.,20100.,20200.):
                        for cp in ('PE','CE'):
                            p=max(.05,round(black_forward(x,k,t,.15,cp,.0),2))
                            rows.append(('rissin_upstox',d,m,e,k,cp,p,p,p,p,vol))
                            if vendor2:rows.append(('thetrademarkk',d,m,e,k,cp,p+vendor2[0],p,p,p,vendor2[1]))
        db.executemany('INSERT INTO opt_1m VALUES(?,?,?,?,?,?,?,?,?,?,?)',rows);db.executemany('INSERT OR IGNORE INTO listing VALUES(?,?,?)',listing)
        db.execute('CREATE INDEX opt_1m_day ON opt_1m(day)');db.commit();db.close();s.commit();s.close()
        self.op,self.sp=str(op),str(sp);return zb.Market(self.op,self.sp)
    def edit(self,sql,args=()):
        c=sqlite3.connect(self.op);c.execute(sql,args);c.commit();c.close();return zb.Market(self.op,self.sp)
    def cfg(self,**kw):
        a=SimpleNamespace(slippage=0.,brokerage=0.,lots=1,max_participation=.1,min_short_days=4,long_target=60,contract_master=None,max_exit_lag_bars=9,forced_exit_haircut=.02,atr_mult=1.);a.__dict__.update(kw);return zb.config(a)
    def bars_for(self,days,level=lambda d,e:20000.):
        return [zs.Bar(d,s,min(s+45,zs.CLOSE),0,0,0,level(d,zs.hhmm(min(s+45,zs.CLOSE)-1))) for d in days for s in range(zs.OPEN,zs.CLOSE,45)]
    def go(self,m,bars,final,entries={0:['bull']},structures=('naked',),tps=(None,),cfg=None,boundaries=()):
        sim=zb.Sim(m,bars,final,cfg or self.cfg(),boundaries);return sim.run(entries,structures=structures,offsets=(0,),tps=tps,progress=False),sim

class Backtest(Market):
    def test_naked_put_settles_at_expiry_when_zone_holds(self):
        m=self.build();bars=self.bars_for(self.days);tr,_=self.go(m,bars,['G']*len(bars))
        t=tr[0];self.assertEqual((t['status'],t['reason'],t['short_expiry'],t['entry_minute']),('closed','expiry','2024-01-11','10:00'))
        self.assertEqual(t['fills'][-1]['event'],'settle');self.assertEqual(t['fills'][-1]['source'],'twap_1500_1529_proxy')
        self.assertAlmostEqual(t['pnl_inr'],t['fills'][0]['cash'])
    def test_fill_waits_for_volume_inside_the_next_bar(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE day=? AND minute BETWEEN '10:00' AND '10:29'",(self.days[0],))
        bars=self.bars_for(self.days);tr,_=self.go(m,bars,['G']*len(bars))
        self.assertEqual(tr[0]['entry_minute'],'10:30')
    def test_no_fill_when_the_whole_bar_is_thin(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE day=? AND minute BETWEEN '10:00' AND '10:44'",(self.days[0],))
        bars=self.bars_for(self.days);tr,_=self.go(m,bars,['G']*len(bars))
        self.assertEqual((tr[0]['status'],tr[0]['reason']),('no_fill','no_liquid_entry'))
    def test_four_days_counted_from_the_fill_day(self):
        # Friday's last bar fills on Monday: Thursday 2024-01-11 is 3 days from Monday, so the next expiry is taken
        days=sessions('2024-01-05',5);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',));bars=self.bars_for(days)
        tr,_=self.go(m,bars,['G']*len(bars),entries={8:['bull']})
        self.assertEqual(tr[0]['entry_day'],'2024-01-08');self.assertEqual(tr[0]['short_expiry'],'2024-01-18')
        tr,_=self.go(m,bars,['G']*len(bars),entries={7:['bull']})   # same Friday, fills Friday: 6 days
        self.assertEqual(tr[0]['short_expiry'],'2024-01-11')
    def test_zone_flip_exits_in_the_next_bar(self):
        m=self.build();bars=self.bars_for(self.days);final=['G']*5+['R']*(len(bars)-5)
        t=self.go(m,bars,final)[0][0];self.assertEqual(t['reason'],'zone_flip');self.assertEqual((t['exit_day'],t['exit_minute']),(bars[5].day,zs.hhmm(bars[5].end)))
    def test_last_bar_exit_fills_next_morning(self):
        m=self.build();bars=self.bars_for(self.days);final=['G']*8+['R']*(len(bars)-8)
        t=self.go(m,bars,final)[0][0];self.assertEqual((t['exit_day'],t['exit_minute']),(self.days[1],'09:15'))
    def test_take_profit_triggers_on_mark(self):
        days=sessions('2024-01-02',8);lv=lambda d,mm:20000.+(150. if d>=days[2] else 0.)
        m=self.build(days=days,level=lv);bars=self.bars_for(days,lv)
        tr,_=self.go(m,bars,['G']*len(bars),tps=(None,.5));by={t['tp']:t for t in tr}
        self.assertEqual(by[.5]['reason'],'take_profit');self.assertGreater(by[.5]['pnl_inr'],.45*by[.5]['base']);self.assertEqual(by[None]['reason'],'expiry')
    def test_calendar_rolls_on_short_expiry_day(self):
        days=sessions('2024-01-02',12);m=self.build(days=days,weekly='2024-01-11',extra=('2024-01-18',));bars=self.bars_for(days)
        tr,_=self.go(m,bars,['G']*len(bars),structures=('calendar',))
        first=tr[0];self.assertEqual((first['reason'],first['exit_day'],first['exit_minute'],first['long_expiry']),('short_expiry_roll','2024-01-11','15:15','2024-03-28'))
        second=[t for t in tr if t['roll']==1 and t['chain']==first['chain']]
        self.assertEqual(len(second),1);self.assertEqual((second[0]['entry_minute'],second[0]['short_expiry']),('15:15','2024-01-18'))
    def test_calendar_long_is_a_later_expiry(self):
        days=sessions('2024-01-08',6);m=self.build(days=days,weekly='2024-01-11');bars=self.bars_for(days)
        tr,_=self.go(m,bars,['G']*len(bars),structures=('calendar',));self.assertEqual((tr[0]['status'],tr[0]['reason']),('no_fill','no_long_expiry'))
    def test_long_sold_only_after_short(self):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND day=? AND minute BETWEEN '12:15' AND '12:24'",(self.days[0],))
        bars=self.bars_for(self.days);final=['G']*3+['R']*(len(bars)-3)
        t=self.go(m,bars,final,structures=('calendar',))[0][0];closes=[f for f in t['fills'] if f['event']=='close']
        self.assertGreater(closes[0]['units'],0);self.assertEqual(closes[0]['minute'],'12:15');self.assertEqual(closes[1]['minute'],'12:25')
    def test_vendor_with_more_volume_is_used(self):
        m=self.build(vendor2=(5.,200000));bars=self.bars_for(self.days);t=self.go(m,bars,['G']*len(bars))[0][0]
        self.assertEqual(t['fills'][0]['vendor'],'thetrademarkk')
    def test_charges_reduce_pnl(self):
        m=self.build();bars=self.bars_for(self.days)
        a=self.go(m,bars,['G']*len(bars))[0][0];b=self.go(m,bars,['G']*len(bars),cfg=self.cfg(brokerage=20.,slippage=.5))[0][0]
        self.assertLess(b['pnl_inr'],a['pnl_inr'])
    def test_boundary_value_recorded_for_open_positions(self):
        m=self.build();bars=self.bars_for(self.days);tr,_=self.go(m,bars,['G']*len(bars),boundaries=(self.days[3],))
        t=tr[0];self.assertIn(self.days[3],t['marks_at']);self.assertIsInstance(t['marks_at'][self.days[3]],float)
    def test_open_at_data_end_is_valued(self):
        days=sessions('2024-01-02',4);m=self.build(days=days);bars=self.bars_for(days);t=self.go(m,bars,['G']*len(bars))[0][0]
        self.assertEqual(t['status'],'open_at_data_end');self.assertIn(zb.END_KEY,t['marks_at'])

class LagCap(Market):
    def run_thin(self,cap):
        m=self.build();m=self.edit("UPDATE opt_1m SET volume=0 WHERE expiry='2024-03-28' AND (day>? OR minute>'11:00')",(self.days[0],))
        bars=self.bars_for(self.days);final=['G']*3+['R']*(len(bars)-3)
        return self.go(m,bars,final,structures=('calendar',),cfg=self.cfg(max_exit_lag_bars=cap))[0][0]
    def test_cap_books_long_at_last_close_less_haircut(self):
        t=self.run_thin(2);f=[x for x in t['fills'] if x['event']=='forced_exit_lag_cap'][0]
        self.assertEqual(t['status'],'closed');self.assertTrue(t['forced']);self.assertAlmostEqual(f['price'],f['ref']*.98);self.assertFalse(f['executable_claim'])
        self.assertGreater(f['cash'],0);self.assertAlmostEqual(f['cash'],-f['units']*f['price'])   # a long sold: cash in
    def test_no_cap_keeps_waiting(self):self.assertEqual(self.run_thin(-1)['status'],'open_at_data_end')

class Plan(Market):
    def test_plan_covers_every_contract_day_a_real_run_uses(self):
        from nifty_iv.zone import contracts as zc
        days=sessions('2024-01-02',12);lv=lambda d,mm:20000.+(80. if d>=days[3] else 0.)
        m=self.build(days=days,level=lv,extra=('2024-01-18',));bars=self.bars_for(days,lv);final=['G']*40+['R']*(len(bars)-40)
        sig={'c':{'bull':[0,10,20],'bear':[45]}}
        a=SimpleNamespace(lots=1,min_short_days=4,long_target=60,contract_master=None,from_date='2024-01-01',to_date='2024-12-31',buffer_sessions=3,offsets='0')
        targets,_=zc.plan_targets(bars,final,sig,m.listing,a)
        tr,_=self.go(m,bars,final,entries=zb.entry_map(bars,sig,'2024-01-01','2024-12-31'),structures=('naked','calendar'),tps=(None,.5))
        used={(f['day'],f['expiry'],f['strike'],f['cp']) for t in tr for f in t.get('fills',[]) if f['event'] in ('open','close')}
        self.assertTrue(used);self.assertLessEqual(used,targets)

class Report(unittest.TestCase):
    def db(self,rows,signals):
        p=Path(tempfile.mkdtemp())/'t.sqlite';con=sqlite3.connect(p);con.executescript(zb.DDL)
        con.executemany('INSERT INTO bars VALUES(?,?,?,?,?)',[(i,d,'10:00',1.,'G') for i,d in enumerate(sessions('2022-01-03',800))])
        con.executemany('INSERT INTO signals VALUES(?,?,?)',signals)
        for r in rows:con.execute(f'INSERT INTO trades({",".join(r)}) VALUES({",".join("?"*len(r))})',tuple(json.dumps(v) if k=='marks_at' else v for k,v in r.items()))
        con.commit();con.close();return p
    def trade(self,t,day,xbar,xday,unit,tp=None,status='closed',roll=0,chain=None,marks=None):
        tid=f'bull|naked|0.0|{tp}|{t}|{roll}'
        return dict(id=tid,chain=chain or tid,side='bull',structure='naked',offset=0.,tp=tp,signal_bar=t,roll=roll,entry_day=day,exit_day=xday,exit_bar=xbar,status=status,unit_pnl=unit,forced=0,marks_at=marks or {})
    def test_one_position_at_a_time(self):
        rows=[self.trade(0,'2022-01-03',5,'2022-01-10',10.),self.trade(3,'2022-01-06',8,'2022-01-13',-50.),self.trade(6,'2022-01-11',9,'2022-01-14',5.)]
        data=zr.load(self.db(rows,[('c','bull',x) for x in (0,3,6)]))
        tr,counts=zr.trades_for([0,3,6],'bull','naked',0.,None,data[2],data[3],data[0],'2022-01-01','2023-01-01')
        self.assertEqual([c['unit'] for c in tr],[10.,5.]);self.assertEqual(counts['skipped_busy'],1)
    def test_open_chain_closed_hypothetically_at_cutoff(self):
        rows=[self.trade(0,'2022-12-26',9,'2023-01-05',-300.,marks={'2023-01-01':-40.})]
        data=zr.load(self.db(rows,[('c','bull',0)]))
        tr,counts=zr.trades_for([0],'bull','naked',0.,None,data[2],data[3],data[0],'2022-01-01','2023-01-01')
        self.assertEqual(tr[0]['unit'],-40.);self.assertTrue(tr[0]['hypothetical']);self.assertEqual(counts['hypothetical_close'],1)
        tr,_=zr.trades_for([0],'bull','naked',0.,None,data[2],data[3],data[0],'2022-01-01','2024-01-01')
        self.assertEqual(tr[0]['unit'],-300.)
    def test_roll_chain_with_open_pair_at_cutoff(self):
        a=self.trade(0,'2022-12-19',5,'2022-12-29',10.);b=self.trade(0,'2022-12-29',12,'2023-01-05',-4.,roll=1,chain=a['id'],marks={'2023-01-01':3.})
        data=zr.load(self.db([a,b],[('c','bull',0)]))
        tr,_=zr.trades_for([0],'bull','naked',0.,None,data[2],data[3],data[0],'2022-01-01','2023-01-01')
        self.assertAlmostEqual(tr[0]['unit'],13.);self.assertEqual(tr[0]['pair_units'],[10.,3.])
        self.assertEqual(zr.stats(tr)['pair_win'],1.)
    def test_tp_rule_needs_trades_per_cutoff(self):
        by={None:dict(total=100.,win=.5,n=45),.25:dict(total=80.,win=.8,n=45),.5:dict(total=90.,win=1.,n=2)}
        self.assertEqual(zr.choose_tp(by,.75,30),.25);self.assertIs(zr.choose_tp(by,.75,50),zr.NO_TP_QUALIFIES)
    def test_no_take_profit_can_be_chosen(self):
        by={None:dict(total=100.,win=.9,n=40),.25:dict(total=50.,win=.8,n=40)}
        self.assertIsNone(zr.choose_tp(by,.75,30))
    def test_losing_configuration_is_not_frozen(self):
        rows=[dict(side='bull',structure='naked',n=40,total=-5.,mean=-.1)]
        self.assertIsNone(zr.best(rows,'bull','naked',30))
    def test_train_window_ignores_later_outcomes(self):
        base=[self.trade(0,'2022-01-03',5,'2022-01-10',10.)];later=self.trade(700,'2024-09-02',705,'2024-09-09',-1e9)
        d1=zr.load(self.db(base,[('c','bull',0)]));d2=zr.load(self.db(base+[later],[('c','bull',0),('c','bull',700)]))
        self.assertEqual(zr.evaluate(d1,'2021-09-01','2024-01-01',.75,1,['naked'],[0.],[None]),zr.evaluate(d2,'2021-09-01','2024-01-01',.75,1,['naked'],[0.],[None]))



class AtrStop(Market):
    def setup(self):
        days=sessions('2024-01-02',10);lv=lambda d,mm:20000.-(100. if d>=days[4] else 0.)
        m=self.build(days=days,level=lv,weekly='2024-01-18',far='2024-03-28')
        bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+20,b.close-20,b.close) for b in self.bars_for(days,lv)]
        return m,bars
    def test_bull_stop_exits_when_close_breaks_level(self):
        m,bars=self.setup();tr,_=self.go(m,bars,['G']*len(bars),entries={20:['bull']},tps=(None,),cfg=self.cfg(atr_mult=1.))
        base=tr[0];self.assertEqual(base['stop'],None);self.assertNotEqual(base['reason'],'atr_stop')
        sim=zb.Sim(m,bars,['G']*len(bars),self.cfg(atr_mult=1.));t=sim.run({20:['bull']},structures=('naked',),offsets=(0,),tps=(None,),stops=(15,),progress=False)[0]
        self.assertEqual(t['reason'],'atr_stop');self.assertEqual(t['stop'],15);self.assertEqual(t['exit_day'],self.days[4])
    def test_wide_stop_not_hit(self):
        m,bars=self.setup();sim=zb.Sim(m,bars,['G']*len(bars),self.cfg(atr_mult=5.))
        t=sim.run({20:['bull']},structures=('naked',),offsets=(0,),tps=(None,),stops=(15,),progress=False)[0]
        self.assertNotEqual(t['reason'],'atr_stop')
    def test_report_family_name(self):
        self.assertEqual(zr.family('naked',30),'naked|atr30');self.assertEqual(zr.family('naked',None),'naked')

class FuturesBench(unittest.TestCase):
    def setUp(self):
        from nifty_iv.zone.legacy import futures as zf;self.zf=zf
        self.days=sessions('2024-01-02',10);self.m=walk(self.days,drift=.3)
        self.bars=zs.aggregate(self.m,45);self.listing={d:{'2024-01-11','2024-01-18'} for d in self.days}
    def cfg(self,**kw):
        c=SimpleNamespace(brokerage=0.,gst=.18,hedge_bps=0.,hedge_statutory=False,slippage_points=0.,lots=1,atr_mult=1.,min_short_days=4,contract_master=None);c.__dict__.update(kw);return c
    def test_pnl_is_index_move_times_units(self):
        final=['G']*30+['R']*(len(self.bars)-30)
        f=self.zf.Futures(self.bars,final,self.m,self.listing,self.cfg());t=[x for x in f.run({2:['bull']},('futures_zone',),(None,)) if x['status']=='closed'][0]
        self.assertEqual(t['reason'],'zone_flip');self.assertEqual(t['exit_bar'],30)
        entry=self.m[self.bars[3].day][self.bars[3].start-zs.OPEN][1];exit_=self.m[self.bars[31].day][self.bars[31].start-zs.OPEN][1]
        self.assertAlmostEqual(t['pnl_inr'],(exit_-entry)*t['lot'])
    def test_bear_sign_and_costs(self):
        final=['R']*30+['G']*(len(self.bars)-30)
        a=self.zf.Futures(self.bars,final,self.m,self.listing,self.cfg()).run({2:['bear']},('futures_zone',),(None,))[0]
        b=self.zf.Futures(self.bars,final,self.m,self.listing,self.cfg(brokerage=20.,hedge_statutory=True,slippage_points=1.)).run({2:['bear']},('futures_zone',),(None,))[0]
        self.assertLess(a['pnl_inr'],0)   # upward drift hurts a short
        self.assertLess(b['pnl_inr'],a['pnl_inr'])
    def test_expiry_rule_matches_option_horizon(self):
        f=self.zf.Futures(self.bars,['G']*len(self.bars),self.m,self.listing,self.cfg());t=f.run({2:['bull']},('futures_expiry',),(None,))[0]
        self.assertEqual((t['reason'],t['short_expiry'],t['exit_day'],t['exit_minute']),('expiry','2024-01-11','2024-01-11','15:15'))
    def test_report_reads_benchmark(self):
        f=self.zf.Futures(self.bars,['G']*len(self.bars),self.m,self.listing,self.cfg());tr=f.run({2:['bull'],40:['bull']},('futures_expiry',),(None,30))
        p=Path(tempfile.mkdtemp())/'f.sqlite';zb.write(str(p),self.bars,['G']*len(self.bars),{'c':{'bull':[2,40],'bear':[]}},tr,{},keep_fills=False)
        data=zr.load(str(p));rows=zr.evaluate(data,'2024-01-01','2025-01-01',.75,1,['futures_expiry','futures_expiry|atr30'],[0.],[None])
        self.assertEqual({r['structure'] for r in rows},{'futures_expiry','futures_expiry|atr30'})

class Boundaries(unittest.TestCase):
    def bars(self):
        return [zs.Bar('2024-12-31',870,915,20000,20005,19995,20000),zs.Bar('2024-12-31',915,930,20000,20005,19995,20000),zs.Bar('2025-01-02',555,600,20100,20115,20080,20110)]
    def fcfg(self):return SimpleNamespace(contract_master=None,slippage_points=1.,lots=1,brokerage=20.,gst=.18,hedge_bps=0.,hedge_statutory=True,atr_mult=1.,min_short_days=4)
    def test_futures_exit_filled_next_session_still_valued_at_cutoff(self):
        from nifty_iv.zone.legacy import futures as zf
        minutes={'2024-12-31':[(915,20000,20000,20000,20000)],'2025-01-02':[(555,20100,20100,20100,20100)]}
        p=zf.Futures(self.bars(),['G','R','R'],minutes,{},self.fcfg(),boundaries=['2025-01-01']).run({0:['bull']},('futures_zone',),(None,))[0]
        self.assertEqual((p['entry_day'],p['exit_day']),('2024-12-31','2025-01-02'));self.assertIn('2025-01-01',p['marks_at'])
    def test_no_mark_before_a_position_exists(self):
        from nifty_iv.zone.legacy import futures as zf
        minutes={'2024-12-31':[(915,20000,20000,20000,20000)],'2025-01-02':[(555,20100,20100,20100,20100)]}
        p=zf.Futures(self.bars()[1:],['G','G'],minutes,{},self.fcfg(),boundaries=['2025-01-01']).run({0:['bull']},('futures_zone',),(None,))[0]
        self.assertEqual(p['entry_day'],'2025-01-02');self.assertNotIn('2025-01-01',p['marks_at'])
    def test_options_exit_filled_next_session_still_valued_at_cutoff(self):
        cfg=zb.config(SimpleNamespace(slippage=0.,brokerage=0.,lots=1,max_participation=.1,min_short_days=4,long_target=60,contract_master=None,max_exit_lag_bars=9,forced_exit_haircut=.02,atr_mult=1.))
        m=zb.PlanMarket({'2024-12-31':{'2025-01-09'},'2025-01-02':{'2025-01-09'}})
        p=zb.Sim(m,self.bars(),['G','R','R'],cfg,boundaries=['2025-01-01']).run({0:['bull']},('naked',),(0,),(None,),progress=False)[0]
        self.assertEqual(p['exit_day'],'2025-01-02');self.assertIn('2025-01-01',p['marks_at'])
    def test_bar_records_minute_coverage(self):
        b=zs.aggregate({'2024-01-02':[(zs.OPEN,1.,2.,.5,1.5)]},45)[0];self.assertEqual(b.minutes,1)

class Options(unittest.TestCase):
    def test_parse_offsets_and_tps(self):
        self.assertEqual(zb.parse_offsets('-400,-300,0,300,400'),(-400,-300,0,300,400))
        self.assertEqual(zb.parse_tps('none,.3,.35,0.45'),(None,.3,.35,.45));self.assertEqual(zb.parse_stops('none,15,30'),(None,15,30))
    def test_summary_periods(self):
        from nifty_iv.zone.legacy import summary as s
        self.assertEqual(s.periods('2021-06-01','2023-01-01'),[('2021-06-01','2022-06-01'),('2022-06-01','2023-01-01')])
if __name__=='__main__':unittest.main()

class PackageLayout(unittest.TestCase):
    """The reorganised package: one release string, the PEP 440 version in pyproject.toml derived from it."""
    def test_pyproject_version_matches_release(self):
        import re,nifty_iv
        v=re.search(r'^version = "([^"]+)"',(Path(__file__).resolve().parents[1]/'pyproject.toml').read_text(),re.M).group(1)
        self.assertEqual(v,nifty_iv.__version__.replace('-','+',1))
