#!/usr/bin/env python3
"""Path engine: simulate every possible trade ONCE and record its whole bar-by-bar life, so that every exit rule
(take-profit x stop-loss, see nifty-zone-grid) can be applied afterwards exactly, without re-simulating.

A path is (signal bar, side, structure, strike offset). It runs from its entry fill to its natural end:
  zone change (all structures); naked short: expiry (cash settled on the 15:00-15:29 spot TWAP);
  calendar: at 15:15 on the short expiry, or when the long reaches 30 days, the pair is closed and a new pair opened
  at the current index while the zone holds; futures_zone: zone change only; futures_expiry: also the nearest
  expiry at least --min-short-days away.
At every 45-minute bar close of its life the path stores (all in rupees at --lots):
  cash_total  cash flows so far (entries, roll exits, roll re-entries)
  cash_pair   cash flows of the pair now held (base for take-profit and premium stops)
  base        credit (naked) or debit (calendar) of the pair now held
  value       bar-close value of the open legs if every leg printed with volume in the bar's last minute, else NaN
  mark        cash_total plus open legs at their latest known close (boundary valuation, daily mark-to-market)
  exit_cash   cash from closing everything if the exit is decided at this bar close (minute fills, see below)
  exit_bar    bar whose following minutes completed that exit (a later signal can only be taken after it)
  exit_day    day of the last exit fill (date ordinal); exit_forced: a long was booked at a modelled price
  roll        1 on a calendar roll bar (a take-profit cannot fire there; a stop can, and then no new pair opens)
  forced_before  number of earlier roll exits in this path that needed a forced long booking
A roll exit that spills past its bar (thin long) completes first; the new pair opens on the bar it completed if the
zone still holds (as nifty-zone-backtest does); bars in between hold no legs. If the zone changes meanwhile, no new pair
opens and the path ends when the old pair's last fill happens.
Bar convention: a decision at bar k fills in the minutes after bar k's close. end_bar / exit_bar = the bar after whose
close the last fill happens (it holds the one-position lock); end_fill_day = the date of that last fill.

Fills (decided at a bar close, executed in the next bar's minutes; 09:15-09:59 next session after the last bar):
  every leg is sliced minute by minute, taking at most --max-participation of that minute's traded volume, in whole
  lots, at the minute's (open+high+low+close)/4; slippage (tenor and volume scaled) and dated statutory charges are
  applied to each leg's total. V4.9.7: a first entry may use the windows of --entry-bars bars (default 3) and must reach its
  full size (V4.9.12: and only the first --entry-minutes trading minutes after the signal bar's close, default 30, continuing into
  the next session's opening minutes if the day ends first) (ceil(--target-fraction x margin-table lots) with --target-margin), else it is not taken; a roll re-entry must complete
  inside its bar. An exit buys shorts back
  before selling longs; whatever is left carries into later bars; a short reaching expiry settles; a long still open
  --max-exit-lag-bars after its shorts are gone is booked at its last close less --forced-exit-haircut (flagged).
  A minute's own volume is used for that minute (user's decision).
Futures paths use NIFTY spot minutes as the price (no futures volume or basis data): first minute open of the next bar
  plus --slippage-points, dated futures charges.
"""
from __future__ import annotations
import argparse,bisect,collections,datetime as dt,json,math,os,sqlite3,sys,time
from pathlib import Path
from types import SimpleNamespace
import numpy as np;import nifty_iv
from nifty_iv.ivcal.calendar_research import fill_detail,hedge_cost
from nifty_iv.core.contract_specs import LotMaster
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.core import strategy_spec as ss
from nifty_iv.core import cli

OPTION_STRUCTURES=ss.NAKED_STRUCTURES+('calendar',);FUTURES_STRUCTURES=('futures_zone','futures_expiry')
SLOTS=8   # trading sessions of an exit whose closing state is stored
FLOATS=('cash_total','cash_pair','base','value','mark','exit_cash')+tuple(f'exit_slot_cash{i}' for i in range(SLOTS))+tuple(f'exit_slot_value{i}' for i in range(SLOTS))
INTS=('exit_bar','exit_day','exit_forced','roll','forced_before')+tuple(f'exit_slot_day{i}' for i in range(SLOTS))+('exit_sessions','exit_fill_days')
def ordinal(day):return dt.date.fromisoformat(day).toordinal()

def pack(rows):
    return b''.join([np.asarray(rows[n],dtype=np.float64).tobytes() for n in FLOATS]+[np.asarray(rows[n],dtype=np.int64).tobytes() for n in INTS])

def unpack(blob,n):
    out={};pos=0
    for name in FLOATS:out[name]=np.frombuffer(blob,dtype=np.float64,count=n,offset=pos);pos+=8*n
    for name in INTS:out[name]=np.frombuffer(blob,dtype=np.int64,count=n,offset=pos);pos+=8*n
    return out

class Path_:
    __slots__=('side','structure','offset','signal_bar','colour','lot','units','held','entry_px','short','long','cash_total','cash_pair','base','rows','head','pairs','wait','forced_rolls','pend','cancel','unwind','first')
    def __init__(self,**kw):
        for k,v in kw.items():setattr(self,k,v)

NMIN=zs.CLOSE-zs.OPEN   # minutes 09:15..15:29

FILL_PRICES=('ohlc4','adverse','open','adverse_buys')
def fill_prices(o,h,l,c,mode):
    """(buy price, sell price) arrays for a fill-price model: ohlc4 = (O+H+L+C)/4 (open if a field is missing);
    adverse = the minute's high for buys and low for sells (stress); open = the minute's open; adverse_buys (V4.9.12) = the
    minute's high for buys, ohlc4 for sells: for a naked short, conservative exits (buy-backs) and unchanged entries."""
    allpos=(o>0)&(h>0)&(l>0)&(c>0)
    if mode=='ohlc4':x=np.where(allpos,(o+h+l+c)/4,o);return x,x
    if mode=='adverse':return np.where(h>0,h,o),np.where(l>0,l,o)
    if mode=='open':return o,o
    if mode=='adverse_buys':x=np.where(allpos,(o+h+l+c)/4,o);return np.where(h>0,h,o),x
    raise ValueError(f'unknown fill price model {mode}')

