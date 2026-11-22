#!/usr/bin/env python3
"""Exit rules for the zone study, evaluated on bar closes for every stored path of a group at once (numpy matrices
paths x bars). A rule fires at the first 45-minute close where its condition holds; the exit then fills in the next
bar's minutes exactly as the path engine stored it. A rule that never fires leaves the path to its natural end.

Anchors: s0 = index close at the signal bar; e0 = index at the minute the entry filled (s0 if unknown). ATR is Wilder
ATR on 45-minute bars; "sig" = its value at the signal bar, "now" = at the current bar. d = signed index move from s0,
de = signed move from e0 (positive = in the trade's favour). Sessions are trading days in the bar file.

STOP families (kind: parameters)
  none
  atr P x M      d  < -M*ATRsig(P)           (signal-anchored; the V4.6 stop is atr P x 1)
  eatr P x M     de < -M*ATRsig(P)           (entry-anchored)
  pct X / epct X d < -X% of s0 / de < -X% of e0
  pts X          de < -X index points
  trail P x M    best signed close since the fill (from e0) - signed close > M*ATRnow(P)
  strail P x M   the same from the signal close (V4.7.0 definition, kept for comparison)
  trailpct X / trailpts X   retracement from that best close > X% of it / > X points
  chand L x M    best signed close over the last L bars of the trade - signed close > M*ATRnow(15)
  donch L c C    close beyond the lowest low (bull) / highest high (bear) of the previous L bars, by C*ATRnow(15)
  be A c C       once the best move reached A*ATRsig(15), exit if de falls to C*ATRsig(15) (breakeven + cushion)
  gb A x G       once the best move reached A*ATRsig(15), exit after giving back G of the best move
  time N         N or more sessions since the entry day
  exp T          naked and futures_expiry: the short expiry is T sessions away, at the 15:15 bar (or later)
  ema L p Q      Q consecutive closes beyond EMA(L) against the trade
  vr R           ATRnow(15) / ATRnow(45) >= R (volatility expansion)
  tr X           the bar's true range >= X * ATR(15) of the previous bar
  tod HHMM       the first bar ending at or after HH:MM (no overnight holding);  wkend: 15:15 bar of the last session before a Saturday
  stag N a A     after N sessions, the best move is still below A*ATRsig(15)
  loss X         naked: pair loss >= X% of the credit;  dloss X  calendar: pair loss >= X% of the debit
  mult X         naked: short option close >= X times its entry fill price
  mloss X        the trade's mark-to-market loss >= X% of its margin (strategy_spec table, upper end, on the entry day: units x
                 margin per unit); every structure (V4.9.8: a stop that scales with the lots and the margin of the period)
PROFIT families
  none;  tp X    pair profit >= X% of the credit (naked) / debit (calendar), on a printed, non-roll bar
  target P x M   d >= M*ATRsig(P);   tpct X   d >= X% of s0;   ptime N   N sessions held and the trade is in profit
Premium rules (tp, loss, dloss, mult) need every leg printed at the bar's last minute; futures have none of them.
"""
from __future__ import annotations
import bisect,collections,datetime as dt,math,re
import numpy as np
import zone_signals as zs
import strategy_spec as ss

ATR_PERIODS=(15,20,30,45,60)
ATR_MULTS=(.25,.5,.75,1,1.25,1.5,2,2.5,3,4,5)
TRAIL_MULTS=(.5,.75,1,1.5,2,2.5,3,4,5)
PCTS=(.25,.5,.75,1,1.25,1.5,2,2.5,3,4,5)
POINTS=(50,75,100,150,200,250,300,400,500,750,1000)
EMAS=(10,20,50,100);LOOKBACKS=(6,12,24,48)
MARGIN_LOSS_PCTS=(1,1.5,2,2.5,3,3.75,5,6.25,7.5,10)   # mloss X = loss >= X% of the position's margin (V4.9.8; V4.9.9 adds 1 / 1.5 / 2 / 3: the plateaus sat at 2-3.75%)
def g(x):return f'{x:g}'

