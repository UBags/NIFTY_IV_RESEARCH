"""Survey of the 2017-2020 NIFTY spot / futures / options files BEFORE any import: what exists, its format, its conventions and its
defects, so the importer can be written against facts. Reads only; writes a report (stdout), a JSON summary and per-contract CSV.

    python scripts/survey_2017_2020.py --root data_2017_2020 --out runs/survey_2017_2020 | tee runs/survey_2017_2020.log
    python scripts/survey_2017_2020.py --root data_2017_2020 --out /tmp/s --max-zips 3      # quick look

Expected layout (found by searching, names need not match exactly):
  <root>/<...spot and futures...>/<YYYY>/NIFTY.csv, NIFTY_F1.csv      rows: SYMBOL,YYYY/MM/DD,HH:MM,O,H,L,C,V,OI
  <root>/<...options...>/<Month YYYY>.zip  ->  'PE 9150.txt' ...       rows: 'PE 9150',YYYY/MM/DD,HH:MM,O,H,L,C,V

Sections:
  1 inventory        files, zips (missing months), members, sizes
  2 format           field counts, symbols vs file names, unparseable rows (examples), header / BOM / blank lines
  3 time convention  first / last minute and bars per day, duplicates, unsorted rows, minutes outside 09:15-15:30
  4 calendar         trading days per year; weekdays without data (holiday candidates); weekend sessions; days missing per dataset
  5 prices           OHLC order, non-positive prices, jumps; spot vs futures basis
  6 volume / OI      what the columns hold (tick counts? units? lots of 75?)
  7 option contracts per zip: inferred expiry (last date), last-Thursday check, strikes and step, listing span, rows after
                     expiry, coverage near expiry, members that end early (other expiries mixed in?)
  8 futures roll     F1 basis around every monthly expiry: which contract F1 holds on expiry day, when it rolls
  9 alignment        put-call parity: implied forward K + C - P vs F1 (points) and the minute offset that fits best, i.e. whether
                     the options and futures timestamps mean the same minute; spot vs futures minute-return lag
 10 database         (optional --pg) what PostgreSQL already holds, overlap and the gap to the 2021+ data
 11 decisions        what the importer must settle, from the findings"""
import argparse,collections,datetime as dt,glob,io,json,math,os,re,sys,time,zipfile
import numpy as np

MONTHS={m:i for i,m in enumerate(['january','february','march','april','may','june','july','august','september','october','november','december'],1)}
OPEN,CLOSE=9*60+15,15*60+30
ROW=re.compile(r'^\s*([^,]+),(\d{4})[/-](\d{2})[/-](\d{2}),(\d{1,2}):(\d{2})(?::(\d{2}))?,(.*)$')

def say(*a):print(*a,flush=True)
def hdr(t):say(f'\n{"="*100}\n{t}\n{"="*100}')
def dfmt(o):return dt.date.fromordinal(int(o)).isoformat()
def hm(m):return f'{int(m)//60:02d}:{int(m)%60:02d}'

class Series:
    """Parsed rows of one file: arrays day (ordinal), minute (of day), o h l c v oi; plus format statistics."""
    def __init__(self,name):
        self.name=name;self.nfields=collections.Counter();self.symbols=collections.Counter();self.bad=[];self.nbad=0
        self.blank=0;self.header=None;self.bom=False;self.crlf=False;self.unsorted=0;self.seconds=0
    def parse(self,text):
        if text.startswith('﻿'):self.bom=True;text=text[1:]
        self.crlf='\r\n' in text[:10000]
        D=[];M=[];X=[];prev=None
        for i,line in enumerate(text.splitlines()):
            if not line.strip():self.blank+=1;continue
            m=ROW.match(line)
            if not m:
                if i==0 and not any(ch.isdigit() for ch in line.split(',')[1:2][0] if line.count(',')):self.header=line[:120];continue
                self.nbad+=1
                if len(self.bad)<5:self.bad.append((i+1,line[:120]))
                continue
            sym,y,mo,d,hh,mm,ss,rest=m.groups();f=rest.split(',');self.nfields[len(f)+3]+=1;self.symbols[sym.strip()]+=1
            if ss and ss!='00':self.seconds+=1
            try:
                day=dt.date(int(y),int(mo),int(d)).toordinal();vals=[float(x) if x.strip() else math.nan for x in f[:6]]
            except ValueError:
                self.nbad+=1
                if len(self.bad)<5:self.bad.append((i+1,line[:120]))
                continue
            while len(vals)<6:vals.append(math.nan)
            key=(day,int(hh)*60+int(mm))
            if prev is not None and key<prev:self.unsorted+=1
            prev=key;D.append(day);M.append(int(hh)*60+int(mm));X.append(vals)
        self.day=np.array(D,np.int64);self.minute=np.array(M,np.int64);X=np.array(X,float).reshape(len(D),6)
        self.o,self.h,self.l,self.c,self.v,self.oi=X.T;self.n=len(D);return self
    def key(self):return self.day*1440+self.minute

