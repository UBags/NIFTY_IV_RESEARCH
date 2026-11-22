#!/usr/bin/env python3
"""Train-only strategy screening and holdout reporting from research_trades.csv.gz.

stdlib-only Python/SQLite; large candidate CSV is streamed to a compact indexed
work database (reused when unchanged). Finalists respect overlap position caps.
Drawdown is realized-only and understates mark-to-market portfolio risk.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import gzip
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

from itertools import combinations

TENORS=(5,15,30,45,60,75,90,120)
FEATURE_SQL={'short_long':'iv_ratio'}
FEATURE_SQL.update({f'{a}_{b}':f'(iv{a}/NULLIF(iv{b},0))' for a,b in combinations(TENORS,2)})
DEFAULT_FEATURES=('short_long','5_15','5_30','5_45','5_60','5_75','5_90',
   '15_30','15_60','30_60','30_90','45_90','60_90')
WORK_DDL='''
CREATE TABLE IF NOT EXISTS market_trades (
 day TEXT NOT NULL,exit_day TEXT NOT NULL,
 hedge TEXT NOT NULL,exit_policy TEXT NOT NULL,strike_step INT,
 strike_offset INT,target_tenor INT,pnl_points REAL,entry_debit_points REAL,
 iv_ratio REAL,iv5 REAL,iv15 REAL,iv30 REAL,iv45 REAL,iv60 REAL,
 iv75 REAL,iv90 REAL,iv120 REAL);
CREATE INDEX IF NOT EXISTS screen ON market_trades(hedge,exit_policy,strike_step,strike_offset,target_tenor,day);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
'''


def float_or_none(value):
 if value in ('',None):return None
 try:
  x=float(value);return x if math.isfinite(x) else None
 except ValueError:return None


def openwork(file,work):
 path=Path(file);p=Path(work)
 if not path.exists():raise SystemExit(f'No research trades: {path}')
 source=(str(path.resolve()),str(path.stat().st_size),str(path.stat().st_mtime_ns))
 if p.exists():
  db=sqlite3.connect(p);meta=dict(db.execute('SELECT key,value FROM meta'))
  if (meta.get('source'),meta.get('size'),meta.get('mtime'))==source and meta.get('complete')=='1':
   print('Reusing completed SQLite screening database:',p,flush=True)
   return db
  db.close();p.unlink()
 db=sqlite3.connect(p)
 db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
 db.executescript(WORK_DDL)
 db.executemany('INSERT INTO meta(key,value) VALUES(?,?)',zip(('source','size','mtime','complete'),(*source,'0')))
 opener=gzip.open if path.suffix=='.gz' else open
 buf=[];count=0
 with opener(path,'rt',encoding='utf-8',newline='') as f:
  for r in csv.DictReader(f):
   vols=[float_or_none(r[f'iv_{t}']) for t in TENORS]
   buf.append((r['day'],r['exit_day'],r['hedge'],r['exit_policy'],int(r['strike_step']),
        int(r['strike_offset']),int(r['target_tenor']),float(r['pnl_points']),
        float(r['entry_debit_points']),float_or_none(r['iv_ratio']),*vols))
   if len(buf)>=50000:
    db.executemany('INSERT INTO market_trades VALUES('+','.join('?'*18)+')',buf)
    count+=len(buf);buf=[];db.commit()
    print(f'Streamed {count:,} trade variants',flush=True)
  if buf:
   db.executemany('INSERT INTO market_trades VALUES('+','.join('?'*18)+')',buf)
   count+=len(buf)
 db.execute("UPDATE meta SET value='1' WHERE key='complete'")
 db.commit();print('Loaded',count,'variants into',p,flush=True)
 return db


def regimes_from_cache(cache):
 db=sqlite3.connect(cache)
 obs=db.execute("SELECT day,close FROM spot WHERE substr(ts,12,5)='15:29' ORDER BY day").fetchall()
 db.close()
 prices=[];labels={}
 # Current date EOD is known only AFTER trading that date; so signal for
 # an entry in the morning uses returns through the PREVIOUS session.
 for day,close in obs:
  if close is None or close<=0:continue
  if len(prices)>=21:
   ch=prices[-1]/prices[-21]-1
   r=[math.log(prices[i]/prices[i-1]) for i in range(len(prices)-20,len(prices))]
   rv=statistics.stdev(r)*math.sqrt(252) if len(r)>=3 else 0.
   trend='up' if ch>.02 else 'down' if ch<-.02 else 'sideways'
   labels[day]=trend+'_'+('highvol' if rv>=.20 else 'lowvol')
  prices.append(close)
 return labels


def portfolio_stats(rows,open_limit,regimes):
 rows=sorted(rows,key=lambda x:(x['day'],x['exit_day']))
 selected=[];active=[]
 for t in rows:
  d=t['day'];active=[end for end in active if end>=d]
  if len(active)>=open_limit:continue
  selected.append(t);active.append(t['exit_day'])
 values=[float(x['pnl_points']) for x in selected]
 if not values:return dict(count=0,total_points=0.,mean_points=0.,mean_debit=0.,win_pct=0.,worst_points=0.,realized_drawdown_points=0.,regimes={})
 realized=sorted(selected,key=lambda x:(x['exit_day'],x['day']))
 equity=peak=drawdown=0.
 for t in realized:
  equity+=float(t['pnl_points']);peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
 groups=defaultdict(list)
 for t in selected:groups[regimes.get(t['day'],'unknown')].append(float(t['pnl_points']))
 return dict(count=len(values),total_points=round(sum(values),3),
     mean_points=round(statistics.mean(values),3),
     mean_debit=round(statistics.mean(float(x['entry_debit_points']) for x in selected),3),
     win_pct=round(100*sum(v>0 for v in values)/len(values),2),
     worst_points=round(min(values),3),
     realized_drawdown_points=round(drawdown,3),
     regimes={k:{'trades':len(v),'sum_points':round(sum(v),2),'mean_points':round(statistics.mean(v),2)}
         for k,v in sorted(groups.items())})


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--trades',default='research_trades.csv.gz');p.add_argument('--cache',default='research_cache.sqlite')
 p.add_argument('--work-db',default='ranking_work.sqlite')
 p.add_argument('--split',default='2026-01-01',help='First holdout entry day')
 p.add_argument('--embargo-days',type=int,default=120,
                help='Drop N training entry days before test, to avoid positions straddling train/test')
 p.add_argument('--thresholds',default='0.90,1.00,1.10,1.20,1.30,1.50')
 p.add_argument('--upper-bounds',default='999',help='Optional upper ratio bands, e.g. 1.30,1.60,2.00,999')
 p.add_argument('--screen-pool',type=int,default=500,help='Number of preliminary finalists to analyse for overlapping-position robustness')
 p.add_argument('--iv-signals',default='default',help='default, all or comma-separated tenor-pair names such as 5_90,15_60,short_long')
 p.add_argument('--min-train',type=int,default=30);p.add_argument('--min-test',type=int,default=10)
 p.add_argument('--max-open',type=int,default=3)
 p.add_argument('--top',type=int,default=25);p.add_argument('--out',default='strategy_rankings')
 a=p.parse_args()
 if a.max_open<1:p.error('max-open must be >0')
 train_cutoff=(dt.date.fromisoformat(a.split)-dt.timedelta(days=a.embargo_days)).isoformat()
 db=openwork(a.trades,a.work_db)
 total=db.execute('SELECT COUNT(*) FROM market_trades').fetchone()[0]
 if not total:raise SystemExit('No executed candidate trades. Investigate missing prices/liquidity.')
 thresholds=[float(x) for x in a.thresholds.split(',')]
 uppers=[float(x) for x in a.upper_bounds.split(',')]
 if any(x<=0 for x in uppers):p.error('Upper bounds must be positive')
 if any(x<=0 for x in thresholds):p.error('IV ratios must be positive')
 requested=list(FEATURE_SQL) if a.iv_signals=='all' else (list(DEFAULT_FEATURES) if a.iv_signals=='default' else [x.strip() for x in a.iv_signals.split(',')])
 if not set(requested)<=set(FEATURE_SQL):p.error('Unknown IV signals: '+str(set(requested)-set(FEATURE_SQL)))
 groups=[]
 for name in requested:
  column=FEATURE_SQL[name]
  for t in thresholds:
   for upper in uppers:
    if upper<=t:continue
    rows=db.execute(f'''SELECT hedge,exit_policy,strike_step,strike_offset,target_tenor,
     COUNT(*) n,SUM(pnl_points) sum_pnl,SUM(pnl_points*pnl_points) sumsq,
     AVG(pnl_points) mean_pnl,AVG(entry_debit_points) mean_debit,
     MIN(pnl_points) min_pnl
     FROM market_trades
     WHERE day < ? AND exit_day < ? AND {column} IS NOT NULL AND {column} >= ? AND {column} < ?
     GROUP BY hedge,exit_policy,strike_step,strike_offset,target_tenor
     HAVING COUNT(*) >= ?''',
     (train_cutoff,a.split,t,upper,a.min_train)).fetchall()
    for h,pol,step,offset,tenor,n,sumval,ssq,mean,debit,worst in rows:
     if debit<=0:continue
     stdev=math.sqrt(max(0.,(ssq-sumval*sumval/n)/(n-1))) if n>1 else 0
     score=(mean-.5*stdev)/debit
     groups.append((score,n,name,t,upper,h,pol,step,offset,tenor))
 groups.sort(key=lambda x:(x[0],x[1]),reverse=True)
 if not groups:
  print(f'No combinations reach {a.min_train} training trades before {train_cutoff}. '
        'Use a longer data window, more realistic strike/liquidity coverage or fewer strategy dimensions.')
  return
 regimes=regimes_from_cache(a.cache)
 results=[];seen=set()
 for score,n,name,t,upper,h,pol,step,offset,tenor in groups:
  key=(name,h,pol,step,offset,tenor)
  if key in seen:continue
  column=FEATURE_SQL[name]
  base=f'''hedge=? AND exit_policy=? AND strike_step=? AND strike_offset=?
              AND target_tenor=? AND {column}>=? AND {column}<?'''
  params=(h,pol,step,offset,tenor,t,upper)
  cols='day,exit_day,pnl_points,entry_debit_points'
  tr=db.execute(f'''SELECT {cols} FROM market_trades WHERE {base}
                AND day < ? AND exit_day < ? ORDER BY day''',params+(train_cutoff,a.split)).fetchall()
  ts=db.execute(f'''SELECT {cols} FROM market_trades WHERE {base}
                AND day >= ? ORDER BY day''',params+(a.split,)).fetchall()
  to_rows=lambda rr:[dict(zip(('day','exit_day','pnl_points','entry_debit_points'),x)) for x in rr]
  train=portfolio_stats(to_rows(tr),a.max_open,regimes)
  test=portfolio_stats(to_rows(ts),a.max_open,regimes)
  if train['count']<a.min_train:continue
  seen.add(key)
  regime_means=[v['mean_points'] for v in train['regimes'].values() if v['trades']>=max(5,train['count']//10)]
  worst_regime_mean=min(regime_means) if regime_means else train['mean_points']
  robust_score=(train['mean_points']-0.5*max(0.,-worst_regime_mean)
      -0.15*train['realized_drawdown_points']/max(1,train['count']))/max(1.,train['mean_debit'])
  results.append(dict(robust_train_score=round(robust_score,6),iv_signal=name,threshold=t,upper_bound=upper,hedge=h,exit_policy=pol,strike_step=step,
           strike_offset=offset,long_tenor=tenor,screening_score=round(score,6),
           train=train,test=test,adequate_holdout=test['count']>=a.min_test))
  if len(results)>=max(a.top,a.screen_pool):break
 # Select & order ONLY from training. Do not use holdout metrics as selection criteria.
 results.sort(key=lambda x:(x['robust_train_score'],x['train']['count']),reverse=True)
 results=results[:a.top]
 folder=Path(a.out);folder.mkdir(parents=True,exist_ok=True)
 output={'disclaimer':'NOT live returns. Option-minute OHLC approximate fills. Delta proxy cannot be traded as spot. Realized-only drawdown may substantially understate actual drawdown. Fees and lot size configurable but not automatically historically adjusted.',
         'split':a.split,'train_cutoff_exclusive':train_cutoff,'embargo_days':a.embargo_days,
         'position_cap':a.max_open,'total_variants':total,
         'ranking':'ONLY training robust scores; holdout shown without reranking',
         'strategies':results}
 (folder/'rankings.json').write_text(json.dumps(output,indent=2))
 with (folder/'rankings.csv').open('w',newline='') as f:
  w=csv.writer(f);w.writerow(['rank','train_robust_score','IV_signal','IV_min_ratio','IV_max_ratio','hedge','exit','rounding','offset','long_target',
   'train_n','train_points','train_worst','train_realized_DD',
   'test_n','test_points','test_worst','test_realized_DD','adequate_holdout'])
  for i,x in enumerate(results,1):
   tr=x['train'];ts=x['test']
   w.writerow([i,x['robust_train_score'],x['iv_signal'],x['threshold'],x['upper_bound'],x['hedge'],x['exit_policy'],x['strike_step'],
    x['strike_offset'],x['long_tenor'],tr['count'],tr['total_points'],tr['worst_points'],
    tr['realized_drawdown_points'],ts['count'],ts['total_points'],ts['worst_points'],
    ts['realized_drawdown_points'],x['adequate_holdout']])
 print('Candidate variants:',total,'Train entry cutoff:',train_cutoff,'Holdout begins:',a.split)
 print('TRAINING-ranked finalists (test does not affect ranking):')
 for i,x in enumerate(results[:12],1):
  print(f" {i:2} {x['iv_signal']:10s} IV=[{x['threshold']:.2f},{x['upper_bound']:.2f}) long={x['long_tenor']:3d} "
        f"offset={x['strike_offset']:>5d} {x['hedge']:33s} {x['exit_policy']:12s} "
        f"train n={x['train']['count']:3d}, {x['train']['total_points']:>10.1f}pts; "
        f"test n={x['test']['count']:3d}, {x['test']['total_points']:>10.1f}pts")
 print('Saved',folder/'rankings.csv','and',folder/'rankings.json')
 if any(not x['adequate_holdout'] for x in results[:5]):print('WARNING: top finalists have insufficient independent holdout trades.')
 print('WARNING: reported drawdown is realized P&L only; not an adequate risk estimate for trading.')

if __name__=='__main__':main()
