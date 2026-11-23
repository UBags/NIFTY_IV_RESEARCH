#!/usr/bin/env python3
"""Same strategy under different fill models: what is lost, and how much slippage it can absorb.

Usage: nifty-iv-compare-fill-models --runs next_bar_open=a.sqlite traded_only=b.sqlite delay1=c.sqlite ...
The first run is the reference. Trades pair on economic identity, and parameter aliases count once.
Models other than next_bar_open are stresses or diagnostics. None of these is a confirmed achievable price.
"""
import argparse,json,math,sqlite3,statistics
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from nifty_iv.core import cli

def load(path):
    with closing(sqlite3.connect(path)) as db:
        rows={};ids={}
        for tid,econ,pnl,lot,status in db.execute('SELECT id,economic_id,pnl_inr,lot_units,status FROM trades ORDER BY id'):
            if econ not in rows:rows[econ]=dict(unit=pnl/lot,status=status,lot=lot,inr=pnl);ids[tid]=econ
        fills=defaultdict(int);zero=set();traded=defaultdict(float)
        for tid,details in db.execute("SELECT trade_id,details FROM fills WHERE details LIKE '%liquid_bar_proxy%'"):
            if tid in ids:
                fills[ids[tid]]+=1;traded[ids[tid]]+=abs(json.loads(details).get('units') or 0)
                if '"fill_bar_zero_volume": true' in details:zero.add(ids[tid])
        days=defaultdict(float)
        for tid in ids:
            last=0.
            for day,pnl in db.execute('SELECT day,pnl_inr FROM marks WHERE trade_id=? ORDER BY day',(tid,)):
                days[day]+=(pnl-last)/rows[ids[tid]]['lot'];last=pnl
    return rows,fills,zero,days,traded


def breakeven_slippage(total_pnl_inr,traded_units):
    """Extra price points per traded unit that take the book to zero, weighted by what each fill actually trades."""
    return total_pnl_inr/traded_units if traded_units else None


def book_stats(days,sessions=None):
    """All admitted trades held together, per index unit, over one session calendar with flat days at zero. A diagnostic book, not a capital-constrained portfolio."""
    if sessions is not None:days={d:days.get(d,0.) for d in sessions}
    if len(days)<2:return dict(calendar_sharpe=None,max_drawdown=None)
    inc=[days[d] for d in sorted(days)];sd=statistics.stdev(inc);eq=peak=dd=0.
    for x in inc:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(calendar_sharpe=statistics.mean(inc)/sd*math.sqrt(252) if sd>0 else None,max_drawdown=dd)


def compare(runs,sessions=None,eval_start=None,eval_end=None):
    """sessions: exchange-session calendar. A fixed eval_start and eval_end make a model's score independent of which other runs are supplied."""
    loaded=[load(path) for _,path in runs];base_name=runs[0][0];base=loaded[0][0];out=[]
    marked=[d for x in loaded for d in x[3]]
    first=eval_start or (min(marked) if marked else '');last=eval_end or (max(marked) if marked else '')
    window=sorted({d for d in (sessions if sessions is not None else marked) if first<=d<=last})
    for (name,path),(rows,fills,zero,days,traded) in zip(runs,loaded):
        shared=set(rows)&set(base);mean=statistics.mean(r['unit'] for r in rows.values()) if rows else None
        legs=statistics.mean(fills.get(t,0) for t in rows) if rows else None
        out.append(dict(model=name,trades=len(rows),failed_versus_reference=len(set(base)-set(rows)),extra_versus_reference=len(set(rows)-set(base)),unresolved=sum(r['status']=='unresolved' for r in rows.values()),
            mean_pnl_per_unit=mean,paired_trades=len(shared),paired_mean_pnl_per_unit=statistics.mean(rows[t]['unit'] for t in shared) if shared else None,paired_mean_diff_per_unit=statistics.mean(rows[t]['unit']-base[t]['unit'] for t in shared) if shared else None,
            trades_using_zero_volume_fill_bar=len(zero),pnl_per_unit_on_those_trades=sum(rows[t]['unit'] for t in zero),**book_stats(days,window),sessions_scored=len(window),
            mean_fills_per_trade=legs,breakeven_extra_slippage_pts_per_traded_unit=breakeven_slippage(sum(r['inr'] for r in rows.values()),sum(traded.get(t,0.) for t in rows))))
    return dict(reference=base_name,research_only=True,eval_start=first,eval_end=last,calendar='cache_sessions' if sessions is not None else 'marks_union_no_session_calendar',results=out)


def main():
    p=cli.Parser(description=__doc__);p.add_argument('--runs',nargs='+',required=True);p.add_argument('--cache',required=True,help='Supplies the session calendar, so flat days count');p.add_argument('--eval-start',required=True);p.add_argument('--eval-end',required=True);p.add_argument('--out',default='fill_model_comparison.json');a=p.parse_args()
    
    with closing(sqlite3.connect(a.cache)) as c:sessions=[r[0] for r in c.execute('SELECT day FROM spot_daily ORDER BY day')]
    report=compare([x.split('=',1) for x in a.runs],sessions,a.eval_start,a.eval_end);Path(a.out).write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))

if __name__=='__main__':main()
