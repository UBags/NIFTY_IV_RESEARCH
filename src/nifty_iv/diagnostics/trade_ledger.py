#!/usr/bin/env python3
"""Trade-by-trade ledger of chosen strategies, with every fill's price, slippage and charges.

The paths file stores each trade's cash flows but not its fills. This tool takes each strategy's trades exactly as the grid and
the scans sequence them (zone_grid.Detail: one position at a time, the exit rule's firing bar), then replays every trade through
the path engine (zone.paths.PathSim, same settings as the paths file: its stored arguments) with the fills recorded: minutes,
quantities, market price, slippage, brokerage, GST, STT, exchange / SEBI / stamp charges, expiry settlements. Each replayed
trade's net cash is checked against the paths file (column check_rupees, expected ~0).

Added on top of the engine (not in any earlier figure): interest on the position's margin at --rate a year for the calendar days
held (entry fill day to the last exit fill or the settlement day; a same-day trade counts 1 day). Margin = units x the upper-end
margin per unit of the strategy_spec table on the entry day (the margin the sizing and mloss stops use; not broker SPAN).

Exits (V4.9.12). Default: the strategy's profit and stop rules are decided at 45-minute bar closes (as everywhere else), AND a
protective stop rests at the broker (--intrabar-stop, default mloss7.5: buy back once the trade's loss reaches 7.5% of its margin),
checked on every minute at the minute's worst price for the trade and filled from that minute. --intrabar-stop none = bar-close
only (the earlier figures). --exit-at same-bar also checks the strategy's own price rules on every minute's close. A trade exited
intrabar has exit_reason intrabar_stop:<rule> or <part>_rule_intrabar:<rule>, and no check_rupees (the paths file has no such exit).

Costs and fills (V4.9.12 defaults). Statutory charges follow the dated schedule in ivcal.calendar_research.fill_charges: STT on the
sell-side premium (0.0625% to 2024-09-30, 0.10% to 2026-03-31, 0.15% from 2026-04-01), NSE transaction charges, SEBI fees, stamp duty
on buys, GST 18% on brokerage + transaction + SEBI. --slippage (default Rs 0.5 per unit per side) is scaled by days to expiry and by
the minute volume in lots (0.5 x sqrt(max(DTE, 7)/7) x (1 + 2/sqrt(lots)), so about Rs 0.5-1.5 on these contracts). --brokerage
(default Rs 20) is charged per lot per side: an upper bound on Rs 20 per executed order, since every minute's slice is at least one
lot. --exit-fill adverse (default): every buy-back fills at its minute's HIGH (plus slippage), every sale at the minute's
(O+H+L+C)/4 less slippage; ohlc4 = buy-backs at (O+H+L+C)/4 too, which reproduces the paths file.

Outputs: <out>_trades.csv (one row per trade), <out>_years.csv (per strategy and calendar year of entry), printed tables.
Years 2025-26 were examined before (docs/CODEMAP_V4_9_11.md); 2026 = January-June."""
import argparse,collections,csv,datetime as dt,json,math,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.zone import paths as zp
from nifty_iv.core import strategy_spec as ss
from nifty_iv.core import cli

def hm(i):return zs.hhmm(zs.OPEN+int(i))

class LedgerSim(zp.PathSim):
    """PathSim that logs what its fills used: per-window tradable caps (entries), slices (exits), settlements."""
    def __init__(self,*a,**k):super().__init__(*a,**k);self.cap_log=[];self.slice_log=[];self.settle_log=[]
    def caps(self,day,i0,i1,c,lot,buy):
        r=super().caps(day,i0,i1,c,lot,buy);self.cap_log.append((day,i0,c,r));return r
    def slice(self,day,i0,i1,c,want,lot,buy=True):
        r=super().slice(day,i0,i1,c,want,lot,buy)
        n,_,_,j=r
        if n:
            d=self.arrays(day);vol=d.vol[self.key(c)][i0:i1];cap=np.floor(vol*self.cfg.max_participation)//lot*lot
            first=i0+int(np.flatnonzero(cap)[0]);self.slice_log.append((day,first,j,c,n))
        return r
    exit_start=None                                                        # (bar t, first minute index) of an intrabar exit (--exit-at same-bar)
    def window(self,t):
        w=super().window(t)
        if w is not None and self.exit_start is not None and self.exit_start[0]==t:return (w[0],max(w[1],self.exit_start[1]),w[2])
        return w
    def settle_px(self,c):
        px=super().settle_px(c)
        if px is not None:self.settle_log.append((c,px))
        return px

