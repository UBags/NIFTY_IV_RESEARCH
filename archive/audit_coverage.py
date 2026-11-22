#!/usr/bin/env python3
"""Snapshot coverage audit: can we price near-ATM weekly and 60-120 DTE puts?"""
import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--cache',default='research_cache.sqlite');p.add_argument('--out',default='coverage_report')
 a=p.parse_args()
 db=sqlite3.connect(a.cache)
 meta=dict(db.execute('SELECT name,value FROM meta'))
 if meta.get('cache_complete')!='1':raise SystemExit('Cache incomplete')
 providers=[x[0] for x in db.execute('SELECT DISTINCT provider FROM opt ORDER BY provider')]
 months=defaultdict(lambda:dict(spot_days=set(),weekly=set(),long=set(),paired=set(),
                                 paired_filled=set(),pair_strike=set()))
 for d,spot in db.execute("SELECT day,close FROM spot WHERE substr(ts,12,5)='09:45'"):
  if spot and spot>0:
   for prov in providers:months[(prov,d[:7])]['spot_days'].add(d)
 for provider in providers:
  # Limit to near-ATM puts, and to actual 09:45 signal candles with volume.
  sql='''SELECT o.day,o.expiry,o.strike,o.volume,s.close
    FROM opt o JOIN spot s ON o.ts=s.ts
    WHERE o.provider=? AND o.cp='PE' AND substr(o.ts,12,5)='09:45'
       AND o.close>0 AND o.volume>0 AND abs(o.strike-s.close)<=500'''
  bucket=defaultdict(lambda:{'weekly':set(),'long':set()})
  for day,ex,k,vol,s in db.execute(sql,(provider,)):
   from datetime import date
   dte=(date.fromisoformat(ex)-date.fromisoformat(day)).days
   if 1<=dte<=8:bucket[day]['weekly'].add(k)
   if 60<=dte<=120:bucket[day]['long'].add(k)
  for day,group in bucket.items():
   b=months[(provider,day[:7])]
   if group['weekly']:b['weekly'].add(day)
   if group['long']:b['long'].add(day)
   overlap=group['weekly'] & group['long']
   if overlap:
    b['paired'].add(day)
    # Test if BOTH PE legs have 09:46 positive-volume opening observations.
    open_rows=db.execute('''SELECT expiry,strike FROM opt WHERE provider=?
       AND ts=? AND cp='PE' AND volume>0 AND open>0''',(provider,day+' 09:46:00')).fetchall()
    open_pairs=defaultdict(list)
    for exp,k in open_rows:open_pairs[k].append(exp)
    for k in overlap:
     exs=db.execute('''SELECT expiry FROM opt WHERE provider=? AND ts=? AND cp='PE'
        AND strike=? AND volume>0 AND close>0''',(provider,day+' 09:45:00',k)).fetchall()
     ages=[]
     for (ex,) in exs:
      dte=(date.fromisoformat(ex)-date.fromisoformat(day)).days
      ages.append((ex,dte))
     if any(1<=n<=8 and x in open_pairs.get(k,()) for x,n in ages) and any(60<=n<=120 and x in open_pairs.get(k,()) for x,n in ages):
      b['paired_filled'].add(day)
      break
  print('Provider',provider,'days with any near-ATM weekly+long signal:',sum(len(v['paired']) for (p,m),v in months.items() if p==provider),flush=True)
 db.close()
 out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
 with (out/'coverage_by_month.csv').open('w',newline='') as f:
  w=csv.writer(f);w.writerow(['provider','month','spot_signal_days','weekly_signal_days','long_60_120_signal_days','paired_signal_days','paired_09_46_fill_days'])
  for (provider,month),v in sorted(months.items()):
   w.writerow([provider,month,*[len(v[k]) for k in ('spot_days','weekly','long','paired','paired_filled')]])
 lines=['# NIFTY research coverage audit','',
 'Diagnostic: near-ATM is +/- 500 points, short 1-8 DTE, long 60-120 DTE.',
 'Positive 09:45 volume and (for the final column) positive 09:46 open volume are required.','',
 '| Provider | Spot signal days | Weekly available | Long available | Same-strike signal | Same-strike 09:46 fills |',
 '|---|---:|---:|---:|---:|---:|']
 for provider in providers:
  keys=[v for (p,m),v in months.items() if p==provider]
  numbers=[sum(len(v[k]) for v in keys) for k in ('spot_days','weekly','long','paired','paired_filled')]
  lines.append('| '+provider+' | '+' | '.join(str(n) for n in numbers)+' |')
 lines+=['','Missing bars remain missing. These counts are NOT historical returns, and do not verify','15:29 exit liquidity or exchange contract validity.','',
 'See coverage_by_month.csv for periods to prioritize or exclude.']
 (out/'COVERAGE_REPORT.md').write_text('\n'.join(lines)+'\n')
 print('Wrote',out/'coverage_by_month.csv',out/'COVERAGE_REPORT.md')

if __name__=='__main__':main()
