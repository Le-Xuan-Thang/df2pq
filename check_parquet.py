"""Verify CSV/Parquet pairs and benchmark repeated, warm-cache CPU reads."""
import argparse
import json
import statistics
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as pq

NAMES = ["time_s"] + [f"channel_{i}" for i in range(1, 9)]


def read_csv(path):
    return csv.read_csv(path, read_options=csv.ReadOptions(column_names=NAMES),
                        parse_options=csv.ParseOptions(delimiter=";"),
                        convert_options=csv.ConvertOptions(column_types={n: pa.float64() for n in NAMES}))


def median_seconds(reader, repeats):
    reader()  # Warm filesystem cache and initialize libraries.
    durations = []
    for _ in range(repeats):
        start = time.perf_counter()
        reader()
        durations.append(time.perf_counter() - start)
    return statistics.median(durations)


def check_pair(source, target, repeats=5):
    original = read_csv(source)
    restored = pq.ParquetFile(target).read()
    if not original.equals(restored, check_metadata=False):
        raise ValueError(f"Data mismatch: {source}")
    csv_time = median_seconds(lambda: read_csv(source), repeats)
    parquet_time = median_seconds(lambda: pq.ParquetFile(target).read(), repeats)
    return dict(file=str(source.name), rows=original.num_rows, columns=original.num_columns,
                csv_bytes=source.stat().st_size, parquet_bytes=target.stat().st_size,
                compression_ratio=source.stat().st_size / target.stat().st_size,
                csv_read_ms=csv_time * 1000, parquet_read_ms=parquet_time * 1000,
                read_speedup=csv_time / parquet_time, equal=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    data = Path(__file__).resolve().parent
    parser.add_argument("--source", type=Path, default=data / "Lello Data")
    parser.add_argument("--parquet", type=Path, default=data / "Lello Parquet")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--report", type=Path, default=data / "results/parquet_check.json")
    args = parser.parse_args()
    if args.limit < 1 or args.repeats < 1:
        parser.error("--limit and --repeats must be positive")
    pairs = [(p, args.parquet / p.relative_to(args.source).with_suffix(".parquet"))
             for p in sorted(args.source.rglob("*.csv"))]
    pairs = [(s, t) for s, t in pairs if t.exists()][:args.limit]
    if not pairs:
        parser.error("No converted CSV/Parquet pairs found")
    results = []
    for source, target in pairs:
        row = check_pair(source, target, args.repeats)
        results.append(row)
        print(json.dumps(row), flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"benchmark": "warm-cache CPU PyArrow, median",
                                      "repeats": args.repeats, "results": results}, indent=2))
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
