# Changelog

All notable changes to df2pq are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and versions follow
[Semantic Versioning](https://semver.org/).

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
