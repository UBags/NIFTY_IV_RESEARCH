"""V4.9.13 speed-ups must give bit-identical results: each test compares the new code with the V4.9.12 algorithm
(kept here as a reference or reproduced by evaluating one rule at a time). Exact equality throughout, no tolerances."""
import sys,unittest,collections,math,heapq
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"src"),str(Path(__file__).resolve().parents[1])]
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import paths as zp
from nifty_iv.zone import rules as zru
from tests.test_paths import GridV491


def old_kernel(ts,cptr,cbar,pix,gx,XB,VH,inw,wh,C,R):
    """V4.9.12 _seq_kernel_py, verbatim (pure Python)."""
    W=inw.shape[1];n=np.zeros((C,R,W),np.int32);s=np.zeros((C,R,W));w=np.zeros((C,R,W),np.int32);ss=np.zeros((C,R,W))
    for c in range(C):
        busy=np.full(R,-1,np.int64)
        for a in range(cptr[c],cptr[c+1]):
            q=cbar[a];pi=pix[q];t=ts[q]
            if pi<0:
                if gx[q]>=0:
                    for r in range(R):
                        if t>busy[r]:busy[r]=gx[q]
                continue
            for r in range(R):
                if t>busy[r]:
                    busy[r]=XB[pi,r]
                    for wi in range(W):
                        if inw[pi,wi]:
                            v=VH[wh[wi],pi,r]
                            if math.isfinite(v):
                                n[c,r,wi]+=1;s[c,r,wi]+=v;ss[c,r,wi]+=v*v
                                if v>0:w[c,r,wi]+=1
    return n,s,w,ss

def old_sequence_all(by_bar,idx,XB,VH,ed,wins_def,his_index,C,R,gaps=None):
    W=len(wins_def);ts=np.array(sorted(by_bar),np.int64)
    sizes=np.array([len(by_bar[t]) for t in ts],np.int64);cfg=np.concatenate([np.asarray(by_bar[t],np.int64) for t in ts])
    bar_of=np.repeat(np.arange(len(ts)),sizes);order=np.argsort(cfg,kind='stable')
    cbar=bar_of[order].astype(np.int64);cptr=np.zeros(C+1,np.int64);np.add.at(cptr,cfg+1,1);cptr=np.cumsum(cptr)
    pix=np.array([idx.get(int(t),-1) for t in ts],np.int64);gx=np.array([(gaps or {}).get(int(t),-1) for t in ts],np.int64)
    inw=np.array([[lo<=e<hi for (_,lo,hi) in wins_def] for e in ed],bool).reshape(len(ed),W);wh=np.array([his_index[hi] for (_,_,hi) in wins_def],np.int64)
    n,s,w,ss=old_kernel(ts,cptr,cbar,pix,gx,np.ascontiguousarray(XB,np.int64),np.ascontiguousarray(VH,np.float64),inw,wh,C,R)
    tr=lambda a:np.ascontiguousarray(a.transpose(2,0,1))
    return tr(n),tr(s),tr(w),tr(ss)


