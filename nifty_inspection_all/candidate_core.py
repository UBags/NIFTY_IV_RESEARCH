#!/usr/bin/env python3
"""V3 NIFTY signal generator and source-aware SQLite access.

Results are RESEARCH ONLY. OHLC minute candles are not executable bid/ask quotes.
Spot-index hedging is a theoretical proxy, NOT a traded futures position.
"""
from __future__ import annotations
import argparse,datetime as dt,json,hashlib,math,sqlite3
from collections import Counter
from functools import lru_cache
from pathlib import Path
from scipy.special import ndtri
from quant_v3 import parity_ols,implied_vol,black_greeks,forward_variance   # implied_vol: Data.greek
from contract_specs_v2 import LotMaster

IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
TENORS=(5,15,30,45,60,75,90,120)
CHECKPOINTS=(('11:30','11:31'),('13:30','13:31'),('15:15','15:16'))


def years(ex,day,time):
 when=dt.datetime.combine(dt.date.fromisoformat(day),dt.time.fromisoformat(time),IST)
 end=dt.datetime.combine(dt.date.fromisoformat(ex),dt.time(15,30),IST)
 return (end-when).total_seconds()/(365.25*86400)

# Fill models whose eligibility and price use nothing first known after the order. Everything else is a conditional diagnostic.
CAUSAL_FILL_MODELS=('next_bar_open',)
ADVERSE_FILL_MODELS=('next_bar_open_adverse',)   # same clock, but the price reads the whole fill bar: conservative, not causal
TRADED_BAR_FILL_MODELS=('next_bar_open_traded_only',)   # drops fills on bars that ended with no trade: an ex-post filter, for sensitivity only
STRESS_FILL_MODELS=ADVERSE_FILL_MODELS+TRADED_BAR_FILL_MODELS

def dte(day,ex):return (dt.date.fromisoformat(ex)-dt.date.fromisoformat(day)).days

def is_quote(q,field='close',volume=0):
 return q is not None and q.get(field) is not None and math.isfinite(q[field]) and q[field]>0 and (q.get('volume') or 0)>=volume

