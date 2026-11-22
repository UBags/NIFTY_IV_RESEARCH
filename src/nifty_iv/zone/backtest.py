#!/usr/bin/env python3
"""Option outcomes for the INDEX v15 zone + swing signals. Writes every trade; does not rank.

For each signal bar and side, every structure x strike offset x take-profit is simulated once and shared by
all signal configurations that fire on that bar (nifty-zone-legacy-report joins them).

  bull: rising valley in a green zone -> sell puts;  bear: falling peak in a red zone -> sell calls.
  naked:    sell the nearest expiry with at least --min-short-days calendar days left ON THE FILL DAY.
  calendar: same short, buy the same strike in the expiry nearest --long-target days (later than the short,
            more than 30 days left). On the short's expiry day the pair is closed at the 15:15 bar and, if the
            zone still holds, a new pair is opened on the current index level. The long is closed at 30 days left.
  Exit when the zone leaves its colour. Optional take-profit: naked at a share of the credit, calendar at a share
  of the debit, checked on bar-close option prices. A short reaching expiry is cash-settled on the 15:00-15:29
  spot TWAP proxy (else the last spot close of that day).

Fills (decided at a 45-minute bar close): the first minute of the next bar in which every leg has a vendor bar
with volume >= lots / --max-participation, at that minute's open. The whole next bar is searched (09:15-09:59 of
the next session after the last bar). A minute's volume is used to accept a fill at its own open: a deliberate
choice, recorded in meta. Exits go minute by minute: a short is bought back at its first
liquid minute and the long is sold at its first liquid minute from then on. Unfilled exits retry in the next bar. Each contract-minute uses the vendor with
the larger volume. OHLC minute bars are not bid/ask quotes. Charges and slippage use calendar_research.

Positions open at each year start and at --split are valued (cash plus bar-close marks) so the report can close
them hypothetically at a training boundary.
"""
from __future__ import annotations
import argparse,collections,datetime as dt,json,math,sqlite3,sys
from pathlib import Path
import nifty_iv
from types import SimpleNamespace
from nifty_iv.ivcal.calendar_research import fill_detail
from nifty_iv.core.contract_specs import LotMaster
from nifty_iv.zone import signals as zs
from nifty_iv.core import strategy_spec as ss

PROVIDERS=('rissin_upstox','thetrademarkk')
STRUCTURES=('naked','calendar');OFFSETS=(-200,-100,0,100,200);TPS=(None,.25,.4,.5,.6,.75)
ROLL_BAR_END=15*60+15
END_KEY='9999-12-31'
PLAN_KEYS=('min_short_days','long_target','lots')

def days_between(a,b):return (dt.date.fromisoformat(b)-dt.date.fromisoformat(a)).days

class Market:
    """Minute option bars for the targeted contracts (nifty-zone-contracts), expiry listings, spot settlement."""
    def __init__(self,options,spot,keep=6):
        self.db=sqlite3.connect(f'file:{options}?mode=ro',uri=True);self.keep=keep;self.cache=collections.OrderedDict()
        self.listing=collections.defaultdict(set)
        for d,e in self.db.execute('SELECT day,expiry FROM listing'):self.listing[d].add(e)
        self.meta={k:json.loads(v) for k,v in self.db.execute('SELECT name,value FROM meta')}
        sp=sqlite3.connect(f'file:{spot}?mode=ro',uri=True);self.twap={};self.last={}
        for d,n,avg in sp.execute("SELECT day,COUNT(*),AVG(close) FROM spot_1m WHERE minute BETWEEN '15:00' AND '15:29' GROUP BY day"):self.twap[d]=(avg,n)
        for d,c in sp.execute('SELECT s.day,s.close FROM spot_1m s JOIN (SELECT day,MAX(minute) m FROM spot_1m GROUP BY day) x ON s.day=x.day AND s.minute=x.m'):self.last[d]=c
        sp.close()
    def close(self):
        db=getattr(self,'db',None)
        if db is not None:db.close();self.db=None
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def __del__(self):self.close()        # a Market dropped without close() still releases its connection
    def day(self,day):
        if day in self.cache:self.cache.move_to_end(day);return self.cache[day]
        out=collections.defaultdict(dict)
        for p,m,e,k,cp,o,h,l,c,v in self.db.execute('SELECT provider,minute,expiry,strike,cp,open,high,low,close,volume FROM opt_1m WHERE day=? ORDER BY provider,minute',(day,)):
            key=(e,float(k),cp);old=out[m].get(key);v=v or 0
            if old is None or v>old[4]:out[m][key]=(o,h,l,c,v,p)
        self.cache[day]=out
        while len(self.cache)>self.keep:self.cache.popitem(last=False)
        return out
    def quote(self,day,minute,expiry,strike,cp):return self.day(day).get(minute,{}).get((expiry,float(strike),cp))
    def expiries(self,day,minute):return sorted(self.listing.get(day,()))
    def settlement(self,expiry):
        tw=self.twap.get(expiry)
        if tw and tw[1]>=25:return tw[0],'twap_1500_1529_proxy'
        return (self.last[expiry],'last_spot_close_proxy') if expiry in self.last else None

