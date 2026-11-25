#!/usr/bin/env python3
"""Extract every minute of exactly the option contracts the zone backtest can touch, for the days it can hold them.

Why: fills may happen in any minute of the bar after a decision, so all minutes are needed, but a full
all-minute chain would be about 125 GB. Which contracts a run can trade depends only on spot (signals, zones,
ATM, rolls) and on the expiry listing, so it is known before any option price is read.

Steps (each month is resumable):
  1. listing: expiries present per trading day, both vendors.
  2. plan: the backtest's own Sim runs with every contract liquid at price 1, no take-profit (which only
     shortens holds), all structures and offsets. Each leg is needed from its fill day to its exit day
     plus --buffer-sessions (slow real exits), never past expiry.
  3. minutes: 09:15-15:29 bars for those (day, expiry, strike, type), both vendors, into opt_1m.
Option rows come from data.datasets.OPTION_DATASETS (V4.9.18: with the 2017-2020 vendor data); rows hidden by an option blackout
(--option-blackouts, config/option_blackouts.csv: January 2018) are left out of the listing and the minutes, so no option position
is open across the window. Reads market_data only; never writes to PostgreSQL. Credentials from PG* variables."""
import argparse,collections,datetime as dt,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
from nifty_iv.data.research_cache import connect
from nifty_iv.data import datasets as ds
from nifty_iv.zone import signals as zs
from nifty_iv.zone import backtest as zb
from nifty_iv.core import cli

LISTING_SQL=f'''SELECT DISTINCT to_char(b.trading_day,'YYYY-MM-DD'),f.dataset,to_char(b.expiry,'YYYY-MM-DD')
FROM market_data.option_bars b JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.underlying='NIFTY' AND b.granularity='1min' AND b.option_type IN ('PE','CE') AND b.trading_day BETWEEN %s AND %s
 AND {ds.OPTION_WHERE} AND f.status='completed' '''
MINUTES_SQL=f'''SELECT DISTINCT ON (f.dataset,b.bar_time,b.expiry,b.strike,b.option_type)
 f.dataset,to_char(b.trading_day,'YYYY-MM-DD'),to_char(b.bar_time AT TIME ZONE 'Asia/Kolkata','HH24:MI'),to_char(b.expiry,'YYYY-MM-DD'),b.strike,b.option_type,b.open,b.high,b.low,b.close,b.volume
FROM market_data.option_bars b
JOIN zone_targets t ON b.trading_day=t.day AND b.expiry=t.expiry AND b.strike=t.strike AND b.option_type=t.cp
JOIN market_data.import_files f ON f.file_id=b.source_file_id
WHERE b.underlying='NIFTY' AND b.granularity='1min' AND b.trading_day BETWEEN %s AND %s
 AND {ds.OPTION_WHERE} AND f.status='completed'
 AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15'::time AND '15:29'::time{{blackout}}
ORDER BY f.dataset,b.bar_time,b.expiry,b.strike,b.option_type,b.source_row_index'''
LISTING_SQL+='{blackout}'

def with_blackouts(sql,windows):
    """(SQL, extra params) with the option-blackout predicate in place of {blackout}; params follow the query's own."""
    frag,params=ds.blackout_sql(windows);return sql.replace('{blackout}',frag),params
DDL='''CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS listing(day TEXT,provider TEXT,expiry TEXT,PRIMARY KEY(day,provider,expiry));
CREATE TABLE IF NOT EXISTS targets(day TEXT,expiry TEXT,strike REAL,cp TEXT,PRIMARY KEY(day,expiry,strike,cp));
CREATE TABLE IF NOT EXISTS opt_1m(provider TEXT,day TEXT,minute TEXT,expiry TEXT,strike REAL,cp TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER,
 PRIMARY KEY(provider,day,minute,expiry,strike,cp));
CREATE TABLE IF NOT EXISTS progress(step TEXT PRIMARY KEY,rows INTEGER);'''

def months(a,b):
    d=dt.date.fromisoformat(a).replace(day=1);end=dt.date.fromisoformat(b)
    while d<=end:
        nxt=(d.replace(day=28)+dt.timedelta(days=4)).replace(day=1);yield d.isoformat(),min(nxt-dt.timedelta(days=1),end).isoformat();d=nxt

