"""Which market_data rows the zone research reads, in one place (V4.9.18).

OPTION_DATASETS  import_files.dataset values whose option_bars the zone extraction (nifty-zone-contracts) reads.
SPOT_FILE        the canonical NIFTY spot file (2021-05 onwards, india-index-options-1m).
SPOT_DATASETS    datasets whose spot_bars are NIFTY spot as a whole (nifty_2017_2020: the vendor NIFTY.csv files;
                 their NIFTY_F1 rows are in future_bars, not spot_bars). Where two sources share a minute the
                 canonical file wins (spot_priority); the 2017-2020 block and the canonical file do not overlap.

Option blackouts (config/option_blackouts.csv; from,to,reason): a window in which no option position may be held. Every
option row of a contract still alive on `from` (expiry >= from) is hidden for trading days up to `to`, so no option
position can be opened before the window and carried into it, nothing trades inside it, and contracts expiring before
`from` are untouched. The first window (January 2018: the vendor's January monthly file is missing; user rule: every
option position closed by the last December session, option strategies restart in February) is the only one so far."""
from __future__ import annotations
import csv,datetime as dt
from pathlib import Path
import nifty_iv

OPTION_DATASETS=('rissin_upstox','thetrademarkk','nifty_2017_2020')
SPOT_FILE='india-index-options-1m/index/NIFTY.parquet'
SPOT_DATASETS=('nifty_2017_2020',)
BLACKOUTS=nifty_iv.REPO_ROOT/'config'/'option_blackouts.csv'

def sql_list(names):
    """'a','b' for constant identifiers of this module (never user input)."""
    if any("'" in n for n in names):raise ValueError(names)
    return ','.join(f"'{n}'" for n in names)

OPTION_WHERE=f"f.dataset IN ({sql_list(OPTION_DATASETS)})"
SPOT_WHERE=f"(f.relative_path='{SPOT_FILE}' OR f.dataset IN ({sql_list(SPOT_DATASETS)}))"
SPOT_PRIORITY=f"CASE WHEN f.relative_path='{SPOT_FILE}' THEN 0 ELSE 1 END"

def read_blackouts(path=BLACKOUTS):
    """[(from, to, reason)] as ISO dates; [] for path None or 'none'. Lines starting with # are comments."""
    if path is None or str(path).lower()=='none':return []
    out=[]
    with open(path,newline='') as fh:
        for r in csv.DictReader(l for l in fh if l.strip() and not l.lstrip().startswith('#')):
            a=dt.date.fromisoformat(r['from'].strip());b=dt.date.fromisoformat(r['to'].strip())
            if b<a:raise SystemExit(f'{path}: option blackout ends before it starts: {r}')
            out.append((a.isoformat(),b.isoformat(),(r.get('reason') or '').strip()))
    return sorted(out)

def blackout_sql(windows,alias='b'):
    """(SQL fragment, params) that hides the option rows of every window: AND NOT (trading_day <= to AND expiry >= from)."""
    sql=''.join(f' AND NOT ({alias}.trading_day<=%s::date AND {alias}.expiry>=%s::date)' for _ in windows)
    return sql,[x for f,t,_ in windows for x in (t,f)]

def blackouts_in(windows,from_date):
    """The windows that can touch a run whose option data starts on from_date (rows up to each window's end are affected)."""
    return [w for w in windows if from_date<=w[1]]

def hidden(windows,day,expiry):
    """Whether an option row (trading day, expiry; ISO) is hidden by a window (the Python twin of blackout_sql, for tests)."""
    return any(day<=t and expiry>=f for f,t,_ in windows)
