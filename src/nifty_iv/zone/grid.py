#!/usr/bin/env python3
"""Exhaustive exit-rule grid over the stored paths (nifty-zone-paths), with parameter-stability ranking.

Every signal configuration x side x structure x strike offset x profit rule x stop rule (zone/rules.py lists them;
about 50 profit x 368 stop rules for naked options) is played one position at a time and scored on training
windows. Nothing is re-simulated: each rule's exit is read off the stored bar-by-bar path. Stops are processed one
family at a time (fixed ATR, trailing ATR, breakeven, ...), one .npz per (side, structure, offset, stop family),
with n, total (points per unit after costs), wins and sum of squares per configuration, window, profit, stop.
Windows: train_YYYY = [--start, YYYY-01-01); train_full = [--start, --split); test_YYYY = [YYYY, YYYY+1).
An entry inside a window whose exit falls after the window end is valued at the last bar close before it.

Selection (V4.9.7): a candidate must lie inside the year band (every full training year positive, the worst >= --band-floor x the mean
year); a walk-forward test year needs --min-train-years full years of training before it (with data from Sept 2021 and the holdout from
2025 there is none: the holdout is the forward test); leave one year out (pick on the other full years, score the one left out) is
reported as a diagnostic. Groups whose entries completed less often than --min-fill-rate are untestable and not ranked; the fill
selection-bias check compares, for the same signals, the at-the-money natural-end result where a group filled and where it did not.
Two selection modes:
  total   per (configuration, offset, stop) the profit rule with the highest win rate among rules with --min-trades and
          at least --tp-keep of the best total; the family's pick is the largest positive total (as nifty-zone-legacy-report)
  stable  the combination whose NEIGHBOURHOOD holds up: score = lower quartile (25th percentile) of the totals of the
          combination and its neighbours one grid step away in each input (N, k, m, j, tw; profit level; stop period
          and multiplier), counting only neighbours with --min-trades; at least 3 values. A combination whose profit
          depends on one exact setting scores low even with a high total.
Families: side x structure x stop family (and any) x configurations (all, swing, zone-only: minimum 5 trades).
Integrity: each group file has a SHA-256 in its sidecar; --resume re-hashes before keeping it; report reading checks
every file's hash and, when --paths is given, that it is the paths file the grid was built from.
"""
from __future__ import annotations
import argparse,collections,datetime as dt,hashlib,heapq,json,math,os,sqlite3,statistics,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import rules as zr
from nifty_iv.core import strategy_spec as ss
from nifty_iv.zone.rules import parse,neighbours,config_neighbours,BarArrays,Batch
from nifty_iv.zone.paths import unpack
from nifty_iv.core import cli

PERIODS=zr.ATR_PERIODS
def profit_rules(structure):return zr.profit_rules(structure)
def stop_rules(structure):return zr.stop_rules(structure)
def stop_class(label):return zr.kind_of(label)

def read_json(path):
    with open(path) as f:return json.load(f)
def write_json(obj,path,**kw):
    """Written to a temporary file, then renamed: a reader never sees half a file."""
    tmp=Path(str(path)+'.tmp')
    with open(tmp,'w') as f:json.dump(obj,f,**kw)
    os.replace(tmp,path)
def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<22),b''):h.update(chunk)
    return h.hexdigest()
def save_npz(path,**arrays):
    """Like numpy.savez_compressed but zlib level 1 (several times faster, files a little larger); numpy.load reads it."""
    import zipfile
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as zf:
        for k,a in arrays.items():
            with zf.open(k+'.npy','w',force_zip64=True) as f:np.lib.format.write_array(f,np.asanyarray(a),allow_pickle=False)

GRID_ALGORITHM='4.9.7'   # bump on any change to how stored grid numbers are computed
def algorithm_digest(holidays=(),gap_policy='free'):
    """Binds stored grid numbers to the code that computes them: the rule module, the path reader and layout, sequencing, the
    group computation and the file writer (not the selection/report code, which may change without recomputing)."""
    import inspect;from nifty_iv.zone import paths as zone_paths;from nifty_iv.zone import signals as zone_signals
    h=hashlib.sha256(GRID_ALGORITHM.encode());h.update(Path(zr.__file__).read_bytes());h.update(np.__version__.encode())
    for f in (load_paths,windows,sequence_all,_sequence_all_numpy,_seq_kernel_py,_dedup_rows,gap_occupancy,stop_chunks,_grid_group,save_npz,zone_paths.unpack,zone_paths.pack,
              zone_signals.atr,zone_signals.rma,zone_signals.ema,zone_signals.Bar,zone_signals.minute_of):   # indicator code the stops use
        try:h.update(inspect.getsource(f).encode())
        except (OSError,TypeError):                                   # no source file (e.g. defined interactively): hash the bytecode
            co=getattr(f,'__code__',None);h.update(co.co_code+repr(co.co_consts).encode() if co else repr(f).encode())
    h.update(json.dumps([zone_paths.FLOATS,zone_paths.INTS,zone_paths.SLOTS]).encode());h.update(json.dumps(sorted(holidays)).encode())   # weekend exits depend on it
    h.update(json.dumps([gap_policy,GAP_REASONS,LIQUIDITY_REASONS]).encode())
    for st in ss.NAKED_STRUCTURES+('calendar','futures_zone','futures_expiry'):h.update(json.dumps([zr.profit_rules(st),zr.stop_rules(st)]).encode())
    h.update(Path(ss.__file__).read_bytes())                                          # strikes, expiry policies, margin table
    return h.hexdigest()
rules_digest=algorithm_digest

def load_paths(db,side,structure,offset):
    cols=[r[1] for r in db.execute('PRAGMA table_info(paths)')];opt=lambda c:c if c in cols else 'NULL'
    out={}
    for row in db.execute(f"SELECT signal_bar,status,reason,entry_day,units,first_bar,end_bar,end_pnl,data,pairs,{opt('entry_index')},{opt('entry_short_px')},short_expiry "
                          'FROM paths WHERE side=? AND structure=? AND offset=? ORDER BY signal_bar',(side,structure,offset)):
        t,status,reason,ed,units,fb,eb,pnl,blob,pairs,eix,spx,sx=row
        if status=='no_fill':out[t]=None;continue
        out[t]=dict(side=side,signal_bar=t,first_bar=fb,status=status,reason=reason,entry_day=ed,units=units,end_bar=eb,end_pnl=pnl,pairs=pairs,
                    entry_index=eix if eix is not None else math.nan,entry_short_px=spx,short_expiry=sx if ss.is_naked(structure) or structure=='futures_expiry' else None,rows=unpack(blob,eb-fb+1))
    return out

GAP_REASONS=('not_extracted','no_long_expiry','no_short_expiry','lot_unknown')   # no fill because the DATA is missing, not because the market was thin
LIQUIDITY_REASONS=('no_liquid_entry','zone_flip_before_fill')                     # the market did not trade the full size in the entry windows
GAP_POLICIES=('busy','free')
def gap_occupancy(db,side,structure,offset,policy):
    """{signal bar: bar holding the slot} for signals whose path did not fill.
    Entry not completed (LIQUIDITY_REASONS, V4.9.7): the order was working until the last entry window it tried (the path's end_bar);
    the slot is held to there, then free (always, whatever the policy). Data gap (GAP_REASONS): policy 'busy': a real trader would
    have traded; the slot is held until the zone changes (an upper bound on the occupancy), outcome unknown (never counted);
    'free': V4.9.1 behaviour, the slot stays free."""
    cols=[r[1] for r in db.execute('PRAGMA table_info(paths)')];out={}
    if 'end_bar' in cols:
        q=f"SELECT signal_bar,end_bar FROM paths WHERE side=? AND structure=? AND offset=? AND status='no_fill' AND end_bar IS NOT NULL AND reason IN ({','.join('?'*len(LIQUIDITY_REASONS))})"
        out={t:int(eb) for t,eb in db.execute(q,(side,structure,offset)+LIQUIDITY_REASONS)}
    if policy=='free':return out
    zone=[z for (z,) in db.execute('SELECT zone FROM bars ORDER BY idx')];col='G' if side=='bull' else 'R';flip=[10**9]*len(zone);nxt=10**9
    for k in range(len(zone)-1,-1,-1):flip[k]=nxt;nxt=k if zone[k]!=col else nxt
    q=f"SELECT signal_bar FROM paths WHERE side=? AND structure=? AND offset=? AND status='no_fill' AND reason IN ({','.join('?'*len(GAP_REASONS))})"
    out.update({t:(flip[t]+1 if flip[t]<10**9 else 10**9) for (t,) in db.execute(q,(side,structure,offset)+GAP_REASONS)})
    return out

