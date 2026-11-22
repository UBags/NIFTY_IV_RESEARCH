#!/usr/bin/env python3
"""Resumable, source-aware Parquet -> PostgreSQL loader for NIFTY_OPTIONS.

Requires Python 3.10+, pyarrow, pandas, psycopg 3 (psycopg[binary]).

USAGE
  python import_nifty_postgres.py --plan
  python import_nifty_postgres.py --init-only
  python import_nifty_postgres.py --scope nifty --file-glob '*/options/NIFTY/2021-05-27.parquet' --max-chunks 2
  python import_nifty_postgres.py --scope nifty
  python import_nifty_postgres.py --scope all
  python import_nifty_postgres.py --build-indexes

Run from the NIFTY_OPTIONS project; default input is ./NIFTY_Data.
Credentials: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD environment variables.

Each COPY chunk and its import checkpoint commit in the SAME transaction.
File changes are NOT overwritten: a changed file requires explicit investigation.
Provider-overlapping observations are intentionally kept, with source_file_id.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Third-party imports are deferred so --help works before installation.

SCHEMA = "market_data"
TABLES = {"option": "option_bars", "spot": "spot_bars", "future": "future_bars"}

COLUMNS = {
    "option": (
        "source_file_id", "source_row_index", "bar_time", "trading_day",
        "provider_trading_day", "exchange", "underlying", "expiry",
        "strike", "option_type", "open", "high", "low", "close",
        "volume", "open_interest", "exercise_style", "granularity",
        "vendor_label", "settle_price",
    ),
    "spot": (
        "source_file_id", "source_row_index", "bar_time", "trading_day",
        "provider_trading_day", "exchange", "symbol", "open", "high",
        "low", "close", "volume",
    ),
    "future": (
        "source_file_id", "source_row_index", "bar_time", "trading_day",
        "provider_trading_day", "exchange", "underlying", "expiry",
        "trading_symbol", "groww_symbol", "instrument_type", "strike",
        "open", "high", "low", "close", "volume", "open_interest",
        "lot_size", "tick_size",
    ),
}

DDL = r"""
CREATE SCHEMA IF NOT EXISTS market_data;

