#!/usr/bin/env python3
"""Top-N zone rules per side, in rupees at --lots, by 12-month period: P&L, win rate, profit factor, Sharpe, Sortino,
Calmar, max drawdown, max loss, max win and how trades ended.

Ranking comes from nifty-zone-legacy-report's train_candidates.csv (training only). Rules that produce identical trades are
merged and listed as aliases. A chain still open at --to is closed hypothetically at the last bar-close prices
before it. --to past the split shows holdout years: say so when you use it.

Periods are calendar periods: "P&L earned" is what the daily path made inside the period; "cohort" is the lifetime P&L of
trades entered in it. Futures are marked to each session close; options are booked on exit days (no daily marks), so
their volatility and drawdown are understated.
Calmar is annualised P&L / max drawdown, both in rupees. Fills were checked for volume at the simulated lot count only."""
import argparse,collections as C,csv,json,math,sqlite3,statistics as S,sys
from pathlib import Path
from nifty_iv.zone.legacy import report as zr

def periods(lo,hi,months=12):
    out=[];y,m=int(lo[:4]),int(lo[5:7]);d=lo[8:]
    while True:
        m2=m+months;y2=y+(m2-1)//12;m2=(m2-1)%12+1;start=f'{y}-{m:02d}-{d}';end=f'{y2}-{m2:02d}-{d}'
        if start>=hi:break
        out.append((start,min(end,hi)));y,m=y2,m2
    return out

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--trades',required=True);ap.add_argument('--report',required=True,help='zone_report output folder (train_candidates.csv)')
    ap.add_argument('--lots',type=int,default=10);ap.add_argument('--top',type=int,default=3);ap.add_argument('--from-date',default='2021-06-01');ap.add_argument('--to-date',default='2025-01-01')
    ap.add_argument('--structure',default='all',help='naked, naked|atr30, futures_zone, ... or all (top N for each)');ap.add_argument('--out',default=None,help='CSV of every row printed')
    a=ap.parse_args()
    split=json.load(open(Path(a.report)/'train_report.json'))['split']
    if a.to_date>split:print(f'NOTE: --to-date {a.to_date} is past the split {split}: these figures include holdout data',file=sys.stderr)
    bars,signals,chains,first=zr.load(a.trades)
    db=sqlite3.connect(f'file:{a.trades}?mode=ro',uri=True);pairs=C.defaultdict(list)
    for cid,ed,xd,st,pnl,lot,marks,reason,side,struct,k,x in db.execute('SELECT chain,entry_day,exit_day,status,pnl_inr,lot,marks_at,reason,side,structure,strike,atm FROM trades WHERE status!="no_fill" ORDER BY roll'):
        pairs[cid].append(dict(ed=ed,xd=xd,st=st,pnl=pnl,lot=lot,marks=json.loads(marks or '{}'),reason=reason,side=side,futures=str(struct).startswith('futures'),entry_px=k,exit_px=x))
    sessions=sorted(set(bars.values()))
    eod={d:c for d,c in db.execute('SELECT b.day,b.close FROM bars b JOIN (SELECT day,MAX(idx) i FROM bars GROUP BY day) m ON b.idx=m.i')}
    def daily_path(x):
        """Rupee P&L by session. Futures: marked to each session's close (true MTM). Options: booked on the exit day (no daily option marks stored)."""
        p=x['pair'];out=C.defaultdict(float)
        if not p['futures'] or p['entry_px'] is None:out[x['exit']]+=x['inr'];return out
        units=p['lot']*a.lots*(1 if p['side']=='bull' else -1);prev=p['entry_px']
        for d in [d for d in sessions if x['entry']<=d<=x['exit']]:
            px=eod.get(d,prev) if d<x['exit'] else (p['exit_px'] if p['exit_px'] is not None else eod.get(d,prev))
            out[d]+=units*(px-prev);prev=px
        out[x['exit']]+=x['inr']-sum(out.values())   # costs and rounding land on the exit day, so the path sums to the trade
        return out
    def chain_list(sb,side,fam,off,tp):
        out=[];busy=-1;miss=C.Counter()
        for t in sb:
            if t<=busy:continue
            cid=first.get((side,fam,off,tp,t))
            if cid is None:miss['no_fill']+=1;continue
            c=chains[cid];busy=c['end_bar']
            if not a.from_date<=c['entry_day']<a.to_date:continue
            inr=0.;end=None;why=None;ok=True;last=None
            for p in pairs[cid]:
                if p['ed']>=a.to_date:continue
                last=p
                if p['st']=='closed' and p['xd']<a.to_date:inr+=p['pnl'];end=p['xd'];why=p['reason'];continue
                v=p['marks'].get(a.to_date)
                if v is None:ok=False;break
                inr+=v*p['lot'];end=max(d for d in sessions if d<a.to_date);why='hypothetical_close'
            if not ok:miss['no_value']+=1;continue
            out.append(dict(entry=c['entry_day'],exit=end,inr=inr*a.lots,why=why,pair=dict(last,exit_px=last['exit_px'] if why!='hypothetical_close' else None)))
        return out,miss
    def metrics(tr,lo,hi):
        """Calendar period: P&L earned in [lo,hi) on the daily path; trade counts and win rate from trades closed in it.
        cohort_lakh is the full lifetime P&L of trades entered in the period, a different clock."""
        days=[d for d in sessions if lo<=d<hi];daily=C.defaultdict(float)
        for x in tr:
            for d,v in daily_path(x).items():daily[d]+=v
        closed=[x for x in tr if lo<=x['exit']<hi];cohort=[x for x in tr if lo<=x['entry']<hi]
        if not closed and not cohort:return dict(n=0)
        r=[daily.get(d,0.) for d in days];mu=S.mean(r) if r else 0.;sd=S.pstdev(r) if len(r)>1 else 0.;dn=math.sqrt(S.mean([min(0,v)**2 for v in r])) if r else 0.
        eq=pk=dd=0.
        for v in r:eq+=v;pk=max(pk,eq);dd=max(dd,pk-eq)
        w=[x['inr'] for x in closed] or [0.];gw=sum(v for v in w if v>0);gl=-sum(v for v in w if v<0);yrs=len(days)/252;earned=sum(r)
        f=lambda v:round(v/1e5,2)
        return dict(n=len(closed),pnl_lakh=f(earned),cohort_lakh=f(sum(x['inr'] for x in cohort)),win=round(sum(v>0 for v in w)/len(closed),2) if closed else None,pf=round(gw/gl,2) if gl else None,
                    sharpe=round(mu/sd*math.sqrt(252),2) if sd else None,sortino=round(mu/dn*math.sqrt(252),2) if dn else None,calmar=round(earned/yrs/dd,2) if dd and yrs else None,
                    max_dd_lakh=f(dd),max_loss_lakh=f(min(w)),max_win_lakh=f(max(w)),clock='mtm' if tr and tr[0]['pair']['futures'] else 'exit_booked')
    rows=list(csv.DictReader(open(Path(a.report)/'train_candidates.csv')));printed=[]
    fams=sorted({r['structure'] for r in rows}) if a.structure=='all' else [a.structure]
    for side,fam in [(s_,f_) for s_ in ('bull','bear') for f_ in fams]:
        seen={};picked=[]
        for r in rows:
            if r['side']!=side or r['structure']!=fam:continue
            off=float(r['offset']);tp=None if r['tp']=='None' else float(r['tp'])
            tr,miss=chain_list(signals[r['config'],side],side,fam,off,tp)
            key=(off,tp,tuple((x['entry'],round(x['inr'],2)) for x in tr))
            if key in seen:seen[key]['aliases'].append(r['config']);continue
            item=dict(r,off=off,tp=tp,tr=tr,miss=miss,aliases=[]);seen[key]=item
            if len(picked)<a.top:picked.append(item)
            elif len(seen)>50*a.top:break
        for rank,p in enumerate(picked,1):
            exits=dict(C.Counter(x['why'] for x in p['tr']))
            print(f"\n{side} {fam} #{rank}: {p['config']} strike offset {p['off']:+.0f} take-profit {p['tp']} | same trades: {p['aliases'] or '-'} | exits {exits} | skipped {dict(p['miss'])}")
            print(f"risk clock: {'daily mark-to-market' if p['tr'] and p['tr'][0]['pair']['futures'] else 'P&L booked on exit day (option marks not stored daily: risk understated)'}")
            print('period | ' + ' | '.join(('trades closed','P&L earned lakh','cohort lakh','win','PF','Sharpe','Sortino','Calmar','max DD','max loss','max win')))
            for lo,hi in periods(a.from_date,a.to_date)+[(a.from_date,a.to_date)]:
                m=metrics([x for x in p['tr'] if lo<=x['entry']<hi],lo,hi);label='ALL' if (lo,hi)==(a.from_date,a.to_date) else f'{lo}..{hi}'
                print(label,'|',' | '.join(str(m.get(k,'')) for k in ('n','pnl_lakh','cohort_lakh','win','pf','sharpe','sortino','calmar','max_dd_lakh','max_loss_lakh','max_win_lakh')))
                printed.append(dict(side=side,structure=fam,rank=rank,config=p['config'],offset=p['off'],tp=p['tp'],period=label,**m))
    if a.out:
        keys=sorted({k for r in printed for k in r},key=lambda k:list(printed[0]).index(k) if k in printed[0] else 99)
        with open(a.out,'w',newline='') as f:w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(printed)

if __name__=='__main__':main()
