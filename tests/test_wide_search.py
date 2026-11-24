"""V4.9.13 wide search: the exact engine equals trade_structure's simulate / period_returns / stats / objective bit for bit;
stage 1 equals the exact engine and keeps each configuration's true best rules (brute force); the account and basket searches
respect the exposure limit and score what they report. Synthetic only."""
import sys,unittest,tempfile,sqlite3,collections,datetime as dt,io,contextlib,json
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
import numpy as np
from nifty_iv.zone import signals as zs, backtest as zb, paths as zp, grid as zg, rules as zr
from nifty_iv.diagnostics import trade_structure as ts, wide_search as ws
from tests.test_zone import Market,sessions
from tests.test_paths import pcfg


def random_strategy(rnd,R=6,n=None):
    n=n or int(rnd.integers(3,120));sig=sorted(rnd.choice(np.arange(20000),n,replace=False).tolist())
    o0=dt.date(2021,8,1).toordinal();base=np.sort(o0+rnd.integers(0,dt.date(2026,8,1).toordinal()-o0,n))
    XB=np.array(sig)[None,:]+rnd.integers(0,40,(R,n));XD=base[None,:]+rnd.integers(0,60,(R,n))
    P=rnd.normal(0,500,(R,n));P[rnd.random(P.shape)<.05]=np.nan
    keep=rnd.random(n)>.1
    return SimpleNamespace(name='s',side='bull',signals=sig,index={t:i for i,t in enumerate(sig) if keep[i]},
                           gaps={int(t):int(t)+int(rnd.integers(1,50)) for t in rnd.choice(np.arange(20000),15)},XB=XB,XD=XD,P=P,
                           units=rnd.uniform(50,900,n),entry=[dt.date.fromordinal(int(x)).isoformat() for x in base],end='2026-07-01',feat={},
                           pairs=[(f'p{r}','none') for r in range(R)],ref=0,key=('bull','naked',0.,'c','p0','none'))


class EngineV4913(unittest.TestCase):
    def test_engine_equals_trade_structure_exactly(self):
        rnd=np.random.default_rng(1);cap=2.5e6
        for trial in range(120):
            sd=random_strategy(rnd);kind=('half','quarter')[trial%2];per=ws.Periods('2021-09-01','2025-01-01','2026-07-01',kind);E=ws.Engine(sd,per,cap)
            b=list(rnd.integers(0,6,int(rnd.integers(1,6))))
            for excl in (None,'2022','2024'):
                T=ts.simulate(sd,b,per.lo,per.mid,excl);pr=ts.period_returns(T,kind,cap,per.lo,per.mid);got=E.train(b,excl)
                self.assertEqual([pr.get(p,0.) for p in per.train],list(got[0]))
                self.assertEqual(got[1],sum(1 for _ in T))
                self.assertEqual(ts.objective(ts.stats(T,kind,cap,per.lo,per.mid,excl),.3),ws.objective(got[0],per,.3,excl))
            T=ts.simulate(sd,b,per.lo,per.hi);pr=ts.period_returns(T,kind,cap,per.mid,per.hi)
            self.assertEqual([pr.get(p,0.) for p in per.oos],list(E.oos(b)[0]))
            for y in per.years:
                lab=per.held_window(y)[0];pr=ts.period_returns([t for t in T if t['entry'][:4]==y],kind,cap,max(per.lo,f'{y}-01-01'),f'{int(y)+1}-03-01')
                self.assertEqual([pr.get(p,0.) for p in lab],list(E.held(b,y)[0]))
    def test_occupancy_marks_bars_from_entry_to_last_exit(self):
        self.assertEqual(list(ws.occupancy(np.array([2,7]),np.array([4,7]),10)),[0,0,0,1,1,0,0,0,0,0])   # 7..7: entry bar 8 > exit: none