CREATE TABLE IF NOT EXISTS market_data.import_files (
    file_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    file_bytes BIGINT NOT NULL,
    mtime_ns BIGINT NOT NULL,
    expected_rows BIGINT NOT NULL,
    row_groups INTEGER NOT NULL,
    dataset TEXT NOT NULL,
    market_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    next_row_group INTEGER NOT NULL DEFAULT 0,
    next_row_offset BIGINT NOT NULL DEFAULT 0,
    loaded_rows BIGINT NOT NULL DEFAULT 0,
    date_mismatches BIGINT NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS market_data.spot_bars (
    source_file_id BIGINT NOT NULL,
    source_row_index BIGINT NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    trading_day DATE NOT NULL,
    provider_trading_day TEXT,
    exchange TEXT,
    symbol TEXT NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume BIGINT
) PARTITION BY RANGE (trading_day);

CREATE TABLE IF NOT EXISTS market_data.option_bars (
    source_file_id BIGINT NOT NULL,
    source_row_index BIGINT NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    trading_day DATE NOT NULL,
    provider_trading_day TEXT,
    exchange TEXT,
    underlying TEXT NOT NULL,
    expiry DATE NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    option_type TEXT NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume BIGINT,
    open_interest BIGINT,
    exercise_style TEXT,
    granularity TEXT,
    vendor_label TEXT,
    settle_price DOUBLE PRECISION
) PARTITION BY RANGE (trading_day);

CREATE TABLE IF NOT EXISTS market_data.future_bars (
    source_file_id BIGINT NOT NULL,
    source_row_index BIGINT NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    trading_day DATE NOT NULL,
    provider_trading_day TEXT,
    exchange TEXT,
    underlying TEXT NOT NULL,
    expiry DATE,
    trading_symbol TEXT,
    groww_symbol TEXT,
    instrument_type TEXT,
    strike DOUBLE PRECISION,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    open_interest DOUBLE PRECISION,
    lot_size TEXT,
    tick_size TEXT
) PARTITION BY RANGE (trading_day);
"""


def classify(rel: str):
    parts = Path(rel).parts
    if len(parts) == 1 and rel == "NIFTY.parquet":
        return "spot", "top_level_nifty_spot", "NIFTY"
    if len(parts) == 1 and rel in {"NIFTY_2024.parquet", "NIFTY_2025.parquet", "NIFTY_2026.parquet"}:
        return "option", "rissin_upstox", "NIFTY"
    if len(parts) >= 3 and parts[0] == "india-index-options-1m":
        if parts[1] == "index" and len(parts) == 3:
            return "spot", "thetrademarkk", Path(rel).stem
        if parts[1] == "options" and len(parts) == 4:
            return "option", "thetrademarkk", parts[2]
        if parts[1] == "stocks_options" and len(parts) == 4:
            return "option", "thetrademarkk", parts[2]
        if parts[1] == "mcx" and len(parts) >= 5:
            return "future", "thetrademarkk_mcx", parts[2]
    return None


def select_files(root: Path, scope: str, file_glob: str | None):
    selected = []
    unknown = []
    for path in root.rglob("*.parquet"):
        rel = path.relative_to(root).as_posix()
        cls = classify(rel)
        if cls is None:
            unknown.append(rel)
            continue
        kind, provider, sym = cls
        if scope == "nifty" and sym != "NIFTY":
            continue
        if file_glob and not fnmatch.fnmatchcase(rel, file_glob):
            continue
        selected.append((path, rel, kind, provider, sym))
    selected.sort(key=lambda x: (0 if x[2] == "spot" else 1 if x[2] == "option" else 2, x[1]))
    return selected, unknown


def ensure_partitions(cur, kind: str, years):
    name = TABLES[kind]
    for y in sorted(set(int(y) for y in years)):
        if y < 1990 or y > 2100:
            raise ValueError(f"Unreasonable trading year {y}")
        # These identifiers come only from trusted internal names and integer years.
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{name}_{y} "
            f"PARTITION OF {SCHEMA}.{name} "
            f"FOR VALUES FROM ('{y}-01-01') TO ('{y + 1}-01-01')"
        )


def null_series(pd, index):
    return pd.Series([None] * len(index), index=index, dtype=object)


def get(df, name, pd):
    return df[name] if name in df.columns else null_series(pd, df.index)


def strict_numeric(series, pd, label, allow_null=True):
    v = pd.to_numeric(series, errors="coerce")
    bad = series.notna() & v.isna()
    if bad.any():
        idx = int(bad[bad].index[0])
        raise ValueError(f"Invalid numeric {label} at batch index {idx}: {series.loc[idx]!r}")
    if not allow_null and v.isna().any():
        raise ValueError(f"Missing required numeric column {label}")
    return v


def normalized_frame(batch, kind, provider, symbol, file_id, start_row):
    import pandas as pd
    df = batch.to_pandas().reset_index(drop=True)
    n = len(df)
    # A few MCX files store ISO strings; timezones must be explicit on those.
    col = get(df, "timestamp", pd)
    if pd.api.types.is_datetime64_any_dtype(col):
        stamp = pd.to_datetime(col, utc=True, errors="coerce").dt.tz_convert("Asia/Kolkata")
    else:
        strings = col.astype("string")
        nonnull = strings.dropna()
        if not nonnull.str.contains(r"(?:Z|[+-]\d\d:?\d\d)$", case=False, regex=True).all():
            raise ValueError("String timestamps without explicit timezone: refusing to assume UTC/IST")
        stamp = pd.to_datetime(strings, utc=True, errors="coerce").dt.tz_convert("Asia/Kolkata")
    if stamp.isna().any():
        raise ValueError(f"Null/invalid timestamp in {int(stamp.isna().sum())} rows")
    day = stamp.dt.strftime("%Y-%m-%d")
    provider_day = (get(df, "trading_day", pd) if "trading_day" in df else get(df, "date", pd))
    provider_day_clean = provider_day.astype("string").str.slice(0, 10)
    mismatches = (provider_day_clean.notna() & (provider_day_clean != day)).sum()
    common = {
        "source_file_id": [file_id] * n,
        "source_row_index": range(start_row, start_row + n),
        "bar_time": stamp.dt.strftime("%Y-%m-%d %H:%M:%S.%f%z"),
        "trading_day": day,
        "provider_trading_day": provider_day,
        "exchange": get(df, "exchange", pd) if kind == "future" else ["NSE"] * n,
    }
    for px in ("open", "high", "low", "close"):
        common[px] = strict_numeric(get(df, px, pd), pd, px)
    if kind == "spot":
        sym = get(df, "symbol", pd)
        common.update({
            "symbol": sym.fillna(symbol).replace("", symbol),
            "volume": strict_numeric(get(df, "volume", pd), pd, "volume").astype("Int64"),
        })
    elif kind == "option":
        sym = get(df, "underlying", pd) if provider == "rissin_upstox" else get(df, "symbol", pd)
        expiry = pd.to_datetime(get(df, "expiry", pd), errors="coerce").dt.strftime("%Y-%m-%d")
        if expiry.isna().any():
            raise ValueError(f"Missing/bad expiry in {int(expiry.isna().sum())} option rows")
        option_type = get(df, "option_type", pd).astype("string").str.upper().str.strip()
        if not option_type.isin(["CE", "PE"]).all():
            raise ValueError(f"Unexpected option_type values: {option_type.unique()[:8].tolist()}")
        strike = strict_numeric(get(df, "strike", pd), pd, "strike", allow_null=False)
        if (strike <= 0).any():
            raise ValueError("Nonpositive option strike found")
        common.update({
            "underlying": sym.fillna(symbol).replace("", symbol),
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "volume": strict_numeric(get(df, "volume", pd), pd, "volume").astype("Int64"),
            "open_interest": strict_numeric((get(df, "oi", pd) if "oi" in df else get(df, "open_interest", pd)), pd, "open_interest").astype("Int64"),
            "exercise_style": get(df, "exercise_style", pd),
            "granularity": get(df, "granularity", pd).fillna("1min"),
            "vendor_label": get(df, "source", pd),
            "settle_price": strict_numeric(get(df, "settle_price", pd), pd, "settle_price"),
        })
    else:
        exp = get(df, "expiry", pd)
        common.update({
            "underlying": get(df, "underlying", pd).fillna(symbol),
            "expiry": pd.to_datetime(exp, errors="coerce").dt.strftime("%Y-%m-%d"),
            "trading_symbol": get(df, "trading_symbol", pd),
            "groww_symbol": get(df, "groww_symbol", pd),
            "instrument_type": get(df, "instrument_type", pd),
            "strike": strict_numeric(get(df, "strike", pd), pd, "strike"),
            "volume": strict_numeric(get(df, "volume", pd), pd, "volume"),
            "open_interest": strict_numeric(get(df, "open_interest", pd), pd, "open_interest"),
            "lot_size": get(df, "lot_size", pd),
            "tick_size": get(df, "tick_size", pd),
        })
    out = pd.DataFrame({c: common[c] for c in COLUMNS[kind]})
    if out["trading_day"].isna().any():
        raise ValueError("Invalid trading day")
    years = set(int(s[:4]) for s in day.unique())
    return out, years, int(mismatches)


def copy_frame(conn, frame, kind):
    buffer = io.StringIO()
    # Unquoted blanks become SQL NULL with COPY CSV NULL ''.
    frame.to_csv(buffer, index=False, header=False, na_rep="", lineterminator="\n")
    payload = buffer.getvalue().encode("utf-8")
    colnames = ", ".join(COLUMNS[kind])
    command = f"COPY {SCHEMA}.{TABLES[kind]} ({colnames}) FROM STDIN WITH (FORMAT CSV, NULL '')"
    with conn.cursor() as cur:
        with cur.copy(command) as cp:
            cp.write(payload)


def connect_db():
    try:
        import psycopg
    except ImportError as e:
        raise SystemExit("Missing psycopg. Run: pip install 'psycopg[binary]'") from e
    return psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "nifty_data"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
        connect_timeout=15,
    )


def register(conn, path: Path, rel: str, kind: str, provider: str, meta):
    st = path.stat()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_data.import_files
              (relative_path, file_bytes, mtime_ns, expected_rows, row_groups, dataset, market_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (relative_path) DO NOTHING
        """, (rel, st.st_size, st.st_mtime_ns, meta.num_rows, meta.num_row_groups, provider, kind))
        cur.execute("""
            SELECT file_id, file_bytes, mtime_ns, expected_rows, row_groups,
                   dataset, market_kind, status, next_row_group, next_row_offset, loaded_rows
            FROM market_data.import_files WHERE relative_path=%s
        """, (rel,))
        row = cur.fetchone()
    conn.commit()
    file_id, b, mtime, exp, ngrp, ds, db_kind, status, rg, off, loaded = row
    if (b, mtime, exp, ngrp, ds, db_kind) != (
        st.st_size, st.st_mtime_ns, meta.num_rows, meta.num_row_groups, provider, kind
    ):
        raise RuntimeError(
            f"File {rel} changed since registration. Old rows retained; "
            "DO NOT reimport automatically. Investigate or use a new database/schema."
        )
    return file_id, status, rg, off, loaded


