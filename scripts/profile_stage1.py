"""Where the wide search's stage-1 time goes on the real paths file: runs stage 1 for a few groups (scratch folder, never the
pipeline's) and prints each phase's seconds, plus how many configurations share a signal list (computed once since V4.9.17).

    python scripts/profile_stage1.py --paths runs/zone_paths_v4914.sqlite --holidays config/nse_holidays.txt \\
        --groups bull:naked:-1,bear:vert_w1:1 [--workers 1]

Phases: load (paths from sqlite), batch (padded matrices), evaluate (rule hits), dedup (identical exit rows), gather (per-rule
exit bars / values), kernel (one position at a time per configuration x rule), score (screening scores, robust median), select
(best rules per configuration). Run it when nothing else is running, or the timings are distorted."""
import argparse,collections,gzip,json,sqlite3,sys,tempfile,time
from pathlib import Path
from nifty_iv.diagnostics import wide_search as ws

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--paths',required=True);ap.add_argument('--holidays');ap.add_argument('--groups',required=True)
    ap.add_argument('--workers',type=int,default=1);a,extra=ap.parse_known_args(argv)
    db=sqlite3.connect(f'file:{a.paths}?mode=ro',uri=True);seqs=collections.defaultdict(set);sig=collections.defaultdict(list)
    for cf,side,t in db.execute('SELECT config,side,bar FROM signals ORDER BY config,side,bar'):sig[cf,side].append(t)
    for (cf,side),v in sig.items():seqs[side].add(tuple(v))
    nconf=len({cf for cf,_ in sig});db.close()
    for side in sorted(seqs):print(f'{side}: {nconf} configurations, {len(seqs[side])} distinct signal lists ({len(seqs[side])/max(nconf,1):.0%})')
    out=Path(tempfile.mkdtemp(prefix='profile_stage1_'));t0=time.time()
    ws.main(['--paths',a.paths]+(['--holidays',a.holidays] if a.holidays else [])+['--groups',a.groups,'--stage1-only','--workers',str(a.workers),'--out',str(out)]+extra)
    for f in sorted((out/'stage1').glob('*.json.gz')):
        r=json.load(gzip.open(f,'rt'))['result']
        if r.get('skipped'):print(f'{f.name}: skipped ({r["skipped"]})');continue
        ph=r.get('phases',{});tot=sum(ph.values()) or 1
        print(f"\n{f.name[:-8]}: {r['seconds']} s, {r['paths']} paths, {r['configs']} configurations ({r.get('distinct_signal_lists')} distinct lists), "
              f"{r['rules']} rules ({r.get('unique_rules')} distinct exit rows), {len(r['rows'])} candidates kept")
        for k,v in sorted(ph.items(),key=lambda x:-x[1]):print(f'  {k:9s} {v:8.2f} s  {100*v/tot:5.1f}%')
    print(f'\n(total {time.time()-t0:.0f} s; scratch folder {out})')

if __name__=='__main__':main()
