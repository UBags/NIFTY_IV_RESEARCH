#!/usr/bin/env python3
"""Why an entry did or did not fill: the option's minute volume in each window the entry could use.

For each signal (a ledger's rows, or --signal), replays the entry exactly as the paths engine does (same contract, size, windows,
participation, whole lots per minute, zone check between windows) and prints, per window: the minutes, the contract's raw traded
volume, how many minutes traded at all, how many minutes were big enough to give us one whole lot at --max-participation (volume
>= lot / participation), and the units we could take. Participation is applied minute by minute and rounded down to whole lots each
minute, so a window's tradable quantity can be far below participation x its total volume when the volume comes in small prints.

  nifty-diag-entry-liquidity --paths zone_paths_levels.sqlite --options zone_options_levels.sqlite --spot zone_spot.sqlite \\
      --ledger ledger_g_atr15x1_r_eatr30_trades.csv --target-fraction 1          # every entry the ledger could not fill
  ... --ledger <trades.csv> --all                                                 # every entry of the ledger
  ... --strategy bear,naked,-2 --signal "2023-09-25 13:45"                         # one signal"""
import argparse,csv,json,sqlite3,sys
from types import SimpleNamespace
import numpy as np
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.diagnostics import trade_ledger as tl
from nifty_iv.core import cli

def bar_index(bars,signal):
    day,t=signal.split()[:2];e=zs.minute_of(t)
    for i,b in enumerate(bars):
        if b.day==day and b.end==e:return i
    raise SystemExit(f'no bar ends at {signal} in the paths file')

def diagnose(sim,bars,k,side,structure,level):
    sim.cap_log.clear();sim.memo.clear()
    pair,why=sim.open_pair(k,side,structure,float(level))
    b=bars[k];rows=[];short=None
    for day,i0,c,r in sim.cap_log:
        if r is None:rows.append(dict(day=day,start=tl.hm(i0),minutes=0,note='no price that day'));continue
        cap,px,vol=r;short=c
        lot=sim.lots.get(day,c[0]).qty
        need=lot/sim.cfg.max_participation
        rows.append(dict(day=day,start=tl.hm(i0),end=tl.hm(i0+len(vol)),minutes=len(vol),raw_volume=int(vol.sum()),minutes_traded=int((vol>0).sum()),
                         minutes_with_a_lot=int((vol>=need).sum()),tradable_units=int(cap.sum()),participation_x_volume=int(vol.sum()*sim.cfg.max_participation),
                         largest_minute=int(vol.max()) if len(vol) else 0))
    lot=sim.lots.get(rows[0]['day'],short[0]).qty if short and rows else None
    want=lot*sim.lots_for(lot,rows[0]['day']) if lot else None
    return dict(signal=f'{b.day} {zs.hhmm(b.end)}',index=b.close,contract=f"{short[2]} {short[1]:.0f} exp {short[0]}" if short else None,
                lot=lot,want_units=want,want_lots=want//lot if want and lot else None,filled=pair is not None,reason=why,windows=rows)

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True);ap.add_argument('--options',required=True);ap.add_argument('--spot',required=True)
    ap.add_argument('--ledger',help='<out>_trades.csv: diagnose its not-filled rows (all rows with --all)');ap.add_argument('--all',action='store_true')
    ap.add_argument('--strategy',help='side,structure,level for --signal');ap.add_argument('--signal',action='append',default=[],help='"YYYY-MM-DD HH:MM" (signal bar close)')
    ap.add_argument('--target-fraction',type=float,help='size as in nifty-diag-trade-ledger (1 = the whole budget); default the paths file\'s')
    ap.add_argument('--max-participation',type=float);ap.add_argument('--entry-minutes',type=int);ap.add_argument('--entry-bars',type=int)
    ap.add_argument('--json',help='also write the details here')
    a=ap.parse_args(argv)
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);cfg=tl.config_from_meta(db);bars,final=tl.bars_from_paths(db);db.close()
    over={k:v for k,v in dict(target_fraction=a.target_fraction,max_participation=a.max_participation,entry_minutes=a.entry_minutes,entry_bars=a.entry_bars).items() if v is not None}
    cfg=SimpleNamespace(**dict(vars(cfg),**over))
    jobs=[]
    if a.ledger:
        for r in csv.DictReader(open(a.ledger)):
            if r['status'] in ('not_filled_at_size',) or (a.all and r['status'] in ('closed','open_at_data_end')):
                side,st,lev=r['strategy'].split('|')[:3];jobs.append((side,st,lev,r['signal']))
    if a.signal:
        if not a.strategy:sys.exit('--signal needs --strategy side,structure,level')
        side,st,lev=a.strategy.split(',');jobs+=[(side,st,lev,s) for s in a.signal]
    if not jobs:sys.exit('nothing to diagnose (no not-filled rows; use --all or --signal)')
    market=zb.Market(a.options,a.spot,keep=2);minutes=zs.load_minutes(a.spot);sim=tl.LedgerSim(market,bars,final,cfg,minutes);out=[]
    print(f"participation {cfg.max_participation:g} per minute, whole lots each minute; entry windows {getattr(cfg,'entry_bars',1)}, entry minutes {getattr(cfg,'entry_minutes',None) or 'no limit'}")
    try:
        for side,st,lev,sig in jobs:
            d=diagnose(sim,bars,bar_index(bars,sig),side,st,lev);out.append(dict(side=side,structure=st,level=lev,**d))
            print(f"\n{side} {st} {lev}  signal {d['signal']}  index {d['index']:.1f}  {d['contract']}  want {d['want_lots']} lots x {d['lot']} = {d['want_units']} units  "
                  f"-> {'FILLED' if d['filled'] else 'NOT FILLED ('+str(d['reason'])+')'}")
            print(f"  {'window':<24}{'min':>4}{'raw vol':>9}{'min>0':>6}{'min>=1 lot':>11}{'largest min':>12}{'20%xvol':>9}{'tradable':>9}".replace('20%',f"{cfg.max_participation:.0%}"))
            for w in d['windows']:
                if 'raw_volume' not in w:print(f"  {w['day']} {w['start']:<13}  {w.get('note','')}");continue
                print(f"  {w['day']} {w['start']}-{w['end']:<7}{w['minutes']:>4}{w['raw_volume']:>9}{w['minutes_traded']:>6}{w['minutes_with_a_lot']:>11}{w['largest_minute']:>12}{w['participation_x_volume']:>9}{w['tradable_units']:>9}")
    finally:market.close()
    if a.json:json.dump(out,open(a.json,'w'),indent=1,default=str)

if __name__=='__main__':main()
