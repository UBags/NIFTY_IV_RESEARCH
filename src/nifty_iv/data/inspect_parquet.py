#!/usr/bin/env python3
"""Read-only schema and file-metadata inventory for NIFTY Parquet datasets.

Scans local Parquet metadata without converting files or touching PostgreSQL.
Reads only a few small samples from representative files per schema.

Examples:
    nifty-data-inspect-parquet --root "$HOME/NIFTY Data"
    nifty-data-inspect-parquet --root "$HOME/NIFTY Data" --scope all
    nifty-data-inspect-parquet --root "$HOME/NIFTY Data" --scope nifty --sample-files 3

Requirements: Python 3.9+, pyarrow (pip install pyarrow).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys


FOCUS_COLUMNS = [
    "timestamp", "datetime", "date", "trading_day", "expiry",
    "underlying", "symbol", "strike", "option_type", "open", "high",
    "low", "close", "volume", "open_interest", "oi", "source", "granularity",
]
STATS_COLUMNS = ("timestamp", "datetime", "trading_day", "expiry", "strike")


def cli():
    p = argparse.ArgumentParser(
        description="Inspect NIFTY Parquet schemas and samples (READ ONLY; no database connection).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Output: SUMMARY.md, FILE_INVENTORY.csv, SCHEMA_CATALOG.json, SAMPLE_ROWS.json, ERRORS.json",
    )
    p.add_argument("--root", type=Path, default=Path.home() / "NIFTY Data",
                   help='Local dataset directory; default: ~/NIFTY Data')
    p.add_argument("--out", type=Path, default=Path.home() / "nifty_parquet_inspection",
                   help='Where to write inspection reports (default: ~/nifty_parquet_inspection)')
    p.add_argument("--scope", choices=("nifty", "all"), default="nifty",
                   help='nifty: focus on the two NIFTY sources; all: inspect every Parquet under root')
    p.add_argument("--sample-files", type=int, default=2,
                   help='Maximum example files to sample per distinct schema (default 2)')
    p.add_argument("--sample-rows", type=int, default=5,
                   help='Maximum rows per example file (default 5)')
    p.add_argument("--max-files", type=int, default=0,
                   help='Optional development limit; 0 means no limit')
    args = p.parse_args()
    if args.sample_files < 0 or args.sample_rows < 0 or args.max_files < 0:
        p.error('--sample-files, --sample-rows and --max-files must be nonnegative')
    return args


def is_nifty_relevant(path: Path, root: Path) -> bool:
    """Focus on NIFTY only, including both alternative spot sources."""
    rel = path.relative_to(root)
    parts = [p.lower() for p in rel.parts]
    name = path.name.lower()
    # Top-level NIFTY.parquet, NIFTY_2024.parquet, etc.
    if len(parts) == 1:
        return name == "nifty.parquet" or (name.startswith("nifty_") and name.endswith(".parquet"))
    # Includes any .../index/NIFTY.parquet, .../options/NIFTY/{expiry}.parquet,
    # and .../upstox_intraday/NIFTY/{year}.parquet.
    if len(parts) >= 2 and parts[-2] == "index" and name == "nifty.parquet":
        return True
    if len(parts) >= 2 and parts[-2] == "nifty":
        return True
    return False


def find_files(root: Path, scope: str):
    paths = sorted(p for p in root.rglob("*.parquet") if p.is_file())
    if scope == "nifty":
        paths = [p for p in paths if is_nifty_relevant(p, root)]
    return paths


def simple(value):
    """Keep JSON and CSV output free of Arrow/Pandas-specific objects."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")[:200]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [simple(x) for x in value]
    if isinstance(value, dict):
        return {str(k): simple(v) for k, v in value.items()}
    return str(value)


