"""scripts/pair_screen.py (user, 2026-11-25): ratios by hand, each screen step, ranking, held-out folds."""
import csv,datetime as dt,importlib.util,io,contextlib,os,tempfile,unittest
from pathlib import Path
import numpy as np
spec=importlib.util.spec_from_file_location('pair_screen',Path(__file__).parents[1]/'scripts'/'pair_screen.py');ps=importlib.util.module_from_spec(spec);spec.loader.exec_module(ps)
H=['2021H2','2022H1','2022H2','2023H1','2023H2','2024H1','2024H2','2025H1','2025H2'];Y=['2021','2022','2023','2024','2025']
DAYS=np.array([dt.date(2021,9,1).toordinal()+7*k for k in range(225)])   # weekly points from Sep 2021 to Oct 2025 (enough for the ratios)

def row(side,cfg,halves,n=40,structure='naked'):
    r=dict(side=side,structure=structure,level=-1,config=cfg,profit='none',stop='s',trades=n,worst_trade=-.05,train_max_dd=.1,oos_2026H1=.05)
    r.update({f'train_{h}':x for h,x in zip(H,halves)});return r
def steady(total,noise=0.,seed=0,dips=()):
    rng=np.random.default_rng(seed);r=np.full(len(DAYS),total/len(DAYS))+rng.normal(0,noise,len(DAYS))
    for k,d in dips:r[k]-=d
    return np.cumsum(r)

class Ratios(unittest.TestCase):
    def test_by_hand(self):
        r=np.array([[.01,-.02,.03,0.]]);dd=np.array([.02])
        sh,so,ca=ps.ratios(r,dd);m=r.mean();self.assertAlmostEqual(sh[0],m/r.std()*np.sqrt(252));self.assertAlmostEqual(so[0],m/np.sqrt(.0004/4)*np.sqrt(252))
        self.assertAlmostEqual(ca[0],m*252/.02);self.assertAlmostEqual(ps.ratios(r,np.array([.001]))[2][0],m*252/.01)   # drawdown floored at 1%

class Screen(unittest.TestCase):
    def run_screen(self,rows,eq,worst,*extra):
        d=tempfile.mkdtemp();c=os.path.join(d,'c.csv');z=os.path.join(d,'r.npz');o=os.path.join(d,'p.csv')
        with open(c,'w',newline='') as fh:w=csv.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
        keys=['|'.join(str(x) for x in ps.ws.strategy_key(r)) for r in rows]
        np.savez(z,keys=np.array(keys),years=np.array(Y),days=DAYS,worst=np.array([np.full(6,worst[r['config']]) for r in rows]),
                 equity=np.array([np.stack([eq[r['config']]]*6) for r in rows],np.float32))
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):ps.main(['--candidates',c,'--risk',z,'--out',o,*extra])
        return list(csv.DictReader(open(o))),buf.getvalue()
    def test_steps_and_ranking(self):
        rows=[row('bull','A',[.03]+[.12]*8),row('bull','B',[.031]+[.121]*8),row('bull','C',[.02]+[.04]*8),  # C: CAGR < 10% (a)
              row('bull','D',[.03]+[.2]*8,n=5),                                                            # D: too few trades (a)
              row('bear','X',[.03]+[.10]*8),row('bear','Y',[.031]+[.101]*8),row('bear','Z',[.03,.3,.3,.3,.3,.1,-.15,.3,.3])]   # Z: pairs' worst half < 2.5%
        eq={'A':steady(.96,.01,1),'B':steady(.96,.01,2,dips=((100,.25),)),'C':steady(.3,.01),'D':steady(1.6,.01),'X':steady(.8,.01,3),'Y':steady(.8,.01,4),'Z':steady(1.8,.01,5)}
        worst={'A':-.05,'B':-.05,'C':-.05,'D':-.05,'X':-.05,'Y':-.12,'Z':-.05}                                # Y: trade loss 12% > 10% (h)
        P,t=self.run_screen(rows,eq,worst,'--top','3','--min-half','.025')
        self.assertIn('green 2, red 3',t)                                          # a: C and D out
        pairs={(p['green_config'],p['red_config']) for p in P}
        self.assertEqual(pairs,{('A','X')})                                        # B: its 25% dip makes every B pair's drawdown > 20%; Y: trade loss; Z: worst half
        self.assertAlmostEqual(float(P[0]['rank_score']),(float(P[0]['sortino'])+float(P[0]['calmar']))/2,places=3)
        self.assertIn('pair drawdown <= 20%',t);self.assertIn('worst trade <= 10.0%',t);self.assertIn('full-year range <= 40%',t)
        import re
        cnt=lambda t,lab:int(re.search(lab+r'\s+(\d+)',t).group(1))
        self.assertEqual(cnt(t,'pair drawdown <= 20%'),cnt(t,r'worst half >= 2\.5%')-2)      # (B,X) and (B,Y) fail on drawdown
        _,t2=self.run_screen(rows,eq,worst,'--max-dd','.5','--top','3')
        self.assertEqual(cnt(t2,'pair drawdown <= 50%'),cnt(t2,r'worst half >= 2\.5%'))     # with the cap relaxed, they pass it
        _,t=self.run_screen(rows,eq,worst,'--loyo');self.assertIn('2023:',t);self.assertIn('held-out basket halves',t)
        with self.assertRaises(SystemExit):self.run_screen(rows,eq,worst,'--pair-cagr','.9')

class StepsJtoL(Screen):
    def test_loyo_cagr_and_half_floor(self):
        import re
        rows=[row('bull','A',[.03]+[.12]*8),row('bull','F',[.03,.6,.6]+[.02]*6),row('bull','G',[.03,.2,.2,.2,.2,.2,-.05,.2,.2]),row('bear','X',[.03]+[.10]*8)]
        eq={k:steady(t,.01,n) for n,(k,t) in enumerate((('A',.96),('F',1.32),('G',1.55),('X',.8)))};worst={k:-.05 for k in eq}
        P,t=self.run_screen(rows,eq,worst,'--top','3','--max-fy-range','2','--max-fy-range2','2')
        cnt=lambda lab:int(re.search(lab+r'\s+(\d+)',t).group(1))
        self.assertEqual(cnt(r'worst half >= 2\.5%'),3);self.assertIn('3 <= 100: no second range cut',t)
        self.assertEqual(cnt('worst leave-one-year-out CAGR >= 20%'),2)        # F+X: without 2022 only 2 x 12% a year -> fails k
        self.assertEqual(cnt('worst half >= 10%'),1)                           # G+X: 2024H2 at +5% fails l
        self.assertEqual([(p['green_config'],p['red_config']) for p in P],[('A','X')])
        self.assertEqual(P[0]['loyo_year'] in ('2022','2023','2024','2025'),True)
        _,t2=self.run_screen(rows,eq,worst,'--top','3','--narrow-above','1','--max-fy-range','2')
        self.assertIn('more than 1: full-year range <= 30%',t2)                # j applies above --narrow-above
