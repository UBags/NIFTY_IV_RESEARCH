#!/usr/bin/env python3
"""Read-only: every NIFTY spot file in market_data against the one the cache uses. Exit 1 on unexplained differences."""
import argparse,json,sys
from nifty_iv.data.research_cache import connect
from nifty_iv.core import cli

CANON='india-index-options-1m/index/NIFTY.parquet'
FILES="""SELECT f.file_id,f.dataset,f.relative_path,f.status,COUNT(*),MIN(b.trading_day)::text,MAX(b.trading_day)::text
FROM market_data.spot_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id WHERE b.symbol='NIFTY'
GROUP BY 1,2,3,4 ORDER BY 3"""
COMPARE="""SELECT COUNT(*),COUNT(*) FILTER (WHERE ABS(a.close-b.close)>%s),COALESCE(MAX(ABS(a.close-b.close)),0)::float
FROM market_data.spot_bars a JOIN market_data.spot_bars b ON a.bar_time=b.bar_time AND b.symbol='NIFTY'
WHERE a.symbol='NIFTY' AND a.source_file_id=%s AND b.source_file_id=%s"""

def main():
    p=cli.Parser(description=__doc__);p.add_argument('--tolerance',type=float,default=.05);p.add_argument('--out',default='spot_source_report.json');a=p.parse_args()
    with connect() as pg,pg.cursor() as c:
        c.execute(FILES);rows=[dict(zip(('file_id','dataset','relative_path','status','bars','first_day','last_day'),r)) for r in c.fetchall()]
        canon=[r for r in rows if r['relative_path']==CANON and r['status']=='completed']
        if len(canon)!=1:raise SystemExit(f'Expected one completed {CANON}, found {len(canon)}')
        bad=0
        for r in rows:
            if r is canon[0]:r['role']='canonical';continue
            c.execute(COMPARE,(a.tolerance,r['file_id'],canon[0]['file_id']));n,d,m=c.fetchone()
            r.update(role='other',overlap_bars=n,discordant_bars=d,max_abs_close_gap=m);bad+=d
    report=dict(canonical=CANON,tolerance=a.tolerance,files=rows,discordant_bars=bad)
    open(a.out,'w').write(json.dumps(report,indent=2,default=str));print(json.dumps(report,indent=2,default=str))
    sys.exit(1 if bad else 0)

if __name__=='__main__':main()
