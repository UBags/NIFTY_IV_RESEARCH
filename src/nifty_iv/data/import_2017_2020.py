#!/usr/bin/env python3
"""Load the 2017-2020 NIFTY vendor files (spot, NIFTY_F1, monthly option zips) into market_data, as dataset nifty_2017_2020.

USAGE
  nifty-data-import-2017-2020 --root data_2017_2020 --attribution runs/attribute_2017_2020/attribution_files.csv --dry-run
  nifty-data-import-2017-2020 --root data_2017_2020 --attribution runs/attribute_2017_2020/attribution_files.csv
  nifty-data-import-2017-2020 --verify

What goes in (findings of scripts/survey_2017_2020.py, check_2017_2020.py, attribute_2017_2020.py):
  * bar_time is the bar START in Asia/Kolkata, as the rest of market_data: spot and NIFTY_F1 label bars by their END, so
    their times move back one minute; option times are kept.
  * only the regular session, bar starts 09:15-15:29 (drops the 15:30+ closing-price prints, 16:3x and evening vendor
    rows, and the Muhurat evening sessions).
  * options: expiry = the zip's monthly expiry (last Thursday, previous trading day on a holiday), checked against the
    zip's last day with data and any expiry written in the file names; strike and type from the file name, checked
    against the symbol inside; OI where the file has it (Sep-Dec 2020), else NULL. Rows the attribution check shows to
    be copies of the previous zip's contract (identical closes on >= --copy-share of the common minutes that day) are
    left out: the previous zip holds them. --overrides can drop or re-date file-days by hand.
  * spot: volume NULL (the vendor column is a tick count, not traded volume).
  * futures: expiry per bar = the current monthly expiry (NIFTY_F1 holds the expiring contract through expiry day:
    check_2017_2020 section D), instrument FUTIDX, lot 75; volume in units; OI NULL (all zero in the files).
  * nothing is filled in: minutes without a trade stay absent, as in the thetrademarkk option data.

One import_files row per source file (each option file inside a zip is its own unit); each unit loads in ONE transaction
with its import_files row, so an interrupted run leaves no partial unit and a rerun continues. A completed unit whose
file or selected rows changed is refused unless --reload (its rows are then replaced in the same transaction).
Credentials: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD."""
from __future__ import annotations
import collections,csv,datetime as dt,hashlib,io,json,os,sys,time,zipfile
from nifty_iv.core import cli
from nifty_iv.data import vendor_2017_2020 as V

DATASET='nifty_2017_2020'
LOCK_KEY='nifty_import_2017_2020'

def say(*a):print(*a,flush=True)

class Unit:
    """One source file: kind, relative_path (unique in import_files), bytes, mtime and a loader returning (frame, stats)."""
    def __init__(self,kind,rel,nbytes,mtime_ns,load):self.kind,self.rel,self.nbytes,self.mtime_ns,self.load=kind,rel,nbytes,mtime_ns,load

def import_postgres_columns(kind):
    """The market_data column order the frames must follow (import_postgres.COLUMNS)."""
    from nifty_iv.data.import_postgres import COLUMNS;return COLUMNS[kind]

# ---------------------------------------------------------------- policies
def read_attribution(path,share,min_common):
    """{(zip 'YYYY-MM', cp, strike, day 'YYYY-MM-DD')} of option file-days that copy the previous zip."""
    out=set()
    with open(path,newline='') as fh:
        for r in csv.DictReader(fh):
            if r['identical_prev'] not in ('',None) and int(r['common_prev'])>=min_common and float(r['identical_prev'])>=share:
                out.add((r['zip'],r['cp'],float(r['strike']),r['day']))
    return out

def read_overrides(path):
    """Rows of zip,cp,strike,day_from,day_to,action,expiry,reason ('*' = any cp / strike); action drop | expiry."""
    rows=[]
    if not path:return rows
    with open(path,newline='') as fh:
        for r in csv.DictReader(l for l in fh if not l.lstrip().startswith('#')):
            if r['action'] not in ('drop','expiry'):raise SystemExit(f'override action must be drop or expiry: {r}')
            if r['action']=='expiry':dt.date.fromisoformat(r['expiry'])
            rows.append(r)
    return rows

