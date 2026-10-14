"""NiceGUI desktop-style interface for df2pq (runs a local server, opens an app window)."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from typing import Any

from nicegui import Client, app, run, ui

from .converter import ENGINE_FORMATS, FORMAT_SUFFIXES, IOOptions
from .gpu import GpuStatus, probe_cudf
from .runner import BatchRunner
from .scan import FolderSummary, fuzzy_match, human_size, places, summarize_folder
from .settings import Settings

ENGINE_LABELS = {"cudf": "cuDF · GPU", "pandas": "Pandas · CPU", "pyarrow": "PyArrow · CPU"}
ENGINE_HINTS = {
    "cudf": "Runs on the GPU. Files larger than VRAM spill to RAM, and a file the GPU cannot "
            "handle is retried on the CPU automatically.",
    "pandas": "Most compatible engine — also reads and writes Excel and Pickle.",
    "pyarrow": "Fast multi-threaded CPU engine with low memory use.",
}
FORMAT_LABELS = {
    "auto": "Auto-detect", "csv": "CSV", "tsv": "TSV", "parquet": "Parquet", "feather": "Feather",
    "json": "JSON", "jsonl": "JSON Lines", "orc": "ORC", "ipc": "Arrow IPC", "excel": "Excel",
    "pickle": "Pickle",
}

CSS = """
:root { --df-font: Inter, "Segoe UI", system-ui, -apple-system, "Noto Sans", sans-serif;
        --df-mono: "JetBrains Mono", "Cascadia Mono", ui-monospace, "DejaVu Sans Mono", monospace;
        --bg:#0B1120; --card:#131C31; --card2:#18233C; --field:#0F1729; --line:#27344F;
        --ink:#F1F5F9; --muted:#94A3B8; --faint:#64748B; --accent:#22C55E; }
body, .q-field, .q-btn, .q-table, .q-item, .q-tab, .q-menu, .q-tooltip, .q-expansion-item,
.q-toggle__label, .q-checkbox__label, .q-badge { font-family: var(--df-font) !important; }
body { background: var(--bg) !important; color: var(--ink); font-size: 14px;
       -webkit-font-smoothing: antialiased; }
.df-card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; }
.df-title { font-size: 28px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; }
.df-muted { color: var(--muted); }
.df-faint { color: var(--faint); }
.df-label { color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: .08em;
            text-transform: uppercase; }
.df-step { background:#1B2D52; color:#60A5FA; font-size:11px; font-weight:700;
           border-radius:6px; padding:2px 8px; }