class DayArrays:
    """One session of the extraction as numpy arrays per contract: buy and sell fill prices (see fill_prices),
    close and volume by minute index; a minute is tradable when open > 0 and volume > 0."""
    __slots__=('buy','sell','close','vol','px','high','low')
    def __init__(self,day_dict,mode='ohlc4'):
        acc={}
        for minute,quotes in day_dict.items():
            i=zs.minute_of(minute)-zs.OPEN
            if not 0<=i<NMIN:continue
            for key,(o,h,l,c,v,_) in quotes.items():
                a=acc.get(key)
                if a is None:a=acc[key]=np.zeros((5,NMIN))
                a[:,i]=(o or 0.,h or 0.,l or 0.,c or 0.,v or 0)
        self.buy={};self.sell={};self.close={};self.vol={};self.high={};self.low={}
        for k,a in acc.items():
            self.buy[k],self.sell[k]=fill_prices(a[0],a[1],a[2],a[3],mode);self.close[k]=a[3];self.vol[k]=np.where((a[0]>0)&(a[4]>0),a[4],0.)
            self.high[k]=a[1];self.low[k]=a[2]
        self.px=self.buy

def load_day(db,day,mode='ohlc4'):
    """DayArrays straight from the extraction: one SQL read, numpy throughout. Where both vendors printed a minute, the
    one with more volume is kept (ties: the first row read), exactly as zone_backtest.Market.day does."""
    rows=db.execute('SELECT minute,expiry,strike,cp,COALESCE(open,0),COALESCE(high,0),COALESCE(low,0),COALESCE(close,0),COALESCE(volume,0) FROM opt_1m WHERE day=? ORDER BY provider,minute',(day,)).fetchall()
    d=DayArrays.__new__(DayArrays)
    if not rows:d.px=d.buy={};d.sell={};d.close={};d.vol={};d.high={};d.low={};return d
    minute,e,k,cp,o,h,l,c,v=zip(*rows);kid={}
    ids=np.fromiter((kid.setdefault((x,float(y),z),len(kid)) for x,y,z in zip(e,k,cp)),np.int64,len(rows))
    mi=np.fromiter(((int(m[:2])*60+int(m[3:5]))-zs.OPEN for m in minute),np.int64,len(rows))
    o,h,l,c,v=(np.asarray(x,dtype=float) for x in (o,h,l,c,v))
    keep=(mi>=0)&(mi<NMIN);order=np.lexsort((np.arange(len(rows)),-v,mi,ids))
    order=order[keep[order]];first=np.ones(len(order),bool);first[1:]=(ids[order][1:]!=ids[order][:-1])|(mi[order][1:]!=mi[order][:-1])
    r=order[first];K=len(kid);pb=np.zeros((K,NMIN));ps=np.zeros((K,NMIN));cl=np.zeros((K,NMIN));vo=np.zeros((K,NMIN));hi=np.zeros((K,NMIN));lo=np.zeros((K,NMIN))
    b_,s_=fill_prices(o[r],h[r],l[r],c[r],mode);pb[ids[r],mi[r]]=b_;ps[ids[r],mi[r]]=s_;cl[ids[r],mi[r]]=c[r];hi[ids[r],mi[r]]=h[r];lo[ids[r],mi[r]]=l[r]
    vo[ids[r],mi[r]]=np.where((o[r]>0)&(v[r]>0),v[r],0.)
    d.buy={key:pb[i] for key,i in kid.items()};d.sell={key:ps[i] for key,i in kid.items()};d.px=d.buy
    d.close={key:cl[i] for key,i in kid.items()};d.vol={key:vo[i] for key,i in kid.items()}
    d.high={key:hi[i] for key,i in kid.items()};d.low={key:lo[i] for key,i in kid.items()}
    return d