class Fixture(Market):
    def paths_db(self):
        days=sessions('2024-01-02',30);lv=lambda d,mm:20000.+(150. if d>=days[5] else 0.)-(220. if d>=days[17] else 0.)
        m=self.build(days=days,level=lv,extra=('2024-01-18','2024-01-25','2024-02-29'));bars=self.bars_for(days,lv)
        final=(['G']*60+['R']*70+['G']*200)[:len(bars)];bars=[zs.Bar(b.day,b.start,b.end,b.close,b.close+12,b.close-12,b.close) for b in bars]
        rnd=np.random.default_rng(1);cfgs=['zone_only_tw2','N3_k0.25_m8_j0.25_tw2','N5_k1.25_m12_j1.5_tw5']
        sig={c:{s:sorted(rnd.choice(len(bars)-20,9,replace=False).tolist()) for s in ('bull','bear')} for c in cfgs}
        p=Path(tempfile.mkdtemp())/'p.sqlite';db=sqlite3.connect(p);db.executescript(zp.DDL)
        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)])
        db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,s,x) for c,v in sig.items() for s in v for x in v[s]])
        zp.PathSim(m,bars,final,pcfg(lots=3)).run(zb.entry_map(bars,sig,'2024-01-01','2024-12-31'),('naked',),(0,),sink=zp.writer(db),progress=False)
        db.commit();db.close();return p

class Stage1V4913(Fixture):
    def _ctx(self,p,per,K=2):
        db=sqlite3.connect(p);bars=zr.BarArrays.from_db(db);signals=collections.defaultdict(list)
        for cf,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[cf,side].append(t)
        db.close();zg._CTX.clear()
        zg._CTX.update(paths=str(p),bars=bars,signals=dict(signals),configs=sorted({c for c,_ in signals}),per=per,scale=1.,capital=2.5e6,rank='lcb',
                       min_trades=1,cand_min_annual=-9.,per_config=K,compound=False,min_fill_rate=0.,gap_policy='busy',workers=1,mem=2e5,
                       robust=False,max_trade_loss=9.,min_weight=1.)
        return bars,signals
    def test_stage1_keeps_each_configurations_best_rules_and_equals_the_engine(self):
        p=self.paths_db();per=ws.Periods('2024-01-01','2024-01-25','2024-03-01','quarter');K=2;bars,signals=self._ctx(p,per,K)
        res=ws.scan_group(('bull','naked',0.));self.assertTrue(res['rows'])
        S_=ws.scorings(per);ppy=4
        with zg.Detail(str(p),(),'busy') as D:
            b,index=D.group('bull','naked',0.);P=zr.profit_rules('naked');S=zr.stop_rules('naked')
            o=zr.outcomes(b,P,S,[per.ohi]);pairs=[(x,y) for x in P for y in S]
            for cf in zg._CTX['configs']:
                sd=SimpleNamespace(name=cf,signals=list(D.signals[cf,'bull']),index=index,gaps=dict(D.gaps),units=b.units*1.,entry=[q['entry_day'] for q in b.paths],
                                   XB=o['xb'].reshape(len(pairs),b.Np),XD=np.minimum(o['xd'],per.ohi).reshape(len(pairs),b.Np),P=o['vh'][0].reshape(len(pairs),b.Np))
                E=ws.Engine(sd,per,2.5e6);allv={}
                for r in range(len(pairs)):
                    vt,n,_,_,_,_=E.train([r]);vo=E.oos([r])[0];allv.setdefault(vt.tobytes()+vo.tobytes(),(r,vt,vo,n))
                for row in [x for x in res['rows'] if x['config']==cf]:                    # every kept row is exact
                    r=pairs.index((row['profit'],row['stop']));vt,n,_,_,_,_=E.train([r])
                    self.assertEqual(list(vt),row['train']);self.assertEqual(list(E.oos([r])[0]),row['oos']);self.assertEqual(n,row['trades'])
                    sel=dict(side='bull',structure='naked',offset=0.,config=cf,profit=row['profit'],stop=row['stop'])   # worst trade: an independent replay
                    w=0.
                    for x in D.trades(sel,per.lo,per.mid,value_to=per.hi):
                        if not np.isfinite(x['unit']):continue
                        m=x['marks'][np.isfinite(x['marks'])];w=min(w,min(float(m.min()) if len(m) else 0.,x['unit'],0.)*x['units']/2.5e6)
                    self.assertAlmostEqual(row['worst_trade'],w,12)
                y,keep,yrs=S_[0];sc=sorted((float(ws.score(vt[None],np.array([n]),keep,yrs,'lcb',1,-9.,ppy)[0]) for r,vt,vo,n in allv.values()),reverse=True)[:K]
                kept=sorted((x['scores'][0] for x in res['rows'] if x['config']==cf),reverse=True)[:K]
                self.assertEqual(kept,sc)                                                   # the true top K distinct streams