def plan_targets(bars,final,sig,listing,a):
    """(day, expiry, strike, cp) the backtest can need. Same Sim, every contract liquid, no take-profit."""
    cfg=SimpleNamespace(slippage=0.,slippage_mode='flat',fill_model='next_bar_open',brokerage=0.,gst=0.,exchange_bps=0.,sebi_bps=0.,stt=0.,stamp=0.,brokerage_order_mode='one_order_per_leg',
        lots=a.lots,max_exit_lag_bars=-1,forced_exit_haircut=0.,max_participation=1.,min_short_days=a.min_short_days,long_target=a.long_target,contract_master=a.contract_master,
        offset_mode=getattr(a,'offset_mode','points'))
    sim=zb.Sim(zb.PlanMarket(listing),bars,final,cfg)
    trades=sim.run(zb.entry_map(bars,sig,a.from_date,a.to_date),structures=tuple(getattr(a,'structures_list',('naked','calendar'))),
                   offsets=zb.parse_offsets(a.offsets,getattr(a,'offset_mode','points')),tps=(None,),progress=False)
    sessions=sorted({b.day for b in bars});pos={d:i for i,d in enumerate(sessions)};out=set()
    for t in trades:
        if t['status']=='no_fill':continue
        legs={(f['expiry'],f['strike'],f['cp']) for f in t['fills'] if f['event']=='open'}
        pad=getattr(a,'strike_pad',0)
        if pad and t['structure']=='calendar':   # a roll whose exit spills reopens later, possibly at a neighbouring strike
            legs|={(e,k+d,cp) for e,k,cp in legs for d in range(-pad,pad+1,100) if d}
        first=t['entry_day'];last=t.get('exit_day') or sessions[-1]
        for e,k,cp in legs:
            hi=pos.get(last,len(sessions)-1)+a.buffer_sessions
            for d in sessions[pos[first]:hi+1]:
                if d<=e:out.add((d,e,k,cp))
    return out,sim.counts

def plan_combo_targets(bars,final,sig,listing,a,structures):
    """V4.9.14: (day, expiry, strike, cp) the multi-leg structures can need: for every signal, structure and level, the option legs
    (strategy_spec.combo_legs at the signal bar's close; futures legs need no option data) from the signal day until the zone changes
    (+ --buffer-sessions) or the legs expire. A superset of what nifty-zone-paths can touch (entries that never fill are included)."""
    from nifty_iv.core import strategy_spec as ss
    levels=getattr(a,'offset_mode','points')=='levels';offs=ss.COMBO_LEVELS if levels else zb.parse_offsets(a.offsets,'points')
    sessions=sorted({b.day for b in bars});pos={d:i for i,d in enumerate(sessions)};out=set();counts=collections.Counter()
    for t,sides in sorted(zb.entry_map(bars,sig,a.from_date,a.to_date).items()):
        b=bars[t];fill=b.day if b.end<zs.CLOSE else (bars[t+1].day if t+1<len(bars) else None)
        if fill is None:continue
        for side in sides:
            colour='G' if side=='bull' else 'R';end=next((k for k in range(t+1,len(bars)) if final[k]!=colour),len(bars)-1)
            last=sessions[min(pos[bars[end].day]+1+a.buffer_sessions,len(sessions)-1)]
            for st in structures:
                if side not in ss.COMBOS[st]['sides']:continue
                e,why=ss.select_short_expiry(st,fill,listing.get(b.day,()),a.min_short_days)
                if e is None:counts[f'{st}_{why}']+=1;continue
                for lv in offs:
                    for cp,k,_ in ss.combo_legs(st,side,b.close,lv,points=not levels):
                        if cp=='FUT':continue
                        for d in sessions[pos[b.day]:pos[last]+1]:
                            if d<=e:out.add((d,e,k,cp))
                    counts[f'{st}_planned']+=1
    return out,counts