class PathSim:
    def __init__(self,market,bars,final,cfg,minutes=None,cache_days=12):
        self.m=market;self.bars=bars;self.final=final;self.cfg=cfg;self.lots=LotMaster(cfg.contract_master);self.counts=collections.Counter()
        self.spot={}
        for d,rows in (minutes or {}).items():
            a=np.full(NMIN,np.nan)
            for m,o,h,l,c in rows:
                if 0<=m-zs.OPEN<NMIN and o:a[m-zs.OPEN]=o
            self.spot[d]=a
        self.lc={};self.gaps=[];self.memo={};self.done=[];self.live=[];self.days=collections.OrderedDict();self.cache_days=cache_days
        self.day_end={}
        for i,b in enumerate(bars):self.day_end[b.day]=i            # last bar index of each session
        self.session_days=sorted(self.day_end)
    def lots_for(self,lot,day=None):
        """Target exchange lots of an entry. --target-margin: ceil(--target-fraction x the whole lots whose upper-end margin
        (strategy_spec margin table) fits that budget on the fill day) (V4.9.7; fraction 1 = the V4.9.6 full account);
        --target-units: round(target / lot); else --lots."""
        tm=getattr(self.cfg,'target_margin',None)
        if tm and day:return ss.entry_lots(day,lot,tm,getattr(self.cfg,'target_fraction',1.))
        tu=getattr(self.cfg,'target_units',None)
        return max(1,int(round(tu/lot))) if tu else self.cfg.lots
    def arrays(self,day):
        d=self.days.get(day)
        if d is None:
            mode=getattr(self.cfg,'fill_price','ohlc4')
            d=self.days[day]=load_day(self.m.db,day,mode) if hasattr(self.m,'db') else DayArrays(self.m.day(day),mode)
            while len(self.days)>self.cache_days:self.days.popitem(last=False)
        return d
    def window(self,k):
        """(day, first minute index, end minute index) of the bar after k, where a decision at bar k's close fills."""
        b=self.bars[k]
        if b.end<zs.CLOSE:return b.day,b.end-zs.OPEN,min(b.end+45,zs.CLOSE)-zs.OPEN
        return (self.bars[k+1].day,0,45) if k+1<len(self.bars) else None
    def key(self,c):return (c[0],float(c[1]),c[2])
    def mark(self,day,minute,c):
        """(close, volume) at a minute, zeros if absent."""
        d=self.arrays(day);k=self.key(c);i=zs.minute_of(minute)-zs.OPEN
        cl=d.close.get(k)
        return (cl[i],d.vol[k][i]) if cl is not None else (0.,0.)
    def extracted(self,day,c):
        """Whether the contract has any minute that day in the extraction (False: a data gap, not illiquidity)."""
        return self.key(c) in self.arrays(day).px
    def slice(self,day,i0,i1,c,want,lot,buy=True):
        """Trade up to `want` units of c over minutes [i0,i1): each minute at most --max-participation of its volume, whole lots,
        at the buy or sell price of the fill-price model. Returns (units, notional, volume used, index of the last minute used or None)."""
        d=self.arrays(day);k=self.key(c);px=(d.buy if buy else d.sell).get(k)
        if px is None or i0>=i1 or want<=0:return 0,0.,0.,None
        vol=d.vol[k][i0:i1];cap=np.floor(vol*self.cfg.max_participation)//lot*lot;cs=np.cumsum(cap)
        total=int(cs[-1])
        if total<=0:return 0,0.,0.,None
        if total<want:j=int(np.flatnonzero(cap)[-1]);taken=cap
        else:
            j=int(np.searchsorted(cs,want));taken=cap[:j+1].copy();taken[j]=want-(cs[j-1] if j else 0.)
        n=int(taken.sum());p=px[i0:i0+len(taken)]
        return n,float(taken@p),float(vol[:len(taken)][taken>0].sum()),i0+j
    def book(self,acc,c,trade_sign,lot):
        notional,qty,vol,day=acc
        if not qty:return 0.
        cash,_,_,_=fill_detail(notional/qty,trade_sign*qty,day,self.cfg,c[0],vol/lot,None,None,lot)
        return cash
    def settle_px(self,c):
        ref=self.m.settlement(c[0])
        if ref is None:return None
        s=ref[0];return max(0.,s-c[1]) if c[2]=='CE' else max(0.,c[1]-s)
    def last_close(self,c,k,t):
        """Latest bar-close price of c at or before bar t, known from bar k's state plus bars k+1..t."""
        for j in range(t,k,-1):
            b=self.bars[j];cl,_=self.mark(b.day,b.mark_minute,c)
            if cl:return cl
        return self.lc.get(c)
    # ---------- entry ----------
    def caps(self,day,i0,i1,c,lot,buy):
        """Per-minute tradable units of c over [i0,i1) (at most --max-participation of each minute's volume, whole lots), the fill
        price array and the volume array; None if c has no price that day."""
        d=self.arrays(day);k=self.key(c);px=(d.buy if buy else d.sell).get(k)
        if px is None or i0>=i1:return None
        vol=d.vol[k][i0:i1];return np.floor(vol*self.cfg.max_participation)//lot*lot,px[i0:i1],vol
    def enter(self,k,legs,lot,start=None,need=None,bars=1,colour=None,minutes=None):
        """legs: [(contract, signed units)], all at the same size, filled in the windows of bars k, k+1, .., k+bars-1 (a window = the
        minutes of the bar after it), from minute index `start` in the first window if given (after an exit that used earlier minutes
        of the same bar). All legs fill together, minute by minute, in equal whole lots: each minute trades the smallest of the legs'
        tradable quantities, so no leg is ever held without the others. Full size or nothing (V4.9.7): the entry completes when the
        matched quantity reaches the target (`need`, default the target); short of it after the last window nothing is traded. With
        colour, a window after the first is used only if the zone at that bar's close is still `colour` (else zone_flip_before_fill).
        minutes (V4.9.12, --entry-minutes): at most this many trading minutes, counted from the first minute after the signal bar's
        close and running on into the next windows (the next session's opening minutes after the day's last bar), are used.
        Returns dict(status='ok'|'none', ...): ok carries bar (bar whose window completed it), day (first fill day), last_day; none
        carries reason, limit='long'|'short' (the thinner leg) and until (the last bar whose window was tried)."""
        want=abs(legs[0][1]);need=want if need is None else need;got=0;pieces=[];tot=collections.Counter();last=k;budget=minutes or None
        for j in range(bars):
            kk=k+j
            if j and kk>=len(self.bars):break
            if j and colour is not None and self.final[kk]!=colour:return dict(status='none',reason='zone_flip_before_fill',limit=None,until=kk)
            w=self.window(kk)
            if w is None:
                if not j:return dict(status='none',reason='no_next_bar',limit=None,until=k)
                break
            day,i0,i1=w;last=kk
            if start is not None and not j:
                sday,smin=start
                if sday==day:i0=max(i0,smin)                  # same window as the previous exit's last fill: only later minutes
            if budget is not None:
                if budget<=0:break
                i1=min(i1,i0+budget);budget-=max(0,i1-i0)
            if i0>=i1:continue
            cs=[self.caps(day,i0,i1,c,lot,u>0) for c,u in legs]
            if any(x is None for x in cs):continue           # a leg without a price that day: nothing tradable in this window
            for (c,_),x in zip(legs,cs):tot[c]+=float(x[0].sum())
            cap=np.min(np.vstack([x[0] for x in cs]),axis=0);cum=np.cumsum(cap);avail=int(cum[-1]) if len(cum) else 0
            if not avail:continue
            left=want-got
            if avail<left:taken=cap;m=int(np.flatnonzero(cap)[-1])
            else:
                m=int(np.searchsorted(cum,left));taken=cap[:m+1].copy();taken[m]=left-(cum[m-1] if m else 0.)
            pieces.append((kk,day,i0,m,taken,cs));got+=int(taken.sum())
            if got>=want:break
        if got<need or not pieces:
            j=int(np.argmin([tot[c] for c,_ in legs])) if tot else 0
            return dict(status='none',reason='no_liquid_entry',limit='long' if legs[j][1]>0 else 'short',until=last)
        cash=0.;px={};filled=[];notional=collections.Counter()
        for kk,day,i0,m,taken,cs in pieces:                  # each window booked on its own day, with that day's costs
            n=int(taken.sum())
            for (c,u),(_,p,vol) in zip(legs,cs):
                nt=float(taken@p[:len(taken)]);used=float(vol[:len(taken)][taken>0].sum());notional[c]+=nt
                cash+=self.book([nt,n,used,day],c,1 if u>0 else -1,lot)
        for c,u in legs:px[c]=notional[c]/got;filled.append((c,got if u>0 else -got))
        kk,day,i0,m,_,_=pieces[-1]
        return dict(cash=cash,px=px,day=pieces[0][1],last_day=day,bar=kk,ix=self.index_at(day,i0,i0+m+1),filled=tuple(filled),units=got,status='ok',windows=len(pieces))
    def index_at(self,day,i0,i1):
        """Spot at the last printed minute in [i0,i1) (the minute the entry completed), NaN if none."""
        a=self.spot.get(day)
        if a is None:return math.nan
        ok=np.flatnonzero(~np.isnan(a[i0:i1]));return float(a[i0+ok[-1]]) if len(ok) else math.nan
    # ---------- exit, memoised on (held, bar) ----------
    def exit(self,held,k,lot):
        """Close `held` by a decision at bar k. Every fill window is booked on its own (price, quantity, dated costs) as a dated
        event; settlements and forced bookings are events too. Returns (cash, exit bar, last fill day, forced, ok, cash by bar,
        legs left by bar, (day, minute) of the last fill, day slots). Memoised on (held, bar, lot)."""
        key=(held,k,lot)
        if key in self.memo:return self.memo[key]
        rem={c:u for c,u in held};cash=0.;last=self.bars[k].day;forced=False;pending=0;t=k;ok=True;lastt=None
        sched=collections.defaultdict(float);rems=[];lastmin=-1;events=[]          # events: (day, cash, contract, signed units closed)
        while rem:
            if t>=len(self.bars):ok=None;t-=1;break                          # data ran out: no value, path stays open
            b=self.bars[t]
            for c in list(rem):
                if c[0]<b.day or (c[0]==b.day and b.end==zs.CLOSE):
                    px=self.settle_px(c)
                    if px is None:ok=None if c[0]>self.bars[-1].day else False;break   # False: settlement price missing
                    u=rem.pop(c);x=u*px;cash+=x;sched[t]+=x;last=max(last,c[0]);events.append((c[0],x,c,u))
            if not ok:break
            w=self.window(t)
            if rem and w:
                day,i0,i1=w;start=i0;shorts_done=True
                def fill(c,lo,buy):
                    nonlocal cash,last,lastmin,lastt
                    want=-rem[c] if rem[c]<0 else rem[c]
                    n,notional,vol,j=self.slice(day,lo,i1,c,want,lot,buy=buy)
                    if n:
                        x=self.book([notional,n,vol,day],c,1 if buy else -1,lot)      # this window's fills, booked now with today's costs
                        q=-n if rem[c]<0 else n;rem[c]-=q;cash+=x;sched[t+1]+=x;last=max(last,day);events.append((day,x,c,q))
                        lastmin=max(lastmin if lastt==t else -1,j);lastt=t
                    return j
                for c in [c for c,u in rem.items() if u<0 and c[0]>=day]:          # shorts first, from the first minute
                    j=fill(c,i0,True)
                    if rem[c]==0:del rem[c];start=max(start,j)
                    else:shorts_done=False
                if shorts_done and not any(u<0 for u in rem.values()):                # longs from the minute the last short finished
                    for c in [c for c,u in rem.items() if u>0 and c[0]>=day]:
                        fill(c,start,False)
                        if rem[c]==0:del rem[c]
            rems.append((t+1,tuple(rem.items())))
            if not rem:break
            pending+=1
            if 0<=self.cfg.max_exit_lag_bars<pending and not any(u<0 for u in rem.values()):
                fday=self.bars[min(t+1,len(self.bars)-1)].day
                for c in list(rem):
                    px=self.last_close(c,k,t)
                    if px is None:continue
                    u=rem.pop(c);x=u*px*(1-self.cfg.forced_exit_haircut);cash+=x;sched[t+1]+=x;forced=True;events.append((fday,x,c,u))
                rems.append((t+1,tuple(rem.items())))
                if not rem:break
            t+=1
        if lastt!=t:lastmin=-1                                   # finished by settlement or forced booking: no minute used in bar t's window
        last_fill=(self.window(t)[0],lastmin) if lastmin>=0 and self.window(t) else None
        slots=self.day_slots(held,k,events,ok)
        out=(cash if ok else math.nan,t,ordinal(last),forced,ok,tuple(sorted(sched.items())),tuple(rems),last_fill,slots)   # ok: True, False (unresolved) or None (data end)
        self.memo[key]=out;return out
    def day_slots(self,held,k,events,ok):
        """State of an exit at the close of every trading session from its first possible fill day to its completion (the first
        SLOTS sessions), whether or not it filled that day: cash received so far (actual dated fills and costs) and the value of
        the legs still open at that session's last close (NaN if a leg has no known close: unpriceable).
        Returns (session days as ordinals, cash, value, number of sessions, number of days with fills)."""
        none=((10**9,)*SLOTS,(math.nan,)*SLOTS,(math.nan,)*SLOTS,0,0)
        w=self.window(k)
        if not ok or not events or w is None:return none
        first=w[0];last=max(d for d,_,_,_ in events);fill_days=len({d for d,_,_,_ in events})
        i0=bisect.bisect_left(self.session_days,first);i1=bisect.bisect_right(self.session_days,last)
        days=self.session_days[i0:i1] or [last];n=len(days)
        if n>1:self.counts['exit_simulations_over_several_sessions']+=1
        if fill_days>1:self.counts['exit_simulations_with_several_fill_days']+=1
        if n>SLOTS:self.counts['exit_simulations_beyond_session_slots']+=1
        D=[];Cs=[];Vs=[]
        for d in days[:SLOTS]:
            cash=sum(x for dd,x,_,_ in events if dd<=d);left={c:u for c,u in held}
            for dd,_,c,q in events:
                if dd<=d:left[c]-=q
            end=self.day_end.get(d,k);value=0.
            for c,u in left.items():
                if not u:continue
                cl=self.last_close(c,k,end) if end>k else self.lc.get(c)
                if not cl:value=math.nan;self.counts['exit_session_slot_unpriced']+=1;break
                value+=u*cl
            D.append(ordinal(d));Cs.append(cash);Vs.append(value)
        pad=SLOTS-len(D)
        return (tuple(D)+(10**9,)*pad,tuple(Cs)+(math.nan,)*pad,tuple(Vs)+(math.nan,)*pad,n,fill_days)
    # ---------- option pairs ----------
    def open_pair(self,k,side,structure,offset,start=None,size=None):
        """Open a short (naked) or short+long (calendar) at the bar after k. size: the path's units on a roll reopen (all of it or
        nothing, so a path's size never changes); None on a first entry (target --lots lots, at least one lot)."""
        b=self.bars[k];cp='PE' if side=='bull' else 'CE';levels=getattr(self.cfg,'offset_mode','points')=='levels'
        strike=ss.strike_for(side,b.close,offset) if levels else float(round(b.close/100)*100+offset);w=self.window(k)
        if w is None:return None,'no_next_bar'
        day=w[0];exps=self.m.expiries(b.day,b.mark_minute)
        short,why=ss.select_short_expiry(structure,day,exps,self.cfg.min_short_days)          # naked_m1 / naked_m2: monthly policies (V4.9.6)
        if short is None:return None,why
        lot=self.lots.get(day,short).qty
        if not lot:return None,'lot_unknown'
        if size and size%lot:return None,'lot_changed'        # a roll after a lot-size change cannot keep the path's size
        units=size or lot*self.lots_for(lot,day);legs=[((short,strike,cp),-units)];long=None
        g=dict(day=day,signal_bar=k,side=side,structure=structure,offset=offset,cp=cp,strike=strike,atm=ss.atm(b.close) if levels else strike-offset,roll=int(size is not None))
        if structure=='calendar':
            far=[e for e in exps if e>short and zb.days_between(day,e)>30]
            if not far:
                self.gap(g,'no_long_expiry','long',None,max((zb.days_between(day,e) for e in exps),default=None));return None,'no_long_expiry'
            long=min(far,key=lambda e:(abs(zb.days_between(day,e)-self.cfg.long_target),e))
            if self.lots.get(day,long).qty!=lot:return None,'lot_mismatch'
            legs.append(((long,strike,cp),units))
        missing=[c for c,_ in legs if not self.extracted(day,c)]
        if missing:                                   # a contract absent from the extraction is a data gap, not a failed fill: do not trade
            if len(missing)<len(legs):self.counts['not_extracted_one_leg_'+('long' if dict(legs)[missing[0]]>0 else 'short')]+=1
            for c in missing:self.gap(g,'not_extracted','long' if dict(legs)[c]>0 else 'short',c[0],zb.days_between(day,c[0]))
            return None,'not_extracted'
        first=size is None
        r=self.enter(k,legs,lot,start,need=size,bars=self.cfg.entry_bars if first else 1,colour=('G' if side=='bull' else 'R') if first else None,
                     minutes=getattr(self.cfg,'entry_minutes',None) if first else None)
        if r['status']=='none':
            self.attempt_end=r.get('until',k)
            if r.get('limit'):self.counts['no_liquid_entry_limited_by_'+r['limit']]+=1
            return None,r.get('reason','no_liquid_entry')
        if r.get('windows',1)>1:self.counts[f"entry_completed_in_window_{r['bar']-k+1}"]+=1
        for c,p in r['px'].items():self.lc[c]=p
        if r['units']<units:self.counts['entry_reduced']+=1;self.counts[f"entry_reduced_to_{10*r['units']//units}0pct"]+=1
        cash=r['cash'];base=cash if ss.is_naked(structure) else -cash
        return dict(held=r['filled'],px=r['px'],lot=lot,units=r['units'],short=short,long=long,day=r['day'],cash=cash,base=base,strike=strike,entry_index=r['ix'],
                    partial=False,short_px=r['px'].get(legs[0][0],math.nan),bar=r['bar']),None
    def gap(self,g,event,leg,expiry,dte):
        """Record a data gap (missing long expiry, contract absent from the extraction) for nifty-zone-coverage."""
        self.gaps.append(dict(g,event=event,leg=leg,expiry=expiry,dte=dte))
    def start(self,t,side,structure,offset):
        self.attempt_end=t;pair,why=self.open_pair(t,side,structure,offset)
        if pair is None:self.counts['reject_'+why]+=1;self.done.append(dict(side=side,structure=structure,offset=offset,signal_bar=t,status='no_fill',reason=why,end_bar=self.attempt_end));return
        p=Path_(side=side,structure=structure,offset=offset,signal_bar=t,colour='G' if side=='bull' else 'R',lot=pair['lot'],units=pair['units'],held=pair['held'],entry_px=pair['px'],
                short=pair['short'],long=pair['long'],cash_total=pair['cash'],cash_pair=pair['cash'],base=pair['base'],rows=collections.defaultdict(list),pairs=1,wait=None,forced_rolls=0,pend=None,cancel=False,unwind=pair['partial'],first=pair['bar']+1,
                head=dict(entry_day=pair['day'],lot=pair['lot'],units=pair['units'],short_expiry=pair['short'],long_expiry=pair['long'],strike=pair['strike'],entry_index=pair['entry_index'],entry_short_px=pair['short_px']))
        self.live.append(p)
    NO_SLOTS=((10**9,)*SLOTS,(math.nan,)*SLOTS,(math.nan,)*SLOTS,0,0)
    def record(self,p,vals,ints,slots=None):
        d,c,v,n,f=slots or self.NO_SLOTS
        for name,x in zip(FLOATS,vals+c+v):p.rows[name].append(x)
        for name,x in zip(INTS,ints+(p.forced_rolls,)+d+(n,f)):p.rows[name].append(x)
    def step(self,p,k):
        """Record bar k for a live option path. Returns True when the path has ended."""
        b=self.bars[k]
        if p.wait is not None:                                   # between pairs: the roll exit is still filling
            q=p.pend;asof=q['before']+sum(x for bar,x in q['sched'] if bar<=k)
            left=next((r for bar,r in reversed(q['rems']) if bar<=k),q['held']);stale=0.
            for c,u in left:
                cl,_=self.mark(b.day,b.mark_minute,c)
                if cl:self.lc[c]=cl
                stale+=u*self.lc.get(c,q['px'][c])
            # as of this close: cash already received plus legs still to sell at their last close; an exit decided now ends when the roll exit does
            self.record(p,(asof,0.,math.nan,math.nan,asof+stale,p.cash_total-asof),(max(k,p.wait),q['day'],int(q['forced']),0))
            if self.final[k]!=p.colour:p.cancel=True              # no new pair; the old one is still being sold
            if k<p.wait:return False
            if p.cancel:return self.finish(p,k,'zone_flip',p.cash_total)    # ends on the bar its last fill completes
            return self.reopen(p,k,self.after(q['last_fill']))
        value=0.;fresh=True;stale=0.
        for c,u in p.held:
            cl,v=self.mark(b.day,b.mark_minute,c)
            if cl:self.lc[c]=cl
            if not cl or not v:fresh=False
            else:value+=u*cl
            stale+=u*self.lc.get(c,p.entry_px[c])
        end=None
        if self.final[k]!=p.colour:end='zone_flip'
        elif ss.is_naked(p.structure) and (b.day>p.short or (b.day==p.short and b.end==zs.CLOSE)):end='expiry'
        roll=p.structure=='calendar' and end is None and ((b.day>=p.short and (b.end>=zb.ROLL_BAR_END or b.day>p.short)) or zb.days_between(b.day,p.long)<=30)
        x_cash,x_bar,x_day,x_forced,ok,sched,rems,last_fill,slots=self.exit(p.held,k,p.lot)
        mark=p.cash_total+stale
        self.record(p,(p.cash_total,p.cash_pair,p.base,value if fresh else math.nan,mark,x_cash),(x_bar,x_day,int(x_forced),int(bool(roll))),slots)
        if p.unwind:end=('roll_reopen_' if p.pairs>1 else '')+'partial_entry_unwound'   # one leg filled, the other not: sell it back at once
        if ok is False:return self.finish(p,k,'unresolved',math.nan)
        if ok is None and (end or roll):return self.finish(p,k,'open_at_data_end',mark)
        if end:return self.finish(p,k,end,p.cash_total+x_cash)
        if roll:
            p.pend=dict(before=p.cash_total,sched=sched,rems=rems,held=p.held,px=p.entry_px,day=x_day,forced=x_forced,last_fill=last_fill)
            p.cash_total+=x_cash;p.held=();p.cash_pair=0.;p.base=math.nan;p.forced_rolls+=int(x_forced)
            if x_bar==k:return self.reopen(p,k,self.after(last_fill))   # the new pair fills only after the old pair's last exit minute
            p.wait=x_bar;self.counts['roll_exit_spilled']+=1
        return False
    def after(self,last_fill):
        """(day, first minute index) a new pair may use after an exit whose last fill was at last_fill=(day, minute index)."""
        if last_fill is None or not self.cfg.roll_entry_after_exit:return None
        return (last_fill[0],last_fill[1]+1)
    def reopen(self,p,k,start=None):
        p.wait=None;new,why=self.open_pair(k,p.side,p.structure,p.offset,start,size=p.units)
        if new is None:self.counts['roll_reopen_'+why]+=1;return self.finish(p,k,'roll_reopen_failed',p.cash_total)
        p.cash_total+=new['cash'];p.cash_pair=new['cash'];p.base=new['base'];p.held=new['held'];p.entry_px=new['px'];p.short=new['short'];p.long=new['long'];p.pairs+=1
        p.unwind=new['partial']
        return False
    def finish(self,p,k,reason,pnl):
        status='closed' if reason not in ('open_at_data_end','unresolved') else reason
        last=p.rows['exit_day'][-1] if p.rows['exit_day'] else None
        self.done.append(dict(p.head,side=p.side,structure=p.structure,offset=p.offset,signal_bar=p.signal_bar,status=status,reason=reason,pairs=p.pairs,
                              first_bar=p.first,end_bar=k,end_pnl=pnl,end_fill_day=dt.date.fromordinal(int(last)).isoformat() if last and last<10**8 else None,rows=p.rows))
        self.counts['end_'+reason]+=1;return True
    # ---------- futures (spot price proxy) ----------
    def fut_fill(self,k):
        w=self.window(k)
        if w is None:return None
        day,i0,i1=w;a=self.spot.get(day)
        if a is None:return None
        seg=a[i0:i1];ok=np.flatnonzero(~np.isnan(seg))
        return (day,float(seg[ok[0]])) if len(ok) else None
    def fut_exit(self,k):
        """First executable exit for a decision at bar k: the next bar with a spot print, however late. None: data ran out."""
        for t in range(k,len(self.bars)):
            f=self.fut_fill(t)
            if f:return f[0],f[1],t
        return None
    def fut_cost(self,units,price,day):return hedge_cost(units,price,day,self.cfg)+abs(units)*self.cfg.slippage_points
    def futures_path(self,t,side,horizon):
        f=self.fut_fill(t);b=self.bars[t]
        if f is None:return self.done.append(dict(side=side,structure=horizon,offset=0.,signal_bar=t,status='no_fill',reason='no_next_bar'))
        day,px=f
        expiry=next((e for e in self.m.expiries(b.day,b.mark_minute) if zb.days_between(day,e)>=self.cfg.min_short_days),None)
        if horizon=='futures_expiry' and expiry is None:return self.done.append(dict(side=side,structure=horizon,offset=0.,signal_bar=t,status='no_fill',reason='no_short_expiry'))
        lot=self.lots.get(day,expiry or (dt.date.fromisoformat(day)+dt.timedelta(days=7)).isoformat()).qty
        if not lot:   # lot size not determinable (e.g. the mid-2021 transition): rejected, as nifty-zone-legacy-futures does
            self.counts['reject_futures_lot_unknown']+=1
            return self.done.append(dict(side=side,structure=horizon,offset=0.,signal_bar=t,status='no_fill',reason='lot_unknown'))
        units=lot*self.lots_for(lot,day)*(1 if side=='bull' else -1);cash=-units*px-self.fut_cost(units,px,day);colour='G' if side=='bull' else 'R'
        rows=collections.defaultdict(list);head=dict(entry_day=day,lot=lot,units=abs(units),short_expiry=expiry,long_expiry=None,strike=px,entry_index=px)
        for k in range(t+1,len(self.bars)):
            b=self.bars[k];value=units*b.close;end=None
            if self.final[k]!=colour:end='zone_flip'
            elif horizon=='futures_expiry' and (b.day>expiry or (b.day==expiry and b.end>=zb.ROLL_BAR_END)):end='expiry'
            f=self.fut_exit(k)
            if f:xd,xp,xb=f;x_cash=units*xp-self.fut_cost(-units,xp,xd)
            else:xd,x_cash,xb=self.bars[-1].day,math.nan,len(self.bars)-1          # no print before the data ends: no exit value
            for name,v in zip(FLOATS,(cash,cash,math.nan,value,cash+value,x_cash)+(math.nan,)*(2*SLOTS)):rows[name].append(v)
            for name,v in zip(INTS,(xb,ordinal(xd),0,0,0)+(10**9,)*SLOTS+(0,0)):rows[name].append(v)   # one fill: no partial state to store
            if end and not f:return self.done.append(dict(head,side=side,structure=horizon,offset=0.,signal_bar=t,status='open_at_data_end',reason='open_at_data_end',pairs=1,first_bar=t+1,end_bar=k,end_pnl=cash+value,rows=rows))
            if end:return self.done.append(dict(head,side=side,structure=horizon,offset=0.,signal_bar=t,status='closed',reason=end,pairs=1,first_bar=t+1,end_bar=k,end_pnl=cash+x_cash,rows=rows))
        self.done.append(dict(head,side=side,structure=horizon,offset=0.,signal_bar=t,status='open_at_data_end',reason='open_at_data_end',pairs=1,first_bar=t+1,end_bar=len(self.bars)-1,
                              end_pnl=rows['mark'][-1] if rows['mark'] else math.nan,rows=rows))
    # ---------- driver ----------
    def run(self,entries,structures,offsets,sink=None,progress=True,heartbeat=60.):
        month=None;t0=time.time();n=0;beat=t0;N=len(self.bars);k0=0
        for k,b in enumerate(self.bars):
            nxt=[]
            for p in self.live:
                if k<p.first or not self.step(p,k):nxt.append(p)          # an entry still filling (V4.9.7: up to --entry-bars windows) is not stepped
            self.live=nxt
            for side in entries.get(k,()):
                for s in structures:
                    if s in FUTURES_STRUCTURES:self.futures_path(k,side,s);continue
                    for off in offsets:self.start(k,side,s,off)
            for key in [x for x in self.memo if x[1]<k]:del self.memo[key]
            if sink and self.done:n+=len(self.done);sink(self.done);self.done=[]
            if progress and b.day[:7]!=month:month=b.day[:7];print(f'{month} live paths {len(self.live)} stored {n} ({time.time()-t0:.0f}s)',flush=True)
            now=time.time()
            if progress and heartbeat and now-beat>=heartbeat:
                rate=(k-k0)/(now-beat) if now>beat else 0.;eta=(N-1-k)/rate if rate>0 else float('nan')
                print(f'  .. bar {k+1}/{N} {b.day} {zs.hhmm(b.end)}: live {len(self.live)}, stored {n}, {rate:.2f} bars/s, ETA {eta/60:.0f} min',flush=True);beat=now;k0=k
        for p in self.live:
            if not p.rows['mark']:self.done.append(dict(side=p.side,structure=p.structure,offset=p.offset,signal_bar=p.signal_bar,status='no_fill',reason='no_bar_after_entry',end_bar=len(self.bars)-1));continue
            self.finish(p,len(self.bars)-1,'open_at_data_end',p.rows['mark'][-1])
        self.live=[]
        if sink and self.done:sink(self.done);self.done=[]

