#!/usr/bin/env python3
"""NIFTY calendar-spread research from a local 1-minute snapshot SQLite cache.

Unhedged uses historical OHLC bars with explicit adverse fill assumptions.
Delta hedges use NIFTY *spot proxy* and are NOT executable futures backtests.
Gamma-reduced variant uses an actual historical call quote but fractional delta
spot proxy. No implied result is a guaranteed trading return.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import gzip
import json
import math
import sqlite3
from collections import Counter
from functools import lru_cache
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
TENOR_DAYS=(5,15,30,45,60,75,90,120)
HEDGES=('none','delta_entry_proxy','delta_daily_proxy','gamma_integer_delta_daily_proxy')
POLICIES=('weekly','p25_s50','p50_s100','iv_converge')


def at(day, minute):
 return dt.datetime.combine(dt.date.fromisoformat(day),dt.time.fromisoformat(minute),IST)

def texp(expiry, t):
 e=dt.datetime.combine(dt.date.fromisoformat(expiry),dt.time(15,30),IST)
 return max((e-t).total_seconds()/(365.25*86400),1e-8)

def bs(s,k,t,iv,cp,r,q):
 if iv<=0 or t<=0 or min(s,k)<=0: return float('nan')
 st=iv*math.sqrt(t);d1=(math.log(s/k)+(r-q+0.5*iv*iv)*t)/st;d2=d1-st
 if cp=='CE': return s*math.exp(-q*t)*norm.cdf(d1)-k*math.exp(-r*t)*norm.cdf(d2)
 return k*math.exp(-r*t)*norm.cdf(-d2)-s*math.exp(-q*t)*norm.cdf(-d1)

def implied_vol(s,k,t,price,cp,r,q):
 if not all(math.isfinite(x) for x in (s,k,t,price)) or s<=0 or k<=0 or price<=0: return None
 disc_s=s*math.exp(-q*t);disc_k=k*math.exp(-r*t)
 lower=max(0.,disc_s-disc_k) if cp=='CE' else max(0.,disc_k-disc_s)
 upper=disc_s if cp=='CE' else disc_k
 if price <= lower+0.02 or price>=upper-0.02: return None
 try:
  f=lambda vol:bs(s,k,t,vol,cp,r,q)-price
  if f(0.02)*f(3.0)>=0:return None
  result=brentq(f,0.02,3.0,xtol=1e-7)
  return result if 0.02<=result<=3.0 else None
 except (ValueError,OverflowError,ZeroDivisionError):return None

def greeks(s,k,t,iv,cp,r,q):
 if iv is None or iv<=0:return None
 d1=(math.log(s/k)+(r-q+0.5*iv*iv)*t)/(iv*math.sqrt(t))
 delta=math.exp(-q*t)*(norm.cdf(d1) if cp=='CE' else norm.cdf(d1)-1)
 gamma=math.exp(-q*t)*norm.pdf(d1)/(s*iv*math.sqrt(t))
 return delta,gamma

def vol_curve_from_rows(rows,spot,day,time,strike,cfg):
 # Filter to this strike's PE quotes; calculate variance interpolation without extrapolation.
 sample=[]
 for ex,rec in rows.items():
  x=rec.get((strike,'PE'))
  if not x or x['close'] is None or (x['volume'] or 0)<cfg.min_signal_volume:continue
  t=texp(ex,at(day,time));dte=t*365.25
  if dte<=0 or dte>365:continue
  vol=implied_vol(spot,strike,t,x['close'],'PE',cfg.rate,cfg.dividend)
  if vol is not None:sample.append((dte,vol*vol*t,vol))
 sample.sort()
 out={}
 for tenor in TENOR_DAYS:
  left=max((x for x in sample if x[0]<=tenor),default=None,key=lambda x:x[0])
  right=min((x for x in sample if x[0]>=tenor),default=None,key=lambda x:x[0])
  if left is None or right is None:out[f'iv_{tenor}']=None;continue
  if left[0]==right[0]:var=left[1]
  else:
   w=(tenor-left[0])/(right[0]-left[0]);var=left[1]*(1-w)+right[1]*w
  out[f'iv_{tenor}']=math.sqrt(max(var,0)/(tenor/365.25)) if var>=0 else None
 return out

class Data:
 def __init__(self,path):
  self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row
  try:meta=dict(self.db.execute('SELECT name,value FROM meta'))
  except sqlite3.Error:raise ValueError('Not a research cache; run prepare_research_cache.py')
  if meta.get('cache_complete')!='1':raise ValueError('Snapshot incomplete. Refusing to run backtest on partial SQLite extraction.')
  for t in ('09:45','09:46','15:29'):
   if t not in meta.get('minutes',''):raise ValueError(f'Cache lacks required {t} minute')
  self.meta=meta
  self.days=[x[0] for x in self.db.execute("SELECT DISTINCT day FROM spot WHERE ts LIKE '% 09:45:%' ORDER BY day")]
  self.dayset=set(self.days)
 @lru_cache(maxsize=6000)
 def spot(self,day,minute):
  x=self.db.execute('SELECT * FROM spot WHERE ts=?',(f'{day} {minute}:00',)).fetchone()
  return dict(x) if x else None
 @lru_cache(maxsize=12)
 def surface(self,provider,day,minute):
  rows=self.db.execute('''SELECT * FROM opt WHERE provider=? AND ts=?''',
   (provider,f'{day} {minute}:00')).fetchall()
  by_exp={}
  for x in rows:by_exp.setdefault(x['expiry'],{})[(x['strike'],x['cp'])]=dict(x)
  return by_exp
 @lru_cache(maxsize=50000)
 def option(self,provider,day,minute,expiry,strike,cp):
  x=self.db.execute('''SELECT * FROM opt WHERE provider=? AND ts=?
    AND expiry=? AND strike=? AND cp=?''',
   (provider,f'{day} {minute}:00',expiry,strike,cp)).fetchone()
  return dict(x) if x else None
 def close(self):self.db.close()


def round_strike(s,step):return math.floor(s/step+0.5)*step

def valid_quote(x,field,minvol):
 if x is None:return False
 v=x[field]
 return v is not None and math.isfinite(v) and v>0 and (x['volume'] or 0)>=minvol

def fee_flow(side,px,qty,cfg,trade_day):
 """Cash flow in index points/unit for option transaction; each leg is one order."""
 if not qty:return 0.
 traded=max(0.01,px+cfg.slippage if side=='buy' else px-cfg.slippage)
 prem=qty*traded
 gross=-prem if side=='buy' else prem
 # STT/stamp duty in option premium terms. Per-order brokerage is converted to points.
 total=(cfg.brokerage*(1+cfg.gst)/cfg.lot_size
       + prem*(cfg.exchange_bps/10000)*(1+cfg.gst)
       + prem*((cfg.stt if cfg.stt is not None else
                 (.000625 if trade_day<'2024-10-01' else .001 if trade_day<'2026-04-01' else .0015))
                if side=='sell' else cfg.stamp))
 return gross-total

def sproxy_cost(position_delta,s,cfg):return abs(position_delta)*s*cfg.hedge_bps/10000

def calc_dg(s,row,cp,ex,when,pricefield,cfg):
 if not valid_quote(row,pricefield,0):return None
 t=texp(ex,when);iv=implied_vol(s,row['strike'],t,row[pricefield],cp,cfg.rate,cfg.dividend)
 return greeks(s,row['strike'],t,iv,cp,cfg.rate,cfg.dividend)


def simulate(data,cfg,candidate,hedge,policy):
 """Simulate 1 unit short put, 1 unit long put, optionally an integer call.
 For weekly policies, inspect EOD at 15:29 and execute signal exits NEXT
 session 09:46. Predetermined weekly expiry exit uses 15:29 OPEN.
 """
 day,exs,exl,k=candidate['day'],candidate['short_expiry'],candidate['long_expiry'],candidate['strike']
 provider=cfg.provider
 s0=data.spot(day,'09:46')
 if not s0 or not valid_quote(s0,'open',0):return None,'no_spot_fill'
 spot0=s0['open'];fillt=at(day,'09:46')
 ps=data.option(provider,day,'09:46',exs,k,'PE')
 pl=data.option(provider,day,'09:46',exl,k,'PE')
 if not all(valid_quote(x,'open',cfg.min_fill_volume) for x in (ps,pl)):
  return None,'missing_entry_fill'
 gamma_qty=0;gq=None
 if hedge=='gamma_integer_delta_daily_proxy':
  gq=data.option(provider,day,'09:46',exs,k,'CE')
  if not valid_quote(gq,'open',cfg.min_fill_volume):return None,'missing_gamma_fill'
  dg_s=calc_dg(spot0,ps,'PE',exs,fillt,'open',cfg)
  dg_l=calc_dg(spot0,pl,'PE',exl,fillt,'open',cfg)
  dg_c=calc_dg(spot0,gq,'CE',exs,fillt,'open',cfg)
  if not dg_s or not dg_l or not dg_c or dg_c[1]<1e-9:return None,'bad_gamma_greek'
  target=(dg_s[1]-dg_l[1])/dg_c[1]
  gamma_qty=max(0,min(cfg.max_gamma_lots,math.floor(target+0.5)))
  if gamma_qty==0:return None,'gamma_rounds_zero'
 cash=fee_flow('sell',ps['open'],1,cfg,day)+fee_flow('buy',pl['open'],1,cfg,day)
 if gamma_qty:cash+=fee_flow('buy',gq['open'],gamma_qty,cfg,day)
 initial_debit=pl['open']-ps['open']
 if initial_debit<=cfg.min_debit:return None,'nonpositive_calendar_debit'
 if hedge!='none':
  dg_s=calc_dg(spot0,ps,'PE',exs,fillt,'open',cfg)
  dg_l=calc_dg(spot0,pl,'PE',exl,fillt,'open',cfg)
  if not dg_s or not dg_l:return None,'bad_delta_greek'
  dg_g=calc_dg(spot0,gq,'CE',exs,fillt,'open',cfg) if gamma_qty else (0.,0.)
  delta_hedge=dg_s[0]-dg_l[0]-gamma_qty*dg_g[0]
  cash-=sproxy_cost(delta_hedge,spot0,cfg)
 else:delta_hedge=0.
 hedge_pnl=0.;last_s=spot0;gross_gamma=gamma_qty
 exit_day=None;exit_time=None;reason=None;marks=0;marked_low=0.;signal_exit=False
 enter_index=data.days.index(day)
 for d in data.days[enter_index:]:
  if d>exs:break
  if d!=day:
   x=data.spot(d,'09:46')
   if not x or not valid_quote(x,'open',0):return None,'missing_daily_spot'
   s=x['open'];hedge_pnl+=delta_hedge*(s-last_s);last_s=s
   if signal_exit:
    exit_day=d;exit_time='09:46';reason='EOD_signal_next_session';break
   if hedge in ('delta_daily_proxy','gamma_integer_delta_daily_proxy'):
    short=data.option(provider,d,'09:46',exs,k,'PE')
    long=data.option(provider,d,'09:46',exl,k,'PE')
    gam=data.option(provider,d,'09:46',exs,k,'CE') if gamma_qty else None
    if not all(valid_quote(xx,'open',cfg.min_fill_volume) for xx in (short,long)):
     return None,'missing_delta_rebalance_quote'
    dg_s=calc_dg(s,short,'PE',exs,at(d,'09:46'),'open',cfg)
    dg_l=calc_dg(s,long,'PE',exl,at(d,'09:46'),'open',cfg)
    dg_g=calc_dg(s,gam,'CE',exs,at(d,'09:46'),'open',cfg) if gamma_qty else (0.,0.)
    if not dg_s or not dg_l or not dg_g:return None,'bad_rebalance_greek'
    desired=dg_s[0]-dg_l[0]-gamma_qty*dg_g[0]
    if abs(desired-delta_hedge)>=cfg.delta_band:
     cash-=sproxy_cost(desired-delta_hedge,s,cfg)
     delta_hedge=desired
  if d==exs or (d<exs and data.days.index(d)==len(data.days)-1):
   exit_day=d;exit_time='15:29';reason='weekly_exit';break
  if policy!='weekly':
   se=data.spot(d,'15:29')
   os=data.option(provider,d,'15:29',exs,k,'PE')
   ol=data.option(provider,d,'15:29',exl,k,'PE')
   og=data.option(provider,d,'15:29',exs,k,'CE') if gamma_qty else None
   if not se or not all(valid_quote(y,'close',cfg.min_mark_volume) for y in (os,ol)) or (gamma_qty and not valid_quote(og,'close',cfg.min_mark_volume)):
    return None,'missing_EOD_mark'
   sm=se['close'];markcash=(os['close']*-1 + ol['close']+gamma_qty*(og['close'] if og else 0))
   unrl=cash+hedge_pnl+delta_hedge*(sm-last_s)+markcash
   marked_low=min(marked_low,unrl);marks+=1
   if policy in ('p25_s50','p50_s100'):
    pt,sl=(0.25,0.50) if policy=='p25_s50' else (0.50,1.00)
    signal_exit=unrl>=initial_debit*pt or unrl<=-initial_debit*sl
   elif policy=='iv_converge':
    ivs=implied_vol(sm,k,texp(exs,at(d,'15:29')),os['close'],'PE',cfg.rate,cfg.dividend)
    ivl=implied_vol(sm,k,texp(exl,at(d,'15:29')),ol['close'],'PE',cfg.rate,cfg.dividend)
    signal_exit=(ivs is not None and ivl is not None and ivs/ivl<cfg.iv_convergence)
 # If weekly expiry not in spot days (holiday / missing coverage), don't invent exit.
 if exit_day is None:return None,'missing_weekly_exit'
 se=data.spot(exit_day,exit_time)
 os=data.option(provider,exit_day,exit_time,exs,k,'PE')
 ol=data.option(provider,exit_day,exit_time,exl,k,'PE')
 og=data.option(provider,exit_day,exit_time,exs,k,'CE') if gamma_qty else None
 if not se or not all(valid_quote(y,'open',cfg.min_exit_volume) for y in (os,ol)) or (gamma_qty and not valid_quote(og,'open',cfg.min_exit_volume)):
  return None,'missing_exit_fill'
 sx=se['open'];hedge_pnl+=delta_hedge*(sx-last_s)
 if hedge!='none':cash-=sproxy_cost(delta_hedge,sx,cfg)
 cash+=fee_flow('buy',os['open'],1,cfg,exit_day)+fee_flow('sell',ol['open'],1,cfg,exit_day)
 if gamma_qty:cash+=fee_flow('sell',og['open'],gamma_qty,cfg,exit_day)
 pnl=cash+hedge_pnl
 out=dict(candidate,hedge=hedge,exit_policy=policy,exit_day=exit_day,exit_time=exit_time,
          exit_reason=reason,gamma_call_lots=gamma_qty,
          entry_debit_points=round(initial_debit,6),pnl_points=round(pnl,6),
          pnl_pct_debit=round(pnl/initial_debit*100,4),
          min_EOD_mark_points=round(marked_low,5),daily_marks=marks,
          delta_proxy_points=round(hedge_pnl,5))
 return out,None


def simulate_carry30(data,cfg,candidate):
 # No repeated weekly writes. Close first weekly short; retain original long
 # until first trading day at or below 30 DTE, then close. No proxy hedging.
 day,exs,exl,k=candidate['day'],candidate['short_expiry'],candidate['long_expiry'],candidate['strike']
 if (dt.date.fromisoformat(exl)-dt.date.fromisoformat(day)).days<=30:return None,'carry_short_tenor'
 prov=cfg.provider;s=data.option(prov,day,'09:46',exs,k,'PE');l=data.option(prov,day,'09:46',exl,k,'PE')
 if not all(valid_quote(x,'open',cfg.min_fill_volume) for x in (s,l)):return None,'missing_entry_fill'
 debit=l['open']-s['open']
 if debit<=cfg.min_debit:return None,'nonpositive_calendar_debit'
 cash=fee_flow('sell',s['open'],1,cfg,day)+fee_flow('buy',l['open'],1,cfg,day)
 short_day=exs if exs in data.dayset else None
 if short_day is None:return None,'missing_weekly_exit'
 y=data.option(prov,short_day,'15:29',exs,k,'PE')
 if not valid_quote(y,'open',cfg.min_exit_volume):return None,'missing_weekly_exit_fill'
 cash+=fee_flow('buy',y['open'],1,cfg,short_day)
 long_day=next((d for d in data.days if d>=short_day and (dt.date.fromisoformat(exl)-dt.date.fromisoformat(d)).days<=30),None)
 if long_day is None or long_day>=exl:return None,'missing_carry_exit_day'
 yl=data.option(prov,long_day,'15:29',exl,k,'PE')
 if not valid_quote(yl,'open',cfg.min_exit_volume):return None,'missing_carry_exit_fill'
 cash+=fee_flow('sell',yl['open'],1,cfg,long_day)
 return dict(candidate,hedge='none',exit_policy='carry30',exit_day=long_day,exit_time='15:29',
       exit_reason='long_30DTE',gamma_call_lots=0,entry_debit_points=round(debit,6),
       pnl_points=round(cash,6),pnl_pct_debit=round(cash/debit*100,4),
       min_EOD_mark_points='',daily_marks=0,delta_proxy_points=0),None

FIELDS=['trade_id','day','entry_time','provider','strike','strike_step','strike_offset','target_tenor','moneyness_pct',
  'short_expiry','long_expiry','short_dte','long_dte','iv_short','iv_long','iv_ratio',
  'iv_spread_pp']+[f'iv_{t}' for t in TENOR_DAYS]+[
  'hedge','exit_policy','exit_day','exit_time','exit_reason','gamma_call_lots',
  'entry_debit_points','pnl_points','pnl_pct_debit','min_EOD_mark_points','daily_marks','delta_proxy_points']


def candidates(data,cfg,stats=None):
 stats = stats if stats is not None else Counter()
 tid=0
 for day in data.days:
  if dt.date.fromisoformat(day).weekday() not in cfg.weekdays:continue
  if day<cfg.start or day>cfg.end:continue
  ss=data.spot(day,'09:45');sp=data.spot(day,'09:46')
  if not ss or not sp or not valid_quote(ss,'close',0) or not valid_quote(sp,'open',0):stats['no_spot']+=1;continue
  s=ss['close'];chain=data.surface(cfg.provider,day,'09:45')
  if not chain:stats['no_chain']+=1;continue
  for step in cfg.round_steps:
   for offset in cfg.offsets:
    k=float(round_strike(s,step)+offset)
    if k<=0:continue
    weekly=[]
    for ex,quotes in chain.items():
     dte=(dt.date.fromisoformat(ex)-dt.date.fromisoformat(day)).days
     row=quotes.get((k,'PE'))
     if 1<=dte<=cfg.max_short_dte and valid_quote(row,'close',cfg.min_signal_volume):weekly.append((dte,ex))
    if not weekly:stats['no_weekly']+=1;continue
    sdte,sex=min(weekly)
    short=chain[sex][(k,'PE')]
    sv=implied_vol(s,k,texp(sex,at(day,'09:45')),short['close'],'PE',cfg.rate,cfg.dividend)
    if sv is None:stats['bad_short_iv']+=1;continue
    curve=vol_curve_from_rows(chain,s,day,'09:45',k,cfg)
    for tenor in cfg.long_tenors:
     choices=[]
     for ex,quotes in chain.items():
      dte=(dt.date.fromisoformat(ex)-dt.date.fromisoformat(day)).days
      row=quotes.get((k,'PE'))
      if dte>sdte and abs(dte-tenor)<=cfg.tenor_tolerance and valid_quote(row,'close',cfg.min_signal_volume):
       choices.append((abs(dte-tenor),dte,ex))
     if not choices:stats['no_long_tenor']+=1;continue
     _,ldte,lex=min(choices)
     long=chain[lex][(k,'PE')]
     lv=implied_vol(s,k,texp(lex,at(day,'09:45')),long['close'],'PE',cfg.rate,cfg.dividend)
     if lv is None:stats['bad_long_iv']+=1;continue
     tid+=1
     row={'trade_id':tid,'day':day,'entry_time':'09:46','provider':cfg.provider,'strike':k,
       'strike_step':step,'strike_offset':offset,'target_tenor':tenor,'moneyness_pct':round((k/s-1)*100,5),
       'short_expiry':sex,'long_expiry':lex,'short_dte':sdte,'long_dte':ldte,
       'iv_short':round(sv,7),'iv_long':round(lv,7),'iv_ratio':round(sv/lv,6),
       'iv_spread_pp':round((sv-lv)*100,5)}
     row.update({f'iv_{t}':round(v,7) if v is not None else '' for t,v in ((x,curve[f'iv_{x}']) for x in TENOR_DAYS)})
     stats['valid_signal_candidates']+=1
     yield row,stats


def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--cache',default='research_cache.sqlite')
 ap.add_argument('--out',default='research_trades.csv.gz')
 ap.add_argument('--provider',choices=('rissin_upstox','thetrademarkk'),default='rissin_upstox')
 ap.add_argument('--from-date',dest='start',default='2025-09-01');ap.add_argument('--to-date',dest='end',default='2026-07-01')
 ap.add_argument('--weekdays',default='2,3,4',help='Mon=0; Wed/Thu/Fri=2,3,4')
 ap.add_argument('--round-steps',default='100,500');ap.add_argument('--offsets',default='-500,-250,-100,0,100,250,500')
 ap.add_argument('--long-tenors',default='15,30,45,60,75,90,120')
 ap.add_argument('--tenor-tolerance',type=int,default=9)
 ap.add_argument('--max-short-dte',type=int,default=8)
 ap.add_argument('--min-signal-volume',type=int,default=1)
 ap.add_argument('--min-fill-volume',type=int,default=1);ap.add_argument('--min-exit-volume',type=int,default=1)
 ap.add_argument('--min-mark-volume',type=int,default=1)
 ap.add_argument('--hedges',default=','.join(HEDGES))
 ap.add_argument('--policies',default=','.join(POLICIES)+',carry30')
 ap.add_argument('--rate',type=float,default=.06);ap.add_argument('--dividend',type=float,default=.015)
 ap.add_argument('--lot-size',type=int,default=65,help='Hypothetical LOT SIZE for converting per-order fees into index points; contracts historically differ!')
 ap.add_argument('--brokerage',type=float,default=20.,help='INR per option leg order, illustrative')
 ap.add_argument('--gst',type=float,default=.18);ap.add_argument('--stt',type=float,default=None,help='Override STT fraction; default uses historical 2024/2026 sale-premium changes')
 ap.add_argument('--stamp',type=float,default=.00003)
 ap.add_argument('--exchange-bps',type=float,default=3.5)
 ap.add_argument('--slippage',type=float,default=0.5,help='Adverse option price points per side')
 ap.add_argument('--hedge-bps',type=float,default=2.0,help='Hypothetical spot-proxy hedge cost per turnover')
 ap.add_argument('--delta-band',type=float,default=.10)
 ap.add_argument('--max-gamma-lots',type=int,default=3)
 ap.add_argument('--iv-convergence',type=float,default=1.0)
 ap.add_argument('--min-debit',type=float,default=0.01)
 ap.add_argument('--max-candidates',type=int,default=0,help='For a quick integration check; 0 means all')
 args=ap.parse_args()
 args.weekdays=set(int(x) for x in args.weekdays.split(','));args.round_steps=[int(x) for x in args.round_steps.split(',')]
 args.offsets=[int(x) for x in args.offsets.split(',')];args.long_tenors=[int(x) for x in args.long_tenors.split(',')]
 hedges=args.hedges.split(',');policies=args.policies.split(',')
 if not set(hedges)<=set(HEDGES):ap.error('Unknown hedge model')
 if not set(policies)<=set(POLICIES)|{'carry30'}:ap.error('Unknown exit policy')
 if args.lot_size<=0:ap.error('lot-size must be >0')
 data=Data(args.cache)
 print('Cache coverage:',data.meta,'days:',len(data.days),flush=True)
 stats=Counter();candidate_stats=Counter();count=0
 path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True)
 opener=gzip.open if path.suffix=='.gz' else open
 try:
  with opener(path,'wt',newline='',encoding='utf-8') as file:
   w=csv.DictWriter(file,fieldnames=FIELDS,extrasaction='ignore');w.writeheader()
   for cand,_ in candidates(data,args,candidate_stats):
    count+=1
    for hedge in hedges:
     for policy in policies:
      if policy=='carry30':
       if hedge!='none':continue
       result,err=simulate_carry30(data,args,cand)
      else:result,err=simulate(data,args,cand,hedge,policy)
      if result is not None:w.writerow(result);stats['completed_variants']+=1
      else:stats[err]+=1
    if count%200==0:print(f'Candidates {count:,}; completed variants {stats["completed_variants"]:,}',flush=True)
    if args.max_candidates and count>=args.max_candidates:break
 finally:data.close()
 print('Final candidates:',count,'output rows:',stats['completed_variants'])
 print('Failure reasons:')
 for k,v in stats.most_common():print(' ',k,v)
 print('Candidate coverage reasons:')
 for k,v in candidate_stats.most_common():print(' ',k,v)
 metadata={'provisional':True,'backend':'1-minute OHLC research, not quotes',
       'candidate_count':count,'completed_variants':stats['completed_variants'],
       'failure_counts':dict(stats),'candidate_counts':dict(candidate_stats),
       'parameters':{k:(sorted(v) if isinstance(v,set) else v) for k,v in vars(args).items()}}
 Path(str(path)+'.metadata.json').write_text(json.dumps(metadata,indent=2))
 print('Output:',path)

if __name__=='__main__': main()
