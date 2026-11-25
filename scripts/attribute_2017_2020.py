#!/usr/bin/env python3
"""Third pass over the 2017-2020 option zips: WHICH expiry do the rows of each file really belong to, day by day?

check_2017_2020.py found files whose prices are identical to the same strike one expiry earlier (April 2017 vs March,
March 2018 vs February, a few in May 2019 and March 2020). Whole-file shares cannot say which days are affected, nor
whether rows in a gap month (March 2018 zip, 29 Dec 2017 - 25 Jan 2018, January zip missing) are a third contract.

Per (zip, file, day):   bars; share of minutes with a close identical to the same strike in the previous / next zip
Per (zip, day):         ATM straddle at the last common minute: time value TV = C + P - |F - K| and the implied
                        volatility it gives under the zip's expiry, the previous and the next monthly expiry
                        (sigma ~ TV / (0.7979 F sqrt(T)); F = spot + (F1 - spot) x T / T_front, F1 the front future);
                        the same for the other zips that day: a file holding the wrong contract shows a volatility
                        far from the other zips under its own label and close to them under the right one;
                        on an expiry day the expiring contract's TV goes to ~0 at 15:29. Also under the CURRENT monthly expiry (for the
                        January 2018 window, where the previous zip is missing: are the March 2018 zip's rows the January contract?).
Writes attribution_files.csv, attribution_days.csv; prints the suspect days and the January 2018 window."""
import argparse,collections,csv,datetime as dt,math,os,sys,time,zipfile
import numpy as np
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from survey_2017_2020 import Series,collect_members,parse_name,last_thursday,say,hdr,dfmt,hm
from check_2017_2020 import load_csvs,zips_by_month,monthly_expiry,next_monthly,OPEN,LAST

def load_zip(zp):
    """{(cp, strike): (key, close, volume)} regular-session rows (bar start), sorted by key."""
    out={};found,_,_=collect_members(zipfile.ZipFile(zp))
    for Zm,i,_ in found:
        pn=parse_name(os.path.splitext(os.path.basename(i.filename))[0])
        if not pn:continue
        S=Series(i.filename).parse(Zm.read(i).decode('utf-8','replace'))
        ok=(S.minute>=OPEN)&(S.minute<=LAST)
        k=(S.day*1440+S.minute)[ok];o=np.argsort(k,kind='stable')
        out[pn[:2]]=(k[o],S.c[ok][o],S.v[ok][o])
    return out