def fill_stats(db,side,structure,offset):
    """Entry fill statistics of a group: fill rate = filled / (filled + entries not completed for lack of traded volume, or because the
    zone changed first). Data gaps, policy skips and data-end cases are excluded from the rate (they say nothing about liquidity)."""
    c=collections.Counter()
    for status,reason,n in db.execute('SELECT status,reason,COUNT(*) FROM paths WHERE side=? AND structure=? AND offset=? GROUP BY 1,2',(side,structure,offset)):
        if status!='no_fill':c['filled']+=n
        elif reason in LIQUIDITY_REASONS:c['entry_not_completed']+=n;c[reason]+=n
        elif reason in GAP_REASONS:c['data_gap']+=n
        else:c[reason or 'other']+=n
    den=c['filled']+c['entry_not_completed']
    return dict(c,fill_rate=round(c['filled']/den,4) if den else None)

MIN_FILL_RATE=.75   # V4.9.7 (user): a group whose entries completed less often than this is untestable on this data
def testable(e,min_fill_rate=MIN_FILL_RATE):
    """Whether a grid group passes the fill-rate gate (groups from grids without fill statistics pass)."""
    r=e.get('fill_rate');return r is None or r>=min_fill_rate

# ---- single-path helpers (tests, ad hoc checks) ----
def Bars(db):return BarArrays.from_db(db)
def _one(path,bars,sign=None,structure='naked'):
    p=dict(path);r=p['rows'];L=len(r['cash_total'])
    p.setdefault('side','bull' if (sign or 1)>0 else 'bear');p.setdefault('units',1);p.setdefault('status','closed')
    p.setdefault('end_pnl',float(r['cash_total'][L-1]+r['exit_cash'][L-1]));p.setdefault('entry_day',bars.day[min(p['signal_bar']+1,len(bars.day)-1)])
    return Batch([p],bars,structure)
def first_hits(rules,path,bars,sign=1,structure='naked'):return zr.evaluate(rules,_one(path,bars,sign,structure))[:,0]
def outcomes(path,bars,profits,stops,his,structure='naked'):
    o=zr.outcomes(_one(path,bars,None,structure),profits,stops,his)
    return dict(J=o['J'][...,0],pnl=o['pnl'][...,0],xb=o['xb'][...,0],xd=o['xd'][...,0],vh=o['vh'][...,0],hyp=o['hyp'][...,0])

def windows(start,split):
    y0=int(start[:4])+1;y1=int(split[:4])
    w=[(f'train_{y}',start,f'{y}-01-01') for y in range(y0,y1)]+[('train_full',start,split)]
    return w+[(f'test_{y}',f'{y}-01-01',min(f'{y+1}-01-01',split)) for y in range(y0,y1)]

def sequence(signal_bars,idx,XB,VH,ed,wins_def,his_index,R):
    """Reference: one configuration, every rule at once, one position at a time."""
    W=len(wins_def);n=np.zeros((W,R),np.int64);s=np.zeros((W,R));w=np.zeros((W,R),np.int64);ss=np.zeros((W,R));busy=np.full(R,-1,np.int64)
    for t in signal_bars:
        pi=idx.get(t)
        if pi is None:continue
        take=t>busy;busy=np.where(take,XB[pi],busy)
        for wi,(_,lo,hi) in enumerate(wins_def):
            if not lo<=ed[pi]<hi:continue
            v=VH[his_index[hi],pi];ok=take&np.isfinite(v);vz=np.where(ok,v,0.);n[wi]+=ok;s[wi]+=vz;ss[wi]+=vz*vz;w[wi]+=ok&(v>0)
    return n,s,w,ss

def _sequence_all_numpy(by_bar,idx,XB,VH,ed,wins_def,his_index,C,R,gaps=None):
    """Reference implementation (numpy): walk the signal bars in time order and update, as one block, the configurations
    that signalled there. XB (paths, rules) exit bars; VH (window ends, paths, rules) values."""
    W=len(wins_def);n=np.zeros((W,C,R),np.int32);s=np.zeros((W,C,R));w=np.zeros((W,C,R),np.int32);ss=np.zeros((W,C,R))
    busy=np.full((C,R),-1,np.int64)
    for t in sorted(by_bar):
        pi=idx.get(t)
        if pi is None:
            if gaps and t in gaps:cs=by_bar[t];b=busy[cs];busy[cs]=np.where(t>b,gaps[t],b)
            continue
        cs=by_bar[t];b=busy[cs];take=t>b;busy[cs]=np.where(take,XB[pi][None,:],b)
        for wi,(_,lo,hi) in enumerate(wins_def):
            if not lo<=ed[pi]<hi:continue
            v=VH[his_index[hi],pi][None,:];ok=take&np.isfinite(v);vz=np.where(ok,v,0.)
            n[wi,cs]+=ok;s[wi,cs]+=vz;ss[wi,cs]+=vz*vz;w[wi,cs]+=ok&(v>0)
    return n,s,w,ss

try:                                   # optional accelerator (numba): same arithmetic in the same order per (configuration, rule)
    if os.environ.get('ZONE_NO_NUMBA'):raise ImportError
    import numba as _nb
    _prange=_nb.prange;HAVE_NUMBA=True
except ImportError:
    _nb=None;_prange=range;HAVE_NUMBA=False

def _seq_kernel_py(ts,cptr,cbar,pix,gx,XB,VH,inw,wh,C,R):
    """Per-configuration sequencing loop (compiled by numba when available; hashed into the grid lineage either way)."""
    W=inw.shape[1];n=np.zeros((C,R,W),np.int32);s=np.zeros((C,R,W));w=np.zeros((C,R,W),np.int32);ss=np.zeros((C,R,W))
    for c in _prange(C):                          # configurations are sequenced independently: one thread each
        busy=np.full(R,-1,np.int64)
        for a in range(cptr[c],cptr[c+1]):
            q=cbar[a];pi=pix[q];t=ts[q]
            if pi<0:
                if gx[q]>=0:                              # data gap (gap policy 'busy'): the slot is held to the estimated natural end, no value
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
_seq_kernel=_nb.njit(parallel=True)(_seq_kernel_py) if HAVE_NUMBA else None

def _dedup_rows(J):
    """Unique rows of the exit-row matrix J (rules, paths) by exact int64 bytes: (index of each unique row's first rule,
    unique-row index of every rule). XB and the window values are functions of the exit row alone."""
    J=np.ascontiguousarray(J,np.int64);seen={};first=[];inv=np.empty(len(J),np.int64)
    for r in range(len(J)):
        u=seen.setdefault(J[r].tobytes(),len(first))
        if u==len(first):first.append(r)
        inv[r]=u
    return np.array(first,np.int64),inv

def sequence_all(by_bar,idx,XB,VH,ed,wins_def,his_index,C,R,gaps=None):
    """sequence() for every configuration at once, one position at a time per (configuration, rule). Returns n, total, wins,
    sum of squares, each (windows, configurations, rules). Uses the numba kernel when numba is installed (identical results;
    set ZONE_NO_NUMBA=1 to force numpy)."""
    if _seq_kernel is None:return _sequence_all_numpy(by_bar,idx,XB,VH,ed,wins_def,his_index,C,R,gaps)
    W=len(wins_def);ts=np.array(sorted(by_bar),np.int64)
    if not len(ts):z=np.zeros((W,C,R));return z.astype(np.int32),z,z.astype(np.int32),z.copy()
    sizes=np.array([len(by_bar[t]) for t in ts],np.int64);cfg=np.concatenate([np.asarray(by_bar[t],np.int64) for t in ts])
    bar_of=np.repeat(np.arange(len(ts)),sizes);order=np.argsort(cfg,kind='stable')          # per configuration, its signal bars in time order
    cbar=bar_of[order].astype(np.int64);cptr=np.zeros(C+1,np.int64);np.add.at(cptr,cfg+1,1);cptr=np.cumsum(cptr)
    pix=np.array([idx.get(int(t),-1) for t in ts],np.int64);gx=np.array([(gaps or {}).get(int(t),-1) for t in ts],np.int64)
    inw=np.array([[lo<=e<hi for (_,lo,hi) in wins_def] for e in ed],bool).reshape(len(ed),W);wh=np.array([his_index[hi] for (_,_,hi) in wins_def],np.int64)
    n,s,w,ss=_seq_kernel(ts,cptr,cbar,pix,gx,np.ascontiguousarray(XB,np.int64),np.ascontiguousarray(VH,np.float64),inw,wh,C,R)
    tr=lambda a:np.ascontiguousarray(a.transpose(2,0,1))
    return tr(n),tr(s),tr(w),tr(ss)

