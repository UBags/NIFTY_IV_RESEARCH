#!/usr/bin/env python3
"""INDEX v15 zones on 45-minute NIFTY bars, the swing filter, and the signal grid. Spot only; no option prices.

Port of INDEX_v15_zone_swing.pine with volume = 1 on every bar. Everything is decided on bar close.
Bars are aligned to each session's 09:15 open, as TradingView builds intraday bars: 45-minute bars end
10:00 ... 15:15 and a 15-minute bar ends 15:30; 90, 180 and 360-minute bars are built the same way.
A higher-timeframe value reaches a 45-minute bar only once that higher bar has closed (lookahead_off).

Assumptions that may differ from TradingView in rare cases, all disclosed:
  * ta.ema is seeded with its first input, ta.rma (ATR) with the SMA of its first `length` inputs.
  * A pivot low is strictly below the N lows before it and at or below the N lows after it (mirror for highs).
"""
from __future__ import annotations
import argparse,collections,datetime as dt,itertools,json,math,sqlite3,statistics
from dataclasses import dataclass
import numpy as np
from nifty_iv.core import cli

OPEN=9*60+15;CLOSE=15*60+30
LEN45=dict(s=(89,48,24),l=(56,43,6),h=(31,40,16),hh=(23,48,7))
GRID=dict(N=(3,5),k=(.25,.5,.75,1.,1.25,1.5),m=(8,12,16,20),j=(.25,.5,.75,1.,1.25,1.5),tw=(2,3,4,5,6))
STALE=16

def hhmm(minute):return f'{minute//60:02d}:{minute%60:02d}'
def minute_of(text):return int(text[:2])*60+int(text[3:5])

@dataclass
class Bar:
    day:str;start:int;end:int;open:float;high:float;low:float;close:float;minutes:int=0
    @property
    def key(self):return (self.day,self.end)
    @property
    def mark_minute(self):return hhmm(min(self.end,CLOSE)-1)

def load_minutes(path,start=None,end=None):
    """{day: [(minute_of_day,o,h,l,c), ...]} from the spot_1m table written by nifty-zone-spot."""
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);q='SELECT day,minute,open,high,low,close FROM spot_1m'
    args=[];w=[]
    if start:w.append('day>=?');args.append(start)
    if end:w.append('day<=?');args.append(end)
    if w:q+=' WHERE '+' AND '.join(w)
    out=collections.defaultdict(list)
    for d,m,o,h,l,c in db.execute(q+' ORDER BY day,minute',args):
        hh,mm=m.split(':');out[d].append((int(hh)*60+int(mm),o,h,l,c))
    db.close();return dict(out)

