#!/usr/bin/env python3
"""Every NIFTY spot minute, 09:15-15:29 IST, from market_data.spot_bars into a small SQLite file.

The research cache keeps only a few minutes a day; 45-minute bars need all of them. Reads the canonical
spot file (2021-05 onwards) and the 2017-2020 vendor spot (V4.9.18, data.datasets; the canonical file wins a
shared minute; the two do not overlap and leave January-May 2021 empty, so build the 2017-2020 block into its
own --out) and never writes to PostgreSQL. Credentials come from the PG* environment variables, as for the cache build."""
import argparse,datetime as dt,sqlite3
from pathlib import Path
from nifty_iv.data.research_cache import connect
from nifty_iv.data import datasets as ds
from nifty_iv.core import cli

SQL=f'''
SELECT DISTINCT ON (b.bar_time) b.trading_day::text,
 to_char(b.bar_time AT TIME ZONE 'Asia/Kolkata','HH24:MI'),b.open,b.high,b.low,b.close
FROM market_data.spot_bars b
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.symbol='NIFTY' AND {ds.SPOT_WHERE} AND f.status='completed'
 AND b.trading_day BETWEEN %s AND %s
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15'::time AND '15:29'::time
 AND b.open>0 AND b.high>0 AND b.low>0 AND b.close>0
ORDER BY b.bar_time,{ds.SPOT_PRIORITY},b.source_row_index
'''
DAYS_SQL=f'''SELECT DISTINCT b.trading_day::text FROM market_data.spot_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.symbol='NIFTY' AND {ds.SPOT_WHERE} AND f.status='completed' AND b.trading_day BETWEEN %s AND %s'''

def gaps(days,limit):
    """[(day, next day, calendar days)] where consecutive sessions are more than `limit` calendar days apart."""
    d=sorted(days);return [(a,b,(dt.date.fromisoformat(b)-dt.date.fromisoformat(a)).days) for a,b in zip(d,d[1:]) if (dt.date.fromisoformat(b)-dt.date.fromisoformat(a)).days>limit]

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--from-date',default='2021-01-01',help='Earlier data warms up the slow EMAs (V4.9.18 default 2021-01-01: the canonical file starts '
                    '2021-05-24, so this gives the same minutes as the earlier 2020-01-01 default now that 2017-2020 spot is in the database; '
                    'the 2017-2020 block: --from-date 2017-01-01 --to-date 2020-12-31 into its own --out)')
    ap.add_argument('--to-date',default='2026-07-01')
    ap.add_argument('--out',default='zone_spot.sqlite')
    ap.add_argument('--max-gap-days',type=int,default=10,help='refuse when two consecutive sessions (in the database range plus what --out already holds) '
                    'are more than this many calendar days apart: the 45-minute bars and their EMAs would run straight across the hole (V4.9.18)')
    a=ap.parse_args()
    start=dt.date.fromisoformat(a.from_date);end=dt.date.fromisoformat(a.to_date);total=0
    have=set()
    if Path(a.out).exists():
        with sqlite3.connect(f'file:{a.out}?mode=ro',uri=True) as old:
            if old.execute("SELECT 1 FROM sqlite_master WHERE name='spot_1m'").fetchone():have={d for (d,) in old.execute('SELECT DISTINCT day FROM spot_1m')}
    with connect() as pg:
        new={d for (d,) in pg.execute(DAYS_SQL,(start,end))};g=gaps(have|new,a.max_gap_days)
        if g:
            raise SystemExit('Spot sessions jump '+'; '.join(f'from {x} to {y} ({n} days)' for x,y,n in g[:5])+f' (more than --max-gap-days {a.max_gap_days}; '
                             f'{len(have)} sessions already in {a.out}, {len(new)} in the database for {start}..{end}). The bars and EMAs would run across '
                             'the hole: build each block into its own --out with --from-date / --to-date inside it. Nothing written.')
        db=sqlite3.connect(a.out)
        db.execute('CREATE TABLE IF NOT EXISTS spot_1m(day TEXT NOT NULL,minute TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,PRIMARY KEY(day,minute))')
        y=start
        while y<=end:
            stop=min(dt.date(y.year,12,31),end)
            rows=[(d,m,float(o),float(h),float(l),float(c)) for d,m,o,h,l,c in pg.execute(SQL,(y,stop))]   # NUMERIC arrives as Decimal
            db.executemany('INSERT OR REPLACE INTO spot_1m VALUES(?,?,?,?,?,?)',rows);db.commit();total+=len(rows)
            print(y.year,len(rows),'minutes',flush=True);y=stop+dt.timedelta(days=1)
    days=db.execute('SELECT COUNT(DISTINCT day),MIN(day),MAX(day) FROM spot_1m').fetchone()
    short=db.execute('SELECT day,COUNT(*) FROM spot_1m GROUP BY day HAVING COUNT(*)<370 ORDER BY day').fetchall()
    print(f'{total} minutes written; {days[0]} sessions {days[1]}..{days[2]}; sessions under 370 minutes: {short}')

if __name__=='__main__':main()
