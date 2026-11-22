# data/

Not tracked. Put (or symlink) the downloaded Parquet tree here as `data/NIFTY_Data/`, or point `NIFTY_DATA_ROOT` at it.
The research code reads PostgreSQL `market_data.*` (loaded by `nifty-data-import-postgres`); only the importer and the coverage check read these files.