def main():
    ap=cli.Parser(description=__doc__);zb.add_common(ap)
    ap.add_argument('--out',default='zone_options.sqlite');ap.add_argument('--buffer-sessions',type=int,default=3)
    ap.add_argument('--strike-pad',type=int,default=0,help='Also extract calendar strikes within this many points (multiples of 100) of the planned ones')
    ap.add_argument('--structures',default='naked,calendar',help='option structures to plan for: naked, naked_m1 (current-month monthly >= 21 days), naked_m2 (next-month monthly), calendar (V4.9.6); V4.9.14 multi-leg: vert_w1, vert_w2, married, covered, collar_w1, collar_w2, longopt, backspread_w1, backspread_w2 (use a new --out)')
    ap.add_argument('--option-blackouts',default=str(ds.BLACKOUTS),help='windows in which no option position may be held (from,to,reason; none = no windows): '
                    'option rows of contracts alive on `from` are hidden up to `to` (V4.9.18; recorded in the plan when the run starts before a window ends)')
    a=ap.parse_args();a.structures_list=[x for x in a.structures.split(',') if x]
    windows=ds.blackouts_in(ds.read_blackouts(a.option_blackouts),a.from_date)
    for f,t,why in windows:print(f'option blackout {f}..{t}: contracts alive on {f} hidden up to {t} ({why[:90]})',flush=True)
    listing_sql,lparams=with_blackouts(LISTING_SQL,windows);minutes_sql,mparams=with_blackouts(MINUTES_SQL,windows)
    db=sqlite3.connect(a.out);db.executescript(DDL)
    done={r[0] for r in db.execute('SELECT step FROM progress')}
    with connect() as pg:
        pg.autocommit=True   # each month's temp table lives in its own explicit transaction
        for lo,hi in months(a.from_date,a.to_date):
            if f'listing {lo}' in done:continue
            rows=pg.execute(listing_sql,(lo,hi,*lparams)).fetchall()
            db.executemany('INSERT OR IGNORE INTO listing VALUES(?,?,?)',rows);db.execute('INSERT INTO progress VALUES(?,?)',(f'listing {lo}',len(rows)));db.commit()
            print('listing',lo,len(rows),flush=True)
        listing=collections.defaultdict(set)
        for d,e in db.execute('SELECT day,expiry FROM listing'):listing[d].add(e)
        plan={k:getattr(a,k) for k in zb.PLAN_KEYS+('from_date','to_date','buffer_sessions','contract_master')}
        plan['offsets']=list(zb.parse_offsets(a.offsets,a.offset_mode))
        if a.offset_mode!='points':plan['offset_mode']=a.offset_mode
        if a.structures_list!=['naked','calendar']:plan['structures']=a.structures_list   # older plans (no keys) stay valid for naked + calendar in points
        if windows:plan['option_blackouts']=[[f,t] for f,t,_ in windows]                  # V4.9.18; runs starting after every window keep their old plan
        old=db.execute("SELECT value FROM meta WHERE name='plan'").fetchone()
        if old and json.loads(old[0])!=plan:sys.exit(f'{a.out} was planned with {old[0]}; use a new --out')
        if not old:
            minutes=zs.load_minutes(a.spot)
            if not minutes:sys.exit(f'No spot minutes in {a.spot}: run nifty-zone-spot first')
            bars,final,_=zs.zones(minutes);sig=zs.signal_grid(bars,final)
            from nifty_iv.core import strategy_spec as ss
            combos=[x for x in a.structures_list if ss.is_combo(x)];legacy=[x for x in a.structures_list if not ss.is_combo(x)]
            targets=set();counts=collections.Counter()
            if legacy:
                a.structures_list=legacy;t1,c1=plan_targets(bars,final,sig,listing,a);targets|=t1;counts.update(c1);a.structures_list=legacy+combos
            if combos:t2,c2=plan_combo_targets(bars,final,sig,listing,a,combos);targets|=t2;counts.update(c2)
            db.executemany('INSERT OR IGNORE INTO targets VALUES(?,?,?,?)',sorted(targets))
            db.execute('INSERT INTO meta VALUES(?,?)',('plan',json.dumps(plan)));db.execute('INSERT INTO meta VALUES(?,?)',('plan_counts',json.dumps(dict(counts))));db.commit()
            print(f'plan: {len(targets)} contract-days; {dict(counts)}',flush=True)
        for lo,hi in months(a.from_date,a.to_date):
            if f'minutes {lo}' in done:continue
            tg=db.execute('SELECT day,expiry,strike,cp FROM targets WHERE day BETWEEN ? AND ?',(lo,hi)).fetchall()
            n=0
            if tg:
                with pg.transaction():
                    pg.execute('CREATE TEMP TABLE zone_targets ON COMMIT DROP AS SELECT trading_day AS day,expiry,strike,option_type AS cp FROM market_data.option_bars WITH NO DATA')
                    with pg.cursor().copy('COPY zone_targets(day,expiry,strike,cp) FROM STDIN') as cp:
                        for r in tg:cp.write_row(r)
                    pg.execute('ANALYZE zone_targets')
                    rows=[(p,d,m,e,float(k),c,*(None if x is None else float(x) for x in (o,h,l,cl)),None if v is None else int(v)) for p,d,m,e,k,c,o,h,l,cl,v in pg.execute(minutes_sql,(lo,hi,*mparams))]
                db.executemany('INSERT OR REPLACE INTO opt_1m VALUES(?,?,?,?,?,?,?,?,?,?,?)',rows);n=len(rows)
            db.execute('INSERT OR REPLACE INTO progress VALUES(?,?)',(f'minutes {lo}',n));db.commit()
            print('minutes',lo,'contract-days',len(tg),'bars',n,flush=True)
    db.execute('CREATE INDEX IF NOT EXISTS opt_1m_day ON opt_1m(day)');db.commit()
    have=db.execute('SELECT COUNT(DISTINCT day||expiry||strike||cp) FROM opt_1m').fetchone()[0];want=db.execute('SELECT COUNT(*) FROM targets').fetchone()[0]
    print(f'done: {have} of {want} planned contract-days have at least one bar')

if __name__=='__main__':main()
