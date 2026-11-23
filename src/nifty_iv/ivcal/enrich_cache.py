#!/usr/bin/env python3
"""V3+ parity + memory-bounded batch IV/Greeks enrichment.

Runs AFTER cache snapshot finishes, ONLY modifies local SQLite cache.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,math,sqlite3
from itertools import groupby
import numpy as np
from nifty_iv.ivcal.quant_v3 import parity_ols
from nifty_iv.ivcal.quant_fast_v3 import invert_and_greeks
from nifty_iv.core import cli

def t_years(expiry,ts):
    when=dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone(dt.timedelta(hours=5,minutes=30)))
    end=dt.datetime.combine(dt.date.fromisoformat(expiry),dt.time(15,30),when.tzinfo)
    return (end-when).total_seconds()/(365.25*86400)

def vendor_concordance(db,points=2.,fraction=.03):
    """Flag minutes where BOTH vendors report volume yet open or close disagree.

    Absence from the table means agreement OR only one vendor reporting; it is a
    bad-tick screen, not evidence that a quote was executable.
    """
    db.executescript('''DROP TABLE IF EXISTS vendor_discordance;
      CREATE TABLE vendor_discordance(ts TEXT NOT NULL,expiry TEXT NOT NULL,strike REAL NOT NULL,cp TEXT NOT NULL,
       open_a REAL,open_b REAL,close_a REAL,close_b REAL,PRIMARY KEY(ts,expiry,strike,cp));''')
    join='''FROM opt a JOIN opt b ON b.provider='thetrademarkk' AND b.ts=a.ts AND b.expiry=a.expiry
      AND b.strike=a.strike AND b.cp=a.cp WHERE a.provider='rissin_upstox' AND a.volume>0 AND b.volume>0'''
    compared=db.execute('SELECT COUNT(*) '+join).fetchone()[0]
    db.execute('INSERT INTO vendor_discordance SELECT a.ts,a.expiry,a.strike,a.cp,a.open,b.open,a.close,b.close '+join+
      ''' AND (ABS(a.close-b.close)>MAX(?,?*(a.close+b.close)/2) OR ABS(a.open-b.open)>MAX(?,?*(a.open+b.open)/2))''',
      (points,fraction,points,fraction))
    discordant=db.execute('SELECT COUNT(*) FROM vendor_discordance').fetchone()[0]
    db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[('vendor_compared_rows',str(compared)),('vendor_discordant_rows',str(discordant))])
    return compared,discordant

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--cache',default='research_cache_v4.sqlite')
    ap.add_argument('--rate',type=float,default=.06)
    ap.add_argument('--min-pair-volume',type=int,default=1)
    ap.add_argument('--max-parity-bps',type=float,default=40.)
    ap.add_argument('--batch-rows',type=int,default=30000)
    a=ap.parse_args()
    if not 100<=a.batch_rows<=100000:ap.error('batch-rows between 100 and 100000')
    db=sqlite3.connect(a.cache);db.row_factory=sqlite3.Row
    meta=dict(db.execute('SELECT name,value FROM meta'))
    if meta.get('version')!='3' or meta.get('research_eligible')!='1':raise SystemExit('V3 complete research cache required')
    # Every run replaces all derived rows; until it finishes the cache reads as not enriched.
    run_id=hashlib.sha256(json.dumps(dict(source=meta.get('source_fingerprint'),rate=a.rate,min_pair_volume=a.min_pair_volume,
        max_parity_bps=a.max_parity_bps,solver='bracketed_v1',parity='pruned_bps_v1'),sort_keys=True).encode()).hexdigest()
    db.execute("INSERT OR REPLACE INTO meta VALUES('enrichment_complete','0')")
    db.execute("DELETE FROM meta WHERE name='enrichment_run_id'")
    db.execute('DELETE FROM greeks_cache');db.execute('DELETE FROM forward_cache');db.commit()
    spot={row[0]:row[1] for row in db.execute('SELECT ts,close FROM spot WHERE close>0')}
    src=db.execute('SELECT provider,ts,expiry,strike,cp,close,volume,source_id,source_row FROM opt ORDER BY provider,ts,expiry,strike,cp')
    forward_rows=[];tasks=[];n=0;greek_count=0;quality_counts={};fallback_count=0
    def flush():
        nonlocal forward_rows,tasks,greek_count,fallback_count
        if not tasks and not forward_rows:return
        if forward_rows:db.executemany('INSERT OR REPLACE INTO forward_cache VALUES(?,?,?,?,?,?,?,?,?,?)',forward_rows)
        if tasks:
            f=np.array([x[2] for x in tasks]);k=np.array([x[1]['strike'] for x in tasks]);
            t=np.array([x[3] for x in tasks]);px=np.array([x[1]['close'] if x[1]['close'] is not None else np.nan for x in tasks]);
            cp=np.array([x[1]['cp'] for x in tasks]);r=np.array([x[4] for x in tasks]);
            ss=np.array([x[5] for x in tasks])
            iv,delta,gamma,vega,report=invert_and_greeks(f,k,t,px,cp,r,spot=ss)
            fallback_count+=report['fallback']
            def finite(x):return float(x) if np.isfinite(x) else None
            grec=[]
            for row,(provider,source,_,_,_,_) in enumerate(tasks):
                grec.append((provider,source['ts'],source['expiry'],float(source['strike']),source['cp'],
                    finite(iv[row]),finite(delta[row]),finite(gamma[row]),finite(vega[row]),
                    source['source_id'],source['source_row']))
            db.executemany('INSERT OR REPLACE INTO greeks_cache VALUES(?,?,?,?,?,?,?,?,?,?,?)',grec)
            greek_count+=len(grec)
        db.commit();forward_rows=[];tasks=[]
    for (provider,ts,expiry),group in groupby(src,key=lambda x:(x[0],x[1],x[2])):
        records=[dict(row) for row in group];s=spot.get(ts);t=t_years(expiry,ts)
        quotes={(float(row['strike']),row['cp']):{'close':row['close'],'volume':row['volume']}
                for row in records}
        est=parity_ols(quotes,s,t,a.rate,a.min_pair_volume,a.max_parity_bps)
        quality_counts[est.quality]=quality_counts.get(est.quality,0)+1
        forward_rows.append((provider,ts,expiry,est.forward,est.matched_pairs,est.dispersion_bps,
                est.quality,est.discount,est.rate,est.estimator))
        if est.forward is not None:
            for record in records:
                record['ts']=ts;record['expiry']=expiry
                tasks.append((provider,record,est.forward,t,est.rate,float(s)))
        n+=1
        if len(tasks)>=a.batch_rows:
            flush()
            print(f'Processed {n:,} groups / {greek_count:,} IV rows / {fallback_count:,} Brent fallbacks',flush=True)
    src.close();flush()
    compared,discordant=vendor_concordance(db)
    for key,val in (('enrichment_rate',str(a.rate)),('enrichment_min_pair_volume',str(a.min_pair_volume)),
       ('enrichment_max_parity_bps',str(a.max_parity_bps)),('enrichment_run_id',run_id),('enrichment_complete','1')):
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,val))
    db.commit();db.close()
    print(f'Vendor concordance: {compared:,} two-vendor traded quotes compared, {discordant:,} discordant')
    print('V3 enrichment complete:',n,'groups,',greek_count,'IV rows,',fallback_count,'fallbacks, quality',quality_counts)

if __name__=='__main__':main()
