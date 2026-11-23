"""Exact comparison of two grid folders (e.g. built by V4.9.12 and V4.9.13 from the same paths file with the same flags).

    python scripts/compare_grid_dirs.py runs/grid_old runs/grid_new

Every grid_*.npz must exist in both and every array must be equal element for element (NaN equal to NaN); no tolerance.
Exit code 0 only if everything matches."""
import sys
from pathlib import Path
import numpy as np


def main(a,b):
    a,b=Path(a),Path(b);fa={f.name for f in a.glob('grid_*.npz')};fb={f.name for f in b.glob('grid_*.npz')}
    bad=0
    for name in sorted(fa^fb):print('only in',a if name in fa else b,':',name);bad+=1
    for i,name in enumerate(sorted(fa&fb),1):
        with np.load(a/name) as x,np.load(b/name) as y:
            if set(x.files)!=set(y.files):print(name,': different arrays',sorted(set(x.files)^set(y.files)));bad+=1;continue
            for k in x.files:
                u,v=x[k],y[k];nan=u.dtype.kind=='f'
                if u.dtype!=v.dtype or u.shape!=v.shape or not np.array_equal(u,v,equal_nan=nan):
                    diff=int((u!=v).sum()) if u.shape==v.shape else 'shape'
                    print(f'{name}: {k} differs ({u.dtype}{u.shape} vs {v.dtype}{v.shape}, cells differing: {diff})');bad+=1
        if i%50==0:print(f'{i}/{len(fa&fb)} files checked',flush=True)
    print(f'{len(fa&fb)} files compared, {bad} problems: '+('IDENTICAL' if not bad else 'NOT IDENTICAL'))
    return 1 if bad else 0


if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit(__doc__)
    raise SystemExit(main(*sys.argv[1:]))