class Data:
 def __init__(self,path):
  self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row
  self.meta=dict(self.db.execute('SELECT name,value FROM meta'))
  if self.meta.get('version')!='3' or self.meta.get('research_eligible')!='1':
   raise ValueError('V3 research-eligible cache required; see prepare_research_cache.py')
  self.days=[r[0] for r in self.db.execute('''SELECT DISTINCT day FROM spot
       WHERE substr(ts,12,5)='09:45' ORDER BY day''')]
  self.dayset=set(self.days)
  self.enriched=self.meta.get('enrichment_complete')=='1'
  self.complete_sessions=dict(self.db.execute('SELECT day,complete_session FROM spot_daily'))
  self.special_sessions=set()
  self.events=[]   # published-in-advance dates (RBI policy, Budget, results of national elections); see --event-calendar
 def entry_session_eligible(self,day,allow_special=False):
  """Known at 09:45: not a weekend and not on a list published in advance. How the day later ended cannot decide this."""
  return allow_special or (dt.date.fromisoformat(day).weekday()<5 and day not in self.special_sessions)
 @lru_cache(16000)
 def spot(self,day,minute):
  x=self.db.execute('SELECT * FROM spot WHERE ts=?',(f'{day} {minute}:00',)).fetchone()
  return dict(x) if x else None
 @lru_cache(80)
 def surface(self,provider,day,minute):
  rows=self.db.execute('SELECT * FROM opt WHERE provider=? AND ts=?',(provider,f'{day} {minute}:00')).fetchall()
  out={}
  for x in rows:out.setdefault(x['expiry'],{})[(float(x['strike']),x['cp'])]=dict(x)
  return out
 @lru_cache(3000)
 def chain(self,provider,day,minute,expiry):
  """One expiry's quotes; avoids materialising the whole surface for a mark or a parity fit."""
  rows=self.db.execute('SELECT * FROM opt WHERE provider=? AND ts=? AND expiry=?',(provider,f'{day} {minute}:00',expiry)).fetchall()
  return {(float(x['strike']),x['cp']):dict(x) for x in rows}
 @lru_cache(3000)
 def expiries_at(self,provider,day,minute):
  return tuple(r[0] for r in self.db.execute('SELECT DISTINCT expiry FROM opt WHERE provider=? AND ts=?',(provider,f'{day} {minute}:00')))
 @lru_cache(30000)
 def opt(self,provider,day,minute,expiry,strike,cp):
  x=self.db.execute('''SELECT * FROM opt WHERE provider=? AND ts=? AND expiry=?
    AND strike=? AND cp=?''',(provider,f'{day} {minute}:00',expiry,float(strike),cp)).fetchone()
  return dict(x) if x else None
 @lru_cache(30000)
 def forward(self,provider,day,minute,expiry,rate,minvol,maxdisp):
  ts=f'{day} {minute}:00'
  if (self.enriched and rate==float(self.meta.get('enrichment_rate','nan'))
      and minvol==int(self.meta.get('enrichment_min_pair_volume','-1'))
      and maxdisp==float(self.meta.get('enrichment_max_parity_bps','nan'))):
   x=self.db.execute('SELECT forward,matched_pairs,dispersion_bps,quality,implied_rate FROM forward_cache WHERE provider=? AND ts=? AND expiry=?',
       (provider,ts,expiry)).fetchone()
   if x:
    return dict(forward=x['forward'],quality=x['quality'],matched_pairs=x['matched_pairs'],dispersion_bps=x['dispersion_bps'],rate=x['implied_rate'])
  s=self.spot(day,minute)
  quotes=self.chain(provider,day,minute,expiry)
  t=years(expiry,day,minute)
  est=parity_ols(quotes,s['close'],t,rate,minvol,maxdisp) if s and is_quote(s) and t>0 else None
  if est:return dict(forward=est.forward,quality=est.quality,matched_pairs=est.matched_pairs,dispersion_bps=est.dispersion_bps,rate=est.rate)
  return dict(forward=None,quality='no_spot',matched_pairs=0,dispersion_bps=None,rate=None)
 @lru_cache(50000)
 def greek(self,provider,day,minute,ex,k,cp,rate,minvol,disp):
  q=self.opt(provider,day,minute,ex,k,cp)
  s=self.spot(day,minute)
  if not is_quote(q,volume=0) or not is_quote(s):return None
  if (self.enriched and rate==float(self.meta.get('enrichment_rate','nan'))
      and minvol==int(self.meta.get('enrichment_min_pair_volume','-1'))
      and disp==float(self.meta.get('enrichment_max_parity_bps','nan'))):
   x=self.db.execute('''SELECT iv,delta_proxy,gamma_proxy,vega FROM greeks_cache
     WHERE provider=? AND ts=? AND expiry=? AND strike=? AND cp=?''',
     (provider,f'{day} {minute}:00',ex,float(k),cp)).fetchone()
   if x and x['iv'] is not None:return dict(x)
  fi=self.forward(provider,day,minute,ex,rate,minvol,disp)
  f=fi['forward'];r=fi.get('rate') if fi.get('rate') is not None else rate
  t=years(ex,day,minute)
  if not f or t<=0:return None
  iv=implied_vol(f,k,t,q['close'],cp,r)
  if not iv:return None
  gr=black_greeks(f,k,t,iv,cp,r,s['close']);gr['iv']=iv
  return gr
 def close(self):
  for name in ('spot','surface','chain','expiries_at','opt','forward','greek'):getattr(type(self),name).cache_clear()   # lru keys hold self
  self.db.close()


def possible_strikes(s,forward,sig,tshort,cfg):
 # Always snap to an exchange strike interval, do not assume all strikes exist.
 for step in cfg.round_steps:
  for val in cfg.strike_values:
   if cfg.strike_mode=='points':target=s+val
   elif cfg.strike_mode=='pct':target=forward*math.exp(val/100)
   elif cfg.strike_mode=='std':target=forward*math.exp(val*sig*math.sqrt(tshort))
   elif cfg.strike_mode=='delta':
    # target put delta: -0.25 -> d1=Phi^-1(0.75)
    if not (-1<val<0 if cfg.option_type=='PE' else 0<val<1):continue
    d1=ndtri(1+val if cfg.option_type=='PE' else val)
    target=forward*math.exp(-d1*sig*math.sqrt(tshort)+.5*sig*sig*tshort)
   else:raise ValueError('unknown strike mode')
   k=math.floor(target/step+.5)*step
   if k>0:yield float(k),step,val


def nearest_short(chain,day,max_dte):
 opts=sorted((dte(day,ex),ex) for ex in chain if 1<=dte(day,ex)<=max_dte)
 return opts[0] if opts else None


def long_strike_offsets(cfg):
 return getattr(cfg,'long_strike_offsets',None) or [0.]


def nearest_long(chain,day,short_dte,k,target,cfg):
 """(listed days, expiry) closest to the target tenor with a 09:45 quote at this exact strike."""
 opts=[]
 for ex,quotes in chain.items():
  ld=dte(day,ex)
  if ld>short_dte and abs(ld-target)<=cfg.tenor_tolerance and is_quote(quotes.get((k,cfg.option_type)),volume=cfg.min_signal_volume):
   opts.append((abs(ld-target),ld,ex))
 if not opts:return None
 _,ld,ex=min(opts)
 return ld,ex