def per_day(S):
    """{day: (first minute, last minute, bars, duplicates)}"""
    out={}
    if not S.n:return out
    k=S.key();order=np.lexsort((S.minute,S.day));d=S.day[order];m=S.minute[order];kk=k[order]
    cut=np.flatnonzero(np.diff(d))+1
    for a,b in zip(np.r_[0,cut],np.r_[cut,len(d)]):
        u=np.unique(kk[a:b]);out[int(d[a])]=(int(m[a]),int(m[b-1]),int(len(u)),int(b-a-len(u)))
    return out

def describe_days(name,S,PD):
    say(f'\n-- {name}: {S.n:,} rows, {len(PD)} days {dfmt(min(PD)) if PD else "-"} .. {dfmt(max(PD)) if PD else "-"}')
    if not PD:return
    f=collections.Counter(v[0] for v in PD.values());l=collections.Counter(v[1] for v in PD.values());n=collections.Counter(v[2] for v in PD.values())
    say('   first minute of the day: '+', '.join(f'{hm(k)} x{c}' for k,c in f.most_common(6)))
    say('   last minute of the day:  '+', '.join(f'{hm(k)} x{c}' for k,c in l.most_common(6)))
    say('   bars per day:            '+', '.join(f'{k} x{c}' for k,c in n.most_common(8)))
    short=sorted(d for d,v in PD.items() if v[2]<0.9*n.most_common(1)[0][0])
    if short:say(f'   short days (< 90% of the usual bars): {len(short)}: '+', '.join(f'{dfmt(d)} ({PD[d][2]}, {hm(PD[d][0])}-{hm(PD[d][1])})' for d in short[:25])+(' ...' if len(short)>25 else ''))
    dup=sum(v[3] for v in PD.values())
    if dup:say(f'   DUPLICATE timestamps: {dup} rows on {sum(1 for v in PD.values() if v[3])} days, e.g. '+', '.join(dfmt(d) for d,v in PD.items() if v[3])[:200])
    out=int(((S.minute<OPEN)|(S.minute>CLOSE)).sum())
    if out:
        ex=sorted({(int(d),int(m)) for d,m in zip(S.day[(S.minute<OPEN)|(S.minute>CLOSE)],S.minute[(S.minute<OPEN)|(S.minute>CLOSE)])})[:8]
        say(f'   rows outside 09:15-15:30: {out} (e.g. '+', '.join(f'{dfmt(d)} {hm(m)}' for d,m in ex)+')')
    if S.unsorted:say(f'   rows out of time order: {S.unsorted}')

def fmt_report(name,S):
    issues=[]
    if S.nbad:issues.append(f'{S.nbad} unparseable rows, e.g. '+'; '.join(f'line {i}: {t!r}' for i,t in S.bad[:3]))
    if len(S.nfields)>1:issues.append(f'field counts vary: {dict(S.nfields)}')
    if S.header:issues.append(f'header line: {S.header!r}')
    if S.bom:issues.append('UTF-8 BOM')
    if S.blank:issues.append(f'{S.blank} blank lines')
    if S.seconds:issues.append(f'{S.seconds} timestamps with non-zero seconds')
    return issues

