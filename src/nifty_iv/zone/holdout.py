#!/usr/bin/env python3
"""Final holdout: replay a frozen portfolio selection ONCE on its frozen horizon (V4.9.3; V4.9.4: horizon, coverage, verdict).

Reads portfolio_freeze.json (written by nifty-zone-portfolio: the in-sample choice fitted to every training year, its sizes and all
assumptions) and replays it through the same executable account as the walk-forward (orders admitted at the signal close against
funds, margin and the open-lot cap), adopted flat on 1 January 2025. Nothing is re-selected, re-ranked or re-calibrated.

Refuses to run if the paths file, or the code that turns a selection into an account result, differs from what was frozen.
Every run is recorded in holdout_ledger.json next to the freeze file; a second run of the same freeze is refused unless
--acknowledge-repeat, and is then labelled a repeat (it is no longer a first look at unseen data).

Outputs (in --out): holdout_report.json, holdout_account_daily.csv, holdout_orders.csv."""
import argparse,datetime as dt,hashlib,json,sys
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.zone import portfolio as zp
from nifty_iv.zone import signals as zs
from nifty_iv.core import cli

EXPECTED_BAR_ENDS=tuple(range(zs.OPEN+45,zs.CLOSE,45))+(zs.CLOSE,)   # a regular session: 45-minute bars ending 10:00 .. 15:15, then 15:30