class PlanMarket:
    """Every contract looks liquid at price 1: used by nifty-zone-contracts to find which contracts and days a run can touch."""
    def __init__(self,listing):self.listing=listing
    def quote(self,day,minute,expiry,strike,cp):return (1.,1.,1.,1.,10**12,'plan')
    def expiries(self,day,minute):return sorted(self.listing.get(day,()))
    def settlement(self,expiry):return (0.,'plan')

class Sim:
    def __init__(self,market,bars,final,cfg,boundaries=()):
        self.m=market;self.bars=bars;self.final=final;self.cfg=cfg;self.lots=LotMaster(cfg.contract_master);self.trades=[];self.counts=collections.Counter()
        self.boundaries=sorted(set(boundaries));self.atr={}
    def window(self,t):
        """Minutes of the bar after t, where a decision taken at bar t's close can fill."""
        b=self.bars[t]
        if b.end<zs.CLOSE:return b.day,[zs.hhmm(m) for m in range(b.end,min(b.end+45,zs.CLOSE))]
        return (self.bars[t+1].day,[zs.hhmm(m) for m in range(zs.OPEN,zs.OPEN+45)]) if t+1<len(self.bars) else None
    def liquid(self,q,lot):return q is not None and q[0] is not None and q[0]>0 and q[4]>=lot*self.cfg.lots/self.cfg.max_participation
    def fill(self,pos,leg,q,day,minute,label):
        units=-leg['units'] if label!='open' else leg['units']
        cash,executed,slip,charges=fill_detail(q[0],units,day,self.cfg,leg['expiry'],q[4]/pos['lot'],None,None,pos['lot'])
        pos['cash']+=cash;pos['fills'].append(dict(event=label,day=day,minute=minute,expiry=leg['expiry'],strike=leg['strike'],cp=leg['cp'],units=units,ref=q[0],price=executed,slip=slip,charges=charges['total'],cash=cash,volume=q[4],vendor=q[5]))
    def stop_level(self,t,side,stop):
        """Index level fixed at the signal bar: close -/+ --atr-mult x ATR(stop) on 45-minute bars. None if no stop or ATR not ready."""
        if not stop:return None
        if stop not in self.atr:self.atr[stop]=zs.atr(self.bars,stop)
        a=self.atr[stop][t]
        if a!=a:return None
        return self.bars[t].close-self.cfg.atr_mult*a if side=='bull' else self.bars[t].close+self.cfg.atr_mult*a
    def open(self,t,side,structure,offset,tp,chain=None,roll=0,stop=None):
        b=self.bars[t];cp='PE' if side=='bull' else 'CE'
        levels=getattr(self.cfg,'offset_mode','points')=='levels'
        strike=ss.strike_for(side,b.close,offset) if levels else float(round(b.close/100)*100+offset)   # levels: side-aware, step by index level (V4.9.6)
        spec=dict(side=side,structure=structure,offset=offset,tp=tp,stop=stop,signal_bar=t if chain is None else chain['signal_bar'],roll=roll)
        w=self.window(t)
        if w is None:return self.reject(spec,b,'no_next_bar')
        day,mins=w;exps=self.m.expiries(b.day,b.mark_minute)
        short,why=ss.select_short_expiry(structure,day,exps,self.cfg.min_short_days)   # counted from the day the order can fill; naked_m1 / naked_m2: monthly policies
        if short is None:return self.reject(spec,b,why)
        lot=self.lots.get(day,short).qty
        if not lot:return self.reject(spec,b,'lot_unknown')
        legs=[dict(expiry=short,strike=strike,cp=cp,units=-lot*self.cfg.lots)];long=None
        if structure=='calendar':
            far=[e for e in exps if e>short and days_between(day,e)>30]
            if not far:return self.reject(spec,b,'no_long_expiry')
            long=min(far,key=lambda e:(abs(days_between(day,e)-self.cfg.long_target),e))
            if self.lots.get(day,long).qty!=lot:return self.reject(spec,b,'lot_mismatch')
            legs.append(dict(expiry=long,strike=strike,cp=cp,units=lot*self.cfg.lots))
        for minute in mins:
            qs=[self.m.quote(day,minute,l['expiry'],l['strike'],l['cp']) for l in legs]
            if all(self.liquid(q,lot) for q in qs):break
        else:return self.reject(spec,b,'no_liquid_entry')
        pos=dict(spec,id=None,lot=lot,legs=legs,open={i:l for i,l in enumerate(legs)},cash=0.,fills=[],entry_day=day,entry_minute=minute,signal_day=b.day,
                 short_expiry=short,long_expiry=long,long_days=days_between(day,long) if long else None,strike=strike,atm=ss.atm(b.close) if levels else strike-offset,pending=None,status='open',marks_at={},last_close={})
        for l,q in zip(legs,qs):self.fill(pos,l,q,day,minute,'open')
        pos['base']=pos['cash'] if ss.is_naked(structure) else -pos['cash']
        pos['id']=f"{side}|{structure}|{offset}|{tp}|{stop}|{spec['signal_bar']}|{roll}";pos['chain']=chain['id'] if chain else pos['id']
        pos['stop_level']=chain['stop_level'] if chain else self.stop_level(t,side,stop)
        self.counts['opened']+=1;return pos
    def reject(self,spec,b,why):
        self.counts['reject_'+why]+=1
        if spec['roll']:self.counts['roll_reopen_failed']+=1
        else:self.trades.append(dict(spec,id=f"{spec['side']}|{spec['structure']}|{spec['offset']}|{spec['tp']}|{spec['stop']}|{spec['signal_bar']}|0",chain=None,status='no_fill',reason=why,signal_day=b.day))
        return None
    def settle(self,pos,i,leg):
        ref=self.m.settlement(leg['expiry'])
        if ref is None:pos['status']='unresolved';self.counts['settlement_missing']+=1;return False
        s,src=ref;intrinsic=max(0.,s-leg['strike']) if leg['cp']=='CE' else max(0.,leg['strike']-s)
        cash=leg['units']*intrinsic;pos['cash']+=cash
        pos['fills'].append(dict(event='settle',day=leg['expiry'],minute='15:30',expiry=leg['expiry'],strike=leg['strike'],cp=leg['cp'],units=-leg['units'],ref=s,price=intrinsic,source=src,cash=cash))
        del pos['open'][i];return True
    def close(self,pos,t,reason):
        """Try to flatten in the bar after t. A long is not sold before its short is bought back."""
        b=self.bars[t];pos['pending']=pos['pending'] or reason
        for i,leg in list(pos['open'].items()):
            if leg['expiry']<b.day or (leg['expiry']==b.day and b.end==zs.CLOSE):self.settle(pos,i,leg)
        if pos['status']=='unresolved':return True
        w=self.window(t)
        if pos['open'] and w:
            day,mins=w;lot=pos['lot']
            for minute in mins:   # minute by minute: shorts at their first liquid minute, longs only once no short is left
                live={i:l for i,l in pos['open'].items() if l['expiry']>=day}
                if not live:break
                shorts_open=any(l['units']<0 for l in pos['open'].values())
                for i,l in sorted(live.items(),key=lambda x:x[1]['units']):
                    if l['units']>0 and shorts_open:continue
                    q=self.m.quote(day,minute,l['expiry'],l['strike'],l['cp'])
                    if self.liquid(q,lot):self.fill(pos,l,q,day,minute,'close');del pos['open'][i]
                if any(l['units']>0 for l in pos['open'].values()) and not any(l['units']<0 for l in pos['open'].values()):
                    for i,l in list(pos['open'].items()):   # the short just went: the long may go in the same minute
                        q=self.m.quote(day,minute,l['expiry'],l['strike'],l['cp'])
                        if l['expiry']>=day and self.liquid(q,lot):self.fill(pos,l,q,day,minute,'close');del pos['open'][i]
        if pos['open']:
            pos['pending_bars']=pos.get('pending_bars',0)+1
            if 0<=self.cfg.max_exit_lag_bars<pos['pending_bars'] and not any(l['units']<0 for l in pos['open'].values()):
                for i,l in list(pos['open'].items()):
                    last=pos['last_close'].get(i)
                    if last is None:continue
                    px=last*(1-self.cfg.forced_exit_haircut);cash=l['units']*px   # selling a long brings cash in (V4.6.4 and earlier had the sign reversed)
                    pos['cash']+=cash;pos['fills'].append(dict(event='forced_exit_lag_cap',day=b.day,minute=b.mark_minute,expiry=l['expiry'],strike=l['strike'],cp=l['cp'],units=-l['units'],ref=last,price=px,cash=cash,executable_claim=False))
                    del pos['open'][i];self.counts['forced_exit_lag_cap']+=1
            if pos['open']:self.counts['exit_pending_bar']+=1;return False
        pos['status']='closed';pos['reason']=pos['pending'];pos['exit_day'],pos['exit_minute']=(pos['fills'][-1]['day'],pos['fills'][-1]['minute']);pos['exit_bar']=t
        return True
    def marks(self,pos,b):
        """Bar-close value of the open legs, or None unless every leg printed with volume at the bar's last minute."""
        value=0.;fresh=True
        for i,l in pos['open'].items():
            q=self.m.quote(b.day,b.mark_minute,l['expiry'],l['strike'],l['cp'])
            if q and q[3]:pos['last_close'][i]=q[3]
            if not q or not q[3] or not q[4]:fresh=False;continue
            value+=l['units']*q[3]
        return value if fresh else None
    def value_now(self,pos):
        """Cash plus the latest known close of each open leg (entry price if none since): a hypothetical close."""
        v=pos['cash']
        for i,l in pos['open'].items():
            px=pos['last_close'].get(i)
            if px is None:px=next(f['ref'] for f in pos['fills'] if f['event']=='open' and f['expiry']==l['expiry'] and f['cp']==l['cp'] and f['strike']==l['strike'])
            v+=l['units']*px
        return v/(pos['lot']*self.cfg.lots)
    def step(self,pos,t):
        """Bar-close decision for one position. Returns a new rolled position or None."""
        b=self.bars[t];colour='G' if pos['side']=='bull' else 'R';v=self.marks(pos,b)
        if pos['pending']:done=self.close(pos,t,pos['pending']);return self.after(pos,t) if done else None
        reason=None
        lvl=pos.get('stop_level')
        if self.final[t]!=colour:reason='zone_flip'
        elif lvl is not None and ((pos['side']=='bull' and b.close<lvl) or (pos['side']=='bear' and b.close>lvl)):reason='atr_stop'
        elif pos['structure']=='calendar' and b.day>=pos['short_expiry'] and (b.end>=ROLL_BAR_END or b.day>pos['short_expiry']):reason='short_expiry_roll'
        elif pos['structure']=='calendar' and days_between(b.day,pos['long_expiry'])<=30:reason='long_30dte'
        elif pos['tp'] and pos['base']>0 and v is not None and pos['cash']+v>=pos['tp']*pos['base']:reason='take_profit'
        if reason is None and ss.is_naked(pos['structure']) and (b.day>pos['short_expiry'] or (b.day==pos['short_expiry'] and b.end==zs.CLOSE)):reason='expiry'
        if reason is None:return None
        return self.after(pos,t) if self.close(pos,t,reason) else None
    def after(self,pos,t):
        self.record(pos)
        colour='G' if pos['side']=='bull' else 'R'
        if pos['status']=='closed' and pos['reason'] in ('short_expiry_roll','long_30dte') and self.final[t]==colour:
            return self.open(t,pos['side'],pos['structure'],pos['offset'],pos['tp'],chain=dict(id=pos['chain'],signal_bar=pos['signal_bar'],stop_level=pos.get('stop_level')),roll=pos['roll']+1,stop=pos.get('stop'))
        return None
    def record(self,pos):
        pnl=pos['cash'] if pos['status']=='closed' else None
        self.trades.append(dict({k:pos[k] for k in ('id','chain','side','structure','offset','tp','stop','signal_bar','roll','signal_day','entry_day','entry_minute','short_expiry','long_expiry','long_days','strike','atm','lot','base','status','marks_at')},
            reason=pos.get('reason') or pos['pending'],exit_day=pos.get('exit_day'),exit_minute=pos.get('exit_minute'),exit_bar=pos.get('exit_bar'),pnl_inr=pnl,
            unit_pnl=pnl/(pos['lot']*self.cfg.lots) if pnl is not None else None,fills=pos['fills'],forced=any(f['event']=='forced_exit_lag_cap' for f in pos['fills'])))
    def run(self,entries,structures=STRUCTURES,offsets=OFFSETS,tps=TPS,progress=True,stops=(None,)):
        live=[];last_month=None;bi=0
        for t,b in enumerate(self.bars):
            # Boundary valuation first: positions open at this bar's close, before any exit decided now fills next session
            nextday=self.bars[t+1].day if t+1<len(self.bars) else END_KEY
            while bi<len(self.boundaries) and self.boundaries[bi]<=b.day:bi+=1
            while bi<len(self.boundaries) and b.day<self.boundaries[bi]<=nextday:
                for pos in live:
                    if pos['entry_day']<self.boundaries[bi]:self.marks(pos,b);pos['marks_at'][self.boundaries[bi]]=self.value_now(pos)
                bi+=1
            nxt=[]
            for pos in live:
                new=self.step(pos,t)
                if pos['status']=='open':nxt.append(pos)
                if new:nxt.append(new)
            for side in entries.get(t,()):
                for s in structures:
                    for o in offsets:
                        for tp in tps:
                            for stop in stops:
                                p=self.open(t,side,s,o,tp,stop=stop)
                                if p:nxt.append(p)
            live=nxt
            if progress and b.day[:7]!=last_month:
                last_month=b.day[:7];print(f'{last_month} open positions {len(live)} trades {len(self.trades)}',flush=True)
        for pos in live:pos['marks_at'][END_KEY]=self.value_now(pos);pos['status']='open_at_data_end';self.record(pos)
        return self.trades

