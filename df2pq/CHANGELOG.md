# Changelog

All notable changes to df2pq are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] — 2026-11-17

### Added
- The window is split into **Paths** (source and destination folders) and
  **Analysis** tabs. Analysis shows a sample file as stored (A, left) next to
  what the output file will contain (B, right: column names, types and rows),
  with the read and write options beside each side and updated live.
- Converting first checks one file and asks before running a batch whose
  settings look wrong: a data row about to become the column names, a likely
  header read as data, or a single column caused by the wrong separator.
- **Damaged rows** option for CSV/TSV: rows with the wrong number of fields or
  zero bytes (for example a recording cut off mid-write) are dropped, kept as
  empty values, or fail the file. Every engine behaves the same, and each case
  is listed in the Run log with its line numbers.

- **Parallel files** in the Run tab converts several files at once with PyArrow
  or pandas, one process each: 200 files of 9.5 MB took 1.4 s with 32 parallel
  files instead of 16.5 s one at a time.
- Engine benchmarks and advice on choosing an engine in `docs/BENCHMARKS.md`,
  reproducible with `tools/benchmark_engines.py`.

### Changed
- Headerless CSV/TSV columns read by PyArrow are named `0`, `1`, `2`… like
  pandas and cuDF name them, instead of `f0`, `f1`, `f2`…

### Fixed
- Hidden files and folders, such as macOS `._` metadata files, are no longer
  converted, offered as samples or checked before a run.
- With auto-detection, a file the engine cannot read (for example Excel with
  cuDF) is listed as ignored instead of stopping the whole batch.
- pandas now reads decimal numbers exactly; its default parser could differ in
  the last digit. The cuDF engine description notes the same limitation on GPU.
- The JSON Lines option only affects reading; `.json` output is always a JSON
  array and `.jsonl` output always one record per line.

## [0.1.1] — 2026-11-17

### Added
- Automatic NVIDIA GPU package download and self-test for packaged and source
  apps, using an app-managed Python/cuDF environment and Windows WSL2 support.
- GPU setup progress with live download percentage and MB per package, failure
  details, retry, and terminal GPU diagnostics.

### Fixed
- cuDF conversions no longer start a new GPU process for every file. One worker
  stays loaded for the session and is warmed by the startup GPU check, cutting
  roughly 3–4 seconds of cuDF import and CUDA start-up from each file.

## [0.1.0] — First release

### Added
- Batch conversion between CSV, TSV, Parquet, Feather, JSON, JSON Lines, ORC,
  Arrow IPC, Excel and Pickle; each source folder is mirrored under the output
  folder and originals are never modified.
- NiceGUI desktop-style interface, opened in a Chrome/Edge app window
  (`python -m df2pq`). The Tkinter interface remains as `df2pq-tk`.
- Smart folder browser with per-folder data-file counts, sizes and formats, a
  fuzzy filter, recent folders and "add every subfolder with data".
- GPU engine (cuDF): startup self-test, spill-to-RAM for files larger than VRAM
  and automatic per-file CPU fallback.
- Preview plan, cancel, progress with ETA, and colour-coded activity log.
- In-app changelog and update checker.
