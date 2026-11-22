#!/usr/bin/env python3
"""Publish V3 research summaries into research_v3 schema; never touches market_data."""
from contextlib import closing
import argparse,os,json,sqlite3
from pathlib import Path
import lineage

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',required=True);p.add_argument('--provider',required=True)
    p.add_argument('--cache',default='research_cache_v4.sqlite')
    p.add_argument('--trades',default='research_trades_v4.sqlite')
    p.add_argument('--rankings',default='strategy_rankings_v4/rankings.json')
    p.add_argument('--quality',default=None,help='Optional coverage_v3/quality_summary.json')
    p.add_argument('--allow-code-change',action='store_true',help='Publish although the code differs from the ranking run; recorded nowhere else, so use sparingly')
    a=p.parse_args()
    ranks=json.loads(Path(a.rankings).read_text())
    if ranks.get('engine') not in ('V3','V4'):raise SystemExit('Not a V3/V4 ranking report')
    if ranks.get('research_mode')=='train_only':raise SystemExit('Refusing to publish a sealed train_only report: it holds no holdout result')
    with closing(sqlite3.connect(a.cache)) as cache:
        cm=dict(cache.execute('SELECT name,value FROM meta'))
    with closing(sqlite3.connect(a.trades)) as db:
        tm=dict(db.execute('SELECT name,value FROM meta'))
        counts=dict(db.execute('SELECT status,COUNT(*) FROM trades GROUP BY status'))
    if cm.get('version')!='3' or tm.get('complete')!='1' or cm['source_fingerprint']!=tm['source_fingerprint']:
        raise SystemExit('Cache and results are not eligible or provenance mismatch')
    try:lineage.verify(ranks.get('lineage'),cm,a.trades,tm,payload=ranks,config=json.loads(tm['config']),allow_code_change=a.allow_code_change)
    except ValueError as e:raise SystemExit(f'Refusing to publish: {e}')
    run_id=ranks['lineage']['run_id']
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(host=os.getenv('PGHOST','localhost'),port=os.getenv('PGPORT','5432'),
        dbname=os.getenv('PGDATABASE','nifty_data'),user=os.getenv('PGUSER','postgres'),
        password=os.getenv('PGPASSWORD'),connect_timeout=15) as conn:
        with conn.cursor() as c:
            c.execute("SELECT experiment_id FROM research_v3.experiments WHERE configuration->'lineage'->>'run_id'=%s",(run_id,))
            prior=c.fetchone()
            if prior:
                print('Already published as experiment_id',prior[0]);return
            c.execute('''INSERT INTO research_v3.experiments(name,engine_version,provider,
                source_fingerprint,configuration,status,finished_at)
                VALUES (%s,%s,%s,%s,%s,'completed',now()) RETURNING experiment_id''',
                (a.name,ranks['engine'].lstrip('V'),a.provider,cm['source_fingerprint'],Jsonb(dict(
                    lineage=ranks['lineage'],quality_sha256=lineage.file_digest(a.quality),trade_config=json.loads(tm['config']),ranking=dict(split=ranks['split'],
                    embargo_days=ranks['embargo_days'],folds=ranks['folds']),
                    status_counts=counts))))
            exp=c.fetchone()[0]
            for i,row in enumerate(ranks['results'],1):
                c.execute('''INSERT INTO research_v3.strategy_rankings
                    (experiment_id,rank,strategy,train,holdout,evidence_flags)
                    VALUES (%s,%s,%s,%s,%s,%s)''',(exp,i,Jsonb(dict(strategy=row['strategy'],
                    signal=row['signal'],lower=row['lower'],upper=row['upper'],score=row['score'])),
                    Jsonb(row['training']),Jsonb(row['holdout']),Jsonb(dict(
                    warning=ranks['warnings'],unfiltered_train_baseline=row.get('unfiltered_train_baseline'),
                    unfiltered_holdout_baseline=row.get('unfiltered_holdout_baseline'),
                    dsr_diagnostic=row.get('deflated_sharpe_diagnostic'),
                    bootstrap=row.get('bootstrap'),spa_within_group=row.get('spa_within_group'),
                    spa_global=ranks.get('spa_global')))))
            if a.quality:
                for item in json.loads(Path(a.quality).read_text()).get('quality_by_provider_month',[]):
                    month=item['month']+'-01'
                    c.execute('''INSERT INTO research_v3.quality_summary
                        (experiment_id,provider,calendar_month,observations)
                        VALUES(%s,%s,%s,%s) ON CONFLICT (experiment_id,provider,calendar_month) DO UPDATE
                        SET observations=excluded.observations''',
                        (exp,item['provider'],month,Jsonb(item)))
        conn.commit()
    print('Published experiment_id',exp,'into research_v3. No market_data tables changed.')

if __name__=='__main__':main()
