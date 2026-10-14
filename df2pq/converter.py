"""Batch-convert tabular files from one or more folders (library and CLI).

Each selected source folder gets a same-named folder below the output folder.
Subfolders below each source are preserved. Source files are never changed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


FORMAT_SUFFIXES = {
    "csv": (".csv",),
    "tsv": (".tsv",),
    "parquet": (".parquet", ".pq"),
    "feather": (".feather",),
    "json": (".json",),
    "jsonl": (".jsonl", ".ndjson"),
    "orc": (".orc",),
    "ipc": (".arrow", ".ipc"),
    "excel": (".xlsx", ".xls"),
    "pickle": (".pkl", ".pickle"),
}

OUTPUT_SUFFIX = {
    "csv": ".csv",
    "tsv": ".tsv",
    "parquet": ".parquet",
    "feather": ".feather",
    "json": ".json",
    "jsonl": ".jsonl",
    "orc": ".orc",
    "ipc": ".arrow",
    "excel": ".xlsx",
    "pickle": ".pkl",
}

ENGINE_FORMATS = {
    "pandas": frozenset(
        {"csv", "tsv", "parquet", "feather", "json", "jsonl", "orc", "excel", "pickle"}
    ),
    "cudf": frozenset({"csv", "tsv", "parquet", "feather", "json", "jsonl", "orc"}),
    "pyarrow": frozenset({"csv", "tsv", "parquet", "feather", "orc", "ipc"}),
}


@dataclass(frozen=True)
class ConversionJob:
    source_root: Path
    source: Path
    input_format: str
    target: Path


@dataclass(frozen=True)
class IOOptions:
    input_separator: str | None = None
    output_separator: str | None = None
    header: bool = True
    json_lines: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert matching files from multiple folders while mirroring each "
            "source folder under a chosen output folder."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        action="append",
        required=True,
        metavar="FOLDER",
        help="Source folder; repeat this option to convert multiple folders.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        metavar="FOLDER",
        help="Output root folder. Each source folder name is recreated inside it.",
    )
    parser.add_argument(
        "--input-format",
        choices=("auto", *FORMAT_SUFFIXES),
        default="auto",
        help="Input format, or auto-detect it from each file extension.",
    )
    parser.add_argument(
        "--output-format",
        choices=tuple(FORMAT_SUFFIXES),
        required=True,
        help="Format to write for every matching input file.",
    )
    parser.add_argument(
        "--engine",
        choices=tuple(ENGINE_FORMATS),
        default="pandas",
        help="Reader/writer backend. See README.md for supported formats.",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_false",
        dest="recursive",
        help="Only scan files directly inside each source folder.",
    )
    parser.set_defaults(recursive=True)
    parser.add_argument(
        "--input-separator",
        "--input-delimiter",
        dest="input_separator",
        help="CSV/TSV input delimiter; defaults to comma for CSV and tab for TSV.",
    )
    parser.add_argument(
        "--output-separator",
        "--output-delimiter",
        dest="output_separator",
        help="CSV/TSV output delimiter; defaults to comma for CSV and tab for TSV.",
    )
    parser.add_argument(
        "--no-header",
        action="store_false",
        dest="header",
        help="Treat delimited-text inputs as headerless.",
    )
    parser.set_defaults(header=True)
    parser.add_argument(
        "--json-lines",
        action="store_true",
        help="Read/write newline-delimited JSON (JSON Lines) instead of a JSON array.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Convert at most the first N matching files (useful for a trial run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned source-to-destination mappings without converting.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output files. By default, existing files are skipped.",
    )
    return parser


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _format_for_path(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    for file_format, suffixes in FORMAT_SUFFIXES.items():
        if suffix in suffixes:
            return file_format
    return None


def build_jobs(
    source_dirs: list[Path],
    output_dir: Path,
    input_format: str,
    output_format: str,
    engine: str,
    *,
    recursive: bool = True,
    limit: int | None = None,
) -> list[ConversionJob]:
    """Discover files and map them to their mirrored output paths."""
    if output_format not in ENGINE_FORMATS[engine]:
        supported = ", ".join(sorted(ENGINE_FORMATS[engine]))
        raise ValueError(f"{engine} cannot write {output_format}; supported formats: {supported}")
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")

    output_root = output_dir.expanduser().resolve()
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"Output path is not a folder: {output_root}")

    roots: list[Path] = []
    root_names: set[str] = set()
    for source_dir in source_dirs:
        root = source_dir.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Source folder does not exist or is not a folder: {root}")
        if root.name in root_names:
            raise ValueError(
                f"Source folder names must be unique so outputs do not collide: {root.name}"
            )
        root_names.add(root.name)
        target_root = output_root / root.name
        if _paths_overlap(root, target_root):
            raise ValueError(
                f"Output folder for {root} would overlap the source folder: {target_root}"
            )
        roots.append(root)

    jobs: list[ConversionJob] = []
    targets: dict[Path, Path] = {}
    for root in roots:
        candidates = root.rglob("*") if recursive else root.iterdir()
        for source in sorted(path for path in candidates if path.is_file()):
            if input_format == "auto":
                detected_format = _format_for_path(source)
            else:
                detected_format = (
                    input_format
                    if source.suffix.casefold() in FORMAT_SUFFIXES[input_format]
                    else None
                )
            if detected_format is None:
                continue
            if detected_format not in ENGINE_FORMATS[engine]:
                supported = ", ".join(sorted(ENGINE_FORMATS[engine]))
                raise ValueError(
                    f"{engine} cannot read {detected_format} input ({source}); "
                    f"supported formats: {supported}"
                )

            relative_path = source.relative_to(root)
            target = (
                output_root
                / root.name
                / relative_path.with_suffix(OUTPUT_SUFFIX[output_format])
            )
            if target in targets:
                raise ValueError(
                    f"Multiple input files map to the same output: {targets[target]} and {source}"
                )
            targets[target] = source
            jobs.append(ConversionJob(root, source, detected_format, target))
            if limit is not None and len(jobs) >= limit:
                return jobs

    if not jobs:
        format_text = "supported formats" if input_format == "auto" else input_format
        raise ValueError(f"No files matching {format_text} were found in the source folders")
    return jobs


def _load_engine(engine: str) -> Any:
    try:
        if engine == "pandas":
            import pandas

            return pandas
        if engine == "pyarrow":
            import pyarrow

            return pyarrow
        cudf = importlib.import_module("cudf")
        try:  # let files larger than VRAM spill to host memory instead of failing
            cudf.set_option("spill", True)
        except Exception:
            pass
        return cudf
    except Exception as exc:
        install_hint = {
            "pandas": "Install pandas to use --engine pandas.",
            "cudf": "Install a compatible RAPIDS cuDF build to use --engine cudf.",
            "pyarrow": "Install pyarrow to use --engine pyarrow.",
        }[engine]
        raise RuntimeError(f"Could not load {engine}: {exc}. {install_hint}") from exc


def _separator(file_format: str, configured: str | None) -> str:
    if configured is not None:
        return configured
    return "\t" if file_format == "tsv" else ","


def _read_pandas(path: Path, file_format: str, options: IOOptions, pd: Any) -> Any:
    if file_format in {"csv", "tsv"}:
        return pd.read_csv(
            path,
            sep=_separator(file_format, options.input_separator),
            header=0 if options.header else None,
        )
    if file_format == "parquet":
        return pd.read_parquet(path)
    if file_format == "feather":
        return pd.read_feather(path)
    if file_format in {"json", "jsonl"}:
        return pd.read_json(path, lines=options.json_lines or file_format == "jsonl")
    if file_format == "orc":
        return pd.read_orc(path)
    if file_format == "excel":
        return pd.read_excel(path)
    if file_format == "pickle":
        return pd.read_pickle(path)
    raise ValueError(f"Unsupported pandas input format: {file_format}")


def _read_cudf(path: Path, file_format: str, options: IOOptions, cudf: Any) -> Any:
    if file_format in {"csv", "tsv"}:
        return cudf.read_csv(
            path,
            sep=_separator(file_format, options.input_separator),
            header=0 if options.header else None,
        )
    if file_format == "parquet":
        return cudf.read_parquet(path)
    if file_format == "feather":
        return cudf.read_feather(path)
    if file_format in {"json", "jsonl"}:
        return cudf.read_json(path, lines=options.json_lines or file_format == "jsonl")
    if file_format == "orc":
        return cudf.read_orc(path)
    raise ValueError(f"Unsupported cuDF input format: {file_format}")


def _read_ipc(path: Path, ipc: Any, pa: Any) -> Any:
    try:
        return ipc.open_file(path).read_all()
    except (pa.ArrowInvalid, OSError, ValueError):
        return ipc.open_stream(path).read_all()


def _read_pyarrow(path: Path, file_format: str, options: IOOptions, pa: Any) -> Any:
    if file_format in {"csv", "tsv"}:
        import pyarrow.csv as arrow_csv

        read_options = arrow_csv.ReadOptions(autogenerate_column_names=not options.header)
        parse_options = arrow_csv.ParseOptions(
            delimiter=_separator(file_format, options.input_separator)
        )
        return arrow_csv.read_csv(path, read_options=read_options, parse_options=parse_options)
    if file_format == "parquet":
        import pyarrow.parquet as parquet

        return parquet.read_table(path)
    if file_format == "feather":
        import pyarrow.feather as feather

        return feather.read_table(path)
    if file_format == "orc":
        import pyarrow.orc as orc

        return orc.ORCFile(path).read()
    if file_format == "ipc":
        import pyarrow.ipc as ipc

        return _read_ipc(path, ipc, pa)
    raise ValueError(f"Unsupported PyArrow input format: {file_format}")


def read_data(path: Path, file_format: str, engine: str, options: IOOptions, backend: Any) -> Any:
    if engine == "pandas":
        return _read_pandas(path, file_format, options, backend)
    if engine == "cudf":
        return _read_cudf(path, file_format, options, backend)
    return _read_pyarrow(path, file_format, options, backend)


def _write_pandas(data: Any, path: Path, file_format: str, options: IOOptions) -> None:
    if file_format in {"csv", "tsv"}:
        data.to_csv(path, index=False, sep=_separator(file_format, options.output_separator))
    elif file_format == "parquet":
        data.to_parquet(path, index=False)
    elif file_format == "feather":
        data.reset_index(drop=True).to_feather(path)
    elif file_format in {"json", "jsonl"}:
        data.to_json(
            path,
            orient="records",
            lines=options.json_lines or file_format == "jsonl",
        )
    elif file_format == "orc":
        data.to_orc(path, index=False)
    elif file_format == "excel":
        data.to_excel(path, index=False)
    elif file_format == "pickle":
        data.to_pickle(path)
    else:
        raise ValueError(f"Unsupported pandas output format: {file_format}")


def _write_cudf(data: Any, path: Path, file_format: str, options: IOOptions) -> None:
    if file_format in {"csv", "tsv"}:
        data.to_csv(path, index=False, sep=_separator(file_format, options.output_separator))
    elif file_format == "parquet":
        data.to_parquet(path, index=False)
    elif file_format == "feather":
        data.to_feather(path)
    elif file_format in {"json", "jsonl"}:
        data.to_json(
            path,
            orient="records",
            lines=options.json_lines or file_format == "jsonl",
        )
    elif file_format == "orc":
        data.to_orc(path, index=False)
    else:
        raise ValueError(f"Unsupported cuDF output format: {file_format}")


def _as_arrow_table(data: Any, engine: str, pa: Any) -> Any:
    if engine == "pyarrow":
        return data
    if engine == "pandas":
        return pa.Table.from_pandas(data, preserve_index=False)
    return data.to_arrow(preserve_index=False)


def _write_pyarrow(
    data: Any,
    path: Path,
    file_format: str,
    options: IOOptions,
    pa: Any,
    engine: str,
) -> None:
    table = _as_arrow_table(data, engine, pa)
    if file_format in {"csv", "tsv"}:
        import pyarrow.csv as arrow_csv

        write_options = arrow_csv.WriteOptions(
            delimiter=_separator(file_format, options.output_separator)
        )
        arrow_csv.write_csv(table, path, write_options=write_options)
    elif file_format == "parquet":
        import pyarrow.parquet as parquet

        parquet.write_table(table, path)
    elif file_format == "feather":
        import pyarrow.feather as feather

        feather.write_feather(table, path)
    elif file_format == "orc":
        import pyarrow.orc as orc

        orc.write_table(table, path)
    elif file_format == "ipc":
        import pyarrow.ipc as ipc

        with pa.OSFile(str(path), "wb") as sink:
            with ipc.new_file(sink, table.schema) as writer:
                writer.write_table(table)
    else:
        raise ValueError(f"Unsupported PyArrow output format: {file_format}")


def write_data(
    data: Any,
    path: Path,
    file_format: str,
    engine: str,
    options: IOOptions,
    backend: Any,
) -> None:
    if engine == "pandas":
        _write_pandas(data, path, file_format, options)
    elif engine == "cudf":
        _write_cudf(data, path, file_format, options)
    else:
        _write_pyarrow(data, path, file_format, options, backend, engine)


def convert_one(
    job: ConversionJob,
    output_format: str,
    engine: str,
    options: IOOptions,
    backend: Any,
    *,
    overwrite: bool = False,
    fallback: tuple[str, Any] | None = None,
) -> str:
    """Convert one file using a temporary file so failed writes are not published.

    Returns "converted", "skipped", or "converted_cpu" when the GPU engine failed
    on this file and ``fallback`` (an ``(engine, backend)`` pair) succeeded.
    """
    if job.target.exists() and not overwrite:
        return "skipped"

    job.target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{job.target.stem}.",
        suffix=job.target.suffix,
        dir=job.target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    used_fallback = False
    try:
        try:
            data = read_data(job.source, job.input_format, engine, options, backend)
            write_data(data, temporary, output_format, engine, options, backend)
        except Exception:
            if (
                fallback is None
                or fallback[0] == engine
                or job.input_format not in ENGINE_FORMATS[fallback[0]]
                or output_format not in ENGINE_FORMATS[fallback[0]]
            ):
                raise
            data = None  # drop GPU buffers before retrying on the CPU
            cpu_data = read_data(job.source, job.input_format, fallback[0], options, fallback[1])
            write_data(cpu_data, temporary, output_format, fallback[0], options, fallback[1])
            used_fallback = True
        finally:
            data = None
        result = "converted_cpu" if used_fallback else "converted"
        if overwrite:
            os.replace(temporary, job.target)
        else:
            # Check again after conversion, then atomically publish on the same
            # filesystem. This also works on filesystems without hard-link support.
            if job.target.exists():
                return "skipped"
            os.replace(temporary, job.target)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        for label, separator in (
            ("--input-separator", args.input_separator),
            ("--output-separator", args.output_separator),
        ):
            if separator is not None and len(separator) != 1:
                raise ValueError(f"{label} must be a single character")
        jobs = build_jobs(
            args.source,
            args.output,
            args.input_format,
            args.output_format,
            args.engine,
            recursive=args.recursive,
            limit=args.limit,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for job in jobs:
            print(
                f"{job.source} -> {job.target} "
                f"({job.input_format} -> {args.output_format})"
            )
        print(f"Dry run: {len(jobs)} file(s) would be converted with {args.engine}.")
        return 0

    try:
        backend = _load_engine(args.engine)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    options = IOOptions(
        input_separator=args.input_separator,
        output_separator=args.output_separator,
        header=args.header,
        json_lines=args.json_lines,
    )
    fallback = None
    if args.engine == "cudf":
        try:
            fallback = ("pandas", _load_engine("pandas"))
        except RuntimeError:
            pass
    converted = skipped = failed = 0
    for index, job in enumerate(jobs, start=1):
        try:
            result = convert_one(
                job,
                args.output_format,
                args.engine,
                options,
                backend,
                overwrite=args.overwrite,
                fallback=fallback,
            )
            if result in {"converted", "converted_cpu"}:
                converted += 1
                note = " (GPU failed, used CPU)" if result == "converted_cpu" else ""
                print(
                    f"[{index}/{len(jobs)}] Converted{note}: {job.source} -> {job.target}",
                    flush=True,
                )
            else:
                skipped += 1
                print(
                    f"[{index}/{len(jobs)}] Skipped (already exists): {job.target}",
                    flush=True,
                )
        except Exception as exc:
            failed += 1
            print(
                f"[{index}/{len(jobs)}] Failed: {job.source}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    print(f"Summary: converted={converted}, skipped={skipped}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
