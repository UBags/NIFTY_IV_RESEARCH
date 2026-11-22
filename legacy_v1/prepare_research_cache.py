#!/usr/bin/env python3
"""Read-only PostgreSQL -> local SQLite snapshot for NIFTY calendar research.

This script does not create/alter any PostgreSQL objects. Run after NIFTY imports
are complete; it scans the source tables ONCE for the selected date range.
"""
from __future__ import annotations
import argparse
import datetime as dt
import os
import sqlite3
from pathlib import Path
from collections import Counter

SCHEMA = '''
CREATE TABLE IF NOT EXISTS spot (
 ts TEXT PRIMARY KEY, day TEXT NOT NULL, open REAL, close REAL,
 volume INTEGER, source_id INTEGER);
CREATE TABLE IF NOT EXISTS opt (
 provider TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
 expiry TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL,
 open REAL, high REAL, low REAL, close REAL, volume INTEGER,
 oi INTEGER, source_id INTEGER NOT NULL, source_row INTEGER,
 PRIMARY KEY(provider,ts,expiry,strike,cp));
CREATE INDEX IF NOT EXISTS opt_day ON opt(provider,day,ts,expiry,strike,cp);
CREATE INDEX IF NOT EXISTS opt_contract ON opt(provider,expiry,strike,cp,ts);
CREATE INDEX IF NOT EXISTS spot_day ON spot(day,ts);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);
'''

SPOT_SQL = '''
SELECT (b.bar_time AT TIME ZONE 'Asia/Kolkata') AS local_time,
 b.trading_day, b.open, b.close, b.volume, b.source_file_id
FROM market_data.spot_bars b
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.symbol='NIFTY' AND b.trading_day BETWEEN %s AND %s
 AND f.dataset='thetrademarkk'
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time = ANY(%s::time[])
'''
OPTION_SQL = '''
SELECT (b.bar_time AT TIME ZONE 'Asia/Kolkata') AS local_time,
 b.trading_day, b.expiry, b.strike, b.option_type,
 b.open,b.high,b.low,b.close,b.volume,b.open_interest,
 b.source_file_id,b.source_row_index,f.dataset
FROM market_data.option_bars b
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.underlying='NIFTY'
 AND b.trading_day BETWEEN %s AND %s
 AND b.option_type IN ('PE','CE') AND b.granularity='1min'
 AND f.dataset IN ('rissin_upstox','thetrademarkk')
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time = ANY(%s::time[])
'''


def connect():
 import psycopg
 return psycopg.connect(host=os.getenv('PGHOST','localhost'),port=int(os.getenv('PGPORT','5432')),
    dbname=os.getenv('PGDATABASE','nifty_data'),user=os.getenv('PGUSER','postgres'),
    password=os.getenv('PGPASSWORD'),connect_timeout=15)

def tolocal(value):
 # PostgreSQL query returns naive IST, no conversion necessary.
 return value.strftime('%Y-%m-%d %H:%M:%S')