def stop_rules(structure):
    naked=ss.is_naked(structure);cal=structure=='calendar'
    r=['none']
    r+=[f'atr{p}x{g(m)}' for p in ATR_PERIODS for m in ATR_MULTS]+[f'eatr{p}x{g(m)}' for p in ATR_PERIODS for m in ATR_MULTS]
    r+=[f'pct{g(x)}' for x in PCTS]+[f'epct{g(x)}' for x in PCTS]+[f'pts{x}' for x in POINTS]
    r+=[f'trail{p}x{g(m)}' for p in ATR_PERIODS for m in TRAIL_MULTS]+[f'strail{p}x{g(m)}' for p in (15,30,45) for m in (1,1.5,2,2.5,3)]
    r+=[f'trailpct{g(x)}' for x in (.25,.5,.75,1,1.5,2,3,4)]+[f'trailpts{x}' for x in (50,100,150,200,300,500,750)]
    r+=[f'chand{L}x{g(m)}' for L in LOOKBACKS for m in (1,1.5,2,2.5,3,4)]+[f'donch{L}c{g(c)}' for L in LOOKBACKS for c in (0,.25,.5,1)]
    r+=[f'be{g(a)}c{g(c)}' for a in (.5,1,1.5,2,3) for c in (0,.1,.25)]+[f'gb{g(a)}x{g(q)}' for a in (.5,1,1.5,2,3) for q in (.25,.33,.5,.67,.75)]
    r+=[f'time{n}' for n in (1,2,3,5,7,10,15,20,30)]
    if naked or structure=='futures_expiry':r+=[f'exp{t}' for t in (3,2,1)]
    r+=[f'ema{L}p{q}' for L in EMAS for q in (1,2,3)]+[f'vr{g(x)}' for x in (1.25,1.5,1.75,2,2.5)]+[f'tr{g(x)}' for x in (1.5,2,3)]
    r+=['tod1400','tod1445','tod1515','wkend']+[f'stag{n}a{g(a)}' for n in (1,2,3,5,7) for a in (.25,.5,1)]
    if naked:r+=[f'loss{x}' for x in (25,35,50,75,100,125,150,200,250,300)]+[f'mult{g(x)}' for x in (1.2,1.35,1.5,1.75,2,2.5,3,4)]
    if cal:r+=[f'dloss{x}' for x in (10,20,30,40,50,75,100,150)]
    r+=[f'mloss{g(x)}' for x in MARGIN_LOSS_PCTS]
    return r

def profit_rules(structure):
    r=['none']
    if ss.is_naked(structure):r+=[f'tp{x}' for x in (5,10,15,20,25,30,35,40,45,50,60,70,75,80,90,95)]
    if structure=='calendar':r+=[f'tp{x}' for x in (5,10,15,20,25,35,50,75,100)]
    return r+[f'target{p}x{g(m)}' for p in (15,30,45) for m in (.5,1,1.5,2,2.5,3,4,5)]+[f'tpct{g(x)}' for x in (.5,1,1.5,2,3,4)]+[f'ptime{n}' for n in (3,5,7)]

# ---- layered (compound) policies: initial protection + profit management + mandatory exit, the earliest one firing ----
def compound_parts(structure):
    naked=ss.is_naked(structure);cal=structure=='calendar'
    init=['none','atr15x1','atr15x1.5','atr15x2','atr15x3','eatr15x1','eatr15x2','pct1','pct2']+(['loss50','loss100'] if naked else ['dloss30','dloss50'] if cal else ['pts100','pts200'])+['mloss2','mloss3','mloss5']   # margin-based cut-offs as initial protection (V4.9.9: 2 / 3 / 5%)
    manage=['none','trail15x1.5','trail15x2','trail15x3','be1c0','be2c0','gb1x0.5','gb2x0.5','trailpct1','trailpct2']
    term=['none','time3','time5','time10','tod1515','wkend']+(['exp1','exp2'] if naked or structure=='futures_expiry' else [])
    return init,manage,term
def compound_rules(structure):
    init,manage,term=compound_parts(structure)
    return [f'c:{i}|{m}|{t}' for t in term for i in init for m in manage if (i,m,t)!=('none','none','none')]