DDL='''CREATE TABLE meta(name TEXT PRIMARY KEY,value TEXT);
CREATE TABLE bars(idx INTEGER PRIMARY KEY,day TEXT,end_minute TEXT,open REAL,high REAL,low REAL,close REAL,zone TEXT);
CREATE TABLE signals(config TEXT,side TEXT,bar INTEGER);
CREATE TABLE paths(id INTEGER PRIMARY KEY,side TEXT,structure TEXT,offset REAL,signal_bar INTEGER,status TEXT,reason TEXT,entry_day TEXT,lot INTEGER,units INTEGER,
 short_expiry TEXT,long_expiry TEXT,strike REAL,pairs INTEGER,first_bar INTEGER,end_bar INTEGER,end_pnl REAL,entry_index REAL,entry_short_px REAL,end_fill_day TEXT,data BLOB);
CREATE TABLE gaps(event TEXT,leg TEXT,day TEXT,signal_bar INTEGER,side TEXT,structure TEXT,offset REAL,cp TEXT,strike REAL,atm REAL,expiry TEXT,dte INTEGER,roll INTEGER);
CREATE INDEX paths_key ON paths(side,structure,offset,signal_bar);CREATE INDEX signals_cfg ON signals(config,side);'''
PATH_COLS=('side','structure','offset','signal_bar','status','reason','entry_day','lot','units','short_expiry','long_expiry','strike','pairs','first_bar','end_bar','end_pnl','entry_index','entry_short_px','end_fill_day')