def price_report(name,S,jump):
    if not S.n:return
    bad=(S.l>np.minimum(S.o,S.c)+1e-9)|(S.h<np.maximum(S.o,S.c)-1e-9)|(S.l>S.h+1e-9)
    nonpos=(np.nanmin(np.stack([S.o,S.h,S.l,S.c]),0)<=0);nan=np.isnan(np.stack([S.o,S.h,S.l,S.c])).any(0)
    order=np.lexsort((S.minute,S.day));c=S.c[order];d=S.day[order];r=np.diff(c)/c[:-1];same=d[1:]==d[:-1]
    big=np.flatnonzero(same&(np.abs(r)>jump))
    say(f'   {name}: OHLC order violated {int(bad.sum())}, non-positive {int(nonpos.sum())}, missing values {int(nan.sum())}; '
        f'minute moves > {jump:.0%}: {len(big)}'+(' (e.g. '+', '.join(f'{dfmt(d[i+1])} {hm(S.minute[order][i+1])} {100*r[i]:+.1f}%' for i in big[np.argsort(-np.abs(r[big]))][:6])+')' if len(big) else ''))
    gaps=np.flatnonzero(~same);og=(c[gaps+1]-c[gaps])/c[gaps]
    if len(og):
        k=np.argsort(-np.abs(og))[:5];say(f'      largest overnight gaps: '+', '.join(f'{dfmt(d[gaps[i]+1])} {100*og[i]:+.1f}%' for i in k))

def vol_report(name,S,lot=75):
    if not S.n:return
    v=S.v[np.isfinite(S.v)];oi=S.oi[np.isfinite(S.oi)]
    pos=v[v>0]
    say(f'   {name}: volume zero in {100*(v==0).mean():.1f}% of rows; median {np.median(pos) if len(pos) else 0:g}, max {v.max() if len(v) else 0:g}; '
        +(f'multiples of {lot}: {100*(np.mod(pos,lot)==0).mean():.1f}%, of 25: {100*(np.mod(pos,25)==0).mean():.1f}%; ' if len(pos) else '')
        +(f'OI column: {"all zero" if (oi==0).all() else f"non-zero in {100*(oi!=0).mean():.1f}%"}' if len(oi) else 'no OI column'))

