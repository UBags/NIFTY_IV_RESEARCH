#!/usr/bin/env python3
"""Every NIFTY spot minute, 09:15-15:29 IST, from market_data.spot_bars into a small SQLite file.

The research cache keeps only a few minutes a day; 45-minute bars need all of them. Reads the same
spot file as nifty-data-research-cache and never writes to PostgreSQL. Credentials come from the
PG* environment variables, as for the cache build."""
import argparse,datetime as dt,sqlite3
from nifty_iv.data.research_cache import connect
from nifty_iv.core import cli

SQL='''
SELECT DISTINCT ON (b.bar_time) b.trading_day::text,
 to_char(b.bar_time AT TIME ZONE 'Asia/Kolkata','HH24:MI'),b.open,b.high,b.low,b.close
FROM market_data.spot_bars b
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.symbol='NIFTY' AND f.relative_path='india-index-options-1m/index/NIFTY.parquet' AND f.status='completed'
 AND b.trading_day BETWEEN %s AND %s
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15'::time AND '15:29'::time
 AND b.open>0 AND b.high>0 AND b.low>0 AND b.close>0
ORDER BY b.bar_time,b.source_row_index
'''

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--from-date',default='2020-01-01',help='Earlier data warms up the slow EMAs');ap.add_argument('--to-date',default='2026-07-01')
    ap.add_argument('--out',default='zone_spot.sqlite');a=ap.parse_args()
    db=sqlite3.connect(a.out)
    db.execute('CREATE TABLE IF NOT EXISTS spot_1m(day TEXT NOT NULL,minute TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,PRIMARY KEY(day,minute))')
    start=dt.date.fromisoformat(a.from_date);end=dt.date.fromisoformat(a.to_date);total=0
    with connect() as pg:
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