def writer(db):
    def sink(rows):
        db.executemany(f'INSERT INTO paths({",".join(PATH_COLS)},data) VALUES({",".join("?"*(len(PATH_COLS)+1))})',
                       [tuple(r.get(c) for c in PATH_COLS)+(pack(r['rows']) if 'rows' in r else None,) for r in rows])
    return sink

def config(a):
    return SimpleNamespace(slippage=a.slippage,slippage_mode='tenor_volume',fill_model='sliced_minutes',brokerage=a.brokerage,gst=.18,exchange_bps=None,sebi_bps=.01,stt=None,stamp=.00003,
        brokerage_order_mode='per_lot_orders',lots=a.lots,max_participation=a.max_participation,min_short_days=a.min_short_days,long_target=a.long_target,contract_master=a.contract_master,
        max_exit_lag_bars=a.max_exit_lag_bars,forced_exit_haircut=a.forced_exit_haircut,hedge_bps=0.,hedge_statutory=True,slippage_points=a.slippage_points,
        roll_entry_after_exit=getattr(a,'roll_entry_after_exit',True),fill_price=getattr(a,'fill_price','ohlc4'),target_units=getattr(a,'target_units',None),
        target_margin=getattr(a,'target_margin',None),offset_mode=getattr(a,'offset_mode','points'),
        target_fraction=getattr(a,'target_fraction',1.),entry_bars=getattr(a,'entry_bars',1),entry_minutes=getattr(a,'entry_minutes',None))