def entry_minutes(cap_log,legs,want):
    """(first, last) (day, minute index) of an entry's fills, from the caps the engine saw (same matching as PathSim.enter)."""
    got=0;first=last=None;n=len(legs)
    for w in range(0,len(cap_log)-n+1,n):
        cs=[x[3] for x in cap_log[w:w+n]];day,i0=cap_log[w][0],cap_log[w][1]
        if any(x is None for x in cs):continue
        cap=np.min(np.vstack([x[0] for x in cs]),axis=0);cum=np.cumsum(cap);avail=int(cum[-1]) if len(cum) else 0
        if not avail:continue
        left=want-got
        if avail<left:taken=cap
        else:
            m=int(np.searchsorted(cum,left));taken=cap[:m+1].copy();taken[m]=left-(cum[m-1] if m else 0.)
        idx=np.flatnonzero(taken)
        if len(idx):
            if first is None:first=(day,i0+int(idx[0]))
            last=(day,i0+int(idx[-1]))
        got+=int(taken.sum())
        if got>=want:break
    return first,last

def bars_from_paths(db):
    out=[];final=[]
    for i,day,end,o,h,l,c,z in db.execute('SELECT idx,day,end_minute,open,high,low,close,zone FROM bars ORDER BY idx'):
        e=zs.minute_of(end);s=zs.OPEN+((e-zs.OPEN-1)//45)*45
        out.append(zs.Bar(day,s,e,o,h,l,c));final.append(z)
    return out,final

def config_from_meta(db):
    row=db.execute("SELECT value FROM meta WHERE name='args'").fetchone()
    if not row:raise SystemExit('the paths file has no stored arguments (meta args): rebuild it with nifty-zone-paths')
    return zp.config(SimpleNamespace(**json.loads(row[0])))

INTRABAR_KINDS=('atr','eatr','pct','epct','pts','target','tpct','mloss','loss','tp','mult')
DISASTER_KINDS=('atr','eatr','pct','epct','pts','mloss','loss','mult')      # stops only: --intrabar-stop
DEFAULT_INTRABAR_STOP='mloss7.5'   # loss >= 7.5% of margin: same rupee risk for both legs; chosen 2026-11-23 from a sweep (none, mult1.75/2/2.5, mloss5/7.5)

def spot_arrays(minutes):
    """{day: (close, high, low) by minute index from the open (NaN where the index has no minute)}."""
    out={}
    for d,rows in minutes.items():
        a=np.full((3,zs.CLOSE-zs.OPEN),np.nan)
        for m,o,h,l,c in rows:
            i=m-zs.OPEN
            if 0<=i<a.shape[1]:a[:,i]=(c,h,l)
        out[d]=a
    return out

def check_intrabar_stop(label):
    if label in (None,'','none'):return None
    try:r=zr.parse(label)
    except ValueError:raise SystemExit(f'--intrabar-stop {label}: unknown rule')
    if r[0] not in DISASTER_KINDS:raise SystemExit(f'--intrabar-stop {label}: must be one of the stop families {", ".join(DISASTER_KINDS)} (or none)')
    return label

def intrabar_trigger(sim,atr,bars,spot,pair,e_last,t,k,side,rules):
    """The first minute, from the minute after the entry completed to the end of bar k (where the bar-close rules decided the
    exit), at which one of `rules` holds. rules: [(part, label, mode)]:
      mode 'close'   (--exit-at same-bar): the rule is tested on the minute's close (NIFTY spot close; option close for the
                     premium and margin rules, the last print carried); the exit fills from the next minute.
      mode 'extreme' (--intrabar-stop): the rule is tested on the minute's worst price for the trade (index low for a bull trade,
                     high for a bear trade; option high for a short leg, low for a long leg), as a resting stop order or a
                     tick-watching program would see it; the exit fills from that same minute.
    Returns (bar j, minute index i, part, label, mode) or None. Rules of other kinds keep their bar-close decision."""
    S=1. if side=='bull' else -1.
    s0=bars[t].close;e0=pair['entry_index'] if math.isfinite(pair['entry_index']) else s0
    todo=[]
    for part,lab,mode in rules:
        if not lab or lab=='none' or lab.startswith('c:'):continue
        r=zr.parse(lab)
        if r[0] in INTRABAR_KINDS:todo.append((part,lab,r,mode))
    if not todo or e_last is None:return None
    todo.sort(key=lambda x:x[3]!='extreme')                                   # within a minute, the resting stop first
    held=pair['held'];cash=pair['cash'];units=abs(pair['units']);mpu=ss.margin_per_unit(pair['day']);entry_px=pair['short_px']
    last={c:sim.lc.get(c) for c,_ in held};worst={c:sim.lc.get(c) for c,_ in held};j0=pair['bar']+1
    for j in range(j0,min(k,len(bars)-1)+1):
        b=bars[j];lo=b.start-zs.OPEN;hi=b.end-zs.OPEN
        if j==j0 and b.day==e_last[0]:lo=max(lo,e_last[1]+1)
        x=spot.get(b.day);arr=sim.arrays(b.day);key={c:sim.key(c) for c,_ in held}
        cl={c:arr.close.get(key[c]) for c,_ in held}
        ex={c:(getattr(arr,'high',{}).get(key[c]) if u<0 else getattr(arr,'low',{}).get(key[c])) for c,u in held}
        for i in range(lo,hi):
            printed=True
            for c,_ in held:
                a=cl[c];v=a[i] if a is not None else 0.
                if v>0:last[c]=v
                else:printed=False
                e=ex[c];w=e[i] if e is not None else 0.
                worst[c]=w if w>0 else last[c]
            if x is not None:xc,xh,xl=x[0][i],x[1][i],x[2][i]
            else:xc=xh=xl=math.nan
            for part,lab,r,mode in todo:
                xi=xc if mode=='close' else (xl if S>0 else xh)
                px=last if mode=='close' else worst
                d=S*(xi-s0);de=S*(xi-e0)
                val=None if any(px[c] is None for c,_ in held) else sum(u*px[c] for c,u in held)
                kind=r[0];hit=False
                if kind in ('atr','eatr','target') and math.isfinite(xi):
                    a=atr[r[1]][t];M=r[2]
                    hit=(d>=M*a) if kind=='target' else (d<-M*a) if kind=='atr' else (de<-M*a)
                elif kind=='pct' and math.isfinite(xi):hit=d<-r[1]/100*s0
                elif kind=='epct' and math.isfinite(xi):hit=de<-r[1]/100*e0
                elif kind=='pts' and math.isfinite(xi):hit=de<-r[1]
                elif kind=='tpct' and math.isfinite(xi):hit=d>=r[1]/100*s0
                elif kind=='mloss' and val is not None:hit=(cash+val)/units<=-r[1]/100*mpu
                elif kind=='loss' and printed and val is not None:hit=cash+val<=-r[1]*cash
                elif kind=='tp' and printed and val is not None:hit=cash+val>=r[1]*cash
                elif kind=='mult' and printed and px[held[0][0]] is not None:hit=px[held[0][0]]>=r[1]*entry_px
                if hit:return j,i,part,lab,mode
    return None

def charges_sum(recs):
    out=collections.Counter()
    for r in recs:
        ch=r['charges'];out['brokerage']+=ch['brokerage'];out['gst']+=ch['gst'];out['stt']+=ch['stt']
        out['other']+=ch['exchange']+ch['sebi']+ch['stamp'];out['total']+=ch['total'];out['orders']+=ch['charged_orders']
        out['slippage']+=abs(r['units'])*r['slip'];out['premium']+=-r['units']*r['actual'];out['qty']+=abs(r['units'])
        out['mkt_notional']+=abs(r['units'])*r['px'];out['fill_notional']+=abs(r['units'])*r['actual']
    return out

def ledger(paths,options,spot,strategies,holidays=(),gap_policy='busy',start='2000-01-01',end='2026-07-01',rate=.10,cfg=None,target_fraction=None,overrides=None,exit_at='next-bar',intrabar_stop=None,exit_fill='ohlc4',info=None):
    """Rows (one per trade) for strategies [(side, structure, level, config, profit, stop)]. target_fraction: replay every trade at
    ceil(fraction x the margin-table lots on the paths' --target-margin) instead of the size the paths file was built with (1 = the
    whole budget); the signals and exit decision bars stay those of the paths file, see main's notes. overrides: cfg fields replayed
    with other values (max_participation, entry_minutes, entry_bars, slippage, brokerage). exit_fill 'adverse': buy-backs at each
    minute's high. info (dict, optional) receives the settings that differ from the paths file and whether the replay is checked."""
    db=sqlite3.connect(f'file:{paths}?mode=ro',uri=True);cfg=cfg or config_from_meta(db);bars,final=bars_from_paths(db)
    overrides={k:v for k,v in (overrides or {}).items() if v is not None}
    if exit_fill=='adverse':overrides['fill_price']='adverse_buys'          # naked shorts only: every buy is a buy-back
    elif exit_fill!='ohlc4':raise SystemExit(f'--exit-fill {exit_fill}: one of ohlc4, adverse')
    changed={k:v for k,v in overrides.items() if getattr(cfg,k,None)!=v}
    resized=target_fraction is not None or bool(changed) or exit_at=='same-bar'    # replayed under other settings: no check against the paths file
    if target_fraction is not None:
        if not getattr(cfg,'target_margin',None):raise SystemExit('--target-fraction needs a paths file built with --target-margin')
        cfg=SimpleNamespace(**dict(vars(cfg),target_fraction=float(target_fraction)))
    if changed:cfg=SimpleNamespace(**dict(vars(cfg),**changed))
    if info is not None:info.update(changed=changed,resized=resized)
    pinfo={}
    for side,st,off,sb,lot,units,strike,sexp,eday,epx,eix in db.execute('SELECT side,structure,offset,signal_bar,lot,units,strike,short_expiry,entry_day,entry_short_px,entry_index FROM paths'):
        pinfo[side,st,float(off),sb]=dict(lot=lot,units=units,strike=strike,expiry=sexp,entry_day=eday,entry_px=epx,entry_index=eix)
    db.close()
    market=zb.Market(options,spot,keep=2);minutes=zs.load_minutes(spot)
    sim=LedgerSim(market,bars,final,cfg,minutes);intrabar_stop=check_intrabar_stop(intrabar_stop)
    spot_px=spot_arrays(minutes) if exit_at=='same-bar' or intrabar_stop else None
    rec=[];orig=zp.fill_detail
    def spy(px,units,day,c_,expiry,volume,high=None,low=None,lot=None):
        r=orig(px,units,day,c_,expiry,volume,high,low,lot);rec.append(dict(day=day,px=px,units=units,cash=r[0],actual=r[1],slip=r[2],charges=r[3]));return r
    zp.fill_detail=spy;rows=[]
    try:
        with zg.Detail(paths,holidays,gap_policy) as D:
            for key in strategies:
                side,st,off,cf,pr,sp=key;sel=dict(side=side,structure=st,offset=float(off),config=cf,profit=pr,stop=sp)
                if st.startswith('futures') or st=='calendar':raise SystemExit(f'{st}: the ledger handles naked structures only')
                for x in D.trades(sel,start,end,value_to=end):
                    t=x['signal_bar'];k=x['first_bar']+x['marks_n']-1                    # exit decision bar = the rule's firing row
                    if not math.isfinite(x['unit']):                                    # no value in the paths file (e.g. settlement price missing): the scans skip it too
                        rows.append(dict(strategy='|'.join(map(str,key)),side=side,year=x['entry_day'][:4],status='unvalued',signal=f"{bars[t].day} {zs.hhmm(bars[t].end)}",
                                         entry_first_fill=x['entry_day'],exit_reason=x['reason'],note='no value in the paths file; excluded from totals, as in the scans'));continue
                    sim.cap_log.clear();sim.slice_log.clear();sim.settle_log.clear();sim.memo.clear();rec.clear()
                    pair,why=sim.open_pair(t,side,st,float(off))
                    if pair is None:
                        if not resized:raise SystemExit(f'replay of {key} signal bar {t} did not fill ({why}); the paths file says it did')
                        rows.append(dict(strategy='|'.join(map(str,key)),side=side,year=x['entry_day'][:4],status='not_filled_at_size',signal=f"{bars[t].day} {zs.hhmm(bars[t].end)}",
                                         entry_first_fill=x['entry_day'],exit_reason=why,note='could not fill under the replay\'s size / fill settings (full size or nothing); excluded from totals'));continue
                    ent=list(rec);rec.clear()
                    legs=list(pair['held']);want=abs(pair['units'])
                    e_first,e_last=entry_minutes(sim.cap_log,legs,want)
                    trig=None;k_bar=k
                    rules=([('profit',pr,'close'),('stop',sp,'close')] if exit_at=='same-bar' else [])+([('disaster',intrabar_stop,'extreme')] if intrabar_stop else [])
                    if rules and not x['hypothetical']:
                        trig=intrabar_trigger(sim,D.bars.atr,bars,spot_px,pair,e_last,t,k,side,rules)
                    if trig:                                                   # close: from the next minute; extreme (resting stop): from that minute
                        j,i,part,lab,mode=trig;s_=i+1 if mode=='close' else i
                        if s_<bars[j].end-zs.OPEN:k=j-1;sim.exit_start=(j-1,s_)
                        else:k=j
                        sim.memo.clear()
                    try:xc,xbar,xday,xforced,ok,_,_,last_fill,_=sim.exit(pair['held'],k,pair['lot'])
                    finally:sim.exit_start=None
                    if trig:sim.memo.clear()
                    units=pair['units'];lot=pair['lot'];c0=pair['held'][0][0]
                    stored=x['unit']*(abs(pair['units']) if resized else x['units']);open_end=(not ok) or bool(x['hypothetical'])       # exit not complete inside the data: valued at its mark, as the paths file does
                    ex=[] if open_end else list(rec)
                    if open_end:sim.slice_log.clear();sim.settle_log.clear();last_fill=None;xc=stored-pair['cash'];xday=dt.date.fromisoformat(min(x['exit_day'],end)).toordinal()
                    E=charges_sum(ent);X=charges_sum(ex);net=pair['cash']+xc
                    settle=xc-sum(r['cash'] for r in ex)                                # settlement cash (or, open at data end, the mark) = exit cash not from fills
                    eday=pair['day'];xday_s=dt.date.fromordinal(int(xday)).isoformat()
                    days=max(1,(dt.date.fromisoformat(xday_s)-dt.date.fromisoformat(eday)).days)
                    margin=ss.margin_per_unit(eday)*units;interest=margin*rate*days/365
                    gross=E['premium']+X['premium']+settle                                          # after slippage, before charges (open at data end: entry + mark)
                    sl=sim.slice_log;x_first=(sl[0][0],sl[0][1]) if sl else None;x_last=(last_fill[0],last_fill[1]) if last_fill else None
                    exit_px_mkt=X['mkt_notional']/X['qty'] if X['qty'] else None;exit_px_fill=X['fill_notional']/X['qty'] if X['qty'] else None
                    settle_px=sim.settle_log[0][1] if sim.settle_log else None
                    if not all(map(math.isfinite,(gross,net,E['total'],X['total']))):
                        raise SystemExit(f"replay of {key} signal bar {t} (entry {pair['day']}) gave a non-finite value: gross {gross}, net {net}, entry charges {E['total']}, exit charges {X['total']}, exit ok={ok}")
                    b=bars[t];bk=bars[k]
                    rows.append(dict(strategy='|'.join(map(str,key)),side=side,year=eday[:4],status='open_at_data_end' if open_end else 'closed',entry_day=eday,exit_day=xday_s,
                        signal=f'{b.day} {zs.hhmm(b.end)}',entry_first_fill=f'{e_first[0]} {hm(e_first[1])}' if e_first else '',entry_last_fill=f'{e_last[0]} {hm(e_last[1])}' if e_last else '',
                        option=f"{'PE' if c0[2]=='PE' else 'CE'} {c0[1]:.0f} exp {c0[0]}",index_at_entry=round(pair['entry_index'],1) if math.isfinite(pair['entry_index']) else None,
                        lots=abs(units)//lot,lot_size=lot,units=abs(units),
                        entry_price_market=round(E['mkt_notional']/E['qty'],2),entry_price_filled=round(E['fill_notional']/E['qty'],2),entry_slippage_rs=round(E['slippage']),
                        margin_rs=round(margin),exit_decision=(f"{bars[trig[0]].day} {hm(trig[1])} intrabar" if trig else f'{bk.day} {zs.hhmm(bk.end)}'),
                        exit_reason=((f"intrabar_stop:{trig[3]}" if trig[2]=='disaster' else f"{trig[2]}_rule_intrabar:{trig[3]}") if trig else x['reason']),bar_close_exit_decision=f'{bars[k_bar].day} {zs.hhmm(bars[k_bar].end)}',
                        exit_first_fill=f'{x_first[0]} {hm(x_first[1])}' if x_first else ('settled' if settle_px is not None else ''),
                        exit_last_fill=f'{x_last[0]} {hm(x_last[1])}' if x_last else (f'{xday_s} 15:30 settlement' if settle_px is not None else 'open at data end (marked)' if open_end else ''),
                        exit_price_market=round(exit_px_mkt,2) if exit_px_mkt is not None else None,exit_price_filled=round(exit_px_fill,2) if exit_px_fill is not None else None,
                        settlement_price=round(settle_px,2) if settle_px is not None else None,exit_slippage_rs=round(X['slippage']),
                        slippage_rs=round(E['slippage']+X['slippage']),gross_pnl_rs=round(gross),
                        brokerage_rs=round(E['brokerage']+X['brokerage']),gst_rs=round(E['gst']+X['gst']),stt_rs=round(E['stt']+X['stt']),other_charges_rs=round(E['other']+X['other']),
                        charges_rs=round(E['total']+X['total']),net_pnl_rs=round(net),days_held=days,interest_rs=round(interest),net_after_interest_rs=round(net-interest),
                        forced_exit=bool(xforced) and not open_end,open_at_data_end=open_end,exit_spilled_bars=max(0,int(xbar)-k-1),check_rupees=None if (resized or trig) else round(net-stored,2)))
    finally:zp.fill_detail=orig;market.close()
    return rows

YEAR_KEYS=('gross_pnl_rs','slippage_rs','brokerage_rs','gst_rs','stt_rs','other_charges_rs','interest_rs','net_pnl_rs','net_after_interest_rs')
def years(rows,rate=.10):
    """Per strategy and calendar year of entry: valued trades summed; unvalued trades counted (a year with only unvalued trades still appears)."""
    g=collections.defaultdict(list);unv=collections.Counter()
    for r in rows:
        if r['status'] in ('unvalued','not_filled_at_size'):unv[r['strategy'],r['year']]+=1;g.setdefault((r['strategy'],r['year']),[])
        else:g[r['strategy'],r['year']].append(r)
    out=[]
    for (s,y),rs in sorted(g.items()):
        d=dict(strategy=s,year=y,trades=len(rs),**{k:sum(r[k] for r in rs) for k in YEAR_KEYS})
        m=[r['margin_rs'] for r in rs];d['avg_margin_rs']=round(sum(m)/len(m)) if m else None
        d['net_pct_of_avg_margin']=round(100*d['net_after_interest_rs']/d['avg_margin_rs'],2) if d['avg_margin_rs'] else None
        my=sum(r['margin_rs']*r['days_held'] for r in rs)/365;d['margin_years']=round(my/d['avg_margin_rs'],3) if d['avg_margin_rs'] else None
        d['net_pct_per_margin_year']=round(100*d['net_after_interest_rs']/my,2) if my else None
        d['days_held']=sum(r['days_held'] for r in rs);d['unvalued_trades']=unv[s,y];out.append(d)
    if len({r['strategy'] for r in rows})>1:out+=combined_years(rows)
    return out

def peak_margin(rows):
    """{year: largest margin held at once by the valued trades of all strategies together}. A trade holds its margin from its entry day
    to its exit day; on a day with both, the exit is taken first (the margin is freed before the next entry)."""
    ev=sorted([(r['entry_day'],1,r['margin_rs']) for r in rows if r.get('entry_day')]+[(r['exit_day'],0,-r['margin_rs']) for r in rows if r.get('entry_day')])
    cur=0;peak={}
    for day,_,m in ev:
        cur+=m
        if m>0:peak[day[:4]]=max(peak.get(day[:4],0),cur)
    return peak

def combined_years(rows):
    """One account running every strategy: per calendar year of entry, the valued trades of all strategies summed, with the peak margin
    held at once (above the per-strategy budget when positions overlap)."""
    val=[r for r in rows if r['status'] in ('closed','open_at_data_end')];pk=peak_margin(val);out=[]
    for y in sorted({r['year'] for r in rows}):
        rs=[r for r in val if r['year']==y];d=dict(strategy='combined',year=y,trades=len(rs),**{k:sum(r[k] for r in rs) for k in YEAR_KEYS})
        d['peak_margin_rs']=pk.get(y);d['net_pct_of_peak_margin']=round(100*d['net_after_interest_rs']/pk[y],2) if pk.get(y) else None
        d['days_held']=sum(r['days_held'] for r in rs);d['unvalued_trades']=sum(1 for r in rows if r['year']==y and r not in val);out.append(d)
    return out

def parse_strategy(s):
    p=[x.strip() for x in s.split(',')]
    if len(p)!=6:raise SystemExit(f'--strategy needs side,structure,level,config,profit,stop: {s}')
    return (p[0],p[1],float(p[2]),p[3],p[4],p[5])

BRIEF=(('strategy','strategy'),('side','side'),('year','year'),('status','status'),('option','option'),('entry_time','entry_first_fill'),('entry_completed','entry_last_fill'),
       ('entry_price','entry_price_filled'),('lots','lots'),('lot_size','lot_size'),('margin_rs','margin_rs'),('exit_reason','exit_reason'),('exit_time','exit_last_fill'),
       ('exit_price','exit_price_filled'),('settlement_price','settlement_price'),('slippage_rs','slippage_rs'),('brokerage_rs','brokerage_rs'),('stt_rs','stt_rs'),
       ('gst_and_other_rs',None),('pnl_rs','net_pnl_rs'),('days_held','days_held'),('interest_rs','interest_rs'),('pnl_after_interest_rs','net_after_interest_rs'))
def brief(r):
    out={k:r.get(src,'') for k,src in BRIEF if src}
    out['gst_and_other_rs']=(r['gst_rs']+r['other_charges_rs']) if 'gst_rs' in r else ''
    return {k:out[k] for k,_ in BRIEF}

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--paths',required=True);ap.add_argument('--options',required=True,help='the option extraction the paths were built from');ap.add_argument('--spot',required=True)
    ap.add_argument('--strategy',action='append',required=True,help='side,structure,level,config,profit,stop (repeat)')
    ap.add_argument('--holidays');ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--start',default='2000-01-01');ap.add_argument('--end',default='2026-07-01');ap.add_argument('--rate',type=float,default=.10,help='interest a year on margin (default 10%%)')
    ap.add_argument('--target-fraction',type=float,help='replay at ceil(this x the margin-table lots on the paths\' budget) instead of the paths\' size (1 = the whole budget). '
                    'Signals and exit decision bars stay those of the paths file; an entry that cannot fill at the larger size is listed and excluded')
    ap.add_argument('--max-participation',type=float,help='replay with this share of each minute\'s volume (default: the paths file\'s, 0.20 unless built otherwise)')
    ap.add_argument('--entry-minutes',type=int,help='replay with entries limited to this many minutes after the signal bar\'s close (default: the paths file\'s; 0 = no limit)')
    ap.add_argument('--entry-bars',type=int,help='replay with entries allowed this many 45-minute windows (default: the paths file\'s, 3); raise it with --entry-minutes above 135')
    ap.add_argument('--exit-at',choices=('next-bar','same-bar'),default='next-bar',help='next-bar (default): profit and stop rules are checked at 45-minute bar closes and the exit fills from the next minute. '
                    'same-bar: price-based rules (atr, eatr, pct, epct, pts, target, tpct, mloss, loss, tp, mult) are also checked on every minute\'s close and the exit fills from the following minute, '
                    'same participation rules; other rules (time, ptime, trailing, zone change, expiry) keep their bar-close decision')
    ap.add_argument('--intrabar-stop',default=DEFAULT_INTRABAR_STOP,help='protective stop checked on every minute at the minute\'s worst price for the trade (as a resting stop order at the broker would be), '
                    'on top of the strategy\'s own rules; the exit fills from that minute. A stop rule: mloss X (loss >= X%% of margin; default mloss7.5), mult X (short option at X times its entry fill), '
                    'loss X (loss >= X%% of the credit), atr/eatr PxM, pct/epct X, pts X; none = off')
    ap.add_argument('--exit-fill',choices=('adverse','ohlc4'),default='adverse',help='price of exit (buy-back) fills: adverse = each minute\'s high plus slippage (conservative); '
                    'ohlc4 = the minute\'s (O+H+L+C)/4 plus slippage, as the paths file, the grid and the scans. Entries (sales) always fill at ohlc4 less slippage')
    ap.add_argument('--slippage',type=float,default=.5,help='option slippage base, rupees per unit per side, scaled by days to expiry and minute volume (about Rs 0.5-1.5 on these contracts)')
    ap.add_argument('--brokerage',type=float,default=20.,help='brokerage in rupees, charged per lot per side (an upper bound on a flat Rs 20 per executed order); '
                    'STT, NSE transaction charges, SEBI fees, stamp duty and 18%% GST follow the dated statutory schedule')
    ap.add_argument('--out',default='trade_ledger')
    a=ap.parse_args(argv);hol=zr.read_holidays(a.holidays) if a.holidays else []
    info={}
    rows=ledger(a.paths,a.options,a.spot,[parse_strategy(s) for s in a.strategy],hol,a.gap_policy,a.start,a.end,a.rate,target_fraction=a.target_fraction,
              overrides=dict(max_participation=a.max_participation,entry_minutes=a.entry_minutes,entry_bars=a.entry_bars,slippage=a.slippage,brokerage=a.brokerage),
              exit_at=a.exit_at,intrabar_stop=a.intrabar_stop,exit_fill=a.exit_fill,info=info)
    if not rows:sys.exit('no trades')
    rows.sort(key=lambda r:(r.get('entry_first_fill') or '',r['strategy']))                      # one time line across strategies
    yr=years(rows,a.rate)
    for name,data in ((f'{a.out}_trades.csv',rows),(f'{a.out}_years.csv',yr),(f'{a.out}_brief.csv',[brief(r) for r in rows])):
        keys=list(dict.fromkeys(k for r in data for k in r))
        with open(name,'w',newline='') as f:w=csv.DictWriter(f,keys);w.writeheader();w.writerows(data)
    resized=info['resized']
    val=[r for r in rows if r['status'] in ('closed','open_at_data_end')];skip=[r for r in rows if r not in val]
    bad=[] if resized else [r for r in val if r['check_rupees'] is not None and not abs(r['check_rupees'])<=1];opn=sum(r['status']=='open_at_data_end' for r in rows)
    rs=lambda v:f'{v:>12,.0f}'
    for s in dict.fromkeys(r['strategy'] for r in rows):
        print(f'\n=== {s} ===')
        print(f"{'year':<6}{'trades':>7}{'gross (after slip)':>20}{'slippage':>12}{'brokerage':>12}{'GST':>10}{'STT':>10}{'other':>10}{'interest':>12}{'net':>12}{'avg margin':>12}{'% avg margin':>14}")
        for d in yr:
            if d['strategy']!=s:continue
            print(f"{d['year']:<6}{d['trades']:>7}{d['gross_pnl_rs']:>20,.0f}{rs(d['slippage_rs'])}{rs(d['brokerage_rs'])}{d['gst_rs']:>10,.0f}{d['stt_rs']:>10,.0f}{d['other_charges_rs']:>10,.0f}"
                  f"{rs(d['interest_rs'])}{rs(d['net_after_interest_rs'])}{rs(d['avg_margin_rs'] or 0)}{(d['net_pct_of_avg_margin'] or 0):>13.1f}%" + (f"  ({d['unvalued_trades']} excluded)" if d['unvalued_trades'] else ''))
    cy=[d for d in yr if d['strategy']=='combined']
    if cy:
        print('\n=== combined (one account, all strategies) ===')
        print(f"{'year':<6}{'trades':>7}{'gross (after slip)':>20}{'charges':>12}{'interest':>12}{'net':>12}{'peak margin':>14}{'% peak margin':>15}")
        for d in cy:
            ch=d['brokerage_rs']+d['gst_rs']+d['stt_rs']+d['other_charges_rs']
            print(f"{d['year']:<6}{d['trades']:>7}{d['gross_pnl_rs']:>20,.0f}{rs(ch)}{rs(d['interest_rs'])}{rs(d['net_after_interest_rs'])}{(d['peak_margin_rs'] or 0):>14,.0f}{(d['net_pct_of_peak_margin'] or 0):>14.1f}%")
        t={k:sum(d[k] for d in cy) for k in ('trades','gross_pnl_rs','interest_rs','net_after_interest_rs')}
        print(f"{'all':<6}{t['trades']:>7}{t['gross_pnl_rs']:>20,.0f}{'':>12}{rs(t['interest_rs'])}{rs(t['net_after_interest_rs'])}{max((d['peak_margin_rs'] or 0) for d in cy):>14,.0f}")
    if resized:
        sp=sum(1 for r in val if r.get('exit_spilled_bars'))
        how=(f'{a.target_fraction:g} x the margin-table lots' if a.target_fraction is not None else "the paths file's size")
        ib=sum(1 for r in val if 'intrabar' in str(r.get('exit_reason','')))
        names=dict(max_participation='participation {:g}',entry_minutes='entry minutes {}',entry_bars='entry windows {}',slippage='slippage Rs {:g}',brokerage='brokerage Rs {:g} a lot')
        for k_,v_ in info['changed'].items():how+=', '+("buy-backs at each minute's high (conservative)" if k_=='fill_price' else names.get(k_,k_+' {}').format(v_))
        how+=(', exits checked every minute (same-bar)' if a.exit_at=='same-bar' else '')
        print(f"\nReplayed at {how}: {len(val)} trades valued ({opn} open at the data end, valued at the paths' mark per unit), "
              f"{sum(r['status']=='not_filled_at_size' for r in rows)} could not fill at that size, {sum(r['status']=='unvalued' for r in rows)} unvalued; {sp} exits needed more than one bar to complete{f'; {ib} exits triggered intrabar' if a.exit_at=='same-bar' else ''}. "
              "Signals and exit decision bars are those of the paths file (built at its own size): a trade that cannot fill would, in a full rerun, free the slot for a later signal.")
    else:
        nchk=sum(r['check_rupees'] is not None for r in val)
        print(f"\n{len(val)} valued trades ({opn} still open at the data end, valued at their mark), {len(skip)} unvalued (excluded, as in the scans); replay check (net cash vs paths file, trades without an intrabar exit): {nchk-len(bad)} of {nchk} match" + (f", {len(bad)} DIFFER (see check_rupees)" if bad else ''))
    if a.intrabar_stop not in (None,'','none'):
        hit=[r for r in val if str(r.get('exit_reason','')).startswith('intrabar_stop:')]
        print(f"intrabar stop {a.intrabar_stop}: fired on {len(hit)} of {len(val)} trades" + (f" (net {sum(r['net_pnl_rs'] for r in hit):,.0f}; dates {', '.join(r['exit_decision'][:16] for r in hit[:8])}{' ...' if len(hit)>8 else ''})" if hit else ''))
    print(f'-> {a.out}_trades.csv, {a.out}_years.csv, {a.out}_brief.csv  (pnl = after slippage and all charges; % avg margin = net after interest / average margin per trade)')
    for r in skip:print(f"  {r['status']}: {r['strategy']} signal {r['signal']} entry {r['entry_first_fill']} ({r['exit_reason']})")
    if bad:sys.exit(1)

if __name__=='__main__':main()