def stop_chunks(stops):
    """Stop labels grouped by storage chunk (family; layered policies by their mandatory-exit family), in list order."""
    out=collections.OrderedDict()
    for i,s in enumerate(stops):out.setdefault(zr.chunk_of(s),[]).append(s)
    return out

_CTX={}   # read-only state shared with forked workers

def _grid_group(g):
    """All stop families of one (side, structure, offset). Runs in a worker; returns its index entries."""
    c=_CTX;side,structure,offset=g
    if _nb is not None:_nb.set_num_threads(max(1,min(_nb.config.NUMBA_NUM_THREADS,(os.cpu_count() or 1)//max(1,c.get('workers',1)))))   # no oversubscription across workers
    db=sqlite3.connect(f'file:{c["paths_db"]}?mode=ro',uri=True);out_dir=c['out_dir'];configs=c['configs'];wins_def=c['wins_def']
    P=profit_rules(structure);chunks=stop_chunks(stop_rules(structure)+(zr.compound_rules(structure) if c['compound'] else []));batch=None;FP=None;built=False;meta=None;index=[];t0=time.time()
    PC=[x for x in zr.compound_profits(structure) if x in P];pc_idx=np.array([P.index(x) for x in PC])
    for kind,S in chunks.items():
        name=f'grid_{side}_{structure}_{int(offset):+d}__{kind}.npz';side_file=out_dir/(name+'.json')
        if c['resume'] and (out_dir/name).exists() and side_file.exists():
            e=read_json(side_file)
            if (e.get('paths_sha256'),e.get('rules_sha256'),e.get('start'),e.get('split'))==(c['sha'],c['rsha'],c['start'],c['split']) and digest(out_dir/name)==e.get('file_sha256'):
                index.append(e);continue
            print(f'{name}: sidecar or file does not match, recomputing',flush=True)
        if not built:
            built=True;paths=load_paths(db,side,structure,offset);filled=[p for p in paths.values() if p is not None]
            idx={p['signal_bar']:i for i,p in enumerate(filled)};ed=[p['entry_day'] for p in filled]
            batch=Batch(filled,c['bars'],structure) if filled else None;FP=zr.evaluate(P,batch) if batch else None
            gaps=gap_occupancy(db,side,structure,offset,c['gap_policy'])
            by_bar=collections.defaultdict(list)
            for ci,cf in enumerate(configs):
                for t in c['signals'].get((cf,side),()):by_bar[t].append(ci)
            by_bar={t:np.array(v) for t,v in by_bar.items()}
            meta=dict(paths=len(paths),filled=len(filled),**{k:v for k,v in fill_stats(db,side,structure,offset).items() if k!='filled'},hypothetical_ends=sum(p['status']=='open_at_data_end' for p in filled),
                      unresolved=sum(p['status']=='unresolved' for p in filled),gap_signals_holding_slot=len(gaps),partial_unwound=sum('partial_entry' in (p['reason'] or '') for p in filled))
        Pk=PC if kind.startswith('compound') else P          # layered policies use the compact profit set
        R=len(Pk)*len(S)
        if batch is None:n=np.zeros((len(wins_def),len(configs),R),np.int32);s_=np.zeros(n.shape);w=np.zeros(n.shape,np.int32);ss=np.zeros(n.shape)
        else:
            FPk=FP[pc_idx] if kind.startswith('compound') else FP
            FS=zr.evaluate(S,batch);J=np.minimum(FPk[:,None,:],FS[None,:,:]).reshape(R,batch.Np)
            first,inv=_dedup_rows(J);Ju=J[first];ar=np.arange(batch.Np)[None,:]          # V4.9.13: rules with identical exit rows sequence identically
            XB=batch.xb[ar,Ju].T                                                       # (V4.9.1: the unused per-rule pnl / exit-day copies are no longer built)
            VH=np.stack([batch.row_value(h)[ar,Ju].T for h in c['his_ord']])
            n,s_,w,ss=sequence_all(by_bar,idx,XB,VH,ed,wins_def,c['his_index'],len(configs),len(first),gaps)
            if len(first)<R:n,s_,w,ss=(a[:,:,inv] for a in (n,s_,w,ss))
        if n.max(initial=0)>np.iinfo(np.int16).max:raise OverflowError('trade count exceeds int16')
        shape=(len(wins_def),len(configs),len(Pk),len(S));tr=lambda a:a.reshape(shape).transpose(1,0,2,3)
        tmp=out_dir/(name+'.tmp.npz')
        save_npz(tmp,n=tr(n).astype(np.int16),total=tr(s_),wins=tr(w).astype(np.int16),sumsq=tr(ss),configs=np.array(configs),profits=np.array(Pk),stops=np.array(S),windows=np.array([x[0] for x in wins_def]))
        os.replace(tmp,out_dir/name)
        e=dict(file=name,side=side,structure=structure,offset=offset,kind=kind,**meta,paths_sha256=c['sha'],rules_sha256=c['rsha'],start=c['start'],split=c['split'],file_sha256=digest(out_dir/name))
        write_json(e,side_file,indent=1);index.append(e)
    if c['progress']:print(f'{side} {structure} {offset:+.0f}: '+(f'{meta["filled"]}/{meta["paths"]} paths (fill rate {meta.get("fill_rate")}), ' if meta else 'all stop families kept from an earlier run, ')+
                           f'{len(configs)} configs x {len(P)} profit x {sum(len(v) for v in chunks.values())} stop rules ({time.time()-t0:.0f}s)',flush=True)
    db.close();return index

def pool_map(fn,items,workers):
    """map in forked worker processes (shared read-only state in _CTX), in input order; serial when workers <= 1."""
    if workers<=1 or len(items)<=1:return [fn(x) for x in items]
    import multiprocessing as mp
    with mp.get_context('fork').Pool(min(workers,len(items)),maxtasksperchild=4) as pool:return pool.map(fn,items,chunksize=1)

HOLDOUT_START='2025-01-01'
def run_grid(paths_db,out_dir,start,split,structures=None,progress=True,resume=False,workers=1,compound=True,holidays=(),gap_policy='busy',allow_holdout=False):
    if split>HOLDOUT_START and not allow_holdout:raise ValueError(f'split {split} is later than {HOLDOUT_START}: it would score holdout outcomes in training')
    holidays=sorted(holidays);db=sqlite3.connect(f'file:{paths_db}?mode=ro',uri=True);bars=BarArrays.from_db(db,holidays);out_dir=Path(out_dir);out_dir.mkdir(exist_ok=True,parents=True)
    signals=collections.defaultdict(list)
    for c,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[c,side].append(t)
    configs=sorted({c for c,_ in signals});wins_def=windows(start,split)
    his=sorted({hi for _,_,hi in wins_def})
    groups=db.execute('SELECT DISTINCT side,structure,offset FROM paths ORDER BY 1,2,3').fetchall();db.close()
    if structures:groups=[g for g in groups if g[1] in structures]
    sha=digest(paths_db);rsha=algorithm_digest(holidays,gap_policy)
    _CTX.clear();_CTX.update(paths_db=str(paths_db),out_dir=out_dir,bars=bars,signals=dict(signals),configs=configs,wins_def=wins_def,his_ord=[dt.date.fromisoformat(h).toordinal() for h in his],
                             his_index={h:i for i,h in enumerate(his)},workers=workers,gap_policy=gap_policy,sha=sha,rsha=rsha,start=start,split=split,resume=resume,progress=progress,compound=compound)
    index=[e for part in pool_map(_grid_group,groups,workers) for e in part]
    write_json(dict(paths_db=str(paths_db),paths_sha256=sha,rules_sha256=rsha,start=start,split=split,windows=wins_def,holidays=holidays,gap_policy=gap_policy,groups=index),out_dir/'grid_index.json',indent=1)
    return out_dir

# ---------------- selection ----------------
def tstat(n,s,ss):
    n=np.asarray(n,float);mean=np.divide(s,n,out=np.zeros_like(n),where=n>0);var=np.divide(ss-n*mean**2,n-1,out=np.zeros_like(n),where=n>1)
    sd=np.sqrt(np.maximum(var,0));return np.divide(mean*np.sqrt(n),sd,out=np.full_like(n,np.nan),where=sd>0)

def _choose(n,tot,w,ss,mt,keep):
    """(C,P,S) arrays -> per (config, stop): profit index by the take-profit rule, and that cell's stats; eligibility."""
    n=n.astype(np.float64);w=w.astype(np.float64);tot=np.asarray(tot,np.float64);ss=np.asarray(ss,np.float64)
    valid=n>=mt;neg=np.full_like(tot,-np.inf)
    best=np.where(valid,tot,neg).max(1);win=np.divide(w,n,out=np.zeros_like(n),where=n>0)
    ok=valid&(tot>=keep*best[:,None,:]);maxwin=np.where(ok,win,-1).max(1);ok2=ok&(win>=maxwin[:,None,:]-1e-12)
    pick=np.where(best>0,np.where(ok2,tot,neg).argmax(1),np.where(valid,tot,neg).argmax(1))
    take=lambda a:np.take_along_axis(a,pick[:,None,:],1)[:,0,:]
    return pick,take(n),take(tot),take(w),take(ss),np.isfinite(best)
def choose(G,wi,mt,keep,cfg_mask,stop_mask):
    pick,n,tot,w,ss,fin=_choose(G['n'][:,wi],G['total'][:,wi],G['wins'][:,wi],G['sumsq'][:,wi],mt,keep)
    return pick,n,tot,w,ss,fin&cfg_mask[:,None]&stop_mask[None,:]

def stability(n,tot,mt,CN,PN,SN,rows=None,min_share=.5,return_coverage=False):
    """Conservative lower quartile of the training totals of each combination and its neighbours one grid step away: the
    floor((n-1)/4)-th smallest of the n valid values (the minimum when n <= 4), not an interpolated percentile (only those with >= mt
    trades count). A combination is scored when it has >= mt trades, a positive total, at least one valid neighbour, and valid
    values for at least `min_share` of the neighbours that exist in the grid (an edge setting with one neighbour needs that one).
    Returns (score, share of positive values[, coverage = valid neighbours / existing neighbours]). Missing index -1 = NaN pad."""
    C,P,S=tot.shape;V=np.full((C+1,P+1,S+1),np.nan);V[:C,:P,:S]=np.where(n>=mt,tot,np.nan)
    score=np.full(tot.shape,np.nan);share=np.full(tot.shape,np.nan);cover=np.full(tot.shape,np.nan)
    ok=np.isfinite(V[:C,:P,:S])&(V[:C,:P,:S]>0)
    if rows is not None:ok&=rows[:,None,None]
    ci,pi,si=np.nonzero(ok)
    if not len(ci):return (score,share,cover) if return_coverage else (score,share)
    ci=ci.astype(np.int32);pi=pi.astype(np.int32);si=si.astype(np.int32)
    S1=np.int32(S+1);PS1=np.int32((P+1)*(S+1));Vf=V.ravel();base=ci*PS1+pi*S1+si
    CNr=np.where(CN<0,C,CN).astype(np.int32);PNr=np.where(PN<0,P,PN).astype(np.int32);SNr=np.where(SN<0,S,SN).astype(np.int32)
    dC=(CNr-np.arange(C,dtype=np.int32)[:,None])*PS1;dP=(PNr-np.arange(P,dtype=np.int32)[:,None])*S1;dS=SNr-np.arange(S,dtype=np.int32)[:,None]
    exist=(CN>=0).sum(1)[ci]+(PN>=0).sum(1)[pi]+(SN>=0).sum(1)[si]                # neighbours that exist in the grid
    K=1+CN.shape[1]+PN.shape[1]+SN.shape[1];X=np.empty((K,len(ci)));X[0]=np.take(Vf,base);j=1
    for k in range(CN.shape[1]):np.take(Vf,base+dC[ci,k],out=X[j]);j+=1
    for k in range(PN.shape[1]):np.take(Vf,base+dP[pi,k],out=X[j]);j+=1
    for k in range(SN.shape[1]):np.take(Vf,base+dS[si,k],out=X[j]);j+=1
    X=np.ascontiguousarray(X.T);cnt=np.isfinite(X).sum(1);pos=np.greater(X,0).sum(1)/np.maximum(cnt,1)
    X.sort(1);q=np.take_along_axis(X,((np.maximum(cnt,1)-1)//4)[:,None],1)[:,0]   # 25th percentile: floor(0.25*(count-1))
    valid_nb=cnt-1;keep=(valid_nb>=1)&(valid_nb>=np.ceil(min_share*exist))
    q[~keep]=np.nan;score[ci,pi,si]=q;share[ci,pi,si]=pos;cover[ci,pi,si]=valid_nb/np.maximum(exist,1)
    return (score,share,cover) if return_coverage else (score,share)

FILTERS={'all':lambda c:True,'swing':lambda c:not c.startswith('zone_only'),'zone_only':lambda c:c.startswith('zone_only')}
MODES=('total','stable')

def verify(out_dir,paths_db=None):
    """Lineage check before any report: every group file matches its sidecar, rules unchanged, the paths file matches."""
    idx=read_json(Path(out_dir)/'grid_index.json');bad=[]
    for e in idx['groups']:
        f=Path(out_dir)/e['file']
        if not f.exists() or digest(f)!=e['file_sha256']:bad.append(e['file'])
        if e.get('paths_sha256')!=idx['paths_sha256'] or (e.get('start'),e.get('split'))!=(idx['start'],idx['split']):bad.append(e['file']+' (lineage)')
    if bad:raise SystemExit(f'Grid files do not match their recorded checksums or lineage: {bad[:5]}{" ..." if len(bad)>5 else ""}. Rerun nifty-zone-grid (with --resume).')
    if idx.get('rules_sha256')!=algorithm_digest(idx.get('holidays',()),idx.get('gap_policy','free')):raise SystemExit('The grid computation (rules, path layout or aggregation code) changed since this grid was built: rerun nifty-zone-grid.')
    if paths_db and digest(paths_db)!=idx['paths_sha256']:raise SystemExit(f'{paths_db} is not the paths file this grid was built from.')
    return idx

def window_arrays(Z,wn,wname,full_years=()):
    """(n, total, wins, sumsq) of a window, each (configs, profits, stops). loyo_YYYY (leave one year out, V4.9.7): the sum of the full
    years' in-sample windows other than YYYY (the grid's continuous one-position sequence, sliced by year: additive)."""
    if wname.startswith('loyo_'):
        ix=[wn.index(f'test_{y}') for y in full_years if y!=wname[5:]]
        return (Z['n'][:,ix].astype(np.int32).sum(1),Z['total'][:,ix].sum(1),Z['wins'][:,ix].astype(np.int32).sum(1),Z['sumsq'][:,ix].sum(1))
    wi=wn.index(wname);return tuple(Z[k][:,wi] for k in ('n','total','wins','sumsq'))

BAND_FLOOR=.70   # V4.9.7 (user): the worst full training year must be at least 70% of the average full year (and every year positive)
def band_mask(Z,wn,years,floor=BAND_FLOOR):
    """(configs, profits, stops) bool: every listed year's total > 0 and the smallest >= floor x their mean. No years: all True."""
    if not years:return np.ones(Z['total'].shape[:1]+Z['total'].shape[2:],bool)
    lo=None;sm=None;pos=None
    for y in years:
        t=Z['total'][:,wn.index(f'test_{y}')]
        lo=t.copy() if lo is None else np.minimum(lo,t);sm=t.astype(np.float64) if sm is None else sm+t;pos=(t>0) if pos is None else pos&(t>0)
    return pos&(lo>=floor*sm/len(years)-1e-9)

def _scan_file(e):
    """One group file: per family, window and subset the best candidate in both modes; breadth; top stable list."""
    c=_CTX;wn,train,keep,top,years,zmt=c['wn'],c['train'],c['keep'],c['top'],c['years'],c['zone_min_trades']
    with np.load(c['out_dir']/e['file']) as z:Z={k:z[k] for k in ('n','total','wins','sumsq','configs','profits','stops')}
    configs=list(Z['configs']);P=list(Z['profits']);S=list(Z['stops']);key=tuple(configs)
    if c.get('cn_key')!=key:c['cn_key']=key;c['CN']=config_neighbours(configs);c['masks']={k:np.array([f(x) for x in configs]) for k,f in FILTERS.items()}
    CN,masks=c['CN'],c['masks'];PN=neighbours(P);side,st,kind=e['side'],e['structure'],e['kind'];best={};breadth=None;tops=[]
    SN=zr.compound_neighbours(S) if kind.startswith('compound') else neighbours(S)
    cls_of=lambda:('compound','any_incl_compound') if kind.startswith('compound') else (kind,'any','any_incl_compound')
    def put(k,cand):
        if k not in best or (cand['rank'],cand['total'])>(best[k]['rank'],best[k]['total']):best[k]=cand
    def mk(ci,pi,si,N,T,W,SS,rank,**extra):
        return dict(file=e['file'],side=side,structure=st,offset=e['offset'],kind=kind,config=configs[ci],profit=P[pi],stop=S[si],ci=int(ci),pi=int(pi),si=int(si),
                    n=int(N),total=round(float(T),1),win=round(float(W/N),3),t=round(float(tstat(N,T,SS)),2),rank=float(rank),**extra)
    for wname,mt in train:
        N,T,W,SS=window_arrays(Z,wn,wname,c['full_years'])
        B=band_mask(Z,wn,c['band'].get(wname,()),c['band_floor'])                 # V4.9.7: every full training year positive, worst >= floor x mean
        NB=np.where(B,N,0)                                                        # total mode: only cells inside the band are eligible
        by_mt=collections.defaultdict(list)
        for subset in FILTERS:by_mt[zmt if subset=='zone_only' else mt].append(subset)
        for m_,subsets in by_mt.items():
            pick,n2,t2,w2,ss2,fin=_choose(NB,T,W,SS,m_,keep)
            rows=None if any(x!='zone_only' for x in subsets) else masks['zone_only']
            score,share,cover=stability(N,T,m_,CN,PN,SN,rows=rows,return_coverage=True)
            for subset in subsets:
                cm=masks[subset];c2=fin&cm[:,None]&(n2>=m_)&(t2>0)
                if c2.any():
                    ci,si=np.unravel_index(int(np.where(c2,t2,-np.inf).argmax()),c2.shape);pi=int(pick[ci,si])
                    cand=mk(ci,pi,si,n2[ci,si],t2[ci,si],w2[ci,si],ss2[ci,si],t2[ci,si],min_trades=m_)
                    for cls in cls_of():put((side,st,cls,subset,'total',wname),cand)
                ok=cm[:,None,None]&np.isfinite(score)&(score>0)&B                 # the centre must be inside the band; neighbours need not be
                if ok.any():
                    flat=np.where(ok,score,-np.inf);ci,pi,si=np.unravel_index(int(flat.argmax()),flat.shape)
                    cand=mk(ci,pi,si,N[ci,pi,si],T[ci,pi,si],W[ci,pi,si],SS[ci,pi,si],score[ci,pi,si],stability=round(float(score[ci,pi,si]),1),positive_share=round(float(share[ci,pi,si]),3),
                            neighbour_coverage=round(float(cover[ci,pi,si]),3),min_trades=m_)
                    for cls in cls_of():put((side,st,cls,subset,'stable',wname),cand)
                if wname=='train_full' and subset=='all':
                    valid=N>=mt;breadth=((side,st,kind),int(valid.sum()),int((valid&(T>0)).sum()),float(np.median(T[valid])) if valid.any() else None,int((valid&B).sum()))
                    k=min(top,int(ok.sum()))
                    if k:
                        flat=np.where(ok,score,-np.inf).ravel()
                        for f_ in np.argpartition(-flat,k-1)[:k]:
                            ci,pi,si=np.unravel_index(f_,score.shape)
                            tops.append(dict(config=configs[ci],offset=e['offset'],profit=P[pi],stop=S[si],kind=kind,n=int(N[ci,pi,si]),total=round(float(T[ci,pi,si]),1),
                                             stability=round(float(score[ci,pi,si]),1),positive_share=round(float(share[ci,pi,si]),3),neighbour_coverage=round(float(cover[ci,pi,si]),3),win=round(float(W[ci,pi,si]/N[ci,pi,si]),3),
                                             years_in_sample={y:round(float(Z['total'][ci,wn.index(f'test_{y}'),pi,si]),1) for y in years},file=e['file'],ci=int(ci),pi=int(pi),si=int(si)))
    base=[]
    if kind=='none' and 'none' in P:          # no-stop, no-target baselines for every zone-only setting, whatever their stability
        pi=P.index('none');wf=wn.index('train_full')
        for ci,cf in enumerate(configs):
            if cf.startswith('zone_only'):
                base.append(dict(side=side,structure=st,offset=e['offset'],config=cf,n=int(Z['n'][ci,wf,pi,0]),total=round(float(Z['total'][ci,wf,pi,0]),1),
                                 **{f'in_sample_{y}':round(float(Z['total'][ci,wn.index(f'test_{y}'),pi,0]),1) for y in years}))
    return best,breadth,(side,st),tops,base

MIN_TRAIN_YEARS=3   # V4.9.7 (user): a walk-forward test year needs at least this many full calendar years of training before it
def full_years_of(years,split):
    """In-sample years whose window is a complete calendar year inside [start, split)."""
    return [y for y in years if f'{int(y)+1}-01-01'<=split]
def plan_windows(years,split,min_trades,min_train_years=MIN_TRAIN_YEARS):
    """Selection windows, their band years, and the walk-forward / leave-one-year-out test years (V4.9.7)."""
    full=full_years_of(years,split);mt_full=min_trades;mt_wf=max(10,min_trades//2)
    before=lambda hi:[y for y in full if f'{int(y)+1}-01-01'<=hi]
    train=[('train_full',mt_full)];band={'train_full':full}
    wf=[y for y in years if len(before(f'{y}-01-01'))>=min_train_years]
    for y in wf:train.append((f'train_{y}',mt_wf));band[f'train_{y}']=before(f'{y}-01-01')
    loyo=full if len(full)>=min_train_years else []
    for y in loyo:train.append((f'loyo_{y}',mt_wf));band[f'loyo_{y}']=[x for x in full if x!=y]
    return dict(train=train,band=band,full_years=full,wf_years=wf,loyo_years=loyo,mt_full=mt_full,mt_wf=mt_wf)

def fill_bias(paths_db,groups):
    """Selection-bias check of the fill rule (V4.9.7). For each option group, the natural-end result per unit (zone change or expiry,
    no rule) of the SAME signals traded at the money (naked, level 0, same side) is compared between the signals this group filled and
    those whose entry it could not complete. Unsequenced, all signals of all configurations. A large gap means that whether this group
    filled says something about how the trade went: its results are biased by the fill rule, whatever its fill rate."""
    db=sqlite3.connect(f'file:{paths_db}?mode=ro',uri=True);out=[];ref={}
    for side in ('bull','bear'):
        ref[side]={t:pnl/u for t,pnl,u in db.execute("SELECT signal_bar,end_pnl,units FROM paths WHERE side=? AND structure='naked' AND offset=0 AND status='closed' AND end_pnl IS NOT NULL AND units>0",(side,))}
    done=set()
    for e in groups:
        side,st,off=e['side'],e['structure'],float(e['offset'])
        if st.startswith('futures') or not ref.get(side) or (side,st,off) in done:continue
        done.add((side,st,off))
        f=[];u=[]
        for t,status,reason in db.execute('SELECT signal_bar,status,reason FROM paths WHERE side=? AND structure=? AND offset=?',(side,st,off)):
            v=ref[side].get(t)
            if v is None or not math.isfinite(v):continue
            if status!='no_fill':f.append(v)
            elif reason in LIQUIDITY_REASONS:u.append(v)
        m=lambda a:statistics.mean(a) if a else None
        t=None
        if len(f)>1 and len(u)>1:
            se=math.sqrt(statistics.variance(f)/len(f)+statistics.variance(u)/len(u))
            t=round((m(f)-m(u))/se,2) if se>0 else None
        out.append(dict(side=side,structure=st,offset=off,fill_rate=e.get('fill_rate'),atm_ref_filled_n=len(f),atm_ref_filled_mean=None if not f else round(m(f),1),
                        atm_ref_not_filled_n=len(u),atm_ref_not_filled_mean=None if not u else round(m(u),1),welch_t=t,flag=bool(t is not None and abs(t)>=2 and min(len(f),len(u))>=5)))
    db.close();return out

def report(out_dir,min_trades=15,keep=.75,paths_db=None,carry=.055,roll_points=0.,top=50,workers=1,zone_min_trades=5,min_train_years=MIN_TRAIN_YEARS,band_floor=BAND_FLOOR,min_fill_rate=MIN_FILL_RATE):
    idx=verify(out_dir,paths_db);out_dir=Path(out_dir);wn=[w[0] for w in idx['windows']];years=[w[5:] for w in wn if w.startswith('test_')]
    W=plan_windows(years,idx['split'],min_trades,min_train_years);train=W['train'];mt_full=W['mt_full'];mt_wf=W['mt_wf']
    seen={};[seen.setdefault((e['side'],e['structure'],float(e['offset'])),e.get('fill_rate')) for e in idx['groups']]
    untestable=[dict(side=k[0],structure=k[1],offset=k[2],fill_rate=r) for k,r in sorted(seen.items()) if r is not None and r<min_fill_rate]
    groups=[e for e in idx['groups'] if testable(e,min_fill_rate)]
    _CTX.clear();_CTX.update(out_dir=out_dir,wn=wn,train=train,keep=keep,top=top,years=years,zone_min_trades=zone_min_trades,band=W['band'],band_floor=band_floor,full_years=W['full_years'])
    best={};breadth=collections.defaultdict(lambda:[0,0,[],0]);tops=collections.defaultdict(list);baselines=[]
    for fbest,fb,grp,ftops,fbase in pool_map(_scan_file,groups,workers):
        baselines+=fbase
        for k,cand in fbest.items():
            if k not in best or (cand['rank'],cand['total'])>(best[k]['rank'],best[k]['total']):best[k]=cand
        if fb:
            b=breadth[fb[0]];b[0]+=fb[1];b[1]+=fb[2];b[3]+=fb[4]
            if fb[3] is not None:b[2].append(fb[3])
        tops[grp]=heapq.nlargest(top,tops[grp]+ftops,key=lambda r:(r['stability'],r['total']))
    # walk-forward and offset neighbours: re-read only the files that hold picks
    cache=collections.OrderedDict()
    def arr(fname):
        if fname not in cache:
            with np.load(out_dir/fname) as z:cache[fname]={k:z[k] for k in ('n','total','wins','sumsq')}
            while len(cache)>4:cache.popitem(last=False)
        return cache[fname]
    def cell(sel,wname):
        A=arr(sel['file']);c=(sel['ci'],wn.index(wname),sel['pi'],sel['si'])
        return dict(n=int(A['n'][c]),total=float(A['total'][c]),wins=int(A['wins'][c]),sumsq=float(A['sumsq'][c]))
    def pooled(cells):
        n=sum(c['n'] for c in cells);s=sum(c['total'] for c in cells);w=sum(c['wins'] for c in cells);ss=sum(c['sumsq'] for c in cells)
        return dict(n=n,total=round(s,1),mean=round(s/n,1) if n else None,win=round(w/n,3) if n else None,t=round(float(tstat(n,s,ss)),2) if n>1 else None)
    by_name={(e['side'],e['structure'],e['kind'],e['offset']):e['file'] for e in idx['groups']}
    def neighbours_offset(sel):
        out={}
        for d in (-100,100,-1,1):                                                    # points mode: +/-100; levels mode: +/-1 level
            f=by_name.get((sel['side'],sel['structure'],sel['kind'],sel['offset']+d))
            if f:out[f'{int(sel["offset"]+d):+d}']=round(cell(dict(sel,file=f),'train_full')['total'],1)
        return out
    fams=[]
    keys=sorted({k[:5] for k in best})
    for side,st,cls,subset,mode in keys:
        tb=best.get((side,st,cls,subset,mode,'train_full'));folds=[];cells=[];lfolds=[];lcells=[]
        for prefix,ys,F,CL in (('train_',W['wf_years'],folds,cells),('loyo_',W['loyo_years'],lfolds,lcells)):
            for y in ys:
                sel=best.get((side,st,cls,subset,mode,f'{prefix}{y}'))
                if not sel:continue
                c=cell(sel,f'test_{y}');CL.append(c)
                F.append(dict(year=y,**{k:sel[k] for k in ('config','offset','profit','stop','n','total','win')},test=pooled([c])))
        if tb:tb=dict(tb,offset_neighbours=neighbours_offset(tb),years_in_sample={y:round(cell(tb,f'test_{y}')['total'],1) for y in W['full_years']})
        fams.append(dict(side=side,structure=st,stops=cls,configs=subset,mode=mode,train_best=tb,walk_forward=folds,walk_forward_pooled=pooled(cells) if cells else None,
                         leave_one_year_out=lfolds,leave_one_year_out_pooled=pooled(lcells) if lcells else None))
    res=dict(min_trades=min_trades,walk_forward_min_trades=mt_wf,zone_only_min_trades=zone_min_trades,tp_keep=keep,windows=idx['windows'],families=fams,baselines=baselines,
             selection=dict(full_years=W['full_years'],band='every full training year positive and the worst >= band_floor x the mean of the full years',band_floor=band_floor,
                            min_train_years=min_train_years,walk_forward_years=W['wf_years'],leave_one_year_out_years=W['loyo_years'],min_fill_rate=min_fill_rate,
                            walk_forward_note=None if W['wf_years'] else f'no walk-forward year: each needs {min_train_years} full calendar years of training before it (full in-sample years: {W["full_years"]}); the holdout is the forward test',
                            leave_one_year_out_note='diagnostic only, never used to choose: pick on the other full years (band over those years), score on the year left out'),
             untestable_groups=untestable,
             breadth=[dict(side=s,structure=t,stops=k,scored=v[0],positive_share=round(v[1]/v[0],3) if v[0] else None,inside_band=v[3],median_of_offset_medians=round(statistics.median(v[2]),1) if v[2] else None)
                      for (s,t,k),v in sorted(breadth.items())],
             stable_candidates={f'{s}_{t}':v for (s,t),v in tops.items()})
    if paths_db:
        test_lo=f'{years[0]}-01-01' if years else None;split=idx['split'];start=idx['start']
        with Detail(paths_db,idx.get('holidays',()),idx.get('gap_policy','free')) as D:
            for f in sorted(fams,key=lambda f:(f['side'],f['structure'])):
                cr=carry if f['structure'].startswith('futures') else 0.
                if f['train_best']:f['train_best_detail']=detail_stats(D.trades(f['train_best'],start,split),start,split,cr)
                for name,src in (('walk_forward_detail','walk_forward'),('leave_one_year_out_detail','leave_one_year_out')):
                    oos=[]
                    for fold in f[src]:oos+=D.trades(dict(fold,side=f['side'],structure=f['structure']),f"{fold['year']}-01-01",min(f"{int(fold['year'])+1}-01-01",split))
                    if oos:f[name]=detail_stats(oos,f"{f[src][0]['year']}-01-01",split,cr)
            res['buy_and_hold']={name:buy_and_hold(D.bars,lo,hi,carry,roll_points) for name,lo,hi in [('train_full',start,split)]+[(f'test_{y}',f'{y}-01-01',min(f'{int(y)+1}-01-01',split)) for y in years]+([('walk_forward_years',test_lo,split)] if test_lo else [])}
        res['fill_selection_bias']=fill_bias(paths_db,idx['groups'])
        res['buy_and_hold_assumptions']=dict(carry_per_year=carry,roll_points=roll_points,note='All futures figures use spot prices (no futures data): `points` / `total` are gross. '
            'The same assumed carry rate is applied to both: buy and hold pays it every day (net_points); a futures pick pays it only while held, long pays and short receives '
            '(total_net_of_assumed_carry in its detail). Roll costs use roll_points per month for buy and hold only; futures picks hold one contract at a time and their exits are their rolls.')
    for f in fams:
        for k in ('train_best',):
            if f[k]:f[k]={x:v for x,v in f[k].items() if x not in ('ci','pi','si','file','rank')}
    write_json(res,out_dir/'grid_report.json',indent=1,default=str)
    with open(out_dir/'grid_walk_forward.csv','w') as fh:
        fh.write('side,structure,stops,configs,mode,min_trades,wf_n,wf_total,wf_mean,wf_win,wf_t,loyo_n,loyo_total,loyo_mean,loyo_win,loyo_t,train_config,train_offset,train_profit,train_stop,train_n,train_total,train_win,train_t,train_stability,train_positive_share,train_neighbour_coverage,offset_neighbours\n')
        for f in fams:
            p=f['walk_forward_pooled'] or {};b=f['train_best'] or {};l=f['leave_one_year_out_pooled'] or {}
            fh.write(','.join(str(x) for x in (f['side'],f['structure'],f['stops'],f['configs'],f['mode'],b.get('min_trades'),p.get('n'),p.get('total'),p.get('mean'),p.get('win'),p.get('t'),
                     l.get('n'),l.get('total'),l.get('mean'),l.get('win'),l.get('t'),
                     b.get('config'),b.get('offset'),b.get('profit'),b.get('stop'),b.get('n'),b.get('total'),b.get('win'),b.get('t'),b.get('stability'),b.get('positive_share'),b.get('neighbour_coverage'),
                     json.dumps(b.get('offset_neighbours','')).replace(',',';')))+'\n')
    with open(out_dir/'stable_candidates.csv','w') as fh:
        fh.write('side,structure,rank,config,offset,profit,stop,n,total,win,stability,positive_share,neighbour_coverage,'+','.join(f'in_sample_{y}' for y in years)+'\n')
        for (s,t),v in sorted(tops.items()):
            for i,r in enumerate(v,1):fh.write(','.join(str(x) for x in (s,t,i,r['config'],r['offset'],r['profit'],r['stop'],r['n'],r['total'],r['win'],r['stability'],r['positive_share'],r['neighbour_coverage'])+tuple(r['years_in_sample'][y] for y in years))+'\n')
    if res.get('fill_selection_bias'):
        with open(out_dir/'grid_fill_bias.csv','w') as fh:
            ks=list(res['fill_selection_bias'][0]);fh.write(','.join(ks)+'\n')
            for r in res['fill_selection_bias']:fh.write(','.join(str(r[k]) for k in ks)+'\n')
    with open(out_dir/'baselines.csv','w') as fh:
        fh.write('side,structure,offset,config,n,total,'+','.join(f'in_sample_{y}' for y in years)+'\n')
        for r in sorted(baselines,key=lambda r:(r['side'],r['structure'],r['offset'],r['config'])):fh.write(','.join(str(r[k]) for k in ('side','structure','offset','config','n','total')+tuple(f'in_sample_{y}' for y in years))+'\n')
    return res

# ---------------- exact detail for selected rules ----------------
class Detail:
    """Re-reads the paths of one group and lists one rule's trades, for statistics the grid does not keep. Keeps the last two
    groups (a green and a red leg alternate without rebuilding). gap_policy as the grid (gap_occupancy)."""
    def __init__(self,paths_db,holidays=(),gap_policy='free'):
        self.db=sqlite3.connect(f'file:{paths_db}?mode=ro',uri=True);self.bars=BarArrays.from_db(self.db,holidays);self.cache=collections.OrderedDict();self.gap_policy=gap_policy
        self.signals=collections.defaultdict(list)
        for c,side,t in self.db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):self.signals[c,side].append(t)
        try:args=json.loads(self.db.execute("SELECT value FROM meta WHERE name='args'").fetchone()[0])
        except Exception:args={}
        self.target_lots=int(args.get('lots') or 0) or None;self.target_units=args.get('target_units');self.target_margin=args.get('target_margin')
        self.target_fraction=float(args.get('target_fraction') or 1.)      # V4.9.7 (absent in older paths files: the whole budget)
    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def group(self,side,st,off):
        key=(side,st,off)
        if key not in self.cache:
            paths=load_paths(self.db,side,st,off);filled=[p for p in paths.values() if p]
            lots=dict(self.db.execute('SELECT signal_bar,lot FROM paths WHERE side=? AND structure=? AND offset=?',key).fetchall())
            self.cache[key]=(Batch(filled,self.bars,st) if filled else None,{p['signal_bar']:i for i,p in enumerate(filled)},lots,gap_occupancy(self.db,side,st,off,self.gap_policy))
            while len(self.cache)>2:self.cache.popitem(last=False)
        self.key=key;self.batch,self.index,self.lots,self.gaps=self.cache[key]
        return self.batch,self.index
    def signal_outcomes(self,sel,value_to):
        """Every signal of the rule in time order, NOT sequenced: ('trade', t, exit bar, trade dict) if its path filled,
        ('gap', t, bar holding the slot, entry day) for a data gap under the 'busy' policy. Trades valued at value_to."""
        b,index=self.group(sel['side'],sel['structure'],float(sel['offset']));h=dt.date.fromisoformat(value_to).toordinal();out=[]
        o=zr.outcomes(b,[sel['profit']],[sel['stop']],[h]) if b is not None else None
        opt=not sel['structure'].startswith('futures')
        for t in self.signals[sel['config'],sel['side']]:
            i=index.get(t) if b is not None else None
            if i is None:
                if t in self.gaps:out.append(('gap',t,self.gaps[t],self.bars.day[min(t+1,len(self.bars.day)-1)]))
                continue
            J=o['J'][0,0];jp=o['FP'][0];js=o['FS'][0];p=b.paths[i]
            j=int(J[i]);L=int(b.L[i]);v=float(o['vh'][0,0,0,i])
            reason=('both_rules_same_bar' if jp[i]==js[i] else 'profit_rule' if j==jp[i] else 'stop_rule') if j<L-1 and j in (jp[i],js[i]) else p['reason']
            inside=b.row_ord[i,:j+1]<h;marks=b.markpu[i,:j+1][inside];mark_ord=b.row_ord[i,:j+1][inside];xd=int(o['xd'][0,0,i])
            stale=int((~np.isfinite(b.R['value'][i,:j+1][inside])).sum()) if opt else 0          # bar closes where a leg had no fresh print
            out.append(('trade',t,int(o['xb'][0,0,i]),dict(signal_bar=t,first_bar=int(b.f[i]),entry_day=p['entry_day'],exit_day=dt.date.fromordinal(min(xd,h)).isoformat() if xd<10**8 else value_to,unit=v,units=float(b.units[i]),
                        mark_ord=mark_ord,hypothetical=xd>=h or bool(o['hyp'][0,0,i]),forced=bool(b.R['exit_forced'][i,j] or b.R['forced_before'][i,j]),reason=reason,pairs=p['pairs'],marks=marks,
                        index=float(b.e0[i]),sign=float(b.sign[i]),lot=self.lots.get(t),stale=stale,marks_n=int(inside.sum()))))
        return out
    def trades(self,sel,lo,hi,flat=False,value_to=None):
        """Trades of one rule entered in [lo, hi), one position at a time, valued at value_to (default hi).
        flat=False: the grid's convention, a continuous strategy (signals before lo can hold the position into the window).
        flat=True: the strategy starts flat at lo (signals whose entry is before lo are ignored), as when it is adopted at lo."""
        out=[];busy=-1
        for kind,t,until,x in self.signal_outcomes(sel,value_to or hi):
            if t<=busy:continue
            day=x if kind=='gap' else x['entry_day']
            if flat and day<lo:continue
            busy=until
            if kind=='trade' and lo<=day<hi:out.append(x)
        return out

def detail_stats(tr,lo,hi,carry=0.):
    if not tr:return dict(n=0)
    u=[x['unit'] for x in tr if math.isfinite(x['unit'])];weeks=collections.defaultdict(float)
    if not u:return dict(n=0)
    for x in tr:
        if math.isfinite(x['unit']):weeks[dt.date.fromisoformat(x['entry_day']).isocalendar()[:2]]+=x['unit']
    w=list(weeks.values());sd=statistics.stdev(w) if len(w)>2 else 0;eq=pk=dd=0.
    for x in u:eq+=x;pk=max(pk,eq);dd=max(dd,pk-eq)
    closed=mpk=mdd=0.;mae=[];mfe=[]                      # bar-close equity: closed trades plus the open trade's mark
    for x in tr:
        m=x['marks'][np.isfinite(x['marks'])]
        if len(m):mae.append(float(min(m.min(),0)));mfe.append(float(max(m.max(),0)))
        for v in np.append(m,x['unit'] if math.isfinite(x['unit']) else []):e=closed+v;mpk=max(mpk,e);mdd=max(mdd,mpk-e)
        if math.isfinite(x['unit']):closed+=x['unit']
    held=[(dt.date.fromisoformat(x['exit_day'])-dt.date.fromisoformat(x['entry_day'])).days for x in tr]
    span=(dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days
    gains=sum(x for x in u if x>0);losses=-sum(x for x in u if x<0)
    return dict(n=len(u),total=round(sum(u),1),mean=round(statistics.mean(u),1),win=round(sum(x>0 for x in u)/len(u),3),t_weekly=round(statistics.mean(w)/(sd/math.sqrt(len(w))),2) if sd else None,
                profit_factor=round(gains/losses,2) if losses else None,worst=round(min(u),1),best=round(max(u),1),max_drawdown=round(dd,1),max_drawdown_mtm=round(mdd,1),
                worst_open_loss=round(min(mae),1) if mae else None,median_mae=round(statistics.median(mae),1) if mae else None,median_mfe=round(statistics.median(mfe),1) if mfe else None,
                median_days_held=statistics.median(held),days_in_market=sum(held),share_of_calendar_days=round(sum(held)/span,3) if span>0 else None,
                hypothetical=sum(x['hypothetical'] for x in tr),forced=sum(x['forced'] for x in tr),exits=dict(collections.Counter(x['reason'] for x in tr)),
                by_year={y:dict(n=len(v),total=round(sum(v),1),win=round(sum(x>0 for x in v)/len(v),3)) for y,v in sorted(_years(tr).items())},
                **_carry(tr,u,held,carry))
def _carry(tr,u,held,carry):
    """Assumed futures carry while held: index x rate x days / 365; a long pays it, a short receives it (spot-proxy futures only)."""
    if not carry:return {}
    c=sum(x.get('sign',1.)*x.get('index',0.)*carry*d/365 for x,d in zip(tr,held) if math.isfinite(x['unit']))
    return dict(assumed_carry_rate=carry,assumed_carry_points=round(c,1),total_net_of_assumed_carry=round(sum(u)-c,1))
def _years(tr):
    out=collections.defaultdict(list)
    for x in tr:
        if math.isfinite(x['unit']):out[x['entry_day'][:4]].append(x['unit'])
    return out

def buy_and_hold(bars,lo,hi,carry=.0,roll_points=0.):
    """Long one unit of index from the first bar close on/after lo to the last before hi. Spot prices: no basis.
    carry: yearly cost of holding a future as a share of the index (an assumption, not data); roll_points per monthly roll."""
    k=[i for i,d in enumerate(bars.day) if lo<=d<hi]
    if not k:return None
    c=bars.close[k];pk=np.maximum.accumulate(c);days=(dt.date.fromisoformat(bars.day[k[-1]])-dt.date.fromisoformat(bars.day[k[0]])).days
    months=len({bars.day[i][:7] for i in k})-1
    return dict(start=bars.day[k[0]],end=bars.day[k[-1]],index_start=round(float(c[0]),1),index_end=round(float(c[-1]),1),points=round(float(c[-1]-c[0]),1),
                max_drawdown=round(float((pk-c).max()),1),rolls=months,roll_cost=round(months*roll_points,1),carry_cost=round(float(c.mean())*carry*days/365,1),calendar_days=days,
                net_points=round(float(c[-1]-c[0])-months*roll_points-float(c.mean())*carry*days/365,1))

def _fmt(p):
    return '-' if not p else f"n {p['n']}, total {p['total']}, win {p['win']}, t {p['t']}"

def print_summary(res):
    """Console summary of the report (plain values; the CSV / JSON files hold everything)."""
    sel=res.get('selection',{})
    if res.get('untestable_groups'):
        print(f"\nUntestable on this data (fill rate < {sel.get('min_fill_rate')}): "+'; '.join(f"{g['side']} {g['structure']} {int(g['offset']):+d} ({g['fill_rate']:.0%})" for g in res['untestable_groups']))
    print(f"\nSelection band: every full year of {sel.get('full_years')} positive, worst >= {sel.get('band_floor')} x the mean year.")
    print(sel.get('walk_forward_note') or f"Walk-forward test years: {sel.get('walk_forward_years')} (each with >= {sel.get('min_train_years')} full training years before it).")
    print('Leave one year out (diagnostic, not a forward test; points per unit; stops=any):')
    for f in res['families']:
        if f['stops']!='any':continue
        b=f['train_best'];yrs=b.get('years_in_sample') if b else None
        pick='no pick inside the band' if not b else (f"{b['config']} level {int(float(b['offset'])):+d} {b['profit']} / {b['stop']}: n {b['n']}, total {b['total']}"+
               (f", years {', '.join(f'{y} {v}' for y,v in yrs.items())}" if yrs else ''))
        wf=f"  WF {_fmt(f['walk_forward_pooled'])}" if f.get('walk_forward_pooled') else ''
        print(f"  {f['side']:4} {f['structure']:14} {f['configs']:9} {f['mode']:6} LOYO {_fmt(f.get('leave_one_year_out_pooled'))}{wf}  | in-sample pick: {pick}")
    flagged=[r for r in res.get('fill_selection_bias',[]) if r['flag']]
    if flagged:
        print('Fill selection bias (ATM natural-end points of the same signals: filled vs not filled; |t| >= 2 flagged):')
        for r in flagged:print(f"  {r['side']} {r['structure']} {int(r['offset']):+d}: fill rate {r['fill_rate']}, filled {r['atm_ref_filled_mean']} (n {r['atm_ref_filled_n']}) vs not {r['atm_ref_not_filled_mean']} (n {r['atm_ref_not_filled_n']}), t {r['welch_t']}")

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True,help='zone_paths.sqlite');ap.add_argument('--out',default='zone_grid')
    ap.add_argument('--start',default='2021-09-01');ap.add_argument('--split',default='2025-01-01')
    ap.add_argument('--holidays',help='Exchange holiday list (one date per line, common formats) for weekend exits; recorded in the grid lineage')
    ap.add_argument('--gap-policy',choices=GAP_POLICIES,default='busy',help="signals that did not fill because data is missing (not_extracted, no_long_expiry, no_short_expiry, lot_unknown): 'busy' holds the slot to the zone change, outcome unknown (default); 'free' leaves it free (V4.9.1). Recorded in the grid lineage")
    ap.add_argument('--no-compound',action='store_true',help='Skip the layered (initial + management + mandatory exit) policies')
    ap.add_argument('--zone-min-trades',type=int,default=5,help='Trade minimum for the zone-only configurations (5 low-support controls; set equal to --min-trades for equal treatment)')
    ap.add_argument('--min-train-years',type=int,default=MIN_TRAIN_YEARS,help='full calendar years of training a walk-forward test year needs before it (V4.9.7 default 3)')
    ap.add_argument('--band-floor',type=float,default=BAND_FLOOR,help='selection band: every full training year positive and the worst >= this share of the mean year (V4.9.7 default 0.70)')
    ap.add_argument('--min-fill-rate',type=float,default=MIN_FILL_RATE,help='fill-rate gate: groups whose entries completed less often are untestable and not ranked (V4.9.7 default 0.75)')
    ap.add_argument('--min-trades',type=int,default=15);ap.add_argument('--tp-keep',type=float,default=.75);ap.add_argument('--top',type=int,default=50,help='Stable candidates kept per side and structure')
    ap.add_argument('--structures',help='Limit to these structures (comma separated)');ap.add_argument('--report-only',action='store_true')
    ap.add_argument('--resume',action='store_true',help='Keep group files already written for the same paths file, rules and windows (checksums verified)')
    ap.add_argument('--carry',type=float,default=.055,help='Assumed yearly futures carry for the buy-and-hold benchmark (not in the data)')
    ap.add_argument('--roll-points',type=float,default=0.,help='Assumed points per unit lost on each monthly futures roll')
    ap.add_argument('--workers',type=int,default=max(1,min(4,(os.cpu_count() or 2)//2)),help='Parallel worker processes (each needs ~1-3 GB of memory)')
    ap.add_argument('--explain',help="side,structure,offset,config,profit,stop: write that combination's trades to CSV (training window) and exit")
    a=ap.parse_args()
    if a.explain:
        side,st,off,cfg,prof,stop=a.explain.split(',');sel=dict(side=side,structure=st,offset=float(off),config=cfg,profit=prof,stop=stop)
        with Detail(a.paths,zr.read_holidays(a.holidays) if a.holidays else (),a.gap_policy) as D:tr=D.trades(sel,a.start,a.split)
        f=Path(a.out);f.mkdir(exist_ok=True,parents=True);name=f/('trades_'+'_'.join(x.replace('.','p') for x in (side,st,off,cfg,prof,stop))+'.csv')
        with open(name,'w') as fh:
            fh.write('signal_bar,entry_day,exit_day,unit_pnl,reason,hypothetical,forced,pairs,worst_open,best_open\n')
            for x in tr:
                m=x['marks'][np.isfinite(x['marks'])]
                fh.write(f"{x['signal_bar']},{x['entry_day']},{x['exit_day']},{x['unit']:.2f},{x['reason']},{int(x['hypothetical'])},{int(x['forced'])},{x['pairs']},{m.min() if len(m) else ''},{m.max() if len(m) else ''}\n")
        print(json.dumps(detail_stats(tr,a.start,a.split),default=str));print('->',name);return
    if a.split>HOLDOUT_START:sys.exit('--split later than 2025-01-01 would score holdout outcomes in training')
    hol=zr.read_holidays(a.holidays) if a.holidays else []
    if a.holidays:
        yrs=sorted({d[:4] for d in hol});print(f'{len(hol)} exchange holidays read from {a.holidays} (years {yrs[0] if yrs else "-"}..{yrs[-1] if yrs else "-"})')
    else:print('No --holidays file: weekend exits use Fridays only (a week whose Friday is a holiday has none)')
    if not a.report_only:run_grid(a.paths,a.out,a.start,a.split,a.structures.split(',') if a.structures else None,resume=a.resume,workers=a.workers,compound=not a.no_compound,holidays=hol,gap_policy=a.gap_policy)
    if not 0<a.band_floor<=1 or not 0<=a.min_fill_rate<=1 or a.min_train_years<1:sys.exit('--band-floor must be in (0, 1], --min-fill-rate in [0, 1], --min-train-years >= 1')
    res=report(a.out,a.min_trades,a.tp_keep,a.paths,a.carry,a.roll_points,a.top,workers=a.workers,zone_min_trades=a.zone_min_trades,min_train_years=a.min_train_years,band_floor=a.band_floor,min_fill_rate=a.min_fill_rate)
    print_summary(res)
    print('\nBuy and hold (per unit, spot):',json.dumps(res.get('buy_and_hold'),indent=None))
    print(f'-> {a.out}/grid_report.json, grid_walk_forward.csv (every stop family), stable_candidates.csv, grid_*.npz')

if __name__=='__main__':main()
