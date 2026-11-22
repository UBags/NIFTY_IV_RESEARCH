"""Optional VERIFIABLE NSE settlement CSV; never silently claim spot as official."""
from __future__ import annotations
import csv
from pathlib import Path

class SettlementMaster:
    def __init__(self,path=None):
        self.rows={}
        if path:
            with Path(path).open(newline='') as file:
                for row in csv.DictReader(file):
                    expiry=row['expiry'].strip();value=float(row['settlement_price'])
                    evidence=row.get('evidence_url','').strip()
                    if value<=0 or not evidence:raise ValueError('Official settlement requires positive price + nonempty evidence_url')
                    if expiry in self.rows:raise ValueError('Duplicate official settlement: '+expiry)
                    self.rows[expiry]=(value,evidence)
    def get(self,expiry):return self.rows.get(expiry)