DDL='''CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT);
CREATE TABLE bars(idx INTEGER PRIMARY KEY,day TEXT,end_minute TEXT,close REAL,zone TEXT);
CREATE TABLE signals(config TEXT,side TEXT,bar INTEGER);
CREATE TABLE trades(id TEXT PRIMARY KEY,chain TEXT,side TEXT,structure TEXT,offset REAL,tp REAL,stop INTEGER,signal_bar INTEGER,roll INTEGER,signal_day TEXT,entry_day TEXT,entry_minute TEXT,
 exit_day TEXT,exit_minute TEXT,exit_bar INTEGER,short_expiry TEXT,long_expiry TEXT,long_days INTEGER,strike REAL,atm REAL,lot INTEGER,base REAL,status TEXT,reason TEXT,pnl_inr REAL,unit_pnl REAL,
 forced INTEGER,marks_at TEXT,fills TEXT);
CREATE INDEX trades_chain ON trades(chain);CREATE INDEX signals_cfg ON signals(config,side);'''
COLS=['id','chain','side','structure','offset','tp','stop','signal_bar','roll','signal_day','entry_day','entry_minute','exit_day','exit_minute','exit_bar','short_expiry','long_expiry','long_days','strike','atm','lot','base','status','reason','pnl_inr','unit_pnl','forced','marks_at','fills']

