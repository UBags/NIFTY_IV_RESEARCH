"""Strategy and account specifications shared by contract planning, path simulation and the portfolio (V4.9.6, V4.9.7).
0. Entry size (V4.9.7): ceil(TARGET_FRACTION x the lots whose upper-end margin fits the budget), see entry_lots.

1. NIFTY margin table (user-supplied): typical overnight margin per lot (SPAN + exposure) by period. The UPPER end of each range is
   used, as a margin per unit (upper margin / that period's lot), so a contract of another lot size trading in a transition window
   is charged in proportion to its own lot. Extend MARGIN_TABLE when more data is added.
2. Strike distance from ATM, set by the index level at the signal: index < 5,000: steps of 50; < 12,000: steps of 100; else 200.
   Distances are levels 1..5 of that step, on BOTH sides of ATM: level > 0 = OTM, level < 0 = ITM, 0 = ATM.
   Green (bull) sells a put: strike = ATM - level x step (OTM below ATM). Red (bear) sells a call: strike = ATM + level x step.
3. Expiry policy of a naked short (structure name):
     naked     nearest listed expiry with >= --min-short-days calendar days (the existing control, H0)
     naked_m1  current calendar month's monthly expiry, only if >= 21 calendar days remain on the fill day (H1);
               fewer days: policy_ineligible (a deliberate skip, not a data gap; the position slot stays free)
     naked_m2  next calendar month's monthly expiry (H2)
   A monthly expiry is a listed expiry in the last seven days of its month (holiday-shifted monthlies included). If the required
   monthly is not in the listing that day: no_short_expiry (a data gap). No rolling, no re-entry: only the existing signals enter."""
from __future__ import annotations
import bisect,datetime as dt,math

# (period start, lot size of the period, typical NIFTY range, margin per lot low, margin per lot HIGH, context)
MARGIN_TABLE=(
    ('2004-01-01',200,'1,500-2,200',25_000,45_000,'Minimum contract value ~2 lakh; brokers offered high intraday leverage'),
    ('2005-07-01',100,'2,300-4,500',25_000,50_000,'Lot halved to keep contract values near 2-3 lakh'),
    ('2007-07-01',50,'2,500-8,000',20_000,55_000,'Halved again as NIFTY crossed 4,500; SPAN + 3% exposure; spiked in 2008'),
    ('2014-11-01',25,'8,000-8,800',20_000,28_000,'Reduced to 25 to keep contract values near 2 lakh'),
    ('2015-11-01',75,'7,500-15,500',45_000,110_000,'SEBI minimum contract 5 lakh; peak-margin phase-in from Dec 2020; spiked Mar 2020'),
    ('2021-07-01',50,'15,500-22,500',85_000,115_000,'100% upfront peak margin from Sep 2021; no intraday leverage'),
    ('2024-04-26',25,'22,000-26,000',65_000,80_000,'Lot halved to keep 5-10 lakh notional'),
    ('2024-11-20',75,'23,500-26,000',210_000,250_000,'SEBI minimum contract 15-20 lakh; +2% ELM on expiry days'),
    ('2026-01-01',65,'24,000+',175_000,200_000,'Adjusted down in periodic review'),
)
_STARTS=[r[0] for r in MARGIN_TABLE]

def margin_row(day):
    """The table row in force on day (ISO date)."""
    i=bisect.bisect_right(_STARTS,str(day)[:10])-1
    if i<0:raise ValueError(f'{day} is before the margin table ({_STARTS[0]})')
    return MARGIN_TABLE[i]

def margin_per_unit(day):
    """Upper-end overnight margin per unit of NIFTY on day: margin per lot (high) / the period's lot."""
    r=margin_row(day);return r[4]/r[1]

def margin_per_lot(day,lot):return margin_per_unit(day)*lot

def max_lots(day,lot,budget):
    """Whole lots whose upper-end margin fits into budget on day (at least 1, so a path always exists)."""
    return max(1,int(math.floor(budget/margin_per_lot(day,lot)+1e-9)))

TARGET_FRACTION=.40   # V4.9.7 (user): one strategy trades 40% of the account's maximum lots, rounded up (a trader runs several)
def entry_lots(day,lot,budget,fraction=TARGET_FRACTION):
    """Lots of one entry: ceil(fraction x max_lots(day, lot, budget)), at least 1. fraction 1 = the whole budget (V4.9.6)."""
    return max(1,int(math.ceil(fraction*max_lots(day,lot,budget)-1e-9)))

# ---- strikes ----
def distance_step(index):
    return 50 if index<5000 else 100 if index<12000 else 200

def atm(index):
    """ATM strike: nearest multiple of 50 below 5,000 (where the step is 50), else of 100 (as before)."""
    g=50 if index<5000 else 100;return round(index/g)*g

def strike_for(side,index,level):
    """Short strike for a moneyness level (-5..5; > 0 OTM, < 0 ITM): bull sells a put at ATM - level x step, bear a call at ATM + level x step."""
    d=level*distance_step(index);return float(atm(index)-d if side=='bull' else atm(index)+d)

LEVELS=tuple(range(-5,6))

# ---- expiry policies ----
NAKED_STRUCTURES=('naked','naked_m1','naked_m2')
def is_naked(structure):return structure in NAKED_STRUCTURES
def is_monthly(expiry):
    d=dt.date.fromisoformat(expiry);return (d+dt.timedelta(days=7)).month!=d.month
def days_between(a,b):return (dt.date.fromisoformat(b)-dt.date.fromisoformat(a)).days