def schema_info(schema):
    fields = []
    for field in schema:
        fields.append({
            "name": field.name,
            "type": str(field.type),
            "nullable": field.nullable,
            "field_metadata": {
                simple(k): simple(v) for k, v in (field.metadata or {}).items()
            },
        })
    return {
        "fields": fields,
        "schema_string": str(schema),
        "schema_metadata": {
            simple(k): simple(v) for k, v in (schema.metadata or {}).items()
        },
    }


def schema_fingerprint(schema):
    # Names, Arrow data types, and nullability are sufficient for SQL planning;
    # metadata like pandas indices does not create an additional table schema.
    compact = [(f.name, str(f.type), f.nullable) for f in schema]
    digest = hashlib.sha256(json.dumps(compact).encode("utf-8")).hexdigest()
    return digest[:12]


def source_group(rel: Path):
    p = [s.lower() for s in rel.parts]
    name = rel.name.lower()
    if "upstox_intraday" in p or (len(p) == 1 and name.startswith("nifty_") and name.endswith(".parquet")):
        return "rissin_upstox_options"
    if "options" in p and "nifty" in p:
        return "thetrademarkk_nifty_options"
    if "index" in p:
        return "thetrademarkk_spot"
    if len(p) == 1 and name == "nifty.parquet":
        return "spot_or_unknown_source"
    return rel.parts[0] if rel.parts else "unknown"


def scalar_key(x):
    """Comparable key for the values in Parquet min/max metadata."""
    if x is None:
        return None
    x = simple(x)
    return x if isinstance(x, (int, float, str)) else str(x)


def metadata_ranges(parquet_file):
    """Get coarse min/max stats without reading row data. Missing stats remain null."""
    meta = parquet_file.metadata
    keys = {x: {"min": None, "max": None, "row_groups_with_stats": 0} for x in STATS_COLUMNS}
    names = parquet_file.schema_arrow.names
    positions = {name: i for i, name in enumerate(names)}
    codecs = set()
    for rg_index in range(meta.num_row_groups):
        group = meta.row_group(rg_index)
        if rg_index == 0:
            for j in range(group.num_columns):
                codecs.add(group.column(j).compression)
        for name in STATS_COLUMNS:
            if name not in positions:
                continue
            # Arrow schema index is usually aligned with Parquet leaf columns for
            # these scalar fields. We verify the Parquet path before using stats.
            pos = positions[name]
            if pos >= group.num_columns:
                continue
            col = group.column(pos)
            if col.path_in_schema != name:
                continue
            st = col.statistics
            if st is None or not st.has_min_max:
                continue
            try:
                lo, hi = scalar_key(st.min), scalar_key(st.max)
                if lo is None or hi is None:
                    continue
                v = keys[name]
                if v["min"] is None or lo < v["min"]:
                    v["min"] = lo
                if v["max"] is None or hi > v["max"]:
                    v["max"] = hi
                v["row_groups_with_stats"] += 1
            except (TypeError, ValueError):
                # Some datasets mix statistics representations across row groups.
                continue
    return keys, sorted(codecs)


def file_inventory_row(path, root, pq):
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    rel = path.relative_to(root)
    meta = pf.metadata
    ranges, codecs = metadata_ranges(pf)
    row = {
        "relative_path": rel.as_posix(),
        "source_group": source_group(rel),
        "size_mb": round(path.stat().st_size / 1024**2, 3),
        "schema_id": schema_fingerprint(schema),
        "row_count": meta.num_rows,
        "row_groups": meta.num_row_groups,
        "column_count": len(schema),
        "columns": ",".join(schema.names),
        "compression": ",".join(codecs),
    }
    for name, values in ranges.items():
        row[f"{name}_min"] = values["min"]
        row[f"{name}_max"] = values["max"]
        row[f"{name}_groups_with_stats"] = values["row_groups_with_stats"]
    return row, schema_info(schema)