def override_for(ovr,zipm,cp,strike,day):
    for r in ovr:
        if r['zip']==zipm and r['cp'] in ('*',cp) and (r['strike']=='*' or float(r['strike'])==strike) and r['day_from']<=day<=r['day_to']:return r
    return None

# ---------------------------------------------------------------- frames
def bar_frame(df,shift):
    """Adds minute (bar start), day (date), bar_time and trading_day; keeps the regular session only. Returns (frame, dropped)."""
    import pandas as pd
    m=df['time'].str.slice(0,2).astype(int)*60+df['time'].str.slice(3,5).astype(int)-shift
    keep=(m>=V.OPEN)&(m<=V.LAST);out=df[keep].copy();m=m[keep]
    out['trading_day']=out['date'].str.replace('/','-')
    out['bar_time']=out['trading_day']+' '+(m//60).map('{:02d}'.format)+':'+(m%60).map('{:02d}'.format)+':00+05:30'
    dup=out.duplicated('bar_time',keep=False)
    if dup.any():                                                   # exact repeats are dropped (counted); conflicting ones stop the unit
        exact=out.duplicated(['bar_time','o','h','l','c','v'],keep='first');conflict=dup&~out.duplicated(['bar_time','o','h','l','c','v'],keep=False)
        if conflict.any():raise ValueError(f'{int(conflict.sum())} rows share a minute with different prices, e.g. line {int(out.row[conflict].iloc[0])+1} at {out.bar_time[conflict].iloc[0]}')
        out=out[~exact];out.attrs['exact_duplicates']=int(exact.sum())
    return out,int((~keep).sum())

def spot_frame(df,file_id):
    import pandas as pd
    return pd.DataFrame({'source_file_id':file_id,'source_row_index':df['row'].values,'bar_time':df['bar_time'].values,'trading_day':df['trading_day'].values,
        'provider_trading_day':df['date'].values,'exchange':'NSE','symbol':'NIFTY','open':df['o'].values,'high':df['h'].values,'low':df['l'].values,'close':df['c'].values,
        'volume':pd.array([None]*len(df),dtype='Int64')})

def future_frame(df,file_id,tdays):
    import pandas as pd
    days=sorted(set(df['trading_day']));exp={d:V.current_monthly(dt.date.fromisoformat(d),tdays).isoformat() for d in days}
    return pd.DataFrame({'source_file_id':file_id,'source_row_index':df['row'].values,'bar_time':df['bar_time'].values,'trading_day':df['trading_day'].values,
        'provider_trading_day':df['date'].values,'exchange':'NSE','underlying':'NIFTY','expiry':df['trading_day'].map(exp).values,'trading_symbol':'NIFTY_F1',
        'groww_symbol':None,'instrument_type':'FUTIDX','strike':None,'open':df['o'].values,'high':df['h'].values,'low':df['l'].values,'close':df['c'].values,
        'volume':df['v'].values,'open_interest':None,'lot_size':'75','tick_size':'0.05'})

def option_frame(df,file_id,expiry,strike,cp,label):
    import numpy as np,pandas as pd
    oi=df['oi'] if 'oi' in df else pd.Series(np.nan,index=df.index)
    return pd.DataFrame({'source_file_id':file_id,'source_row_index':df['row'].values,'bar_time':df['bar_time'].values,'trading_day':df['trading_day'].values,
        'provider_trading_day':df['date'].values,'exchange':'NSE','underlying':'NIFTY','expiry':expiry.values if hasattr(expiry,'values') else expiry,'strike':strike,'option_type':cp,
        'open':df['o'].values,'high':df['h'].values,'low':df['l'].values,'close':df['c'].values,'volume':pd.array(df['v'].round().astype('int64'),dtype='Int64'),
        'open_interest':pd.array(oi.round(),dtype='Int64'),'exercise_style':'European','granularity':'1min','vendor_label':label,'settle_price':None})

# ---------------------------------------------------------------- plan
def plan(root,copies,ovr,report):
    """Spot and futures units, the option zips (read one at a time later: all of them at once would take GBs) and the
    trading days (from spot)."""
    files=V.find_files(root)
    if not files['spot'] or not files['future'] or not files['option']:raise SystemExit(f'expected NIFTY.csv, NIFTY_F1.csv and month zips under {root}: {({k:len(v) for k,v in files.items()})}')
    spot={}
    for p in files['spot']:
        try:
            with open(p,'rb') as fh:spot[p]=V.read_rows(fh.read())
        except ValueError as e:raise SystemExit(f'{p}: {e} (nothing loaded: spot sets the trading calendar)')
    tdays=set()
    for df in spot.values():
        f,_=bar_frame(df,1);tdays|={dt.date.fromisoformat(d) for d in f['trading_day'].unique()}
    units=[]
    rel=lambda p:f'{DATASET}/'+os.path.relpath(p,root).replace(os.sep,'/')
    for p in files['spot']:
        def load(file_id,p=p):
            f,off=bar_frame(spot[p],1);return spot_frame(f,file_id),dict(read=len(spot[p]),off_session=off,duplicates=f.attrs.get('exact_duplicates',0))
        st=os.stat(p);units.append(Unit('spot',rel(p),st.st_size,st.st_mtime_ns,load))
    for p in files['future']:
        def load(file_id,p=p):
            with open(p,'rb') as fh:df=V.read_rows(fh.read())
            if set(df['sym'].unique())!={'NIFTY_F1'}:raise ValueError(f'symbols {set(df.sym.unique())}')
            f,off=bar_frame(df,1);return future_frame(f,file_id,tdays),dict(read=len(df),off_session=off,duplicates=f.attrs.get('exact_duplicates',0))
        st=os.stat(p);units.append(Unit('future',rel(p),st.st_size,st.st_mtime_ns,load))
    zips=[(y,mo,zp,rel(zp)) for (y,mo),zp in sorted(files['option'].items())]
    return units,zips,tdays

def zip_units(y,mo,zp,zrel,tdays,copies,ovr,report):
    """Units of one option zip. The zip is read and checked as a whole first (expiry vs last data day, names vs symbols)."""
    zipm=f'{y}-{mo:02d}';E=V.last_thursday(y,mo,tdays);st=os.stat(zp)
    found,notes=V.collect_members(zipfile.ZipFile(zp));parsed=[]
    for Zm,i,trail in found:
        base=os.path.splitext(os.path.basename(i.filename))[0];pn=V.parse_name(base)
        if pn is None:raise SystemExit(f'{zipm}: file name not understood: {i.filename} (no rows of this zip loaded; add the style to vendor_2017_2020.parse_name)')
        cp,k,nexp,_=pn
        if nexp and nexp!=E:raise SystemExit(f'{zipm}: {i.filename} names expiry {nexp}, the zip rule gives {E}')
        try:df=V.read_rows(Zm.read(i))
        except ValueError as e:raise SystemExit(f'{zipm}: {i.filename}: {e} (no rows of this zip loaded)')
        syms={V.parse_name(s.strip()) for s in df['sym'].unique()}
        if any(s is None or s[:2]!=(cp,k) for s in syms):raise SystemExit(f'{zipm}: {i.filename}: symbols inside {sorted(df.sym.unique())} do not match the name')
        parsed.append((i,trail,cp,k,df))
    last=max(df['date'].max() for *_,df in parsed).replace('/','-') if parsed else None
    if last!=E.isoformat() and not any(r['zip']==zipm and r['action']=='expiry' for r in ovr):
        raise SystemExit(f'{zipm}: last day with data {last} is not the rule expiry {E}: add an override after checking')
    report['zips'][zipm]=dict(expiry=E.isoformat(),files=len(parsed),path=' > '.join(notes) or 'flat')
    units=[]
    for i,trail,cp,k,df in parsed:
        r='!'.join((zrel,)+tuple(trail)+(i.filename,))
        def load(file_id,cp=cp,k=k,df=df,label=os.path.basename(i.filename)):
            import pandas as pd
            f,off=bar_frame(df,0);days=f['trading_day']
            cop=days.map(lambda d:(zipm,cp,k,d) in copies).astype(bool)
            exp=pd.Series(E.isoformat(),index=f.index);drop=cop.copy();re_=0
            if ovr:
                for d in days[~cop].unique():
                    o=override_for(ovr,zipm,cp,k,d)
                    if o is None:continue
                    sel=(days==d)&~cop
                    if o['action']=='drop':drop|=sel
                    else:exp[sel]=o['expiry'];re_+=int(sel.sum())
            dup_=f.attrs.get('exact_duplicates',0);f=f[~drop];exp=exp[~drop]
            return option_frame(f,file_id,exp,k,cp,label),dict(read=len(df),off_session=off,duplicates=dup_,copy_dropped=int(cop.sum()),override_dropped=int((drop&~cop).sum()),
                                                                 redated=re_,ohlc_bad=int(((f.l>f[['o','c']].min(axis=1)+1e-9)|(f.h<f[['o','c']].max(axis=1)-1e-9)).sum()),
                                                                 zero_volume=int((f.v==0).sum()))
        units.append(Unit('option',r,i.file_size,st.st_mtime_ns,load))
    return units

# ---------------------------------------------------------------- database
def ensure_schema(conn):
    from nifty_iv.data import import_postgres as ip
    with conn.cursor() as cur:cur.execute(ip.DDL)
    conn.commit()

def load_unit(conn,u,reload_):
    """Loads one unit in one transaction. Returns ('loaded' | 'skipped', rows, stats)."""
    from nifty_iv.data import import_postgres as ip
    with conn.transaction():
        cur=conn.cursor()
        row=cur.execute('SELECT file_id,file_bytes,mtime_ns,expected_rows,status,dataset FROM market_data.import_files WHERE relative_path=%s FOR UPDATE',(u.rel,)).fetchone()
        if row and row[5]!=DATASET:raise RuntimeError(f'{u.rel} is registered by dataset {row[5]}')
        if row is None:
            fid=cur.execute('''INSERT INTO market_data.import_files(relative_path,file_bytes,mtime_ns,expected_rows,row_groups,dataset,market_kind,status)
                               VALUES (%s,%s,%s,0,1,%s,%s,'loading') RETURNING file_id''',(u.rel,u.nbytes,u.mtime_ns,DATASET,u.kind)).fetchone()[0]
        else:fid=row[0]
        frame,stats=u.load(fid)
        if row and row[4]=='completed':
            same=row[1]==u.nbytes and row[2]==u.mtime_ns and row[3]==len(frame)
            if same:return 'skipped',len(frame),stats
            if not reload_:raise RuntimeError(f'{u.rel} changed since it was loaded (bytes {row[1]}->{u.nbytes}, rows {row[3]}->{len(frame)}): rerun with --reload to replace it')
        if row:cur.execute(f'DELETE FROM market_data.{ip.TABLES[u.kind]} WHERE source_file_id=%s',(fid,))
        if len(frame):
            ip.ensure_partitions(cur,u.kind,{int(d[:4]) for d in frame['trading_day'].unique()})
            ip.copy_frame(conn,frame,u.kind)
        cur.execute('''UPDATE market_data.import_files SET file_bytes=%s,mtime_ns=%s,expected_rows=%s,loaded_rows=%s,status='completed',error_message=NULL,
                       updated_at=now(),completed_at=now() WHERE file_id=%s''',(u.nbytes,u.mtime_ns,len(frame),len(frame),fid))
    return 'loaded',len(frame),stats

def mark_failed(conn,u,msg):
    try:
        with conn.transaction():
            conn.execute('''INSERT INTO market_data.import_files(relative_path,file_bytes,mtime_ns,expected_rows,row_groups,dataset,market_kind,status,error_message)
                            VALUES (%s,%s,%s,0,1,%s,%s,'failed',%s) ON CONFLICT (relative_path) DO UPDATE SET status=CASE WHEN market_data.import_files.status='completed'
                            THEN 'completed' ELSE 'failed' END,error_message=EXCLUDED.error_message,updated_at=now()''',(u.rel,u.nbytes,u.mtime_ns,DATASET,u.kind,msg[:2000]))
    except Exception as e:say(f'    (could not record the failure: {e})')

def verify(conn):
    """Rows in each table against import_files for the dataset; per-year counts; units not completed."""
    from nifty_iv.data import import_postgres as ip
    ok=True
    for kind in ('spot','future','option'):
        st,n,rows=conn.execute("SELECT count(*) FILTER (WHERE status='completed'),count(*),coalesce(sum(loaded_rows),0) FROM market_data.import_files WHERE dataset=%s AND market_kind=%s",(DATASET,kind)).fetchone()
        have=conn.execute(f'SELECT count(*) FROM market_data.{ip.TABLES[kind]} b JOIN market_data.import_files f ON f.file_id=b.source_file_id WHERE f.dataset=%s',(DATASET,)).fetchone()[0]
        years=conn.execute(f'''SELECT extract(year FROM trading_day)::int,count(*),min(trading_day),max(trading_day) FROM market_data.{ip.TABLES[kind]} b
                               JOIN market_data.import_files f ON f.file_id=b.source_file_id WHERE f.dataset=%s GROUP BY 1 ORDER BY 1''',(DATASET,)).fetchall()
        good=have==rows and st==n;ok&=good
        say(f'  {kind}: units {st}/{n} completed, rows {have:,} in the table vs {int(rows):,} recorded {"OK" if good else "MISMATCH"}; by year '+', '.join(f'{y}: {c:,} ({a_}..{b_})' for y,c,a_,b_ in years))
    t=conn.execute("""SELECT count(*) FROM (SELECT 1 FROM market_data.option_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id WHERE f.dataset=%s
                      GROUP BY b.expiry,b.strike,b.option_type,b.bar_time HAVING count(*)>1) x""",(DATASET,)).fetchone()[0]
    say(f'  duplicate option bars (same contract and minute twice): {t}');ok&=t==0
    o=conn.execute("""SELECT f.dataset,min(b.trading_day),max(b.trading_day),count(*) FROM market_data.option_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id
                      WHERE b.trading_day<'2021-01-01' AND f.dataset<>%s GROUP BY 1""",(DATASET,)).fetchall()
    say(f'  other datasets with option rows before 2021: {o or "none"}')
    return ok

# ---------------------------------------------------------------- main
def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('--root',help='folder holding the spot/futures folder and the option zips')
    ap.add_argument('--attribution',help='attribution_files.csv from scripts/attribute_2017_2020.py (required for options unless --no-attribution)')
    ap.add_argument('--no-attribution',action='store_true',help='load option files without leaving out copied file-days (not recommended)')
    ap.add_argument('--copy-share',type=float,default=.5,help='a file-day is a copy of the previous zip when this share of the common minutes have identical closes')
    ap.add_argument('--min-common',type=int,default=1,help='common minutes needed before a file-day can be called a copy')
    ap.add_argument('--overrides',default=None,help='CSV zip,cp,strike,day_from,day_to,action,expiry,reason (action drop | expiry; * = any cp / strike)')
    ap.add_argument('--kinds',default='spot,future,option');ap.add_argument('--limit',type=int,default=0,help='load at most N units (a trial)')
    ap.add_argument('--dry-run',action='store_true',help='read, check and count everything; write nothing to PostgreSQL')
    ap.add_argument('--reload',action='store_true',help='replace completed units whose file or selected rows changed')
    ap.add_argument('--verify',action='store_true',help='only check what PostgreSQL holds for the dataset');ap.add_argument('--out',default='runs/import_2017_2020')
    a=ap.parse_args(argv);t0=time.time()
    conn=None
    if not a.dry_run:
        from nifty_iv.data.import_postgres import connect_db
        conn=connect_db()
        if a.verify:sys.exit(0 if verify(conn) else 1)
        if not conn.execute('SELECT pg_try_advisory_lock(hashtext(%s))',(LOCK_KEY,)).fetchone()[0]:sys.exit('another import of this dataset is running (advisory lock held)')
        conn.commit();ensure_schema(conn)
    if not a.root:sys.exit('--root is required')
    kinds=set(a.kinds.split(','))
    copies=set()
    if 'option' in kinds:
        if a.attribution:copies=read_attribution(a.attribution,a.copy_share,a.min_common)
        elif not a.no_attribution:sys.exit('--attribution attribution_files.csv is required for options (or --no-attribution)')
    ovr=read_overrides(a.overrides)
    os.makedirs(a.out,exist_ok=True);report=dict(dataset=DATASET,root=os.path.abspath(a.root),attribution=a.attribution,copy_share=a.copy_share,min_common=a.min_common,
        overrides=ovr,copy_file_days=len(copies),zips={},dry_run=a.dry_run,started=dt.datetime.now().isoformat(timespec='seconds'))
    say(f'planning: reading {a.root} ({len(copies)} copied file-days from the attribution, {len(ovr)} overrides)')
    units,zips,tdays=plan(os.path.expanduser(a.root),copies,ovr,report)
    units=[u for u in units if u.kind in kinds];zips=zips if 'option' in kinds else []
    say(f'{len(units)} spot / futures units, {len(zips)} option zips; {len(tdays)} trading days {min(tdays)}..{max(tdays)}')
    tot=collections.defaultdict(collections.Counter);man=[];fails=0;done=0
    def run(u):
        nonlocal fails
        try:
            if a.dry_run:frame,stats=u.load(0);status='dry-run';rows=len(frame)
            else:status,rows,stats=load_unit(conn,u,a.reload)
        except Exception as e:
            fails+=1;msg=f'{type(e).__name__}: {e}';say(f'  FAILED {u.rel}: {msg}')
            if conn is not None:conn.rollback();mark_failed(conn,u,msg)
            man.append(dict(unit=u.rel,kind=u.kind,status='failed',rows=0,error=msg));return
        tot[u.kind].update(stats);tot[u.kind]['loaded']+=rows;tot[u.kind]['units_'+status]+=1
        man.append(dict(unit=u.rel,kind=u.kind,status=status,rows=rows,**stats))
        if u.kind!='option':say(f'  {status} {u.rel}: {rows:,} rows  ({time.time()-t0:.0f}s)')
    for u in units:
        if a.limit and done>=a.limit:break
        run(u);done+=1
    for y,mo,zp,zrel in zips:
        if a.limit and done>=a.limit:break
        try:zu=zip_units(y,mo,zp,zrel,tdays,copies,ovr,report)
        except SystemExit as e:
            fails+=1;say(f'  FAILED zip {y}-{mo:02d}: {e}');man.append(dict(unit=zrel,kind='option-zip',status='failed',rows=0,error=str(e)));continue
        n0=tot['option']['loaded']
        for u in zu:
            if a.limit and done>=a.limit:break
            run(u);done+=1
        say(f'  {y}-{mo:02d}: {len(zu)} files, {tot["option"]["loaded"]-n0:,} rows  ({time.time()-t0:.0f}s)')
        del zu
    with open(os.path.join(a.out,'manifest.csv'),'w',newline='') as fh:
        keys=sorted({k for r in man for k in r},key=lambda k:(k not in ('unit','kind','status','rows'),k));w=csv.DictWriter(fh,fieldnames=keys);w.writeheader();w.writerows(man)
    report.update(totals={k:dict(v) for k,v in tot.items()},failed=fails,finished=dt.datetime.now().isoformat(timespec='seconds'),seconds=round(time.time()-t0))
    with open(os.path.join(a.out,'report.json'),'w') as fh:json.dump(report,fh,indent=1,default=str)
    say('\ntotals:')
    for k,v in tot.items():say(f'  {k}: '+', '.join(f'{x} {y:,}' for x,y in sorted(v.items())))
    say(f'failed units: {fails}')
    if conn is not None and not fails:
        say('verify:');ok=verify(conn)
        if not ok:fails+=1
    say(f'-> {a.out}/manifest.csv, report.json ({time.time()-t0:.0f}s)')
    sys.exit(1 if fails else 0)

if __name__=='__main__':main()