# ---- multi-leg structures (V4.9.14, user 2026-11-24) ----
# Legs in the signal's direction: 'own' = the option type a naked trade on this side sells (PE for bull, CE for bear) at a strike
# `level` steps OTM on the protective side; 'opp' = the other type, `level` steps OTM on the profit side; 'FUT' = the monthly future
# (spot x (1 + carry x days to expiry / 365); no futures data in the extraction), long for bull, short for bear.
# A leg: (kind, sign relative to the side's direction for FUT / long +1 short -1 for options, level offset added to the path's level, ratio).
# margin share: margin as a share of the naked margin (user: 2-leg capped loss 60%, 3 or 4 legs 110%; covered / long options 100%);
# lots factor: entry lots = floor(naked lots x factor) (user: 1.6x, 0.9x; 1.0x covered, long options).
COMBO_LEVELS=(0,1,2,3)                                      # user: the sold / hedge leg at ATM and 1-3 levels OTM; widths 1-2
def _combos():
    m1='naked_m1';out={}
    for w in (1,2):
        out[f'vert_w{w}']=dict(legs=(('own',-1,0,1),('own',1,w,1)),expiry='naked',share=.6,factor=1.6,cls='credit',sides=('bull','bear'),
                               desc=f'bull put / bear call spread, long leg {w} level(s) further OTM')
        out[f'collar_w{w}']=dict(legs=(('FUT',1,0,1),('own',1,0,1),('opp',-1,w-1,1)),expiry=m1,share=1.1,factor=.9,cls='combo',sides=('bull','bear'),
                                 desc=f'collar: future + protective option + short option {w-1} level(s) further OTM on the profit side')
        out[f'backspread_w{w}']=dict(legs=(('opp',-1,0,1),('opp',1,w,2)),expiry=m1,share=1.1,factor=.9,cls='combo',sides=('bull',),
                                     desc=f'call ratio backspread: short 1, long 2 calls {w} level(s) further OTM (bull only, user list)')
    out['married']=dict(legs=(('FUT',1,0,1),('own',1,0,1)),expiry=m1,share=.6,factor=1.6,cls='combo',sides=('bull','bear'),
                        desc='married put (bull) / protective call (bear): future + long protective option')
    out['covered']=dict(legs=(('FUT',1,0,1),('opp',-1,0,1)),expiry=m1,share=1.,factor=1.,cls='combo',sides=('bull','bear'),
                        desc='covered call (bull) / covered put (bear): future + short option on the profit side')
    out['longopt']=dict(legs=(('opp',1,0,1),),expiry=m1,share=1.,factor=1.,cls='debit',sides=('bull','bear'),
                        desc='futures replacement: long call (bull) / long put (bear)')
    return out
COMBOS=_combos();COMBO_STRUCTURES=tuple(COMBOS)
def is_combo(structure):return structure in COMBOS
def structure_class(structure):
    """naked | calendar | futures | credit | debit | combo (premium-based rules exist for naked, calendar, credit, debit)."""
    if is_naked(structure):return 'naked'
    if structure=='calendar':return 'calendar'
    if structure.startswith('futures'):return 'futures'
    return COMBOS[structure]['cls']
def expires(structure):
    """Whether the position ends at an option / futures expiry (exp rules apply)."""
    return is_naked(structure) or is_combo(structure) or structure=='futures_expiry'
def margin_share(structure):return COMBOS[structure]['share'] if is_combo(structure) else 1.
def lots_factor(structure):return COMBOS[structure]['factor'] if is_combo(structure) else 1.
def combo_legs(structure,side,index,level,points=False):
    """[(cp or 'FUT', strike (0 for the future), signed ratio)] of a combo for a path at `level` (levels mode: strike_for steps;
    points mode: `level` points, widths x 100 points, as the --offset-mode points ladder)."""
    d=1 if side=='bull' else -1;opp='bear' if side=='bull' else 'bull';own_cp='PE' if side=='bull' else 'CE';opp_cp='CE' if side=='bull' else 'PE'
    def strike(s,lv):
        if not points:return strike_for(s,index,lv)
        a=float(round(index/100)*100);return a-lv if s=='bull' else a+lv
    out=[]
    for kind,sg,dl,ratio in COMBOS[structure]['legs']:
        if kind=='FUT':out.append(('FUT',0.,d*sg*ratio));continue
        lv=level+(dl*100 if points else dl)
        out.append((own_cp,strike(side,lv),sg*ratio) if kind=='own' else (opp_cp,strike(opp,lv),sg*ratio))
    return out

def select_short_expiry(structure,fill_day,expiries,min_short_days=4,m1_min_days=21):
    """(expiry, None) or (None, reason) for a naked short under its expiry policy; expiries = listed expiries (sorted ISO)."""
    if is_combo(structure):structure=COMBOS[structure]['expiry']                    # user: verticals weekly; futures combos current monthly >= 21 days
    exps=[e for e in sorted(expiries) if e>=fill_day]
    if structure in ('naked','calendar','futures_expiry'):
        e=next((e for e in exps if days_between(fill_day,e)>=min_short_days),None);return (e,None) if e else (None,'no_short_expiry')
    month=fill_day[:7]
    if structure=='naked_m1':
        m=[e for e in exps if e[:7]==month and is_monthly(e)]
        if not m:return None,'no_short_expiry'
        return (m[-1],None) if days_between(fill_day,m[-1])>=m1_min_days else (None,'policy_ineligible')
    if structure=='naked_m2':
        d=dt.date.fromisoformat(fill_day);nxt=(d.replace(day=1)+dt.timedelta(days=32)).strftime('%Y-%m')
        m=[e for e in exps if e[:7]==nxt and is_monthly(e)]
        return (m[-1],None) if m else (None,'no_short_expiry')
    raise ValueError(f'unknown structure {structure}')