def aggregate(minutes,tf):
    """Session-aligned bars of tf minutes. A bar's end is start+tf, capped at the 15:30 close."""
    bars=[]
    for day in sorted(minutes):
        groups=collections.OrderedDict()
        for m,o,h,l,c in minutes[day]:
            if not OPEN<=m<CLOSE:continue
            groups.setdefault((m-OPEN)//tf,[]).append((o,h,l,c))
        for b,rows in groups.items():
            start=OPEN+b*tf
            bars.append(Bar(day,start,min(start+tf,CLOSE),rows[0][0],max(r[1] for r in rows),min(r[2] for r in rows),rows[-1][3],len(rows)))
    return bars

def ema(x,n):
    a=2/(n+1);out=np.empty(len(x));prev=math.nan
    for i,v in enumerate(x):
        if math.isnan(v):out[i]=prev;continue
        prev=v if math.isnan(prev) else a*v+(1-a)*prev;out[i]=prev
    return out

def wma(x,n):
    out=np.full(len(x),math.nan);w=np.arange(1,n+1,dtype=float);s=w.sum()
    for i in range(n-1,len(x)):
        win=x[i-n+1:i+1]
        if not np.isnan(win).any():out[i]=float(win@w)/s
    return out

def sma(x,n):
    out=np.full(len(x),math.nan)
    for i in range(n-1,len(x)):
        win=x[i-n+1:i+1]
        if not np.isnan(win).any():out[i]=float(win.mean())
    return out

def pine_round(x):return int(math.floor(x+.5))

def calc_ema(close,lens):
    """calculateEMA with volume 1: ema(close*1)/ema(1) is ema(close)."""
    l1,l2,l3=(max(int(v),2) for v in lens)
    vw2=ema(ema(np.asarray(close,float),l1),l2)
    return wma(2*wma(vw2,l3//2)-wma(vw2,l3),pine_round(math.sqrt(l3)))

def sample(chart,htf,values):
    """Value of the latest higher-timeframe bar whose close is at or before each chart bar's close."""
    out=np.full(len(chart),math.nan);p=0
    for i,b in enumerate(chart):
        while p<len(htf) and htf[p].key<=b.key:p+=1
        if p:out[i]=values[p-1]
    return out

def rma(x,n):
    out=np.full(len(x),math.nan);prev=math.nan
    for i in range(len(x)):
        if math.isnan(prev):
            if i>=n-1:prev=float(np.mean(x[i-n+1:i+1]));out[i]=prev
        else:prev=(prev*(n-1)+x[i])/n;out[i]=prev
    return out

def atr(bars,n=14):
    tr=np.array([b.high-b.low if i==0 else max(b.high-b.low,abs(b.high-bars[i-1].close),abs(b.low-bars[i-1].close)) for i,b in enumerate(bars)])
    return rma(tr,n)

def zones(minutes,carry=True,lens=LEN45,tf=45,fixed=True):
    """Bars and per-bar zone: 'G', 'R' or None. fixed=False reproduces v14 (persistence filter inactive)."""
    chart=aggregate(minutes,tf);close=np.array([b.close for b in chart])
    e1=calc_ema(close,lens['s']);e2=calc_ema(close,lens['l'])
    h=[aggregate(minutes,tf*f) for f in (2,4,8)]
    e3=sma(sample(chart,h[0],calc_ema([b.close for b in h[0]],lens['h'])),2)
    e5=sma(sample(chart,h[1],calc_ema([b.close for b in h[1]],lens['hh'])),4)
    e6=sma(sample(chart,h[2],calc_ema([b.close for b in h[2]],lens['hh'])),8)
    pre=[None]*len(chart);final=[None]*len(chart);prev_final=None
    for i,c in enumerate(close):
        p1=pre[i-1] if i else None;vals=(e1[i],e2[i],e3[i],e5[i],e6[i])
        if any(math.isnan(v) for v in vals):pre[i]=p1
        else:
            fmax,fmin=max(e2[i],e3[i],e5[i]),min(e2[i],e3[i],e5[i]);smax,smin=max(e1[i],e6[i]),min(e1[i],e6[i])
            between=(c<fmin and c>smax) or (c>fmax and c<smin)
            if between:pre[i]=p1 if carry else None
            elif c>max(fmax,smax):pre[i]='G'
            elif c<min(fmin,smin):pre[i]='R'
            else:pre[i]=p1
        if not fixed:final[i]=pre[i];continue
        last3=[pre[x] if x>=0 else None for x in (i,i-1,i-2)]
        if last3==['G']*3:prev_final='G'
        elif last3==['R']*3:prev_final='R'
        elif last3==[None]*3:prev_final=None
        final[i]=prev_final
    return chart,final,dict(ema1=e1,ema2=e2,ema3=e3,ema5=e5,ema6=e6,pre=pre)

def pivots(values,N,kind):
    """Pivot centres: strictly beyond the N before, at least as extreme as the N after. Known at centre+N."""
    out=[]
    for c in range(N,len(values)-N):
        left=values[c-N:c];right=values[c+1:c+N+1];v=values[c]
        if (kind=='low' and v<min(left) and v<=min(right)) or (kind=='high' and v>max(left) and v>=max(right)):out.append(c)
    return out

def swing_events(bars,a,N,k,m,j):
    """Qualifying rising valleys and falling peaks: (confirm_bar, centre, level) lists. Pine section 5."""
    low=[b.low for b in bars];high=[b.high for b in bars];res={}
    for kind,vals,opp in (('low',low,high),('high',high,low)):
        ev=[];last=None
        for c in pivots(vals,N,kind):
            if last is not None:
                gap=c-last;span=opp[last:c+1];aa=a[c]
                if kind=='low':ok=gap>=m and vals[c]-vals[last]>=j*aa and max(span)-vals[c]>=k*aa
                else:ok=gap>=m and vals[last]-vals[c]>=j*aa and vals[c]-min(span)>=k*aa
                if ok:ev.append((c+N,c,vals[c]))
            last=c
        res[kind]=ev
    return res

def entries(bars,final,events,tw,stale=STALE):
    """Entry bars, exactly as the Pine state machine: one entry per qualifying pivot, a newer one replaces it."""
    close=[b.close for b in bars];out={'bull':[],'bear':[]}
    for side,kind,colour in (('bull','low','G'),('bear','high','R')):
        conf={e[0]:e for e in events[kind]};used=True;cb=None;lvl=None
        for t in range(len(bars)):
            if t in conf:cb,_,lvl=conf[t];used=False
            if used:continue
            since=t-cb
            beyond=close[t]>lvl if side=='bull' else close[t]<lvl
            if tw<=since<=tw+stale and final[t]==colour and beyond:out[side].append(t);used=True;continue
            if since>tw+stale or not beyond:used=True
    return out

def zone_only_entries(final,tw):
    """Control: tw bars after each change into a colour, if the colour still holds. No swing filter."""
    out={'bull':[],'bear':[]}
    for t in range(len(final)):
        s=t-tw
        if s<1:continue
        for side,colour in (('bull','G'),('bear','R')):
            if final[s]==colour and final[s-1]!=colour and all(final[x]==colour for x in range(s,t+1)):out[side].append(t)
    return out

def signal_grid(bars,final,grid=GRID,stale=STALE):
    """{config_id: {'bull': [bar,...], 'bear': [...]}} for the swing grid plus zone-only controls."""
    a=atr(bars);out={}
    for N,k,m,j in itertools.product(grid['N'],grid['k'],grid['m'],grid['j']):
        ev=swing_events(bars,a,N,k,m,j)
        for tw in grid['tw']:out[f'N{N}_k{k}_m{m}_j{j}_tw{tw}']=entries(bars,final,ev,tw,stale)
    for tw in grid['tw']:out[f'zone_only_tw{tw}']=zone_only_entries(final,tw)
    return out

def forward_returns(bars,idx,h):
    return [(bars[i+h].close/bars[i].close-1)*1e4 for i in idx if i+h<len(bars)]

def step0(bars,final,signals,split,horizons=(9,18,45)):
    """Signed forward spot return in bp after each entry, against every bar of the same colour. Train only."""
    train=[i for i,b in enumerate(bars) if b.day<split];cut=max(train)+1 if train else 0
    rep={}
    for side,colour,sign in (('bull','G',1),('bear','R',-1)):
        base=[i for i in range(cut) if final[i]==colour]
        rep[side]={'all_bars_in_colour':{h:summary([sign*x for x in forward_returns(bars,[i for i in base if i+h<cut],h)]) for h in horizons}}
        for cid,sig in signals.items():
            idx=[i for i in sig[side] if i<cut]
            rep[side][cid]={h:summary([sign*x for x in forward_returns(bars,[i for i in idx if i+h<cut],h)]) for h in horizons}
    return rep

def summary(x):
    if len(x)<2:return dict(n=len(x))
    m=statistics.mean(x);s=statistics.stdev(x)
    return dict(n=len(x),mean_bp=round(m,1),t=round(m/(s/math.sqrt(len(x))),2) if s else None,up=round(sum(v>0 for v in x)/len(x),3))

def zs_h(m):return hhmm(m)

def main():
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--spot',required=True,help='zone_spot.sqlite from nifty-zone-spot');ap.add_argument('--split',default='2025-01-01')
    ap.add_argument('--out',default='zone_signals.json');ap.add_argument('--v14',action='store_true',help='Reproduce v14: persistence filter inactive')
    a=ap.parse_args()
    minutes=load_minutes(a.spot)
    if not minutes:raise SystemExit(f'No spot minutes in {a.spot}: run nifty-zone-spot first')
    bars,final,_=zones(minutes,fixed=not a.v14)
    sig=signal_grid(bars,final)
    rep=step0(bars,final,sig,a.split)
    json.dump(dict(split=a.split,bars=len(bars),first=bars[0].key,last=bars[-1].key,step0=rep),open(a.out,'w'),indent=1)
    green=sum(x=='G' for x in final);red=sum(x=='R' for x in final)
    short=[(b.day,zs_h(b.start),b.minutes,b.end-b.start) for b in bars if b.minutes<b.end-b.start]
    print(f'Bar coverage: {len(short)} of {len(bars)} 45-minute bars have fewer minutes than their span (kept, by decision); first: {short[:8]}')
    print(f'{len(bars)} bars {bars[0].day}..{bars[-1].day}: green {green}, red {red}, configs {len(sig)} -> {a.out}')
    for side in ('bull','bear'):
        r=rep[side];best=sorted(((v[18].get('t') or -9,k) for k,v in r.items() if k!='all_bars_in_colour' and v[18].get('n',0)>=30),reverse=True)[:5]
        print(side,'all bars in colour, 18-bar:',r['all_bars_in_colour'][18],'| top t (n>=30, before split):',[(k,r[k][18]) for _,k in best])

if __name__=='__main__':main()
