"""NIFTY historical lot-size rule estimates, from NSE circulars.

Contract-master CSV overrides are authoritative. Rules flag ambiguous cases instead
of silently inventing exact contracts. Margin requirements are NOT inferred here.
"""
from __future__ import annotations
import csv
import datetime as dt
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache

SOURCES={
 '2021':'NSE/FAOP/47854',
 '2024a':'NSE/FAOP/61415',
 '2024b':'NSE/FAOP/64625',
 '2025':'NSE/FAOP/70616',
}

@dataclass(frozen=True)
class ContractSize:
    qty: int | None
    source: str
    reliability: str


def monthly_like(expiry):
    d=dt.date.fromisoformat(str(expiry))
    return (d+dt.timedelta(days=7)).month!=d.month


@lru_cache(50000)
def rule_lot_size(trading_day,expiry):
    d=dt.date.fromisoformat(str(trading_day)); ex=dt.date.fromisoformat(str(expiry))
    if ex<d:return ContractSize(None,'expired','invalid')
    if d<dt.date(2021,7,1):
        if ex<=dt.date(2021,6,30):return ContractSize(75,SOURCES['2021'],'circular_inferred')
        # July and >3-month transition depended on contract cycle and effective date.
        return ContractSize(None,SOURCES['2021'],'transition_ambiguous')
    if d<dt.date(2021,8,1) and not monthly_like(ex):
        return ContractSize(None,SOURCES['2021'],'transition_ambiguous')
    if d<dt.date(2024,4,26):return ContractSize(50,SOURCES['2021'],'circular_inferred')
    if d<dt.date(2024,11,20):return ContractSize(25,SOURCES['2024a'],'circular_inferred')
    # Late-2024 transition: existing weeklies and Jan25 monthly retain 25,
    # newly introduced weeklies Jan25+ and monthly Feb25+ take 75.
    if d<dt.date(2024,12,27) and ex>=dt.date(2025,3,1) and ex.month in (3,6,9,12):
        return ContractSize(None,SOURCES['2024b'],'quarterly_transition_needs_master')
    if ex<=dt.date(2024,12,31):return ContractSize(25,SOURCES['2024b'],'circular_inferred')
    if ex==dt.date(2025,1,30):return ContractSize(25,SOURCES['2024b'],'circular_inferred')
    if ex.year==2025 and ex.month==1:
        return ContractSize(75,SOURCES['2024b'],'circular_inferred')
    # 2025 change is by EXPIRY, not just trading day. Newly listed weekly
    # Jan-06-2026+ and monthly Jan-27-2026+ carry 65 even in Dec 2025.
    # Existing March-2026 quarterlies were 75 through Dec-30 EOD.
    if d<dt.date(2025,12,31) and ex.month in (3,6,9,12) and ex>=dt.date(2026,3,1):
        return ContractSize(75,SOURCES['2025'],'circular_inferred')
    if ex<=dt.date(2025,12,30):return ContractSize(75,SOURCES['2025'],'circular_inferred')
    if ex.year==2026 and ex.month==1:
        if ex==dt.date(2026,1,27) or ex>=dt.date(2026,1,6) and not monthly_like(ex):
            return ContractSize(65,SOURCES['2025'],'circular_inferred')
        return ContractSize(None,SOURCES['2025'],'transition_ambiguous')
    if d<dt.date(2025,12,31) and ex.year==2026 and ex.month>=2 and monthly_like(ex) and ex.month not in (3,6,9,12):
        return ContractSize(65,SOURCES['2025'],'circular_inferred')
    return ContractSize(65,SOURCES['2025'],'circular_inferred')


class LotMaster:
    """Optional authoritative per-trading-day/expiry overrides exported from NSE contract master.
    Expected CSV: trading_day,expiry,lot_size,source. Empty trading_day = all days.
    """
    def __init__(self,path=None):
        self.exact={};self.default={}
        if path:
            with Path(path).open(newline='') as file:
                for row in csv.DictReader(file):
                    d=row['trading_day'].strip();e=row['expiry'].strip();q=int(row['lot_size'])
                    if q<1:raise ValueError('nonpositive lot size')
                    if d:self.exact[d,e]=ContractSize(q,row.get('source') or 'contract_master','authoritative')
                    else:self.default[e]=ContractSize(q,row.get('source') or 'contract_master','authoritative')
    def get(self,day,expiry):
        return self.exact.get((day,expiry)) or self.default.get(expiry) or rule_lot_size(day,expiry)