def compound_profits(structure):
    return ['none']+(['tp25','tp50','tp75'] if ss.is_naked(structure) or structure=='calendar' else ['tpct2'])+['target15x2','target15x4']
def compound_neighbours(labels):
    """Neighbours of a layered policy: the same policy with its initial or management part one step away (same family)."""
    parts=[x[2:].split('|') for x in labels];pos={tuple(p):i for i,p in enumerate(parts)};out=[]
    comp=[sorted({p[d] for p in parts}) for d in range(3)];nb=[dict(zip(c,neighbours(c))) for c in comp]
    for p in parts:
        n=[]
        for d in (0,1):
            for j in nb[d][p[d]]:
                if j>=0:
                    q=list(p);q[d]=comp[d][j];k=pos.get(tuple(q))
                    if k is not None:n.append(k)
        out.append(n)
    w=max([len(x) for x in out]+[1]);A=np.full((len(labels),w),-1,np.int64)
    for i,x in enumerate(out):A[i,:len(x)]=x
    return A

PATTERNS=[(k,re.compile(p)) for k,p in (('none','none'),('wkend','wkend'),('tp',r'tp(\d+)'),('tpct',r'tpct([\d.]+)'),('target',r'target(\d+)x([\d.]+)'),('ptime',r'ptime(\d+)'),
    ('atr',r'atr(\d+)x([\d.]+)'),('eatr',r'eatr(\d+)x([\d.]+)'),('pct',r'pct([\d.]+)'),('epct',r'epct([\d.]+)'),('pts',r'pts(\d+)'),
    ('trail',r'trail(\d+)x([\d.]+)'),('strail',r'strail(\d+)x([\d.]+)'),('trailpct',r'trailpct([\d.]+)'),('trailpts',r'trailpts(\d+)'),
    ('chand',r'chand(\d+)x([\d.]+)'),('donch',r'donch(\d+)c([\d.]+)'),('be',r'be([\d.]+)c([\d.]+)'),('gb',r'gb([\d.]+)x([\d.]+)'),
    ('time',r'time(\d+)'),('exp',r'exp(\d+)'),('ema',r'ema(\d+)p(\d+)'),('vr',r'vr([\d.]+)'),('tr',r'tr([\d.]+)'),('tod',r'tod(\d{4})'),
    ('stag',r'stag(\d+)a([\d.]+)'),('loss',r'loss(\d+)'),('dloss',r'dloss(\d+)'),('mult',r'mult([\d.]+)'),('mloss',r'mloss([\d.]+)'))]
PERCENT_KINDS=('tp','loss','dloss')

def parse(label):
    """('atr',30,1.5) / ('tp',.25) / ('loss',1.5) / ('none',) / ('compound','atr15x2','trail15x2','time5') ..."""
    if label.startswith('c:'):return ('compound',)+tuple(label[2:].split('|'))
    for kind,rx in PATTERNS:
        m=rx.fullmatch(label)
        if m:
            vals=tuple(int(x) if re.fullmatch(r'\d+',x) else float(x) for x in m.groups())
            if kind in PERCENT_KINDS:vals=(vals[0]/100,)
            return (kind,)+vals
    raise ValueError(f'unknown rule {label}')
def kind_of(label):return parse(label)[0]
def chunk_of(label):
    """Storage chunk of a stop label: its family, or compound_<terminal part's family> for layered policies."""
    k=parse(label)
    return f'compound_{kind_of(k[3])}' if k[0]=='compound' else k[0]

def neighbours(labels):
    """For each label, indices of labels of the same kind that differ by one step in one parameter (-1 padded)."""
    parsed=[parse(x) for x in labels];by=collections.defaultdict(dict)
    for i,p in enumerate(parsed):by[p[0]][p[1:]]=i
    out=[]
    for p in parsed:
        kind,vals=p[0],p[1:];nb=[]
        for d in range(len(vals)):
            axis=sorted({v[d] for v in by[kind] if all(v[e]==vals[e] for e in range(len(vals)) if e!=d)})
            k=axis.index(vals[d])
            for kk in (k-1,k+1):
                if 0<=kk<len(axis):nb.append(by[kind][vals[:d]+(axis[kk],)+vals[d+1:]])
        out.append(nb)
    w=max([len(x) for x in out]+[1]);A=np.full((len(labels),w),-1,np.int64)
    for i,x in enumerate(out):A[i,:len(x)]=x
    return A

