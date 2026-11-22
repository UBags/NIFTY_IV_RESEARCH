"""NIFTY_IV_RESEARCH: zone + swing option-selling research (active) and relative-IV calendar research (dormant).

Subpackages: core (shared specs, contract sizes, lineage), data (PostgreSQL import and research caches), zone (the zone
pipeline), diagnostics (studies on a paths file), ivcal (relative-IV calendar pipeline; its simulator's fill and cost
functions are also used by zone.paths)."""
import os as _os
from pathlib import Path as _Path

__version__=(_Path(__file__).with_name('VERSION.txt')).read_text().strip()
REPO_ROOT=_Path(__file__).resolve().parents[2]          # src/nifty_iv -> repository root (source checkout or editable install)
DATA_ROOT=_Path(_os.environ.get('NIFTY_DATA_ROOT',REPO_ROOT/'data'/'NIFTY_Data'))   # downloaded Parquet; override with NIFTY_DATA_ROOT
