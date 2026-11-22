#!/usr/bin/env python3
"""Read-only: how wide is the market, per tenor and moneyness, as far as minute bars can show?

Two independent measures, both on real traded bars in the enriched cache. Neither is a bid/ask.
  smile_residual_pts  price distance from a local quadratic fit of IV across strikes, same minute and expiry (quote noise plus half-spread)
  bar_range_pts       high minus low of bars with a small traded size, an upper-side view of the spread crossed inside a minute
Compare both with the --slippage model in the last column before trusting a backtest's costs.
"""
import argparse,csv,json,sqlite3,statistics as st
from collections import defaultdict
from pathlib import Path
import numpy as np
from nifty_iv.ivcal.candidate_core import dte

def model_slip(base,tenor,volume):
    return base*(max(tenor,7)/7)**.5*(1+2/max(1,volume)**.5)

BUCKETS=((1,3,'1-3d'),(4,8,'4-8d'),(9,21,'9-21d'),(22,45,'22-45d'),(46,75,'46-75d'),(76,130,'76-130d'),(131,400,'131d+'))
def bucket(d):return next((b for a,z,b in BUCKETS if a<=d<=z),None)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache',required=True);p.add_argument('--provider',default='rissin_upstox');p.add_argument('--out',default='slippage_calibration')
    p.add_argument('--slippage',type=float,default=.5);p.add_argument('--minute',default='09:47',help='Cached minute to sample');p.add_argument('--max-days',type=int,default=250)
    a=p.parse_args()
    db=sqlite3.connect(a.cache);db.row_factory=sqlite3.Row
    days=[r[0] for r in db.execute('SELECT day FROM spot_daily ORDER BY day')];days=days[::max(1,len(days)//a.max_days)]
    out=defaultdict(lambda:defaultdict(list))
    for day in days:
        rows=db.execute('''SELECT o.expiry,o.strike,o.cp,o.close,o.high,o.low,o.volume,g.iv,g.vega,f.forward FROM opt o
          JOIN greeks_cache g ON g.provider=o.provider AND g.ts=o.ts AND g.expiry=o.expiry AND g.strike=o.strike AND g.cp=o.cp
          JOIN forward_cache f ON f.provider=o.provider AND f.ts=o.ts AND f.expiry=o.expiry
          WHERE o.provider=? AND o.ts=? AND o.volume>0 AND g.iv IS NOT NULL''',(a.provider,f'{day} {a.minute}:00')).fetchall()
        by=defaultdict(list)
        for r in rows:by[r['expiry'],r['cp']].append(r)
        for (ex,cp),quotes in by.items():
            b=bucket(dte(day,ex))
            if not b or len(quotes)<6:continue
            f=quotes[0]['forward'];k=np.array([q['strike'] for q in quotes]);iv=np.array([q['iv'] for q in quotes]);vg=np.array([q['vega'] for q in quotes])
            x=np.log(k/f);near=np.abs(x)<.06
            if near.sum()<6:continue
            res=vg[near]*(iv[near]-np.polyval(np.polyfit(x[near],iv[near],3),x[near]))   # price points, per unit of volatility moved
            for q,e in zip([q for q,n in zip(quotes,near) if n],res):
                m='atm' if abs(np.log(q['strike']/f))<.01 else 'near' if abs(np.log(q['strike']/f))<.03 else 'wing'
                c=out[b,m];c['res'].append(abs(float(e)));c['range'].append(float(q['high']-q['low']));c['vol'].append(int(q['volume']));c['dte'].append(dte(day,ex))
    dest=Path(a.out);dest.mkdir(parents=True,exist_ok=True);rows=[]
    order=[b for _,_,b in BUCKETS]
    for (b,m),c in sorted(out.items(),key=lambda z:(order.index(z[0][0]),z[0][1])):
        res,rng,vol,dd=c['res'],c['range'],c['vol'],c['dte']
        rows.append(dict(tenor=b,moneyness=m,bars=len(res),smile_residual_pts_median=round(st.median(res),2),smile_residual_pts_p90=round(float(np.quantile(res,.9)),2),
            bar_range_pts_median=round(st.median(rng),2),median_volume=st.median(vol),model_slip_pts=round(model_slip(a.slippage,st.median(dd),st.median(vol)),2)))
    with (dest/'slippage_by_tenor.csv').open('w',newline='') as f:
        if rows:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (dest/'meta.json').write_text(json.dumps(dict(cache=str(a.cache),provider=a.provider,minute=a.minute,sessions=len(days),warning='Minute bars do not show bid/ask. Treat as a sanity check on --slippage.')))
    for r in rows:print(r)

if __name__=='__main__':main()