CONFIG_RX=re.compile(r'N(\d+)_k([\d.]+)_m(\d+)_j([\d.]+)_tw(\d+)');ZONE_RX=re.compile(r'zone_only_tw(\d+)')
def config_neighbours(configs):
    """Signal configurations one grid step away in one of N, k, m, j, tw (zone-only: tw only)."""
    key={};keys=[];vals=collections.defaultdict(set)
    for i,c in enumerate(configs):
        m=CONFIG_RX.fullmatch(c)
        if m:t=('s',)+tuple(float(x) for x in m.groups())
        else:
            z=ZONE_RX.fullmatch(c);t=('z',float(z.group(1))) if z else ('x',i)
        key[t]=i;keys.append(t)
        for d,v in enumerate(t[1:]):vals[t[0],d].add(v)
    out=[]
    for t in keys:
        nb=[]
        for d in range(len(t)-1):
            axis=sorted(vals[t[0],d]);k=axis.index(t[d+1])
            for kk in (k-1,k+1):
                if 0<=kk<len(axis):
                    u=t[:d+1]+(axis[kk],)+t[d+2:]
                    if u in key:nb.append(key[u])
        out.append(nb)
    w=max([len(x) for x in out]+[1]);A=np.full((len(configs),w),-1,np.int64)
    for i,x in enumerate(out):A[i,:len(x)]=x
    return A

class BarArrays:
    """Per-bar arrays for rule evaluation."""
    def __init__(self,day,end,high,low,close,holidays=None):
        self.day=list(day);self.end=np.asarray(end,np.int64);self.high=np.asarray(high,float);self.low=np.asarray(low,float);self.close=np.asarray(close,float);n=len(self.close)
        self.ord=np.array([dt.date.fromisoformat(d).toordinal() for d in self.day],np.int64)
        self.days=sorted(set(self.day));ix={d:i for i,d in enumerate(self.days)};self.session=np.array([ix[d] for d in self.day],np.int64)
        bars=[zs.Bar(d,0,0,c,h,l,c) for d,h,l,c in zip(self.day,self.high,self.low,self.close)]
        self.atr={p:zs.atr(bars,p) for p in sorted(set(ATR_PERIODS)|{15,45})}
        prev=np.r_[self.close[0],self.close[:-1]]
        self.tr=np.maximum(self.high-self.low,np.maximum(abs(self.high-prev),abs(self.low-prev)))
        self.atr15_prev=np.r_[np.nan,self.atr[15][:-1]]
        self.below={};self.above={}
        for L in EMAS:
            e=zs.ema(self.close,L);b=self.close<e;a=self.close>e
            for q in (1,2,3):self.below[L,q]=_run(b)>=q;self.above[L,q]=_run(a)>=q
        self.prev_low={};self.prev_high={}
        for L in LOOKBACKS:
            lo=np.r_[np.full(L,np.inf),self.low];hi=np.r_[np.full(L,-np.inf),self.high]
            self.prev_low[L]=np.lib.stride_tricks.sliding_window_view(lo,L)[:n].min(1)
            self.prev_high[L]=np.lib.stride_tricks.sliding_window_view(hi,L)[:n].max(1)
        self.set_holidays(holidays)
    def set_holidays(self,holidays=None):
        """Weekend exits from the exchange calendar, never from gaps in the data: a session is the last before a weekend when
        every weekday after it up to Saturday is an exchange holiday (so Friday, or Thursday when Friday is a holiday).
        Without a holiday list only Fridays qualify (weeks whose Friday is a holiday then have no weekend exit)."""
        self.holidays=frozenset(holidays or ())
        wk=np.zeros(len(self.days),bool)
        for i,d in enumerate(dt.date.fromisoformat(x) for x in self.days):
            if d.weekday()>4:continue
            later=[d+dt.timedelta(x) for x in range(1,5-d.weekday())]           # the weekdays left this week
            wk[i]=all(x.isoformat() in self.holidays for x in later)
        self.weekend=(self.end>=zs.CLOSE-15)&wk[self.session]
    @classmethod
    def from_db(cls,db,holidays=None):
        rows=db.execute('SELECT day,end_minute,high,low,close FROM bars ORDER BY idx').fetchall()
        return cls([r[0] for r in rows],[zs.minute_of(r[1]) for r in rows],[r[2] for r in rows],[r[3] for r in rows],[r[4] for r in rows],holidays)
    @classmethod
    def from_close(cls,close,atr=None,day=None,end=None):
        """Test helper: flat high/low around close; optional fixed ATR per period."""
        close=np.asarray(close,float);n=len(close)
        b=cls(day or ['2024-01-02']*n,end if end is not None else [600+45*i for i in range(n)],close,close,close)
        if atr is not None:
            for p in b.atr:b.atr[p]=np.asarray(atr.get(p,atr.get(15)),float) if isinstance(atr,dict) else np.full(n,float(atr))
            b.atr15_prev=np.r_[np.nan,b.atr[15][:-1]]
        return b
    def session_of(self,day):return bisect.bisect_left(self.days,day)