def coverage_problems(days,lo,hi,max_gap=7,holidays=None,bars_per_day=None,weak_tolerance=.08,bar_ends=None,short_sessions=()):
    """Why the sessions do not cover [lo, hi).
    With the exchange holiday list: every expected session (weekday not a holiday) must be present. Without it (weak check): the
    missing weekdays may not exceed weak_tolerance of all weekdays (NSE closes on about 5-6% of weekdays). Always: first / last
    session within max_gap days of the horizon, no gap over max_gap days. Bars: with bar_ends {day: set of bar end minutes} every
    regular session must hold every EXPECTED_BAR_ENDS bar (declared short_sessions excepted); with bars_per_day only, no session
    may have fewer than half the usual count (relative check, weaker)."""
    if not days:return ['no sessions']
    d=[dt.date.fromisoformat(x) for x in days];a=dt.date.fromisoformat(lo);b=dt.date.fromisoformat(hi);out=[]
    if (d[0]-a).days>max_gap:out.append(f'first session {days[0]} is more than {max_gap} days after {lo}')
    if (b-d[-1]).days>max_gap+1:out.append(f'last session {days[-1]} is more than {max_gap} days before {hi}')
    gaps=[(x,y) for x,y in zip(d,d[1:]) if (y-x).days>max_gap]
    if gaps:out.append(f'{len(gaps)} gap(s) over {max_gap} days, first {gaps[0][0]} .. {gaps[0][1]}')
    have=set(days);wk=[(a+dt.timedelta(i)).isoformat() for i in range((b-a).days) if (a+dt.timedelta(i)).weekday()<5]
    if holidays is not None:
        miss=[x for x in wk if x not in have and x not in set(holidays)]
        if miss:out.append(f'{len(miss)} expected session(s) missing (not holidays), first {miss[0]}')
    else:
        miss=[x for x in wk if x not in have]
        if len(miss)>weak_tolerance*len(wk):out.append(f'{len(miss)} of {len(wk)} weekdays missing (> {weak_tolerance:.0%}; no holiday list to check exactly)')
    if bar_ends is not None:
        exp=set(EXPECTED_BAR_ENDS);short=[x for x in days if x not in set(short_sessions) and not exp<=set(bar_ends.get(x,()))]
        if short:out.append(f'{len(short)} session(s) missing expected bars (of {len(exp)} per regular session), first {short[0]} ({len(exp-set(bar_ends.get(short[0],())))} missing)')
    elif bars_per_day:
        vals=sorted(bars_per_day.get(x,0) for x in days);usual=vals[len(vals)//2]
        thin=[x for x in days if bars_per_day.get(x,0)<.5*usual]
        if thin:out.append(f'{len(thin)} session(s) with fewer than half the usual {usual} bars, first {thin[0]}')
    return out

def run(freeze_path,paths_db,out,to=None,holidays=(),acknowledge_repeat=False,progress=True,weak_coverage=False,registry=None):
    freeze_path=Path(freeze_path);F=json.loads(freeze_path.read_text());fsha=hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    if zg.digest(paths_db)!=F['paths_sha256']:raise SystemExit(f'{paths_db} is not the paths file the selection was frozen on')
    if zp.code_digest()!=F['code_sha256']:raise SystemExit('the portfolio / holdout / grid / rule / path / contract-size / cost code changed since the freeze: the result would not be the frozen procedure')
    P=F['params'];lo=P.get('holdout_start',zg.HOLDOUT_START);hi=P.get('holdout_end','2026-07-01')
    if to and to!=hi:raise SystemExit(f'the holdout horizon is frozen at {lo} .. {hi} (exclusive); --to {to} differs')
    if sorted(holidays)!=sorted(P.get('holidays',[])):raise SystemExit('--holidays must be the list used for the freeze')
    ledger_path=freeze_path.with_name('holdout_ledger.json');ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    reg_path=Path(registry).expanduser() if registry else None;reg=json.loads(reg_path.read_text()) if reg_path and reg_path.exists() else []
    earlier=[r for r in ledger+reg if r['freeze_sha256']==fsha]                  # local ledger and the central registry (copying the freeze elsewhere does not reset it)
    if earlier and not acknowledge_repeat:raise SystemExit(f'this freeze was already evaluated on the holdout ({earlier[0]["run_at"]}); a repeat is not a first look (use --acknowledge-repeat to run and label it)')
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    D=zg.Detail(paths_db,holidays,P.get('gap_policy','free'));K=D.target_lots or 10
    days=sorted({d for d in D.bars.day if lo<=d<hi});ends={}
    for d,e in zip(D.bars.day,D.bars.end):
        if lo<=d<hi:ends.setdefault(d,set()).add(int(e))
    hol=P.get('holidays') or None
    if hol is None and not weak_coverage:D.close();raise SystemExit('the holdout needs the exchange holiday list (grid / freeze built with --holidays) to check that every session is present; --weak-coverage-check runs a looser check, labelled')
    cov=coverage_problems(days,lo,hi,holidays=hol,bar_ends=ends,short_sessions=P.get('short_sessions',()))
    if cov:D.close();raise SystemExit('INSUFFICIENT COVERAGE of the frozen holdout: '+'; '.join(cov))
    try:gap_events=dict(D.db.execute('SELECT event,COUNT(*) FROM gaps WHERE day>=? AND day<? GROUP BY 1',(lo,hi)).fetchall())
    except Exception:gap_events={}
    ordv=np.array([dt.date.fromisoformat(d).toordinal() for d in days]);last={};[last.__setitem__(d,c) for d,c in zip(D.bars.day,D.bars.close)];close=np.array([last[d] for d in days])
    zp.HOLIDAYS=frozenset(hol or ())
    sz=zp.Sizing(P['sizing'],P['ref_units'],K,D.target_units,P.get('cap_unit','reference'),D.target_margin,D.target_fraction)
    margin=zp.MarginModel(P.get('margin_model','rates'),P.get('margin'),P.get('calendar_margin_factor',1.))
    sel_of=lambda k:dict(side=k[0],structure=k[1],offset=k[2],config=k[3],profit=k[4],stop=k[5])
    acc=zp.Account(D,days,ordv,close,hi,P['capital'],P['buffer'],P['min_days'],margin,sz,sel_of,P.get('max_lots'),P.get('max_margin'))
    legs=[('holdout','green' if s['side']=='bull' else 'red',(s['side'],s['structure'],float(s['offset']),s['config'],s['profit'],s['stop']),s['lots'],lo,hi) for s in F['selection']]
    A,M,opn,orders,Lo=acc.run(legs,P['carry']);xl=np.array(acc.last_exchange_lots);gc=np.array(acc.last_gross_contracts)
    years=sorted({int(d[:4]) for d in days});s,daily,G,R,short,breach=zp.account_summary(acc,A,M,Lo,opn,orders,years,0,start_date=lo,xl=xl,gc=gc)
    rows=[]
    for y in years:
        r,dd,ab=acc.year(A,y);a,b=ab;e0=A[a-1] if a>0 else 0.;sy=np.r_[0.,A[a:b+1]-e0]
        rows.append(dict(year=y,return_=None if r is None else round(float(r),6),days=dd,annualised=bool(dd>=P['min_days']),full_calendar_year=zp.is_full_year(y,dd),daily_close_drawdown_in_year=round(float((np.maximum.accumulate(sy)-sy).max())),
                         funds_short_sessions=int(short[a:b+1].sum()),buffer_breach_sessions=int(breach[a:b+1].sum())))
    full=[r for r in rows if r['annualised'] and r['return_'] is not None];rets=[r['return_'] for r in full]
    summ=dict(s,years=[r['year'] for r in full],full_years=[r['year'] for r in rows if r['full_calendar_year']],returns=rets,mean_of_yearly_returns=round(float(np.mean(rets)),6) if rets else None,worst=round(float(min(rets)),6) if rets else None,
              every_year_deployable=P['sizing']=='exchange' and not s['funds_short_sessions'])
    al=P.get('allocation');f=al*len([x for x in F['selection'] if x.get('lots')]) if al else 1.          # V4.9.7 allocated basis: frozen share per leg
    v=zp.verdict(summ,rows,P['target']*f,P['floor']*f,P['max_dd']*f,P.get('return_basis','simple'),P.get('holdout_min_full_years',1));v['verdict_share']=round(f,6)
    rep=dict(holdout=dict(start=lo,end_exclusive=hi,sessions=len(days),first_session=days[0],last_session=days[-1],coverage_check='exact (holiday list)' if hol is not None else 'weak (no holiday list)',
                          option_data_gap_events=gap_events),freeze=str(freeze_path),freeze_sha256=fsha,selection=F['selection'],params=P,years=rows,summary=summ,**v,
             repeat=bool(earlier),label='REPEAT RUN: not a first look at unseen data' if earlier else 'first holdout run of this freeze')
    zg.write_json(rep,out/'holdout_report.json',indent=1,default=str);zp._csv(out/'holdout_account_daily.csv',daily);zp._csv(out/'holdout_orders.csv',orders)
    ledger.append(dict(freeze_sha256=fsha,run_at=dt.datetime.now().isoformat(timespec='seconds'),out=str(out),verdict=v['oos_verdict'],repeat=bool(earlier)))
    zg.write_json(ledger,ledger_path,indent=1)
    if reg_path:reg_path.parent.mkdir(parents=True,exist_ok=True);reg.append(dict(ledger[-1],freeze=str(freeze_path.resolve())));zg.write_json(reg,reg_path,indent=1)
    D.close()
    return rep

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--freeze',required=True,help='portfolio_freeze.json from nifty-zone-portfolio');ap.add_argument('--paths',required=True,help='the zone_paths.sqlite of the freeze (covering the holdout)')
    ap.add_argument('--out',default='holdout');ap.add_argument('--to',help='optional check: must equal the frozen holdout end (exclusive)');ap.add_argument('--holidays')
    ap.add_argument('--acknowledge-repeat',action='store_true',help='run a freeze that was already evaluated (labelled a repeat)')
    ap.add_argument('--weak-coverage-check',action='store_true',help='without a holiday list: accept up to 8%% missing weekdays (labelled weak)')
    ap.add_argument('--registry',default='~/.nifty_holdout_registry.json',help='central append-only record of holdout runs, checked as well as the ledger next to the freeze (copying a freeze elsewhere does not reset it); keep it outside the project')
    a=ap.parse_args(argv);hol=zr.read_holidays(a.holidays) if a.holidays else []
    rep=run(a.freeze,a.paths,a.out,a.to,hol,a.acknowledge_repeat,weak_coverage=a.weak_coverage_check,registry=a.registry)
    print(f"Holdout {rep['holdout']['start']} .. {rep['holdout']['end_exclusive']} exclusive, sessions {rep['holdout']['first_session']} .. {rep['holdout']['last_session']} ({rep['label']}):")
    for r in rep['years']:print(f"  {r['year']}: {'n/a' if r['return_'] is None else format(r['return_'],'.1%')}{'' if r['annualised'] else ' (part year, not annualised)'}  drawdown in year Rs {r['daily_close_drawdown_in_year']:,}")
    s=rep['summary'];print(f"  overall {s['overall_simple']:.1%} a year over {s['elapsed_years']:.2f} years (CAGR {s['cagr_fixed_size_no_withdrawals']})");print(f"  continuous daily-close drawdown Rs {s['daily_close_drawdown_continuous']:,}; assumed margin peak Rs {s['assumed_margin_peak']:,}; orders cut {s['orders_cut']}, rejected {s['orders_rejected']}")
    print(f"  VERDICT: {rep['oos_verdict']}"+(f" ({'; '.join(rep['oos_verdict_reasons'])})" if rep['oos_verdict_reasons'] else ''))
    print(f'-> {a.out}/holdout_report.json, holdout_account_daily.csv, holdout_orders.csv')

if __name__=='__main__':main()
