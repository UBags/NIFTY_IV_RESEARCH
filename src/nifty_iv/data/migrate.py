#!/usr/bin/env python3
"""Versioned, additive PostgreSQL migrations: research_v3 schema ONLY.

No modifications to the active Parquet importer or market_data objects.
"""
import argparse,hashlib,os
from pathlib import Path
from nifty_iv.core import cli

def main():
    p=cli.Parser(description=__doc__)
    p.add_argument('--dry-run',action='store_true');p.add_argument('--status',action='store_true')
    a=p.parse_args()
    migrations=sorted((Path(__file__).parent/'migrations').glob('*.sql'))
    if a.dry_run:
        for path in migrations:print(path.name,hashlib.sha256(path.read_bytes()).hexdigest())
        return
    import psycopg
    with psycopg.connect(host=os.getenv('PGHOST','localhost'),port=os.getenv('PGPORT','5432'),
        dbname=os.getenv('PGDATABASE','nifty_data'),user=os.getenv('PGUSER','postgres'),
        password=os.getenv('PGPASSWORD'),connect_timeout=15) as con:
        with con.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='research_v3' AND table_name='migration_history')")
            exists=cur.fetchone()[0]
            applied=dict(cur.execute('SELECT version,sha256 FROM research_v3.migration_history').fetchall()) if exists else {}
            if a.status:
                print('Already applied:',applied)
                return
            for path in migrations:
                version=path.stem;sql=path.read_text();checksum=hashlib.sha256(path.read_bytes()).hexdigest()
                if version in applied:
                    if applied[version]!=checksum:raise RuntimeError('Migration checksum changed: '+version)
                    print('Already applied:',version);continue
                cur.execute(sql)
                cur.execute('INSERT INTO research_v3.migration_history(version,sha256) VALUES(%s,%s)',(version,checksum))
                con.commit();print('Applied:',version)

if __name__=='__main__':main()
