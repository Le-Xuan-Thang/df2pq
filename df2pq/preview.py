"""Show what a conversion will produce before any file is written.

``preview_file`` reads the first rows of one source file with the same options
the batch will use, round-trips them through the output format, and flags
settings that look wrong (for example a data row being used as the header).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
from typing import Any

from .converter import (
    ENGINE_FORMATS,
    OUTPUT_SUFFIX,
    IOOptions,
    _separator,
    read_data,
    write_data,
)

TEXT_FORMATS = frozenset({"csv", "tsv", "json", "jsonl"})
SEPARATOR_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


@dataclass
class Table:
    columns: list[str]
    dtypes: list[str]
    rows: list[list[str]]


@dataclass
class Preview:
    source: Path
    raw_lines: list[str]          # the file's first lines, for text formats
    before: Table | None          # the file as read, for binary formats
    after: Table | None           # what the output file will contain
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def _looks_numeric(text: Any) -> bool:
    text = str(text).strip()
    try:
        float(text)
        return True
    except ValueError:
        # pandas renames a repeated column name "1.5" to "1.5.1"
        base = re.sub(r"\.\d+$", "", text)
        try:
            float(base)
            return base != text
        except ValueError:
            return False


def _raw_lines(path: Path, count: int) -> list[str]:
    with path.open("rb") as f:
        head = f.read(64 * 1024)
    lines = head.decode("utf-8", errors="replace").splitlines()[:count]
    return [line if len(line) <= 400 else line[:400] + " …" for line in lines]


def _read_head(path: Path, file_format: str, options: IOOptions, rows: int) -> Any:
    """Read only the first rows where the format allows it."""
    import pandas as pd

    if file_format in {"csv", "tsv"}:
        return pd.read_csv(path, sep=_separator(file_format, options.input_separator),
                           header=0 if options.header else None, nrows=rows)
    if file_format == "jsonl" or (file_format == "json" and options.json_lines):
        return pd.read_json(path, lines=True, nrows=rows)
    if file_format == "parquet":
        import pyarrow.parquet as pq

        batch = next(pq.ParquetFile(path).iter_batches(batch_size=rows), None)
        if batch is None:
            return pq.read_schema(path).empty_table().to_pandas()
        return batch.to_pandas()
    if file_format == "excel":
        return pd.read_excel(path, nrows=rows)
    if file_format == "ipc":
        import pyarrow

        return read_data(path, "ipc", "pyarrow", options, pyarrow).slice(0, rows).to_pandas()
    return read_data(path, file_format, "pandas", options, pd).head(rows)


def _table(frame: Any) -> Table:
    return Table(
        columns=[str(c) for c in frame.columns],
        dtypes=[str(t) for t in frame.dtypes],
        rows=[["" if v is None else str(v) for v in row]
              for row in frame.astype(object).where(frame.notna(), None).values.tolist()],
    )


def _round_trip(frame: Any, output_format: str, options: IOOptions) -> Any:
    """Write the sample in the output format and read it back."""
    import pandas as pd
    import pyarrow

    if output_format in ENGINE_FORMATS["pandas"]:
        engine, backend, data = "pandas", pd, frame
    else:
        engine, backend = "pyarrow", pyarrow
        data = pyarrow.Table.from_pandas(frame, preserve_index=False)
    with tempfile.TemporaryDirectory(prefix="df2pq-preview-") as tmp:
        target = Path(tmp) / f"preview{OUTPUT_SUFFIX[output_format]}"
        write_data(data, target, output_format, engine, options, backend)
        # Read the output back with its own defaults, as a downstream user would.
        read_options = IOOptions(input_separator=options.output_separator, header=True,
                                 json_lines=options.json_lines)
        result = read_data(target, output_format, engine, read_options, backend)
        return result.to_pandas() if engine == "pyarrow" else result


def _check(frame: Any, raw: list[str], file_format: str, options: IOOptions) -> list[str]:
    warnings = []
    columns = [str(c) for c in frame.columns]
    if len(frame) == 0:
        warnings.append("No data rows were read from this file.")
    if (file_format in {"csv", "tsv"} and options.header and columns
            and all(_looks_numeric(c) for c in columns)):
        warnings.append(
            "Every column name is a number, so the first row is probably data, not a header. "
            "Converting with “Input has a header” on would turn that row into column names "
            "and drop it from every file. Turn the option off if this file has no header.")
    if file_format in {"csv", "tsv"} and not options.header and len(frame) > 1:
        first, rest = frame.iloc[0], frame.iloc[1:]
        header_like = sum(
            1 for c in frame.columns
            if not _looks_numeric(first[c]) and rest[c].map(_looks_numeric).all())
        if header_like and header_like >= len(frame.columns) / 2:
            warnings.append(
                "The first row is text while the rows below it are numbers, so it looks like "
                "a header. Turn on “Input has a header” if it holds column names.")
    if file_format in {"csv", "tsv"} and len(columns) == 1 and raw:
        current = _separator(file_format, options.input_separator)
        counts = {sep: raw[0].count(sep) for sep in SEPARATOR_NAMES if sep != current}
        best = max(counts, key=counts.get)
        if counts[best]:
            shown = "\\t" if best == "\t" else best
            warnings.append(
                f"Only one column was found, but the first line contains "
                f"{SEPARATOR_NAMES[best]}s. Set the input separator to “{shown}”.")
    return warnings


def preview_file(path: Path, input_format: str, output_format: str, options: IOOptions,
                 rows: int = 10) -> Preview:
    """Never raises: read errors are reported in ``Preview.error``."""
    raw = _raw_lines(path, rows + 1) if input_format in TEXT_FORMATS else []
    try:
        frame = _read_head(path, input_format, options, rows)
    except Exception as exc:
        return Preview(path, raw, None, None, error=f"Could not read the file: {exc}")
    before = None if raw else _table(frame)
    warnings = _check(frame, raw, input_format, options)
    try:
        after = _table(_round_trip(frame, output_format, options))
    except Exception as exc:
        return Preview(path, raw, before, None, warnings,
                       error=f"Could not write {output_format}: {exc}")
    return Preview(path, raw, before, after, warnings)