def last_thursday(y,m,tdays):
    d=dt.date(y,m,1)+dt.timedelta(days=31);d=d.replace(day=1)-dt.timedelta(days=1)
    while d.weekday()!=3:d-=dt.timedelta(days=1)
    o=d.toordinal()
    while tdays and o not in tdays and o>d.toordinal()-7:o-=1                                     # holiday: the previous trading day
    return o

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root',required=True,help='folder holding the spot/futures folder and the options folder');ap.add_argument('--out',required=True)
    ap.add_argument('--max-zips',type=int,default=0,help='survey only the first N option zips (quick look)')
    ap.add_argument('--lot',type=int,default=75,help='NIFTY lot in 2017-2020 (for the volume-unit check)')
    ap.add_argument('--pg',action='store_true',help='also report what PostgreSQL holds (PG* variables)')
    a=ap.parse_args(argv);t0=time.time();os.makedirs(a.out,exist_ok=True);R={}
    root=os.path.expanduser(a.root)
    # ---------------------------------------------------------------- 1 inventory
    hdr('1  INVENTORY')
    csvs=sorted(glob.glob(os.path.join(root,'**','*.csv'),recursive=True));zips=sorted(glob.glob(os.path.join(root,'**','*.zip'),recursive=True))
    other=sorted(set(glob.glob(os.path.join(root,'**','*'),recursive=True))-set(csvs)-set(zips));other=[p for p in other if os.path.isfile(p)]
    say(f'root {root}: {len(csvs)} csv files, {len(zips)} zip files, {len(other)} other files'+(': '+', '.join(os.path.relpath(p,root) for p in other[:10]) if other else ''))
    spot=collections.defaultdict(list);fut=collections.defaultdict(list);unknown=[]
    for p in csvs:
        b=os.path.basename(p).upper();yr=re.findall(r'(20\d\d)',os.path.relpath(p,root));yr=yr[-1] if yr else '?'
        if re.fullmatch(r'NIFTY\.CSV',b):spot[yr].append(p)
        elif re.fullmatch(r'NIFTY_F\d+\.CSV',b):fut[(yr,b[:-4])].append(p)
        else:unknown.append(p)
    say('spot files per year: '+', '.join(f'{y}: {len(v)}' for y,v in sorted(spot.items())))
    say('futures files per year: '+', '.join(f'{y} {s}: {len(v)}' for (y,s),v in sorted(fut.items())))
    if unknown:say(f'other csv files: '+', '.join(os.path.relpath(p,root) for p in unknown[:10]))
    zinfo={}
    for z in zips:
        nm=os.path.basename(z)[:-4];m=re.match(r'([A-Za-z]+)\s*[-_ ]?\s*(\d{4})',nm)
        if not m or m.group(1).lower() not in MONTHS:say(f'  zip with an unexpected name: {nm}');continue
        zinfo[(int(m.group(2)),MONTHS[m.group(1).lower()])]=z
    yrs=sorted({y for y,_ in zinfo});have=set(zinfo)
    say(f'option zips: {len(zinfo)} named by month, years {yrs}')
    missing=[(y,mo) for y in range(min(yrs or [2017]),max(yrs or [2020])+1) for mo in range(1,13) if (y,mo) not in have]
    say(f'  MISSING months: '+(', '.join(f'{dt.date(y,mo,1):%B %Y}' for y,mo in missing) if missing else 'none'))
    R['inventory']=dict(csv=len(csvs),zips=len(zinfo),missing_months=[f'{y}-{mo:02d}' for y,mo in missing])
    # ---------------------------------------------------------------- spot / futures
    def load_years(files):
        out={}
        for y,ps in sorted(files.items()):
            for p in ps:
                with open(p,'r',encoding='utf-8',errors='replace') as fh:out[(y,p)]=Series(os.path.relpath(p,root)).parse(fh.read())
        return out
    SP=load_years(spot);FU=load_years({k[0]+'|'+k[1]:v for k,v in fut.items()})
    def concat(D):
        if not D:return None
        S=Series('all');parts=list(D.values())
        for f in ('day','minute','o','h','l','c','v','oi'):setattr(S,f,np.concatenate([getattr(x,f) for x in parts]))
        S.n=len(S.day);return S
    spot_all=concat(SP);fut_by={}
    for (k,p),S in FU.items():fut_by.setdefault(k.split('|')[1],{})[(k,p)]=S
    fut_all={s:concat(D) for s,D in fut_by.items()}
    # ---------------------------------------------------------------- 2 format (spot / futures)
    hdr('2  FORMAT (spot, futures)')
    for D in (SP,FU):
        for (y,p),S in D.items():
            iss=fmt_report(S.name,S);syms=dict(S.symbols.most_common(3))
            say(f'  {S.name}: {S.n:,} rows, fields {dict(S.nfields)}, symbols {syms}'+('; '+'; '.join(iss) if iss else ''))
    # ---------------------------------------------------------------- 3 time conventions
    hdr('3  TIME CONVENTION (spot, futures)')
    PDs={}
    if spot_all is not None:PDs['spot']=per_day(spot_all);describe_days('spot NIFTY',spot_all,PDs['spot'])
    for s,S in fut_all.items():PDs[s]=per_day(S);describe_days(f'futures {s}',S,PDs[s])
    # ---------------------------------------------------------------- 7 options (parsed per zip; summarised below)
    hdr('7  OPTION CONTRACTS (per zip)')
    tdays=set(PDs.get('spot',{}))
    rows=[];optdays=collections.Counter();opt_first=collections.Counter();opt_last=collections.Counter();opt_nb=collections.Counter()
    fmt_issues=collections.Counter();fmt_examples=[];vol=[];parity=[];vol_v=[];zsum=[];unrec=[];lagpairs=collections.defaultdict(list);covr=[]
    F1=fut_all.get('NIFTY_F1');fk=None
    if F1 is not None:fk=dict(zip(F1.key().tolist(),F1.c.tolist()))
    SPk=dict(zip(spot_all.key().tolist(),spot_all.c.tolist())) if spot_all is not None else {}
    items=sorted(zinfo.items())[:a.max_zips] if a.max_zips else sorted(zinfo.items())
    for (y,mo),zp in items:
        try:Z=zipfile.ZipFile(zp)
        except zipfile.BadZipFile:say(f'  {dt.date(y,mo,1):%B %Y}: BAD ZIP');continue
        mem=[i for i in Z.infolist() if not i.is_dir()];dirs=collections.Counter(os.path.dirname(i.filename) for i in mem)
        txt=[i for i in mem if i.filename.lower().endswith('.txt')];oth=[i.filename for i in mem if not i.filename.lower().endswith('.txt')]
        C={};zlast=0;zfirst=10**9;names=collections.Counter()
        for i in txt:
            base=os.path.basename(i.filename)[:-4];m=re.fullmatch(r'\s*(PE|CE)\s*(\d+(?:\.\d+)?)\s*',base,re.I)
            S=Series(base).parse(Z.read(i).decode('utf-8','replace'))
            for k in fmt_report(base,S):fmt_issues[k.split(',')[0].split(':')[0]]+=1
            if S.nbad and len(fmt_examples)<5:fmt_examples.append(f'{dt.date(y,mo,1):%b %Y}/{i.filename}: '+'; '.join(f'line {a_}: {t!r}' for a_,t in S.bad[:2]))
            if not m:names['unexpected file name']+=1;continue
            cp,k=m.group(1).upper(),float(m.group(2));syms=list(S.symbols)
            if syms and syms[0].replace(' ','').upper()!=f'{cp}{k:g}'.upper():names['symbol inside differs from file name']+=1
            if (cp,k) in C:names['two files for one contract']+=1
            C[(cp,k)]=S
            if S.n:zlast=max(zlast,int(S.day.max()));zfirst=min(zfirst,int(S.day.min()))
            pd_=per_day(S)
            for d_,(f_,l_,nb,du) in pd_.items():optdays[d_]+=1;opt_first[f_]+=1;opt_last[l_]+=1
            vol_v.append(S.v[np.isfinite(S.v)&(S.v>0)][:2000])
        if not C:
            ext=collections.Counter(os.path.splitext(i.filename)[1].lower() or '(none)' for i in mem)
            say(f'  {dt.date(y,mo,1):%B %Y}: NO RECOGNISED OPTION FILES: {len(mem)} members, extensions {dict(ext)}, folders {dict(collections.Counter(os.path.dirname(i.filename) or "(top)" for i in mem).most_common(5))}')
            say('      member names: '+' | '.join(i.filename for i in mem[:12])+(' ...' if len(mem)>12 else ''))
            for i in [i for i in mem if i.file_size>0][:2]:
                head=Z.read(i)[:400].decode('utf-8','replace').splitlines()[:3];say(f'      {i.filename}: '+' / '.join(repr(h[:100]) for h in head))
            unrec.append(dict(month=f'{y}-{mo:02d}',members=len(mem),extensions=dict(ext),names=[i.filename for i in mem[:30]]))
            continue
        exp=zlast;lt=last_thursday(y,mo,tdays)
        strikes=sorted({k for _,k in C});steps=collections.Counter(np.diff(strikes).round(2).tolist()) if len(strikes)>1 else {}
        ends=collections.Counter();early=[];after=0;cover=[]
        for (cp,k),S in C.items():
            if not S.n:ends['empty']+=1;continue
            ld=int(S.day.max());fd=int(S.day.min());after+=int((S.day>exp).sum())
            if ld<exp-3:early.append(f'{cp} {k:g} ends {dfmt(ld)}')
            near=[d for d in sorted(set(S.day.tolist())) if exp-14<=d<=exp]
            rows.append(dict(zip_month=f'{y}-{mo:02d}',expiry=dfmt(exp),cp=cp,strike=k,first_day=dfmt(fd),last_day=dfmt(ld),rows=S.n,days=len(set(S.day.tolist())),
                             median_volume=float(np.median(S.v[S.v>0])) if (S.v>0).any() else 0.,zero_volume_share=round(float((S.v==0).mean()),4),
                             bars_last_10_days=int(((S.day>exp-14)&(S.day<=exp)).sum())))
        ce=sum(1 for c_,_ in C if c_=='CE');pe=len(C)-ce
        flag=[]
        if exp!=lt:flag.append(f'last data day {dfmt(exp)} is not the last-Thursday expiry {dfmt(lt)}')
        if dt.date.fromordinal(exp).month!=mo:flag.append('expiry month differs from the zip name')
        if early:flag.append(f'{len(early)} contracts end > 3 days before the others (e.g. {early[0]})')
        if len(dirs)>1 or list(dirs)!=['']:flag.append(f'folders inside: {dict(dirs)}')
        if oth:flag.append(f'non-txt members: {oth[:3]}')
        if names:flag.append(', '.join(f'{k} x{v}' for k,v in names.items()))
        # 9 parity: implied forward K + C - P near the money vs F1 at minute offsets
        res={}
        if fk:
            front_from=last_thursday(y,mo-1,tdays) if mo>1 else last_thursday(y-1,12,tdays)               # F1 should be this expiry after the previous one
            ks=[k for k in strikes if ('CE',k) in C and ('PE',k) in C]
            days_=sorted(d for d in set().union(*[set(C[('CE',k)].day.tolist()) for k in ks]) if front_from<d<=exp) if ks else []
            sample=days_[::max(1,len(days_)//6)][:8];cache={}
            for d_ in sample:
                for mi in (10*60+30,12*60+30,14*60+30):
                    f0=fk.get(d_*1440+mi)
                    if f0 is None:continue
                    k=min(ks,key=lambda x:abs(x-f0))
                    if k not in cache:cache[k]=tuple(dict(zip(C[(c_,k)].key().tolist(),C[(c_,k)].c.tolist())) for c_ in ('CE','PE'))
                    ck,pk=cache[k]
                    for off in (-2,-1,0,1,2):
                        q=d_*1440+mi;c_=ck.get(q);p_=pk.get(q);f_=fk.get(q+off)
                        if c_ is not None and p_ is not None and f_ is not None:res.setdefault(off,[]).append(k+c_-p_-f_)
            for off,v in res.items():parity.append((off,v))
        bo=min(res,key=lambda o:np.median(np.abs(res[o]))) if res else None
        if fk and ks:
            for d_ in sample:
                f0=fk.get(d_*1440+12*60)
                if f0 is None:continue
                k=min(ks,key=lambda x:abs(x-f0));ck=cache[k][0] if k in cache else dict(zip(C[('CE',k)].key().tolist(),C[('CE',k)].c.tolist()))
                for mi in range(9*60+20,15*60+25):
                    q=d_*1440+mi;c1,c0=ck.get(q),ck.get(q-1)
                    if c1 is None or c0 is None or c1==c0:continue
                    for off in (-2,-1,0,1,2):
                        f1_,f0_=fk.get(q+off),fk.get(q+off-1)
                        if f1_ is not None and f0_ is not None:lagpairs[off].append((c1-c0,f1_-f0_))
        if SPk:
            cs=set(strikes)
            dclose={}
            for k_,v in SPk.items():
                d_=k_//1440
                if zfirst<=d_<=exp and (d_ not in dclose or k_>dclose[d_][0]):dclose[d_]=(k_,v)
            for d_,(_,v) in dclose.items():
                atm=round(v/100)*100;covr.append(tuple(all(atm+j*100 in cs for j in range(-L,L+1)) for L in (2,3,5)))
        med0=np.median(np.abs(res[0])) if res.get(0) else float('nan');medb=np.median(np.abs(res[bo])) if bo is not None else float('nan')
        zsum.append(dict(month=f'{y}-{mo:02d}',expiry=dfmt(exp),last_thursday=dfmt(lt),contracts=len(C),ce=ce,pe=pe,strikes=len(strikes),
                         strike_min=strikes[0],strike_max=strikes[-1],steps=dict(steps),first_day=dfmt(zfirst),parity_abs_median_pts=None if math.isnan(med0) else round(float(med0),2),parity_best_offset=bo,flags=flag))
        say(f'  {dt.date(y,mo,1):%b %Y}: expiry {dfmt(exp)} ({dt.date.fromordinal(exp):%a}), {ce} CE + {pe} PE, strikes {strikes[0]:g}-{strikes[-1]:g} steps {dict(steps)}, '
            f'data from {dfmt(zfirst)}; parity |K+C-P-F1| median {med0:.1f} pts at offset 0'+(f', {medb:.1f} at best offset {bo:+d}' if bo not in (None,0) else '')+('\n      ! '+'\n      ! '.join(flag) if flag else ''))
    # ---------------------------------------------------------------- summaries that need all data
    hdr('2b FORMAT (options)')
    say('  issues by kind (files): '+(', '.join(f'{k} x{v}' for k,v in fmt_issues.items()) if fmt_issues else 'none'))
    for e in fmt_examples:say('   '+e)
    hdr('3b TIME CONVENTION (options, all contracts pooled)')
    say('   first minute of a contract-day: '+', '.join(f'{hm(k)} x{c}' for k,c in opt_first.most_common(6)))
    say('   last minute of a contract-day:  '+', '.join(f'{hm(k)} x{c}' for k,c in opt_last.most_common(6)))
    hdr('4  CALENDAR')
    sd=set(PDs.get('spot',{}));fd=set(PDs.get('NIFTY_F1',{}));od=set(optdays)
    alld=sd|fd|od
    if alld:
        lo,hi=min(alld),max(alld)
        for y in sorted({dt.date.fromordinal(d).year for d in alld}):
            ys=[d for d in sd if dt.date.fromordinal(d).year==y];yf=[d for d in fd if dt.date.fromordinal(d).year==y];yo=[d for d in od if dt.date.fromordinal(d).year==y]
            wk=[o for o in range(dt.date(y,1,1).toordinal(),dt.date(y,12,31).toordinal()+1) if dt.date.fromordinal(o).weekday()<5 and lo<=o<=hi]
            hol=[o for o in wk if o not in sd]
            say(f'  {y}: spot {len(ys)} days, futures {len(yf)}, options {len(yo)}; weekdays without spot data ({len(hol)}, holiday candidates): '
                +', '.join(f'{dfmt(o)} {dt.date.fromordinal(o):%a}' for o in hol))
        wkend=sorted(d for d in alld if dt.date.fromordinal(d).weekday()>=5)
        say('  weekend sessions: '+(', '.join(f'{dfmt(d)} {dt.date.fromordinal(d):%a} (spot {"yes" if d in sd else "no"}, F1 {"yes" if d in fd else "no"}, options {optdays.get(d,0)} contracts)' for d in wkend) if wkend else 'none'))
        for a_,b_,nm in ((sd,fd,'spot but not futures'),(fd,sd,'futures but not spot'),(sd,od,'spot but no option data'),(od,sd,'option data but no spot')):
            x=sorted(a_-b_);say(f'  days with {nm}: {len(x)}'+(': '+', '.join(dfmt(d) for d in x[:20])+(' ...' if len(x)>20 else '') if x else ''))
    hdr('5  PRICES')
    if spot_all is not None:price_report('spot',spot_all,.02)
    for s,S in fut_all.items():price_report(f'futures {s}',S,.02)
    if spot_all is not None and F1 is not None:
        common=sorted(set(SPk)&set(fk))[::7];b=np.array([fk[k]-SPk[k] for k in common])
        say(f'   F1 - spot basis (points, every 7th common minute): median {np.median(b):.1f}, 1%..99% {np.percentile(b,1):.1f} .. {np.percentile(b,99):.1f}, min {b.min():.1f}, max {b.max():.1f}')
    if covr:
        cv=np.array(covr);say(f'   strike coverage (contract-days, ATM = spot close to the nearest 100): ATM +-200 listed {100*cv[:,0].mean():.0f}%, +-300 {100*cv[:,1].mean():.0f}%, +-500 {100*cv[:,2].mean():.0f}% '
                                 '(the strategies use levels 1-5 of 100 points below NIFTY 12,000)')
    hdr('6  VOLUME / OI COLUMNS')
    if spot_all is not None:vol_report('spot',spot_all,a.lot)
    for s,S in fut_all.items():vol_report(f'futures {s}',S,a.lot)
    if vol_v:
        v=np.concatenate(vol_v);say(f'   options (sample of {len(v):,} non-zero bars): median {np.median(v):g}; multiples of {a.lot}: {100*(np.mod(v,a.lot)==0).mean():.1f}%, '
                                    f'of 25: {100*(np.mod(v,25)==0).mean():.1f}% -> volume in {"units (contracts x lot)" if (np.mod(v,a.lot)==0).mean()>.95 else "lots or mixed: check"}')
    hdr('8  FUTURES ROLL (F1 around each monthly expiry)')
    if F1 is not None and spot_all is not None:
        exps=sorted({dt.date.fromisoformat(z['expiry']).toordinal() for z in zsum}) or sorted({last_thursday(y,m,tdays) for y in range(2017,2021) for m in range(1,13)})
        dayclose=lambda D,d:max(((k,v) for k,v in D.items() if k//1440==d),default=(None,None))[1]
        lines=[]
        for e in exps:
            prevd=max((d for d in sd if d<e),default=None);nxt=min((d for d in sd if d>e),default=None)
            def basis(d):
                if d is None:return None
                f=dayclose(fk,d);s=dayclose(SPk,d);return None if f is None or s is None else f-s
            bp,be,bn=basis(prevd),basis(e),basis(nxt)
            fmtb=lambda x:'   -  ' if x is None else f'{x:6.1f}'
            verdict=('F1 = expiring contract through the expiry close (settles to spot), next contract from the next day' if be is not None and abs(be)<3
                     else 'F1 had already rolled before the expiry close' if be is not None and abs(be)>=3 else 'no data')
            lines.append(f'  expiry {dfmt(e)}: basis at close day before {fmtb(bp)}, expiry day {fmtb(be)}, next day {fmtb(bn)}  -> {verdict}')
        for l_ in lines:say(l_)
        R['roll']=lines
    hdr('9  ALIGNMENT')
    if parity:
        agg=collections.defaultdict(list)
        for off,v in parity:agg[off]+=v
        say('  put-call parity K + C - P - F1 (same expiry, near the money), by minute offset of F1 vs the option timestamp:')
        for off in sorted(agg):
            v=np.array(agg[off]);say(f'     F1 at option minute {off:+d}: median {np.median(v):+.2f} pts, median |.| {np.median(np.abs(v)):.2f}, n {len(v)}')
        best=min(agg,key=lambda o:np.median(np.abs(agg[o])));say(f'  -> best fit at offset {best:+d}'+(' (option and futures labels mean the same minute)' if best==0 else ' (labels are shifted: see decisions)'))
        R['parity_best_offset']=int(best)
    if lagpairs:
        say('  ATM call minute change vs F1 minute change (same expiry), correlation by F1 offset:')
        cc={}
        for off in sorted(lagpairs):
            a_=np.array(lagpairs[off]);cc[off]=float(np.corrcoef(a_[:,0],a_[:,1])[0,1]);say(f'     F1 at option minute {off:+d}: correlation {cc[off]:.3f} (n {len(a_):,})')
        bo=max(cc,key=cc.get);say(f'  -> highest at offset {bo:+d}'+(' : option label t = futures label t+1 (options label the bar START, spot/futures the bar END)' if bo==1 else ''));R['option_futures_lag']=bo
    if spot_all is not None and F1 is not None:
        common=sorted(set(SPk)&set(fk));ks=np.array(common);sv=np.array([SPk[k] for k in common]);fv=np.array([fk[k] for k in common])
        same=np.diff(ks)==1;rs=np.diff(sv)/sv[:-1];rf=np.diff(fv)/fv[:-1]
        out=[]
        for lag in (-2,-1,0,1,2):
            if lag>=0:x,yv=rs[lag:],rf[:len(rf)-lag];m_=same[lag:]&same[:len(same)-lag]
            else:x,yv=rs[:lag],rf[-lag:];m_=same[:lag]&same[-lag:]
            out.append((lag,float(np.corrcoef(x[m_],yv[m_])[0,1])))
        say('  spot vs F1 minute-return correlation by lag (spot minute t vs F1 minute t+lag): '+', '.join(f'{l:+d}: {c:.3f}' for l,c in out))
        say(f'  -> highest at lag {max(out,key=lambda x:x[1])[0]:+d}')
        R['spot_futures_lag']=max(out,key=lambda x:x[1])[0]
    if a.pg:
        hdr('10 DATABASE')
        try:
            from nifty_iv.data.research_cache import connect
            with connect() as pg:
                for t in ('option_bars','spot_bars','future_bars'):
                    r=pg.execute(f'select min(trading_day),max(trading_day),count(*) from market_data.{t}').fetchone()
                    say(f'  market_data.{t}: {r[0]} .. {r[1]}, {r[2]:,} rows')
                for r in pg.execute("select dataset,market_kind,count(*),min(relative_path) from market_data.import_files group by 1,2 order by 1,2").fetchall():say(f'  import_files: {r}')
        except Exception as e:say(f'  could not query PostgreSQL: {e}')
    # ---------------------------------------------------------------- 11 decisions
    hdr('11 FOR THE IMPORTER: what the findings say')
    say(f'  - option expiry: not in the files; per zip = the last day with data (checked against the last-Thursday rule above)')
    say(f'  - bar labels: spot/futures first minute {hm(collections.Counter(v[0] for v in PDs.get("spot",{}).values()).most_common(1)[0][0]) if PDs.get("spot") else "-"}, '
        f'options first minute {hm(opt_first.most_common(1)[0][0]) if opt_first else "-"}; the database stores bar START times 09:15..15:29 -> '
        'shift any file whose bars run 09:16..15:30 by one minute (confirm with section 9)')
    say('  - holidays 2017-2020: the weekday list in section 4 (verify against NSE circulars) -> config/nse_holidays.txt')
    say('  - futures: NIFTY_F1 is a continuous front month without expiry; its contract per day follows section 8')
    say('  - weekly options (from Feb 2019): present only if section 7 shows contracts ending before the monthly expiry')
    with open(os.path.join(a.out,'contracts.csv'),'w') as fh:
        if rows:
            import csv as _c;w=_c.DictWriter(fh,list(rows[0]));w.writeheader();w.writerows(rows)
    R['zips']=zsum;R['unrecognised_zips']=unrec
    with open(os.path.join(a.out,'survey.json'),'w') as fh:json.dump(R,fh,indent=1,default=str)
    say(f'\n-> {a.out}/survey.json, contracts.csv ({time.time()-t0:.0f}s)')

if __name__=='__main__':main()
