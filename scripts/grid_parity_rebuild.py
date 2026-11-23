"""Rebuild a grid folder with the current code from the same inputs as an existing one, then compare the two exactly.

    python scripts/grid_parity_rebuild.py --old runs/zone_grid_levels --holidays config/nse_holidays.txt --out runs/grid_v4913

Steps (each failing check stops the run before any computing):
  1. reads the old grid's grid_index.json: paths file, start, split, gap policy, holidays, structures, layered policies;
  2. finds the paths file (as recorded, else next to the old grid folder, else --paths) and checks its SHA-256 equals the
     one the old grid was built from;
  3. checks the --holidays file gives exactly the holiday dates recorded in the old grid;
  4. reports how long the old grid took (from its files' timestamps: approximate, excludes the first group);
  5. builds the new grid (resumable: rerun the same command after an interruption; finished files are verified and kept);
  6. compares every grid_*.npz array element for element (NaN = NaN, dtype and shape included, no tolerance) and every
     sidecar's group statistics (all fields except the code / file hashes, which must differ);
  7. writes <out>/parity_report.txt; exit code 0 only if everything is identical.
"""
import argparse,datetime as dt,json,sys,time
from pathlib import Path
import numpy as np
from nifty_iv.zone import grid as zg
from nifty_iv.zone import rules as zr

HASH_FIELDS={'rules_sha256','file_sha256'}
# grid_index rules_sha256 of the V4.9.12 grid code (commits c9126db .. c987301 give the same value), gap policy 'busy',
# config/nse_holidays.txt as of V4.9.12 (26accdc9692d90b4... = sha256 of its 89 dates), by numpy version (the digest includes it)
V4912_DIGEST={('2.5.3','busy','26accdc9692d90b4'):'97b18b5305243aab6301db9d37477c1fdf8af3263dfa9a5625d09902e42f6911',
              ('2.5.4','busy','26accdc9692d90b4'):'fd47b695853aca6003214d991ba037b5c5b56dd3575955686688469fd684f33e'}


def say(msg,log):
    print(msg,flush=True);log.append(msg)


def find_paths(recorded,old,given):
    for c in ([Path(given)] if given else [])+[Path(recorded),old.parent/recorded,old.parent/Path(recorded).name]:
        if c.is_file():return c.resolve()
    raise SystemExit(f'paths file not found: recorded as {recorded!r}; tried it relative to here and to {old.parent}. Pass --paths.')


def old_duration(old):
    t=sorted(f.stat().st_mtime for f in old.glob('grid_*.npz'))
    if len(t)<2:return 'unknown'
    f=lambda s:dt.datetime.fromtimestamp(s).strftime('%Y-%m-%d %H:%M')
    return f'{(t[-1]-t[0])/3600:.2f} h between its first and last file ({f(t[0])} .. {f(t[-1])}); a resumed or interrupted run makes this unreliable'