class RiskV4913(unittest.TestCase):
    def test_mark_to_market_equity_equals_a_day_by_day_replay(self):
        rnd=np.random.default_rng(8);per=ws.Periods('2021-09-01','2025-01-01','2026-07-01','half')
        o0=dt.date(2021,8,1).toordinal();per.days=np.array(sorted(set(o0+rnd.choice(1900,1100,replace=False))),np.int64)
        for trial in range(25):
            sd=random_strategy(rnd,R=4,n=40);E=ws.Engine(sd,per,2.5e6);Np=sd.P.shape[1];L=12
            E.J=rnd.integers(0,L,(4,Np));E.u=np.asarray(sd.units);E.W=-rnd.random((4,Np))*.05
            E.row_ord=np.array([np.sort(e+rnd.integers(0,8,L)) for e in E.eord]);E.mf=rnd.normal(0,200,(Np,L))
            E.XD=np.maximum(E.XD,E.row_ord[np.arange(Np)[None,:],E.J])                  # an exit fills on or after its decision day
            b=list(rnd.integers(0,4,int(rnd.integers(1,4))));K=len(b);worst,Q=E.risk(b)
            _,_,_,_,oi,ov=E.run(b,per.olo,per.ohi,per.olo,per.olo,per.tstart[:1]);ref=np.zeros(len(per.days));wref=0.
            for i in oi[ov]:
                if E.eord[i]<per.omid:wref=min(wref,sum(E.W[r,i] for r in b)/K)
                for r in b:
                    j=E.J[r,i]
                    for d_,day in enumerate(per.days):
                        if day<E.row_ord[i,0]:continue
                        if day>=E.XD[r,i]:ref[d_]+=E.PU[r,i]/K;continue
                        rows=[q for q in range(j+1) if E.row_ord[i,q]<=day];ref[d_]+=E.mf[i,rows[-1]]*E.u[i]/K
            self.assertAlmostEqual(worst,wref,12);np.testing.assert_allclose(Q,ref/2.5e6,rtol=1e-12,atol=1e-15)
    def test_drawdown_and_neighbourhood_median(self):
        self.assertEqual(ws.drawdown(np.array([.1,.3,.05,.2,-.1,.4])),.4)
        self.assertEqual(float(ws.drawdown(np.array([-.2,-.1]))),.2)                     # the start counts as the first peak
        cf=['N3_k0.25_m8_j0.25_tw2','N3_k0.5_m8_j0.25_tw2','N3_k0.25_m8_j0.5_tw2','N5_k0.25_m8_j0.25_tw2','zone_only_tw2','zone_only_tw3']
        nb=ws.neighbours(cf);self.assertEqual(sorted(x for x in nb[0] if x>=0),[0,1,2,3]);self.assertEqual(sorted(x for x in nb[4] if x>=0),[4,5])
        sc=np.array([[1.],[3.],[-np.inf],[2.],[5.],[-np.inf]]);r=ws.robust(sc,nb)
        self.assertEqual(r[0,0],2.);self.assertEqual(r[4,0],5.);self.assertTrue(np.isneginf(r[2,0]))    # upper median of 1,3,-inf,2 = 2; of 5,-inf = 5; failing self -> -inf


