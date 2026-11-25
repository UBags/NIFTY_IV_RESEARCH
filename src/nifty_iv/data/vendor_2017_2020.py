"""Readers for the 2017-2020 NIFTY vendor files (spot, NIFTY_F1 futures, monthly option zips), as established by
scripts/survey_2017_2020.py, check_2017_2020.py and attribute_2017_2020.py (V4.9.18).

Layout and conventions (all verified on the real files, Nov 2026):
  spot / futures   <root>/.../<year>/NIFTY.csv, NIFTY_F1.csv: SYMBOL,YYYY/MM/DD,HH:MM,O,H,L,C,V,OI; bars labelled by their END
                   (09:16..15:30, plus after-hours rows); spot V is a tick count, OI always 0.
  options          <root>/.../<Month YYYY>.zip, one monthly expiry per zip; zips may hold zips (any depth, through folders):
                   the copy named CSV is used, TXT copies are ignored (TXT only when a month has no CSV, March 2018).
                   File names: 'CE 8250', 'NIFTY8250CE', 'NIFTY25Jun208250CE'. Rows SYMBOL,YYYY/MM/DD,HH:MM,O,H,L,C,V[,OI],
                   bars labelled by their START (09:15..15:29, plus after-hours rows); V in units (multiples of 75).
  expiry           not in the files: the last Thursday of the zip's month, moved to the previous trading day on a
                   holiday (equal to the last day with data for all 46 zips)."""
from __future__ import annotations
import collections,datetime as dt,io,os,re,zipfile

MONTHS={m:i for i,m in enumerate(['january','february','march','april','may','june','july','august','september','october','november','december'],1)}
MON3={m[:3]:i for m,i in MONTHS.items()}
OPEN,LAST=9*60+15,15*60+29                       # bar-start minutes of the regular session
NAME_OLD=re.compile(r'\s*(PE|CE)\s*(\d+(?:\.\d+)?)\s*',re.I)
NAME_NIFTY=re.compile(r'\s*NIFTY\s*(?:(\d{1,2})([A-Za-z]{3})(\d{2}))?\s*(\d+(?:\.\d+)?)\s*(CE|PE)\s*',re.I)
ZIP_NAME=re.compile(r'([A-Za-z]+)\s*[-_ ]?\s*(\d{4})')

def parse_name(base):
    """(cp, strike, expiry date or None, style) from a file / symbol name, or None."""
    m=NAME_OLD.fullmatch(base)
    if m:return m.group(1).upper(),float(m.group(2)),None,'CE 8250'
    m=NAME_NIFTY.fullmatch(base)
    if not m:return None
    dd,mon,yy,k,cp=m.groups();exp=None
    if dd:
        if mon.lower() not in MON3:return None
        try:exp=dt.date(2000+int(yy),MON3[mon.lower()],int(dd))
        except ValueError:return None
    return cp.upper(),float(k),exp,('NIFTY25Jun208250CE' if dd else 'NIFTY8250CE')

def collect_members(Z,trail=(),notes=None,depth=0):
    """[(ZipFile, ZipInfo, trail)] of the data members, descending zips of zips; notes say what was chosen."""
    notes=[] if notes is None else notes
    mem=[i for i in Z.infolist() if not i.is_dir()]
    csv=[i for i in mem if i.filename.lower().endswith('.csv')];txt=[i for i in mem if i.filename.lower().endswith('.txt')]
    inner=[i for i in mem if i.filename.lower().endswith('.zip')]
    if csv:return [(Z,i,trail) for i in csv],notes
    if inner and depth<8:
        bn=lambda i:os.path.basename(i.filename).lower()
        pick=[i for i in inner if 'csv' in bn(i)] or [i for i in inner if 'txt' not in bn(i)] or inner
        out=[]
        for i in pick:
            notes.append(i.filename);o,_=collect_members(zipfile.ZipFile(io.BytesIO(Z.read(i))),trail+(i.filename,),notes,depth+1);out+=o
        return out,notes
    if txt:notes.append('txt only');return [(Z,i,trail) for i in txt],notes
    return [],notes

def find_files(root):
    """{'spot': [paths], 'future': [paths], 'option': {(year, month): zip path}} under root."""
    out={'spot':[],'future':[],'option':{}}
    for d,_,fs in os.walk(root):
        for f in sorted(fs):
            p=os.path.join(d,f);u=f.upper()
            if u=='NIFTY.CSV':out['spot'].append(p)
            elif u=='NIFTY_F1.CSV':out['future'].append(p)
            elif u.endswith('.ZIP'):
                m=ZIP_NAME.match(f[:-4])
                if m and m.group(1).lower() in MONTHS:out['option'][(int(m.group(2)),MONTHS[m.group(1).lower()])]=p
    out['spot'].sort();out['future'].sort();return out

def last_thursday(y,m,trading_days):
    """Monthly expiry of (y, m): its last Thursday, or the trading day before it when that Thursday is a holiday."""
    d=(dt.date(y,m,28)+dt.timedelta(days=4)).replace(day=1)-dt.timedelta(days=1)
    while d.weekday()!=3:d-=dt.timedelta(days=1)
    e=d
    while trading_days and e not in trading_days and e>d-dt.timedelta(days=7):e-=dt.timedelta(days=1)
    return e

def current_monthly(day,trading_days):
    """The monthly expiry trading as the current month on `day` (the futures contract NIFTY_F1 holds that day)."""
    e=last_thursday(day.year,day.month,trading_days)
    if e<day:
        n=(day.replace(day=28)+dt.timedelta(days=4));e=last_thursday(n.year,n.month,trading_days)
    return e

def read_rows(data):
    """pandas DataFrame of a vendor file: sym, date (str), time (str), o, h, l, c, v, oi (NaN when absent), row (0-based line).
    Raises ValueError on any malformed row: nothing is guessed."""
    import numpy as np,pandas as pd
    text=data.decode('utf-8-sig','strict') if isinstance(data,bytes) else data
    lines=text.splitlines();rows=[(n,l) for n,l in enumerate(lines) if l.strip()]
    if not rows:return pd.DataFrame(columns=['sym','date','time','o','h','l','c','v','oi','row'])
    if not re.match(r'^[^,]*,\d{4}[/-]\d{2}[/-]\d{2},',rows[0][1]):rows=rows[1:]            # a header line
    parts=[l.split(',') for _,l in rows];cnt=collections.Counter(len(p) for p in parts);n=cnt.most_common(1)[0][0]
    if n not in (8,9) or len(cnt)>1:
        j=next(q for q,p in enumerate(parts) if len(p)!=n) if len(cnt)>1 else 0
        raise ValueError(f'{len(parts[j])} fields at line {rows[j][0]+1} (file mostly {n}): {rows[j][1][:80]!r}')
    df=pd.DataFrame(parts,columns=['sym','date','time','o','h','l','c','v','oi'][:n])
    if n==8:df['oi']=np.nan
    df['row']=[r for r,_ in rows]
    bad=~df['date'].str.fullmatch(r'\d{4}[/-]\d{2}[/-]\d{2}')|~df['time'].str.fullmatch(r'\d{1,2}:\d{2}(:00)?')
    if bad.any():raise ValueError(f'bad date/time at line {int(df.row[bad].iloc[0])+1}: {lines[int(df.row[bad].iloc[0])][:80]!r}')
    for c in ('o','h','l','c','v','oi'):df[c]=pd.to_numeric(df[c],errors='raise')
    df['date']=df['date'].str.replace('-','/');df['time']=df['time'].str.slice(0,5).str.zfill(5)
    return df
