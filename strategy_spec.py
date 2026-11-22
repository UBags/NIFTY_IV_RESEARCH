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

def select_short_expiry(structure,fill_day,expiries,min_short_days=4,m1_min_days=21):
    """(expiry, None) or (None, reason) for a naked short under its expiry policy; expiries = listed expiries (sorted ISO)."""
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
