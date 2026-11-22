#!/usr/bin/env python3
"""Futures benchmark for the zone + swing signals, from spot only: does selling options add anything over simply
taking the index direction?

Same signals, entries and zone-change exits as zone_backtest.py; the position is long (green) or short (red) the
index, priced from spot minutes as a futures proxy (basis ignored). Two holding rules:
  futures_zone    exit when the zone changes colour (or the ATR stop is hit)
  futures_expiry  also exit at 15:15 on the expiry a naked short would have used (nearest with >= --min-short-days
                  calendar days from the fill day), so holding periods match the option trades
Fills: decided at a 45-minute bar close, filled at the next bar's first spot minute open (09:15 next session after
the last bar), less --slippage-points. Costs: brokerage and dated statutory charges for NIFTY futures
(calendar_research.hedge_cost). Lot size: the options lot for the same expiry.

Writes the zone_backtest trade format, so zone_report.py and zone_summary.py work on it unchanged."""
import argparse,collections,datetime as dt,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
from calendar_research import hedge_cost
from contract_specs_v2 import LotMaster
import zone_signals as zs
import zone_backtest as zb

def days_between(a,b):return (dt.date.fromisoformat(b)-dt.date.fromisoformat(a)).days

class Futures:
    def __init__(self,bars,final,minutes,listing,cfg,boundaries=()):
        self.bars=bars;self.final=final;self.cfg=cfg;self.listing=listing;self.lots=LotMaster(cfg.contract_master)
        self.px={d:{zs.hhmm(m):o for m,o,h,l,c in rows} for d,rows in minutes.items()}
        self.boundaries=sorted(set(boundaries));self.atr={};self.trades=[];self.counts=collections.Counter()
    def fill_price(self,t):
        """(day, minute, open) of the first spot minute in the bar after t."""
        b=self.bars[t]
        if b.end<zs.CLOSE:day,mins=b.day,range(b.end,min(b.end+45,zs.CLOSE))
        elif t+1<len(self.bars):day,mins=self.bars[t+1].day,range(zs.OPEN,zs.OPEN+45)
        else:return None
        for m in mins:
            p=self.px.get(day,{}).get(zs.hhmm(m))
            if p:return day,zs.hhmm(m),p
        return None
    def stop_level(self,t,side,stop):
        if not stop:return None
        if stop not in self.atr:self.atr[stop]=zs.atr(self.bars,stop)
        a=self.atr[stop][t]
        if a!=a:return None
        return self.bars[t].close-self.cfg.atr_mult*a if side=='bull' else self.bars[t].close+self.cfg.atr_mult*a
    def cost(self,units,price,day):
        return hedge_cost(units,price,day,self.cfg)+abs(units)*self.cfg.slippage_points
    def open(self,t,side,rule,stop):
        f=self.fill_price(t);sid=f"{side}|{rule}|0|None|{stop}|{t}|0"
        base=dict(id=sid,chain=sid,side=side,structure=rule,offset=0.,tp=None,stop=stop,signal_bar=t,roll=0,signal_day=self.bars[t].day)
        if f is None:self.counts['no_fill']+=1;self.trades.append(dict(base,chain=None,status='no_fill',reason='no_next_bar'));return None
        day,minute,price=f
        expiry=next((e for e in sorted(self.listing.get(self.bars[t].day,())) if days_between(day,e)>=self.cfg.min_short_days),None)
        if rule=='futures_expiry' and expiry is None:self.counts['no_expiry']+=1;self.trades.append(dict(base,chain=None,status='no_fill',reason='no_short_expiry'));return None
        lot=self.lots.get(day,expiry).qty if expiry else zb.LotMaster(None).get(day,(dt.date.fromisoformat(day)+dt.timedelta(days=7)).isoformat()).qty
        if not lot:self.counts['lot_unknown']+=1;self.trades.append(dict(base,chain=None,status='no_fill',reason='lot_unknown'));return None
        units=lot*self.cfg.lots*(1 if side=='bull' else -1)
        return dict(base,lot=lot,units=units,entry_day=day,entry_minute=minute,entry=price,cash=-self.cost(units,price,day),short_expiry=expiry,stop_level=self.stop_level(t,side,stop),marks_at={},status='open')
    def close(self,pos,t,reason):
        f=self.fill_price(t)
        if f is None:return False
        day,minute,price=f
        pos['cash']+=pos['units']*(price-pos['entry'])-self.cost(-pos['units'],price,day)
        pos.update(status='closed',reason=reason,exit_day=day,exit_minute=minute,exit_bar=t,exit=price);return True
    def step(self,pos,t):
        b=self.bars[t];colour='G' if pos['side']=='bull' else 'R';lvl=pos['stop_level'];reason=None
        if self.final[t]!=colour:reason='zone_flip'
        elif lvl is not None and ((pos['side']=='bull' and b.close<lvl) or (pos['side']=='bear' and b.close>lvl)):reason='atr_stop'
        elif pos['structure']=='futures_expiry' and (b.day>pos['short_expiry'] or (b.day==pos['short_expiry'] and b.end>=zb.ROLL_BAR_END)):reason='expiry'
        if reason and self.close(pos,t,reason):self.record(pos)
    def value(self,pos,t):
        return (pos['cash']+pos['units']*(self.bars[t].close-pos['entry']))/(pos['lot']*self.cfg.lots)
    def record(self,pos):
        pnl=pos['cash'] if pos['status']=='closed' else None
        self.trades.append(dict({k:pos.get(k) for k in ('id','chain','side','structure','offset','tp','stop','signal_bar','roll','signal_day','entry_day','entry_minute','exit_day','exit_minute','exit_bar','short_expiry','lot','status','reason','marks_at')},
            pnl_inr=pnl,unit_pnl=pnl/(pos['lot']*self.cfg.lots) if pnl is not None else None,strike=pos['entry'],atm=pos.get('exit'),forced=False))
    def run(self,entries,rules,stops):
        live=[];bi=0
        for t,b in enumerate(self.bars):
            nextday=self.bars[t+1].day if t+1<len(self.bars) else zb.END_KEY
            while bi<len(self.boundaries) and self.boundaries[bi]<=b.day:bi+=1
            while bi<len(self.boundaries) and b.day<self.boundaries[bi]<=nextday:   # value before exits decided now fill next session
                for p in live:
                    if p['entry_day']<self.boundaries[bi]:p['marks_at'][self.boundaries[bi]]=self.value(p,t)
                bi+=1
            for pos in live:self.step(pos,t)
            live=[p for p in live if p['status']=='open']
            for side in entries.get(t,()):
                for rule in rules:
                    for stop in stops:
                        p=self.open(t,side,rule,stop)
                        if p:live.append(p)
        for p in live:p['marks_at'][zb.END_KEY]=self.value(p,len(self.bars)-1);p['status']='open_at_data_end';self.record(p)
        return self.trades

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--spot',required=True);ap.add_argument('--options',help='zone_options.sqlite: expiry listing for futures_expiry and lot sizes');ap.add_argument('--out',default='zone_futures.sqlite')
    ap.add_argument('--from-date',default='2021-06-01');ap.add_argument('--to-date',default='2026-07-01');ap.add_argument('--split',default='2025-01-01')
    ap.add_argument('--rules',default='futures_zone,futures_expiry');ap.add_argument('--stops',default='none,15,30,45');ap.add_argument('--atr-mult',type=float,default=1.)
    ap.add_argument('--slippage-points',type=float,default=1.);ap.add_argument('--brokerage',type=float,default=20.);ap.add_argument('--lots',type=int,default=1)
    ap.add_argument('--min-short-days',type=int,default=4);ap.add_argument('--contract-master')
    a=ap.parse_args();rules=tuple(a.rules.split(','))
    listing=collections.defaultdict(set)
    if a.options:
        for d,e in sqlite3.connect(f'file:{a.options}?mode=ro',uri=True).execute('SELECT day,expiry FROM listing'):listing[d].add(e)
    elif 'futures_expiry' in rules:sys.exit('futures_expiry needs --options for the expiry listing')
    minutes=zs.load_minutes(a.spot)
    if not minutes:sys.exit(f'No spot minutes in {a.spot}')
    bars,final,_=zs.zones(minutes);sig=zs.signal_grid(bars,final)
    cfg=SimpleNamespace(brokerage=a.brokerage,gst=.18,hedge_bps=0.,hedge_statutory=True,slippage_points=a.slippage_points,lots=a.lots,atr_mult=a.atr_mult,min_short_days=a.min_short_days,contract_master=a.contract_master)
    fut=Futures(bars,final,minutes,listing,cfg,zb.boundaries(bars,a.split))
    trades=fut.run(zb.entry_map(bars,sig,a.from_date,a.to_date),rules,zb.parse_stops(a.stops))
    meta=dict(release=open(Path(__file__).with_name('VERSION.txt')).read().strip(),args=vars(a),counts=dict(fut.counts),kind='futures_benchmark',
              note='strike column holds the entry index level and atm the exit level')
    zb.write(a.out,bars,final,sig,trades,meta,keep_fills=False)
    print('Counts:',dict(fut.counts),'|',collections.Counter((t['structure'],t['status']) for t in trades));print(f'{len(trades)} rows -> {a.out}')

if __name__=='__main__':main()
