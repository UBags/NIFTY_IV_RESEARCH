#!/usr/bin/env python3
"""Option-data coverage diagnostic for a zone_paths run (V4.8.5; --tenor-check V4.9.3).

nifty-zone-paths records every data gap it meets in the `gaps` table of the paths file:
  no_long_expiry  a calendar found no listed expiry more than 30 days out on its fill day
  not_extracted   a contract the path needed has no minute that day in the options extraction
This script classifies each gap against the options file (listing, extraction plan, extracted minutes) and writes:
  coverage_gaps.csv      one row per gap with its classification
  coverage_summary.csv   counts by every grouping below
  coverage_listing.csv   per year and provider: trading days, days with a listed expiry > 30 days out
Classes for not_extracted:
  expiry_not_listed   the expiry is not in the source listing that day (source has no such contract)
  not_planned         the expiry is listed but nifty-zone-contracts never planned this strike that day (fix: plan / --strike-pad)
  planned_no_source   planned and queried, but the source returned no bars for it that day (absent from the vendor data)
Gap counts are events, not trades: one entry can record a gap for each missing leg, and roll reopens are flagged (roll=1).
Reads both files only."""
import argparse,collections,csv,datetime as dt,sqlite3,sys
from nifty_iv.core.contract_specs import monthly_like
from pathlib import Path

def dte_bucket(d):
    if d is None:return 'none'
    for hi,lab in ((7,'0-7'),(30,'8-30'),(45,'31-45'),(60,'46-60'),(90,'61-90')):
        if d<=hi:return lab
    return '>90'

def moneyness(cp,offset):
    """Label from the strike's distance to ATM in POINTS (strike - ATM): a put above ATM / a call below ATM is ITM. V4.9.7: callers pass
    strike - atm (the gap row's own strike and ATM), so levels-mode rows (offset = level, > 0 OTM on both sides) are labelled correctly."""
    if not offset:return 'ATM'
    itm=(offset>0)==(cp=='PE');return ('ITM' if itm else 'OTM')+f'{int(abs(offset))}'

def tenor_coverage(listing,entry_days=None):
    """Feasibility of monthly-expiry policies from the source listing (before any build). A monthly expiry is a listed expiry in
    the last seven days of its month (contract_specs_v2.monthly_like; holiday-shifted monthlies included).
      H1  current-month monthly with >= 21 calendar days left on the entry day (policy-eligible: such a monthly exists that month;
          listed: it is in that day's listing)
      H2  next calendar month's monthly listed on the entry day
    listing: {day: iterable of expiries}. entry_days: {label: [days]} (e.g. signal entry days per side), else every listed day.
    Returns rows per (population, year)."""
    monthly=collections.defaultdict(set)
    for d,ex in listing.items():
        for e in ex:
            if monthly_like(e):monthly[e[:7]].add(e)
    pops=entry_days or {'all_listed_days':sorted(listing)};rows=[]
    for lab,days in pops.items():
        acc=collections.defaultdict(lambda:collections.Counter())
        for d in days:
            dd=dt.date.fromisoformat(d);y=d[:4];c=acc[y];c['days']+=1;lst=set(listing.get(d,()))
            cur=[m for m in monthly.get(d[:7],()) if (dt.date.fromisoformat(m)-dd).days>=21]
            c['h1_policy_eligible']+=bool(cur);c['h1_listed']+=any(m in lst for m in cur)
            nm=(dd.replace(day=1)+dt.timedelta(days=32)).strftime('%Y-%m');c['h2_listed']+=any(m in lst for m in monthly.get(nm,()))
            c['any_expiry_over_30d_listed']+=any((dt.date.fromisoformat(e)-dd).days>30 for e in lst)
        for y,c in sorted(acc.items()):rows.append(dict(population=lab,year=y,**{k:c[k] for k in ('days','h1_policy_eligible','h1_listed','h2_listed','any_expiry_over_30d_listed')}))
    return rows

