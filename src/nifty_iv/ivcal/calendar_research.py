#!/usr/bin/env python3
"""V3 NIFTY research simulator: retained positions, missing-mark audit and rolling calendars.

All prices are minute OHLC proxies (no bid/ask). Expiry settlement is spot-CLOSE
PROXY, NOT official NSE settlement. Futures delta hedge is an index proxy only.
No portfolio margin is claimed to be NSE SPAN. See README_V4.md.
"""
from __future__ import annotations
import argparse,sqlite3,datetime as dt,math,json,hashlib,sys
from collections import Counter,defaultdict
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
import numpy as np
from nifty_iv.core import lineage as lineage_mod
from nifty_iv.ivcal.candidate_core import IST,Data as BaseData,generate,years,dte,is_quote,possible_strikes,CAUSAL_FILL_MODELS,STRESS_FILL_MODELS,ADVERSE_FILL_MODELS,TRADED_BAR_FILL_MODELS
from nifty_iv.core.contract_specs import LotMaster
from nifty_iv.ivcal.settlements import SettlementMaster
from nifty_iv.ivcal.quant_v3 import black_forward,black_greeks,implied_vol,trailing_rv,yang_zhang_rv,forward_variance
from nifty_iv.core import cli

TARIFF_VERSION='nse-2026-11-statutory-v1'   # bump when a dated rate is added or changed
ENTRY_WINDOWS=('09:46','09:47','09:48','09:49','09:50')
SIGNALS=(('11:30','11:31','11:32'),('13:30','13:31','13:32'),('15:15','15:16','15:17'))
CLOSE_WINDOW=('15:16','15:17')
# Take/stop as fractions of the amount at risk at entry. None = no price trigger.
PRICE_EXITS={'p25_s50':(.25,-.5),'p50_s100':(.5,-1.),'p25_s50_tm1':(.25,-.5)}
TM1_EXITS=('tm1','p25_s50_tm1')   # flat at the close of the last session before the short expires
WEEKLY_FAMILY=('weekly','p25_s50','p50_s100','iv_converge')+TM1_EXITS   # one short cycle, then out
ROLLING_POLICIES=('rolling','rolling_static','rolling_diagonal','rolling_recenter','rolling_tm1')
STRUCTURES=('put','call','double','iron_fly')

class Data(BaseData):
    def __init__(self,path):
        super().__init__(path)
        self.daily={x['day']:dict(x) for x in self.db.execute('SELECT * FROM spot_daily')}
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.has_vendor_flags='vendor_discordance' in tables
        if 'spot_daily_ext' in tables:   # absent in caches built before this release
            for x in self.db.execute('SELECT * FROM spot_daily_ext'):
                if x['day'] in self.daily:self.daily[x['day']].update(dict(x))
        self.rv={};self.trend={}
        previous=[]
        for day in sorted(self.daily):
            yz=yang_zhang_rv(previous,5) or {}
            self.rv[day]={'rv5_close':trailing_rv(previous,'close',5),
                          'rv5_parkinson':trailing_rv(previous,'parkinson',5),
                          'rv5_yz':yz.get('rv'),'gap_share_5d':yz.get('gap_share'),
                          'rv5_1m_range_proxy':yz.get('rv1m_range_proxy'),'rv5_1m_closeclose':yz.get('rv1m_closeclose')}
            self.trend[day]=trend_fields(previous)
            previous.append(self.daily[day])
        # Day keys for every complete observed daily session. 09:45 must exist for signals.
        self.days=sorted(set(self.days)&set(self.daily))   # days a NEW trade can start: needs a 09:45 signal
        self.valuation_days=sorted(set(self.days)|{d for d,x in self.daily.items() if (x.get('minute_bars') or 0)>=1})   # days an OPEN trade is carried
        self.dayset=set(self.valuation_days)
        self.holidays=[]   # gaps in later data are not a calendar; supply published holidays with --holiday-calendar
    def close(self):
        type(self).discordant.cache_clear()
        super().close()
    @lru_cache(30000)
    def discordant(self,day,minute,expiry,strike,cp):
        return self.db.execute('SELECT 1 FROM vendor_discordance WHERE ts=? AND expiry=? AND strike=? AND cp=?',
            (f'{day} {minute}:00',expiry,float(strike),cp)).fetchone() is not None

def prior_short_expiry(sale_expiries):
    return max(x[0] for x in sale_expiries) if sale_expiries else None


def attribute_leg(prev,price,f1,iv1,t1,strike,cp,units,r1=None):
    """One leg's mark change split by forward, vol and time, from the last marked state. Residual is explicit."""
    if not all((prev.get('iv'),prev.get('f'),prev.get('t'),f1,iv1)) or t1<=0:return None
    g=black_greeks(prev['f'],strike,prev['t'],prev['iv'],cp,prev['r'])
    if not g:return None
    dF=f1-prev['f'];dv=iv1-prev['iv']
    r0=prev['r'];r1=r0 if r1 is None else r1
    # Reprice one input at a time, so the steps sum to the model's whole change and little is left unexplained.
    p0=black_forward(prev['f'],strike,prev['t'],prev['iv'],cp,r0);after_time=black_forward(prev['f'],strike,t1,prev['iv'],cp,r0)
    after_forward=black_forward(f1,strike,t1,prev['iv'],cp,r0);after_vol=black_forward(f1,strike,t1,iv1,cp,r0);after_rate=black_forward(f1,strike,t1,iv1,cp,r1)
    if not all(map(math.isfinite,(p0,after_time,after_forward,after_vol,after_rate))):return None
    theta=after_time-p0;delta=g['delta_f']*dF;gamma=after_forward-after_time-delta;vega=g['vega']*dv;vanna_volga=after_vol-after_forward-vega;rate=after_rate-after_vol
    return dict(delta=units*delta,gamma=units*gamma,vega=units*vega,vanna_volga=units*vanna_volga,rate=units*rate,theta=units*theta,
        residual=units*(price-prev['price']-delta-gamma-vega-vanna_volga-rate-theta))


def trend_fields(prior):
    """Market-cycle measures from sessions completed BEFORE the day in question."""
    closes=[x['close'] for x in prior if x.get('close') and x.get('complete_session',1)]
    if not closes:return {}
    last=closes[-1];sma=lambda n:sum(closes[-n:])/n if len(closes)>=n else None
    out={f'spot_to_sma{n}':last/sma(n) if sma(n) else None for n in (50,100,200)}
    out['trend_21d']=last/closes[-21]-1 if len(closes)>=21 else None
    out['pct_below_52w_high']=1-last/max(closes[-252:]) if len(closes)>=252 else None
    return out


def session_time_iv(iv,day,expiry,holidays,minute='09:45'):
    """Same total variance on a 252-session clock, using only a holiday list published in advance."""
    if iv is None:return None
    sessions=int(np.busday_count(np.datetime64(day)+np.timedelta64(1,'D'),np.datetime64(expiry)+np.timedelta64(1,'D'),holidays=holidays))
    h,m=map(int,minute.split(':'));today=(15*60+30-h*60-m)/375
    t_biz=(sessions+today)/252;t_cal=years(expiry,day,minute)
    return iv*math.sqrt(t_cal/t_biz) if t_biz>0 and t_cal>0 else None


def campaign_economics(ledger,long_cost,width_inr):
    """Weekly short cycles from the ledger: gross premium versus what survived the buyback or settlement."""
    cycles={}
    for x in ledger:
        if 'cash_flow' not in x or x.get('role_tag')=='gamma':continue
        key=(x.get('expiry'),x.get('strike'),x.get('cp'))
        c=cycles.get(key)
        if x['event'] in ('initial_short','roll_short_sale'):
            cycles[key]=dict(credit=x['cash_flow'],net=x['cash_flow'],day=x['day'])
        elif c is not None and x.get('units',0)!=0:c['net']+=x['cash_flow']
    gross=net=0.;out=dict(cycles=len(cycles),winning_cycles=0,gross_short_credit=0.,net_short_pnl=0.,gross_payback_cycle=None,
        net_payback_cycle=None,gross_risk_free_cycle=None,net_risk_free_cycle=None,long_cost=long_cost,strike_width_inr=width_inr)
    for n,c in enumerate(sorted(cycles.values(),key=lambda z:z['day']),1):
        gross+=c['credit'];net+=c['net'];out['winning_cycles']+=c['net']>0
        if out['gross_payback_cycle'] is None and gross>=long_cost:out['gross_payback_cycle']=n
        if out['net_payback_cycle'] is None and net>=long_cost:out['net_payback_cycle']=n
        if out['gross_risk_free_cycle'] is None and gross>=long_cost+width_inr:out['gross_risk_free_cycle']=n
        if out['net_risk_free_cycle'] is None and net>=long_cost+width_inr:out['net_risk_free_cycle']=n
    out.update(gross_short_credit=gross,net_short_pnl=net)
    return out


@dataclass
class Leg:
    expiry:str
    strike:float
    cp:str
    units:int  # positive = purchased, negative = written
    role:str


def required_volume(cfg,order_units,lot,base):
    """Vendor-unit volume a bar must show so this order is at most max_participation of it."""
    p=getattr(cfg,'max_participation',None)
    if p is not None and not 0<=p<=1:raise ValueError(f'max_participation must be within 0 and 1, got {p}')
    if not p:return base
    size=order_units if getattr(cfg,'volume_unit','contracts')=='units' else order_units/lot
    return max(base,math.ceil(size/p-1e-9))