def extract(conn, query, db, params, kind, batch=10000):
 count=0
 with conn.cursor(name=f'nifty_research_{kind}') as cur:
  cur.itersize=batch
  cur.execute(query,params)
  while True:
   rows=cur.fetchmany(batch)
   if not rows: break
   if kind=='spot':
    data=[(tolocal(t),str(d),o,c,v,sid) for t,d,o,c,v,sid in rows]
    db.executemany('''INSERT INTO spot(ts,day,open,close,volume,source_id)
      VALUES(?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET
      open=excluded.open,close=excluded.close,volume=excluded.volume,
      source_id=excluded.source_id WHERE
      COALESCE(excluded.volume,0)>COALESCE(spot.volume,0)''',data)
   else:
    data=[(dataset,tolocal(t),str(d),str(ex),float(k),cp,o,h,l,c,v,oi,sid,ridx)
          for t,d,ex,k,cp,o,h,l,c,v,oi,sid,ridx,dataset in rows]
    # Prefer positive-volume observations, then the row with greater volume,
    # within each provider. Never merge prices between providers.
    db.executemany('''INSERT INTO opt(provider,ts,day,expiry,strike,cp,open,high,low,close,volume,oi,source_id,source_row)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,ts,expiry,strike,cp)
      DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,
      close=excluded.close,volume=excluded.volume,oi=excluded.oi,
      source_id=excluded.source_id,source_row=excluded.source_row
      WHERE COALESCE(excluded.volume,0)>COALESCE(opt.volume,0)
      ''',data)
   db.commit()
   count += len(rows)
   if count % 200000 < batch: print(f'{kind}: processed {count:,} source rows',flush=True)
 return count

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--from-date',default='2025-09-01');p.add_argument('--to-date',default='2026-07-01')
 p.add_argument('--times',default='09:45,09:46,15:29',help='IST minute starts; include exit and rebalance times')
 p.add_argument('--out',default='research_cache.sqlite');p.add_argument('--force-rebuild',action='store_true')
 p.add_argument('--root',default=str(Path(__file__).resolve().parent/'NIFTY_Data'),help='Root of downloaded Parquet tree; use to verify import completion')
 p.add_argument('--check-only',action='store_true',help='Show source-file completion without scanning quotes')
 args=p.parse_args();start=dt.date.fromisoformat(args.from_date);end=dt.date.fromisoformat(args.to_date)
 if end<start: p.error('to-date must not precede from-date')
 times=[dt.time.fromisoformat(s.strip()) for s in args.times.split(',')]
 with connect() as pg:
  with pg.cursor() as c:
   c.execute('''SELECT f.dataset,f.market_kind,f.status,COUNT(*),SUM(f.loaded_rows),SUM(f.expected_rows)
      FROM market_data.import_files f WHERE
      (f.relative_path IN ('NIFTY.parquet','india-index-options-1m/index/NIFTY.parquet')
       OR f.relative_path LIKE 'NIFTY_20%%.parquet'
       OR f.relative_path LIKE 'india-index-options-1m/options/NIFTY/%%')
      GROUP BY 1,2,3 ORDER BY 1,2,3''')
   states=c.fetchall()
  print('NIFTY import ledger:')
  for s in states: print(' ',s)
  root=Path(args.root)
  if not root.exists():
   raise SystemExit(f'Cannot verify completeness: Parquet root does not exist: {root}')
  expected={p.relative_to(root).as_posix() for p in root.glob('NIFTY*.parquet')}
  expected |= {p.relative_to(root).as_posix() for p in (root/'india-index-options-1m'/'options'/'NIFTY').glob('*.parquet')}
  nspot=root/'india-index-options-1m'/'index'/'NIFTY.parquet'
  if nspot.exists():expected.add(nspot.relative_to(root).as_posix())
  with pg.cursor() as c:
   c.execute('SELECT relative_path,status,loaded_rows,expected_rows FROM market_data.import_files')
   ledger={x[0]:x[1:] for x in c.fetchall()}
  missing=expected-set(ledger)
  notdone=[f for f in expected if f in ledger and (ledger[f][0]!='completed' or ledger[f][1]!=ledger[f][2])]
  print(f'Local NIFTY files: {len(expected)}; not registered: {len(missing)}; not complete: {len(notdone)}',flush=True)
  if missing:print('  Unregistered examples:', sorted(missing)[:4])
  if notdone:print('  Incomplete examples:', sorted(notdone)[:4])
  if args.check_only:return
  if missing or notdone:
   raise SystemExit('NIFTY Parquet import not complete. Do NOT snapshot partial history.')
  if not any(s[0]=='thetrademarkk' and s[1]=='spot' for s in states):
   raise SystemExit('Nested NIFTY spot file not yet imported')
  path=Path(args.out)
  if path.exists():
   if not args.force_rebuild: raise SystemExit(f'{path} exists. Refusing to append or blend snapshots; pass --force-rebuild to replace it.')
   path.unlink()
  db=sqlite3.connect(path)
  try:
   db.executescript(SCHEMA)
   db.execute('PRAGMA journal_mode=WAL')
   db.execute('PRAGMA synchronous=NORMAL')
   db.execute('INSERT INTO meta(name,value) VALUES(?,?)',('cache_complete','0'))
   db.execute('INSERT INTO meta(name,value) VALUES(?,?)',('range_start',str(start)))
   db.execute('INSERT INTO meta(name,value) VALUES(?,?)',('range_end',str(end)))
   db.execute('INSERT INTO meta(name,value) VALUES(?,?)',('minutes',','.join(t.strftime('%H:%M') for t in times)))
   db.commit()
   nspot=extract(pg,SPOT_SQL,db,(start,end,times),'spot')
   nopt=extract(pg,OPTION_SQL,db,(start,end,times),'option')
   ns=db.execute('SELECT COUNT(*) FROM spot').fetchone()[0]
   no=db.execute('SELECT COUNT(*) FROM opt').fetchone()[0]
   print(f'Final SQLite: spot={ns:,} option={no:,}; raw retrieved spot={nspot:,} option={nopt:,}')
   if ns<=0 or no<=0: raise ValueError('Empty cache; check imported years / source coverage')
   db.execute("UPDATE meta SET value='1' WHERE name='cache_complete'")
   db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
  finally: db.close()
 print('Cache complete:',path)

if __name__=='__main__':main()