def config(a):
    return SimpleNamespace(slippage=a.slippage,slippage_mode='tenor_volume',fill_model='next_bar_open',brokerage=a.brokerage,gst=.18,exchange_bps=None,sebi_bps=.01,stt=None,stamp=.00003,
        brokerage_order_mode='per_lot_orders',lots=a.lots,max_exit_lag_bars=a.max_exit_lag_bars,forced_exit_haircut=a.forced_exit_haircut,
        max_participation=a.max_participation,atr_mult=getattr(a,'atr_mult',1.),min_short_days=a.min_short_days,long_target=a.long_target,contract_master=a.contract_master,
        offset_mode=getattr(a,'offset_mode','points'))

def boundaries(bars,split):
    years=sorted({b.day[:4] for b in bars})
    return sorted({f'{y}-01-01' for y in years[1:]}|set(x for x in split.split(',') if x))

def entry_map(bars,sig,start,end):
    entries=collections.defaultdict(set)
    for s in sig.values():
        for side in ('bull','bear'):
            for t in s[side]:
                if start<=bars[t].day<=end:entries[t].add(side)
    return {t:sorted(v) for t,v in entries.items()}

def write(path,bars,final,sig,trades,meta,keep_fills=True):
    if Path(path).exists():Path(path).unlink()
    db=sqlite3.connect(path);db.executescript(DDL)
    db.executemany('INSERT INTO bars VALUES(?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.close,final[i]) for i,b in enumerate(bars)])
    db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,side,t) for c,s in sig.items() for side in ('bull','bear') for t in s[side]])
    db.executemany(f'INSERT INTO trades VALUES({",".join("?"*len(COLS))})',[tuple((json.dumps(t.get(c)) if keep_fills or c!='fills' else None) if c in ('fills','marks_at') else t.get(c) for c in COLS) for t in trades])
    db.executemany('INSERT INTO meta VALUES(?,?)',[(k,json.dumps(v)) for k,v in meta.items()]);db.commit();db.close()