def mark_status(conn, file_id, status, msg=None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE market_data.import_files
            SET status=%s, error_message=%s, updated_at=now(),
                completed_at=CASE WHEN %s='completed' THEN now() ELSE completed_at END
            WHERE file_id=%s
        """, (status, msg, status, file_id))
    conn.commit()


def import_file(conn, info, batch_rows, budget):
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise SystemExit("Missing pyarrow. Run: pip install pyarrow") from e
    path, rel, kind, provider, sym = info
    pf = pq.ParquetFile(path)
    fid, status, first_rg, first_offset, loaded = register(conn, path, rel, kind, provider, pf.metadata)
    if status == "completed":
        if loaded != pf.metadata.num_rows:
            raise RuntimeError(f"Completed ledger row count mismatch: {rel}")
        print(f"  SKIP complete: {rel} ({loaded:,} rows)", flush=True)
        return 0
    print(f"  FILE {rel} [{provider}/{kind}] {loaded:,}/{pf.metadata.num_rows:,} rows", flush=True)
    mark_status(conn, fid, "partial")
    prefix = 0
    for rg in range(pf.metadata.num_row_groups):
        if rg < first_rg:
            prefix += pf.metadata.row_group(rg).num_rows
            continue
        group_total = pf.metadata.row_group(rg).num_rows
        position = 0
        for batch in pf.iter_batches(batch_size=batch_rows, row_groups=[rg], use_threads=True):
            n = batch.num_rows
            if rg == first_rg and position + n <= first_offset:
                position += n
                continue
            if rg == first_rg and position < first_offset:
                cut = first_offset - position
                batch = batch.slice(cut)
                position += cut
                n = batch.num_rows
            if budget[0] is not None and budget[0] <= 0:
                return 1
            start_row = prefix + position
            try:
                frame, years, mismatches = normalized_frame(batch, kind, provider, sym, fid, start_row)
                with conn.cursor() as cur:
                    ensure_partitions(cur, kind, years)
                copy_frame(conn, frame, kind)
                next_offset = position + n
                next_rg = rg if next_offset < group_total else rg + 1
                if next_rg != rg:
                    next_offset = 0
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE market_data.import_files SET
                        next_row_group=%s, next_row_offset=%s,
                        loaded_rows=loaded_rows+%s,
                        date_mismatches=date_mismatches+%s,
                        status='partial', error_message=NULL, updated_at=now()
                        WHERE file_id=%s
                    """, (next_rg, next_offset, n, mismatches, fid))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                mark_status(conn, fid, "failed", f"rg={rg} row={start_row}: {exc}"[:4000])
                raise RuntimeError(f"Failed at file={rel} rg={rg} row={start_row}: {exc}") from exc
            position += n
            loaded += n
            if budget[0] is not None:
                budget[0] -= 1
            if budget[0] is None or budget[0] <= 0 or (loaded // batch_rows) % 20 == 0:
                print(f"    progress {loaded:,}/{pf.metadata.num_rows:,} rows", flush=True)
            if budget[0] is not None and budget[0] <= 0:
                return 1
        prefix += group_total
    if loaded != pf.metadata.num_rows:
        raise RuntimeError(f"Incomplete row count for {rel}: {loaded} vs {pf.metadata.num_rows}")
    mark_status(conn, fid, "completed")
    print(f"  COMPLETE {rel}: {loaded:,} rows", flush=True)
    return 0


def build_indexes(conn):
    statements = [
        "CREATE INDEX IF NOT EXISTS option_bars_trade_lookup ON market_data.option_bars (underlying, trading_day, option_type, expiry, strike, bar_time)",
        "CREATE INDEX IF NOT EXISTS option_bars_contract_lookup ON market_data.option_bars (underlying, expiry, strike, option_type, bar_time)",
        "CREATE INDEX IF NOT EXISTS spot_bars_time_lookup ON market_data.spot_bars (symbol, bar_time)",
        "CREATE INDEX IF NOT EXISTS future_bars_time_lookup ON market_data.future_bars (underlying, bar_time)",
    ]
    print("Building indexes. For hundreds of millions of rows, this may be resource-intensive.", flush=True)
    for statement in statements:
        print(f"  {statement}", flush=True)
        with conn.cursor() as cur:
            cur.execute(statement)
        conn.commit()
    print("Indexes complete.")


def plan(files, limit=10):
    groups = Counter((kind, provider) for _, _, kind, provider, _ in files)
    print(f"Files selected: {len(files):,}")
    for (kind, provider), count in sorted(groups.items()):
        print(f"  {kind:<7s} {provider:<22s} {count:>5} files")
    print("First files:")
    for p, rel, kind, provider, sym in files[:limit]:
        print(f"  {kind:7s} {p.stat().st_size/1024**2:8.1f} MiB  {rel}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent / "NIFTY_Data")
    parser.add_argument("--scope", choices=("nifty", "all"), default="nifty")
    parser.add_argument("--file-glob", help="Optional glob matched against relative Parquet path")
    parser.add_argument("--batch-rows", type=int, default=50_000)
    parser.add_argument("--max-chunks", type=int, help="Stop after N committed chunks, safely resumable")
    parser.add_argument("--limit-files", type=int, help="Limit number of selected files")
    parser.add_argument("--plan", action="store_true", help="List selected files; NO DATABASE CONNECTION")
    parser.add_argument("--init-only", action="store_true", help="Create schema/tables; import no data")
    parser.add_argument("--build-indexes", action="store_true", help="Build indexes explicitly AFTER import")
    args = parser.parse_args()
    if args.batch_rows < 1 or (args.max_chunks is not None and args.max_chunks < 1):
        parser.error("--batch-rows and --max-chunks must be positive")
    if args.plan or not (args.init_only or args.build_indexes):
        if not args.root.is_dir():
            parser.error(f"Parquet root not found: {args.root}")
        files, unknown = select_files(args.root, args.scope, args.file_glob)
        if unknown:
            print(f"WARNING: {len(unknown)} unrecognised Parquet paths ignored (first: {unknown[:3]})")
        if args.limit_files is not None:
            files = files[:args.limit_files]
        if not files:
            parser.error("No matching Parquet files")
        plan(files)
        if args.plan:
            return
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(14268, 2026)")
            if not cur.fetchone()[0]:
                raise RuntimeError("Another importer currently owns the NIFTY loader lock")
            cur.execute("SET TIME ZONE 'Asia/Kolkata'")
            cur.execute("SET statement_timeout = 0")
            cur.execute(DDL)
        conn.commit()
        if args.init_only:
            print("Database schema market_data created/verified. No bars imported.")
            return
        if args.build_indexes:
            build_indexes(conn)
            return
        budget = [args.max_chunks]
        start = time.monotonic()
        for idx, info in enumerate(files, 1):
            print(f"[{idx}/{len(files)}]", flush=True)
            stopped = import_file(conn, info, args.batch_rows, budget)
            if stopped:
                print("Chunk limit reached. Rerun the same command without --max-chunks to resume.")
                break
        with conn.cursor() as cur:
            cur.execute("""SELECT status, count(*), sum(loaded_rows) FROM market_data.import_files
                           GROUP BY status ORDER BY status""")
            print("\nIMPORT LEDGER SUMMARY:")
            for status, count, total in cur.fetchall():
                print(f"  {status:<10} files={count:5d} rows={total or 0:,}")
        conn.commit()
        print(f"Elapsed: {time.monotonic()-start:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Last committed chunk is preserved; rerun to resume.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)
