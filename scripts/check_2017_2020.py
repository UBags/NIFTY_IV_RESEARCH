#!/usr/bin/env python3
"""Second pass over the 2017-2020 NIFTY files, after survey_2017_2020.py: the checks that need every row, before any import.

Conventions settled by the survey and used here: spot and NIFTY_F1 label a bar by its END (09:16..15:30), options by its START
(09:15..15:29); everything below is in bar-START minutes (spot / futures labels minus one), the database convention.

A  contracts    one row per (expiry, CE/PE, strike): source zip, rows, days, duplicate timestamps (identical / conflicting),
                zero-volume rows, rows outside 09:15-15:29, OHLC violations             -> contracts_full.csv
B  overlaps     the same strike in consecutive expiries with IDENTICAL prices on common minutes (a copied file), and every
                contract that stops > 3 days before its expiry: copy? expiring then (time value ~ 0)? or just untraded
C  sanity       front-month option closes below intrinsic (vs NIFTY_F1 in the same minute) by more than max(5 pts, 2%)
D  futures roll per monthly expiry: F1 - spot basis minute by minute on the day before, the expiry day and the day after;
                the minute where F1 switches contract                                   -> roll.csv
E  strike ladder per trading day: current / next monthly, which ATM +- k x 100 strikes (k = 0..5) traded that day -> ladder.csv
F  off-session  rows outside 09:15-15:29 by kind and year; short and special sessions
G  calendar     holiday list for config/nse_holidays.txt (Muhurat days listed as holidays, as the 2021+ list does); per day the
                expiries with data (the listing the engine would see); days whose current or next monthly is missing -> listing.csv
H  database     (--pg) what PostgreSQL holds per table and dataset, and the density / time window of existing option and spot
                data on a sample day (to match the conventions)
Writes everything to --out, plus checks.json."""
import argparse,collections,datetime as dt,glob,json,math,os,re,sys,time,zipfile
import numpy as np
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from survey_2017_2020 import Series,collect_members,parse_name,last_thursday,MONTHS,say,hdr,dfmt,hm   # same parsing as the survey

OPEN,LAST=9*60+15,15*60+29                                   # bar-start minutes of the regular session
def y_of(o):return dt.date.fromordinal(int(o)).year
def iso(o):return dfmt(o)

def load_csvs(root):
    spot,fut=[],[]
    for p in sorted(glob.glob(os.path.join(root,'**','*.csv'),recursive=True)):
        b=os.path.basename(p).upper()
        if b=='NIFTY.CSV':spot.append(p)
        elif b=='NIFTY_F1.CSV':fut.append(p)
    def cat(ps):
        parts=[]
        for p in ps:
            with open(p,encoding='utf-8',errors='replace') as fh:parts.append(Series(p).parse(fh.read()))
        day=np.concatenate([x.day for x in parts]);mi=np.concatenate([x.minute for x in parts])-1      # END label -> bar start
        c=np.concatenate([x.c for x in parts]);return day,mi,c
    return cat(spot),cat(fut)

def zips_by_month(root):
    out={}
    for z in sorted(glob.glob(os.path.join(root,'**','*.zip'),recursive=True)):
        m=re.match(r'([A-Za-z]+)\s*[-_ ]?\s*(\d{4})',os.path.basename(z)[:-4])
        if m and m.group(1).lower() in MONTHS:out[(int(m.group(2)),MONTHS[m.group(1).lower()])]=z
    return out

def monthly_expiry(o,tdays):
    """Monthly expiry the market would trade as 'current' on day o (last Thursday, holiday-shifted, on or after o)."""
    d=dt.date.fromordinal(o);e=last_thursday(d.year,d.month,tdays)
    if e<o:
        n=(d.replace(day=28)+dt.timedelta(days=4));e=last_thursday(n.year,n.month,tdays)
    return e