def parse_offsets(text,mode='points'):
    """Offsets from the CLI text; None = the default of the mode (points -200..200, levels -5..5). Ranges like -5..5 allowed."""
    if text is None:return ss.LEVELS if mode=='levels' else OFFSETS
    if '..' in str(text):a,b=str(text).split('..');return tuple(range(int(a),int(b)+1))
    v=tuple(int(float(x)) for x in str(text).split(',') if x.strip())
    if mode=='levels' and any(abs(x)>5 for x in v):raise SystemExit('levels are -5..5 (> 0 OTM, < 0 ITM)')
    return v
def parse_stops(text):return tuple(None if x.strip().lower() in ('none','') else int(x) for x in text.split(','))
def parse_tps(text):return tuple(None if x.strip().lower() in ('none','') else float(x) for x in text.split(','))

def add_common(ap):
    ap.add_argument('--spot',required=True,help='zone_spot.sqlite');ap.add_argument('--from-date',default='2021-06-01');ap.add_argument('--to-date',default='2026-07-01')
    ap.add_argument('--lots',type=int,default=1);ap.add_argument('--min-short-days',type=int,default=4);ap.add_argument('--long-target',type=int,default=60)
    ap.add_argument('--contract-master')
    ap.add_argument('--offsets',default=None,help='points mode: strike offsets from ATM (rounded to 100), + is a higher strike (default -200..200); levels mode: moneyness levels, > 0 OTM, < 0 ITM (default -5..5)')
    ap.add_argument('--offset-mode',choices=('points','levels'),default='points',help='levels (V4.9.6): side-aware strikes, green PE at ATM - level x step, red CE at ATM + level x step; step 50 / 100 / 200 below 5,000 / 12,000 / above')

