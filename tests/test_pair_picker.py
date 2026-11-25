import csv,importlib.util,os,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('pair_picker',Path(__file__).parents[1]/'scripts'/'pair_picker.py');pp=importlib.util.module_from_spec(spec);spec.loader.exec_module(pp)
H=['2021H2','2022H1','2022H2','2023H1','2023H2','2024H1','2024H2','2025H1','2025H2']

def row(side,cfg,stop,halves,worst=-.05,dd=.1,n=40,oos=.1,structure='naked',level=-1):
    r=dict(side=side,structure=structure,level=level,config=cfg,profit='none',stop=stop,trades=n,worst_trade=worst,train_max_dd=dd,oos_2026H1=oos)
    r.update({f'train_{h}':x for h,x in zip(H,halves)});return r

class PairPicker(unittest.TestCase):
    def run_pick(self,rows,*extra):
        d=tempfile.mkdtemp();c=os.path.join(d,'c.csv');o=os.path.join(d,'p.csv')
        with open(c,'w',newline='') as fh:
            w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
        pp.main(['--candidates',c,'--out',o,*extra]);return list(csv.DictReader(open(o)))
    def test_distinct_and_limits(self):
        good=[.2]*9
        rows=[row('bull','A','s1',good),row('bull','A','s2',[.21]*9),          # same signal, different exit: one of them only
              row('bull','B','s1',[.15]*9),
              row('bull','C','s1',[.5]*9,worst=-.2),                           # beyond the trade-loss limit
              row('bear','X','s1',good),row('bear','Y','s1',[.1]*9),row('bear','Z','s1',[.4]*9,dd=.3)]   # Z beyond --max-dd
        P=self.run_pick(rows,'--top','5')
        self.assertEqual(len(P),2)
        self.assertEqual({p['green_config'] for p in P},{'A','B'});self.assertEqual({p['red_config'] for p in P},{'X','Y'})
        self.assertEqual(P[0]['green_stop'],'s2')                              # the better of the two A exits, paired with the better red
        self.assertEqual(P[0]['red_config'],'X')
        P=self.run_pick(rows,'--distinct','rule','--top','5')
        self.assertEqual(len(P),2)                                             # only 2 reds qualify
    def test_positive_share(self):
        mixed=[.4,.4,.4,.4,.4,.4,.4,.4,-.3]                                     # 8 of 9 positive after adding a red of 0
        rows=[row('bull','A','s',mixed),row('bear','X','s',[0.]*9)]
        self.assertEqual(len(self.run_pick(rows,'--range-cap','1')),1)
        with self.assertRaises(SystemExit):self.run_pick(rows,'--range-cap','1','--min-positive','1')

    def write(self,rows,name):
        d=tempfile.mkdtemp();c=os.path.join(d,name)
        with open(c,'w',newline='') as fh:
            w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
        return c
    def test_half_floor_partial_and_oos(self):
        rows=[row('bull','A','s',[.0]+[.3]*8,oos=.01),row('bull','B','s',[.25]*9,oos=.2),row('bear','X','s',[.0]*9)]
        self.assertEqual(self.run_pick(rows,'--min-half','.26')[0]['green_config'],'A')   # partial 2021H2 skipped; B below .26
        with self.assertRaises(SystemExit):self.run_pick(rows,'--min-half','.26','--floor-partial')  # A's 2021H2 = 0 counts now
        self.assertEqual(self.run_pick(rows,'--min-half','.05','--floor-partial')[0]['green_config'],'B')
        with self.assertRaises(SystemExit):self.run_pick(rows,'--min-half','.26','--include-oos')    # A's 2026H1 1% fails
        with self.assertRaises(SystemExit):self.run_pick(rows,'--min-half','.26','--include-oos','half')
        self.assertEqual(self.run_pick(rows,'--min-half','.26','--include-oos','quarter')[0]['green_config'],'A')   # no quarter floor set
    def test_quarter_floor(self):
        Q=['2021Q3','2021Q4']+[f'{y}Q{q}' for y in range(2022,2026) for q in range(1,5)]
        def qrow(r,qv,oq=(.05,.05)):
            d={k:r[k] for k in ('side','structure','level','config','profit','stop')};d['trades']=r['trades']
            d.update({f'train_{p}':x for p,x in zip(Q,qv)});d.update({'oos_2026Q1':oq[0],'oos_2026Q2':oq[1]});return d
        A=row('bull','A','s',[.3]*9);Bb=row('bull','B','s',[.2]*9);X=row('bear','X','s',[.0]*9)
        rows=[A,Bb,X]
        qa=[.15]*18;qa[5]=.01                                                   # A has one weak quarter (2022Q4)
        qf=self.write([qrow(A,qa),qrow(Bb,[.1]*18),qrow(X,[0.]*18)],'q.csv')
        self.assertEqual(self.run_pick(rows)[0]['green_config'],'A')
        P=self.run_pick(rows,'--min-quarter','.05','--quarters',qf)
        self.assertEqual(P[0]['green_config'],'B');self.assertIn('train_2022Q4',P[0])
        qp=list(qa);qp[5]=.15;qp[0]=-.5                                         # weak only in the partial 2021Q3: skipped by default
        qf2=self.write([qrow(A,qp),qrow(Bb,[.1]*18),qrow(X,[0.]*18)],'q2.csv')
        self.assertEqual(self.run_pick(rows,'--min-quarter','.05','--quarters',qf2)[0]['green_config'],'A')
        with self.assertRaises(SystemExit):self.run_pick(rows,'--min-quarter','.05')              # needs --quarters
        qo=self.write([qrow(A,[.15]*18,(-.1,.2)),qrow(Bb,[.1]*18),qrow(X,[0.]*18)],'q3.csv')     # A: 2026Q1 -10%
        self.assertEqual(self.run_pick(rows,'--min-quarter','.05','--quarters',qo,'--include-oos','half')[0]['green_config'],'A')
        self.assertEqual(self.run_pick(rows,'--min-quarter','.05','--quarters',qo,'--include-oos','quarter')[0]['green_config'],'B')