def next_monthly(e,tdays):
    d=dt.date.fromordinal(e)+dt.timedelta(days=10);return last_thursday(d.year,d.month,tdays)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--max-zips',type=int,default=0);ap.add_argument('--pg',action='store_true',help='also section H (PG* variables)')
    a=ap.parse_args(argv);t0=time.time();os.makedirs(a.out,exist_ok=True);root=os.path.expanduser(a.root);J={}
    (sd,sm,sc),(fd,fm,fc)=load_csvs(root)
    sess=lambda m:(m>=OPEN)&(m<=LAST)
    SK=dict(zip((sd*1440+sm)[sess(sm)].tolist(),sc[sess(sm)].tolist()));FK=dict(zip((fd*1440+fm)[sess(fm)].tolist(),fc[sess(fm)].tolist()))
    tdays=sorted(set(sd[sess(sm)].tolist()));tset=set(tdays)
    sclose={}                                                 # last regular-session spot close per day
    for k in sorted(SK):sclose[k//1440]=SK[k]
    # ---------------------------------------------------------------- F off-session rows (spot / futures)
    hdr('F  ROWS OUTSIDE 09:15-15:29 (bar start; spot / futures labels moved back one minute)')
    for nm,d_,m_ in (('spot',sd,sm),('NIFTY_F1',fd,fm)):
        off=~sess(m_);byy=collections.Counter(y_of(x) for x in d_[off]);late=collections.Counter(int(x) for x in m_[off&(m_>LAST)])
        say(f'  {nm}: {int(off.sum())} rows off-session ({dict(sorted(byy.items()))}); starts after 15:29: '+', '.join(f'{hm(k)} x{v}' for k,v in late.most_common(8)))
        evening=sorted({int(x) for x,mm in zip(d_,m_) if mm>=17*60});ev_only=[x for x in evening if x not in set(d_[sess(m_)].tolist())]
        say(f'     days with rows after 17:00: {[iso(x) for x in evening]}; of which no regular session (Muhurat): {[iso(x) for x in ev_only]}')
    pd_=collections.Counter(sd[sess(sm)].tolist());usual=collections.Counter(pd_.values()).most_common(1)[0][0]
    short=sorted(d for d,n in pd_.items() if n<0.9*usual)
    say(f'  regular-session spot minutes per day: usual {usual}; short sessions (< 90%): '+', '.join(f'{iso(d)} ({pd_[d]})' for d in short))
    J['short_sessions']=[iso(d) for d in short]
    # ---------------------------------------------------------------- A/B/C/E options, zip by zip
    hdr('A  OPTION CONTRACTS  /  B  OVERLAPS  /  C  SANITY')
    Z=zips_by_month(root);items=sorted(Z.items())[:a.max_zips] if a.max_zips else sorted(Z.items())
    rows=[];prev=None;copies=[];early=[];below=collections.Counter();below_n=collections.Counter();below_ex=[];traded=collections.defaultdict(set)
    anyrow=collections.defaultdict(set);offs=collections.Counter();offs_late=collections.Counter();dup_tot=collections.Counter();zero_by=collections.Counter();rows_by=collections.Counter()
    expiries=[]
    for (y,mo),zp in items:
        E=last_thursday(y,mo,tset);expiries.append(E);found,_,notes=collect_members(zipfile.ZipFile(zp));cur={}
        for Zm,i,trail in found:
            base=os.path.splitext(os.path.basename(i.filename))[0];pn=parse_name(base)
            if not pn:continue
            cp,k,_,_=pn;S=Series(base).parse(Zm.read(i).decode('utf-8','replace'))
            if not S.n:continue
            key=S.day*1440+S.minute;ins=sess(S.minute)
            u,first,cnt=np.unique(key,return_index=True,return_counts=True);dupk=u[cnt>1]
            conflict=0
            for q in dupk[:2000]:
                w=np.flatnonzero(key==q);vals={(S.o[j],S.h[j],S.l[j],S.c[j],S.v[j]) for j in w};conflict+=len(vals)>1
            bad=(S.l>np.minimum(S.o,S.c)+1e-9)|(S.h<np.maximum(S.o,S.c)-1e-9)|(np.minimum.reduce([S.o,S.h,S.l,S.c])<=0)
            yr=y_of(E);offs[yr]+=int((~ins).sum());offs_late[yr]+=int((S.minute>LAST).sum());dup_tot['dup']+=int(cnt[cnt>1].sum()-len(dupk));dup_tot['conflict']+=conflict
            zero_by[yr]+=int((S.v==0).sum());rows_by[yr]+=S.n
            for d_ in set(S.day[ins&(S.v>0)].tolist()):traded[d_].add((E,cp,k))
            for d_ in set(S.day[ins].tolist()):anyrow[d_].add(E)
            kk=key[ins];cc=S.c[ins];order=np.argsort(kk,kind='stable');kk=kk[order];cc=cc[order]
            cur[(cp,k)]=(kk,cc)
            # C: front-month closes below intrinsic vs F1 (F1 is this contract while it is the current monthly)
            front=np.array([monthly_expiry(int(d_),tset)==E for d_ in S.day[ins]]) if ins.any() else np.zeros(0,bool)
            if front.any():
                fk=key[ins][front];px=S.c[ins][front];f=np.array([FK.get(int(q),np.nan) for q in fk])
                intr=np.maximum(0,(f-k) if cp=='CE' else (k-f));gap=intr-px;ok=np.isfinite(f)
                flag=ok&(gap>np.maximum(5,.02*px))
                below[yr]+=int(flag.sum());below_n[yr]+=int(ok.sum())
                if flag.any() and len(below_ex)<12:
                    j=np.flatnonzero(flag)[np.argmax(gap[flag])];q=int(fk[j]);below_ex.append(f'{iso(E)} {cp} {k:g} {iso(q//1440)} {hm(q%1440)}: close {px[j]:g}, F1 {f[j]:g}, intrinsic {intr[j]:.1f}')
            ld=int(S.day.max());fdy=int(S.day.min())
            rows.append(dict(expiry=iso(E),cp=cp,strike=k,zip=f'{y}-{mo:02d}',member='/'.join(os.path.basename(t) for t in trail+(i.filename,)),
                             first_day=iso(fdy),last_day=iso(ld),rows=S.n,days=len(set(S.day.tolist())),duplicate_rows=int(cnt[cnt>1].sum()-len(dupk)),conflicting_duplicates=conflict,
                             zero_volume_rows=int((S.v==0).sum()),off_session_rows=int((~ins).sum()),ohlc_bad=int(bad.sum()),has_oi=bool(np.isfinite(S.oi).any() and (S.oi>0).any())))
            if ld<E-3 and S.n>=50:                            # B: stops early: copy of the previous expiry's file? expiring that day?
                lastk=int(kk[-1]) if len(kk) else None;s_=SK.get(lastk) if lastk else None
                tv=None if s_ is None else float(cc[-1]-max(0,(s_-k) if cp=='CE' else (k-s_)))
                early.append(dict(expiry=iso(E),cp=cp,strike=k,last_day=iso(ld),rows=S.n,last_bar=None if lastk is None else hm(lastk%1440),
                                  time_value_at_last_bar=None if tv is None else round(tv,2),last_day_is_an_expiry=ld in set(expiries)|{last_thursday(dt.date.fromordinal(ld).year,dt.date.fromordinal(ld).month,tset)}))
        if prev is not None:                                  # B: identical prices on common minutes with the same strike one expiry earlier
            Ep,P=prev
            for ck,(kk,cc) in cur.items():
                if ck not in P:continue
                pk,pc=P[ck];com,ia,ib=np.intersect1d(kk,pk,return_indices=True)
                if len(com)>=20:
                    same=float(np.mean(cc[ia]==pc[ib]))
                    if same>.5:copies.append(dict(expiry=iso(E),previous_expiry=iso(Ep),cp=ck[0],strike=ck[1],common_minutes=int(len(com)),identical_close_share=round(same,3)))
        for e_ in early:
            if e_['expiry']==iso(E):
                m=[c for c in copies if c['expiry']==e_['expiry'] and c['cp']==e_['cp'] and c['strike']==e_['strike']]
                e_['copy_of_previous']=m[0]['identical_close_share'] if m else 0.
        prev=(E,cur);say(f'  {y}-{mo:02d}: expiry {iso(E)}, {len(cur)} contracts'+(f'  [{" | ".join(notes)}]' if notes else ''))
    import csv
    with open(os.path.join(a.out,'contracts_full.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    say(f'\n  A  {len(rows)} contracts; duplicate timestamps: {dup_tot["dup"]} rows ({dup_tot["conflict"]} with different values); rows by year {dict(rows_by)}')
    say('     zero-volume rows by year: '+', '.join(f'{y_}: {zero_by[y_]} ({100*zero_by[y_]/max(1,rows_by[y_]):.2f}%)' for y_ in sorted(rows_by)))
    say('     option rows outside 09:15-15:29 by expiry year: '+', '.join(f'{y_}: {offs[y_]} (after 15:29: {offs_late[y_]})' for y_ in sorted(offs)))
    say(f'     contracts with OHLC violations: {sum(1 for r in rows if r["ohlc_bad"])}; with an OI column: {sum(1 for r in rows if r["has_oi"])}')
    cnt=collections.Counter((r['expiry'],r['cp'],r['strike']) for r in rows);dd=[k for k,v in cnt.items() if v>1]
    say(f'  B  the same contract in two zips: {len(dd)}'+(f' e.g. {dd[:5]}' if dd else ''))
    say(f'     strikes with identical prices to the previous expiry on common minutes (copied files): {len(copies)}')
    for c in copies[:20]:say(f'       {c}')
    say(f'     contracts stopping > 3 days before expiry with >= 50 rows: {len(early)}')
    for e_ in early:
        v=('COPY of the previous expiry' if e_.get('copy_of_previous',0)>.5 else 'EXPIRING that day (time value ~0): previous expiry mislabelled' if e_['last_day_is_an_expiry'] and e_['time_value_at_last_bar'] is not None and abs(e_['time_value_at_last_bar'])<3 else 'untraded later (genuine)')
        say(f'       {e_["expiry"]} {e_["cp"]} {e_["strike"]:g}: last {e_["last_day"]} {e_["last_bar"]}, rows {e_["rows"]}, time value at last bar {e_["time_value_at_last_bar"]}, identical-to-previous {e_.get("copy_of_previous",0)} -> {v}')
    say('  C  front-month closes below F1 intrinsic by > max(5 pts, 2%): '+', '.join(f'{y_}: {below[y_]} of {below_n[y_]}' for y_ in sorted(below_n)))
    for x in below_ex:say(f'       {x}')
    J.update(copies=copies,early=early,below={str(k):[below[k],below_n[k]] for k in below_n})
    # ---------------------------------------------------------------- D futures roll
    hdr('D  FUTURES ROLL (F1 - spot, regular session, bar starts)')
    allexp=sorted(set(expiries)|{last_thursday(y,m,tset) for y in range(2017,2021) for m in range(1,13)})
    ti={d:i for i,d in enumerate(tdays)};roll=[]
    for E in allexp:
        if E not in ti:continue
        out=dict(expiry=iso(E))
        for lab,o in (('before',-1),('expiry',0),('after',1)):
            j=ti[E]+o
            if not 0<=j<len(tdays):continue
            d_=tdays[j];ks=[d_*1440+m for m in range(OPEN,LAST+1) if d_*1440+m in SK and d_*1440+m in FK]
            if not ks:continue
            b=np.array([FK[q]-SK[q] for q in ks]);out[f'{lab}_open']=round(float(b[:5].mean()),1);out[f'{lab}_close']=round(float(b[-5:].mean()),1)
            if lab=='expiry':
                db=np.abs(np.diff(b));i_=int(np.argmax(db)) if len(db) else 0
                out['largest_jump']=round(float(db[i_]),1) if len(db) else None;out['jump_at']=hm(ks[i_+1]%1440) if len(db) else None
                out['basis_1500_1529_mean']=round(float(b[[n for n,q in enumerate(ks) if q%1440>=15*60]].mean()),1)
        e=out.get('expiry_close');a_=out.get('after_open')
        out['verdict']=('expiring contract to the close' if e is not None and abs(e)<3 else
                        f'switched at {out.get("jump_at")}' if out.get('largest_jump',0)>=max(8,abs(e or 0)/2) else 'unclear: look at the minutes')
        roll.append(out)
    for r in roll:say('  '+', '.join(f'{k} {v}' for k,v in r.items()))
    with open(os.path.join(a.out,'roll.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=sorted({k for r in roll for k in r},key=lambda x:(x!='expiry',x)));w.writeheader();w.writerows(roll)
    J['roll']=roll
    # ---------------------------------------------------------------- E strike ladder
    try:
        from nifty_iv.core import strategy_spec as ss;step=ss.distance_step;atmf=ss.atm;how='strategy_spec (100 below NIFTY 12,000, 200 above)'
    except Exception:step=lambda x:100;atmf=lambda x:round(x/100)*100;how='100 points (nifty_iv not importable)'
    hdr(f'E  STRIKE LADDER (strikes ATM +- k steps with a traded bar that day; ATM and step as {how}; ATM from the last spot close)')
    lad=[];agg=collections.defaultdict(collections.Counter);dn=collections.Counter()
    for d_ in tdays:
        if d_ not in sclose:continue
        atm=atmf(sclose[d_]);st=step(sclose[d_]);m1=monthly_expiry(d_,tset);m2=next_monthly(m1,tset);T=traded.get(d_,set());yr=y_of(d_);dn[yr]+=1
        row=dict(day=iso(d_),spot=sclose[d_],atm=atm,step=st,m1=iso(m1),m2=iso(m2))
        for tag,E in (('m1',m1),('m2',m2)):
            for cp in ('PE','CE'):
                ks=[kk for kk in range(-5,6) if (E,cp,float(atm+st*kk)) in T];row[f'{tag}_{cp}']=' '.join(map(str,ks))
                for kk in ks:agg[(yr,tag,cp)][kk]+=1
        lad.append(row)
    with open(os.path.join(a.out,'ladder.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=list(lad[0]));w.writeheader();w.writerows(lad)
    for yr in sorted(dn):
        for tag in ('m1','m2'):
            for cp in ('PE','CE'):
                c=agg[(yr,tag,cp)];say(f'  {yr} {tag} {cp}: '+'  '.join(f'{kk:+d}:{100*c[kk]/dn[yr]:3.0f}%' for kk in range(-5,6)))
    J['ladder']={f'{k[0]} {k[1]} {k[2]}':{str(kk):round(v/dn[k[0]],3) for kk,v in c.items()} for k,c in agg.items()}
    # ---------------------------------------------------------------- G calendar and listing
    hdr('G  CALENDAR / LISTING')
    lo,hi=tdays[0],tdays[-1]
    wk=[o for o in range(lo,hi+1) if dt.date.fromordinal(o).weekday()<5]
    muh=sorted(({int(x) for x,mm in zip(sd,sm) if mm>=17*60}-tset) & set(wk))       # evening rows and no regular session
    hol=sorted((set(o for o in wk if o not in tset)|set(muh)))
    for yr in range(2017,2021):say(f'  holidays {yr} ({sum(1 for o in hol if y_of(o)==yr)}): '+' '.join(iso(o) for o in hol if y_of(o)==yr))
    with open(os.path.join(a.out,'nse_holidays_2017_2020.txt'),'w') as fh:fh.write('\n'.join(iso(o) for o in hol)+'\n')
    wkend=sorted(o for o in tset if dt.date.fromordinal(o).weekday()>=5)
    say(f'  weekend regular sessions: {[iso(o) for o in wkend]}  (Muhurat weekday sessions counted as holidays: {[iso(o) for o in muh]})')
    lst=[];miss1=[];miss2=[]
    for d_ in tdays:
        L=sorted(anyrow.get(d_,()));m1=monthly_expiry(d_,tset);m2=next_monthly(m1,tset)
        lst.append(dict(day=iso(d_),expiries=' '.join(iso(e) for e in L),m1=iso(m1),m1_present=m1 in L,m2=iso(m2),m2_present=m2 in L))
        if m1 not in L:miss1.append(d_)
        if m2 not in L:miss2.append(d_)
    with open(os.path.join(a.out,'listing.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=list(lst[0]));w.writeheader();w.writerows(lst)
    def runs(ds):
        out=[];s=None
        for d_ in ds:
            if s is None or ti[d_]!=ti[p_]+1:
                if s is not None:out.append((s,p_))
                s=d_
            p_=d_
        if s is not None:out.append((s,p_))
        return out
    say('  listing around the missing January 2018 zip:')
    for r in lst:
        if '2017-12-20'<=r['day']<='2018-02-05':say(f"    {r['day']}: {r['expiries']}  (current monthly {r['m1']} {'present' if r['m1_present'] else 'MISSING'}, next {r['m2']} {'present' if r['m2_present'] else 'MISSING'})")
    say('  days without data for the CURRENT monthly: '+'; '.join(f'{iso(a_)}..{iso(b_)} ({ti[b_]-ti[a_]+1})' for a_,b_ in runs(miss1)))
    say('  days without data for the NEXT monthly:    '+'; '.join(f'{iso(a_)}..{iso(b_)} ({ti[b_]-ti[a_]+1})' for a_,b_ in runs(miss2)))
    first=collections.defaultdict(lambda:10**9)
    for d_,es in anyrow.items():
        for e in es:first[e]=min(first[e],d_)
    say('  first day with data per expiry (sessions before the expiry): '+', '.join(f'{iso(e)}: {iso(first[e])} ({sum(1 for d_ in tdays if first[e]<=d_<=e)})' for e in sorted(first)))
    J.update(holidays=[iso(o) for o in hol],missing_m1=[[iso(a_),iso(b_)] for a_,b_ in runs(miss1)],missing_m2=[[iso(a_),iso(b_)] for a_,b_ in runs(miss2)])
    # ---------------------------------------------------------------- H database
    if a.pg:
        hdr('H  POSTGRESQL')
        try:
            import psycopg
            with psycopg.connect() as pg:
                pg.execute("SET statement_timeout='300s'")
                for r in pg.execute("SELECT dataset,market_kind,status,count(*),sum(loaded_rows) FROM market_data.import_files GROUP BY 1,2,3 ORDER BY 1,2,3"):say(f'  import_files: {r}')
                for t,w in (('option_bars',"underlying='NIFTY'"),('spot_bars',"symbol='NIFTY'"),('future_bars',"underlying='NIFTY'")):
                    say(f'  {t}: trading days {pg.execute(f"SELECT min(trading_day),max(trading_day) FROM market_data.{t} WHERE {w}").fetchone()}')
                for (ds,) in pg.execute("SELECT DISTINCT dataset FROM market_data.import_files WHERE market_kind='option'").fetchall():
                    d0=pg.execute("""SELECT min(b.trading_day) FROM market_data.option_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id
                                     WHERE f.dataset=%s AND b.underlying='NIFTY' AND b.trading_day>='2021-06-01'""",(ds,)).fetchone()[0]
                    if d0 is None:continue
                    q=pg.execute("""SELECT count(*),avg(n),min(t0),max(t1),avg(z) FROM (SELECT b.expiry,b.strike,b.option_type,count(*) n,
                        min((b.bar_time AT TIME ZONE 'Asia/Kolkata')::time) t0,max((b.bar_time AT TIME ZONE 'Asia/Kolkata')::time) t1,avg((b.volume=0)::int) z
                        FROM market_data.option_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id
                        WHERE f.dataset=%s AND b.underlying='NIFTY' AND b.trading_day=%s GROUP BY 1,2,3) x""",(ds,d0)).fetchone()
                    say(f'  options {ds} on {d0}: {q[0]} contracts, {q[1]:.0f} bars each on average (375 = every minute), times {q[2]}..{q[3]}, zero-volume share {q[4]:.3f}')
                q=pg.execute("""SELECT trading_day,count(*),min((bar_time AT TIME ZONE 'Asia/Kolkata')::time),max((bar_time AT TIME ZONE 'Asia/Kolkata')::time),count(DISTINCT source_file_id)
                    FROM market_data.spot_bars WHERE symbol='NIFTY' AND trading_day=(SELECT min(trading_day) FROM market_data.spot_bars WHERE symbol='NIFTY' AND trading_day>='2021-06-01') GROUP BY 1""").fetchone()
                say(f'  spot sample: {q}')
        except Exception as e:say(f'  database not checked: {type(e).__name__}: {e}')
    with open(os.path.join(a.out,'checks.json'),'w') as fh:json.dump(J,fh,indent=1,default=str)
    say(f'\n-> {a.out}: contracts_full.csv, roll.csv, ladder.csv, listing.csv, nse_holidays_2017_2020.txt, checks.json ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