def volume_unit_report(db,provider,lot_of,days_per_month=5,rows_per_day=3000):
    """Month by month, spread across each month and so across every lot-size change: is vendor volume a lot multiple?"""
    months={}
    for (day,) in db.execute('SELECT day FROM spot_daily ORDER BY day'):months.setdefault(day[:7],[]).append(day)
    report={}
    for month,days in months.items():
        step=max(1,len(days)//days_per_month);hit=seen=0
        for day in days[::step]:
            for expiry,volume in db.execute('SELECT expiry,volume FROM opt WHERE provider=? AND ts>=? AND ts<? AND volume>0 LIMIT ?',(provider,day+' 00',day+' 99',rows_per_day)):
                size=lot_of(day,expiry)
                if size:seen+=1;hit+=int(volume)%size==0
        if seen:report[month]=dict(rows=seen,lot_multiple_share=hit/seen,unit='units' if hit/seen>=.995 else 'contracts' if hit/seen<=.2 else 'ambiguous')
    return report


def detect_volume_unit(db,provider,lot_of,report=None):
    """'units' or 'contracts' when every month agrees; None when any month is ambiguous or months disagree."""
    report=report if report is not None else volume_unit_report(db,provider,lot_of)
    kinds={x['unit'] for x in report.values()}
    return next(iter(kinds)) if len(kinds)==1 and 'ambiguous' not in kinds else None


def resolve_volume_unit(data,cfg):
    """Fix cfg.volume_unit from the data; refuses to guess when participation depends on it."""
    cfg.volume_unit_source='declared'
    if cfg.volume_unit!='auto':return cfg.volume_unit
    cfg.volume_unit_source='inferred_from_lot_multiples'
    report=volume_unit_report(data.db,cfg.provider,lambda d,e:cfg.lot_master.get(d,e).qty)
    cfg.volume_unit_report=report
    found=detect_volume_unit(data.db,cfg.provider,None,report)
    if found is None and cfg.max_participation:raise SystemExit('Vendor volume unit is ambiguous or changes by month: '+json.dumps(report)+' Pass --volume-unit units or contracts after checking the vendor.')
    cfg.volume_unit=found or 'contracts'
    print('Vendor volume unit:',cfg.volume_unit,flush=True)
    return cfg.volume_unit


def exchange_rate(day,cfg):
    """NSE option charge on premium including IPFT. A fixed --exchange-bps overrides the schedule."""
    if cfg.exchange_bps is not None:return cfg.exchange_bps/10000
    return .000495 if day<'2024-10-01' else .0003553   # 35.03 per lakh + IPFT 50 per crore; from 2026-03-01 35.5299 + 0.01, the same total


def charged_orders(units,lot,cfg):
    """Broker orders billed for one fill. Without a lot size, one order."""
    mode=getattr(cfg,'brokerage_order_mode','one_order_per_leg')
    lots=abs(units)/lot if lot else 1
    if mode=='one_order_per_leg' or not lot:return 1
    if mode=='per_lot_orders':return math.ceil(lots-1e-9)
    if mode=='lots_per_order':return math.ceil(lots/cfg.lots_per_order-1e-9)
    if mode=='fixed_orders':return cfg.fixed_orders
    raise ValueError(f'unknown brokerage order mode {mode}')


def fill_charges(premium,units,day,cfg,lot=None):
    """Line items for one fill, in rupees. All are costs; `total` is what leaves the account beyond the premium."""
    orders=charged_orders(units,lot,cfg);brokerage=cfg.brokerage*orders
    exchange=premium*exchange_rate(day,cfg);sebi=premium*getattr(cfg,'sebi_bps',0.)/10000
    gst=(brokerage+exchange+sebi)*cfg.gst
    rate=cfg.stt if cfg.stt is not None else (.000625 if day<'2024-10-01' else .001 if day<'2026-04-01' else .0015)
    stt=premium*rate if units<0 else 0.;stamp=premium*cfg.stamp if units>0 else 0.
    return dict(charged_orders=orders,brokerage=brokerage,exchange=exchange,sebi=sebi,gst=gst,stt=stt,stamp=stamp,total=brokerage+exchange+sebi+gst+stt+stamp)


def hedge_cost(change,price,day,cfg):
    """Cost of moving the index proxy by `change` units, priced as a NIFTY future when statutory costs are on."""
    notional=abs(change)*price;cost=notional*cfg.hedge_bps/10000
    if change and getattr(cfg,'hedge_statutory',False):
        stt=.000125 if day<'2024-10-01' else .0002 if day<'2026-04-01' else .0005
        exchange=.0000198 if day<'2024-10-01' else .0000183   # includes IPFT of Rs 10 per crore
        cost+=notional*((exchange+.000001)*(1+cfg.gst)+(.00002 if change>0 else stt))+min(cfg.brokerage,notional*.0003)*(1+cfg.gst)
    return cost


def exercise_fee_count(units,lot,cfg):
    """How many times the broker's exercise fee applies to one settled in-the-money leg."""
    basis=getattr(cfg,'exercise_fee_basis','per_leg')
    if basis=='zero':return 0
    if basis=='per_leg' or not lot:return 1
    if basis=='per_contract':return math.ceil(abs(units)/lot-1e-9)
    if basis=='per_order':return charged_orders(units,lot,cfg)
    raise ValueError(f'unknown exercise fee basis {basis}')


def validate_config(a):
    """Settings that would make results meaningless, listed rather than silently applied."""
    bad=[]
    def need(name,low,high=math.inf,optional=False):
        v=getattr(a,name,None)
        if v is None and optional:return
        if v is None or not (low<=v<=high):bad.append(f'{name} must be within {low} and {high}, got {v}')
    for n in ('slippage','brokerage','hedge_bps','sebi_bps','exercise_fee','intrabar_lambda','intrabar_min','min_signal_volume','min_pair_volume','min_fill_volume','min_exit_volume'):
        if hasattr(a,n):need(n,0)
    for n in ('gst','stamp','mark_stress'):
        if hasattr(a,n):need(n,0,1)
    for n in ('stt','exercise_stt'):need(n,0,1,optional=True)
    need('exchange_bps',0,optional=True);need('max_participation',0,1)
    if hasattr(a,'entry_delay_minutes'):
        need('entry_delay_minutes',0,3)   # the cached entry window is five minutes
        if a.entry_delay_minutes and getattr(a,'fill_model','') not in CAUSAL_FILL_MODELS+STRESS_FILL_MODELS:bad.append('entry_delay_minutes needs a next_bar_open model')
    for n in ('lots','lots_per_order','fixed_orders'):
        if hasattr(a,n):need(n,1)
    if getattr(a,'start','')>getattr(a,'end',''):bad.append('from-date is after to-date')
    return bad


def evidence_class(cfg):
    model=getattr(cfg,'fill_model','')
    if model in CAUSAL_FILL_MODELS:return 'causal_ohlc_proxy'
    if model in ADVERSE_FILL_MODELS:return 'ex_post_adverse_stress'
    return 'ex_post_traded_bar_filter' if model in TRADED_BAR_FILL_MODELS else 'conditional_ohlc_proxy'


def eod_spot(data,day):
    """(index level, source) for marks on a day. A flagged estimate beats silently omitting the day."""
    for minute in ('15:29','15:16'):
        q=data.spot(day,minute)
        if is_quote(q):return float(q['close']),'minute_'+minute.replace(':','')
    close=(data.daily.get(day) or {}).get('close')
    if close:return float(close),'daily_close'
    q=data.spot(day,'09:45')
    return (float(q['close']),'minute_0945') if is_quote(q) else (None,'missing')


def delay_mode(cfg):
    """False: same-minute open. True: next printed minute's open (needs that minute's volume). 'close': the qualifying minute's close.
    'next_open': eligible on a finished minute's volume, then the next printed minute's open within the window; the only mode whose gate precedes its fill."""
    model=getattr(cfg,'fill_model','same_minute_first_print')
    if model in TRADED_BAR_FILL_MODELS:return 'next_open_traded'
    if model in CAUSAL_FILL_MODELS+STRESS_FILL_MODELS:return 'next_open'
    return False if model=='same_minute_first_print' else 'close' if model=='qualified_minute_close' else True


NEXT_OPEN_MODES=('next_open','next_open_traded')


def quote_fill(data,provider,day,expiry,strike,cp,minutes=ENTRY_WINDOWS,minimum=1,delayed=False,skip=0):
    """First liquid printed candle OPEN after the signal. Not genuine market VWAP.

    delayed: the first print only becomes known at that minute's end, so fill from a LATER printed minute.
    """
    seen=None;waited=0
    for minute in minutes:
        q=data.opt(provider,day,minute,expiry,strike,cp)
        if delayed in NEXT_OPEN_MODES:
            if seen:
                waited+=1
                if waited>skip and is_quote(q,'open',int(delayed=='next_open_traded')):return known_volume_fill(q,seen_quote,minute,seen)   # outside the ex-post filter, that bar's later volume cannot decide an opening-price fill
            if not seen and is_quote(q,'close',minimum):seen,seen_quote=minute,q   # the order then rests until the next print in the window
            continue
        if delayed=='close':
            if is_quote(q,'close',minimum):return fill_tuple(q,minute,minute,'qualified_minute_close','close')
            continue
        if not is_quote(q,'open',minimum):continue
        if not delayed:return fill_tuple(q,minute)
        if seen:return fill_tuple(q,minute,seen,'next_printed_minute')
        seen=minute
    return None


def fill_tuple(q,minute,observed=None,assumption='same_minute_first_print',field='open',fill_bar=None):
    return (float(q[field]),minute,int(q.get('volume') or 0),q.get('source_id'),q.get('source_row'),q.get('high'),q.get('low'),observed or minute,assumption,field,fill_bar)


def known_volume_fill(q,eligible,minute,eligible_minute):
    """Next-minute open priced and costed from the finished minute: volume and range printed after the fill stay out.
    fill_bar keeps that later data for the ex-post stress and the zero-volume diagnostic only."""
    fill=list(fill_tuple(q,minute,eligible_minute,'next_bar_open_after_eligible_minute','open',dict(volume=int(q.get('volume') or 0),high=q.get('high'),low=q.get('low'))))
    fill[2]=int(eligible.get('volume') or 0);fill[5]=eligible.get('high');fill[6]=eligible.get('low')
    return tuple(fill)


def execute_fills(data,cfg,day,contracts,minutes=ENTRY_WINDOWS,minimum=1):
    """Leg fills under the configured sync and fill model, or None. Shared with the coverage audit."""
    delayed=delay_mode(cfg);skip=getattr(cfg,'entry_delay_minutes',0) if minutes==ENTRY_WINDOWS else 0
    if getattr(cfg,'fill_sync','independent')=='synchronized':
        return synchronized_fills(data,cfg.provider,day,contracts,minutes,minimum,delayed,skip)
    fills=[quote_fill(data,cfg.provider,day,e,s,c,minutes,minimum,delayed,skip) for e,s,c in contracts]
    return fills if all(fills) else None


def synchronized_fills(data,provider,day,contracts,minutes=ENTRY_WINDOWS,minimum=1,delayed=False,skip=0):
    """First minute in which EVERY leg printed; all legs fill from that same minute.

    Removes the spot drift that independent first-print fills build into a spread.
    Returns None when no common minute exists; never synthesises a shifted price.
    delayed: the common print is only known once that minute ends, so fill at the next common minute.
    """
    seen=None;waited=0;field='close' if delayed=='close' else 'open'
    for minute in minutes:
        if delayed in NEXT_OPEN_MODES:
            quotes=[data.opt(provider,day,minute,e,s,c) for e,s,c in contracts]
            waited+=bool(seen)
            if seen and waited>skip and all(is_quote(q,'open',int(delayed=='next_open_traded')) for q in quotes):return [known_volume_fill(q,p,minute,seen) for q,p in zip(quotes,seen_quotes)]
            if not seen and all(is_quote(q,'close',minimum) for q in quotes):seen,seen_quotes=minute,quotes
            continue
        out=[]
        for expiry,strike,cp in contracts:
            q=data.opt(provider,day,minute,expiry,strike,cp)
            if not is_quote(q,field,minimum):break
            out.append(q)
        else:
            if delayed=='close':return [fill_tuple(q,minute,minute,'qualified_minute_close','close') for q in out]
            if not delayed:return [fill_tuple(q,minute) for q in out]
            if seen:return [fill_tuple(q,minute,seen,'next_common_printed_minute') for q in out]
            seen=minute
    return None


def exercise_stt_rate(day,cfg):
    """STT on intrinsic value of an option that expires ITM; charged to the BUYER only."""
    override=getattr(cfg,'exercise_stt',None)
    if override is not None:return override
    return .00125 if day<'2026-04-01' else .0015


CAS_AUCTION_WINDOW=('2026-08-03','2026-12-07')  # index expiry used auction closes; see CHANGES_V4.md


def settlement_reference(data,cfg,expiry,counter):
    """(underlying, ledger event, evidence, is_official) or None. Only a verified CSV is official."""
    master=getattr(cfg,'settlement_master',None)
    official=master.get(expiry) if master else None
    if official:return official[0],'verified_official_settlement',official[1],True
    if getattr(cfg,'settlement_proxy','close_1529')=='twap_1500_1529':
        if CAS_AUCTION_WINDOW[0]<=expiry<CAS_AUCTION_WINDOW[1]:counter['settlement_proxy_cas_window']+=1
        else:
            row=data.daily.get(expiry) or {}
            if row.get('twap_1500_1529') and (row.get('twap_minutes') or 0)>=25:
                return row['twap_1500_1529'],'settlement_proxy_TWAP_NOT_NSE_OFFICIAL','SPOT_1500_1529_TWAP_PROXY',False
            counter['settlement_twap_unavailable']+=1
    sp=data.spot(expiry,'15:29') or data.spot(expiry,'15:16')
    if is_quote(sp):return sp['close'],'settlement_proxy_NOT_NSE_OFFICIAL','INTRADAY_SPOT_CLOSE_PROXY',False
    close=(data.daily.get(expiry) or {}).get('close')
    if close:return close,'settlement_proxy_NOT_NSE_OFFICIAL','DAILY_CLOSE_PROXY',False   # still the expiry session's own level
    return None


def fill_flow(px,units,day,cfg,expiry,volume,high=None,low=None,lot=None):
    return fill_detail(px,units,day,cfg,expiry,volume,high,low,lot)[:3]


def fill_detail(px,units,day,cfg,expiry,volume,high=None,low=None,lot=None):
    tenor=max(1,dte(day,expiry))
    base=cfg.slippage
    if getattr(cfg,'fill_model','') in ('next_minute_adverse_range',)+ADVERSE_FILL_MODELS and high is not None and low is not None:
        px=float(high) if units>0 else float(low)   # worst price the bar printed, before slippage
    slip=base*(max(tenor,7)/7)**.5*(1+2/max(1,volume)**.5) if cfg.slippage_mode in ('tenor_volume','intrabar_range') else base
    if cfg.slippage_mode=='intrabar_range':
        # Range-based cost is a floor-raiser: a one-print bar has no range but is the LEAST liquid.
        side=max(0.,(high-px) if units>0 else (px-low)) if high is not None and low is not None else 0.
        slip=max(slip,min(side,cfg.intrabar_lambda*side+cfg.intrabar_min))
    actual=max(.01,px+(slip if units>0 else -slip))
    premium=abs(units)*actual
    charges=fill_charges(premium,units,day,cfg,lot)
    return -units*actual-charges['total'],actual,slip,charges


def estimate_mark(data,cfg,day,leg,history,minute='15:29'):
    """Available-as-of-today mark, never determines entry eligibility.

    A: same-minute traded quote; B: non-traded published candle; C: bounded
    same-expiry smile interpolation; D: forward-shifted prior sigma (stale);
    E: conservative intrinsic/zero fallback. E always carries a risk flag.
    Returns (price, lower, upper, method, age_days).
    """
    provider=cfg.provider
    s=eod_spot(data,day)[0]
    q=data.opt(provider,day,minute,leg.expiry,leg.strike,leg.cp)
    if is_quote(q,volume=1):
        p=float(q['close'])
        return p,max(0,p*(1-cfg.mark_stress)),p*(1+cfg.mark_stress),'traded_bar',0
    # A zero-volume candle is a vendor indication, not executable evidence.
    if is_quote(q,volume=0):
        p=float(q['close'])
        return p,max(0,p*(1-cfg.mark_stress)),p*(1+cfg.mark_stress),'zero_volume_quote',0
    t=years(leg.expiry,day,minute)
    f_info=data.forward(provider,day,minute,leg.expiry,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
    f=f_info.get('forward')
    r=f_info.get('rate') if f_info.get('rate') is not None else cfg.rate
    if f and t>0:
        strikes=[]
        for (k,cp),row in data.chain(provider,day,minute,leg.expiry).items():
            if cp!=leg.cp or not is_quote(row,volume=1):continue
            g=data.greek(provider,day,minute,leg.expiry,k,cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
            if g and g['iv'] and abs(k-leg.strike)<=cfg.max_interp_gap:
                strikes.append((k,g['iv']**2*t))
        low=max((x for x in strikes if x[0]<leg.strike),default=None,key=lambda x:x[0])
        high=min((x for x in strikes if x[0]>leg.strike),default=None,key=lambda x:x[0])
        if low and high and high[0]-low[0]<=cfg.max_interp_gap:
            w=low[1]+(high[1]-low[1])*(leg.strike-low[0])/(high[0]-low[0])
            if w>0:
                p=black_forward(f,leg.strike,t,math.sqrt(w/t),leg.cp,r)
                if math.isfinite(p):
                    return p,max(0,p*(1-cfg.mark_stress)),p*(1+cfg.mark_stress),'smile_interpolated',0
    key=(leg.expiry,leg.strike,leg.cp)
    if key in history and s:
        prev=history[key]
        age=(dt.date.fromisoformat(day)-dt.date.fromisoformat(prev['day'])).days
        if age<=cfg.max_mark_age_days and t>0 and prev.get('iv'):
            # Sticky-IV valuation using current spot and previous forward basis.
            base=prev['f']/prev['spot'] if prev.get('f') and prev.get('spot') else 1.
            p=black_forward(s*base,leg.strike,t,prev['iv'],leg.cp,r)
            if math.isfinite(p):
                return p,max(0,p*(1-cfg.mark_stress*2)),p*(1+cfg.mark_stress*2),'prior_iv_model',age
    intrinsic=max(leg.strike-s,0) if leg.cp=='PE' and s else max(s-leg.strike,0) if s else 0.
    # Final fallback deliberately stress-penalized; preserve position and flag.
    prior=history.get(key,{}).get('price',intrinsic)
    p=max(intrinsic,prior if s else 0.)
    return p,max(0,intrinsic),max(p*2,intrinsic+cfg.mark_stress*max(s or 0,1)),'unreliable_conservative',999


def structure_strikes(cand,structure,cfg):
    """[(strike,cp)] for the structure's short strikes, or None when a double's wings cross."""
    k=cand['strike']
    if structure=='put':return [(float(k),'PE')]
    if structure=='call':return [(float(k),'CE')]
    if structure=='iron_fly':return [(float(k),'PE'),(float(k),'CE')]
    if structure=='double':
        # Symmetric distance in log-forward-moneyness; not exactly delta-neutral.
        f=cand['short_f'];off=cfg.double_z*cand['iv_short']*math.sqrt(max(years(cand['short_expiry'],cand['day'],'09:45'),1e-6))
        kp=round(f*math.exp(-off)/100)*100;kc=round(f*math.exp(off)/100)*100
        return [(float(kp),'PE'),(float(kc),'CE')] if kp<kc else None
    raise ValueError('Unknown structure')


def fly_width(cand,cfg):
    """Iron-fly wing distance: wing_z expected moves to the short expiry, on a 100-point strike, at least one strike."""
    move=cand['iv_short']*cand['short_f']*math.sqrt(max(years(cand['short_expiry'],cand['day'],'09:45'),1e-6))
    return float(max(100,round(getattr(cfg,'fly_wing_z',1.5)*move/100)*100))


def sessions_between(session,expiry,holidays=()):
    """Weekdays strictly between session and expiry, less published holidays. Zero means session is the last one before expiry."""
    return int(np.busday_count(np.datetime64(session)+np.timedelta64(1,'D'),np.datetime64(expiry),holidays=list(holidays)))


def scheduled_exit_day(cand,policy,cfg):
    """Latest day the rule itself plans to be open. Known at entry, so filtering on it cannot select on outcomes."""
    if policy in WEEKLY_FAMILY:return cand['short_expiry']
    last_long=dt.date.fromisoformat(cand['long_expiry'])-dt.timedelta(days=30)+dt.timedelta(days=getattr(cfg,'max_short_dte',8))
    return max(cand['short_expiry'],min(last_long.isoformat(),cand['long_expiry']))


def _digest(payload):
    return hashlib.sha256(json.dumps(payload,default=str,sort_keys=True).encode()).hexdigest()[:32]


def contract_digest(result):
    """Which contracts, dated and signed. Prices do not enter."""
    return _digest(sorted((str(x.get('day')),str(x.get('expiry')),x.get('strike'),str(x.get('cp')),x.get('units')) for x in result['ledger'] if 'charges' in x))


def path_digest(result):
    """Cash-flow identity: every dated signed amount, option or hedge. Event labels and status words do not enter."""
    rows=[(str(x.get('day')),str(x.get('fill_time') or x.get('time')),str(x.get('expiry')),x.get('strike'),str(x.get('cp')),x.get('units'),round(float(x.get('cash_flow',0.)),4),round(float(x.get('hedge_cash',0.)),4))
          for x in result['ledger'] if 'cash_flow' in x or 'hedge_cash' in x]
    return _digest(sorted(rows,key=str))


def mtm_digest(marks):
    """Valuation identity: the daily mark path and its bounds. Identical paths score identically, so this decides what counts as one trial."""
    return _digest([(m['day'],round(m['pnl'],4),round(m['lower'],4),round(m['upper'],4),round(m['capital_scenario'],2)) for m in marks])


def event_instant(text,date_only_next_day):
    """'YYYY-MM-DD HH:MM' in IST. A bare date has no time, so as a first-known stamp it counts only from the next day."""
    text=text.strip()
    if len(text)==10:
        day=dt.date.fromisoformat(text)
        return (day+dt.timedelta(days=1)).isoformat()+' 00:00' if date_only_next_day else day.isoformat()+' 00:00'
    when=dt.datetime.fromisoformat(text)
    when=when.replace(tzinfo=IST) if when.tzinfo is None else when.astimezone(IST)
    return when.strftime('%Y-%m-%d %H:%M')


def parse_event_line(line):
    """event_date,first_known_at[,superseded_at[,source]]. One row per vintage: a rescheduled event keeps its old row, closed by superseded_at."""
    parts=[x.strip() for x in line.split(',')]
    if len(parts)<2 or not parts[1]:raise ValueError(f'--event-calendar needs event_date,first_known_at on every line: {line}')
    dt.date.fromisoformat(parts[0])
    return parts[0],event_instant(parts[1],True),event_instant(parts[2],False) if len(parts)>2 and parts[2] else '9999-12-31 00:00'


def holiday_audit(daily,holidays,full_bars=350):
    """Disagreements between a holiday file and the sessions in the cache, over the cache's span. Post-run validation of an input, never a signal."""
    days=sorted(daily);listed={str(x) for x in holidays}
    if not days:return dict(listed_but_full_session=[],unlisted_weekday_missing=[])
    span=[(dt.date.fromisoformat(days[0])+dt.timedelta(n)).isoformat() for n in range((dt.date.fromisoformat(days[-1])-dt.date.fromisoformat(days[0])).days+1)]
    return dict(listed_but_full_session=[d for d in days if d in listed and (daily[d].get('minute_bars') or 0)>=full_bars],
        unlisted_weekday_missing=[d for d in span if dt.date.fromisoformat(d).weekday()<5 and d not in daily and d not in listed])


def economic_key(cand,policy,hedge,structure,cfg):
    """Everything that decides a simulated path. Parameter labels (rounding step, offset) that land on the same contracts do not."""
    if structure=='iron_fly':
        return (cand['day'],policy,hedge,structure,cand['qty'],cand['short_expiry'],float(cand['strike']),fly_width(cand,cfg))
    strikes=structure_strikes(cand,structure,cfg) or [(float(cand['strike']),'crossed')]
    long_strike=float(cand.get('long_strike',cand['strike'])) if structure=='put' else None
    legs=tuple(sorted(x for s,cp in strikes for x in ((cand['long_expiry'],long_strike if long_strike is not None else s,cp),(cand['short_expiry'],s,cp))))
    return (cand['day'],policy,hedge,structure,cand['qty'],legs,cand['target_tenor'] if policy=='rolling_recenter' else None)


SIMULATED_FIELDS={'iv_short','iv_long','iv_ratio','forward_iv','forward_iv_ratio','var_carry_daily','structure_iv_basis'}
TENOR_IVS={f'iv_{t}' for t in (5,15,30,45,60,75,90,120)}


def simulate_once(memo,data,cfg,cand,policy,hedge,structure):
    """(outcome, reason, reused). One simulation per economic key; each alias keeps its own labels and signal features."""
    key=economic_key(cand,policy,hedge,structure,cfg);reused=key in memo
    if not reused:memo[key]=simulate(data,cfg,cand,policy,hedge,structure)
    outcome,why=memo[key]
    if not outcome or not reused:return outcome,why,reused
    result,marks=outcome
    keep=SIMULATED_FIELDS|(TENOR_IVS if structure not in ('put','iron_fly') else set())
    return (dict(result,**{k:v for k,v in cand.items() if k not in keep}),marks),why,True


def simulate(data,cfg,cand,policy='weekly',hedge='none',structure='put'):
    if structure=='double' and policy not in ('weekly','p25_s50','p50_s100')+TM1_EXITS:
        raise ValueError('Double-calendar rolling/IV convergence unsupported; reject before entry')
    if structure=='iron_fly' and (policy not in ('weekly','p25_s50','p50_s100')+TM1_EXITS or hedge!='none'):
        raise ValueError('Iron fly runs unhedged on a one-cycle exit only')
    if hedge=='gamma_reduced_delta_proxy' and structure!='put':
        raise ValueError('Gamma reduction currently available only for puts')
    provider=cfg.provider;day=cand['day'];long_exp=cand['long_expiry'];lot=cand['qty'];qty=lot*getattr(cfg,'lots',1)
    short_exp=cand['short_expiry'];k=cand['strike']
    if policy in TM1_EXITS+('rolling_tm1',) and sessions_between(day,short_exp,data.holidays)==0:return None,'tm1_entry_on_last_session'   # a same-day round trip is a different strategy, and holding on would be the expiry-day trade this policy exists to avoid
    spot0=data.spot(day,'09:45')
    if not is_quote(spot0):return None,'missing_entry_spot'
    longlegs=[];shortlegs=[]
    strikes=structure_strikes(cand,structure,cfg)
    if strikes is None:return None,'bad_double_strikes'
    legpairs=[]
    sync=getattr(cfg,'fill_sync','independent')=='synchronized'
    long_strike=float(cand.get('long_strike',k)) if structure=='put' else None
    long_k=lambda s:long_strike if long_strike is not None else s
    contracts=[c for strike,cp in strikes for c in ((long_exp,long_k(strike),cp),(short_exp,strike,cp))]
    fly=None
    if structure=='iron_fly':
        fly=fly_width(cand,cfg);long_strike=None   # wings replace the long-dated leg
        fly_legs=[(float(k),'PE',-qty,'short'),(float(k),'CE',-qty,'short'),(float(k)-fly,'PE',qty,'long'),(float(k)+fly,'CE',qty,'long')]
        contracts=[(short_exp,s,c) for s,c,_,_ in fly_legs]
    if hedge=='gamma_reduced_delta_proxy':contracts.append((short_exp,float(k),'CE'))   # a third entry leg on the same clock
    delayed=delay_mode(cfg);price_field='close' if delayed=='close' else 'open'
    fill_floor=required_volume(cfg,qty,lot,cfg.min_fill_volume)
    skip=getattr(cfg,'entry_delay_minutes',0)
    common=synchronized_fills(data,provider,day,contracts,minimum=fill_floor,delayed=delayed,skip=skip) if sync else None
    if fly:
        if sync and common is None:return None,'no_synchronized_entry_minute'
        fills=common if sync else [quote_fill(data,provider,day,short_exp,s,c,minimum=fill_floor,delayed=delayed,skip=skip) for s,c,_,_ in fly_legs]
        if not all(fills) or not all(is_quote(data.opt(provider,day,'09:45',short_exp,s,c),volume=cfg.min_signal_volume) for s,c,_,_ in fly_legs):return None,'missing_liquid_entry_leg'
        legpairs=[(Leg(short_exp,s,c,u,r),f) for (s,c,u,r),f in zip(fly_legs,fills)]
    for n,(strike,cp) in enumerate([] if fly else strikes):
        a=quote_fill(data,provider,day,long_exp,long_k(strike),cp,minimum=fill_floor,delayed=delayed,skip=skip)
        b=quote_fill(data,provider,day,short_exp,strike,cp,minimum=fill_floor,delayed=delayed,skip=skip)
        if sync and common is None:return None,'no_synchronized_entry_minute' if a and b else 'missing_liquid_entry_leg'
        if sync:a,b=common[2*n],common[2*n+1]
        sq=data.opt(provider,day,'09:45',short_exp,strike,cp)
        lq=data.opt(provider,day,'09:45',long_exp,long_k(strike),cp)
        if not a or not b or not is_quote(sq,volume=cfg.min_signal_volume) or not is_quote(lq,volume=cfg.min_signal_volume):
            return None,'missing_liquid_entry_leg'
        legpairs.extend([(Leg(long_exp,long_k(strike),cp,qty,'long'),a),
                         (Leg(short_exp,strike,cp,-qty,'short'),b)])
    if getattr(cfg,'exclude_vendor_discordant',False):
        for leg,fill in legpairs:
            if data.discordant(day,fill[1],leg.expiry,leg.strike,leg.cp) or data.discordant(day,'09:45',leg.expiry,leg.strike,leg.cp):
                return None,'vendor_discordant_entry'
    entry_minutes=[fill[1] for _,fill in legpairs]
    # Candidate signal must match the option types actually being traded.
    iv_pairs=[]
    for strike,cp in ([] if fly else strikes):
        g1=data.greek(provider,day,'09:45',short_exp,strike,cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        g2=data.greek(provider,day,'09:45',long_exp,long_k(strike),cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        if not g1 or not g2:return None,'missing_structure_iv'
        iv_pairs.append((g1['iv'],g2['iv']))
    cand=dict(cand)
    t_short=years(short_exp,day,'09:45');t_long=years(long_exp,day,'09:45') if long_exp else None
    if not fly:
        cand['iv_short']=sum(x[0] for x in iv_pairs)/len(iv_pairs)
        cand['iv_long']=sum(x[1] for x in iv_pairs)/len(iv_pairs)
        cand['iv_ratio']=cand['iv_short']/cand['iv_long']
        wings=[forward_variance(s,l,t_short,t_long) for s,l in iv_pairs] if long_strike in (None,float(k)) else [None]
        cand['forward_iv']=sum(wings)/len(wings) if all(wings) else None   # per wing, then averaged: two strikes have no shared forward variance
        cand['structure_iv_basis']='wing_average_descriptive' if structure=='double' else 'cross_strike_term_skew_ratio' if long_strike not in (None,float(k)) else 'single_structure'
        cand['forward_iv_ratio']=cand['iv_short']/cand['forward_iv'] if cand['forward_iv'] else None
        cand['var_carry_daily']=(cand['iv_short']**2-cand['iv_long']**2)/max(365*t_short,1e-6)
    else:
        cand['structure_iv_basis']='atm_term_curve'
        cand['var_carry_daily']=(cand['iv_short']**2-cand['iv_30']**2)/max(365*t_short,1e-6) if cand.get('iv_30') else None   # short variance against the at-the-money 30-day level
    if structure not in ('put','iron_fly'):
        # Fixed-delta forward tenor curve is not CE/double calibrated here.
        for tenor in (5,15,30,45,60,75,90,120):cand[f'iv_{tenor}']=None
    exposure={'delta':0.,'gamma':0.,'vega':0.};valid_exposures=True
    for leg,_ in legpairs:
        gr=data.greek(provider,day,'09:45',leg.expiry,leg.strike,leg.cp,
                      cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        if not gr:valid_exposures=False;break
        exposure['delta']+=leg.units*gr['delta_proxy']
        exposure['gamma']+=leg.units*gr['gamma_proxy']
        exposure['vega']+=leg.units*gr['vega']
    if not valid_exposures:exposure={'delta':None,'gamma':None,'vega':None}
    # Decided from the 09:45 signal quotes: a later fill price cannot choose whether the order existed.
    signal_debit=0.
    for leg,_ in legpairs:signal_debit+=leg.units*float(data.opt(provider,day,'09:45',leg.expiry,leg.strike,leg.cp)['close'])
    if fly:
        if signal_debit>=-cfg.min_debit:return None,'iron_fly_no_credit'
    elif signal_debit<=cfg.min_debit:return None,'nonpositive_debit'
    debit=signal_debit
    positions={};ledger=[];marks=[];history={}
    cash=0.;hedge_units=0.;hedge_pnl=0.;last_spot=None;hedge_base=0.
    proxy_used=False;unresolved_reasons=[];short_sales=0;pending_exit=False
    exit_first_attempt=None;exit_lag_sessions=0;forced_exit=False
    official_settlements=0;gamma_reduction_pct=None
    entry_delta_proxy=None
    sale_expiries=set();initial_cash_cost=0.;counter=Counter();campaign_events=[]
    attribution=Counter()
    roll_decisions=[];roll_wait_until=None;roll_mode=getattr(cfg,'roll_signal_policy','unconditional')
    def roll_gate(session,short_expiry,short_strike,long_expiry,long_strike,cp,minute='09:45'):
        """Record this week's 09:45 relative IV; decide whether the new short may be sold."""
        nonlocal roll_wait_until
        args=(cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        gs=data.greek(provider,session,minute,short_expiry,short_strike,cp,*args)
        gl=data.greek(provider,session,minute,long_expiry,long_strike,cp,*args)
        ratio=gs['iv']/gl['iv'] if gs and gl and gs.get('iv') and gl.get('iv') else None
        allowed=roll_mode=='unconditional' or (ratio is not None and cfg.roll_iv_min<=ratio<cfg.roll_iv_max)
        roll_decisions.append(dict(day=session,short_expiry=short_expiry,short_strike=short_strike,long_expiry=long_expiry,
            long_strike=long_strike,iv_ratio=ratio,policy=roll_mode,decision='sell' if allowed else 'skip'))
        if not allowed:
            counter['roll_signal_skip']+=1
            if roll_mode=='require_ratio':roll_wait_until=short_expiry   # this week is forgone
        return allowed
    def transact(leg,fill,trade_day,label,signal_time=None):
        nonlocal cash,short_sales,initial_cash_cost
        px,minute,vol,source_id,source_row,bar_high,bar_low,observed_minute,assumption,fill_field,fill_bar=fill
        if fill_bar and getattr(cfg,'fill_model','') in ADVERSE_FILL_MODELS:bar_high,bar_low=fill_bar['high'],fill_bar['low']
        zero_volume=bool(fill_bar and not fill_bar['volume'])
        if zero_volume:counter['fills_on_zero_volume_bar']+=1
        contracts_traded=vol/lot if getattr(cfg,'volume_unit','contracts')=='units' else vol   # one slippage scale across vendors
        amount,executed,slip,charges=fill_detail(px,leg.units,trade_day,cfg,leg.expiry,contracts_traded,bar_high,bar_low,lot)
        cash+=amount
        key=(leg.expiry,leg.strike,leg.cp)
        if key in positions:
            positions[key].units+=leg.units
            if positions[key].units==0:del positions[key]
        else:positions[key]=Leg(leg.expiry,leg.strike,leg.cp,leg.units,leg.role)
        if leg.role=='short' and leg.units<0:
            short_sales+=1;sale_expiries.add((leg.expiry,leg.strike,leg.cp))
        if trade_day==day:initial_cash_cost+=amount
        ledger.append(dict(event=label,day=trade_day,time=minute,expiry=leg.expiry,strike=leg.strike,
            cp=leg.cp,units=leg.units,reference_price=px,mode='liquid_bar_proxy',volume=vol,charges=charges,evidence_class=evidence_class(cfg),liquidity_asof=observed_minute,fill_bar_zero_volume=zero_volume,role_tag=leg.role,
            observed_bar_minute=observed_minute,fill_assumption=assumption,signal_time=signal_time,order_time=signal_time,
            fill_time=minute,price_evidence_time=minute,price_field=fill_field,executable_claim=False,
            executed_proxy=executed,slippage=slip,cash_flow=amount,
            source_file_id=source_id,source_row_index=source_row))
    for leg,fill in legpairs:transact(leg,fill,day,'initial_'+leg.role,'09:45')
    if hedge=='gamma_reduced_delta_proxy':
        # Buy an additional short-expiry call; this only reduces residual gamma
        # when original put calendar is negative-gamma, never promises neutral.
        gsum=0.
        for leg,_ in legpairs:
            g=data.greek(provider,day,'09:45',leg.expiry,leg.strike,leg.cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
            if not g:return None,'missing_gamma_greeks'
            gsum+=leg.units*g['gamma_proxy']
        addon=data.greek(provider,day,'09:45',short_exp,k,'CE',cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
        extra=common[-1] if sync and common else quote_fill(data,provider,day,short_exp,k,'CE',ENTRY_WINDOWS,fill_floor,delayed,skip)
        if not addon or not extra or addon['gamma_proxy']<=0 or gsum>=0:
            return None,'unavailable_gamma_reduction'
        lots=min(cfg.max_gamma_lots,max(1,round(-gsum/(qty*addon['gamma_proxy']))))
        if extra[2]<required_volume(cfg,lots*qty,lot,cfg.min_fill_volume):return None,'gamma_addon_exceeds_participation'
        before=abs(gsum)
        after=abs(gsum+lots*qty*addon['gamma_proxy'])
        gamma_reduction_pct=100*(1-after/max(before,1e-9))
        if after>=before:return None,'gamma_addon_not_beneficial'
        transact(Leg(short_exp,k,'CE',lots*qty,'gamma'),extra,day,'gamma_reduction_option_buy','09:45')
        if exposure['gamma'] is not None:
            exposure['gamma']+=lots*qty*addon['gamma_proxy']
            exposure['delta']+=lots*qty*addon['delta_proxy']
            exposure['vega']+=lots*qty*addon['vega']
    # Static index delta hedge is *not tradable*: track theoretical cash flows separately.
    if hedge!='none':
        greeks=[]
        for leg,_ in legpairs:
            g=data.greek(provider,day,'09:45',leg.expiry,leg.strike,leg.cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
            if not g:return None,'missing_entry_hedge_greeks'
            greeks.append((leg,g))
        entry_delta_proxy=sum(leg.units*g['delta_proxy'] for leg,g in greeks)
        if hedge=='gamma_reduced_delta_proxy':entry_delta_proxy+=lots*qty*addon['delta_proxy']
        hedge_units=-entry_delta_proxy
        proxy_used=True
        hedge_minute=max(entry_minutes)   # the hedge opens when the spread does, never before
        hs=data.spot(day,hedge_minute)
        if not is_quote(hs,price_field):return None,'missing_hedge_spot_at_fill'
        last_spot=float(hs[price_field])
        cost=hedge_cost(hedge_units,last_spot,day,cfg);cash-=cost
        ledger.append(dict(event='index_delta_proxy_open',day=day,time=hedge_minute,units=hedge_units,spot=last_spot,mode='UNTRADEABLE_PROXY',hedge_cash=-cost))
    debit=max(abs(cash) if fly else -cash,cfg.min_debit)   # for a fly, the credit received; stops measure against everything committed at entry: every leg, the gamma add-on, the hedge cost
    session_open=lambda s:data.entry_session_eligible(s,getattr(cfg,'allow_special_sessions',False))
    exit_window=lambda qualifying,fill:(qualifying,fill) if delayed is True or delayed in NEXT_OPEN_MODES else (qualifying,)
    def flatten_hedge(session):
        """Close the index proxy when the last option leg closes, not at the day-end mark."""
        nonlocal hedge_units,hedge_pnl,last_spot,cash
        today=[x for x in ledger if x.get('day')==session and 'cash_flow' in x]
        minute=max((x['fill_time'] for x in today if x.get('fill_time')),default=None)
        field=price_field
        if minute is None or any('settlement' in x['event'] for x in today):minute,field='15:29','close'
        for candidate in [m for m in data.meta.get('minutes',minute).split(',') if m>=minute] or [minute]:
            sp=data.spot(session,candidate)
            if is_quote(sp,field if candidate==minute else 'close'):
                price=float(sp[field if candidate==minute else 'close'])
                move=hedge_units*(price-last_spot);cost=hedge_cost(-hedge_units,price,session,cfg);hedge_pnl+=move;cash-=cost
                ledger.append(dict(event='index_delta_proxy_close',day=session,time=candidate,units=-hedge_units,spot=price,mode='UNTRADEABLE_PROXY',hedge_cash=move-cost,
                    signal_time=minute,order_time=minute,fill_time=candidate,price_evidence_time=candidate,price_field=field if candidate==minute else 'close',delayed_spot=candidate!=minute))
                hedge_units=0.;last_spot=price;return
        counter['hedge_close_spot_missing']+=1
    def force_exit_at_mark(session):
        """Lag cap reached on a long left without its short: book the remaining longs at a modelled 15:29 price.
        Not a fill. A same-day printed close less the haircut; anything older takes the mark's lower bound."""
        nonlocal cash
        for key,leg in list(positions.items()):
            px,lo,_,method,_age=estimate_mark(data,cfg,session,leg,history)
            ref=px*(1-getattr(cfg,'forced_exit_haircut',.02)) if method in ('traded_bar','zero_volume_quote') else lo
            amount,executed,slip,charges=fill_detail(ref,-leg.units,session,cfg,leg.expiry,1,None,None,lot)
            cash+=amount;del positions[key]
            ledger.append(dict(event='forced_exit_lag_cap',day=session,time='15:29',expiry=leg.expiry,strike=leg.strike,cp=leg.cp,units=-leg.units,
                reference_price=ref,mode='model_mark',volume=0,charges=charges,evidence_class='model_mark',liquidity_asof=None,fill_bar_zero_volume=False,role_tag='exit',
                observed_bar_minute=None,fill_assumption='lag_cap_model_mark',signal_time='15:29',order_time='15:29',fill_time='15:29',price_evidence_time='15:29',
                price_field='mark_'+method,executable_claim=False,executed_proxy=executed,slippage=slip,cash_flow=amount,source_file_id=None,source_row_index=None))
        counter['forced_exit_lag_cap']+=1
    def close_legs(session,windows,label,signal_time,allow_expiry=False):
        """Close what can be closed. A long is never released while a short it protects stays open."""
        live=sorted((x for x in positions.values() if session<x.expiry or (allow_expiry and session==x.expiry)),key=lambda x:x.units)
        if not live:return
        contracts_=[(x.expiry,x.strike,x.cp) for x in live]
        floor=required_volume(cfg,max(abs(x.units) for x in live),lot,cfg.min_exit_volume)
        together=synchronized_fills(data,provider,session,contracts_,windows,floor,delayed) if sync and len(live)>1 else None
        if together:
            for leg,fill in zip(live,together):transact(Leg(leg.expiry,leg.strike,leg.cp,-leg.units,'exit'),fill,session,label,signal_time)
            return
        for leg in live:
            if leg.units>0 and any(x.units<0 for x in positions.values()):continue
            fill=quote_fill(data,provider,session,leg.expiry,leg.strike,leg.cp,windows,required_volume(cfg,abs(leg.units),lot,cfg.min_exit_volume),delayed)
            if fill:transact(Leg(leg.expiry,leg.strike,leg.cp,-leg.units,'exit'),fill,session,label,signal_time)
    def sell_atm_weekly(session,signal,windows):
        """Sell the nearest-the-money weekly above the long strike, as the guidebook's rolling step does."""
        nonlocal long_exp
        spot_now=data.spot(session,signal);long_leg=next((x for x in positions.values() if x.units>0 and x.role=='long'),None)
        if not is_quote(spot_now) or not long_leg:return False
        atm=float(round(spot_now['close']/cfg.roll_step)*cfg.roll_step)
        if atm<=long_leg.strike:counter['atm_short_below_long_skipped']+=1;return False
        chosen=min(atm,long_leg.strike+cfg.max_short_long_width)
        for _,newex in sorted((dte(session,e),e) for e in data.expiries_at(provider,session,signal) if 1<=dte(session,e)<=cfg.max_short_dte and e not in {x[0] for x in sale_expiries}):
            if dte(newex,long_exp)<=30:continue
            if cfg.lot_master.get(session,newex).qty!=lot:counter['rolling_lot_mismatch']+=1;continue
            signq=data.opt(provider,session,signal,newex,chosen,long_leg.cp)
            fill=quote_fill(data,provider,session,newex,chosen,long_leg.cp,windows,fill_floor,delayed,skip if windows==ENTRY_WINDOWS else 0)
            if not fill or not is_quote(signq,volume=cfg.min_signal_volume):continue
            if not roll_gate(session,newex,chosen,long_leg.expiry,long_leg.strike,long_leg.cp,signal):return False
            transact(Leg(newex,chosen,long_leg.cp,-qty,'short'),fill,session,'roll_short_sale',signal)
            return True
        return False
    def roll_short_tm1(session,old):
        """Buy the expiring short and sell next week's in one minute, a session early. Falls back to the ordinary roll if it cannot."""
        windows=exit_window(*CLOSE_WINDOW);floor=max(fill_floor,required_volume(cfg,abs(old.units),lot,cfg.min_exit_volume))
        for _,newex in sorted((dte(session,e),e) for e in data.expiries_at(provider,session,'15:15') if e>old.expiry and dte(session,e)<=cfg.max_short_dte and e not in {x[0] for x in sale_expiries}):
            if dte(newex,long_exp)<=30:counter['tm1_roll_long_too_short']+=1;return False
            if cfg.lot_master.get(session,newex).qty!=lot:counter['rolling_lot_mismatch']+=1;continue
            if not is_quote(data.opt(provider,session,'15:15',newex,old.strike,old.cp),volume=cfg.min_signal_volume):continue
            pair=synchronized_fills(data,provider,session,[(old.expiry,old.strike,old.cp),(newex,old.strike,old.cp)],windows,floor,delayed)
            if not pair:counter['tm1_roll_no_common_minute']+=1;continue
            long_leg=next((x for x in positions.values() if x.units>0 and x.role=='long'),None)
            if long_leg and not roll_gate(session,newex,old.strike,long_leg.expiry,long_leg.strike,old.cp,'15:15'):return False
            transact(Leg(old.expiry,old.strike,old.cp,-old.units,'exit'),pair[0],session,'tm1_short_buyback','15:15')
            transact(Leg(newex,old.strike,old.cp,-qty,'short'),pair[1],session,'roll_short_sale','15:15')
            return True
        return False
    force_unobservable=getattr(cfg,'unobservable_stop_policy','wait')=='force_exit'
    entry_index=data.valuation_days.index(day)
    last_day=day;end_reason=None;final_status=None;last_good_short=None;tm1_attempted=False;tm1_partial=False
    for session in data.valuation_days[entry_index:]:
        last_day=session
        if not positions:break
        # For future-day open, update the theoretical delta hedge using mark-to-market proxy.
        if hedge!='none' and session!=day:
            sp=data.spot(session,'09:46')
            if is_quote(sp,'open'):
                s_open=float(sp['open']);move=hedge_units*(s_open-last_spot);hedge_pnl+=move;last_spot=s_open
                ledger.append(dict(event='index_delta_proxy_mark',day=session,time='09:46',spot=s_open,mode='UNTRADEABLE_PROXY',hedge_cash=move))
                if hedge in ('delta_daily_proxy','gamma_reduced_delta_proxy'):
                    gsum=0.;valid=True
                    for leg in positions.values():
                        g=data.greek(provider,session,'09:45',leg.expiry,leg.strike,leg.cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
                        if g is None:valid=False;break
                        gsum+=leg.units*g['delta_proxy']
                    if valid and abs(-gsum-hedge_units)>=cfg.delta_band*qty:
                        cost=hedge_cost(-gsum-hedge_units,s_open,session,cfg);cash-=cost
                        ledger.append(dict(event='index_delta_proxy_rebalance',day=session,time='09:46',units=-gsum-hedge_units,spot=s_open,mode='UNTRADEABLE_PROXY',hedge_cash=-cost))
                        hedge_units=-gsum
        # Retry pending exits first: unavailable liquid bar cannot terminate a real position.
        if pending_exit:
            close_legs(session,ENTRY_WINDOWS,'pending_exit_fill','09:45',allow_expiry=end_reason=='t_minus_1')
            if not positions: end_reason='late_liquid_exit';pending_exit=False
        # Roll after last short expires, never before; campaign long is reused.
        rollover=policy in ROLLING_POLICIES
        if policy=='atm_weekly_roll' and not pending_exit and not [x for x in positions.values() if x.units<0] and session!=day and dte(session,long_exp)>30 and \
            not (roll_wait_until and session<=roll_wait_until) and dt.date.fromisoformat(session).weekday() in cfg.weekdays and session_open(session):
            sell_atm_weekly(session,'09:45',ENTRY_WINDOWS)
        carry=policy=='carry30'
        shorts=[x for x in positions.values() if x.units<0]
        longs=[x for x in positions.values() if x.units>0 and x.role!='gamma']
        if not pending_exit and rollover and not shorts and longs and session!=day and dte(session,long_exp)>30 and \
            not (roll_wait_until and session<=roll_wait_until) and session_open(session) and \
            dt.date.fromisoformat(session).weekday() in cfg.weekdays:
            surf=data.expiries_at(provider,session,'09:45')
            sc=data.spot(session,'09:45')
            if is_quote(sc):
                # strike target uses current 09:45 only; bounded strike gap for diagonals
                spotnow=float(sc['close']); old=strikes[0][0]
                target=old
                if policy in ('rolling_diagonal','rolling_recenter'):
                    target=round(spotnow/cfg.roll_step)*cfg.roll_step
                    target=float(target)
                if policy=='rolling_diagonal' and abs(target-old)>cfg.max_diagonal_gap:
                    target=old+math.copysign(cfg.max_diagonal_gap,target-old)
                    target=float(round(target/cfg.roll_step)*cfg.roll_step)
                rolled_atomically=False
                recenter_due=policy=='rolling_recenter' and abs(math.log(spotnow/old))>cfg.recenter_threshold
                if recenter_due:
                    # All three legs or none: no leg moves before the short sale is known to be possible.
                    options=sorted((abs(dte(session,e)-cand['target_tenor']),e)
                         for e in surf if dte(session,e)>=35 and abs(dte(session,e)-cand['target_tenor'])<=cfg.tenor_tolerance)
                    if not options:counter['recenter_no_long_tenor']+=1
                    elif cfg.lot_master.get(session,options[0][1]).qty!=lot:counter['recenter_lot_mismatch']+=1
                    else:
                        nex=options[0][1];old_leg=longs[0];cp=old_leg.cp;exit_floor=required_volume(cfg,abs(old_leg.units),lot,cfg.min_exit_volume)
                        for newex in sorted((e for e in surf if 1<=dte(session,e)<=cfg.max_short_dte and e not in {x[0] for x in sale_expiries}),key=lambda e:dte(session,e)):
                            if dte(newex,nex)<=30 or cfg.lot_master.get(session,newex).qty!=lot:continue
                            signq=data.opt(provider,session,'09:45',newex,target,cp)
                            if sync:trio=synchronized_fills(data,provider,session,[(old_leg.expiry,old_leg.strike,cp),(nex,target,cp),(newex,target,cp)],minimum=max(fill_floor,exit_floor),delayed=delayed,skip=skip)
                            else:
                                trio=[quote_fill(data,provider,session,old_leg.expiry,old_leg.strike,cp,minimum=exit_floor,delayed=delayed,skip=skip),
                                      quote_fill(data,provider,session,nex,target,cp,minimum=fill_floor,delayed=delayed,skip=skip),
                                      quote_fill(data,provider,session,newex,target,cp,minimum=fill_floor,delayed=delayed,skip=skip)]
                                trio=trio if all(trio) else None
                            if not trio or not is_quote(signq,volume=cfg.min_signal_volume):continue
                            if roll_gate(session,newex,target,nex,target,cp):
                                transact(Leg(old_leg.expiry,old_leg.strike,cp,-old_leg.units,'exit'),trio[0],session,'recenter_old_long_exit','09:45')
                                transact(Leg(nex,target,cp,qty,'long'),trio[1],session,'recenter_new_long_open','09:45')
                                transact(Leg(newex,target,cp,-qty,'short'),trio[2],session,'roll_short_sale','09:45')
                                campaign_events.append(dict(day=session,event='recenter',old_strike=old,new_strike=target))
                                long_exp=nex;strikes=[(target,cp)];old=target;rolled_atomically=True
                            break
                        if not rolled_atomically and not (roll_decisions and roll_decisions[-1]['day']==session):
                            counter['recenter_no_common_minute']+=1
                available=sorted((dte(session,e),e) for e in surf if 1<=dte(session,e)<=cfg.max_short_dte and e not in {x[0] for x in sale_expiries})
                gated=bool(roll_decisions) and roll_decisions[-1]['day']==session and roll_decisions[-1]['decision']=='skip'
                for _,newex in ([] if rolled_atomically or gated else available):
                    if dte(newex,long_exp)<=30:continue
                    chosen=target if policy=='rolling_diagonal' else strikes[0][0]
                    cp=strikes[0][1]
                    if cfg.lot_master.get(session,newex).qty!=lot:
                        counter['rolling_lot_mismatch']+=1;continue
                    signq=data.opt(provider,session,'09:45',newex,chosen,cp)
                    f=quote_fill(data,provider,session,newex,chosen,cp,minimum=fill_floor,delayed=delayed,skip=skip)
                    if not f or not is_quote(signq,volume=cfg.min_signal_volume):continue
                    if not roll_gate(session,newex,chosen,long_exp,next(x.strike for x in positions.values() if x.units>0 and x.role=='long'),cp):break
                    transact(Leg(newex,chosen,cp,-qty,'short'),f,session,'roll_short_sale','09:45')
                    if recenter_due:counter['recenter_fell_back_to_plain_roll']+=1   # the old strike is kept, on the record
                    if chosen!=old:campaign_events.append(dict(day=session,event='diagonal',short_strike=chosen,long_strike=old,downside_gap=max(0,chosen-old) if cp=='PE' else max(0,old-chosen)))
                    break
        # Stops use existing signal candle only; fill is a later candle.
        early=False
        if (policy in tuple(PRICE_EXITS)+('iv_converge',) or pending_exit) and session in data.dayset:
            for signal,qualifying,fillminute in SIGNALS:
                if pending_exit:   # an unfinished exit retries at every later checkpoint the same day
                    close_legs(session,exit_window(qualifying,fillminute),'pending_exit_fill',signal,allow_expiry=end_reason=='t_minus_1')
                    if not positions:pending_exit=False;end_reason=end_reason or 'late_liquid_exit'
                    continue
                if policy not in tuple(PRICE_EXITS)+('iv_converge',):continue
                marked=0.;valid=True
                for leg in positions.values():
                    q=data.opt(provider,session,signal,leg.expiry,leg.strike,leg.cp)
                    if not is_quote(q,volume=0):
                        valid=False;counter['stop_unobservable_checkpoints']+=1
                        ledger.append(dict(event='stop_unobservable',day=session,time=signal,reason='missing_option_quote',expiry=leg.expiry,strike=leg.strike,cp=leg.cp))
                        if force_unobservable:pending_exit=True;end_reason='unobservable_stop_forced'
                        break
                    marked+=leg.units*q['close']
                if not valid:continue
                gain=cash+hedge_pnl+marked
                if hedge!='none' and last_spot is not None:   # mark the hedge to this checkpoint, as the options are
                    check_spot=data.spot(session,signal)
                    if is_quote(check_spot):gain+=hedge_units*(check_spot['close']-last_spot)
                triggered=False
                if policy=='iv_converge' and len(strikes)==1:
                    strike,cp=strikes[0]
                    shortleg=next((x for x in positions.values() if x.units<0),None)
                    longleg=next((x for x in positions.values() if x.units>0 and x.role=='long'),None)
                    if shortleg and longleg:
                        gs=data.greek(provider,session,signal,shortleg.expiry,shortleg.strike,cp,
                            cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
                        gl=data.greek(provider,session,signal,longleg.expiry,longleg.strike,cp,
                            cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
                        triggered=bool(gs and gl and gl['iv'] and gs['iv']/gl['iv']<=cfg.iv_convergence)
                        if not (gs and gl and gl['iv']):
                            counter['stop_unobservable_checkpoints']+=1
                            ledger.append(dict(event='stop_unobservable',day=session,time=signal,reason='missing_iv'))
                            if force_unobservable:pending_exit=True;end_reason='unobservable_stop_forced'
                else:
                    take,stop=PRICE_EXITS[policy]
                    triggered=gain>=take*debit or gain<=stop*debit
                if triggered:
                    pending_exit=True;early=True;end_reason='checkpoint_'+policy
                    close_legs(session,exit_window(qualifying,fillminute),'stop_exit',signal,allow_expiry=True)
                    if not positions:
                        pending_exit=False;break
                    continue
        # One session before the short expires: stand aside from expiry-day gamma and the expiry-day margin step-up.
        open_shorts=[x for x in positions.values() if x.units<0]
        if open_shorts and not pending_exit and session<open_shorts[0].expiry and sessions_between(session,open_shorts[0].expiry,data.holidays)==0:
            if policy in TM1_EXITS:
                pending_exit=True;end_reason='t_minus_1';tm1_attempted=True
                close_legs(session,exit_window(*CLOSE_WINDOW),'tm1_exit','15:15')
                if not positions:pending_exit=False
                else:tm1_partial=True
            elif policy=='rolling_tm1':counter['tm1_roll_completed' if roll_short_tm1(session,open_shorts[0]) else 'tm1_roll_failed']+=1
        # Exit short by close on expiry. If no liquid close, CASH-SETTLE on proxy
        # spot closing price (NSE official settlement unavailable in these datasets).
        for leg in list(positions.values()):
            if (leg.units<0 or fly) and session==leg.expiry:
                fill=quote_fill(data,provider,session,leg.expiry,leg.strike,leg.cp,exit_window(*CLOSE_WINDOW),required_volume(cfg,abs(leg.units),lot,cfg.min_exit_volume),delayed)
                if fill and (not pending_exit or end_reason=='t_minus_1'):
                    transact(Leg(leg.expiry,leg.strike,leg.cp,-leg.units,'exit'),fill,session,'preexpiry_buyback','15:15')
        for leg in list(positions.values()):
            # >= not ==: an expiry whose session is absent from data.days must still settle.
            if session>=leg.expiry:
                reference=settlement_reference(data,cfg,leg.expiry,counter)
                if reference:
                    underlying,event,evidence,is_official=reference
                    settlement=max(leg.strike-underlying,0) if leg.cp=='PE' else max(underlying-leg.strike,0)
                    exercise_stt=leg.units*settlement*exercise_stt_rate(leg.expiry,cfg) if leg.units>0 else 0.
                    exercise_fee=getattr(cfg,'exercise_fee',0.)*exercise_fee_count(leg.units,lot,cfg)*(1+cfg.gst) if settlement>0 else 0.
                    if exercise_fee:counter['exercise_fee_assumed']+=1
                    cash+=leg.units*settlement-exercise_stt-exercise_fee
                    del positions[(leg.expiry,leg.strike,leg.cp)]
                    if is_official:official_settlements+=1
                    else:proxy_used=True;counter['settlement_proxy']+=1
                    if session>leg.expiry:counter['settled_after_missing_expiry_session']+=1
                    ledger.append(dict(event=event,day=session,expiry=leg.expiry,cp=leg.cp,
                        strike=leg.strike,units=leg.units,underlying_price=underlying,
                        evidence=evidence,payoff=settlement,exercise_stt=exercise_stt,exercise_fee=exercise_fee,
                        cash_flow=leg.units*settlement-exercise_stt-exercise_fee))
                else:
                    counter['missing_settlement_spot']+=1
        shorts=[x for x in positions.values() if x.units<0]
        if policy=='atm_weekly_roll' and getattr(cfg,'roll_timing','next_session')=='expiry_day_close' and not shorts and session==prior_short_expiry(sale_expiries) and \
            not pending_exit and positions and dte(session,long_exp)>30 and not (roll_wait_until and session<=roll_wait_until) and session_open(session):
            sell_atm_weekly(session,'15:15',exit_window(*CLOSE_WINDOW))
            shorts=[x for x in positions.values() if x.units<0]
        if not shorts and (policy in WEEKLY_FAMILY or
                          dte(session,long_exp)<=30 or pending_exit):
            pending_exit=True
            close_legs(session,exit_window(*CLOSE_WINDOW),'end_long_exit','15:15')
            if not positions:pending_exit=False;end_reason=end_reason or ('long_30DTE' if long_exp and dte(session,long_exp)<=30 else 'weekly_exit')
        if pending_exit and positions and all(x.units>0 for x in positions.values()):
            if exit_first_attempt is None:exit_first_attempt=session
            elif session!=exit_first_attempt:exit_lag_sessions+=1
            if 0<=getattr(cfg,'max_exit_lag',-1)<=exit_lag_sessions:
                force_exit_at_mark(session);pending_exit=False;forced_exit=True;end_reason='forced_exit_lag_cap'
        if hedge!='none' and not positions and hedge_units:flatten_hedge(session)
        # Mark ALL positions without future bars, even if no mark for a long leg.
        pnl=cash+hedge_pnl;lower=pnl;upper=pnl;methods=[];capital=0.;mark_details=[]
        eod_price,eod_source=eod_spot(data,session)
        for leg in positions.values():
            px,lo,hi,method,age=estimate_mark(data,cfg,session,leg,history)
            pnl+=leg.units*px
            lower+=leg.units*(lo if leg.units>0 else hi)
            upper+=leg.units*(hi if leg.units>0 else lo)
            methods.append(method);counter['mark_'+method]+=1
            mark_details.append(dict(expiry=leg.expiry,strike=leg.strike,cp=leg.cp,units=leg.units,
                price=px,lower=lo,upper=hi,method=method,age_minutes=age*1440,
                source_file_id=(data.opt(provider,session,'15:29',leg.expiry,leg.strike,leg.cp) or {}).get('source_id'),
                source_row_index=(data.opt(provider,session,'15:29',leg.expiry,leg.strike,leg.cp) or {}).get('source_row')))
            if leg.units<0 and eod_price:capital+=cfg.capital_scenario_pct*eod_price*abs(leg.units)
            # Only update last-good with a quote from TODAY; model marks do not refresh the stale clock.
            if method in ('traded_bar','zero_volume_quote'):
                g=data.greek(provider,session,'15:29',leg.expiry,leg.strike,leg.cp,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps)
                fi=data.forward(provider,session,'15:29',leg.expiry,cfg.rate,cfg.min_pair_volume,cfg.max_parity_bps);f=fi['forward']
                t_now=years(leg.expiry,session,'15:29')
                key_now=(leg.expiry,leg.strike,leg.cp)
                if key_now in history and history[key_now]['day']<session:
                    part=attribute_leg(history[key_now],px,f,g.get('iv') if g else None,t_now,leg.strike,leg.cp,leg.units,fi.get('rate') if fi.get('rate') is not None else cfg.rate)
                    if part:
                        for name,value in part.items():attribution[name]+=value
                history[key_now]={'price':px,'day':session,'iv':g.get('iv') if g else None,'f':f,
                    'spot':eod_price,'t':t_now,'r':fi.get('rate') if fi.get('rate') is not None else cfg.rate}
        if hedge!='none' and eod_price and last_spot is not None:
            pnl+=hedge_units*(eod_price-last_spot)
            lower+=hedge_units*(eod_price-last_spot);upper+=hedge_units*(eod_price-last_spot)
        if fly and positions:capital=fly*qty   # defined risk: the wing width is the most a fly can lose before costs
        capital=max(capital,max(0,debit))
        residual='' if eod_source=='minute_1529' or not positions else f'|SPOT_EOD_{eod_source.upper()}'
        if eod_source!='minute_1529' and positions:counter['marks_without_1529_spot']+=1
        if pending_exit and positions:   # one leg gone, the other awaiting a liquid bar
            short_units=sum(-x.units for x in positions.values() if x.units<0)
            long_units=sum(x.units for x in positions.values() if x.units>0 and x.role!='gamma')
            if short_units>long_units:residual+='|PARTIAL_EXIT_NAKED_SHORT';counter['partial_exit_days_naked_short']+=1
            elif long_units>short_units:residual+='|PARTIAL_EXIT_LONG_ONLY';counter['partial_exit_days_long_only']+=1
        marks.append(dict(day=session,pnl=pnl,lower=min(lower,upper),upper=max(lower,upper),
                          quality=('|'.join(sorted(set(methods))) if methods else 'closed')+residual,
                          capital_scenario=capital,open_legs=len(positions),mark_details=mark_details))
        if not positions:break
    if hedge!='none' and last_spot is not None and hedge_units:
        endspot=eod_spot(data,last_day)[0]
        if endspot:
            move=hedge_units*(endspot-last_spot);cost=hedge_cost(-hedge_units,endspot,last_day,cfg)
            hedge_pnl+=move;cash-=cost
            ledger.append(dict(event='index_delta_proxy_close',day=last_day,units=-hedge_units,
                               mode='UNTRADEABLE_PROXY',hedge_cash=move-cost))
    status='unresolved' if positions else ('proxy_settled' if counter['settlement_proxy'] else 'closed_verified_settlement' if official_settlements else 'closed_traded')
    if status=='unresolved':counter['unresolved_horizon']+=1
    if data.complete_sessions.get(day,1)!=1:counter['entry_day_later_incomplete']+=1   # diagnostic only: unknowable at 09:45
    if status!='unresolved' and hedge!='none' and marks:
        adjustment=(cash+hedge_pnl)-marks[-1]['pnl']
        marks[-1]['pnl']+=adjustment
        marks[-1]['lower']+=adjustment
        marks[-1]['upper']+=adjustment
    if status=='unresolved' and marks:
        # Retain open positions. Conservative mark is not a traded exit.
        marks[-1]['pnl']=marks[-1]['lower']
        marks[-1]['quality']+='|HORIZON_CONSERVATIVE_UNREALIZED'
    pnl=cash+hedge_pnl if status!='unresolved' else marks[-1]['lower'] if marks else None
    long_cost=-sum(x['cash_flow'] for x in ledger if x['event']=='initial_long')
    width_inr=fly*qty if fly else max(0.,float(k)-long_strike)*qty if long_strike is not None else 0.
    campaign=campaign_economics(ledger,long_cost,width_inr)
    pnl_attribution={name:float(attribution[name]) for name in ('delta','gamma','vega','vanna_volga','rate','theta','residual')}
    pnl_attribution['transaction_charges']=-float(sum(x['charges']['total'] for x in ledger if 'charges' in x))
    pnl_attribution['slippage']=-float(sum(abs(x['units'])*x['slippage'] for x in ledger if 'charges' in x))
    pnl_attribution['exercise_costs']=-float(sum(x.get('exercise_stt',0.)+x.get('exercise_fee',0.) for x in ledger))
    pnl_attribution['hedge_net']=float(sum(x.get('hedge_cash',0.) for x in ledger))
    if pnl is not None:pnl_attribution['not_attributed_model_gap']=float(pnl-sum(pnl_attribution.values()))   # entry and exit marks, expiry, stale or proxy marks, horizon stress
    # Distinguish an executable trade from the modelled hedge and settlement.
    settled=any('settlement' in x['event'] for x in ledger)
    tm1_outcome=None
    if policy in TM1_EXITS:
        tm1_outcome=('carried_to_settlement' if settled else 'partial_closed_later' if tm1_partial else 'flat_at_t_minus_1') if tm1_attempted else ('carried_to_settlement' if settled else 'exited_before_t_minus_1' if not positions else 'open_at_data_end')
        counter['tm1_outcome_'+tm1_outcome]+=1
    planned_day=scheduled_exit_day(cand,policy,cfg);planned_mark=next((x['pnl'] for x in marks if x['day']==planned_day),None)
    execution_class='theoretical_delta' if hedge!='none' else 'settlement_proxy' if proxy_used else 'OHLC_execution_proxy'
    result=dict(cand,day=day,exit_day=last_day,status=status,exit_reason=end_reason,
                hedge=hedge,policy=policy,structure=structure,pnl_inr=pnl,initial_debit_inr=debit,
                capital_scenario_pct=cfg.capital_scenario_pct,short_sales=short_sales,
                executed_fills=sum(x.get('mode')=='liquid_bar_proxy' for x in ledger),
                missing_marks=dict(counter),execution_class=execution_class,
                scheduled_exit_mark_inr=planned_mark,post_schedule_drift_inr=(pnl-planned_mark) if pnl is not None and planned_mark is not None else None,
                exit_lag_sessions=exit_lag_sessions,forced_exit=forced_exit,
                lot_units=qty,gamma_reduction_pct=gamma_reduction_pct,fly_width=fly,tm1_outcome=tm1_outcome,
                campaign=campaign,pnl_attribution=pnl_attribution,defined_risk_capital_inr=width_inr+max(0.,debit),
                entry_fill_minutes=entry_minutes,roll_decisions=roll_decisions,
                partial_exit_days=dict(naked_short=counter['partial_exit_days_naked_short'],long_only=counter['partial_exit_days_long_only']),
                entry_delta_proxy=exposure['delta'] if entry_delta_proxy is None else entry_delta_proxy,
                entry_gamma_proxy=exposure['gamma'],entry_vega=exposure['vega'],
                official_settlements=official_settlements,
                campaign_events=campaign_events,ledger=ledger,
                mtm_low=min(x['lower'] for x in marks),last_mark=marks[-1] if marks else None)
    return (result,marks),None

RESULT_SQL='''
CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE trades(
 id TEXT PRIMARY KEY,day TEXT NOT NULL,exit_day TEXT NOT NULL,status TEXT NOT NULL,
 provider TEXT NOT NULL,strike_mode TEXT NOT NULL,strike_step INTEGER,strike_value REAL,
 target_tenor INTEGER,hedge TEXT NOT NULL,exit_policy TEXT NOT NULL,structure TEXT NOT NULL,
 short_dte_bucket TEXT, iv_ratio REAL,forward_iv_ratio REAL,
 iv5 REAL,iv15 REAL,iv30 REAL,iv45 REAL,iv60 REAL,iv75 REAL,iv90 REAL,iv120 REAL,
 rv5_close REAL,rv5_parkinson REAL,iv_to_rv REAL,
 iv_to_rv_parkinson REAL,skew_slope REAL,
 rv5_yz REAL,iv_to_rv_yz REAL,gap_share_5d REAL,rv5_1m_range_proxy REAL,iv_to_rv_1m_range_proxy REAL,
 rv5_1m_closeclose REAL,iv_to_rv_1m_closeclose REAL,lot_units INTEGER,
 bf25_short REAL,var_carry_daily REAL,
 long_strike REAL,long_strike_offset REAL,iv_ratio_biz REAL,iv_to_rv_yz_biz REAL,spot_to_sma50 REAL,spot_to_sma100 REAL,spot_to_sma200 REAL,trend_21d REAL,pct_below_52w_high REAL,
 pnl_inr REAL,initial_debit_inr REAL,capital_proxy_inr REAL,entry_capital_scenario REAL,
 short_sales INTEGER,mark_flags INTEGER,execution_class TEXT,details TEXT NOT NULL,economic_id TEXT NOT NULL,
 scheduled_exit_day TEXT NOT NULL,short_sessions INTEGER NOT NULL,entry_weekday INTEGER NOT NULL,short_expiry_weekday INTEGER NOT NULL,
 entry_risk_pts REAL,events_in_short INTEGER NOT NULL DEFAULT 0,path_id TEXT NOT NULL,contract_id TEXT NOT NULL,mtm_id TEXT NOT NULL,tm1_outcome TEXT);
CREATE TABLE marks(trade_id TEXT NOT NULL,day TEXT NOT NULL,pnl_inr REAL NOT NULL,
 lower REAL NOT NULL,upper REAL NOT NULL,quality TEXT NOT NULL,capital_scenario REAL NOT NULL,
 mark_details TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(trade_id,day));
CREATE TABLE fills(trade_id TEXT NOT NULL,seq INTEGER NOT NULL,day TEXT,event TEXT NOT NULL,
 details TEXT NOT NULL,PRIMARY KEY(trade_id,seq));
CREATE TABLE diagnostics(reason TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE INDEX trades_grid ON trades(strike_mode,strike_step,strike_value,target_tenor,hedge,exit_policy,structure,day);
CREATE INDEX marks_day ON marks(day);
'''

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--cache',default='research_cache_v4.sqlite')
    ap.add_argument('--out',default='research_trades_v4.sqlite')
    ap.add_argument('--provider',choices=('rissin_upstox','thetrademarkk'),default='rissin_upstox')
    ap.add_argument('--from-date',dest='start',default='2024-11-01');ap.add_argument('--to-date',dest='end',default='2026-07-01')
    ap.add_argument('--weekdays',default='2,3,4');ap.add_argument('--long-tenors',default='30,45,60,75,90,120')
    ap.add_argument('--tenor-tolerance',type=int,default=9);ap.add_argument('--max-short-dte',type=int,default=8)
    ap.add_argument('--strike-mode',choices=('points','pct','std','delta'),default='points')
    ap.add_argument('--round-steps',default='100,500');ap.add_argument('--strike-values',default='-500,0,500')
    ap.add_argument('--hedges',default='none');ap.add_argument('--policies',default='weekly,carry30,rolling_static,rolling_diagonal,rolling_recenter')
    ap.add_argument('--structures',default='put,double',help='put, double, call, iron_fly')
    ap.add_argument('--fly-wing-z',type=float,default=1.5,help='iron_fly: wing distance in expected moves to the short expiry')
    ap.add_argument('--event-calendar',default=None,help='CSV lines event_date,first_known_at[,superseded_at[,source]]. Timestamps are ISO-8601, IST unless an offset is given. A bare date counts from the next day')
    ap.add_argument('--event-policy',choices=('all','skip_event_weeks','only_event_weeks'),default='all',help='Keep or drop entries whose short window holds a listed event')
    ap.add_argument('--allow-calendar-mismatch',action='store_true',help='Run although --holiday-calendar disagrees with the sessions in the cache')
    ap.add_argument('--workers',type=int,default=1,help='Simulate calendar months in parallel; output matches --workers 1')
    ap.add_argument('--option-type',choices=('PE','CE'),default='PE',help='Generate put or call IV candidates; choose CE with --structures call')
    ap.add_argument('--contract-master',default=None)
    ap.add_argument('--settlement-master',default=None,help='Verified CSV: expiry,settlement_price,evidence_url')
    ap.add_argument('--rate',type=float,default=.06);ap.add_argument('--min-signal-volume',type=int,default=1)
    ap.add_argument('--min-pair-volume',type=int,default=1);ap.add_argument('--max-parity-bps',type=float,default=40.)
    ap.add_argument('--min-fill-volume',type=int,default=1);ap.add_argument('--min-exit-volume',type=int,default=1)
    ap.add_argument('--slippage',type=float,default=.5);ap.add_argument('--slippage-mode',choices=('flat','tenor_volume','intrabar_range'),default='tenor_volume')
    ap.add_argument('--intrabar-lambda',type=float,default=.5);ap.add_argument('--intrabar-min',type=float,default=.05)
    ap.add_argument('--fill-sync',choices=('synchronized','independent'),default='synchronized',help='synchronized: all entry legs fill from the same minute')
    ap.add_argument('--settlement-proxy',choices=('twap_1500_1529','close_1529'),default='twap_1500_1529',help='Used only without a --settlement-master row; always labelled proxy')
    ap.add_argument('--exercise-stt',type=float,default=None,help='Override statutory STT on long ITM expiry; default is by date')
    ap.add_argument('--exclude-vendor-discordant',action='store_true')
    ap.add_argument('--long-strike-offsets',default='0',help='Long strike minus short strike, points. Negative buys protection lower (a put diagonal)')
    ap.add_argument('--roll-timing',choices=('next_session','expiry_day_close'),default='next_session',help='atm_weekly_roll only')
    ap.add_argument('--max-short-long-width',type=float,default=2000.,help='atm_weekly_roll: cap on short strike minus long strike')
    ap.add_argument('--fill-model',choices=('next_bar_open','next_bar_open_adverse','next_bar_open_traded_only','qualified_minute_close','same_minute_first_print','next_minute','next_minute_adverse_range'),default='next_bar_open',help='next_bar_open: eligible on a finished minute, filled at the next bar open, with no gate on that bar. next_bar_open_adverse is an ex-post stress. Others need --allow-conditional-fill-models')
    ap.add_argument('--allow-conditional-fill-models',action='store_true',help='Diagnostics only: open-based models condition the fill on volume printed after the order')
    ap.add_argument('--max-participation',type=float,default=.10,help='Largest share of a bar\'s volume one order may take; 0 disables')
    ap.add_argument('--volume-unit',choices=('auto','units','contracts'),default='auto',help='What the vendor volume column counts')
    ap.add_argument('--special-sessions',default=None,help='File of YYYY-MM-DD dates, one per line, published in advance as shortened or special; no new orders then')
    ap.add_argument('--holiday-calendar',default=None,help='File of published YYYY-MM-DD holidays for business-day IV. Without it, weekdays only')
    ap.add_argument('--allow-special-sessions',action='store_true',help='Permit new orders on weekends and listed special sessions')
    ap.add_argument('--roll-signal-policy',choices=('unconditional','require_ratio','wait_until_eligible'),default='unconditional',help='IV gate on each later weekly short; run alternatives into separate --out files')
    ap.add_argument('--roll-iv-min',type=float,default=None);ap.add_argument('--roll-iv-max',type=float,default=999.)
    ap.add_argument('--brokerage',type=float,default=20.);ap.add_argument('--gst',type=float,default=.18)
    ap.add_argument('--exchange-bps',type=float,default=None,help='Fixed override. Default is the dated NSE schedule including IPFT')
    ap.add_argument('--sebi-bps',type=float,default=.01,help='SEBI turnover fee on premium, Rs 10 per crore')
    ap.add_argument('--exercise-fee',type=float,default=20.,help='Broker fee per ITM settled leg, before GST. Conservative; set 0 if your broker charges none')
    ap.add_argument('--entry-delay-minutes',type=int,default=0,help='Stress: fill this many minutes later at entry and rolls (0-3). Exits keep their cached two-minute window')
    ap.add_argument('--exercise-fee-basis',choices=('per_contract','per_order','per_leg','zero'),default='per_contract',help='How the assumed --exercise-fee scales')
    ap.add_argument('--lots',type=int,default=1,help='Lots per leg; costs and participation scale with it')
    ap.add_argument('--brokerage-order-mode',choices=('per_lot_orders','one_order_per_leg','lots_per_order','fixed_orders'),default='per_lot_orders',help='How many orders the broker bills per fill')
    ap.add_argument('--lots-per-order',type=int,default=1);ap.add_argument('--fixed-orders',type=int,default=1)
    ap.add_argument('--unobservable-stop-policy',choices=('wait','force_exit'),default='wait',help='force_exit is a stress: try to close at the next liquid checkpoint')
    ap.add_argument('--no-hedge-statutory',dest='hedge_statutory',action='store_false',help='Charge only --hedge-bps on the index proxy')
    ap.add_argument('--stt',type=float,default=None)
    ap.add_argument('--stamp',type=float,default=.00003);ap.add_argument('--hedge-bps',type=float,default=2.)
    ap.add_argument('--delta-band',type=float,default=.1);ap.add_argument('--max-gamma-lots',type=int,default=3)
    ap.add_argument('--min-debit',type=float,default=1.);ap.add_argument('--iv-convergence',type=float,default=1.)
    ap.add_argument('--max-candidates',type=int,default=0)
    ap.add_argument('--max-mark-age-days',type=int,default=5);ap.add_argument('--max-interp-gap',type=float,default=1000.)
    ap.add_argument('--mark-stress',type=float,default=.20)
    ap.add_argument('--max-exit-lag',type=int,default=-1,help='Sessions a long may wait for a liquid exit after its short is gone, then it is booked at a modelled mark. -1 keeps waiting')
    ap.add_argument('--forced-exit-haircut',type=float,default=.02,help='Discount to a same-day printed close when --max-exit-lag forces an exit')
    ap.add_argument('--roll-step',type=int,default=100);ap.add_argument('--max-diagonal-gap',type=int,default=500)
    ap.add_argument('--recenter-threshold',type=float,default=.025);ap.add_argument('--double-z',type=float,default=.75)
    ap.add_argument('--capital-scenario-pct',type=float,default=.15,
                    help='SCENARIO capital charge per short contract; not NSE SPAN!')
    a=ap.parse_args()
    problems=validate_config(a)
    if problems:ap.error('; '.join(problems))
    if a.fill_model not in CAUSAL_FILL_MODELS+STRESS_FILL_MODELS and not a.allow_conditional_fill_models:ap.error(f'{a.fill_model} conditions the fill on later volume. Pass --allow-conditional-fill-models for a diagnostic run')
    if a.roll_signal_policy!='unconditional' and a.roll_iv_min is None:ap.error('--roll-iv-min is required with a conditional roll policy')
    if 'rolling_recenter' in a.policies and a.fill_sync=='independent':ap.error('rolling_recenter requires --fill-sync synchronized')
    a.long_strike_offsets=[float(x) for x in a.long_strike_offsets.split(',')]
    if any(x>0 for x in a.long_strike_offsets):ap.error('offsets must be zero or negative for put diagonals')
    a.weekdays={int(x) for x in a.weekdays.split(',')}
    a.long_tenors=[int(x) for x in a.long_tenors.split(',')]
    a.round_steps=[int(x) for x in a.round_steps.split(',')]
    a.strike_values=[float(x) for x in a.strike_values.split(',')]
    hedges=[x.strip() for x in a.hedges.split(',')]
    policies=[x.strip() for x in a.policies.split(',')]
    structures=[x.strip() for x in a.structures.split(',')]
    if set(hedges)-{'none','delta_entry_proxy','delta_daily_proxy','gamma_reduced_delta_proxy'}:ap.error('unknown hedge')
    if set(policies)-{'weekly','carry30','rolling','rolling_static','rolling_diagonal','rolling_recenter','rolling_tm1','atm_weekly_roll','p25_s50','p50_s100','iv_converge','tm1','p25_s50_tm1'}:ap.error('unknown policy')
    if set(structures)-set(STRUCTURES):ap.error('unknown structure')
    if a.workers<1:ap.error('--workers must be at least 1')
    if a.workers>1 and a.max_candidates:ap.error('--max-candidates needs --workers 1')
    if a.event_policy!='all' and not a.event_calendar:ap.error('--event-policy needs --event-calendar')
    if set(policies)&set(TM1_EXITS+('rolling_tm1',)) and not a.holiday_calendar:ap.error('T-1 policies need --holiday-calendar: without published holidays the last session before expiry cannot be identified')
    a.include_iron_fly='iron_fly' in structures;a.include_calendars=bool(set(structures)-{'iron_fly'})
    if 'iron_fly' in structures and a.hedges!='none':ap.error('iron_fly runs with --hedges none only')
    if any(a.long_strike_offsets) and (a.option_type!='PE' or set(structures)-{'put'}):ap.error('--long-strike-offsets applies to put structures only')
    if not .01<=a.capital_scenario_pct<=1:ap.error('invalid capital stress scenario')
    a.lot_master=LotMaster(a.contract_master)
    a.settlement_master_path=a.settlement_master
    a.settlement_master=SettlementMaster(a.settlement_master)
    if a.option_type=='PE' and set(structures)-{'put','double','iron_fly'}:ap.error('Call-only runs require --option-type CE --structures call')
    if a.option_type=='CE' and set(structures)-{'call'}:ap.error('CE candidate generator supports --structures call only')
    if Path(a.out).exists():raise SystemExit('Results exist: refusing overwrite')
    data=Data(a.cache)
    read_dates=lambda path:[x.strip() for x in Path(path).read_text().splitlines() if x.strip() and not x.startswith('#')]
    if a.special_sessions:data.special_sessions=set(read_dates(a.special_sessions))
    if a.holiday_calendar:data.holidays=[np.datetime64(x) for x in read_dates(a.holiday_calendar)]
    audit=holiday_audit(data.daily,data.holidays) if a.holiday_calendar else None
    if audit:
        print('Holiday calendar audit:',{k:len(v) for k,v in audit.items()},flush=True)
        if (audit['listed_but_full_session'] or audit['unlisted_weekday_missing']) and set(policies)&set(TM1_EXITS+('rolling_tm1',)) and not a.allow_calendar_mismatch:raise SystemExit('--holiday-calendar disagrees with the cache: '+json.dumps(audit)+' Correct the file or pass --allow-calendar-mismatch')
    if a.event_calendar:
        try:data.events=[parse_event_line(line) for line in read_dates(a.event_calendar)]
        except ValueError as e:raise SystemExit(str(e))
    resolve_volume_unit(data,a)
    if a.exclude_vendor_discordant and not data.has_vendor_flags:raise SystemExit('Run nifty-iv-enrich-cache first: vendor_discordance table missing')
    out=sqlite3.connect(a.out);out.execute('PRAGMA synchronous=OFF');out.execute('PRAGMA journal_mode=MEMORY');out.executescript(RESULT_SQL)   # an interrupted run stays complete=0
    out.execute("INSERT INTO meta VALUES('complete','0')")
    out.execute("INSERT INTO meta VALUES('engine','4.5.4')")
    out.execute('INSERT INTO meta VALUES(?,?)',('holiday_calendar_audit',json.dumps(audit)))
    out.execute('INSERT INTO meta VALUES(?,?)',('event_calendar_sha256',lineage_mod.file_digest(a.event_calendar) or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('special_sessions_sha256',lineage_mod.file_digest(a.special_sessions) or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('holiday_calendar_sha256',lineage_mod.file_digest(a.holiday_calendar) or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('tariff_version',TARIFF_VERSION))
    out.execute('INSERT INTO meta VALUES(?,?)',('market_content_digest',data.meta.get('market_content_digest') or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('contract_master_sha256',lineage_mod.file_digest(a.contract_master) or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('settlement_master_sha256',lineage_mod.file_digest(a.settlement_master_path) or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('enrichment_run_id',data.meta.get('enrichment_run_id') or ''))
    out.execute('INSERT INTO meta VALUES(?,?)',('source_fingerprint',data.meta['source_fingerprint']))
    out.execute('INSERT INTO meta VALUES(?,?)',('config',json.dumps({k:v for k,v in vars(a).items() if k not in ('lot_master','settlement_master')},default=str)))
    out.commit();stats=Counter()
    try:
        if a.workers>1:parallel_months(a,data,out,structures,policies,hedges,stats)
        else:simulate_range(data,a,out,structures,policies,hedges,stats)
        out.executemany('INSERT INTO diagnostics VALUES(?,?)',list(stats.items()))
        out.execute("UPDATE meta SET value='1' WHERE name='complete'")
        out.commit()
    except Exception:
        out.rollback();raise
    finally:out.close();data.close()
    print('V4 run complete:',stats['candidate_signals'],'candidate signals; diagnostics:',dict(stats))


def month_ranges(start,end):
    a=dt.date.fromisoformat(start);z=dt.date.fromisoformat(end);out=[]
    while a<=z:
        nxt=dt.date(a.year+(a.month==12),a.month%12+1,1)
        out.append((a.isoformat(),min(z,nxt-dt.timedelta(days=1)).isoformat()));a=nxt
    return out


def shard_month(job):
    """One worker: one calendar month of entries into its own file. Trades may run past the month; only entry days are partitioned."""
    a,(first,last),path,structures,policies,hedges,dates=job
    a=argparse.Namespace(**{**vars(a),'start':first,'end':last})
    data=Data(a.cache);data.special_sessions,data.holidays,data.events=dates
    out=sqlite3.connect(path);out.execute('PRAGMA synchronous=OFF');out.execute('PRAGMA journal_mode=OFF');out.executescript(RESULT_SQL)
    stats=Counter()
    try:simulate_range(data,a,out,structures,policies,hedges,stats,progress=False);out.commit()
    finally:out.close();data.close()
    return dict(stats)


def parallel_months(a,data,out,structures,policies,hedges,stats):
    import multiprocessing as mp,tempfile
    with tempfile.TemporaryDirectory(dir=Path(a.out).resolve().parent) as tmp:
        jobs=[(a,r,str(Path(tmp)/f'{r[0]}.sqlite'),structures,policies,hedges,(data.special_sessions,data.holidays,data.events)) for r in month_ranges(a.start,a.end)]
        with mp.get_context('fork' if sys.platform.startswith('linux') else 'spawn').Pool(a.workers) as pool:
            for job,result in zip(jobs,pool.imap(shard_month,jobs)):
                stats.update(result)
                out.execute('ATTACH DATABASE ? AS shard',(job[2],))
                for table in ('trades','marks','fills'):out.execute(f'INSERT INTO {table} SELECT * FROM shard.{table}')
                out.commit();out.execute('DETACH DATABASE shard');Path(job[2]).unlink()
                print('Month done:',job[1][0],'candidates so far',stats['candidate_signals'],flush=True)


def simulate_range(data,a,out,structures,policies,hedges,stats,progress=True):
    n=0
    skew_cache={};memo={};memo_day=None
    def smile_features(day,expiry):
        """(put skew slope, 25-delta butterfly) from the 09:45 chain; None when wings are thin."""
        key=(day,expiry)
        if key not in skew_cache:
            greeks={'PE':[],'CE':[]}
            for (strike,cp),q in data.chain(a.provider,day,'09:45',expiry).items():
                if not is_quote(q,volume=a.min_signal_volume):continue
                gr=data.greek(a.provider,day,'09:45',expiry,strike,cp,a.rate,a.min_pair_volume,a.max_parity_bps)
                if gr and gr.get('iv') and gr.get('delta_proxy') is not None:
                    greeks[cp].append((abs(gr['delta_proxy']),gr['iv']))
            def near(cp,target):
                if not greeks[cp]:return None
                best=min(greeks[cp],key=lambda item:abs(item[0]-target))
                return best[1] if abs(best[0]-target)<=.10 else None
            p25,p50,c25=near('PE',.25),near('PE',.50),near('CE',.25)
            skew=(p25-p50)/p50 if p25 and p50 else None
            bf25=(c25+p25)/2-p50 if c25 and p25 and p50 else None
            skew_cache[key]=(skew,bf25)
        return skew_cache[key]
    for cand in generate(data,a,stats):
        cand['skew_slope'],cand['bf25_short']=smile_features(cand['day'],cand['short_expiry'])
        n+=1;stats['candidate_signals']+=1
        for structure in structures:
            for policy in policies:
                for hedge in hedges:
                    if structure=='double' and policy not in ('weekly','p25_s50','p50_s100')+TM1_EXITS:continue
                    if (structure=='iron_fly')!=bool(cand.get('is_fly')):continue   # each structure runs on its own candidates
                    if structure=='iron_fly' and policy not in ('weekly','p25_s50','p50_s100')+TM1_EXITS:continue
                    if structure=='double' and hedge=='gamma_reduced_delta_proxy':
                        stats['gamma_structure_incompatible']+=1;continue
                    if policy in ('atm_weekly_roll','rolling_tm1') and structure!='put':continue
                    if policy=='rolling_recenter' and cand.get('long_strike_offset'):
                        stats['recenter_not_defined_for_diagonal']+=1;continue
                    if hedge!='none' and policy not in WEEKLY_FAMILY:
                        stats['rolling_hedge_not_supported']+=1;continue
                    ident=hashlib.sha256(json.dumps([cand['day'],cand['short_expiry'],cand['long_expiry'],
                        cand['strike'],cand['strike_step'],cand['strike_value'],cand['target_tenor'],policy,structure,hedge]+([cand['long_strike_offset']] if cand.get('long_strike_offset') else [])).encode()).hexdigest()[:32]
                    if cand['day']!=memo_day:memo.clear();memo_day=cand['day']
                    outcome,why,reused=simulate_once(memo,data,a,cand,policy,hedge,structure)
                    stats['simulations_reused_for_aliases']+=reused
                    if not outcome:stats[why]+=1;continue
                    result,marks=outcome
                    rv=data.rv.get(result['day'],{}).get('rv5_close')
                    iv_rv=result['iv_short']/rv if rv and rv>1e-9 else None
                    rvs=data.rv.get(result['day'],{})
                    over=lambda x:result['iv_short']/x if x and x>0 else None
                    biz_short=session_time_iv(result['iv_short'],result['day'],result['short_expiry'],data.holidays)
                    biz_long=session_time_iv(result['iv_long'],result['day'],result['long_expiry'],data.holidays)
                    iv_biz_ratio=biz_short/biz_long if biz_short and biz_long else None
                    out.execute('INSERT INTO trades VALUES('+','.join('?'*66)+')',(
                        ident,result['day'],result['exit_day'],result['status'],result['provider'],
                        result['strike_mode'],result['strike_step'],result['strike_value'],
                        result['target_tenor'],hedge,policy,structure,result['short_dte_bucket'],
                        result['iv_ratio'],result['forward_iv_ratio'],
                        *(result.get(f'iv_{x}') for x in (5,15,30,45,60,75,90,120)),
                        rv,data.rv.get(result['day'],{}).get('rv5_parkinson'),iv_rv,
                        result['iv_short']/data.rv[result['day']]['rv5_parkinson'] if data.rv.get(result['day'],{}).get('rv5_parkinson') and data.rv[result['day']]['rv5_parkinson']>0 else None,
                        cand.get('skew_slope'),
                        rvs.get('rv5_yz'),over(rvs.get('rv5_yz')),rvs.get('gap_share_5d'),rvs.get('rv5_1m_range_proxy'),over(rvs.get('rv5_1m_range_proxy')),
                        rvs.get('rv5_1m_closeclose'),over(rvs.get('rv5_1m_closeclose')),result['lot_units'],
                        cand.get('bf25_short'),result.get('var_carry_daily'),
                        result.get('long_strike',result['strike']),result.get('long_strike_offset',0.),
                        iv_biz_ratio,biz_short/rvs['rv5_yz'] if biz_short and rvs.get('rv5_yz') else None,
                        *(data.trend.get(result['day'],{}).get(x) for x in ('spot_to_sma50','spot_to_sma100','spot_to_sma200','trend_21d','pct_below_52w_high')),
                        result['pnl_inr'],result['initial_debit_inr'],
                        max((m['capital_scenario'] for m in marks),default=0),
                        max(0,result['initial_debit_inr'],result['fly_width']*result['lot_units'] if result.get('fly_width') else a.capital_scenario_pct*result['short_f']*result['lot_units']*(2 if structure=='double' else 1)),
                        result['short_sales'],
                        sum(v for k,v in result['missing_marks'].items() if k.startswith('mark_') and k!='mark_traded_bar'),
                        result['execution_class'],json.dumps(result,default=str),hashlib.sha256(json.dumps(economic_key(cand,policy,hedge,structure,a),default=str).encode()).hexdigest()[:32],
                        scheduled_exit_day(cand,policy,a),result['short_sessions'],dt.date.fromisoformat(result['day']).weekday(),dt.date.fromisoformat(result['short_expiry']).weekday(),
                        result['iv_short']*result['short_f']*math.sqrt(max(years(result['short_expiry'],result['day'],'09:45'),1e-6)),result.get('events_in_short',0),path_digest(result),contract_digest(result),mtm_digest(marks),result.get('tm1_outcome')))
                    out.executemany('INSERT INTO marks VALUES(?,?,?,?,?,?,?,?)',((ident,m['day'],m['pnl'],
                        m['lower'],m['upper'],m['quality'],m['capital_scenario'],json.dumps(m['mark_details'])) for m in marks))
                    out.executemany('INSERT INTO fills VALUES(?,?,?,?,?)',((ident,i,fill.get('day'),fill['event'],json.dumps(fill))
                        for i,fill in enumerate(result['ledger'])))
                    stats['status_'+result['status']]+=1
                    for name,value in result['missing_marks'].items():
                        if name.startswith('tm1_'):stats[name]+=value
                    for name in ('stop_unobservable_checkpoints','entry_day_later_incomplete','fills_on_zero_volume_bar','exercise_fee_assumed','settled_after_missing_expiry_session','marks_without_1529_spot','recenter_fell_back_to_plain_roll'):
                        if result['missing_marks'].get(name):
                            stats['trades_with_'+name]+=1;stats['pnl_inr_on_trades_with_'+name]+=int(result['pnl_inr'] or 0)
        if n%25==0:
            out.commit()
            if progress:print('Candidate signals:',n,'Recorded:',sum(v for k,v in stats.items() if k.startswith('status_')),flush=True)
        if a.max_candidates and n>=a.max_candidates:break

if __name__=='__main__':main()