class AccountV4913(unittest.TestCase):
    def test_greedy_account_respects_exposure_and_reports_its_objective(self):
        rnd=np.random.default_rng(3);per=ws.Periods('2021-09-01','2025-01-01','2026-07-01','half');T=len(per.train);nb=400
        V={i:rnd.normal(.04,.06,T) for i in range(40)};OCC={i:(rnd.random(nb)<rnd.uniform(.1,.6)).astype(np.float32) for i in range(40)}
        a=SimpleNamespace(weights=[.25,.5,1.],min_annual=.2,max_strategies=6,max_exposure=1.,rounds=2,max_trade_loss=9.,max_dd=9.)
        for excl in (None,'2023'):
            ch,obj,steps=ws.build_account(list(V),V,OCC,per,a,excl)
            self.assertTrue(ch);self.assertEqual(obj,ws.objective(ws.account_vec(ch,V),per,a.min_annual,excl))
            self.assertLessEqual(float(sum(w*OCC[c] for c,w in ch).max()),1.+1e-9)
            EQ={i:np.cumsum(rnd.normal(.0004,.01,300)) for i in V};WS={i:-rnd.uniform(0,.08) for i in V};dmask=np.ones(300,bool)
            a.max_trade_loss=9.;a.max_dd=9.;free,_,_=ws.build_account(list(V),V,OCC,per,a,excl,WORST=WS,EQ=EQ,dmask=dmask)
            dd_free=float(ws.drawdown(sum(w*EQ[c] for c,w in free)));a.max_trade_loss=.03;a.max_dd=dd_free/2   # a limit that binds
            chr_,_,_=ws.build_account(list(V),V,OCC,per,a,excl,WORST=WS,EQ=EQ,dmask=dmask)
            self.assertTrue(all(w*WS[c]>=-.03-1e-12 for c,w in chr_))
            if chr_:self.assertLessEqual(float(ws.drawdown(sum(w*EQ[c] for c,w in chr_))),a.max_dd+1e-12)
            a.max_trade_loss=9.;a.max_dd=9.
            single=max(ws.objective(w*V[c],per,a.min_annual,excl) for c in V for w in a.weights if (w*OCC[c]).max()<=1)
            self.assertGreaterEqual(obj,single)
    def test_basket_search_beats_the_reference_and_respects_exposure(self):
        rnd=np.random.default_rng(4);per=ws.Periods('2021-09-01','2025-01-01','2026-07-01','half')
        sd=random_strategy(rnd,R=5,n=80);E=ws.Engine(sd,per,2.5e6)
        a=SimpleNamespace(min_annual=.0,rounds=2,max_exposure=1.,nbars=21000,ntrain=21000,max_trade_loss=9.,max_dd=9.)
        bk,obj=ws.basket_search([(E,1.)],3,per,a)
        self.assertEqual(obj,ws.objective(E.train(bk[0])[0],per,0.))
        self.assertGreaterEqual(obj,ws.objective(E.train([0,0,0])[0],per,0.))
        import itertools                                                                 # never above the exhaustive best
        best=max(ws.objective(E.train(list(b))[0],per,0.) for b in itertools.combinations_with_replacement(range(5),3))
        self.assertLessEqual(obj,best+1e-15)
        a.max_exposure=.5;bk,obj=ws.basket_search([(E,1.)],3,per,a)                      # a full position cannot fit: nothing feasible
        self.assertEqual(obj,-np.inf)


class CommandV4913(Fixture):
    def test_command_runs_end_to_end_and_the_pool_equals_the_engine(self):
        p=self.paths_db();out=Path(tempfile.mkdtemp())/'ws';buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            ws.main(['--paths',str(p),'--train-start','2024-01-01','--split','2024-01-25','--end','2024-03-01','--period','quarter','--min-trades','1',
                     '--cand-min-annual','-9','--min-annual','0.1','--workers','1','--out',str(out),'--baskets','2','--rounds','1','--no-compound',
                     '--max-trade-loss','0.5','--max-dd','0.5'])
        txt=buf.getvalue();self.assertIn('differ from the exact engine for 0',txt)
        f=json.loads((out/'final.json').read_text());self.assertTrue(f['strategies'])
        for n in ('candidates.csv','account.csv','periods.csv','loyo.csv'):self.assertTrue((out/n).exists())
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            ws.main(['--paths',str(p),'--train-start','2024-01-01','--split','2024-01-25','--end','2024-03-01','--period','quarter','--min-trades','1',
                     '--cand-min-annual','-9','--workers','1','--out',str(out),'--stage1-only','--no-compound','--max-trade-loss','0.5','--max-dd','0.5'])
        self.assertIn('2 groups already done, 0 to scan',buf.getvalue())


if __name__=='__main__':unittest.main()
