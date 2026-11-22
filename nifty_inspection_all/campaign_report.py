#!/usr/bin/env python3
"""Campaign economics by structure: does weekly premium become income, and where does P&L come from?

Tests the guidebook's claims on simulated campaigns. Research only; OHLC proxy fills.
"""
import argparse,csv,json,sqlite3,statistics
from contextlib import closing
from collections import defaultdict
from pathlib import Path

def summarise(rows):
    n=len(rows);out=dict(campaigns=n,closed=sum(r['status']!='unresolved' for r in rows))
    if not n:return out
    camp=[r['campaign'] for r in rows];cycles=sum(c['cycles'] for c in camp)
    gross=sum(c['gross_short_credit'] for c in camp);net=sum(c['net_short_pnl'] for c in camp)
    attr=defaultdict(float)
    for r in rows:
        for k,v in (r.get('pnl_attribution') or {}).items():attr[k]+=v
    pnl=[r['pnl_inr'] for r in rows];per_unit=[r['pnl_inr']/r['lot_units'] for r in rows]
    out.update(mean_pnl_per_unit=statistics.mean(per_unit),median_pnl_per_unit=statistics.median(per_unit),worst_pnl_per_unit=min(per_unit),
        win_rate=sum(p>0 for p in pnl)/n,short_cycles=cycles,cycle_win_rate=sum(c['winning_cycles'] for c in camp)/cycles if cycles else None,
        gross_short_credit=gross,net_short_pnl=net,premium_retained=net/gross if gross else None,
        reached_gross_payback=sum(c['gross_payback_cycle'] is not None for c in camp)/n,reached_net_payback=sum(c['net_payback_cycle'] is not None for c in camp)/n,
        reached_book_risk_free_on_gross=sum(c['gross_risk_free_cycle'] is not None for c in camp)/n,reached_risk_free_on_net=sum(c['net_risk_free_cycle'] is not None for c in camp)/n,
        attribution={k:round(v,2) for k,v in sorted(attr.items())})
    return out


def paired(rows):
    """Same entry day, short strike, tenor and policy: diagonal minus same-strike calendar, per index unit."""
    book=defaultdict(dict)
    for r in rows:book[(r['day'],r['strike'],r['target_tenor'],r['policy'])][r.get('long_strike_offset') or 0.]=r
    diffs=defaultdict(list)
    for legs in book.values():
        base=legs.get(0.)
        if not base:continue
        for off,r in legs.items():
            if off:diffs[off].append(r['pnl_inr']/r['lot_units']-base['pnl_inr']/base['lot_units'])
    return {str(k):dict(pairs=len(v),mean_diff_per_unit=statistics.mean(v),median_diff_per_unit=statistics.median(v),share_diagonal_better=sum(x>0 for x in v)/len(v)) for k,v in sorted(diffs.items())}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--trades',required=True);p.add_argument('--out',default='campaign_report_v4');a=p.parse_args()
    with closing(sqlite3.connect(a.trades)) as db:   # one row per economic trade: parameter aliases must not count twice
        rows=[json.loads(r[0]) for r in db.execute('SELECT details FROM trades GROUP BY economic_id')]
    groups=defaultdict(list)
    for r in rows:groups[(r['policy'],r.get('long_strike_offset') or 0.,r['target_tenor'],r['structure'])].append(r)
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    report=dict(research_only=True,fills='OHLC_fill_proxy',groups=[dict(policy=k[0],long_strike_offset=k[1],target_tenor=k[2],structure=k[3],**summarise(v)) for k,v in sorted(groups.items())],paired_with_same_strike_calendar=paired(rows))
    (out/'campaign_report.json').write_text(json.dumps(report,indent=2,default=str))
    fields=[f for f in report['groups'][0] if f!='attribution'] if report['groups'] else []
    with (out/'campaign_report.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(report['groups'])
    print('Campaign report:',out,'groups:',len(report['groups']))

if __name__=='__main__':main()