class SequencingV4913(unittest.TestCase):
    def _case(self,rnd,C,Np,Ru,R):
        bars_=np.sort(rnd.choice(np.arange(600),Np,replace=False));idx={int(t):i for i,t in enumerate(bars_)}
        wins=zg.windows('2021-09-01','2025-01-01');his=sorted({w[2] for w in wins});hix={h:i for i,h in enumerate(his)}
        J=rnd.integers(0,30,(Ru,Np))[rnd.integers(0,Ru,R)]                     # duplicate exit rows, as wide stops produce
        xb=bars_[:,None]+rnd.integers(1,70,(Np,30));vt=rnd.normal(0,80,(len(his),Np,30))
        for frac,val in ((.05,np.nan),(.02,np.inf),(.02,-np.inf),(.01,1e150),(.03,0.)):vt[rnd.random(vt.shape)<frac]=val
        ed=[f'{2021+int(t)*4//600}-{1+int(t)%12:02d}-15' for t in bars_]
        gap_bars=[int(x) for x in rnd.choice(np.setdiff1d(np.arange(600),bars_),6,replace=False)]
        by=collections.defaultdict(list)
        for c in range(C):
            pool=np.r_[bars_,gap_bars]
            for t in rnd.choice(pool,min(len(pool),int(rnd.integers(0,30))),replace=False):by[int(t)].append(c)
        by={t:np.array(v) for t,v in by.items()}
        return dict(J=J,xb=xb,vt=vt,by=by,idx=idx,ed=ed,wins=wins,hix=hix,C=C,R=R,Np=Np,gaps={t:t+int(rnd.integers(3,60)) for t in gap_bars})
    def test_tiled_kernel_and_row_dedup_equal_the_old_sequencing(self):
        rnd=np.random.default_rng(4913)
        for trial in range(12):
            k=self._case(rnd,int(rnd.integers(3,25)),int(rnd.integers(5,60)),int(rnd.integers(2,20)),int(rnd.integers(20,150)))
            ar=np.arange(k['Np'])[None,:];H=len(k['vt'])
            XB=k['xb'][ar,k['J']].T;VH=np.stack([k['vt'][h][ar,k['J']].T for h in range(H)])
            for gaps in (None,k['gaps']):
                ref=old_sequence_all(k['by'],k['idx'],XB,VH,k['ed'],k['wins'],k['hix'],k['C'],k['R'],gaps)
                first,inv=zg._dedup_rows(k['J']);Ju=k['J'][first]
                self.assertTrue(np.array_equal(Ju[inv],k['J']))
                XBu=k['xb'][ar,Ju].T;VHu=np.stack([k['vt'][h][ar,Ju].T for h in range(H)])
                for f in (zg.sequence_all,zg._sequence_all_numpy):
                    got=f(k['by'],k['idx'],XB,VH,k['ed'],k['wins'],k['hix'],k['C'],k['R'],gaps)
                    dd=[a[:,:,inv] for a in f(k['by'],k['idx'],XBu,VHu,k['ed'],k['wins'],k['hix'],k['C'],len(first),gaps)]
                    for a,b,c in zip(ref,got,dd):
                        self.assertEqual(a.dtype,b.dtype);self.assertTrue(np.array_equal(a,b,equal_nan=True),(trial,f.__name__))
                        self.assertTrue(np.array_equal(a,c,equal_nan=True),(trial,f.__name__,'dedup'))
    def test_more_rules_than_one_tile(self):
        rnd=np.random.default_rng(7);k=self._case(rnd,9,40,200,200)            # 200 distinct rules: four tiles of 64
        ar=np.arange(k['Np'])[None,:];XB=k['xb'][ar,k['J']].T;VH=np.stack([k['vt'][h][ar,k['J']].T for h in range(len(k['vt']))])
        ref=old_sequence_all(k['by'],k['idx'],XB,VH,k['ed'],k['wins'],k['hix'],k['C'],k['R'],k['gaps'])
        for a,b in zip(ref,zg.sequence_all(k['by'],k['idx'],XB,VH,k['ed'],k['wins'],k['hix'],k['C'],k['R'],k['gaps'])):self.assertTrue(np.array_equal(a,b))


class RulesV4913(GridV491):
    def test_evaluate_with_shared_lookbacks_equals_one_rule_at_a_time(self):
        p,_,_=self._grid();import sqlite3;db=sqlite3.connect(p)
        paths=[x for x in zg.load_paths(db,'bull','naked',0.).values() if x is not None]
        b=zru.Batch(paths,zru.BarArrays.from_db(db),'naked');db.close()
        rules=[r for r in zru.stop_rules('naked') if zru.kind_of(r) in ('chand','donch')]
        self.assertGreater(len(rules),20)
        rnd=np.random.default_rng(3)
        for order in (rules,list(rnd.permutation(rules))):
            all_=zru.evaluate(order,b);one=np.stack([zru.evaluate([r],b)[0] for r in order])
            self.assertTrue(np.array_equal(all_,one))
        full=zru.stop_rules('naked');self.assertTrue(np.array_equal(zru.evaluate(full,b),np.stack([zru.evaluate([r],b)[0] for r in full])))


