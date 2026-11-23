#!/usr/bin/env python3
"""V3 quality and survivorship audit, independent of strategy profitability."""
import argparse,sqlite3,json,csv
from collections import defaultdict,Counter
from pathlib import Path
from nifty_iv.core import cli

def main():
    a=cli.Parser(description=__doc__)
    a.add_argument('--cache',default='research_cache_v4.sqlite')
    a.add_argument('--trades',default=None)
    a.add_argument('--out',default='coverage_v4')
    args=a.parse_args();db=sqlite3.connect(args.cache)
    meta=dict(db.execute('SELECT name,value FROM meta'))
    if meta.get('version')!='3':raise SystemExit('V3 cache required')
    rows=[]
    for provider,ym,n,liquid in db.execute('''SELECT provider,substr(day,1,7),COUNT(*),
      SUM(CASE WHEN volume>0 THEN 1 ELSE 0 END) FROM opt
      WHERE substr(ts,12,5)='09:46' GROUP BY provider,substr(day,1,7)'''):
        rows.append(dict(provider=provider,month=ym,entry_candles=n,liquid_candles=liquid))
    spot_sessions=dict(db.execute('SELECT substr(day,1,7),COUNT(*) FROM spot_daily GROUP BY substr(day,1,7)'))
    for row in rows:row['spot_sessions']=spot_sessions.get(row['month'],0)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    with (out/'provider_month_coverage.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['provider','month','entry_candles','liquid_candles','spot_sessions'])
        writer.writeheader();writer.writerows(rows)
    summary=dict(version='3',source=meta.get('source_fingerprint'),quality_by_provider_month=rows,
        completed_spot_sessions=db.execute('SELECT COUNT(*) FROM spot_daily WHERE complete_session=1').fetchone()[0],
        incomplete_spot_sessions=db.execute('SELECT COUNT(*) FROM spot_daily WHERE complete_session=0').fetchone()[0],
        retained_raw_candles=db.execute('SELECT COUNT(*) FROM opt_raw').fetchone()[0],
        canonical_candles=db.execute('SELECT COUNT(*) FROM opt').fetchone()[0],
        vendor_compared_rows=meta.get('vendor_compared_rows'),vendor_discordant_rows=meta.get('vendor_discordant_rows'))
    summary['session_quality']=dict(db.execute('SELECT session_quality,COUNT(*) FROM spot_daily_ext GROUP BY session_quality')) if db.execute("SELECT 1 FROM sqlite_master WHERE name='spot_daily_ext'").fetchone() else None
    summary['bar_integrity']=[dict(provider=p,bars=n,ohlc_violations=v,nonpositive_prices=z) for p,n,v,z in db.execute('''SELECT provider,COUNT(*),
        SUM(high<MAX(open,close) OR low>MIN(open,close) OR high<low),SUM(open<=0 OR high<=0 OR low<=0 OR close<=0) FROM opt GROUP BY provider''')]
    summary['duplicate_minutes_with_conflicting_close']=db.execute('''SELECT COUNT(*) FROM (SELECT 1 FROM opt_raw GROUP BY provider,ts,expiry,strike,cp
        HAVING COUNT(*)>1 AND MAX(close)-MIN(close)>MAX(2.0,0.03*AVG(close)))''').fetchone()[0]
    # Short or special sessions (drills, Muhurat-style days, outages) that can enter marks or Sharpe calendars.
    has_0945={r[0] for r in db.execute("SELECT day FROM spot WHERE substr(ts,12,5)='09:45'")}
    has_1529={r[0] for r in db.execute("SELECT day FROM spot WHERE substr(ts,12,5)='15:29'")}
    summary['special_sessions']=[dict(day=d,minute_bars=n,has_0945=d in has_0945,has_1529=d in has_1529)
        for d,n in db.execute('SELECT day,minute_bars FROM spot_daily WHERE complete_session=0 ORDER BY day')]
    # Both spot tables read one source file; this catches any later divergence.
    gaps=sorted(abs(c-s) for c,s in db.execute("""SELECT d.close,x.close FROM spot_daily d JOIN spot x
        ON x.ts=d.day||' 15:29:00' WHERE x.close>0"""))
    summary['spot_source_consistency']=dict(days=len(gaps),median_abs_close_gap_pts=gaps[len(gaps)//2] if gaps else None,
        max_abs_close_gap_pts=gaps[-1] if gaps else None,status='pass' if not gaps or gaps[-1]<=.05 else 'REVIEW',note='spot_daily close vs timed 15:29 spot close; both come from one file, so any gap needs explaining')
    db.close()
    if args.trades:
        tr=sqlite3.connect(args.trades)
        summary['outcomes']=dict(tr.execute('SELECT status,COUNT(*) FROM trades GROUP BY status'))
        summary['unique_economic_trades']=tr.execute('SELECT COUNT(DISTINCT economic_id) FROM trades').fetchone()[0]
        summary['diagnostics']=dict(tr.execute('SELECT reason,count FROM diagnostics'))
        summary['mark_methods']=dict(tr.execute('SELECT quality,COUNT(*) FROM marks GROUP BY quality'))
        summary['incomplete_positions_retained']=summary['outcomes'].get('unresolved',0)
        tr.close()
    (out/'quality_summary.json').write_text(json.dumps(summary,indent=2))
    print('Quality audit ->',out)

if __name__=='__main__':main()