HOLIDAY_FORMATS=('%Y-%m-%d','%d-%m-%Y','%d/%m/%Y','%d-%b-%Y','%d %b %Y','%d-%B-%Y','%d %B %Y','%B %d, %Y','%b %d, %Y','%d.%m.%Y')
def read_holidays(path):
    """Exchange holidays from a text file: any line containing a date in a common format (ISO, dd-mm-yyyy, 26-Jan-2024,
    'January 26, 2024', ...). Returns sorted ISO dates."""
    out=set()
    for line in open(path,encoding='utf-8',errors='replace'):
        toks=re.findall(r'\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{1,2}[- ][A-Za-z]{3,9}[- ,]+\d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}',line)
        for t in toks:
            t=t.replace(',',', ').replace('  ',' ').strip() if re.match(r'[A-Za-z]',t) else t
            for f in HOLIDAY_FORMATS:
                try:out.add(dt.datetime.strptime(t,f).date().isoformat());break
                except ValueError:continue
    return sorted(out)

def _run(b):
    """Length of the current run of True values at each position."""
    out=np.zeros(len(b),np.int64);c=0
    for i,x in enumerate(b):c=c+1 if x else 0;out[i]=c
    return out

class Batch:
    """All stored paths of one group padded into (paths x bars) matrices."""
    FIELDS=('cash_total','cash_pair','base','value','mark','exit_cash','exit_bar','exit_day','exit_forced','roll','forced_before','exit_sessions')
    INT_FIELDS=('exit_bar','exit_day','exit_forced','roll','forced_before','exit_sessions')
    def __init__(self,paths,bars,structure):
        self.paths=paths;self.bars=bars;self.structure=structure;Np=self.Np=len(paths)
        self.L=np.array([len(p['rows']['cash_total']) for p in paths],np.int64);Lmax=self.Lmax=int(self.L.max()) if Np else 1
        j=np.arange(Lmax);self.valid=j[None,:]<self.L[:,None]
        self.t=np.array([p['signal_bar'] for p in paths],np.int64)
        self.f=np.array([p.get('first_bar') if p.get('first_bar') is not None else p['signal_bar']+1 for p in paths],np.int64)   # V4.9.7: rows start after the entry completes
        self.K=self.f[:,None]+np.minimum(j[None,:],self.L[:,None]-1)
        self.R={}
        for f in self.FIELDS:
            A=np.empty((Np,Lmax),np.int64 if f in self.INT_FIELDS else float)
            for i,p in enumerate(paths):
                a=p['rows'].get(f)
                if a is None:a=np.full(len(p['rows']['cash_total']),0 if f=='exit_sessions' else 10**9 if f in self.INT_FIELDS else np.nan)   # older path files
                A[i,:len(a)]=a;A[i,len(a):]=a[-1]
            self.R[f]=A
        from zone_paths import SLOTS
        self.S=SLOTS;self.slot_day=np.full((Np,Lmax,SLOTS),10**9,np.int64);self.slot_cash=np.full((Np,Lmax,SLOTS),np.nan);self.slot_value=np.full((Np,Lmax,SLOTS),np.nan)
        for i,p in enumerate(paths):
            r=p['rows'];n=len(r['cash_total'])
            for q in range(SLOTS):
                for A,name in ((self.slot_day,f'exit_slot_day{q}'),(self.slot_cash,f'exit_slot_cash{q}'),(self.slot_value,f'exit_slot_value{q}')):
                    a=r.get(name)
                    if a is not None:A[i,:n,q]=a;A[i,n:,q]=a[-1]
        self._rv={}
        self.sign=np.array([1. if p['side']=='bull' else -1. for p in paths]);self.units=np.array([p['units'] for p in paths],float)
        self.s0=bars.close[self.t];e=np.array([p.get('entry_index',math.nan) for p in paths],float);e=np.where(np.isfinite(e),e,self.s0);self.e0=e
        self.c=bars.close[self.K];self.d=self.sign[:,None]*(self.c-self.s0[:,None]);self.de=self.sign[:,None]*(self.c-self.e0[:,None])
        self.best=np.maximum(np.maximum.accumulate(self.sign[:,None]*self.c,1),(self.sign*self.e0)[:,None])        # best signed close since the fill
        self.mfe=np.maximum(np.maximum.accumulate(self.de,1),0.)
        es=np.array([bars.session_of(p['entry_day']) for p in paths],np.int64);self.held=bars.session[self.K]-es[:,None]
        self.pair=self.R['cash_pair']+self.R['value']
        with np.errstate(invalid='ignore'):self.printed=np.isfinite(self.pair)&(self.R['base']>0)
        exp=[p.get('short_expiry') for p in paths]
        self.to_exp=np.array([bars.session_of(x) if x else 10**6 for x in exp],np.int64)[:,None]-bars.session[self.K]
        self.entry_px=np.array([p.get('entry_short_px') or math.nan for p in paths],float)
        # outcomes at each row: P&L per unit if the exit is decided there; the natural end is the path's own end
        last=self.L-1;ar=np.arange(Np)
        self.pnl=(self.R['cash_total']+self.R['exit_cash'])/self.units[:,None]
        self.pnl[ar,last]=np.array([p['end_pnl'] if p['end_pnl'] is not None else math.nan for p in paths],float)/self.units
        self.xb=self.R['exit_bar'].copy();self.xd=self.R['exit_day'].copy();self.hyp=np.zeros((Np,Lmax),bool)
        op=np.array([p['status']=='open_at_data_end' for p in paths])
        self.xb[ar[op],last[op]]=10**9;self.xd[ar[op],last[op]]=10**9;self.hyp[ar[op],last[op]]=True
        self.markpu=self.R['mark']/self.units[:,None];self.row_ord=bars.ord[self.K]
        self.mpu=np.array([ss.margin_per_unit(p['entry_day']) for p in paths],float)          # margin per unit on the entry day (mloss)
        self._atr_sig={};self._atr_now={}
    def atr_sig(self,p):
        if p not in self._atr_sig:self._atr_sig[p]=self.bars.atr[p][self.t]
        return self._atr_sig[p]
    def atr_now(self,p):
        if p not in self._atr_now:self._atr_now[p]=self.bars.atr[p][self.K]
        return self._atr_now[p]
    def row_value(self,h):
        """(paths, rows): per-unit value at window end h (date ordinal) of a trade whose exit is decided at that row.
        Realised P&L if the exit's last fill is before h; if its exit had begun before h, the state at the close of the last
        session before h (actual cash received + legs still open at that session's close; NaN if unpriceable or past the
        stored sessions); otherwise the path's own mark at the last close before h (all legs still held)."""
        if h not in self._rv:
            k=(self.slot_day<h).sum(-1)-1;kk=np.maximum(k,0)[...,None]
            v=(self.R['cash_total']+np.take_along_axis(self.slot_cash,kk,-1)[...,0]+np.take_along_axis(self.slot_value,kk,-1)[...,0])/self.units[:,None]
            v=np.where((self.R['exit_sessions']>self.S)&(k==self.S-1),np.nan,v)   # past the stored sessions: unknown
            self._rv[h]=np.where(self.xd<h,self.pnl,np.where(k>=0,v,self.mark_at(h)[:,None]))
        return self._rv[h]
    def value_at(self,J,pnl,xd,h):
        """row_value(h) at the exit rows J (rules, rules, paths)."""
        return self.row_value(h)[np.arange(self.Np)[None,None,:],J]
    def mark_at(self,h):
        """Per-unit mark at the last row before date ordinal h (NaN if none)."""
        jh=(self.row_ord<h).sum(1)-1;v=self.markpu[np.arange(self.Np),np.maximum(jh,0)];return np.where(jh>=0,v,np.nan)

