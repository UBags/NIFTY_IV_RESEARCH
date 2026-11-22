#!/usr/bin/env python3
"""Temporary, complete synthetic V3 run; NO REAL DATA, NO POSTGRES CONNECTION."""
import sys,subprocess,tempfile,json,sqlite3
from datetime import date
from pathlib import Path
import os
ROOT=Path(__file__).resolve().parents[1];SRC=ROOT/'src'
sys.path[:0]=[str(SRC),str(ROOT)]
from tests.test_v3 import fixture

def invoke(module,*args):
    command=[sys.executable,'-m',f'nifty_iv.ivcal.{module}',*map(str,args)]
    print('>', ' '.join(command),flush=True)
    subprocess.run(command,check=True,cwd=ROOT,env={**os.environ,'PYTHONPATH':os.pathsep.join([str(SRC),os.environ.get('PYTHONPATH','')])})

def main():
    with tempfile.TemporaryDirectory(prefix='nifty_v3_smoke_') as folder:
        import os;os.environ.setdefault('NIFTY_HOLDOUT_LOG',str(Path(folder)/'holdout_log.jsonl'))
        root=Path(folder);cache=root/'cache.sqlite';trades=root/'trades.sqlite';rank=root/'rankings'
        fixture(cache,long=date(2026,6,30),end=date(2026,6,30))
        invoke('enrich_cache','--cache',cache,'--batch-rows',8000)
        invoke('calendar_research','--cache',cache,'--out',trades,'--from-date','2026-01-07',
            '--to-date','2026-04-15','--long-tenors','120','--tenor-tolerance','60',
            '--round-steps','500','--strike-values','0','--policies','weekly,rolling_static',
            '--structures','put','--max-candidates','75','--slippage-mode','flat')
        invoke('audit_quality','--cache',cache,'--trades',trades,'--out',root/'quality')
        invoke('audit_contract_coverage','--cache',cache,'--out',root/'contract_quality')
        invoke('rank_strategies','--cache',cache,'--trades',trades,
            '--research-mode','open','--split','2026-03-16','--holdout-entry-cutoff','none','--embargo-days','0','--folds','2','--min-train','5',
            '--min-holdout','1','--signals','iv_ratio','--lower-ratios','.1',
            '--upper-ratios','3','--out',rank)
        with sqlite3.connect(trades) as db:
            n=db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
            marks=db.execute('SELECT COUNT(*) FROM marks').fetchone()[0]
            fills=db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
        result=json.loads((rank/'rankings.json').read_text())
        assert n>40 and marks>n and fills>n,(n,marks,fills)
        assert result['n_finalists']>=1,result
        assert result['complete_grid_mtm']
        counts=result['search_counts'];assert counts['unique_masks_scored']==result['evaluated_unique_full_mtm_masks']<=counts['unique_masks_encountered'],counts
        spa=result['spa_global']
        assert spa is None or spa.get('days',0)>=spa.get('models',0)>0 or spa.get('days',0)>=20,spa
        print('PASS synthetic only:',n,'trade simulations,',marks,'daily marks,',fills,
            'fills,',result['n_finalists'],'ranked configurations')

if __name__=='__main__':main()
