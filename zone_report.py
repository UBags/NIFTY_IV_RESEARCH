#!/usr/bin/env python3
"""Rank zone + swing configurations on training trades, freeze one per side and structure, and evaluate the
frozen set once on the holdout.

A configuration is: signal config x side x structure x strike offset x take-profit. Its trades are its signal
bars in order, one position at a time (a signal while a position or roll chain is open is skipped). A calendar
roll chain counts as one trade.

Take-profit rule (per signal config, side, structure, offset): among the cut-offs, keep those whose total is
at least --tp-keep of the best total, and choose the highest win rate; ties go to the larger total.
Each cut-off needs --min-trades on its own. Only configurations with a positive training total are frozen.
Each cut-off needs --min-trades on its own. Only configurations with a positive training total are frozen.
Ranking: total net points per unit (P&L / units), with at least --min-trades trades.

Training is entries before --split. A chain still open at the split is closed hypothetically at the last bar-close
prices before it (counted as `hypothetical`). train mode never reads a holdout outcome.
final_holdout evaluates exactly the frozen rows and records the inspection first.
"""
from __future__ import annotations
import argparse,collections,datetime as dt,hashlib,json,math,os,sqlite3,statistics,sys
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()

def family(structure,stop):
    """Structure plus its index stop, e.g. naked|atr30. Each family is ranked and walk-forwarded on its own."""
    return structure if not stop else f'{structure}|atr{int(stop)}'