def _first(mask,b):
    """(R,Np,Lmax) mask -> first row per rule and path, else the natural end."""
    mask=mask&b.valid[None];hit=mask.any(-1);return np.where(hit,mask.argmax(-1),(b.L-1)[None,:])

def evaluate(rules,b):
    """First firing row of every rule for every path: array (len(rules), Np)."""
    out=np.empty((len(rules),b.Np),np.int64);groups=collections.defaultdict(list)
    for i,lab in enumerate(rules):groups[kind_of(lab)].append(i)
    ints=lambda idx,pos:np.array([parse(rules[i])[pos] for i in idx])
    S=b.sign[:,None];bars=b.bars
    cap=max(1,int(2e7//max(1,b.Np*b.Lmax)))           # rules per sub-batch: no temporary above ~2e7 elements
    if 'compound' in groups:                           # earliest of its three parts, each evaluated once
        idx=groups.pop('compound');parts=[parse(rules[i])[1:] for i in idx];uniq=sorted({x for p in parts for x in p})
        F=dict(zip(uniq,evaluate(uniq,b)))
        for i,p in zip(idx,parts):out[i]=np.minimum(np.minimum(F[p[0]],F[p[1]]),F[p[2]])
    for kind,allidx in groups.items():
      for s0_ in range(0,len(allidx),cap):
        idx=np.array(allidx[s0_:s0_+cap])
        if kind=='none':out[idx]=(b.L-1)[None,:];continue
        npar=len(parse(rules[idx[0]]))-1
        P=ints(idx,1) if npar else None;M=ints(idx,-1).astype(float)[:,None,None] if npar else None
        if kind=='tp':m=(b.printed&(b.R['roll']==0))[None]&(b.pair[None]>=M*b.R['base'][None])
        elif kind in ('loss','dloss'):m=b.printed[None]&(b.pair[None]<=-M*b.R['base'][None])
        elif kind=='mult':
            with np.errstate(invalid='ignore',divide='ignore'):px=np.where(b.printed,b.R['value']/-b.units[:,None],np.nan)
            m=px[None]>=M*b.entry_px[None,:,None]
        elif kind=='ptime':
            prof=(b.de>0) if b.structure.startswith('futures') else (b.printed&(b.pair>0))
            m=(b.held[None]>=M)&prof[None]
        elif kind in ('target','atr','eatr'):
            a=np.stack([b.atr_sig(p) for p in P])[:,:,None]
            with np.errstate(invalid='ignore'):
                m=(b.d[None]>=M*a) if kind=='target' else (b.d[None]<-M*a) if kind=='atr' else (b.de[None]<-M*a)   # strictly beyond, as in V4.6
            m&=np.isfinite(a)
        elif kind=='tpct':m=b.d[None]>=M/100*b.s0[None,:,None]
        elif kind=='pct':m=b.d[None]<-M/100*b.s0[None,:,None]
        elif kind=='epct':m=b.de[None]<-M/100*b.e0[None,:,None]
        elif kind=='pts':m=b.de[None]<-M
        elif kind=='mloss':
            with np.errstate(invalid='ignore'):m=b.markpu[None]<=-M/100*b.mpu[None,:,None]
        elif kind in ('trail','strail'):
            best=b.best if kind=='trail' else np.maximum(np.maximum.accumulate(S*b.c,1),(b.sign*b.s0)[:,None])
            a=np.stack([b.atr_now(p) for p in P])
            with np.errstate(invalid='ignore'):m=np.isfinite(a)&((S*b.c)[None]<best[None]-M*np.nan_to_num(a,nan=np.inf))
        elif kind=='trailpct':m=(b.best-S*b.c)[None]>M/100*abs(b.best)[None]
        elif kind=='trailpts':m=(b.best-S*b.c)[None]>M
        elif kind=='chand':
            m=np.zeros((len(idx),b.Np,b.Lmax),bool);a=np.nan_to_num(b.atr_now(15),nan=np.inf)
            for r,(L,mm) in enumerate(zip(P,M[:,0,0])):
                pad=np.concatenate([np.repeat((b.sign*b.e0)[:,None],L-1,1),S*b.c],1)
                roll=np.lib.stride_tricks.sliding_window_view(pad,L,axis=1).max(-1)
                m[r]=(S*b.c)<roll-mm*a
        elif kind=='donch':
            m=np.zeros((len(idx),b.Np,b.Lmax),bool);a=np.nan_to_num(b.atr_now(15),nan=np.inf)
            for r,(L,cc) in enumerate(zip(P,M[:,0,0])):
                lo=bars.prev_low[L][b.K];hi=bars.prev_high[L][b.K]
                cush=cc*a if cc else 0.                       # zero cushion needs no ATR (0 x inf would be NaN)
                m[r]=np.where(S>0,b.c<lo-cush,b.c>hi+cush)
        elif kind in ('be','gb','stag'):
            a=b.atr_sig(15)[None,:,None]
            A=ints(idx,1).astype(float)[:,None,None]
            with np.errstate(invalid='ignore'):
                if kind=='be':m=(b.mfe[None]>=A*a)&(b.de[None]<=M*a)
                elif kind=='gb':m=(b.mfe[None]>=A*a)&(b.de[None]<=(1-M)*b.mfe[None])
                else:m=(b.held[None]>=A)&(b.mfe[None]<M*a)
        elif kind=='time':m=b.held[None]>=M
        elif kind=='exp':m=(b.to_exp[None]<M)|((b.to_exp[None]==M)&(bars.end[b.K][None]>=zs.CLOSE-15))
        elif kind=='ema':
            m=np.stack([np.where(S>0,bars.below[L,q][b.K],bars.above[L,q][b.K]) for L,q in zip(P,M[:,0,0].astype(int))])
        elif kind=='vr':
            with np.errstate(invalid='ignore',divide='ignore'):m=(b.atr_now(15)/b.atr_now(45))[None]>=M
        elif kind=='tr':
            with np.errstate(invalid='ignore'):m=bars.tr[b.K][None]>=M*bars.atr15_prev[b.K][None]
        elif kind=='tod':
            cut=np.array([int(str(int(v)).zfill(4)[:2])*60+int(str(int(v)).zfill(4)[2:]) for v in M[:,0,0]])[:,None,None]
            m=bars.end[b.K][None]>=cut
        elif kind=='wkend':m=np.repeat(bars.weekend[b.K][None],len(idx),0)
        else:raise ValueError(kind)
        out[idx]=_first(m,b)
    return out

def outcomes(b,profits,stops,his):
    """Per (profit, stop, path): per-unit P&L, exit bar, exit day, value at each window end in `his` (date ordinals)."""
    FP=evaluate(profits,b);FS=evaluate(stops,b)
    J=np.minimum(FP[:,None,:],FS[None,:,:]);ar=np.arange(b.Np)[None,None,:]
    pnl=b.pnl[ar,J];xd=b.xd[ar,J]
    vh=np.stack([b.value_at(J,pnl,xd,h) for h in his]) if his else np.zeros((0,)+J.shape)
    return dict(J=J,FP=FP,FS=FS,pnl=pnl,xb=b.xb[ar,J],xd=xd,vh=vh,hyp=b.hyp[ar,J])