if __name__=='__main__':unittest.main()

class PairPickerV4(unittest.TestCase):
    """--rank v4: pair drawdown from summed daily equity, worst trade of the worse leg, held-out year's risk columns in --loyo."""
    Q=['2021Q3','2021Q4']+[f'{y}Q{q}' for y in range(2022,2026) for q in range(1,5)];Y=['2021','2022','2023','2024','2025']
    def files(self,rows,risk):
        import numpy as np
        d=tempfile.mkdtemp();c=os.path.join(d,'c.csv');q=os.path.join(d,'q.csv');z=os.path.join(d,'r.npz')
        with open(c,'w',newline='') as fh:w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
        Qr=[]
        for r in rows:
            x={k:r[k] for k in ('side','structure','level','config','profit','stop','trades')};x.update({f'train_{p}':.02 for p in self.Q});x.update({'oos_2026Q1':0,'oos_2026Q2':0});Qr.append(x)
        with open(q,'w',newline='') as fh:w=csv.DictWriter(fh,list(Qr[0]));w.writeheader();w.writerows(Qr)
        keys=['|'.join(str(x) for x in pp.ws.strategy_key(r)) for r in rows]
        np.savez(z,keys=np.array(keys),years=np.array(self.Y),days=np.arange(10),worst=np.array([risk[r['config']][0] for r in rows]),
                 equity=np.array([risk[r['config']][1] for r in rows],np.float32))
        return c,q,z,os.path.join(d,'p.csv')
    def test_drawdown_of_the_sum_and_loyo(self):
        import numpy as np,io,contextlib
        up=np.linspace(0,.3,10);dip=up.copy();dip[4:]-=.15                 # dip: 15% drawdown alone
        hedge=np.zeros(10);hedge[4:]+=.15                                   # offsets the dip exactly
        F=lambda e,w=-.02:(np.full(6,w),np.stack([e]*6))
        rows=[row('bull','A','s',[.2]*9),row('bull','B','s',[.21]*9),row('bear','X','s',[.05]*9),row('bear','Y','s',[.05]*9)]
        risk={'A':F(dip),'B':F(up+.0,-.09),'X':F(hedge),'Y':F(np.zeros(10))}
        c,q,z,o=self.files(rows,risk)
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):pp.main(['--candidates',c,'--out',o,'--quarters',q,'--risk',z,'--rank','v4','--top','1'])
        P=list(csv.DictReader(open(o)))
        self.assertEqual((P[0]['green_config'],P[0]['red_config']),('A','X'))   # A+X: the hedge removes A's drawdown; B has a 9% trade
        self.assertAlmostEqual(float(P[0]['v4_mdd']),0.,places=6);self.assertAlmostEqual(float(P[0]['v4_trade_loss']),.02,places=6)
        self.assertIn('v4 ',buf.getvalue())
        with self.assertRaises(SystemExit):pp.main(['--candidates',c,'--out',o,'--quarters',q,'--rank','v4'])
        wB=np.full(6,-.09);wB[1+self.Y.index('2023')]=-.02                 # without 2023's trades B's worst trade is 2%
        eA=np.stack([dip]*6);eX=np.stack([np.zeros(10)]*6);eX[0]=hedge     # the hedge only exists with every year in
        risk={'A':(np.full(6,-.02),eA),'B':(wB,np.stack([up]*6)),'X':(np.full(6,-.02),eX),'Y':(np.full(6,-.02),np.stack([np.zeros(10)]*6))}
        c,q,z,o=self.files(rows,risk);buf=io.StringIO()
        with contextlib.redirect_stdout(buf):pp.main(['--candidates',c,'--out',o,'--quarters',q,'--risk',z,'--rank','v4','--top','1','--loyo'])
        t=buf.getvalue()
        self.assertIn('  2023: 26/26 ',t)    # without 2023: B's worst trade is 2% and the hedge is gone (A+X 15% drawdown): B is picked
        self.assertIn('  2022: 25/25 ',t)    # without 2022: B's 9% trade (38) costs more than A's 15% drawdown (35): A is picked