.df-card-title { font-size: 15px; font-weight: 650; }
.df-pill { border-radius: 999px; padding: 5px 14px; font-size: 12px; font-weight: 600; }
.df-pill-gpu { background:#10301F; color:#4ADE80; }
.df-pill-cpu { background:#18233C; color:#94A3B8; }
.df-pill-busy { background:#3A2E0E; color:#FBBF24; }
.df-log { font-family: var(--df-mono) !important; font-size: 12.5px; background: var(--field);
          border: 1px solid var(--line); border-radius: 10px; }
.q-table { background: transparent !important; color: var(--ink); }
.q-table thead th { color: var(--faint); font-size: 11px; font-weight: 700; letter-spacing: .08em;
                    background: var(--card2); }
.q-table tbody td { border-color: var(--line); }
.q-table__top, .q-table__bottom:not(.q-table__bottom--nodata) { display: none; }
.q-table__bottom--nodata { border-top: 0; min-height: 0; padding: 0; }
.df-table { background: var(--field) !important; border: 1px solid var(--line);
            border-radius: 10px; overflow: hidden; }
.q-field--outlined .q-field__control { background: var(--field); border-radius: 10px; }
.q-field--outlined .q-field__control:before { border-color: var(--line); }
.q-btn { text-transform: none !important; letter-spacing: 0 !important; font-weight: 600; }
.df-side-btn { justify-content: flex-start; width: 100%; color: var(--ink); }
.q-linear-progress { border-radius: 99px; }
.q-expansion-item__container, .q-expansion-item { background: transparent; }
.nicegui-content { padding: 0 !important; }
"""


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"


class State:
    """Single-user app state; the server only listens on localhost."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.runner = BatchRunner()
        self.sources: list[Path] = []
        self.summaries: dict[tuple[Path, bool], FolderSummary] = {}
        self.gpu: GpuStatus | None = None
        self.gpu_shown = False
        self.engine = "pandas"
        self.input_format = "auto"
        self.output_format = self.settings.get("output_format", "parquet")
        self.output_dir = self.settings.get("output_dir", "")
        self.auto_output = ""
        self.input_separator = ""
        self.output_separator = ""
        self.limit = ""
        self.recursive = True
        self.header = True
        self.json_lines = False
        self.overwrite = False
        self.run_started = 0.0
        self.busy = False

    def engine_options(self) -> dict[str, str]:
        gpu_ok = self.gpu is not None and self.gpu.available
        return {k: v for k, v in ENGINE_LABELS.items() if k != "cudf" or gpu_ok}

    def active_formats(self) -> frozenset[str]:
        supported = ENGINE_FORMATS[self.engine]
        return supported if self.input_format == "auto" else frozenset({self.input_format})

    def current_summaries(self) -> list[FolderSummary]:
        return [s for p in self.sources if (s := self.summaries.get((p, self.recursive)))]


state = State()


def format_options(engine: str) -> tuple[dict[str, str], dict[str, str]]:
    """(read options including auto-detect, write options) supported by ``engine``."""
    supported = ENGINE_FORMATS[engine]
    outs = {f: FORMAT_LABELS[f] for f in FORMAT_SUFFIXES if f in supported}
    return {"auto": FORMAT_LABELS["auto"], **outs}, outs


# ---- folder picker dialog ----------------------------------------------------------------
async def pick_folders(*, title: str, action: str, multiple: bool, formats: frozenset[str] | None,
                       recursive: bool, start: Path | None = None) -> list[Path] | None:
    ctx: dict[str, Any] = {"current": Path.home(), "children": [], "sums": {}, "gen": 0,
                           "dirty": False}
    recent_browse = state.settings.recent("recent_browse")
    first = start if start and start.is_dir() else (recent_browse[0] if recent_browse else None)

    with ui.dialog().props("no-shake") as dialog, ui.card().classes("df-card p-0 no-wrap").style(
            "width:min(1060px,95vw);max-width:none;height:min(720px,92vh)"):
        with ui.row().classes("w-full h-full no-wrap gap-0"):
            with ui.column().classes("gap-0 p-3").style(
                    "width:210px;border-right:1px solid var(--line);background:var(--card2)"):
                ui.label("Places").classes("df-label px-2 py-2")
                for label, path in places():
                    ui.button(label, icon="folder", on_click=lambda p=path: goto(p)).props(
                        "flat no-caps align=left dense").classes("df-side-btn")
                recents = state.settings.recent("recent_sources")[:6]
                if recents:
                    ui.label("Recent sources").classes("df-label px-2 pt-4 pb-2")
                    for path in recents:
                        ui.button(path.name or str(path), icon="history",
                                  on_click=lambda p=path: goto(p)).props(
                            "flat no-caps align=left dense").classes("df-side-btn").tooltip(str(path))
            with ui.column().classes("grow gap-3 p-5 h-full no-wrap").style("min-width:0"):
                ui.label(title).classes("df-card-title")
                with ui.row().classes("w-full no-wrap items-center gap-2"):
                    ui.button(icon="arrow_upward", on_click=lambda: goto(ctx["current"].parent)
                              ).props("flat round dense").tooltip("Parent folder")
                    path_input = ui.input(placeholder="Type or paste a folder path, then Enter"
                                          ).props("outlined dense").classes("grow")
                with ui.row().classes("w-full no-wrap items-center gap-3"):
                    filter_input = ui.input(
                        placeholder="Filter folders — 'cmp07' finds campaign_07_23").props(
                        "outlined dense clearable").classes("grow")
                    data_only = ui.checkbox("Only folders with data").props("dense")
                    hidden = ui.checkbox("Hidden").props("dense")
                columns = [
                    {"name": "name", "label": "FOLDER", "field": "name", "align": "left"},
                    {"name": "files", "label": "DATA FILES", "field": "files", "align": "right"},
                    {"name": "size", "label": "SIZE", "field": "size", "align": "right"},
                    {"name": "formats", "label": "FORMATS", "field": "formats", "align": "left"},
                ]
                table = ui.table(columns=columns, rows=[], row_key="path",
                                 selection="multiple" if multiple else "single",
                                 on_select=lambda _e: update_footer()).props(
                    "flat dense virtual-scroll hide-pagination :rows-per-page-options=[0]"
                ).classes("df-table w-full grow").style("min-height:0;overflow:auto")
                table.add_slot("body-cell-name", """
                    <q-td :props="props" :class="props.row.has ? '' : 'text-grey-6'">
                      <q-icon name="folder" size="18px" class="q-mr-sm"
                              :color="props.row.has ? 'green-4' : 'grey-7'" />{{ props.row.name }}
                    </q-td>""")
                with table.add_slot("no-data"):
                    ui.label("No matching folders.").classes("df-muted p-6")
                with ui.row().classes("w-full items-center no-wrap"):
                    info = ui.label().classes("df-muted grow")
                    all_button = None
                    if multiple:
                        all_button = ui.button("Add every subfolder with data",
                                               on_click=lambda: add_all()).props("outline no-caps")
                    ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat no-caps")
                    ok = ui.button(action, on_click=lambda: accept()).props(
                        "unelevated no-caps color=primary text-color=black")

    def count(p: Path) -> int:
        s = ctx["sums"].get(str(p))
        return s.files(formats) if s else 0

    def render() -> None:
        query = filter_input.value or ""
        rows = []
        for p in ctx["children"]:
            if query and not fuzzy_match(query, p.name):
                continue
            s = ctx["sums"].get(str(p))
            n = s.files(formats) if s else 0
            if data_only.value and s is not None and n == 0:
                continue
            if s is None:
                vals = ("…", "", "")
            elif n == 0:
                vals = ("—", "", "")
            else:
                vals = (f"{n:,}{'+' if s.truncated else ''}", human_size(s.size(formats)),
                        s.format_text())
            rows.append({"path": str(p), "name": p.name, "has": n > 0, "files": vals[0],
                         "size": vals[1], "formats": vals[2]})
        table.rows = rows
        table.update()
        update_footer()

    def update_footer() -> None:
        sel = table.selected
        here = ctx["sums"].get(str(ctx["current"]))
        if sel:
            total = sum(count(Path(r["path"])) for r in sel)
            info.set_text(f"{len(sel)} folder(s) selected · {total:,} data file(s)")
            ok.set_text(f"{action} {len(sel)} selected")
        else:
            ok.set_text(f"{action} this folder")
            if here is None:
                info.set_text(f"Scanning {ctx['current'].name or ctx['current']}…")
            else:
                n = here.files(formats)
                info.set_text(f"This folder: {n:,}{'+' if here.truncated else ''} data file(s), "
                              f"{human_size(here.size(formats))}" if n else
                              "No convertible files directly here — open a subfolder, or pick this one.")
        if all_button is not None:
            all_button.set_enabled(any(count(p) for p in ctx["children"]))

    async def scan(p: Path, gen: int, sem: asyncio.Semaphore) -> None:
        async with sem:
            if gen != ctx["gen"]:
                return
            s = await run.io_bound(summarize_folder, p, recursive=recursive, max_files=30_000,
                                   time_budget=1.5)
        if gen == ctx["gen"]:
            ctx["sums"][str(p)] = s
            ctx["dirty"] = True

    async def goto(path: Path) -> None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            return
        if not path.is_dir():
            info.set_text(f"Not a folder: {path}")
            return
        ctx["gen"] += 1
        gen = ctx["gen"]
        ctx.update(current=path, sums={})
        path_input.set_value(str(path))
        filter_input.set_value("")
        table.selected.clear()
        show_hidden = hidden.value

        def listing() -> list[Path]:
            try:
                return sorted((Path(e.path) for e in os.scandir(path)
                               if e.is_dir() and (show_hidden or not e.name.startswith("."))),
                              key=lambda p: p.name.casefold())
            except OSError:
                return []

        ctx["children"] = await run.io_bound(listing)
        render()
        sem = asyncio.Semaphore(4)
        for p in [path, *ctx["children"]]:
            asyncio.create_task(scan(p, gen, sem))

    def accept() -> None:
        sel = [Path(r["path"]) for r in table.selected]
        finish(sel or [ctx["current"]])

    def add_all() -> None:
        finish([p for p in ctx["children"] if count(p)])

    def finish(paths: list[Path]) -> None:
        state.settings.remember(ctx["current"], "recent_browse")
        dialog.submit(paths)

    async def on_path_enter() -> None:
        await goto(Path(os.path.expandvars(path_input.value.strip().strip('"'))))

    path_input.on("keydown.enter", on_path_enter)
    filter_input.on_value_change(lambda _e: render())
    data_only.on_value_change(lambda _e: render())
    hidden.on_value_change(lambda _e: goto(ctx["current"]))
    table.on("rowDblclick", lambda e: goto(Path(e.args[1]["path"])))

    def flush() -> None:
        if ctx["dirty"]:
            ctx["dirty"] = False
            render()

    ui.timer(0.25, flush)
    dialog.open()
    await goto(first if first and first.is_dir() else Path.home())
    result = await dialog
    dialog.delete()
    return result


# ---- main page ------------------------------------------------------------------------------
@ui.page("/")
def index() -> None:
    ui.add_css(CSS)
    ui.add_head_html('<meta name="color-scheme" content="dark">')
    ui.dark_mode(True)
    ui.colors(primary="#22C55E", secondary="#60A5FA", negative="#F87171", warning="#FBBF24")
    S = state
    w: dict[str, Any] = {}
    ins0, outs0 = format_options(S.engine)
    if S.output_format not in outs0:
        S.output_format = "parquet"

    with ui.column().classes("w-full gap-0 mx-auto").style("max-width:1480px;padding:26px 30px"):
        with ui.row().classes("w-full items-center justify-between no-wrap").style(
                "margin-bottom:20px"):
            with ui.column().classes("gap-1"):
                ui.label("df2pq").classes("df-title")
                ui.label("Batch-convert folders of tabular data. Your originals are never "
                         "modified.").classes("df-muted")
            w["gpu"] = ui.label("Checking GPU…").classes("df-pill df-pill-busy")

        with ui.row().classes("w-full no-wrap gap-5 items-stretch"):
            # 1 — sources
            with ui.column().classes("df-card grow gap-3 p-5").style("min-width:0;flex:3"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("1").classes("df-step")
                    ui.label("Source folders").classes("df-card-title")
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.button("Add folders", icon="add", on_click=lambda: add_sources()).props(
                        "unelevated no-caps color=primary text-color=black")
                    with ui.button("Recent", icon="history").props("flat no-caps") as recent_btn:
                        w["recent_menu"] = ui.menu()
                    recent_btn.on("click", lambda: fill_recent())
                    w["remove"] = ui.button("Remove", icon="remove_circle_outline",
                                            on_click=lambda: remove_selected()).props("flat no-caps")
                    ui.button("Clear", on_click=lambda: clear_sources()).props("flat no-caps")
                    ui.space()
                    w["summary"] = ui.label().classes("df-muted")
                columns = [
                    {"name": "folder", "label": "FOLDER", "field": "folder", "align": "left"},
                    {"name": "files", "label": "DATA FILES", "field": "files", "align": "right"},
                    {"name": "size", "label": "SIZE", "field": "size", "align": "right"},
                    {"name": "formats", "label": "FORMATS", "field": "formats", "align": "left"},
                ]
                w["table"] = ui.table(columns=columns, rows=[], row_key="path",
                                      selection="multiple").props("flat").classes(
                    "df-table w-full").style("min-height:260px")
                w["table"].add_slot("body-cell-folder", """
                    <q-td :props="props">
                      <div style="font-weight:600">{{ props.row.folder }}</div>
                      <div class="text-caption text-grey-6" style="word-break:break-all">
                        {{ props.row.path }}</div>
                    </q-td>""")
                w["table"].add_slot("body-cell-files", """
                    <q-td :props="props" :class="props.row.empty ? 'text-amber-5' : ''">
                      {{ props.row.files }}</q-td>""")
                with w["table"].add_slot("no-data"):
                    with ui.column().classes("w-full items-center gap-2 py-10"):
                        ui.icon("folder_open", size="52px").classes("df-faint")
                        ui.label("No source folders yet").classes("df-card-title")
                        ui.label("Pick folders to convert. Subfolders are kept, and each "
                                 "folder's name is mirrored in the output.").classes(
                            "df-muted text-center").style("max-width:380px")
                        ui.button("Add folders", icon="add", on_click=lambda: add_sources()
                                  ).props("unelevated no-caps color=primary text-color=black")

            # 2 + 3 — destination and conversion
            with ui.column().classes("gap-5").style("flex:2;min-width:380px"):
                with ui.column().classes("df-card w-full gap-2 p-5"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("2").classes("df-step")
                        ui.label("Destination").classes("df-card-title")
                    ui.label("Output root folder").classes("df-label")
                    with ui.row().classes("w-full no-wrap items-center gap-2"):
                        w["output"] = ui.input(value=S.output_dir,
                                               placeholder="Where converted files go").props(
                            "outlined dense").classes("grow")
                        ui.button("Browse", icon="folder_open", on_click=lambda: choose_output()
                                  ).props("outline no-caps")
                    w["layout"] = ui.label().classes("df-muted text-caption")

                with ui.column().classes("df-card w-full gap-2 p-5"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("3").classes("df-step")
                        ui.label("Conversion").classes("df-card-title")
                    ui.label("Engine").classes("df-label")
                    w["engine"] = ui.select(S.engine_options(), value=S.engine).props(
                        "outlined dense options-dense").classes("w-full")
                    w["engine_hint"] = ui.label().classes("df-muted text-caption")
                    with ui.row().classes("w-full no-wrap items-end gap-2 pt-2"):
                        with ui.column().classes("grow gap-1"):
                            ui.label("Read").classes("df-label")
                            w["input_format"] = ui.select(ins0, value=S.input_format).props(
                                "outlined dense options-dense").classes("w-full")
                        ui.icon("arrow_forward").classes("df-faint pb-2")
                        with ui.column().classes("grow gap-1"):
                            ui.label("Write").classes("df-label")
                            w["output_format"] = ui.select(outs0, value=S.output_format).props(
                                "outlined dense options-dense").classes("w-full")
                    with ui.expansion("Advanced options", icon="tune").classes("w-full pt-2"):
                        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1"):
                            w["recursive"] = ui.checkbox("Scan subfolders", value=True).props("dense")
                            w["header"] = ui.checkbox("Input has a header", value=True).props("dense")
                            w["json_lines"] = ui.checkbox("JSON Lines").props("dense")
                            w["overwrite"] = ui.checkbox("Overwrite existing").props("dense")
                            w["in_sep"] = ui.input("Input separator").props("outlined dense")
                            w["out_sep"] = ui.input("Output separator").props("outlined dense")
                            w["limit"] = ui.input("File limit (blank = all)").props("outlined dense")
                        ui.label("Blank separators mean comma for CSV, tab for TSV.").classes(
                            "df-muted text-caption pt-1")

        # 4 — run
        with ui.column().classes("df-card w-full gap-3 p-5").style("margin-top:20px"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("4").classes("df-step")
                    ui.label("Run").classes("df-card-title")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    w["preview"] = ui.button("Preview plan", on_click=lambda: start(True)).props(
                        "outline no-caps")
                    w["cancel"] = ui.button("Cancel", on_click=lambda: S.runner.cancel()).props(
                        "flat no-caps")
                    w["convert"] = ui.button("Convert files", icon="play_arrow",
                                             on_click=lambda: start(False)).props(
                        "unelevated no-caps color=primary text-color=black").style("padding:6px 22px")
            w["status"] = ui.label().classes("text-base font-medium")
            w["detail"] = ui.label().classes("df-muted text-caption")
            w["progress"] = ui.linear_progress(value=0, show_value=False, size="8px").props(
                "rounded color=primary track-color=grey-9")
            w["log"] = ui.log(max_lines=500).classes("df-log w-full").style("height:170px")

    # ---- behaviour ---------------------------------------------------------------------
    def set_status(text: str, tone: str = "") -> None:
        colors = {"": "#F1F5F9", "info": "#60A5FA", "ok": "#4ADE80", "warn": "#FBBF24",
                  "err": "#F87171"}
        w["status"].set_text(text)
        w["status"].style(f"color:{colors[tone]}")

    def refresh_formats() -> None:
        ins, outs = format_options(S.engine)
        if S.input_format not in ins:
            S.input_format = "auto"
        if S.output_format not in outs:
            S.output_format = "parquet" if "parquet" in outs else next(iter(outs))
        w["input_format"].set_options(ins, value=S.input_format)
        w["output_format"].set_options(outs, value=S.output_format)

    def refresh() -> None:
        formats = S.active_formats()
        rows = []
        for p in S.sources:
            s = S.summaries.get((p, S.recursive))
            if s is None:
                rows.append({"path": str(p), "folder": p.name or str(p), "files": "scanning…",
                             "size": "", "formats": "", "empty": False})
            elif s.files(formats) == 0:
                rows.append({"path": str(p), "folder": p.name or str(p), "files": "none",
                             "size": "", "formats": s.format_text() or "no data files",
                             "empty": True})
            else:
                rows.append({"path": str(p), "folder": p.name or str(p),
                             "files": f"{s.files(formats):,}{'+' if s.truncated else ''}",
                             "size": human_size(s.size(formats)), "formats": s.format_text(),
                             "empty": False})
        w["table"].rows = rows
        w["table"].update()

        sums = S.current_summaries()
        total = sum(s.files(formats) for s in sums)
        pending = len(sums) < len(S.sources)
        if S.sources:
            w["summary"].set_text(
                f"{len(S.sources)} folder{'s' if len(S.sources) != 1 else ''} · "
                + ("scanning…" if pending else
                   f"{total:,} files · {human_size(sum(s.size(formats) for s in sums))}"))
        else:
            w["summary"].set_text("")
        w["engine_hint"].set_text(ENGINE_HINTS[S.engine] if S.engine != "cudf" or
                                  (S.gpu and S.gpu.available) else S.gpu.detail)
        out = S.output_dir.strip() or "OUTPUT"
        name = S.sources[0].name if S.sources else "SOURCE_NAME"
        w["layout"].set_text(f"Files are written to  {out}/{name}/…  with subfolders kept.")

        ready = bool(S.sources) and bool(S.output_dir.strip()) and not S.busy
        w["convert"].set_enabled(ready)
        w["preview"].set_enabled(ready)
        w["cancel"].set_enabled(S.busy)
        if S.busy:
            return
        if not S.sources:
            set_status("Add source folders to begin.")
        elif not S.output_dir.strip():
            set_status("Choose an output folder to continue.", "warn")
        elif not pending and total == 0:
            set_status("No convertible files match the current Read format.", "warn")
        elif not pending:
            set_status(f"Ready — {total:,} file(s) will become {FORMAT_LABELS[S.output_format]}.")

    def scan_async(path: Path) -> None:
        key = (path, S.recursive)
        if key in S.summaries:
            return

        async def work() -> None:
            S.summaries[key] = await run.io_bound(summarize_folder, path,
                                                  recursive=key[1], max_files=500_000)
            refresh()

        asyncio.create_task(work())

    def add_paths(paths: list[Path]) -> None:
        names = {p.name for p in S.sources}
        skipped = []
        for raw in paths:
            path = raw.expanduser().resolve()
            if not path.is_dir() or path in S.sources:
                continue
            if path.name in names:
                skipped.append(path.name)
                continue
            S.sources.append(path)
            names.add(path.name)
            S.settings.remember(path)
            scan_async(path)
            if not S.output_dir or S.output_dir == S.auto_output:
                S.auto_output = str(path.parent / "df2pq_output")
                w["output"].set_value(S.auto_output)
        S.settings.save()
        if skipped:
            ui.notify("Folder names must be unique; skipped: " + ", ".join(skipped),
                      type="warning")
        refresh()

    async def add_sources() -> None:
        chosen = await pick_folders(title="Add source folders", action="Add", multiple=True,
                                    formats=S.active_formats(), recursive=S.recursive)
        if chosen:
            add_paths(chosen)

    def fill_recent() -> None:
        w["recent_menu"].clear()
        with w["recent_menu"]:
            recents = S.settings.recent("recent_sources")
            if not recents:
                ui.menu_item("No recent folders yet").props("disable")
            for p in recents:
                ui.menu_item(f"{p.name or p}   —   {p.parent}",
                             on_click=lambda p=p: add_paths([p]))

    def remove_selected() -> None:
        gone = {r["path"] for r in w["table"].selected}
        S.sources[:] = [p for p in S.sources if str(p) not in gone]
        w["table"].selected.clear()
        refresh()

    def clear_sources() -> None:
        S.sources.clear()
        w["table"].selected.clear()
        refresh()

    async def choose_output() -> None:
        current = Path(S.output_dir).expanduser() if S.output_dir else None
        chosen = await pick_folders(title="Choose the output root folder", action="Use",
                                    multiple=False, formats=None, recursive=False, start=current)
        if chosen:
            w["output"].set_value(str(chosen[0]))

    def on_engine(e: Any) -> None:
        if e.value and e.value != S.engine:
            S.engine = e.value
            refresh_formats()
            refresh()

    def on_field(attr: str):
        def handler(e: Any) -> None:
            setattr(S, attr, e.value if e.value is not None else "")
            if attr == "recursive":
                for p in S.sources:
                    scan_async(p)
            refresh()
        return handler

    w["engine"].on_value_change(on_engine)
    w["input_format"].on_value_change(on_field("input_format"))
    w["output_format"].on_value_change(on_field("output_format"))
    w["output"].on_value_change(on_field("output_dir"))
    for key, attr in (("recursive", "recursive"), ("header", "header"),
                      ("json_lines", "json_lines"), ("overwrite", "overwrite"),
                      ("in_sep", "input_separator"), ("out_sep", "output_separator"),
                      ("limit", "limit")):
        w[key].on_value_change(on_field(attr))

    def start(dry_run: bool) -> None:
        if S.runner.running or not S.sources or not S.output_dir.strip():
            return
        try:
            limit = int(S.limit.strip()) if S.limit.strip() else None
            if limit is not None and limit < 1:
                raise ValueError("The file limit must be a positive integer.")
            for label, value in (("Input separator", S.input_separator),
                                 ("Output separator", S.output_separator)):
                if value and len(value) != 1:
                    raise ValueError(f"{label} must be a single character.")
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        S.settings.set("output_format", S.output_format)
        S.settings.set("output_dir", S.output_dir.strip())
        S.settings.set("engine", S.engine)
        S.settings.save()
        w["log"].clear()
        S.busy = True
        S.run_started = time.monotonic()
        w["progress"].props("indeterminate")
        w["detail"].set_text("")
        set_status("Scanning source folders…", "info")
        refresh()
        S.runner.start(
            sources=list(S.sources), output=Path(S.output_dir).expanduser(),
            input_format=S.input_format, output_format=S.output_format, engine=S.engine,
            options=IOOptions(input_separator=S.input_separator or None,
                              output_separator=S.output_separator or None,
                              header=S.header, json_lines=S.json_lines),
            recursive=S.recursive, limit=limit, overwrite=S.overwrite, dry_run=dry_run)

    def finish_run(message: str, tone: str) -> None:
        S.busy = False
        w["progress"].props(remove="indeterminate")
        w["log"].push(message, classes="text-blue-400")
        w["detail"].set_text("")
        refresh()
        set_status(message, tone)

    def handle(kind: str, payload: Any) -> None:
        if kind == "plan":
            w["progress"].props(remove="indeterminate")
            w["progress"].set_value(0)
            set_status(f"Processing {payload:,} file(s)…", "info")
        elif kind == "preview":
            index, total, job, fmt = payload
            w["progress"].set_value(index / total)
            w["detail"].set_text(f"Preview {index:,}/{total:,} · {job.source.name}")
            w["log"].push(f"{job.source}  →  {job.target}  ({job.input_format} → {fmt})",
                          classes="text-blue-400")
        elif kind == "file":
            index, total, job, result, error = payload
            w["progress"].set_value(index / total)
            elapsed = time.monotonic() - S.run_started
            eta = f" · ~{human_duration(elapsed / index * (total - index))} left" if index < total else ""
            w["detail"].set_text(f"{index:,}/{total:,} · {job.source.name} · "
                                 f"{human_duration(elapsed)} elapsed{eta}")
            if result == "failed":
                w["log"].push(f"FAILED   {job.source}\n         {error}", classes="text-red-400")
            elif result == "skipped":
                w["log"].push(f"SKIPPED  {job.target}", classes="text-slate-500")
            elif result == "converted_cpu":
                w["log"].push(f"DONE·CPU {job.target}  (GPU failed on this file)",
                              classes="text-amber-400")
            else:
                w["log"].push(f"DONE     {job.target}", classes="text-green-400")
        elif kind == "done":
            converted, skipped, failed, dry = payload
            if dry:
                finish_run("Preview complete. No files were converted.", "info")
            else:
                took = human_duration(time.monotonic() - S.run_started)
                w["progress"].set_value(1)
                finish_run(f"Complete in {took} — {converted:,} converted, {skipped:,} skipped, "
                           f"{failed:,} failed.", "ok" if failed == 0 else "err")
        elif kind == "cancelled":
            c, s, f = payload
            finish_run(f"Cancelled — {c:,} converted, {s:,} skipped, {f:,} failed.", "warn")
        elif kind == "error":
            finish_run("Could not start conversion.", "err")
            w["log"].push(f"ERROR  {payload}", classes="text-red-400")
            ui.notify(str(payload), type="negative", multi_line=True)

    def show_gpu() -> None:
        g = S.gpu
        S.gpu_shown = True
        if g.available:
            w["gpu"].set_text(f"●  GPU ready · {g.device}")
            w["gpu"].classes(replace="df-pill df-pill-gpu")
        else:
            w["gpu"].set_text("●  CPU only")
            w["gpu"].classes(replace="df-pill df-pill-cpu")
            w["gpu"].tooltip(g.detail)
        opts = S.engine_options()
        saved = S.settings.get("engine", "cudf")
        target = "cudf" if g.available and saved == "cudf" else S.engine
        w["engine"].set_options(opts, value=target)
        if target != S.engine:
            S.engine = target
            refresh_formats()
        refresh()

    def poll() -> None:
        if S.gpu is not None and not S.gpu_shown:
            show_gpu()
        try:
            for _ in range(200):
                kind, payload = S.runner.events.get_nowait()
                handle(kind, payload)
        except Exception:
            pass

    refresh_formats()
    if S.gpu is not None:
        show_gpu()
    refresh()
    ui.timer(0.15, poll)


# ---- launcher ------------------------------------------------------------------------------
def _find_browser() -> str | None:
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
             "microsoft-edge", "microsoft-edge-stable", "msedge")
    for name in names:
        if found := shutil.which(name):
            return found
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((c for c in candidates if Path(c).exists()), None)


def _open_window(url: str) -> None:
    browser = _find_browser()
    if browser:
        profile = state.settings.path.parent / "browser-profile"
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1360,960",
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="df2pq desktop interface")
    parser.add_argument("--port", type=int, default=0, help="Port to listen on (default: free port).")
    parser.add_argument("--no-window", action="store_true",
                        help="Only start the server and print its URL.")
    args = parser.parse_args(argv)
    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}"
    use_window = not args.no_window

    def on_startup() -> None:
        threading.Thread(target=lambda: setattr(state, "gpu", probe_cudf()),
                         name="df2pq-gpu-probe", daemon=True).start()
        print(f"df2pq running at {url}", flush=True)
        if use_window:
            _open_window(url)

    async def quit_when_window_closes() -> None:
        if not use_window:
            return
        await asyncio.sleep(6)  # a page reload reconnects within this grace period
        while state.runner.running:
            await asyncio.sleep(3)
        if not any(c.has_socket_connection for c in Client.instances.values()):
            state.settings.save()
            app.shutdown()

    app.on_startup(on_startup)
    app.on_disconnect(quit_when_window_closes)
    ui.run(host="127.0.0.1", port=port, title="df2pq — Batch Data Converter", dark=True,
           reload=False, show=False, favicon="🗂️")


if __name__ in {"__main__", "__mp_main__"}:
    main()
