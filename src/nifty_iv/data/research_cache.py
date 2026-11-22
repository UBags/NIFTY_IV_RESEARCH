#!/usr/bin/env python3
"""Read-only PostgreSQL -> source-aware SQLite snapshot, NIFTY only.

Run AFTER the NIFTY import finishes; no PostgreSQL DDL. Retain ALL selected
raw option rows and a deterministic canonical view of each vendor's duplicate
minute. No cross-provider merges. Times are IST with later-minute fill proxies.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,os,shutil,sqlite3
from pathlib import Path
import nifty_iv

TIMES=['09:45','09:46','09:47','09:48','09:49','09:50',
       '11:30','11:31','11:32','13:30','13:31','13:32','15:15','15:16','15:17','15:29']
DDL='''
CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE spot_daily(day TEXT PRIMARY KEY,open REAL NOT NULL,high REAL NOT NULL,
 low REAL NOT NULL,close REAL NOT NULL,minute_bars INTEGER NOT NULL,
 source_id INTEGER,complete_session INTEGER NOT NULL DEFAULT 0);
CREATE TABLE spot(ts TEXT PRIMARY KEY,day TEXT NOT NULL,open REAL,close REAL,
 volume INTEGER,source_id INTEGER,source_row INTEGER);
CREATE TABLE opt_raw (
 provider TEXT NOT NULL,ts TEXT NOT NULL,day TEXT NOT NULL,expiry TEXT NOT NULL,
 strike REAL NOT NULL,cp TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,
 volume INTEGER,oi INTEGER,source_id INTEGER NOT NULL,source_row INTEGER NOT NULL,
 PRIMARY KEY(source_id,source_row));
CREATE TABLE opt (
 provider TEXT NOT NULL,ts TEXT NOT NULL,day TEXT NOT NULL,expiry TEXT NOT NULL,
 strike REAL NOT NULL,cp TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,
 volume INTEGER,oi INTEGER,source_id INTEGER NOT NULL,source_row INTEGER NOT NULL,
 PRIMARY KEY(provider,ts,expiry,strike,cp));
CREATE TABLE duplicate_stats(provider TEXT NOT NULL,day TEXT NOT NULL,
 source_rows INTEGER NOT NULL DEFAULT 0,canonical_rows INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(provider,day));
CREATE TABLE forward_cache(provider TEXT NOT NULL,ts TEXT NOT NULL,expiry TEXT NOT NULL,
 forward REAL,matched_pairs INTEGER NOT NULL,dispersion_bps REAL,quality TEXT NOT NULL,
 discount REAL,implied_rate REAL,estimator TEXT,
 PRIMARY KEY(provider,ts,expiry));
CREATE TABLE greeks_cache(provider TEXT NOT NULL,ts TEXT NOT NULL,expiry TEXT NOT NULL,
 strike REAL NOT NULL,cp TEXT NOT NULL,iv REAL,delta_proxy REAL,gamma_proxy REAL,
 vega REAL,source_id INTEGER NOT NULL,source_row INTEGER NOT NULL,
 PRIMARY KEY(provider,ts,expiry,strike,cp));
CREATE TABLE spot_daily_ext(day TEXT PRIMARY KEY,twap_1500_1529 REAL,twap_minutes INTEGER,
 parkinson_1m_var REAL,closeclose_1m_var REAL,closeclose_1m_n INTEGER,session_quality TEXT);
CREATE TABLE extract_progress(step TEXT PRIMARY KEY,spot_rows INTEGER NOT NULL DEFAULT 0,option_rows INTEGER NOT NULL DEFAULT 0,content_digest TEXT NOT NULL DEFAULT '');
'''

DAILY_SQL='''
SELECT b.trading_day, (ARRAY_AGG(b.open ORDER BY b.bar_time ASC))[1],
 MAX(b.high),MIN(b.low),(ARRAY_AGG(b.close ORDER BY b.bar_time DESC))[1],
 COUNT(DISTINCT b.bar_time) AS minute_bars,MIN(b.source_file_id),
 AVG(b.close) FILTER (WHERE (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '15:00'::time AND '15:29'::time),
 COUNT(DISTINCT b.bar_time) FILTER (WHERE (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '15:00'::time AND '15:29'::time),
 SUM(POWER(LN(b.high/b.low),2))/(4*LN(2))
FROM market_data.spot_bars b
JOIN market_data.import_files f ON b.source_file_id=f.file_id
WHERE f.relative_path='india-index-options-1m/index/NIFTY.parquet'
 AND b.symbol='NIFTY' AND b.trading_day BETWEEN %s AND %s
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15'::time AND '15:29'::time
 AND b.open>0 AND b.high>0 AND b.low>0 AND b.close>0
GROUP BY b.trading_day ORDER BY b.trading_day
'''

# Squared log returns between consecutive minutes only; a gap is skipped, never bridged.
DAILY_RV_SQL='''
SELECT trading_day,SUM(r*r),COUNT(r) FROM (
 SELECT b.trading_day,LN(b.close/LAG(b.close) OVER w) AS r,
  b.bar_time-LAG(b.bar_time) OVER w AS gap
 FROM market_data.spot_bars b
 JOIN market_data.import_files f ON b.source_file_id=f.file_id
 WHERE f.relative_path='india-index-options-1m/index/NIFTY.parquet'
  AND b.symbol='NIFTY' AND b.trading_day BETWEEN %s AND %s
  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15'::time AND '15:29'::time
  AND b.close>0
 WINDOW w AS (PARTITION BY b.trading_day ORDER BY b.bar_time)
) x WHERE gap=INTERVAL '1 minute' GROUP BY trading_day
'''

SPOT_SQL='''
WITH target AS MATERIALIZED (
 SELECT (d::date + mins.minute) AT TIME ZONE 'Asia/Kolkata' AS ts
 FROM generate_series(%s::date,%s::date,'1 day'::interval) AS d
 CROSS JOIN unnest(%s::time[]) AS mins(minute)
)
SELECT b.bar_time AT TIME ZONE 'Asia/Kolkata' AS local_time, b.trading_day,
 b.open,b.close,b.volume,b.source_file_id,b.source_row_index
FROM market_data.spot_bars b
JOIN target tgt ON b.bar_time=tgt.ts
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.symbol='NIFTY' AND b.trading_day BETWEEN %s AND %s
 AND f.relative_path='india-index-options-1m/index/NIFTY.parquet' AND f.status='completed'
'''
OPT_SQL='''
WITH target AS MATERIALIZED (
 SELECT (d::date + mins.minute) AT TIME ZONE 'Asia/Kolkata' AS ts
 FROM generate_series(%s::date,%s::date,'1 day'::interval) AS d
 CROSS JOIN unnest(%s::time[]) AS mins(minute)
)
SELECT b.bar_time AT TIME ZONE 'Asia/Kolkata' AS local_time,b.trading_day,b.expiry,
 b.strike,b.option_type,b.open,b.high,b.low,b.close,b.volume,b.open_interest,
 b.source_file_id,b.source_row_index,f.dataset
FROM market_data.option_bars b
JOIN target tgt ON b.bar_time=tgt.ts
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.underlying='NIFTY' AND b.trading_day BETWEEN %s AND %s
 AND b.option_type IN ('PE','CE') AND b.granularity='1min'
 AND f.dataset IN ('rissin_upstox','thetrademarkk') AND f.status='completed'
'''

def connect():
 import psycopg
 return psycopg.connect(host=os.getenv('PGHOST','localhost'),port=int(os.getenv('PGPORT','5432')),
     dbname=os.getenv('PGDATABASE','nifty_data'),user=os.getenv('PGUSER','postgres'),
     password=os.getenv('PGPASSWORD'),connect_timeout=15)

def ist_time(t):return t.strftime('%Y-%m-%d %H:%M:%S')
def num(x):return None if x is None else float(x)   # psycopg returns Decimal for NUMERIC; sqlite3 cannot bind it
def whole(x):return None if x is None else int(x)


FULL_SESSION_MINUTES=375
def session_quality(n,minimum):
 return 'full' if n>=FULL_SESSION_MINUTES else 'near_full' if n>=minimum else 'partial'


FINAL_INDEXES=('CREATE INDEX IF NOT EXISTS opt_contract ON opt(provider,expiry,strike,cp,ts)',
 'CREATE INDEX IF NOT EXISTS opt_day ON opt(provider,day)','CREATE INDEX IF NOT EXISTS spot_day ON spot(day,ts)',
 'CREATE INDEX IF NOT EXISTS raw_group ON opt_raw(provider,day)')
def finalize(db):
 """Safe to repeat after a crash at any point: indexes are conditional, stats are rebuilt atomically."""
 for sql in FINAL_INDEXES:
  db.execute(sql);db.commit()
 db.execute('DELETE FROM duplicate_stats')
 db.execute('''INSERT INTO duplicate_stats SELECT r.provider,r.day,r.n,COALESCE(c.n,0) FROM
   (SELECT provider,day,COUNT(*) n FROM opt_raw GROUP BY provider,day) r
   LEFT JOIN (SELECT provider,day,COUNT(*) n FROM opt GROUP BY provider,day) c ON c.provider=r.provider AND c.day=r.day''')
 db.commit()


def extraction_config(digest,start,end,minutes,args):
 """Every setting that changes extracted rows or classifications; a resume must match all of it."""
 sql=hashlib.sha256((DDL+DAILY_SQL+DAILY_RV_SQL+SPOT_SQL+OPT_SQL).encode()).hexdigest()
 return json.dumps(dict(source=digest,start=str(start),end=str(end),minutes=minutes,min_session_minutes=args.min_session_minutes,
   allow_partial=args.allow_partial,daily_warmup_days=args.daily_warmup_days,query_sha256=sql,timezone='Asia/Kolkata'),sort_keys=True)


def month_windows(start,end):
 a=start
 while a<=end:
  nxt=(a.replace(day=1)+dt.timedelta(days=32)).replace(day=1)
  b=min(end,nxt-dt.timedelta(days=1));yield a,b;a=nxt


def ledger_check(pg,root):
 expected={p.relative_to(root).as_posix() for p in root.glob('NIFTY*.parquet')}
 expected|={p.relative_to(root).as_posix() for p in (root/'india-index-options-1m'/'options'/'NIFTY').glob('*.parquet')}
 spot=root/'india-index-options-1m'/'index'/'NIFTY.parquet'
 if spot.exists():expected.add(spot.relative_to(root).as_posix())
 if not expected:raise SystemExit(f'No NIFTY Parquets found under {root}')
 with pg.cursor() as c:
  c.execute('''SELECT relative_path,status,loaded_rows,expected_rows,file_bytes,mtime_ns
                FROM market_data.import_files''')
  ledger={r[0]:r[1:] for r in c.fetchall()}
 absent=expected-ledger.keys()
 incomplete=[x for x in expected if x in ledger and (ledger[x][0]!='completed' or ledger[x][1]!=ledger[x][2])]
 print(f'NIFTY source files {len(expected)}, missing ledger entries {len(absent)}, incomplete {len(incomplete)}',flush=True)
 if absent:print('Missing:',sorted(absent)[:5])
 if incomplete:print('Incomplete:',sorted(incomplete)[:5])
 digest=hashlib.sha256()
 for key in sorted(expected):digest.update(f'{key}|{ledger.get(key)}\n'.encode())
 return not absent and not incomplete, digest.hexdigest()


def insert_rows(db,rows,kind):
 if kind=='spot':
  db.executemany('''INSERT INTO spot VALUES(?,?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET
    open=excluded.open,close=excluded.close,volume=excluded.volume,
    source_id=excluded.source_id,source_row=excluded.source_row WHERE
    COALESCE(excluded.volume,0)>COALESCE(spot.volume,0) OR
    (COALESCE(excluded.volume,0)=COALESCE(spot.volume,0)
    AND (excluded.source_id,excluded.source_row)>(spot.source_id,spot.source_row))''',rows)
 else:
  db.executemany('INSERT INTO opt_raw VALUES('+','.join('?'*14)+')',rows)
  db.executemany('''INSERT INTO opt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
   ON CONFLICT(provider,ts,expiry,strike,cp) DO UPDATE SET
   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
   volume=excluded.volume,oi=excluded.oi,source_id=excluded.source_id,
   source_row=excluded.source_row WHERE
   COALESCE(excluded.volume,0)>COALESCE(opt.volume,0) OR
   (COALESCE(excluded.volume,0)=COALESCE(opt.volume,0) AND
   (excluded.source_id,excluded.source_row)>(opt.source_id,opt.source_row))''',rows)


def extract(pg,sql,db,params,kind,digest,batch=10000):
 count=0
 with pg.cursor(name='nifty_v3_'+kind) as cur:
  cur.itersize=batch;cur.execute(sql,params)
  while True:
   src=cur.fetchmany(batch)
   if not src:break
   if kind=='spot':
    rows=[(ist_time(t),str(day),num(op),num(cl),whole(v),sid,ridx) for t,day,op,cl,v,sid,ridx in src]
   else:
    rows=[(provider,ist_time(t),str(day),str(ex),float(k),cp,num(op),num(hi),num(lo),num(cl),whole(v),whole(oi),sid,ridx)
      for t,day,ex,k,cp,op,hi,lo,cl,v,oi,sid,ridx,provider in src]
   insert_rows(db,rows,kind);count+=len(rows)
   for row in rows:digest[0]=(digest[0]+int.from_bytes(hashlib.blake2b(repr(row).encode(),digest_size=8).digest(),'big'))%(1<<64)   # the caller commits per month, so a crash loses at most one month
   if count%200000 < batch:print(f'{kind}: {count:,} source records processed',flush=True)
 return count


def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--root',default=str(nifty_iv.DATA_ROOT))
 ap.add_argument('--from-date',default='2024-10-01');ap.add_argument('--to-date',default='2026-07-01')
 ap.add_argument('--out',default='research_cache_v4.sqlite')
 ap.add_argument('--times',default=','.join(TIMES));ap.add_argument('--check-only',action='store_true')
 ap.add_argument('--force-rebuild',action='store_true',help='Replace the output only after the new cache verifies')
 ap.add_argument('--resume',action='store_true',help='Continue an interrupted build from its .partial file')
 ap.add_argument('--daily-warmup-days',type=int,default=420,help='Extra daily spot history before --from-date, so trailing averages are ready')
 ap.add_argument('--min-free-gb',type=float,default=20.,help='Stop before a month if free disk falls below this')
 ap.add_argument('--min-session-minutes',type=int,default=370,help='Below this a session is partial and excluded from RV signals')
 ap.add_argument('--allow-partial',action='store_true',help='DIAGNOSTICS ONLY; no backtesting allowed')
 args=ap.parse_args();root=Path(args.root)
 if not root.exists():ap.error(f'root does not exist: {root}')
 start=dt.date.fromisoformat(args.from_date);end=dt.date.fromisoformat(args.to_date)
 if start>end:ap.error('from-date after to-date')
 times=[dt.time.fromisoformat(x.strip()) for x in args.times.split(',')]
 mandatory={'09:45','09:46','15:15','15:16','15:17','15:29'}
 if not mandatory.issubset({x.strftime('%H:%M') for x in times}):ap.error('minimum times missing')
 with connect() as pg:
  complete,digest=ledger_check(pg,root)
  if args.check_only:return
  if not complete and not args.allow_partial:raise SystemExit('Import incomplete. Refusing research snapshot until imports finish.')
  path=Path(args.out);staging=path.with_name(path.name+'.partial')
  if path.exists() and not args.force_rebuild:raise SystemExit('Output exists. Use --force-rebuild to replace it.')
  if staging.exists() and not args.resume:raise SystemExit(f'{staging} exists. Use --resume, or delete it to start over.')
  path.parent.mkdir(parents=True,exist_ok=True)
  fresh=not staging.exists()
  db=sqlite3.connect(staging)
  try:
   db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
   minutes=','.join(x.strftime('%H:%M') for x in times)
   if fresh:
    db.executescript(DDL)
    items={'version':'3','release':'4.4.3','cache_complete':'0','research_eligible':'0','source_fingerprint':digest,
          'range_start':str(start),'range_end':str(end),'minutes':minutes,'min_session_minutes':str(args.min_session_minutes),
          'partial_import':str(not complete),'source_provenance':'retained in opt_raw and opt',
          'extraction_config':extraction_config(digest,start,end,minutes,args)}
    db.executemany('INSERT INTO meta VALUES(?,?)',items.items());db.commit()
   else:
    m=dict(db.execute('SELECT name,value FROM meta'))
    if m.get('extraction_config')!=extraction_config(digest,start,end,minutes,args):
     raise SystemExit('Cannot resume: source ledger, dates, minutes or extraction settings changed. Delete the .partial file.')
   done={r[0] for r in db.execute('SELECT step FROM extract_progress')}
   if 'daily' not in done:
    with pg.cursor() as cursor:
     warm=start-dt.timedelta(days=args.daily_warmup_days)
     cursor.execute(DAILY_SQL,(warm,end))
     fetched=[r for r in cursor.fetchall() if None not in r[1:5]]
     daily=[(str(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),int(r[5]),int(r[6]),int(r[5]>=args.min_session_minutes)) for r in fetched]
     cursor.execute(DAILY_RV_SQL,(warm,end))
     closeclose={str(d):(num(v),whole(n)) for d,v,n in cursor.fetchall()}
     daily_ext=[(str(r[0]),num(r[7]),whole(r[8]),num(r[9]),*closeclose.get(str(r[0]),(None,None)),session_quality(int(r[5]),args.min_session_minutes)) for r in fetched]
    db.executemany('INSERT INTO spot_daily VALUES(?,?,?,?,?,?,?,?)',daily)
    db.executemany('INSERT INTO spot_daily_ext VALUES(?,?,?,?,?,?,?)',daily_ext)
    db.execute("INSERT INTO extract_progress(step) VALUES('daily')");db.commit()
    print(f'Daily OHLC: {len(daily):,} sessions; complete: {sum(row[-1] for row in daily):,}',flush=True)
   for a,b in month_windows(start,end):
    step=f'month:{a:%Y-%m}'
    if step in done:print('Already extracted',step,flush=True);continue
    free_gb=shutil.disk_usage(staging.parent).free/1e9
    if free_gb<args.min_free_gb:raise SystemExit(f'Only {free_gb:.1f} GB free; stopping before {step}. Rerun with --resume after freeing space.')
    params=(a,b,times,a,b)
    content=[0]   # row order from PostgreSQL is not fixed, so the digest is order-independent
    nspot=extract(pg,SPOT_SQL,db,params,'spot',content);nopt=extract(pg,OPT_SQL,db,params,'option',content)
    db.execute('INSERT INTO extract_progress VALUES(?,?,?,?)',(step,nspot,nopt,f'{content[0]:016x}'));db.commit()
    print(f'{step}: spot {nspot:,}, options {nopt:,}; staging {staging.stat().st_size/1e9:.2f} GB; free {free_gb:.0f} GB',flush=True)
   nspot,nopt=db.execute('SELECT SUM(spot_rows),SUM(option_rows) FROM extract_progress').fetchone()
   finalize(db)
   ncan=db.execute('SELECT count(*) FROM opt').fetchone()[0]
   raw=db.execute('SELECT count(*) FROM opt_raw').fetchone()[0]
   print(f'Loaded spot source={nspot:,}; option raw={nopt:,}; canonical={ncan:,}; duplicate surplus={nopt-ncan:,}')
   if raw!=nopt:raise SystemExit(f'Row reconciliation failed: {raw} stored, {nopt} extracted')
   check=db.execute('PRAGMA quick_check').fetchone()[0]
   if check!='ok':raise SystemExit('SQLite integrity check failed: '+check)
   ok=complete and nspot>0 and ncan>0
   first,last=db.execute('SELECT MIN(day),MAX(day) FROM spot_daily').fetchone()
   db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[('cache_complete','1'),('research_eligible','1' if ok else '0'),
     ('market_content_digest',hashlib.sha256('|'.join(r[0]+r[1] for r in db.execute("SELECT step,content_digest FROM extract_progress WHERE step LIKE 'month:%' ORDER BY step")).encode()).hexdigest()),
     ('first_day',str(first)),('last_day',str(last)),('option_raw_rows',str(raw)),('option_canonical_rows',str(ncan))])
   db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
  finally:db.close()
  os.replace(staging,path)   # the previous good cache survives any failure above
 print('V4 snapshot complete:',path,'eligible=',ok)
if __name__=='__main__':main()
