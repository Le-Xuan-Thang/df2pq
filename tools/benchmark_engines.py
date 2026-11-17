"""Benchmark df2pq's engines on reproducible synthetic data (developer tool, not shipped).

Generates headerless numeric CSV files, then converts them one file at a time,
exactly as the app does (same job builder, converter and GPU worker), and prints
a Markdown table for docs/BENCHMARKS.md.

    uv run python tools/benchmark_engines.py --data /tmp/df2pq-bench --output /tmp/out \\
        --files 200 --rows 60000 --engines cudf,pyarrow,pandas,pyarrow:32

"engine:N" converts N files at once, through the app's own batch runner (worker
start-up included), as with "Parallel files" in the Run tab.

Put --output on the drive you want to measure: the destination filesystem can
matter as much as the engine (see the NTFS results in docs/BENCHMARKS.md).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from df2pq.converter import IOOptions, _load_engine, build_jobs, convert_one  # noqa: E402
from df2pq.runner import BatchRunner  # noqa: E402


@dataclass
class Result:
    engine: str
    workers: int = 1
    files: int = 0
    seconds: float = 0.0
    bytes_read: int = 0
    max_ulp: int | None = None
    error: str = ""


def _write_file(args: tuple[Path, int, int, int]) -> None:
    path, rows, columns, seed = args
    rng = np.random.default_rng(seed)
    time_s = np.arange(rows) / 100.0
    channels = 11.7 + rng.normal(0, 1e-3, size=(rows, columns - 1))
    data = np.column_stack([time_s, channels])
    # repr() keeps all 17 significant digits, so parsing precision is visible.
    with path.open("w") as f:
        f.writelines(";".join(map(repr, row.tolist())) + "\n" for row in data)


def make_data(folder: Path, files: int, rows: int, columns: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / f"record_{i:05d}.csv" for i in range(files)]
    missing = [(p, rows, columns, i) for i, p in enumerate(paths) if not p.exists()]
    with ProcessPoolExecutor(min(32, os.cpu_count() or 1)) as pool:
        list(pool.map(_write_file, missing))
    return paths


def max_ulp_error(source: Path, output: Path) -> int:
    """Largest distance, in units in the last place, from the exact decimal value."""
    import pyarrow.parquet as pq

    exact = np.array([[float(v) for v in line.split(";")]
                      for line in source.read_text().splitlines()])
    got = pq.read_table(output).to_pandas().to_numpy(dtype=float)
    return int(np.abs(got.view(np.int64) - exact.view(np.int64)).max())


def run(paths_dir: Path, output: Path, engines: list[str]) -> list[Result]:
    options = IOOptions(input_separator=";", header=False)
    results = []
    for spec in engines:
        engine, _, count = spec.partition(":")
        result = Result(engine, int(count or 1))
        results.append(result)
        target_root = output / spec.replace(":", "-")
        shutil.rmtree(target_root, ignore_errors=True)
        try:
            if result.workers > 1:
                runner = BatchRunner()
                start = time.perf_counter()
                runner._run(sources=[paths_dir], output=target_root, input_format="csv",
                            output_format="parquet", engine=engine, options=options,
                            recursive=False, limit=None, overwrite=True, dry_run=False,
                            workers=result.workers)
                result.seconds = time.perf_counter() - start
                events = []
                while not runner.events.empty():
                    events.append(runner.events.get())
                kind, payload = events[-1]
                if kind != "done" or payload[2]:
                    raise RuntimeError(f"batch ended with {kind} {payload}")
                jobs = build_jobs([paths_dir], target_root, "csv", "parquet", engine)
                result.files = len(jobs)
                result.bytes_read = sum(j.source.stat().st_size for j in jobs)
                result.max_ulp = max_ulp_error(jobs[0].source, jobs[0].target)
                continue
            backend = _load_engine(engine)
            jobs = build_jobs([paths_dir], target_root, "csv", "parquet", engine)
            # One untimed file first: library import and GPU start-up are paid once
            # per session in the app, so they are not part of the per-file speed.
            warm = jobs[0].__class__(jobs[0].source_root, jobs[0].source, "csv",
                                     target_root / "warm-up.parquet")
            convert_one(warm, "parquet", engine, options, backend, overwrite=True)
            start = time.perf_counter()
            for job in jobs:
                convert_one(job, "parquet", engine, options, backend, overwrite=True)
            result.seconds = time.perf_counter() - start
            result.files = len(jobs)
            result.bytes_read = sum(j.source.stat().st_size for j in jobs)
            result.max_ulp = max_ulp_error(jobs[0].source, jobs[0].target)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        finally:
            shutil.rmtree(target_root, ignore_errors=True)
    return results


def table(results: list[Result]) -> str:
    lines = ["| Engine | Parallel files | Time | Per file | Throughput | Values vs. source text |",
             "|---|---|---|---|---|---|"]
    for r in results:
        if r.error:
            lines.append(f"| {r.engine} | {r.workers} | failed: {r.error} | | | |")
            continue
        values = "exact" if r.max_ulp == 0 else f"up to {r.max_ulp} ULP off (≈1e-15 relative)"
        lines.append(f"| {r.engine} | {r.workers} | {r.seconds:.1f} s | "
                     f"{1000 * r.seconds / r.files:.0f} ms | "
                     f"{r.bytes_read / 1e6 / r.seconds:.0f} MB/s | {values} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True, help="Where to create the CSV files")
    parser.add_argument("--output", type=Path, required=True,
                        help="Where converted files are written (and deleted afterwards)")
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--rows", type=int, default=60_000)
    parser.add_argument("--columns", type=int, default=9)
    parser.add_argument("--engines", default="cudf,pyarrow,pandas")
    args = parser.parse_args()

    folder = args.data / f"{args.files}x{args.rows}x{args.columns}"
    paths = make_data(folder, args.files, args.rows, args.columns)
    size = sum(p.stat().st_size for p in paths)
    print(f"{len(paths)} CSV files, {size / 1e9:.2f} GB, {args.rows:,} rows × {args.columns} "
          f"columns each · output on {args.output} · {platform.python_version()}",
          file=sys.stderr)
    print(table(run(folder, args.output, args.engines.split(","))))


if __name__ == "__main__":
    main()