def first_example_batch(parquet_file, row_group_idx, columns, count):
    """At most count rows; stream batches rather than loading an entire row group."""
    batches = parquet_file.iter_batches(
        row_groups=[row_group_idx], columns=columns,
        batch_size=max(1, count), use_threads=False,
    )
    return next(batches, None)


def sample_file(path, root, pq, row_limit):
    pf = pq.ParquetFile(path)
    available = pf.schema_arrow.names
    cols = [x for x in FOCUS_COLUMNS if x in available]
    if not cols:
        cols = available[: min(12, len(available))]
    # Small samples from the first, middle and final row group
    # (when present), with a fixed total row budget.
    groups = sorted(set([0, pf.num_row_groups // 2, pf.num_row_groups - 1])) if pf.num_row_groups else []
    result = {
        "relative_path": path.relative_to(root).as_posix(),
        "selected_columns": cols,
        "samples": [],
    }
    if row_limit == 0:
        return result
    budget = row_limit
    for group in groups:
        if budget <= 0:
            break
        per_group = max(1, (row_limit + len(groups) - 1) // len(groups))
        batch = first_example_batch(pf, group, cols, min(budget, per_group))
        if batch is None:
            continue
        for obj in batch.to_pylist()[:budget]:
            result["samples"].append({
                "row_group": group,
                "values": simple(obj),
            })
            budget -= 1
    return result


def write_csv(path, records, columns):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_json(path, object_):
    path.write_text(json.dumps(object_, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def generate_summary(out, root, scope, inventory, schema_catalog, samples, errors, total_discovered):
    groups = defaultdict(list)
    for row in inventory:
        groups[row["source_group"]].append(row)
    lines = [
        "# NIFTY Parquet — structure inspection",
        "",
        f"Generated (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Dataset root: `{root}`",
        f"Inspection scope: `{scope}`",
        f"Files selected: **{total_discovered}**",
        f"Files successfully inspected: **{len(inventory)}**",
        f"Unreadable files: **{len(errors)}**",
        f"Distinct Arrow schemas: **{len(schema_catalog)}**",
        f"Total rows (sum across files; sources may overlap): **{sum(r['row_count'] for r in inventory):,}**",
        "",
        "## Sources / subdirectories",
        "",
        "| Source group | Files | Rows | Size (MiB) | Schema IDs |",
        "|---|---:|---:|---:|---|",
    ]
    for name, rows in sorted(groups.items()):
        lines.append(
            f"| {name} | {len(rows)} | {sum(r['row_count'] for r in rows):,} "
            f"| {sum(r['size_mb'] for r in rows):,.1f} "
            f"| {', '.join(sorted({r['schema_id'] for r in rows}))} |"
        )
    lines += ["", "## Schema details", ""]
    for sid, info in sorted(schema_catalog.items(), key=lambda p: -p[1]["file_count"]):
        lines += [
            f"### Schema `{sid}` — {info['file_count']} files",
            "",
            f"Example file: `{info['example_files'][0]}`",
            "",
            "| Column | Arrow type | Nullable |",
            "|---|---|---|",
        ]
        for field in info["schema"]["fields"]:
            lines.append(f"| `{field['name']}` | `{field['type']}` | {field['nullable']} |")
        lines.append("")
    lines += [
        "## Files containing timestamps/strike/expiry",
        "",
        "File-level minimum and maximum values, where available from Parquet metadata,",
        "are in `FILE_INVENTORY.csv`. These are metadata statistics, not full-content",
        "quality checks. Blank values mean no usable metadata statistics.",
        "",
        "## Sample rows",
        "",
        "`SAMPLE_ROWS.json` includes small examples from a few files per schema.",
        "Sampling does **not** certify 09:45 coverage, valid quotes, correct timezone",
        "assumptions, long-dated contract availability or completeness.",
        "",
        "## Important notes for the eventual PostgreSQL import",
        "",
        "- Do not import yet; review file overlap and column types first.",
        "- Two independent spot-index sources may contain duplicate observations.",
        "- The source dataset's README warns that options coverage is partial.",
        "- For an IV backtest, we must later verify matching 09:45 IST spot and",
        "  weekly/60–120-DTE option contracts rather than trusting file names alone.",
        "- Preserve timezone and original source/expiry to avoid accidental merges.",
        "",
        "## Please upload these reports",
        "",
        "1. `SUMMARY.md`",
        "2. `SCHEMA_CATALOG.json`",
        "3. `SAMPLE_ROWS.json`",
        "4. `FILE_INVENTORY.csv`",
        "5. `ERRORS.json` if any files could not be read",
        "",
    ]
    if errors:
        lines += ["## Errors", ""]
        for e in errors[:30]:
            lines.append(f"- `{e['relative_path']}`: {e['error']}")
        if len(errors) > 30:
            lines.append(f"- ... and {len(errors) - 30} more. See `ERRORS.json`.")
        lines.append("")
    (out / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    args = cli()
    root = args.root.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: folder not found: {root}", file=sys.stderr)
        print('Example: nifty-data-inspect-parquet --root "$HOME/NIFTY Data"', file=sys.stderr)
        return 2
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("ERROR: pyarrow is required. Install: python -m pip install pyarrow", file=sys.stderr)
        return 2

    paths = find_files(root, args.scope)
    if args.max_files:
        paths = paths[:args.max_files]
    if not paths:
        print(f"No Parquet files selected under {root} (scope={args.scope})", file=sys.stderr)
        print("Try --scope all to examine every Parquet file.", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    print(f"Root: {root}\nOutput: {out}\nScope: {args.scope}\nFiles selected: {len(paths)}", flush=True)

    inventory, catalog, errors = [], {}, []
    for i, path in enumerate(paths, 1):
        rel = path.relative_to(root).as_posix()
        try:
            row, schema = file_inventory_row(path, root, pq)
            inventory.append(row)
            sid = row["schema_id"]
            if sid not in catalog:
                catalog[sid] = {"file_count": 0, "example_files": [], "schema": schema}
            catalog[sid]["file_count"] += 1
            if len(catalog[sid]["example_files"]) < args.sample_files:
                catalog[sid]["example_files"].append(rel)
        except Exception as e:
            errors.append({"relative_path": rel, "error": f"{type(e).__name__}: {e}"})
            print(f"  ERROR {rel}: {e}", file=sys.stderr)
        if i == 1 or i == len(paths) or i % 100 == 0:
            print(f"  Metadata: {i}/{len(paths)}", flush=True)

    samples = []
    if args.sample_rows:
        for sid, info in catalog.items():
            for rel in info["example_files"]:
                try:
                    samples.append(sample_file(root / rel, root, pq, args.sample_rows))
                except Exception as e:
                    errors.append({"relative_path": rel, "error": f"Sample error: {type(e).__name__}: {e}"})
                    print(f"  SAMPLE ERROR {rel}: {e}", file=sys.stderr)

    columns = [
        "relative_path", "source_group", "size_mb", "schema_id", "row_count",
        "row_groups", "column_count", "columns", "compression",
    ]
    for name in STATS_COLUMNS:
        columns += [f"{name}_min", f"{name}_max", f"{name}_groups_with_stats"]
    write_csv(out / "FILE_INVENTORY.csv", inventory, columns)
    write_json(out / "SCHEMA_CATALOG.json", catalog)
    write_json(out / "SAMPLE_ROWS.json", samples)
    write_json(out / "ERRORS.json", errors)
    generate_summary(out, root, args.scope, inventory, catalog, samples, errors, len(paths))
    print("\nInspection finished; input Parquet files were not modified.")
    print("Reports:")
    for filename in ("SUMMARY.md", "SCHEMA_CATALOG.json", "SAMPLE_ROWS.json", "FILE_INVENTORY.csv", "ERRORS.json"):
        print(f"  {out / filename}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