def main():
    ap=cli.Parser(description=__doc__);zb.add_common(ap)
    ap.add_argument('--options',required=True,help='zone_options.sqlite from nifty-zone-contracts');ap.add_argument('--out',default='zone_paths.sqlite')
    ap.add_argument('--structures',default='naked,calendar,futures_zone,futures_expiry',help='also naked_m1 (current-month monthly >= 21 days) and naked_m2 (next-month monthly), if planned in the extraction')
    ap.add_argument('--slippage',type=float,default=.5);ap.add_argument('--brokerage',type=float,default=20.);ap.add_argument('--max-participation',type=float,default=.20,help='share of each minute\'s traded volume an order may take (V4.9.7 default 0.20)')
    ap.add_argument('--max-exit-lag-bars',type=int,default=9);ap.add_argument('--forced-exit-haircut',type=float,default=.02)
    ap.add_argument('--slippage-points',type=float,default=1.,help='Futures: index points of slippage per unit per side')
    ap.add_argument('--cache-days',type=int,default=12,help='Option days kept in memory')
    ap.add_argument('--fill-price',choices=FILL_PRICES,default='ohlc4',help='ohlc4 (default); adverse = high for buys, low for sells (stress); open; adverse_buys = high for buys, ohlc4 for sells')
    ap.add_argument('--target-margin',type=float,help='Margin budget in rupees (e.g. 2500000): entry lots = ceil(--target-fraction x the whole lots whose upper-end margin from the strategy_spec margin table fits it on the fill day); overrides --target-units and --lots')
    ap.add_argument('--target-fraction',type=float,default=ss.TARGET_FRACTION,help='share of the budget\'s maximum lots one entry trades, rounded up (V4.9.7 default 0.40: a trader runs several strategies; 1 = V4.9.6)')
    ap.add_argument('--entry-bars',type=int,default=3,help='45-minute windows an entry may use to reach its full size (V4.9.7 default 3); full size or nothing; a roll re-entry uses one')
    ap.add_argument('--entry-minutes',type=int,default=30,help='trading minutes after the signal bar\'s close in which a first entry must reach its full size (V4.9.12 default 30; 0 = no limit beyond --entry-bars)')
    ap.add_argument('--target-units',type=float,help='Target units per entry (e.g. 650): exchange lots = round(target / lot of the day), so positions keep their size across lot-size changes (50 / 25 / 75 / 65); overrides --lots')
    ap.set_defaults(lots=10)
    a=ap.parse_args();a.offsets=zb.parse_offsets(a.offsets,a.offset_mode)
    if not 0<a.max_participation<=1 or not 0<a.target_fraction<=1 or a.entry_bars<1 or a.entry_minutes<0:sys.exit('--max-participation and --target-fraction must be in (0, 1], --entry-bars >= 1, --entry-minutes >= 0')
    a.entry_minutes=a.entry_minutes or None
    structures=[s for s in a.structures.split(',') if s]
    bad=set(structures)-set(OPTION_STRUCTURES+FUTURES_STRUCTURES)
    if bad:sys.exit(f'Unknown structures {sorted(bad)}')
    market=zb.Market(a.options,a.spot,keep=2);planned=market.meta.get('plan',{})
    missing=sorted(set(a.offsets)-set(planned.get('offsets',list(zb.OFFSETS))))
    if missing:sys.exit(f'Offsets {missing} were not in the contract extraction (planned {planned.get("offsets")})')
    if planned.get('offset_mode','points')!=a.offset_mode:sys.exit(f"--offset-mode {a.offset_mode} differs from the contract extraction ({planned.get('offset_mode','points')})")
    unplanned=sorted(set(s for s in structures if s in OPTION_STRUCTURES)-set(planned.get('structures',['naked','calendar'])))
    if unplanned:sys.exit(f'Structures {unplanned} were not in the contract extraction (planned {planned.get("structures",["naked","calendar"])}): rerun nifty-zone-contracts --structures')
    for key in ('min_short_days','long_target','from_date','to_date'):
        if key in planned and planned[key]!=getattr(a,key):sys.exit(f'--{key.replace("_","-")} differs from the contract extraction ({planned[key]})')
    minutes=zs.load_minutes(a.spot)
    if not minutes:sys.exit(f'No spot minutes in {a.spot}')
    bars,final,_=zs.zones(minutes);sig=zs.signal_grid(bars,final);entries=zb.entry_map(bars,sig,a.from_date,a.to_date)
    sim=PathSim(market,bars,final,config(a),minutes,cache_days=a.cache_days)
    tmp=a.out+'.building';Path(tmp).unlink(missing_ok=True)   # the previous good file stays until this build finishes
    db=sqlite3.connect(tmp);db.executescript(DDL)
    db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)',[(i,b.day,zs.hhmm(b.end),b.open,b.high,b.low,b.close,final[i]) for i,b in enumerate(bars)])
    db.executemany('INSERT INTO signals VALUES(?,?,?)',[(c,side,t) for c,s in sig.items() for side in ('bull','bear') for t in s[side]])
    sink=writer(db);t0=time.time()
    sim.run(entries,structures,a.offsets,sink=lambda rows:(sink(rows),db.commit()))
    G=('event','leg','day','signal_bar','side','structure','offset','cp','strike','atm','expiry','dte','roll')
    db.executemany(f'INSERT INTO gaps VALUES({",".join("?"*len(G))})',[tuple(g[c] for c in G) for g in sim.gaps])
    meta=dict(release=nifty_iv.__version__,args=vars(a),counts=dict(sim.counts),grid=zs.GRID,stale=zs.STALE,
              fill_model='sliced minutes: <= max participation of each minute, whole lots, all legs matched minute by minute; first entry: up to --entry-bars windows and --entry-minutes minutes, full target size or nothing (no trade if the zone changes first); roll re-entry: one window, full size; fill price per --fill-price, slippage and charges on each leg total per window',seconds=round(time.time()-t0))
    db.executemany('INSERT INTO meta VALUES(?,?)',[(k,json.dumps(v,default=str)) for k,v in meta.items()]);db.commit()
    n=db.execute('SELECT COUNT(*),SUM(status="no_fill") FROM paths').fetchone();db.close();market.close();os.replace(tmp,a.out)
    print('Counts:',dict(sim.counts));print(f'{n[0]} paths ({n[1]} not filled) -> {a.out}')

if __name__=='__main__':main()
