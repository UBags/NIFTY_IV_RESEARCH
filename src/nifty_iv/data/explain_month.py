#!/usr/bin/env python3
"""Read-only query plans for one month of the extract. Run on a bounded month before any tuning."""
import argparse,calendar,datetime as dt
from nifty_iv.data.research_cache import connect,DAILY_SQL,DAILY_RV_SQL,SPOT_SQL,OPT_SQL,TIMES

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--month',required=True,help='YYYY-MM');a=p.parse_args()
    y,m=map(int,a.month.split('-'));s=dt.date(y,m,1);e=dt.date(y,m,calendar.monthrange(y,m)[1]);T=[dt.time.fromisoformat(x) for x in TIMES]
    with connect() as g,g.cursor() as c:
        c.execute('SET TRANSACTION READ ONLY')
        for name,sql,par in (('DAILY_SQL',DAILY_SQL,(s,e)),('DAILY_RV_SQL',DAILY_RV_SQL,(s,e)),('SPOT_SQL',SPOT_SQL,(s,e,T,s,e)),('OPT_SQL',OPT_SQL,(s,e,T,s,e))):
            print('---',name,flush=True);c.execute('EXPLAIN (ANALYZE,BUFFERS,TIMING OFF) '+sql,par);print('\n'.join(r[0] for r in c.fetchall()))

if __name__=='__main__':main()