class PathsV4913(unittest.TestCase):
    def _sim(self,mp):
        sim=zp.PathSim.__new__(zp.PathSim);sim.cfg=SimpleNamespace(max_participation=mp);return sim
    def test_slice_and_caps_equal_the_old_arithmetic(self):
        rnd=np.random.default_rng(12)
        for _ in range(400):
            vol=rnd.choice([0.,1.,24.,25.,49.,75.,130.,300.,1e3,7.5e3],375);px=rnd.uniform(.05,500,375)
            mp=float(rnd.choice([.1,.15,.2,.25,1.]));lot=int(rnd.choice([15,25,50,65,75]))
            d=SimpleNamespace(buy={'k':px},sell={'k':px*.99},vol={'k':vol});sim=self._sim(mp)
            sim.arrays=lambda day,d=d:d;sim.key=lambda c:'k'
            i0=int(rnd.integers(0,375));i1=int(rnd.integers(i0,376));want=int(rnd.integers(0,40))*lot
            v=vol[i0:i1];cap=np.floor(v*mp)//lot*lot                                     # V4.9.12 arithmetic
            if i0>=i1 or want<=0 or cap.sum()<=0:ref=(0,0.,0.,None)
            else:
                cs=np.cumsum(cap);total=int(cs[-1])
                if total<want:j=int(np.flatnonzero(cap)[-1]);taken=cap
                else:j=int(np.searchsorted(cs,want));taken=cap[:j+1].copy();taken[j]=want-(cs[j-1] if j else 0.)
                ref=(int(taken.sum()),float(taken@px[i0:i0+len(taken)]),float(v[:len(taken)][taken>0].sum()),i0+j)
            for buy in (True,False):
                got=sim.slice('day',i0,i1,('x',1.,'CE'),want,lot,buy)
                if buy:self.assertEqual(got,ref)
            if i0<i1:
                c=sim.caps('day',i0,i1,('x',1.,'CE'),lot,True)
                self.assertTrue(np.array_equal(c[0],cap));c[0][:]=-1                      # a fresh writable copy: the cache is untouched
                self.assertTrue(np.array_equal(sim.caps('day',i0,i1,('x',1.,'CE'),lot,True)[0],cap))
    def test_participation_and_lot_are_part_of_the_cache_key(self):
        vol=np.array([100.,260.,0.,75.]);d=SimpleNamespace(vol={'k':vol});sim=self._sim(.25)
        a=sim.capacity(d,'k',25).copy();sim.cfg.max_participation=.5;b=sim.capacity(d,'k',25);c=sim.capacity(d,'k',50)
        self.assertEqual(list(a),[25.,50.,0.,0.]);self.assertEqual(list(b),[50.,125.,0.,25.]);self.assertEqual(list(c),[50.,100.,0.,0.])
    def test_day_cache_is_least_recently_used(self):
        loads=[];sim=zp.PathSim.__new__(zp.PathSim);sim.days=collections.OrderedDict();sim.cache_days=3;sim.cfg=SimpleNamespace()
        sim.m=SimpleNamespace(day=lambda d:loads.append(d) or {})
        for d in ('d1','d2','d3','d1','d4','d1','d2'):sim.arrays(d)
        self.assertEqual(loads,['d1','d2','d3','d4','d2'])                              # d1 kept: it was used after d2


class TradeStructureV4913(unittest.TestCase):
    def test_prior_trade_feature_equals_the_quadratic_scan(self):
        from nifty_iv.diagnostics import trade_structure as ts
        rnd=np.random.default_rng(9)
        for trial in range(30):
            n=int(rnd.integers(5,60));sig=sorted(rnd.choice(np.arange(2000),n,replace=False).tolist());K=int(rnd.integers(1,4))
            base=np.array([700000+int(t)//3 for t in sig])
            if trial%5==4:base=base[rnd.permutation(n)]                                   # out-of-order entries: the fallback path
            XB=np.array(sig)[None,:]+rnd.integers(0,9,(4,n));XD=base[None,:]+rnd.integers(0,12,(4,n));P=rnd.normal(0,1,(4,n))
            sd=SimpleNamespace(name='s',side='bull',signals=sig,index={t:i for i,t in enumerate(sig)},gaps={},XB=XB,XD=XD,P=P,
                               units=np.ones(n),entry=[ts.dt.date.fromordinal(int(x)).isoformat() for x in base],end='9999',
                               feat={t:np.zeros(3) for t in sig})
            basket=list(rnd.integers(0,4,K));out=ts.simulate(sd,basket,'0000','9999')
            for i,t in enumerate(out):                                                    # V4.9.12: the last appended trade closed before entry
                prior=[c for c in out[:i] if max(d for d,_ in c['legs'])<t['entry']]
                self.assertEqual(t['features'][-1],0. if not prior else (1. if prior[-1]['pnl']>0 else -1.))


if __name__=='__main__':unittest.main()
