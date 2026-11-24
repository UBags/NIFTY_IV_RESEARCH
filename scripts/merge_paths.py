"""Merge two paths files built from the same spot data: the structures in --add replace those structures in --base.

    python scripts/merge_paths.py --base runs/zone_paths_levels.sqlite --add runs/zone_paths_combos.sqlite --out runs/zone_paths_v4914.sqlite

Checks before writing: identical bars and signals tables, and the same sizing arguments (target margin, target fraction,
participation, entry window, lots) in both files' meta args; otherwise stops. The base file is copied, the --add structures' paths
and gaps are deleted from the copy and the --add rows inserted. --base and --add are not modified. The merged meta records both
argument sets. Grid folders and scans must be rebuilt on the merged file (its SHA-256 differs)."""
import argparse,json,shutil,sqlite3,sys
from pathlib import Path

SIZING=('target_margin','target_fraction','target_units','lots','max_participation','entry_minutes','entry_bars','offset_mode','slippage','brokerage','fill_price')


def args_of(db):
    r=db.execute("SELECT value FROM meta WHERE name='args'").fetchone();return json.loads(r[0]) if r else {}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base',required=True);ap.add_argument('--add',required=True);ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);out=Path(a.out)
    if out.exists():sys.exit(f'{out} exists: choose a new --out')
    b=sqlite3.connect(f'file:{a.base}?mode=ro',uri=True);d=sqlite3.connect(f'file:{a.add}?mode=ro',uri=True)
    for t,order in (('bars','idx'),('signals','config,side,bar')):
        if b.execute(f'SELECT * FROM {t} ORDER BY {order}').fetchall()!=d.execute(f'SELECT * FROM {t} ORDER BY {order}').fetchall():
            sys.exit(f'STOP: the {t} tables differ: the files were not built from the same spot data / signals')
    ab,ad=args_of(b),args_of(d);norm=lambda k,v:(v or 0) if k=='entry_minutes' else v              # no limit: absent (before V4.9.12), None or 0
    diff={k:(ab.get(k),ad.get(k)) for k in SIZING if norm(k,ab.get(k))!=norm(k,ad.get(k))}
    if diff:sys.exit(f'STOP: sizing arguments differ (base, add): {diff}')
    add=[r[0] for r in d.execute('SELECT DISTINCT structure FROM paths ORDER BY 1')]
    if not add:sys.exit('STOP: --add has no paths')
    b.close();shutil.copyfile(a.base,out);m=sqlite3.connect(out)
    q=','.join('?'*len(add));old=m.execute(f'SELECT COUNT(*) FROM paths WHERE structure IN ({q})',add).fetchone()[0]
    m.execute(f'DELETE FROM paths WHERE structure IN ({q})',add);m.execute(f'DELETE FROM gaps WHERE structure IN ({q})',add)
    cols=[r[1] for r in d.execute('PRAGMA table_info(paths)') if r[1]!='id'];gcols=[r[1] for r in d.execute('PRAGMA table_info(gaps)')]
    n=0
    for row in d.execute(f'SELECT {",".join(cols)} FROM paths'):m.execute(f'INSERT INTO paths({",".join(cols)}) VALUES({",".join("?"*len(cols))})',row);n+=1
    g=0
    for row in d.execute(f'SELECT {",".join(gcols)} FROM gaps'):m.execute(f'INSERT INTO gaps({",".join(gcols)}) VALUES({",".join("?"*len(gcols))})',row);g+=1
    merged=dict(ab,structures=','.join(sorted({*str(ab.get('structures','')).split(','),*add}-{''})),merged_from=dict(base=str(a.base),add=str(a.add),add_args=ad))
    m.execute("INSERT OR REPLACE INTO meta VALUES('args',?)",(json.dumps(merged),));m.commit()
    total=m.execute('SELECT structure,COUNT(*) FROM paths GROUP BY 1 ORDER BY 1').fetchall();m.close();d.close()
    print(f'{out}: replaced {old} base paths of {",".join(add)} with {n} paths ({g} gap rows); now '+', '.join(f'{s} {c}' for s,c in total))


if __name__=='__main__':main()
