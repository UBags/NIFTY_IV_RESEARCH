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

def charges_sum(recs):
    out=collections.Counter()
    for r in recs:
        ch=r['charges'];out['brokerage']+=ch['brokerage'];out['gst']+=ch['gst'];out['stt']+=ch['stt']
        out['other']+=ch['exchange']+ch['sebi']+ch['stamp'];out['total']+=ch['total'];out['orders']+=ch['charged_orders']
        out['slippage']+=abs(r['units'])*r['slip'];out['premium']+=-r['units']*r['actual'];out['qty']+=abs(r['units'])
        out['mkt_notional']+=abs(r['units'])*r['px'];out['fill_notional']+=abs(r['units'])*r['actual']
    return out

def ledger(paths,options,spot,strategies,holidays=(),gap_policy='busy',start='2000-01-01',end='2026-07-01',rate=.10,cfg=None,target_fraction=None):
    """Rows (one per trade) for strategies [(side, structure, level, config, profit, stop)]. target_fraction: replay every trade at
    ceil(fraction x the margin-table lots on the paths' --target-margin) instead of the size the paths file was built with (1 = the
    whole budget); the signals and exit decision bars stay those of the paths file, see main's notes."""
    db=sqlite3.connect(f'file:{paths}?mode=ro',uri=True);cfg=cfg or config_from_meta(db);bars,final=bars_from_paths(db)
    resized=target_fraction is not None
    if resized:
        if not getattr(cfg,'target_margin',None):raise SystemExit('--target-fraction needs a paths file built with --target-margin')
        cfg=SimpleNamespace(**dict(vars(cfg),target_fraction=float(target_fraction)))
    pinfo={}
    for side,st,off,sb,lot,units,strike,sexp,eday,epx,eix in db.execute('SELECT side,structure,offset,signal_bar,lot,units,strike,short_expiry,entry_day,entry_short_px,entry_index FROM paths'):
        pinfo[side,st,float(off),sb]=dict(lot=lot,units=units,strike=strike,expiry=sexp,entry_day=eday,entry_px=epx,entry_index=eix)
    db.close()
    market=zb.Market(options,spot,keep=2);minutes=zs.load_minutes(spot)
    sim=LedgerSim(market,bars,final,cfg,minutes)
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
                                         entry_first_fill=x['entry_day'],exit_reason=why,note='the larger size could not fill (full size or nothing); excluded from totals'));continue
                    ent=list(rec);rec.clear()
                    legs=list(pair['held']);want=abs(pair['units'])
                    e_first,e_last=entry_minutes(sim.cap_log,legs,want)
                    xc,xbar,xday,xforced,ok,_,_,last_fill,_=sim.exit(pair['held'],k,pair['lot'])
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
                    rows.append(dict(strategy='|'.join(map(str,key)),side=side,year=eday[:4],status='open_at_data_end' if open_end else 'closed',
                        signal=f'{b.day} {zs.hhmm(b.end)}',entry_first_fill=f'{e_first[0]} {hm(e_first[1])}' if e_first else '',entry_last_fill=f'{e_last[0]} {hm(e_last[1])}' if e_last else '',
                        option=f"{'PE' if c0[2]=='PE' else 'CE'} {c0[1]:.0f} exp {c0[0]}",index_at_entry=round(pair['entry_index'],1) if math.isfinite(pair['entry_index']) else None,
                        lots=abs(units)//lot,lot_size=lot,units=abs(units),
                        entry_price_market=round(E['mkt_notional']/E['qty'],2),entry_price_filled=round(E['fill_notional']/E['qty'],2),entry_slippage_rs=round(E['slippage']),
                        margin_rs=round(margin),exit_decision=f'{bk.day} {zs.hhmm(bk.end)}',exit_reason=x['reason'],
                        exit_first_fill=f'{x_first[0]} {hm(x_first[1])}' if x_first else ('settled' if settle_px is not None else ''),
                        exit_last_fill=f'{x_last[0]} {hm(x_last[1])}' if x_last else (f'{xday_s} 15:30 settlement' if settle_px is not None else 'open at data end (marked)' if open_end else ''),
                        exit_price_market=round(exit_px_mkt,2) if exit_px_mkt is not None else None,exit_price_filled=round(exit_px_fill,2) if exit_px_fill is not None else None,
                        settlement_price=round(settle_px,2) if settle_px is not None else None,exit_slippage_rs=round(X['slippage']),
                        slippage_rs=round(E['slippage']+X['slippage']),gross_pnl_rs=round(gross),
                        brokerage_rs=round(E['brokerage']+X['brokerage']),gst_rs=round(E['gst']+X['gst']),stt_rs=round(E['stt']+X['stt']),other_charges_rs=round(E['other']+X['other']),
                        charges_rs=round(E['total']+X['total']),net_pnl_rs=round(net),days_held=days,interest_rs=round(interest),net_after_interest_rs=round(net-interest),
                        forced_exit=bool(xforced) and not open_end,open_at_data_end=open_end,exit_spilled_bars=max(0,int(xbar)-k-1),check_rupees=None if resized else round(net-stored,2)))
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
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',required=True);ap.add_argument('--options',required=True,help='the option extraction the paths were built from');ap.add_argument('--spot',required=True)
    ap.add_argument('--strategy',action='append',required=True,help='side,structure,level,config,profit,stop (repeat)')
    ap.add_argument('--holidays');ap.add_argument('--gap-policy',choices=zg.GAP_POLICIES,default='busy')
    ap.add_argument('--start',default='2000-01-01');ap.add_argument('--end',default='2026-07-01');ap.add_argument('--rate',type=float,default=.10,help='interest a year on margin (default 10%%)')
    ap.add_argument('--target-fraction',type=float,help='replay at ceil(this x the margin-table lots on the paths\' budget) instead of the paths\' size (1 = the whole budget). '
                    'Signals and exit decision bars stay those of the paths file; an entry that cannot fill at the larger size is listed and excluded')
    ap.add_argument('--out',default='trade_ledger')
    a=ap.parse_args(argv);hol=zr.read_holidays(a.holidays) if a.holidays else []
    rows=ledger(a.paths,a.options,a.spot,[parse_strategy(s) for s in a.strategy],hol,a.gap_policy,a.start,a.end,a.rate,target_fraction=a.target_fraction)
    if not rows:sys.exit('no trades')
    yr=years(rows,a.rate)
    for name,data in ((f'{a.out}_trades.csv',rows),(f'{a.out}_years.csv',yr),(f'{a.out}_brief.csv',[brief(r) for r in rows])):
        keys=list(dict.fromkeys(k for r in data for k in r))
        with open(name,'w',newline='') as f:w=csv.DictWriter(f,keys);w.writeheader();w.writerows(data)
    resized=a.target_fraction is not None
    val=[r for r in rows if r['status'] in ('closed','open_at_data_end')];skip=[r for r in rows if r not in val]
    bad=[] if resized else [r for r in val if not abs(r['check_rupees'])<=1];opn=sum(r['status']=='open_at_data_end' for r in rows)
    rs=lambda v:f'{v:>12,.0f}'
    for s in dict.fromkeys(r['strategy'] for r in rows):
        print(f'\n=== {s} ===')
        print(f"{'year':<6}{'trades':>7}{'gross (after slip)':>20}{'slippage':>12}{'brokerage':>12}{'GST':>10}{'STT':>10}{'other':>10}{'interest':>12}{'net':>12}{'avg margin':>12}{'% avg margin':>14}")
        for d in yr:
            if d['strategy']!=s:continue
            print(f"{d['year']:<6}{d['trades']:>7}{d['gross_pnl_rs']:>20,.0f}{rs(d['slippage_rs'])}{rs(d['brokerage_rs'])}{d['gst_rs']:>10,.0f}{d['stt_rs']:>10,.0f}{d['other_charges_rs']:>10,.0f}"
                  f"{rs(d['interest_rs'])}{rs(d['net_after_interest_rs'])}{rs(d['avg_margin_rs'] or 0)}{(d['net_pct_of_avg_margin'] or 0):>13.1f}%" + (f"  ({d['unvalued_trades']} excluded)" if d['unvalued_trades'] else ''))
    if resized:
        sp=sum(1 for r in val if r.get('exit_spilled_bars'))
        print(f"\nReplayed at {a.target_fraction:g} x the margin-table lots: {len(val)} trades valued ({opn} open at the data end, valued at the paths' mark per unit), "
              f"{sum(r['status']=='not_filled_at_size' for r in rows)} could not fill at that size, {sum(r['status']=='unvalued' for r in rows)} unvalued; {sp} exits needed more than one bar to complete. "
              "Signals and exit decision bars are those of the paths file (built at its own size): a trade that cannot fill would, in a full rerun, free the slot for a later signal.")
    else:
        print(f"\n{len(val)} valued trades ({opn} still open at the data end, valued at their mark), {len(skip)} unvalued (excluded, as in the scans); replay check (net cash vs paths file): {len(val)-len(bad)} match" + (f", {len(bad)} DIFFER (see check_rupees)" if bad else ''))
    print(f'-> {a.out}_trades.csv, {a.out}_years.csv, {a.out}_brief.csv  (pnl = after slippage and all charges; % avg margin = net after interest / average margin per trade)')
    for r in skip:print(f"  {r['status']}: {r['strategy']} signal {r['signal']} entry {r['entry_first_fill']} ({r['exit_reason']})")
    if bad:sys.exit(1)

if __name__=='__main__':main()
