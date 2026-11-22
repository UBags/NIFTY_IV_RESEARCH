"""Detects drift between a ranking report and the inputs that produced it.

Hashes catch accidental change; they are not tamper-proof without a protected signing key.
"""
from __future__ import annotations
import hashlib,json,platform,sqlite3
from contextlib import closing
from pathlib import Path

RELEASE='4.9.0'
TABLES={'trades':'id','marks':'trade_id,day','fills':'trade_id,seq','diagnostics':'reason','meta':'name'}

def table_digest(db,table,order):
    h=hashlib.sha256()
    for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def trades_digest(path):
    """Digest of every table in a trade database, including marks, fills and run config."""
    h=hashlib.sha256()
    with closing(sqlite3.connect(path)) as db:
        present={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,order in TABLES.items():
            if table in present:h.update(table.encode());h.update(table_digest(db,table,order).encode())
    return h.hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None


def code_digest(root=None):
    root=Path(root or Path(__file__).resolve().parents[1]);h=hashlib.sha256()   # the nifty_iv package
    for p in sorted([*root.rglob('*.py'),*root.rglob('migrations/*.sql'),*root.glob('VERSION.txt')]):
        h.update(p.relative_to(root).as_posix().encode());h.update(p.read_bytes())
    return h.hexdigest()


def report_digest(payload):
    body={k:v for k,v in payload.items() if k!='lineage'}
    return hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()


def environment():
    import numpy,scipy
    return dict(python=platform.python_version(),numpy=numpy.__version__,scipy=scipy.__version__)


def build(trades_path,trade_meta,args):
    body=dict(release=RELEASE,code_sha256=code_digest(),trades_sha256=trades_digest(trades_path),
        source_fingerprint=trade_meta.get('source_fingerprint'),enrichment_run_id=trade_meta.get('enrichment_run_id') or '',
        market_content_digest=trade_meta.get('market_content_digest') or '',contract_master_sha256=trade_meta.get('contract_master_sha256') or '',special_sessions_sha256=trade_meta.get('special_sessions_sha256') or '',holiday_calendar_sha256=trade_meta.get('holiday_calendar_sha256') or '',tariff_version=trade_meta.get('tariff_version') or '',settlement_master_sha256=trade_meta.get('settlement_master_sha256') or '',
        ranking_args={k:v for k,v in sorted(args.items()) if k not in ('out','trades','cache')},environment=environment())
    body['run_id']=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
    return body


def check_inputs(cache_meta,trade_meta):
    """The trade file must come from this cache and this enrichment run."""
    if cache_meta.get('research_eligible')!='1':raise ValueError('Cache is not research eligible')
    if cache_meta.get('enrichment_complete')!='1':raise ValueError('Cache enrichment is incomplete')
    for name in ('source_fingerprint','enrichment_run_id','market_content_digest'):
        if (cache_meta.get(name) or '')!=(trade_meta.get(name) or ''):raise ValueError(f'Trades and cache differ on {name}')


def verify(lineage,cache_meta,trades_path,trade_meta,payload=None,config=None,allow_code_change=False):
    """Raises ValueError unless the report still matches these inputs."""
    if not lineage:raise ValueError('Ranking report has no lineage block')
    checks=[('source_fingerprint',cache_meta.get('source_fingerprint')),('enrichment_run_id',cache_meta.get('enrichment_run_id') or ''),
            ('market_content_digest',cache_meta.get('market_content_digest') or ''),('trades_sha256',trades_digest(trades_path))]
    if not allow_code_change:checks.append(('code_sha256',code_digest()))
    for name,actual in checks:
        if lineage.get(name)!=actual:raise ValueError(f'Lineage mismatch on {name}')
    if (trade_meta.get('enrichment_run_id') or '')!=(cache_meta.get('enrichment_run_id') or ''):raise ValueError('Trades were built on a different enrichment run')
    if payload is not None and lineage.get('report_sha256')!=report_digest(payload):raise ValueError('Lineage mismatch on report_sha256')
    for meta_key,config_key in (('contract_master_sha256','contract_master'),('settlement_master_sha256','settlement_master_path'),('special_sessions_sha256','special_sessions'),('event_calendar_sha256','event_calendar'),('holiday_calendar_sha256','holiday_calendar')):
        recorded=trade_meta.get(meta_key) or ''
        path=(config or {}).get(config_key)
        if recorded and (not path or not Path(path).exists() or file_digest(path)!=recorded):raise ValueError(f'Lineage mismatch on {meta_key}')
