#!/usr/bin/env python3
"""Train-only view of how much P&L comes from waiting on a long after its short is gone.

Reads only trades whose planned exit precedes --cutoff, so it cannot open a holdout. Pass the
train_end_exclusive from rankings.json. Expiry eras follow each trade's short expiry, because an
entry in late August 2025 can already be on a Tuesday contract."""
import argparse,collections,json,math,sqlite3,statistics
from nifty_iv.core import cli
TUESDAY_FIRST='2025-09-01'

def load(path,cutoff):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row;rows=[]
    for r in db.execute("SELECT * FROM trades WHERE scheduled_exit_day<? AND status='closed_traded' ORDER BY day",(cutoff,)):
        d=json.loads(r['details']);mark=db.execute('SELECT pnl_inr FROM marks WHERE trade_id=? AND day=?',(r['id'],r['scheduled_exit_day'])).fetchone()
        unit=r['pnl_inr']/r['lot_units'];lag=d['exit_reason'] in ('late_liquid_exit','forced_exit_lag_cap')
        rows.append(dict(tenor=r['target_tenor'],short_expiry=d['short_expiry'],unit=unit,planned=(mark[0]/r['lot_units'] if lag and mark else unit),lag=lag,forced=d['exit_reason']=='forced_exit_lag_cap'))
    db.close();return rows

def block(rows):
    if not rows:return dict(trades=0)
    weeks=collections.defaultdict(list)
    for x in rows:weeks[x['short_expiry']].append(x['unit'])
    means=[statistics.mean(v) for v in weeks.values()]
    t=statistics.mean(means)/(statistics.stdev(means)/math.sqrt(len(means))) if len(means)>2 and statistics.stdev(means)>0 else None
    return dict(trades=len(rows),weeks=len(means),mean_per_unit=statistics.mean(x['unit'] for x in rows),mean_at_planned_mark=statistics.mean(x['planned'] for x in rows),
                clustered_t=t,lagged=sum(x['lag'] for x in rows),forced=sum(x['forced'] for x in rows),post_schedule_drift=sum(x['unit']-x['planned'] for x in rows))

def report(rows):
    out={}
    for tenor in sorted({x['tenor'] for x in rows}):
        pick=[x for x in rows if x['tenor']==tenor]
        out[str(tenor)]=dict(all=block(pick),thursday_era=block([x for x in pick if x['short_expiry']<TUESDAY_FIRST]),tuesday_era=block([x for x in pick if x['short_expiry']>=TUESDAY_FIRST]))
    return out

def main():
    ap=cli.Parser(description=__doc__);ap.add_argument('--trades',required=True);ap.add_argument('--cutoff',required=True,help='train_end_exclusive from rankings.json')
    a=ap.parse_args();print(json.dumps(report(load(a.trades,a.cutoff)),indent=1))
if __name__=='__main__':main()