def signal_iv(data,provider,day,expiry,strike,cfg):
 """09:45 IV through data.greek: the enriched cache when it applies, and the same number the simulator reads."""
 g=data.greek(provider,day,'09:45',expiry,strike,cfg.option_type,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
 return g['iv'] if g and g.get('iv') else None


def curve(data,provider,chain,day,k,cfg):
 """Term structure at one fixed strike. Calendars read this."""
 observations=[]
 for ex,qs in chain.items():
  q=qs.get((k,cfg.option_type));t=years(ex,day,'09:45')
  if not is_quote(q,volume=cfg.min_signal_volume) or not (0<t<1):continue
  sigma=signal_iv(data,provider,day,ex,k,cfg)
  if sigma:observations.append((t*365.25,sigma*sigma*t))
 return _interpolate_curve(observations)


def atm_curve(data,provider,chain,day,cfg):
 """Term structure read at each expiry's own near-forward liquid strike. Every point is at-the-money, so no point mixes skew into another.
 Iron flies read this: they need no quote at their own strike in far months."""
 observations=[]
 for ex,qs in chain.items():
  t=years(ex,day,'09:45')
  if not 0<t<1:continue
  f=data.forward(provider,day,'09:45',ex,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps).get('forward')
  near=sorted((abs(s-f),s) for (s,cp),q in qs.items() if cp==cfg.option_type and abs(s/f-1)<=.04 and is_quote(q,volume=cfg.min_signal_volume)) if f else []
  sigma=signal_iv(data,provider,day,ex,near[0][1],cfg) if near else None
  if sigma:observations.append((t*365.25,sigma*sigma*t))
 return _interpolate_curve(observations)


def _interpolate_curve(observations):
 observations.sort()
 out={}
 for tenor in TENORS:
  below=max((x for x in observations if x[0]<=tenor),default=None,key=lambda x:x[0])
  above=min((x for x in observations if x[0]>=tenor),default=None,key=lambda x:x[0])
  if tenor==5 and not below and above and above[0]<=6.5:
   out['iv_5']=math.sqrt(above[1]/(above[0]/365.25));continue   # front weekly sits just past 5 days: flat vol
  if not below or not above:out[f'iv_{tenor}']=None;continue
  if below[0]==above[0]:var=below[1]
  else:
   weight=(tenor-below[0])/(above[0]-below[0]);var=(1-weight)*below[1]+weight*above[1]
  out[f'iv_{tenor}']=math.sqrt(var/(tenor/365.25)) if var>0 else None
 return out


def generate(data,cfg,stats):
 for day in data.days:
  if not cfg.start<=day<=cfg.end or dt.date.fromisoformat(day).weekday() not in cfg.weekdays:continue
  if not data.entry_session_eligible(day,getattr(cfg,'allow_special_sessions',False)):stats['ineligible_session']+=1;continue
  st=data.spot(day,'09:45')
  if not is_quote(st):stats['missing_spot']+=1;continue
  chain=data.surface(cfg.provider,day,'09:45')
  nearest=nearest_short(chain,day,cfg.max_short_dte)
  if not nearest:stats['no_short_expiry']+=1;continue
  sd,short_exp=nearest
  decided=f'{day} 09:45'
  events=sum(day<=x<=short_exp and known<=decided<gone for x,known,gone in data.events)   # as known at the 09:45 decision: announced by then, not yet withdrawn or rescheduled
  policy=getattr(cfg,'event_policy','all')
  if (policy=='skip_event_weeks' and events) or (policy=='only_event_weeks' and not events):stats['event_policy_skip']+=1;continue
  f_info=data.forward(cfg.provider,day,'09:45',short_exp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
  shortf=f_info['forward']
  if not shortf:stats['no_short_forward']+=1;continue
  # Forward-ATM volatility: nearest liquid put at current short expiry.
  atm_q=sorted(((abs(k-shortf),k,q) for (k,cp),q in chain[short_exp].items()
                if cp==cfg.option_type and is_quote(q,volume=cfg.min_signal_volume)),key=lambda x:x[0])
  if not atm_q:stats['missing_atm_option']+=1;continue
  short_r=f_info.get('rate') if f_info.get('rate') is not None else cfg.rate
  kt=atm_q[0][1];vol=signal_iv(data,cfg.provider,day,short_exp,kt,cfg)
  if not vol:stats['bad_atm_iv']+=1;continue
  seen=set();flat_vols=atm_curve(data,cfg.provider,chain,day,cfg) if getattr(cfg,'include_iron_fly',False) else None
  for k,step,val in possible_strikes(st['close'],shortf,vol,years(short_exp,day,'09:45'),cfg):
   if (k,step,val) in seen:continue
   seen.add((k,step,val))
   sq=chain[short_exp].get((k,cfg.option_type))
   if not is_quote(sq,volume=cfg.min_signal_volume):stats['missing_short_signal']+=1;continue
   sv=signal_iv(data,cfg.provider,day,short_exp,k,cfg)
   if not sv:stats['bad_short_iv']+=1;continue
   vols=curve(data,cfg.provider,chain,day,k,cfg)
   closed={str(x) for x in data.holidays}
   sessions=sum(1 for n in range(1,sd+1) if (x:=dt.date.fromisoformat(day)+dt.timedelta(days=n)).weekday()<5 and x.isoformat() not in closed)   # weekdays less published holidays; later data gaps must not reach a signal
   bucket='1-2' if sessions<=2 else '3-5' if sessions<=5 else '6-8'
   if getattr(cfg,'include_iron_fly',False):
    lot_s=cfg.lot_master.get(day,short_exp)
    if lot_s.qty is None:stats['ambiguous_lot_size']+=1
    else:
     # A same-expiry structure: nothing here reads a long-dated quote, forward or IV.
     fly=dict(day=day,provider=cfg.provider,strike=k,long_strike=k,long_strike_offset=0.,strike_step=step,strike_value=val,strike_mode=cfg.strike_mode,
          short_expiry=short_exp,long_expiry=None,short_dte=sd,long_dte=None,target_tenor=0,qty=lot_s.qty,short_lot_evidence=lot_s.source,long_lot_evidence=None,
          short_f=shortf,long_f=None,short_forward_quality=f_info['quality'],long_forward_quality=None,moneyness_pct=(k/st['close']-1)*100,
          iv_short=sv,iv_long=None,iv_ratio=None,iv_spread_pp=None,forward_iv=None,forward_iv_ratio=None,net_vega=None,
          short_sessions=sessions,events_in_short=events,short_dte_bucket=bucket,is_fly=True)
     fly.update(flat_vols);stats['eligible_iron_fly_signals']+=1
     yield fly
   if not getattr(cfg,'include_calendars',True):continue
   for target in cfg.long_tenors:
    for offset in long_strike_offsets(cfg):
     kl=float(k+offset)
     chosen_long=nearest_long(chain,day,sd,kl,target,cfg) if kl>0 else None
     if not chosen_long:stats['no_long_tenor']+=1;continue
     ld,lex=chosen_long
     lf_info=data.forward(cfg.provider,day,'09:45',lex,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
     lf=lf_info['forward']
     if not lf:stats['no_long_forward']+=1;continue
     long_r=lf_info.get('rate') if lf_info.get('rate') is not None else cfg.rate
     lv=signal_iv(data,cfg.provider,day,lex,kl,cfg)
     if not lv:stats['bad_long_iv']+=1;continue
     lot_s=cfg.lot_master.get(day,short_exp);lot_l=cfg.lot_master.get(day,lex)
     if lot_s.qty is None or lot_l.qty is None:stats['ambiguous_lot_size']+=1;continue
     if lot_s.qty!=lot_l.qty:stats['mismatched_lots']+=1;continue
     t1=years(short_exp,day,'09:45');t2=years(lex,day,'09:45')
     fwd_vol=forward_variance(sv,lv,t1,t2) if offset==0 else None   # variance across different strikes is not forward variance
     if offset==0 and fwd_vol is None:stats['nonpositive_forward_variance']+=1;continue
     gs=black_greeks(shortf,k,t1,sv,cfg.option_type,short_r,st['close'])
     gl=black_greeks(lf,kl,t2,lv,cfg.option_type,long_r,st['close'])
     row=dict(day=day,provider=cfg.provider,strike=k,long_strike=kl,long_strike_offset=float(offset),strike_step=step,strike_value=val,
          strike_mode=cfg.strike_mode,short_expiry=short_exp,long_expiry=lex,
          short_dte=sd,long_dte=ld,target_tenor=target,qty=lot_s.qty,
          short_lot_evidence=lot_s.source,long_lot_evidence=lot_l.source,
          short_f=shortf,long_f=lf,short_forward_quality=f_info['quality'],
          long_forward_quality=lf_info['quality'],moneyness_pct=(k/st['close']-1)*100,
          iv_short=sv,iv_long=lv,iv_ratio=sv/lv,iv_spread_pp=(sv-lv)*100,
          forward_iv=fwd_vol,forward_iv_ratio=sv/fwd_vol if fwd_vol else None,
          net_vega=gl['vega']-gs['vega'],
          short_sessions=sessions,events_in_short=events,short_dte_bucket=bucket)
     row.update(vols)
     stats['eligible_signals']+=1
     yield row


