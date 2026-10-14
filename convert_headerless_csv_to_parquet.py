# /// script
# requires-python = ">=3.10"
# dependencies = ["pyarrow>=18"]
# ///
"""Convert headerless semicolon-delimited numeric CSV files to verified Parquet.

The input must contain nine numeric columns: time_s and channel_1 through
channel_8. Run from the project root with `uv run python convert_headerless_csv_to_parquet.py`.
"""
from pathlib import Path
import argparse
import os
import tempfile

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as pq


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    project_root = Path(__file__).resolve().parent
    parser.add_argument("--source", type=Path, default=project_root / "CSV Data")
    parser.add_argument("--output", type=Path, default=project_root / "Parquet Data")
    parser.add_argument("--limit", type=int, help="Convert only the first N files for a trial")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    files = sorted(args.source.rglob("*.csv"))
    if not files:
        parser.error(f"No CSV files in {args.source}")
    if args.limit:
        files = files[:args.limit]
    names = ["time_s"] + [f"channel_{i}" for i in range(1, 9)]
    input_bytes = output_bytes = converted = skipped = 0
    for i, source in enumerate(files, 1):
        target = args.output / source.relative_to(args.source).with_suffix(".parquet")
        if target.exists():
            skipped += 1
            print(f"[{i}/{len(files)}] Exists, skipped: {target}", flush=True)
            continue
        table = csv.read_csv(
            source,
            read_options=csv.ReadOptions(column_names=names),
            parse_options=csv.ParseOptions(delimiter=";"),
            convert_options=csv.ConvertOptions(column_types={n: pa.float64() for n in names}),
        )
        if table.num_rows == 0 or any(c.null_count for c in table.columns):
            raise ValueError(f"Empty recording or missing numeric values: {source}")
        table = table.replace_schema_metadata({
            b"source_relative_path": str(source.relative_to(args.source)).encode(),
            b"time_unit": b"seconds",
        })
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".parquet.tmp")
        os.close(fd)
        try:
            pq.write_table(table, temporary, compression="zstd", compression_level=3,
                           use_dictionary=False, row_group_size=65536)
            restored = pq.ParquetFile(temporary).read()
            if not table.equals(restored, check_metadata=True):
                raise ValueError(f"Parquet round-trip verification failed: {source}")
            # Publish only verified files; never replace an existing destination.
            os.link(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        input_bytes += source.stat().st_size
        output_bytes += target.stat().st_size
        converted += 1
        print(f"[{i}/{len(files)}] {source.name}: {table.num_rows:,} rows, "
              f"{source.stat().st_size / target.stat().st_size:.2f}x smaller", flush=True)
    print(f"Converted: {converted}; skipped: {skipped}")
    if input_bytes:
        print(f"CSV: {input_bytes / 1e6:.2f} MB; Parquet: {output_bytes / 1e6:.2f} MB; "
              f"saved: {100 * (1 - output_bytes / input_bytes):.1f}%")


if __name__ == "__main__":
    main()