def load(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    bars={i:d for i,d in db.execute('SELECT idx,day FROM bars')}
    signals=collections.defaultdict(list)
    for c,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY bar'):signals[c,side].append(t)
    chains={};first={}
    has_stop='stop' in [r[1] for r in db.execute('PRAGMA table_info(trades)')]
    for row in db.execute(f"SELECT id,chain,side,structure,offset,tp,{'stop' if has_stop else 'NULL'},signal_bar,roll,entry_day,exit_day,exit_bar,status,unit_pnl,forced,marks_at FROM trades ORDER BY roll"):
        tid,chain,side,st,off,tp,stop,sb,roll,ed,xd,xb,status,u,forced,marks=row
        st=family(st,stop)
        key=(side,st,off,tp,sb)
        if status=='no_fill':first[key]=None;continue
        c=chains.setdefault(chain,dict(entry_day=ed,end_bar=-1,pairs=[],week=dt.date.fromisoformat(ed).isocalendar()[:2]))
        c['pairs'].append(dict(entry_day=ed,exit_day=xd,status=status,unit=u,forced=bool(forced),marks=json.loads(marks) if marks else {}))
        if xb is not None:c['end_bar']=max(c['end_bar'],xb)
        if status=='open_at_data_end':c['end_bar']=10**9
        if roll==0:first[key]=chain
    db.close();return bars,signals,chains,first

def chain_value(c,hi):
    """Per-unit P&L of a chain as of `hi`: realized pairs, plus a hypothetical close of a pair open at `hi`."""
    unit=0.;pair_units=[];hyp=False;forced=False
    for p in c['pairs']:
        if p['entry_day']>=hi:continue
        if p['status']=='closed' and p['exit_day']<hi:unit+=p['unit'];pair_units.append(p['unit']);forced|=p['forced'];continue
        v=p['marks'].get(hi)
        if v is None:return None
        unit+=v;pair_units.append(v);hyp=True
    return dict(unit=unit,pair_units=pair_units,hypothetical=hyp,forced=forced,week=c['week'])

def trades_for(signal_bars,side,st,off,tp,chains,first,bars,lo,hi):
    """Chains taken by one configuration with entries in [lo,hi), valued at hi (open ones closed hypothetically)."""
    out=[];busy=-1;counts=collections.Counter()
    for t in signal_bars:
        if t<=busy:counts['skipped_busy']+=1;continue
        chain=first.get((side,st,off,tp,t))
        if chain is None:counts['no_fill']+=1;continue
        c=chains[chain];busy=c['end_bar']
        if not lo<=c['entry_day']<hi:continue
        v=chain_value(c,hi)
        if v is None:counts['no_value_at_window_end']+=1;continue
        counts['hypothetical_close']+=v['hypothetical'];out.append(v)
    return out,counts

def stats(tr):
    if not tr:return dict(n=0,total=0.,mean=None,win=None,t=None,worst=None)
    u=[c['unit'] for c in tr];weeks=collections.defaultdict(float);pairs=[x for c in tr for x in c['pair_units']]
    for c in tr:weeks[c['week']]+=c['unit']
    w=list(weeks.values());sd=statistics.stdev(w) if len(w)>2 else 0
    eq=0.;peak=0.;dd=0.
    for x in u:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(n=len(u),total=round(sum(u),1),mean=round(statistics.mean(u),2),win=round(sum(x>0 for x in u)/len(u),3),
                t=round(statistics.mean(w)/(sd/math.sqrt(len(w))),2) if sd else None,worst=round(min(u),1),max_drawdown=round(dd,1),weeks=len(w),
                pairs=len(pairs),pair_win=round(sum(x>0 for x in pairs)/len(pairs),3) if pairs else None,
                hypothetical=sum(c['hypothetical'] for c in tr),forced=sum(c['forced'] for c in tr))

NO_TP_QUALIFIES=object()   # distinct from None, which means "no take-profit"

def choose_tp(by_tp,keep,min_trades=0):
    """Highest win rate among cut-offs with enough trades and at least `keep` of the best total.
    Returns NO_TP_QUALIFIES if no cut-off has enough trades; None is a valid answer (no take-profit)."""
    by_tp={k:v for k,v in by_tp.items() if v['n']>=min_trades}
    if not by_tp:return NO_TP_QUALIFIES
    best=max(v['total'] for v in by_tp.values())
    if best<=0:return max(by_tp,key=lambda k:(by_tp[k]['total'],-(k or 9)))
    ok={k:v for k,v in by_tp.items() if v['total']>=keep*best}
    return max(ok,key=lambda k:(ok[k]['win'] or 0,ok[k]['total']))

def evaluate(data,lo,hi,keep,min_trades,structures,offsets,tps,configs=None):
    bars,signals,chains,first=data;rows=[]
    for (cid,side),sb in signals.items():
        if configs is not None and cid not in configs:continue
        for st in structures:
            for off in offsets:
                by={};excl={}
                for tp in tps:
                    tr,counts=trades_for(sb,side,st,off,tp,chains,first,bars,lo,hi);by[tp]=stats(tr);excl[tp]=counts['no_value_at_window_end']
                tp=choose_tp(by,keep,min_trades)
                if tp is NO_TP_QUALIFIES:continue
                rows.append(dict(config=cid,side=side,structure=st,offset=off,tp=tp,**by[tp],excluded_no_value=excl[tp],all_tp={str(k):v['total'] for k,v in by.items()}))
    return rows

def best(rows,side,st,min_trades):
    c=[r for r in rows if r['side']==side and r['structure']==st and r['n']>=min_trades and r['total']>0]
    return max(c,key=lambda r:(r['total'],r['mean'] or 0)) if c else None

def axes(data):
    _,_,_,first=data;keys=[k for k in first]
    return sorted({k[1] for k in keys}),sorted({k[2] for k in keys}),sorted({k[3] for k in keys},key=lambda x:-1 if x is None else x)

def audit_log():
    return Path(os.environ.get('NIFTY_HOLDOUT_LOG',Path.home()/'.nifty_research'/'holdout_inspections.jsonl'))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--trades',required=True);ap.add_argument('--split',default='2025-01-01');ap.add_argument('--start',default='2021-09-01',help='First entry day counted (EMA warm-up before it)')
    ap.add_argument('--min-trades',type=int,default=15);ap.add_argument('--tp-keep',type=float,default=.75);ap.add_argument('--out',default='zone_report')
    ap.add_argument('--mode',choices=('train','final_holdout'),default='train');ap.add_argument('--freeze');ap.add_argument('--end',default='9999-12-31')
    a=ap.parse_args();out=Path(a.out);out.mkdir(exist_ok=True)
    data=load(a.trades);structures,offsets,tps=axes(data);sha=digest(a.trades)
    if a.mode=='final_holdout':
        if not a.freeze:sys.exit('final_holdout needs --freeze')
        fr=json.load(open(a.freeze))
        if fr['trades_sha256']!=sha:sys.exit('Trades file differs from the one the selection was frozen on')
        log=audit_log();log.parent.mkdir(parents=True,exist_ok=True)
        prior=sum(1 for line in log.open() if json.loads(line).get('trades_sha256')==sha and json.loads(line).get('split')==fr['split']) if log.exists() else 0
        with log.open('a') as f:f.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),tool='zone_report',split=fr['split'],trades_sha256=sha))+'\n')
        bars,signals,chains,first=data;res=[]
        for r in fr['rows']:
            tr,counts=trades_for(signals[r['config'],r['side']],r['side'],r['structure'],r['offset'],r['tp'],chains,first,bars,fr['split'],a.end)
            res.append(dict(r,holdout=stats(tr),holdout_counts=dict(counts)))
        json.dump(dict(inspection_number=prior+1,split=fr['split'],rows=res),open(out/'holdout.json','w'),indent=1)
        print(f'Holdout inspection {prior+1} for this trades file' + (' (REPEATED: not a clean test)' if prior else ''))
        for r in res:print(r['side'],r['structure'],r['config'],r['offset'],r['tp'],'train',{k:r[k] for k in ('n','total','mean','win')},'holdout',r['holdout'])
        return
    rows=evaluate(data,a.start,a.split,a.tp_keep,a.min_trades,structures,offsets,tps)
    freeze=[];summary={}
    for side in ('bull','bear'):
        for st in structures:
            b=best(rows,side,st,a.min_trades)
            group=[r for r in rows if r['side']==side and r['structure']==st]
            zone=[r for r in group if r['config'].startswith('zone_only')]
            tot=sorted(r['total'] for r in group)
            summary[f'{side}_{st}']=dict(candidates=len(group),best=b,best_zone_only_control=max(zone,key=lambda r:r['total']) if zone else None,
                share_positive=round(sum(x>0 for x in tot)/len(tot),3) if tot else None,median_total=tot[len(tot)//2] if tot else None)
            if b:freeze.append({k:b[k] for k in ('config','side','structure','offset','tp','n','total','mean','win','t','max_drawdown')})
            else:summary[f'{side}_{st}']['note']="no configuration with a positive training total and enough trades: nothing frozen"
    years=sorted({d[:4] for d in data[0].values() if a.start<=d<a.split})[1:]
    zone_cfgs={c for (c,_) in data[1] if c.startswith('zone_only')}
    def walk_forward(side,st,configs,mt):
        oos=[];folds=[]
        for y in years:
            sel=best(evaluate(data,a.start,f'{y}-01-01',a.tp_keep,mt,[st],offsets,tps,configs),side,st,mt)
            if not sel:continue
            bars,signals,chains,first=data
            tr,_=trades_for(signals[sel['config'],side],side,st,sel['offset'],sel['tp'],chains,first,bars,f'{y}-01-01',min(f'{int(y)+1}-01-01',a.split))
            oos.extend(tr);folds.append(dict(year=y,selected=sel['config'],offset=sel['offset'],tp=sel['tp'],in_sample_total=sel['total'],oos=stats(tr)))
        return folds,(stats(oos) if oos else None)
    wf={};controls={}
    for side in ('bull','bear'):
        for st in structures:
            folds,pooled=walk_forward(side,st,None,max(10,a.min_trades//2))
            if folds:wf[f'{side}_{st}']=folds
            if pooled:wf[f'{side}_{st}_pooled']=pooled
            # Zone-only controls: never dropped by the trade minimum; same folds, own pooled result
            ctl=[r for r in evaluate(data,a.start,a.split,a.tp_keep,1,[st],offsets,tps,zone_cfgs) if r['side']==side]
            cf,cp=walk_forward(side,st,zone_cfgs,5)
            controls[f'{side}_{st}']=dict(training=sorted(ctl,key=lambda r:-r['total'])[:10],walk_forward=cf,walk_forward_pooled=cp)
    json.dump(dict(split=a.split,start=a.start,trades_sha256=sha,min_trades=a.min_trades,tp_keep=a.tp_keep,candidates_scored=len(rows),summary=summary,walk_forward=wf,zone_only_controls=controls),open(out/'train_report.json','w'),indent=1,default=str)
    json.dump(dict(split=a.split,trades_sha256=sha,rows=freeze),open(out/'selection_freeze.json','w'),indent=1,default=str)
    with open(out/'train_candidates.csv','w') as f:
        f.write('config,side,structure,offset,tp,n,total,mean,win,t,worst,excluded_no_value\n')
        for r in sorted(rows,key=lambda r:-r['total']):f.write(','.join(str(r[k]) for k in ('config','side','structure','offset','tp','n','total','mean','win','t','worst','excluded_no_value'))+'\n')
    print(f'{len(rows)} training candidates scored -> {out}')
    for k,v in summary.items():
        print(k,'candidates',v['candidates'],'positive share',v['share_positive'],'median',v['median_total'])
        print('   best',v['best'] and {x:v['best'][x] for x in ('config','offset','tp','n','total','mean','win','t','worst','max_drawdown','pair_win','hypothetical','forced')},v.get('note',''))
        c=controls.get(k,{});top=(c.get('training') or [None])[0]
        print('   zone-only control (no trade minimum)',top and {x:top[x] for x in ('config','offset','tp','n','total','mean','win','t')})
        print('   zone-only walk-forward out-of-sample',c.get('walk_forward_pooled'))
        if f'{k}_pooled' in wf:print('   walk-forward out-of-sample',wf[f'{k}_pooled'])

if __name__=='__main__':main()