def main():
    ap=argparse.ArgumentParser(description=__doc__);add_common(ap)
    ap.add_argument('--options',required=True,help='zone_options.sqlite from nifty-zone-contracts');ap.add_argument('--out',default='zone_trades.sqlite')
    ap.add_argument('--split',default='2025-01-01',help='Extra valuation dates besides each 1 January, comma separated')
    ap.add_argument('--slippage',type=float,default=.5);ap.add_argument('--brokerage',type=float,default=20.);ap.add_argument('--max-participation',type=float,default=.10)
    ap.add_argument('--max-exit-lag-bars',type=int,default=9,help='45-minute bars a long may wait for a liquid exit once its short is gone, then it is booked at its last bar-close price less the haircut (not a fill). -1 waits')
    ap.add_argument('--forced-exit-haircut',type=float,default=.02);ap.add_argument('--structures',default=','.join(STRUCTURES))
    ap.add_argument('--tps',default='none,.25,.4,.5,.6,.75',help='Take-profit levels as shares of credit (calendar: debit); none = no take-profit')
    ap.add_argument('--stops',default='none',help='ATR periods for an index stop, e.g. none,15,30,45: exit when a 45-minute close goes beyond signal close -/+ --atr-mult x ATR')
    ap.add_argument('--atr-mult',type=float,default=1.);ap.add_argument('--keep-fills',action='store_true',help='Store every fill (large file)')
    a=ap.parse_args()
    market=Market(a.options,a.spot)
    planned=market.meta.get('plan',{})
    a.offsets=parse_offsets(a.offsets,a.offset_mode) if a.offsets is None or isinstance(a.offsets,str) else a.offsets
    missing=sorted(set(a.offsets)-set(planned.get('offsets',OFFSETS)))
    if missing:sys.exit(f'Offsets {missing} were not in the contract extraction (planned {planned.get("offsets",list(OFFSETS))}): rerun nifty-zone-contracts with --offsets into a new --out')
    for k in PLAN_KEYS+('from_date','to_date'):
        if k in planned and planned[k]!=getattr(a,k):sys.exit(f'--{k.replace("_","-")} {getattr(a,k)} differs from the contract extraction ({planned[k]}): rerun nifty-zone-contracts or match it')
    minutes=zs.load_minutes(a.spot)
    if not minutes:sys.exit(f'No spot minutes in {a.spot}: run nifty-zone-spot first')
    bars,final,_=zs.zones(minutes);sig=zs.signal_grid(bars,final)
    sim=Sim(market,bars,final,config(a),boundaries(bars,a.split))
    trades=sim.run(entry_map(bars,sig,a.from_date,a.to_date),structures=tuple(a.structures.split(',')),offsets=a.offsets,tps=parse_tps(a.tps),stops=parse_stops(a.stops))
    meta=dict(release=nifty_iv.__version__,args=vars(a),counts=dict(sim.counts),grid=zs.GRID,stale=zs.STALE,boundaries=sim.boundaries,
              fill_rule='first minute of the next bar with enough volume, at that minute\'s open; the minute\'s own volume is used')
    write(a.out,bars,final,sig,trades,meta,keep_fills=a.keep_fills)
    print('Counts:',dict(sim.counts));print(f'{len(trades)} trade rows -> {a.out}')

if __name__=='__main__':main()
