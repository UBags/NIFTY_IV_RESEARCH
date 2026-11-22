#!/usr/bin/env python3
"""Opportunity coverage using the simulator's own selection and fill rules, stage by stage.

For an ATM500 put calendar on every session. NOT a backtest and NOT proof of bid/ask fills.
"""
import argparse,sqlite3,csv,json,datetime as dt
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from nifty_iv.ivcal.candidate_core import dte,is_quote,nearest_short,nearest_long
import statistics
from nifty_iv.ivcal.calendar_research import Data,CLOSE_WINDOW,ENTRY_WINDOWS,quote_fill,synchronized_fills,execute_fills,required_volume,resolve_volume_unit,validate_config
from nifty_iv.core.contract_specs import LotMaster

STAGES=('session_eligible','short_expiry_listed','short_signal_0945','long_signal_0945','any_independent_liquid_print','any_synchronous_liquid_print',
        'fill_model_eligible','lot_size_known','greeks_valid','signal_debit_positive','admitted')

SELECTION_FIELDS=('rv5_close','prior_day_abs_return','short_dte','long_dte','iv_short','iv_long')   # all known at 09:45; IVs exist only where Greeks did

def audit_day(data,cfg,day,step=500):
    """Yields (tenor, stages reached, missing-reason-free dict) for one session, ATM on 09:45 spot."""
    spot=data.spot(day,'09:45')
    if not is_quote(spot):return
    k=float(int(spot['close']/step+.5)*step);chain=data.surface(cfg.provider,day,'09:45')
    short=nearest_short(chain,day,cfg.max_short_dte)
    for target in cfg.long_tenors:
        reached=[];rec=dict(strike=k,short_expiry=short[1] if short else None,long_expiry=None,fill_minute=None,weekly_exit_eligible=None,short_dte=short[0] if short else None,long_dte=None,iv_short=None,iv_long=None)
        def done():return target,reached,rec
        if not data.entry_session_eligible(day,getattr(cfg,'allow_special_sessions',False)):yield done();continue
        reached.append('session_eligible')
        if not short:yield done();continue
        reached.append('short_expiry_listed');sd,sx=short
        if not is_quote(chain[sx].get((k,'PE')),volume=cfg.min_signal_volume):yield done();continue
        reached.append('short_signal_0945')
        kl=k+cfg.long_strike_offset;lng=nearest_long(chain,day,sd,kl,target,cfg)
        if not lng:yield done();continue
        reached.append('long_signal_0945');lx=lng[1];rec['long_expiry']=lx;rec['long_dte']=lng[0]
        legs=[(lx,kl,'PE'),(sx,k,'PE')]
        lot=cfg.lot_master.get(day,sx).qty;order=lot*getattr(cfg,'lots',1) if lot else None
        floor=required_volume(cfg,order,lot,cfg.min_fill_volume) if lot else cfg.min_fill_volume   # the simulator's own order-size gate
        # These two stages are diagnostics; only execute_fills below, the simulator's call, decides admission.
        if all(quote_fill(data,cfg.provider,day,e,s,c,minimum=floor) for e,s,c in legs):reached.append('any_independent_liquid_print')
        if synchronized_fills(data,cfg.provider,day,legs,minimum=floor):reached.append('any_synchronous_liquid_print')
        fills=execute_fills(data,cfg,day,legs,minimum=floor)
        if not fills:yield done();continue
        reached.append('fill_model_eligible');rec['fill_minute']=fills[0][1]
        a,b=cfg.lot_master.get(day,sx),cfg.lot_master.get(day,lx)
        if a.qty is None or a.qty!=b.qty:yield done();continue
        reached.append('lot_size_known')
        args=(cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        greeks=[data.greek(cfg.provider,day,'09:45',e,s,c,*args) for e,s,c in legs]
        if not all(greeks):yield done();continue
        rec['iv_long'],rec['iv_short']=greeks[0].get('iv'),greeks[1].get('iv')
        reached.append('greeks_valid')
        if order*(chain[lx][(kl,'PE')]['close']-chain[sx][(k,'PE')]['close'])<=getattr(cfg,'min_debit',1.):yield done();continue
        reached.append('signal_debit_positive');reached.append('admitted')
        exit_floor=required_volume(cfg,order,lot,getattr(cfg,'min_exit_volume',1))
        rec['weekly_exit_eligible']=bool(execute_fills(data,cfg,sx,legs,CLOSE_WINDOW,exit_floor))   # reads later data: a diagnostic, never part of admission
        yield done()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache',default='research_cache_v4.sqlite')
    p.add_argument('--provider',choices=('rissin_upstox','thetrademarkk'),default='rissin_upstox')
    p.add_argument('--long-tenors',default='60,90,120');p.add_argument('--tenor-tolerance',type=int,default=9)
    p.add_argument('--max-short-dte',type=int,default=8);p.add_argument('--contract-master',default=None)
    p.add_argument('--min-signal-volume',type=int,default=1);p.add_argument('--min-fill-volume',type=int,default=1)
    p.add_argument('--min-pair-volume',type=int,default=1);p.add_argument('--max-parity-bps',type=float,default=40.)
    p.add_argument('--rate',type=float,default=.06)
    p.add_argument('--fill-sync',choices=('synchronized','independent'),default='synchronized')
    p.add_argument('--fill-model',default='next_bar_open',choices=('next_bar_open','next_bar_open_adverse','qualified_minute_close','same_minute_first_print','next_minute','next_minute_adverse_range'))
    p.add_argument('--participation-stress',default='.01,.02,.05,.1',help='Extra passes: admitted counts at each cap and lot size; empty to skip')
    p.add_argument('--entry-delay-minutes',type=int,default=0);p.add_argument('--lots',type=int,default=1);p.add_argument('--lots-stress',default='1,2,5,10');p.add_argument('--min-exit-volume',type=int,default=1);p.add_argument('--min-debit',type=float,default=1.)
    p.add_argument('--allow-special-sessions',action='store_true')
    p.add_argument('--max-participation',type=float,default=.10);p.add_argument('--volume-unit',choices=('auto','units','contracts'),default='auto')
    p.add_argument('--long-strike-offset',type=float,default=0.,help='Long strike minus short strike; negative checks a put diagonal')
    p.add_argument('--out',default='contract_coverage_v4')
    a=p.parse_args()
    problems=validate_config(a)
    if problems:p.error('; '.join(problems))
    cfg=SimpleNamespace(**vars(a));cfg.long_tenors=[int(x) for x in a.long_tenors.split(',')];cfg.option_type='PE';cfg.lot_master=LotMaster(a.contract_master)
    data=Data(a.cache);monthly={};cube={};rows=[]
    resolve_volume_unit(data,cfg)
    ordered=sorted(data.daily);prior_move={b:abs(data.daily[b]['open']/data.daily[a]['close']-1) for a,b in zip(ordered,ordered[1:]) if data.daily[a].get('close') and data.daily[b].get('open')}   # overnight move into the day, known at 09:45
    for day in data.days:
        wd=dt.date.fromisoformat(day).strftime('%a')
        for target,reached,rec in audit_day(data,cfg,day):
            for bag,key in ((monthly,(day[:7],target)),(cube,(day[:7],target,wd))):
                c=bag.setdefault(key,Counter());c['days']+=1;c.update(reached)
            rows.append(dict(day=day,weekday=wd,tenor=target,last_stage=reached[-1] if reached else 'none',rv5_close=data.rv.get(day,{}).get('rv5_close'),prior_day_abs_return=prior_move.get(day),**rec))
    stress=[]
    for cap,lots in [(float(x),int(n)) for x in a.participation_stress.split(',') if x.strip() for n in a.lots_stress.split(',') if n.strip()]:
        counts=Counter();alt=SimpleNamespace(**{**vars(cfg),'max_participation':cap,'lots':lots})
        for day in data.days:
            for target,reached,_ in audit_day(data,alt,day):
                counts[(day[:7],target,'days')]+=1;counts[(day[:7],target,'admitted')]+='admitted' in reached
        stress+=[dict(max_participation=cap,lots=lots,month=m,tenor=t,opportunity_days=n,admitted=counts[(m,t,'admitted')]) for (m,t,k),n in sorted(counts.items()) if k=='days']
    selection=[]
    for tenor in sorted({r['tenor'] for r in rows}):
        for label,group in (('admitted',[r for r in rows if r['tenor']==tenor and r['last_stage']=='admitted']),('rejected',[r for r in rows if r['tenor']==tenor and r['last_stage']!='admitted'])):
            mean=lambda name:(lambda v:statistics.mean(v) if v else None)([r[name] for r in group if r.get(name) is not None])
            selection.append(dict(tenor=tenor,group=label,days=len(group),**{'mean_'+n:mean(n) for n in SELECTION_FIELDS}))
    data.close()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    with (out/'admitted_vs_rejected.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['tenor','group','days',*('mean_'+n for n in SELECTION_FIELDS)]);w.writeheader();w.writerows(selection)
    if stress:
        with (out/'participation_stress.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(stress[0]));w.writeheader();w.writerows(stress)
    if getattr(cfg,'volume_unit_report',None):(out/'volume_unit_report.json').write_text(json.dumps(cfg.volume_unit_report,indent=2))
    def write(name,head,bag):
        with (out/name).open('w',newline='') as f:
            w=csv.writer(f);w.writerow(head+['days',*STAGES])
            for key,c in sorted(bag.items()):w.writerow([*key,c['days'],*(c[s] for s in STAGES)])
    write('atm500_monthly.csv',['month','tenor'],monthly);write('coverage_cube.csv',['month','tenor','weekday'],cube)
    with (out/'opportunities.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['day','weekday','tenor','last_stage','strike','short_expiry','long_expiry','fill_minute','weekly_exit_eligible','short_dte','long_dte','iv_short','iv_long','rv5_close','prior_day_abs_return']);w.writeheader();w.writerows(rows)
    (out/'summary.json').write_text(json.dumps(dict(provider=a.provider,fill_model=a.fill_model,fill_sync=a.fill_sync,stages=STAGES,
        monthly=[dict(month=k[0],tenor=k[1],days=c['days'],**{s:c[s] for s in STAGES}) for k,c in sorted(monthly.items())],
        notes='Same expiry selection and fill rules as nifty-iv-calendar-research; no bid/ask data'),indent=2))
    print('Contract coverage:',out,'opportunity-days:',len(rows),'admitted:',sum(r['last_stage']=='admitted' for r in rows))

if __name__=='__main__':main()
