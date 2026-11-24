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

if __name__=='__main__':unittest.main()