def tenor_main(a):
    O=sqlite3.connect(f'file:{a.options}?mode=ro',uri=True);listing=collections.defaultdict(set)
    for d,e in O.execute('SELECT day,expiry FROM listing'):listing[d].add(e)
    pops=None
    if a.paths and Path(a.paths).exists():
        P=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);day=[d for (d,) in P.execute('SELECT day FROM bars ORDER BY idx')];pops=collections.defaultdict(list)
        for side,t in P.execute('SELECT DISTINCT side,bar FROM signals'):
            if t+1<len(day):pops[f'signal_entries_{side}'].append(day[t+1])
        pops['all_listed_days']=sorted(listing);P.close()
    rows=tenor_coverage(listing,pops);out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    with open(out/'coverage_tenor.csv','w',newline='') as f:w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)
    print('Monthly-expiry policy coverage from the source listing (days):');print(f"  {'population':<22} year  days  H1 eligible  H1 listed  H2 listed  any >30d listed")
    for r in rows:print(f"  {r['population']:<22} {r['year']} {r['days']:>5} {r['h1_policy_eligible']:>12} {r['h1_listed']:>10} {r['h2_listed']:>10} {r['any_expiry_over_30d_listed']:>16}")
    print(f'-> {out}/coverage_tenor.csv');O.close()

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',default='zone_paths.sqlite');ap.add_argument('--options',default='zone_options_wide.sqlite')
    ap.add_argument('--out',default='coverage');ap.add_argument('--no-history',action='store_true',help='skip the one full scan of opt_1m for other days of each missing contract')
    ap.add_argument('--tenor-check',action='store_true',help='only: how often a current-month monthly with >= 21 days (H1) and the next-month monthly (H2) are listed, per year, on all days and on signal entry days (V4.9.3)')
    a=ap.parse_args(argv)
    if a.tenor_check:return tenor_main(a)
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    P=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);O=sqlite3.connect(f'file:{a.options}?mode=ro',uri=True)
    if not P.execute("SELECT 1 FROM sqlite_master WHERE name='gaps'").fetchone():sys.exit(f'{a.paths} has no gaps table: rebuild it with nifty-zone-paths V4.8.5 or later')
    cols=[r[1] for r in P.execute('PRAGMA table_info(gaps)')];gaps=[dict(zip(cols,r)) for r in P.execute('SELECT * FROM gaps')]
    listing=collections.defaultdict(lambda:collections.defaultdict(set))           # day -> expiry -> providers
    for d,pv,e in O.execute('SELECT day,provider,expiry FROM listing'):listing[d][e].add(pv)
    days={g['day'] for g in gaps if g['event']=='not_extracted'};planned={};have={}
    for d in sorted(days):
        planned[d]={(e,float(k),cp) for e,k,cp in O.execute('SELECT expiry,strike,cp FROM targets WHERE day=?',(d,))}
        have[d]=collections.defaultdict(set)
        for e,k,cp in O.execute('SELECT DISTINCT expiry,strike,cp FROM opt_1m WHERE day=?',(d,)):have[d][(e,cp)].add(float(k))
    hist={}
    if not a.no_history:
        want={(g['expiry'],float(g['strike']),g['cp']) for g in gaps if g['event']=='not_extracted'}
        if want:
            O.execute('CREATE TEMP TABLE w(expiry TEXT,strike REAL,cp TEXT)');O.executemany('INSERT INTO w VALUES(?,?,?)',sorted(want))
            for e,k,cp,n,lo,hi in O.execute('SELECT o.expiry,o.strike,o.cp,COUNT(DISTINCT o.day),MIN(o.day),MAX(o.day) FROM opt_1m o JOIN w USING(expiry,strike,cp) GROUP BY 1,2,3'):
                hist[(e,float(k),cp)]=(n,lo,hi)
    rows=[]
    for g in gaps:
        d=g['day'];r=dict(g,month=d[:7],year=d[:4],dte_bucket=dte_bucket(g['dte']),moneyness=moneyness(g['cp'],(g['strike']-g['atm']) if g.get('strike') is not None and g.get('atm') is not None else g['offset']))
        lst=listing.get(d,{})
        if g['event']=='no_long_expiry':
            r['cls']='no_far_expiry_listed';r['providers']='+'.join(sorted({p for v in lst.values() for p in v}))
            r['max_listed_dte']=g['dte'];r['nearest_extracted_strike_gap']=None;r['other_days_extracted']=None
        else:
            c=(g['expiry'],float(g['strike']),g['cp']);pv=lst.get(g['expiry'],set())
            r['cls']='expiry_not_listed' if not pv else 'not_planned' if c not in planned.get(d,()) else 'planned_no_source'
            r['providers']='+'.join(sorted(pv));r['max_listed_dte']=None
            ks=have.get(d,{}).get((g['expiry'],g['cp']),())
            r['nearest_extracted_strike_gap']=min((abs(k-c[1]) for k in ks),default=None)
            h=hist.get(c);r['other_days_extracted']=h[0] if h else (None if a.no_history else 0)
        rows.append(r)
    keys=['event','cls','leg','roll','day','month','year','side','structure','cp','offset','moneyness','strike','atm','expiry','dte','dte_bucket','max_listed_dte','providers','nearest_extracted_strike_gap','other_days_extracted','signal_bar']
    with open(out/'coverage_gaps.csv','w',newline='') as f:w=csv.DictWriter(f,keys,extrasaction='ignore');w.writeheader();w.writerows(rows)
    groups=[('event','cls'),('event','cls','leg'),('event','cls','year'),('event','cls','month'),('event','cls','cp'),('event','cls','dte_bucket'),
            ('event','cls','moneyness'),('event','cls','providers'),('event','cls','roll'),('event','cls','structure','side')]
    summ=[]
    for gk in groups:
        cnt=collections.Counter(tuple(r[k] for k in gk) for r in rows)
        for key,n in sorted(cnt.items(),key=lambda x:(x[0][:2],-x[1])):summ.append(dict(grouping='+'.join(gk),key=' | '.join(map(str,key)),count=n))
    with open(out/'coverage_summary.csv','w',newline='') as f:w=csv.DictWriter(f,['grouping','key','count']);w.writeheader();w.writerows(summ)
    lrow=collections.defaultdict(lambda:[0,0])
    for d,ex in listing.items():
        for pv in {p for v in ex.values() for p in v}|{'any'}:
            far=any((dt.date.fromisoformat(e)-dt.date.fromisoformat(d)).days>30 for e,v in ex.items() if pv=='any' or pv in v)
            x=lrow[(d[:4],pv)];x[0]+=1;x[1]+=far
    with open(out/'coverage_listing.csv','w',newline='') as f:
        w=csv.writer(f);w.writerow(['year','provider','trading_days','days_with_listed_expiry_over_30d'])
        for (y,pv),(n,far) in sorted(lrow.items()):w.writerow([y,pv,n,far])
    print(f'{len(rows)} gap events -> {out}/coverage_gaps.csv, coverage_summary.csv, coverage_listing.csv')
    for gk in (('event','cls'),('event','cls','leg'),('event','cls','year'),('event','cls','dte_bucket'),('event','cls','moneyness'),('event','cls','providers')):
        print('\n'+' + '.join(gk));cnt=collections.Counter(tuple(r[k] for k in gk) for r in rows)
        for key,n in sorted(cnt.items(),key=lambda x:(x[0][:2],-x[1])):print(f'  {" | ".join(map(str,key)):<60} {n:>7}')
    print('\nlisted expiry > 30 days out, by year and provider (days with / trading days)')
    for (y,pv),(n,far) in sorted(lrow.items()):print(f'  {y} {pv:<16} {far:>4} / {n}')
    P.close();O.close()

if __name__=='__main__':main()
