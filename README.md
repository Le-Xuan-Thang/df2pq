# df2pq — Batch Data Converter

df2pq converts tabular files from one or more folders and recreates each
selected source folder below a destination folder. Nested subfolders are
preserved, source files are left untouched, and existing outputs are skipped
unless overwrite is enabled.

df2pq is released under the MIT License. See `LICENSE`.

## Installation

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # base install (pandas, pyarrow, NiceGUI)
uv sync --extra excel        # optional: Excel readers/writers
uv sync --extra gpu-cu13     # optional: cuDF for CUDA 13 (Python 3.11+)
```

cuDF is optional and must match the CUDA/RAPIDS version on your system; the
`gpu-cu13` extra targets CUDA 13.

## Desktop GUI

Launch the app from the repository root:

```bash
uv run df2pq                # NiceGUI interface (same as: uv run python -m df2pq)
uv run df2pq-tk             # legacy Tkinter interface
```

The interface is served on localhost by NiceGUI. It opens in a Chrome/Edge app
window when one is installed (otherwise your default browser) and the server
exits when that window is closed. Use `--no-window` to only print the URL.

Click the version button in the header to read the changelog ("What's new") or
check GitHub for a newer release ("Updates"). Checks run automatically at most
every 12 hours and can be switched off. For a clean git checkout, "Update now"
fast-forwards the repository and refreshes dependencies (keeping optional extras
such as cuDF); other installs show the command or release page to use. The
changelog lives in `df2pq/CHANGELOG.md`.

GPU notes: the GUI checks at startup that cuDF really runs and shows the result
in the header pill; cuDF becomes the default engine when it does. Files larger
than VRAM spill to host RAM, and a file the GPU cannot handle is retried on the
CPU automatically. cuDF needs several GB of disk, so if `uv sync --extra
gpu-cu13` fails with "No space left on device", free space on the drive that
holds `.venv` and re-run it.

Add each source folder, choose the output root, then select an engine and file
formats. The GUI includes a preview, progress reporting, cancellation, and
options for separators, headers, recursion, limits, JSON Lines, and overwriting.
The output layout is `OUTPUT_ROOT/SOURCE_FOLDER_NAME/...`.

## Command-line usage

Convert two CSV folders into Parquet (`df2pq-convert` is the installed alias for
`python -m df2pq.converter`):

```bash
uv run python -m df2pq.converter \
  --source "/data/campaign_07_23" \
  --source "/data/campaign_12_23" \
  --output "/data/Parquet" \
  --input-format csv \
  --output-format parquet \
  --engine pandas
```

The output paths will be `/data/Parquet/campaign_07_23/...` and
`/data/Parquet/campaign_12_23/...`, retaining subfolders below each source.
`--input-format auto` (the default) detects formats from file extensions and
can convert mixed-format input folders to one chosen output format. Use
`--dry-run` to preview file mappings, `--limit 5` for a small trial, and
`--overwrite` to replace existing outputs.

The formats available depend on the selected engine:

| Engine | Read/write formats |
| --- | --- |
| `pandas` | CSV, TSV, Parquet, Feather, JSON/JSONL, ORC, Excel, Pickle |
| `cudf` | CSV, TSV, Parquet, Feather, JSON/JSONL, ORC |
| `pyarrow` | CSV, TSV, Parquet, Feather, ORC, Arrow IPC (`.arrow`/`.ipc`) |

Install the corresponding libraries for the selected engine. Excel requires an
Excel reader/writer engine such as `openpyxl`. cuDF requires a compatible
RAPIDS/CUDA environment. JSON Lines files can be read or written with
`--json-lines` or select `jsonl` as the input/output format.

For headerless, semicolon-separated numeric CSV files:

```bash
uv run python -m df2pq.converter \
  --source "/data/recordings_a" \
  --source "/data/recordings_b" \
  --output "/data/Parquet" \
  --input-format csv \
  --output-format parquet \
  --engine pyarrow \
  --input-separator ";" \
  --no-header
```

Run `uv run df2pq-convert --help` for all CLI options. The legacy `df2pq-tk`
interface needs Tkinter; on Linux install the system Tk package (for example,
`python3-tk`) if it is missing.

## Tests

Run the standard suite with `uv run python -m unittest discover -s tests -v`.
The cuDF/GPU integration test is opt-in: prefix the command with
`DF2PQ_TEST_CUDF=1` on a machine with a working RAPIDS installation.

## Portable desktop builds

GitHub Actions builds these on pushes to `main` and version tags:

| Platform | Files |
| --- | --- |
| Windows x64 | `df2pq-windows-x86_64-setup.exe` installer, portable `.zip` |
| Linux x64 | `.deb`, `.rpm`, portable `.tar.gz` |
| macOS (Intel, Apple Silicon) | app `.zip` |

Pushing a tag such as `v0.1.0` also creates a GitHub Release with the downloads
attached. Install on Linux with `sudo apt install ./df2pq_*.deb` or
`sudo dnf install ./df2pq-*.rpm`; the app is then available as `df2pq` and in
the application menu.

To build locally, install the packaging extra and run PyInstaller:

```bash
uv sync --extra packaging --extra excel
uv run df2pq-build
```

Packaged and source apps automatically detect working NVIDIA GPUs at startup.
When GPU packages are missing, the app downloads a private Python 3.12 environment,
cuDF and CUDA runtime packages (several GB), then tests GPU computation before
showing the GPU engine. CPU conversion remains available during setup. Later
launches reuse the downloaded environment. An NVIDIA driver must already be
installed; the app does not change system drivers or require administrator access.
CUDA 13 is selected for driver 580.65.06 or newer; supported older drivers use
CUDA 12 (minimum 525.60.13). macOS and non-NVIDIA GPUs use CPU engines.

On Windows, cuDF runs through the default **WSL2 Linux distribution**. Install and
initialize it first (`wsl --install -d Ubuntu`); the app then downloads and manages
the GPU environment inside WSL2 and translates Windows file paths automatically.
Files must be accessible from WSL2. WSL1 and native Windows cuDF are unsupported.

Setup progress appears beside the GPU indicator, with a progress bar, the current
package’s download percentage and downloaded/total MB. Percentages are per package;
dependency resolution and installation show an indeterminate bar. Failures appear in the activity
log and indicator tooltip, with a **Retry GPU setup** button. Detailed package
installation output is saved in `setup.log` in the GPU data directory:
`~/.local/share/df2pq/gpu` on Linux (`XDG_DATA_HOME` is respected), or
`%LOCALAPPDATA%/df2pq/gpu` on Windows. Windows runtime packages are stored under
`~/.local/share/df2pq/gpu` inside WSL2. Fix network, disk-space or driver errors
and retry; GPU mode is enabled only after the self-test passes.

For terminal diagnostics, run `df2pq --gpu-status` (no downloads) or
`df2pq --setup-gpu` (install/repair and test). Rebuild the installers to include
this automatic setup feature; already downloaded older installers are unchanged.

The project also includes a verified converter for headerless nine-column
numeric CSV files (`convert_headerless_csv_to_parquet.py`), a compatibility
entry point (`convert_csv_to_parquet.py`), and a Parquet benchmark
(`check_parquet.py`). When installed, these are available as
`df2pq-headerless-csv` and `df2pq-check`.
