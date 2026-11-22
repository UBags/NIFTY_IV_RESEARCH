#!/usr/bin/env python3
"""Two strategies on their own opportunity universes, then on the entries both could trade.

Usage: compare_structures.py --trades t.sqlite --a iron_fly:tm1 --b put:tm1[:60] [--hedge none]
Entries pair on provider, day, short expiry, actual short strike and hedge. Identical-mark aliases count once; several genuinely
different rows on one key (for example two calendar tenors) are averaged and counted as many_to_one_keys.
all_opened keeps unresolved trades at their conservative horizon marks; those marks are not realized returns.
Per index unit and per rupee of entry scenario capital. Capital and payoffs differ, so neither alone is a risk-adjusted verdict. Research only.
"""
import argparse,json,math,sqlite3,statistics as st
from collections import Counter,defaultdict
from contextlib import closing

def side(db,spec,hedge='none'):
    parts=spec.split(':');where='structure=? AND exit_policy=? AND hedge=?';args=parts[:2]+[hedge]
    if len(parts)>2:where+=' AND target_tenor=?';args.append(int(parts[2]))
    rows=defaultdict(dict)
    for provider,day,expiry,strike,pnl,lot,cap,status,mode,mtm in db.execute(f"""SELECT provider,day,json_extract(details,'$.short_expiry'),json_extract(details,'$.strike'),
        pnl_inr,lot_units,entry_capital_scenario,status,strike_mode,mtm_id FROM trades WHERE {where}""",args):
        rows[provider,day,expiry,float(strike),hedge][mtm]=(pnl/lot,pnl/cap,status,mode)
    return {k:dict(unit=st.mean(x[0] for x in v.values()),capital=st.mean(x[1] for x in v.values()),unresolved=any(x[2]=='unresolved' for x in v.values()),
        rows=len(v),strike_modes=sorted({x[3] for x in v.values()})) for k,v in rows.items()}

def summary(values):
    n=len(values);m=st.mean(values) if n else None;sd=st.stdev(values) if n>1 else None
    return dict(n=n,mean=m,naive_t=m/sd*math.sqrt(n) if sd else None)

def universe(x):
    obs=[v for v in x.values() if not v['unresolved']];open_=[v for v in x.values() if v['unresolved']]
    return dict(all_opened=dict(per_unit=summary([v['unit'] for v in x.values()]),per_capital=summary([v['capital'] for v in x.values()])),
        fully_observed_diagnostic=dict(per_unit=summary([v['unit'] for v in obs]),per_capital=summary([v['capital'] for v in obs])),
        unresolved=dict(n=len(open_),mean_conservative_mark_per_unit=st.mean(v['unit'] for v in open_) if open_ else None),
        many_to_one_keys=sum(v['rows']>1 for v in x.values()),strike_modes=sorted({m for v in x.values() for m in v['strike_modes']}))

def unmatched(mine,other):
    days={(k[0],k[1],k[4]) for k in other};reasons=Counter('other_strike_or_expiry_that_day' if (k[0],k[1],k[4]) in days else 'no_entry_that_day' for k in set(mine)-set(other))
    return dict(reasons)

def paired(a,b,keys):
    return dict(n=len(keys),a_per_unit=summary([a[k]['unit'] for k in keys]),b_per_unit=summary([b[k]['unit'] for k in keys]),
        a_minus_b_per_unit=summary([a[k]['unit']-b[k]['unit'] for k in keys]),a_minus_b_per_capital=summary([a[k]['capital']-b[k]['capital'] for k in keys]))

def compare(a,b):
    both=sorted(set(a)&set(b));seen=[k for k in both if not a[k]['unresolved'] and not b[k]['unresolved']]
    return dict(universe_a=universe(a),universe_b=universe(b),unmatched_a=unmatched(a,b),unmatched_b=unmatched(b,a),
        paired_all_opened=paired(a,b,both),paired_fully_observed_diagnostic=paired(a,b,seen),
        note='naive_t ignores overlap between entries and overstates significance. Unresolved trades carry conservative marks, not realized returns')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--trades',required=True);p.add_argument('--a',required=True);p.add_argument('--b',required=True);p.add_argument('--hedge',default='none');a=p.parse_args()
    with closing(sqlite3.connect(a.trades)) as db:print(json.dumps(compare(side(db,a.a,a.hedge),side(db,a.b,a.hedge)),indent=2))
if __name__=='__main__':main()