def compare(old,new,log):
    fa={f.name for f in old.glob('grid_*.npz')};fb={f.name for f in new.glob('grid_*.npz')};bad=0;cells=0
    for name in sorted(fa^fb):say(f'  only in {old if name in fa else new}: {name}',log);bad+=1
    for name in sorted(fa&fb):
        with np.load(old/name) as x,np.load(new/name) as y:
            if set(x.files)!=set(y.files):say(f'  {name}: arrays {sorted(set(x.files)^set(y.files))} missing on one side',log);bad+=1;continue
            for k in x.files:
                u,v=x[k],y[k];cells+=u.size
                if u.dtype!=v.dtype or u.shape!=v.shape or not np.array_equal(u,v,equal_nan=u.dtype.kind=='f'):
                    n=int((u!=v).sum()) if u.shape==v.shape else 'shape'
                    say(f'  {name}: {k} differs ({u.dtype}{u.shape} vs {v.dtype}{v.shape}; cells differing: {n})',log);bad+=1
        sa,sb=(json.loads(Path(str(d/name)+'.json').read_text()) for d in (old,new))
        diff=sorted(k for k in set(sa)|set(sb) if k not in HASH_FIELDS and sa.get(k)!=sb.get(k))
        if diff:say(f'  {name}: sidecar fields differ: '+', '.join(f'{k} {sa.get(k)!r} vs {sb.get(k)!r}' for k in diff),log);bad+=1
    return len(fa&fb),cells,bad


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--old',required=True,help='existing grid folder (contains grid_index.json)')
    ap.add_argument('--out',required=True,help='new grid folder (rerun the same command to resume)')
    ap.add_argument('--holidays',help='holiday file; required when the old grid recorded holidays, and must give the same dates')
    ap.add_argument('--paths',help='paths file, if not found where the old grid recorded it')
    ap.add_argument('--workers',type=int,default=4,help='parallel group workers (default 4; results do not depend on it)')
    ap.add_argument('--compare-only',action='store_true',help='skip building; just compare --old and --out')
    a=ap.parse_args(argv);log=[];old=Path(a.old);out=Path(a.out)
    say('command: '+' '.join([Path(sys.argv[0]).name]+[f'--{k.replace("_","-")} {v}' for k,v in vars(a).items() if v not in (None,False)]),log)
    x=json.loads((old/'grid_index.json').read_text())
    structs=sorted({g['structure'] for g in x['groups']});compound=any(g['kind'].startswith('compound') for g in x['groups'])
    say(f"old grid {old}: {len(x['groups'])} files, structures {','.join(structs)}, layered policies {compound}, start {x['start']}, split {x['split']}, gap policy {x.get('gap_policy')}, {len(x.get('holidays') or [])} holidays",log)
    paths=find_paths(x['paths_db'],old,a.paths)
    say(f'paths file {paths}: checking SHA-256 (about a minute for 1 GB) ...',log)
    if zg.digest(paths)!=x['paths_sha256']:raise SystemExit('STOP: the paths file is not the one the old grid was built from (SHA-256 differs); a comparison would be meaningless')
    say('  matches the old grid',log)
    rec=sorted(x.get('holidays') or [])
    if rec and not a.holidays:raise SystemExit(f'STOP: the old grid used {len(rec)} holidays; pass --holidays')
    hol=zr.read_holidays(a.holidays) if a.holidays else []
    if sorted(hol)!=rec:
        raise SystemExit(f'STOP: {a.holidays} gives {len(hol)} dates, the old grid recorded {len(rec)}; only in file: {sorted(set(hol)-set(rec))[:10]}, only in grid: {sorted(set(rec)-set(hol))[:10]}')
    say(f'holidays: {len(hol)} dates, identical to the old grid',log)
    import hashlib;key=(np.__version__,x.get('gap_policy','busy'),hashlib.sha256('|'.join(rec).encode()).hexdigest()[:16]);want=V4912_DIGEST.get(key)
    if want is None:say(f'WARNING: no reference digest for numpy {key[0]} / this holiday list: cannot confirm the old grid came from V4.9.12 code',log)
    elif x['rules_sha256']!=want:raise SystemExit('STOP: the old grid was not built by the V4.9.12 grid code under this numpy (its rules_sha256 differs), so differences would not isolate the V4.9.13 changes')
    else:say(f'old grid was built by the V4.9.12 grid code (rules_sha256 {want[:12]}..., numpy {key[0]})',log)
    say(f'old grid build time: {old_duration(old)}',log)
    if not a.compare_only:
        say(f'building {out} with the current code (workers {a.workers}) ...',log);t0=time.time()
        zg.run_grid(str(paths),out,x['start'],x['split'],structs,progress=True,resume=True,workers=a.workers,compound=compound,holidays=hol,gap_policy=x.get('gap_policy','busy'))
        say(f'new grid build time: {(time.time()-t0)/3600:.2f} h',log)
    say('comparing ...',log);n,cells,bad=compare(old,out,log)
    verdict='IDENTICAL' if not bad and n==len(x['groups']) else 'NOT IDENTICAL'
    say(f'{n} files, {cells:,} cells compared, {bad} problems: {verdict}',log)
    out.mkdir(parents=True,exist_ok=True);(out/'parity_report.txt').write_text('\n'.join(log)+'\n')
    print(f'report: {out/"parity_report.txt"}')
    return 0 if verdict=='IDENTICAL' else 1


if __name__=='__main__':raise SystemExit(main())