def tdays_to(day,minute,expiry):
    """Calendar days from (day, minute) to the expiry's 15:30."""
    return max(expiry-day+(15*60+30-minute)/1440.,1/1440.)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root',required=True);ap.add_argument('--out',required=True);ap.add_argument('--max-zips',type=int,default=0)
    ap.add_argument('--copy-share',type=float,default=.5,help='a file-day is a copy when this share of its minutes equals the neighbour zip')
    a=ap.parse_args(argv);t0=time.time();os.makedirs(a.out,exist_ok=True)
    (sd,sm,sc),(fd,fm,fc)=load_csvs(os.path.expanduser(a.root))
    sess=lambda m:(m>=OPEN)&(m<=LAST)
    SK=dict(zip((sd*1440+sm)[sess(sm)].tolist(),sc[sess(sm)].tolist()));FK=dict(zip((fd*1440+fm)[sess(fm)].tolist(),fc[sess(fm)].tolist()))
    tset=set(sd[sess(sm)].tolist())
    Z=sorted(zips_by_month(os.path.expanduser(a.root)).items());Z=Z[:a.max_zips] if a.max_zips else Z
    E=[last_thursday(y,m,tset) for (y,m),_ in Z]
    cache={}
    def get(i):
        if i<0 or i>=len(Z):return {}
        if i not in cache:cache[i]=load_zip(Z[i][1])
        return cache[i]
    frows=[];drows=[];straddle=collections.defaultdict(dict)          # straddle[day][zip index] = (K, minute, C, P, S, F1)
    for i,((y,mo),zp) in enumerate(Z):
        for j in list(cache):
            if j<i-1:del cache[j]
        cur,prv,nxt=get(i),get(i-1),get(i+1)
        for ck,(k,c,v) in cur.items():
            day=k//1440;ud,inv=np.unique(day,return_inverse=True);n=np.bincount(inv,minlength=len(ud))
            res={}
            for tag,N in (('prev',prv),('next',nxt)):
                same=np.zeros(len(ud));com=np.zeros(len(ud))
                if ck in N:
                    pk,pc,_=N[ck];_,ia,ib=np.intersect1d(k,pk,return_indices=True)
                    if len(ia):
                        com=np.bincount(inv[ia],minlength=len(ud)).astype(float);same=np.bincount(inv[ia],weights=(c[ia]==pc[ib]).astype(float),minlength=len(ud))
                res[tag]=(com,same)
            for q,d_ in enumerate(ud.tolist()):
                cp_,cs_=res['prev'][0][q],res['prev'][1][q];cn_,ns_=res['next'][0][q],res['next'][1][q]
                frows.append(dict(zip=f'{y}-{mo:02d}',expiry=dfmt(E[i]),cp=ck[0],strike=ck[1],day=dfmt(d_),bars=int(n[q]),
                                  common_prev=int(cp_),identical_prev=round(cs_/cp_,3) if cp_ else None,common_next=int(cn_),identical_next=round(ns_/cn_,3) if cn_ else None))
        # ATM straddle per day for this zip
        strikes=sorted({k_ for cp_,k_ in cur if ('CE',k_) in cur and ('PE',k_) in cur})
        days=sorted({int(x) for (cp_,k_),(kk,_,_) in cur.items() for x in np.unique(kk//1440)})
        for d_ in days:
            ref=SK.get(d_*1440+15*60) or next((SK[d_*1440+m] for m in range(LAST,OPEN-1,-1) if d_*1440+m in SK),None)
            if ref is None:continue
            best=None
            for K in sorted(strikes,key=lambda x:abs(x-ref))[:3]:
                ck_,cc_,_=cur[('CE',K)];pk_,pc_,_=cur[('PE',K)]
                lo,hi=d_*1440+OPEN,d_*1440+LAST+1
                a1=slice(*np.searchsorted(ck_,[lo,hi]));a2=slice(*np.searchsorted(pk_,[lo,hi]))
                com,ia,ib=np.intersect1d(ck_[a1],pk_[a2],return_indices=True)
                com=[q for q in range(len(com)) if int(com[q]) in SK and int(com[q]) in FK]
                if com:
                    q=com[-1];key=int(ck_[a1][ia[q]]);best=(K,key%1440,float(cc_[a1][ia[q]]),float(pc_[a2][ib[q]]),SK[key],FK[key]);break
            if best:straddle[d_][i]=best
        say(f'  {y}-{mo:02d}: expiry {dfmt(E[i])}, {len(cur)} files, {len(days)} days')
    with open(os.path.join(a.out,'attribution_files.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=list(frows[0]));w.writeheader();w.writerows(frows)
    # ---------------------------------------------------------------- per (zip, day) volatility under each candidate expiry
    allexp=sorted(set(E)|{last_thursday(yy,mm,tset) for yy in range(2016,2022) for mm in range(1,13)})
    def prev_m(e):
        j=allexp.index(e) if e in allexp else None;return allexp[j-1] if j else None
    def next_m(e):
        j=allexp.index(e) if e in allexp else None;return allexp[j+1] if j is not None and j+1<len(allexp) else None
    def sigma(K,m,C,P,S,F1,d_,e):
        e1=monthly_expiry(d_,tset);T1=tdays_to(d_,m,e1);T=tdays_to(d_,m,e)
        F=F1 if e==e1 or T1<1 else S+(F1-S)*T/T1
        tv=C+P-abs(F-K);return round(tv,2),round(100*tv/(0.7979*F*math.sqrt(T/365)),1)
    copy=collections.defaultdict(lambda:[0,0])
    for r in frows:
        if r['identical_prev'] is not None and r['common_prev']>=5:copy[(r['zip'],r['day'])][1]+=1;copy[(r['zip'],r['day'])][0]+=r['identical_prev']>=a.copy_share
    for d_,byz in sorted(straddle.items()):
        lab={i:sigma(*v,d_,E[i]) for i,v in byz.items()}
        for i,(K,m,C,P,S,F1) in byz.items():
            others=[lab[j][1] for j in byz if j!=i]
            tvp,sp=sigma(K,m,C,P,S,F1,d_,prev_m(E[i])) if prev_m(E[i]) and prev_m(E[i])>=d_ else (None,None)
            tvn,sn=sigma(K,m,C,P,S,F1,d_,next_m(E[i])) if next_m(E[i]) else (None,None)
            cm=monthly_expiry(d_,tset);_,sc_=sigma(K,m,C,P,S,F1,d_,cm)
            z=f'{Z[i][0][0]}-{Z[i][0][1]:02d}';cc=copy.get((z,dfmt(d_)),[0,0])
            drows.append(dict(day=dfmt(d_),zip=z,expiry=dfmt(E[i]),current_monthly=dfmt(monthly_expiry(d_,tset)),atm_strike=K,minute=hm(m),call=C,put=P,spot=S,F1=F1,
                              time_value=lab[i][0],iv_label=lab[i][1],iv_prev_expiry=sp,iv_next_expiry=sn,iv_current_monthly=sc_,iv_other_zips=min(others,key=lambda x:abs(x-lab[i][1])) if others else None,
                              files_identical_to_prev=f'{cc[0]}/{cc[1]}'))
    with open(os.path.join(a.out,'attribution_days.csv'),'w',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=list(drows[0]));w.writeheader();w.writerows(drows)
    # ---------------------------------------------------------------- report
    hdr('COPIED FILE-DAYS (identical closes on >= copy-share of common minutes with the previous zip)')
    byzip=collections.defaultdict(list)
    for (z,d_),(k,n) in sorted(copy.items()):
        if k:byzip[z].append((d_,k,n))
    for z,L in byzip.items():
        say(f'  {z}: {sum(k for _,k,_ in L)} file-days on {len(L)} days, {L[0][0]}..{L[-1][0]}: '+', '.join(f'{d_[5:]} {k}/{n}' for d_,k,n in L))
    if not byzip:say('  none')
    hdr('ZIP-DAYS WHOSE STRADDLE DOES NOT FIT THEIR OWN EXPIRY (iv under the label vs the other zips that day off by > 40%, or time value <= 0)')
    sus=[r for r in drows if (r['iv_other_zips'] and r['iv_label']>0 and not .6<r['iv_label']/r['iv_other_zips']<1.4) or r['time_value']<=0.5 and r['expiry']!=r['day']]
    for r in sus[:200]:say(f"  {r['day']} zip {r['zip']} (exp {r['expiry']}): K {r['atm_strike']:g} at {r['minute']} C {r['call']:g} P {r['put']:g} TV {r['time_value']}; iv label {r['iv_label']} prev {r['iv_prev_expiry']} next {r['iv_next_expiry']} current-monthly {r['iv_current_monthly']} | other zips {r['iv_other_zips']} | identical-to-prev files {r['files_identical_to_prev']}")
    say(f'  ({len(sus)} zip-days)')
    for lo,hi,t in (('2017-03-10','2017-04-03','April 2017 zip window'),('2017-12-26','2018-03-02','January 2018 gap / March 2018 zip'),('2019-03-28','2019-05-03','May 2019 zip window'),('2020-02-20','2020-03-30','March 2020 zip window')):
        hdr(t)
        for r in drows:
            if lo<=r['day']<=hi:say(f"  {r['day']} zip {r['zip']} (exp {r['expiry']}, current monthly {r['current_monthly']}): K {r['atm_strike']:g} {r['minute']} TV {r['time_value']:>7}; iv label {r['iv_label']:>6} prev {r['iv_prev_expiry']} next {r['iv_next_expiry']} current-monthly {r['iv_current_monthly']} | other zips {r['iv_other_zips']} | identical-to-prev {r['files_identical_to_prev']}")
    say(f'\n-> {a.out}: attribution_files.csv, attribution_days.csv ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
