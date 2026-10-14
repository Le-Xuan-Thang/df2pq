"""Cross-platform Tkinter desktop interface for df2pq."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .converter import (
    ENGINE_FORMATS,
    FORMAT_SUFFIXES,
    IOOptions,
    _load_engine,
    build_jobs,
    convert_one,
)
from .folder_picker import FolderPicker
from .gpu import GpuStatus, probe_cudf
from .scan import FolderSummary, human_size, summarize_folder
from .settings import Settings
from .theme import C, Card, Fonts, apply_theme

ENGINE_LABELS = {
    "cuDF · GPU": "cudf",
    "Pandas · CPU": "pandas",
    "PyArrow · CPU": "pyarrow",
}
ENGINE_HINTS = {
    "cudf": "Runs on the GPU. Files larger than VRAM spill to RAM; a file the GPU cannot "
            "handle is retried on the CPU automatically.",
    "pandas": "Most compatible engine — supports Excel and Pickle too.",
    "pyarrow": "Fast multi-threaded CPU engine with low memory use.",
}

FORMAT_LABELS = {
    "auto": "Auto-detect",
    "csv": "CSV",
    "tsv": "TSV",
    "parquet": "Parquet",
    "feather": "Feather",
    "json": "JSON",
    "jsonl": "JSON Lines",
    "orc": "ORC",
    "ipc": "Arrow IPC",
    "excel": "Excel",
    "pickle": "Pickle",
}


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {sec:02d}s" if minutes else f"{sec}s"


class ConverterApp:
    """Desktop UI that keeps Tk responsive while batches are converted."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("df2pq — Batch Data Converter")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.settings = Settings()
        self.fonts: Fonts = apply_theme(root)

        self.sources: list[Path] = []
        self.summaries: dict[tuple[Path, bool], FolderSummary] = {}
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.close_when_done = False
        self.advanced_open = False
        self.config_widgets: list[tk.Widget] = []
        self.gpu: GpuStatus | None = None
        self.output_auto = True
        self.run_started = 0.0

        self.engine_var = tk.StringVar(value="Pandas · CPU")
        self.input_format_var = tk.StringVar(value=FORMAT_LABELS["auto"])
        self.output_format_var = tk.StringVar(
            value=FORMAT_LABELS.get(self.settings.get("output_format", "parquet"), "Parquet")
        )
        self.output_dir_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        self.output_auto = not self.output_dir_var.get()
        self.input_separator_var = tk.StringVar()
        self.output_separator_var = tk.StringVar()
        self.limit_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.header_var = tk.BooleanVar(value=True)
        self.json_lines_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Add source folders to begin.")
        self.current_file_var = tk.StringVar(value="")
        self.summary_var = tk.StringVar(value="")
        self.layout_var = tk.StringVar(value="")
        self.engine_hint_var = tk.StringVar(value=ENGINE_HINTS["pandas"])

        self._build_ui()
        self._fit_window()
        self._refresh_engine_choices()
        self._refresh_format_choices()
        for var in (self.engine_var, self.input_format_var, self.output_format_var,
                    self.output_dir_var, self.limit_var):
            var.trace_add("write", lambda *_: self._on_settings_changed())
        self.recursive_var.trace_add("write", lambda *_: self._rescan_all())
        self._bind_shortcuts()
        self._on_settings_changed()
        root.after(80, self._poll_events)
        threading.Thread(target=self._probe_gpu, name="df2pq-gpu-probe", daemon=True).start()

    # ---- layout ---------------------------------------------------------
    def _fit_window(self) -> None:
        """Size the window from its content so nothing is clipped on HiDPI screens."""
        root, px = self.root, self.fonts.px
        root.update_idletasks()
        max_w, max_h = int(root.winfo_screenwidth() * 0.92), int(root.winfo_screenheight() * 0.92)
        width = min(max(root.winfo_reqwidth(), px(1100)), max_w)
        height = min(max(root.winfo_reqheight(), px(780)), max_h)
        root.geometry(f"{width}x{height}")
        root.minsize(min(px(900), width), min(px(640), height))

    def _build_ui(self) -> None:
        page = ttk.Frame(self.root, style="App.TFrame", padding=(24, 20))
        page.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2, minsize=self.fonts.px(400))
        page.rowconfigure(1, weight=3)
        page.rowconfigure(3, weight=2)

        header = ttk.Frame(page, style="App.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        header.columnconfigure(0, weight=1)
        titles = ttk.Frame(header, style="App.TFrame")
        titles.grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text="df2pq", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text="Batch-convert folders of tabular data. Your originals are "
                  "never modified.", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w")
        self.gpu_pill = ttk.Label(header, text="Checking GPU…", style="Busy.Pill.TLabel")
        self.gpu_pill.grid(row=0, column=1, sticky="e")

        self._build_sources(page)
        right = ttk.Frame(page, style="App.TFrame")
        right.grid(row=1, column=1, sticky="nsew", padx=(16, 0), pady=(0, 14))
        right.columnconfigure(0, weight=1)
        self._build_destination(right)
        self._build_format(right)
        self._build_run(page)
        self._build_activity(page)

    def _build_sources(self, page: ttk.Frame) -> None:
        card = Card(page, "Source folders", "1")
        card.grid(row=1, column=0, sticky="nsew", pady=(0, 14))
        card.body.rowconfigure(2, weight=1)

        bar = ttk.Frame(card.body, style="Card.TFrame")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        bar.columnconfigure(3, weight=1)
        self.add_source_button = ttk.Button(bar, text="＋  Add folders…", style="Secondary.TButton",
                                            command=self._add_sources)
        self.add_source_button.grid(row=0, column=0, padx=(0, 6))
        self.recent_button = ttk.Button(bar, text="Recent ▾", style="Quiet.TButton",
                                        command=self._show_recent)
        self.recent_button.grid(row=0, column=1, padx=(0, 6))
        self.remove_source_button = ttk.Button(bar, text="Remove", style="Quiet.TButton",
                                               command=self._remove_sources)
        self.remove_source_button.grid(row=0, column=2, padx=(0, 6))
        self.clear_source_button = ttk.Button(bar, text="Clear", style="Quiet.TButton",
                                              command=self._clear_sources)
        self.clear_source_button.grid(row=0, column=3, sticky="w")
        ttk.Label(bar, textvariable=self.summary_var, style="Muted.TLabel").grid(
            row=0, column=4, sticky="e")

        wrap = tk.Frame(card.body, background=C["border"], padx=1, pady=1)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.source_tree = ttk.Treeview(
            wrap, columns=("files", "size", "formats"), show="tree headings",
            selectmode="extended", style="Data.Treeview",
        )
        self.source_tree.heading("#0", text="FOLDER", anchor="w")
        self.source_tree.heading("files", text="DATA FILES", anchor="e")
        self.source_tree.heading("size", text="SIZE", anchor="e")
        self.source_tree.heading("formats", text="FORMATS", anchor="w")
        self.source_tree.column("#0", width=self.fonts.px(300), minwidth=self.fonts.px(160), stretch=True)
        self.source_tree.column("files", width=self.fonts.px(100), minwidth=self.fonts.px(80), stretch=False, anchor="e")
        self.source_tree.column("size", width=self.fonts.px(90), minwidth=self.fonts.px(70), stretch=False, anchor="e")
        self.source_tree.column("formats", width=self.fonts.px(170), minwidth=self.fonts.px(90), stretch=False, anchor="w")
        self.source_tree.tag_configure("empty", foreground=C["amber"])
        self.source_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.source_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.source_tree.configure(yscrollcommand=scroll.set)
        self.source_tree.bind("<Delete>", lambda _e: self._remove_sources())

        self.empty_state = ttk.Frame(wrap, style="Inset.TFrame")
        ttk.Label(self.empty_state, text="No source folders yet",
                  style="EmptyTitle.TLabel").pack(pady=(0, 4))
        ttk.Label(self.empty_state, text="Pick folders to convert. Subfolders are kept and "
                  "each folder's name is mirrored in the output.",
                  style="Empty.TLabel", wraplength=self.fonts.px(380), justify="center").pack(pady=(0, 14))
        self.empty_button = ttk.Button(self.empty_state, text="＋  Add folders…",
                                       style="Primary.TButton", command=self._add_sources)
        self.empty_button.pack()
        ttk.Label(self.empty_state, text="Shortcut: Ctrl+O", style="Empty.TLabel").pack(pady=(8, 0))
        self.empty_state.place(relx=0.5, rely=0.5, anchor="center")
        self.config_widgets.extend([self.add_source_button, self.recent_button,
                                    self.remove_source_button, self.clear_source_button,
                                    self.empty_button])

    def _build_destination(self, right: ttk.Frame) -> None:
        card = Card(right, "Destination", "2")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(card.body, text="OUTPUT ROOT FOLDER", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 5))
        row = ttk.Frame(card.body, style="Card.TFrame")
        row.grid(row=2, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(row, textvariable=self.output_dir_var)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.output_entry.bind("<Key>", lambda _e: setattr(self, "output_auto", False))
        self.output_button = ttk.Button(row, text="Browse…", style="Secondary.TButton",
                                        command=self._choose_output)
        self.output_button.grid(row=0, column=1)
        ttk.Label(card.body, textvariable=self.layout_var, style="Muted.TLabel",
                  wraplength=self.fonts.px(360), justify="left").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.config_widgets.extend([self.output_entry, self.output_button])

    def _build_format(self, right: ttk.Frame) -> None:
        card = Card(right, "Conversion", "3")
        card.grid(row=1, column=0, sticky="new")
        body = card.body
        ttk.Label(body, text="ENGINE", style="Field.TLabel").grid(row=1, column=0, sticky="w")
        self.engine_combo = ttk.Combobox(body, textvariable=self.engine_var, state="readonly")
        self.engine_combo.grid(row=2, column=0, sticky="ew", pady=(4, 2))
        ttk.Label(body, textvariable=self.engine_hint_var, style="Muted.TLabel",
                  wraplength=self.fonts.px(360), justify="left").grid(row=3, column=0, sticky="w", pady=(0, 12))

        pair = ttk.Frame(body, style="Card.TFrame")
        pair.grid(row=4, column=0, sticky="ew")
        pair.columnconfigure((0, 2), weight=1, uniform="fmt")
        ttk.Label(pair, text="READ", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(pair, text="WRITE", style="Field.TLabel").grid(row=0, column=2, sticky="w")
        self.input_combo = ttk.Combobox(pair, textvariable=self.input_format_var, state="readonly")
        self.input_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(pair, text="→", style="Card.TLabel", font=self.fonts.ui_font(13)).grid(
            row=1, column=1, padx=10, pady=(4, 0))
        self.output_combo = ttk.Combobox(pair, textvariable=self.output_format_var,
                                         state="readonly")
        self.output_combo.grid(row=1, column=2, sticky="ew", pady=(4, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._refresh_format_choices)

        self.advanced_toggle = ttk.Button(body, text="▸  Advanced options", style="Quiet.TButton",
                                          command=self._toggle_advanced)
        self.advanced_toggle.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.advanced_frame = ttk.Frame(body, style="Card.TFrame")
        self.advanced_frame.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.advanced_frame.columnconfigure((0, 1), weight=1, uniform="adv")
        checks = [
            ("Scan subfolders", self.recursive_var),
            ("Input has a header", self.header_var),
            ("JSON Lines", self.json_lines_var),
            ("Overwrite existing outputs", self.overwrite_var),
        ]
        self.advanced_widgets: list[tk.Widget] = []
        for index, (text, var) in enumerate(checks):
            check = ttk.Checkbutton(self.advanced_frame, text=text, variable=var)
            check.grid(row=index // 2, column=index % 2, sticky="w", pady=1)
            self.advanced_widgets.append(check)
        self.input_separator_entry = self._labeled_entry(
            "Input separator", self.input_separator_var, 2, 0)
        self.output_separator_entry = self._labeled_entry(
            "Output separator", self.output_separator_var, 2, 1)
        self.limit_entry = self._labeled_entry("File limit (blank = all)", self.limit_var, 3, 0)
        ttk.Label(self.advanced_frame, text="Blank separators mean comma for CSV, tab for TSV.",
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w",
                                             pady=(6, 0))
        self.config_widgets.extend([self.engine_combo, self.input_combo, self.output_combo,
                                    self.advanced_toggle, *self.advanced_widgets,
                                    self.input_separator_entry, self.output_separator_entry,
                                    self.limit_entry])
        self.advanced_frame.grid_remove()

    def _labeled_entry(self, label: str, variable: tk.StringVar, row: int, column: int
                       ) -> ttk.Entry:
        cell = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        cell.grid(row=row, column=column, sticky="ew", padx=(0, 8 if column == 0 else 0),
                  pady=(8, 0))
        cell.columnconfigure(0, weight=1)
        ttk.Label(cell, text=label, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(cell, textvariable=variable, width=10)
        entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        return entry

    def _build_run(self, page: ttk.Frame) -> None:
        card = Card(page, "Run", "4")
        card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        body = card.body
        body.columnconfigure(0, weight=1)
        top = ttk.Frame(body, style="Card.TFrame")
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        info = ttk.Frame(top, style="Card.TFrame")
        info.grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(info, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.current_file_var, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0))
        buttons = ttk.Frame(top, style="Card.TFrame")
        buttons.grid(row=0, column=1, sticky="e")
        self.preview_button = ttk.Button(buttons, text="Preview plan", style="Secondary.TButton",
                                         command=lambda: self._start(dry_run=True))
        self.preview_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ttk.Button(buttons, text="Cancel", style="Quiet.TButton",
                                        command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.convert_button = ttk.Button(buttons, text="Convert files  ▶", style="Primary.TButton",
                                         command=lambda: self._start(dry_run=False))
        self.convert_button.grid(row=0, column=2)
        self.progress = ttk.Progressbar(body, mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, sticky="ew", pady=(12, 0))

    def _build_activity(self, page: ttk.Frame) -> None:
        card = Card(page, "Activity")
        card.grid(row=3, column=0, columnspan=2, sticky="nsew")
        card.body.rowconfigure(1, weight=1)
        wrap = tk.Frame(card.body, background=C["border"], padx=1, pady=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log = tk.Text(
            wrap, height=6, wrap="word", state="disabled", background=C["field"],
            foreground=C["muted"], insertbackground=C["ink"], selectbackground=C["blue_soft"],
            selectforeground=C["ink"], relief="flat", borderwidth=0, padx=12, pady=8,
            font=self.fonts.mono_font(9), highlightthickness=0, spacing1=2,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        for tag, color in {"done": C["accent"], "cpu": C["amber"], "skip": C["faint"],
                           "fail": C["red"], "info": C["blue"]}.items():
            self.log.tag_configure(tag, foreground=color)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self._add_sources())
        self.root.bind("<Control-Return>", lambda _e: self._start(dry_run=False))

    # ---- engine / format state -----------------------------------------
    def _probe_gpu(self) -> None:
        self.events.put(("gpu", probe_cudf()))

    def _refresh_engine_choices(self) -> None:
        labels = [label for label, engine in ENGINE_LABELS.items()
                  if engine != "cudf" or (self.gpu is not None and self.gpu.available)]
        self.engine_combo.configure(values=tuple(labels))
        if self.engine_var.get() not in labels:
            self.engine_var.set("Pandas · CPU")

    def _on_gpu_probed(self, status: GpuStatus) -> None:
        self.gpu = status
        if status.available:
            self.gpu_pill.configure(text=f"●  GPU ready · {status.device}", style="Gpu.Pill.TLabel")
        else:
            self.gpu_pill.configure(text="●  CPU only", style="Cpu.Pill.TLabel")
            ENGINE_HINTS["cudf"] = status.detail
        self._refresh_engine_choices()
        saved = self.settings.get("engine", "cudf")
        if status.available and saved == "cudf" and self.engine_var.get() == "Pandas · CPU":
            self.engine_var.set("cuDF · GPU")
            self._refresh_format_choices()
        self._on_settings_changed()

    def _engine(self) -> str:
        return ENGINE_LABELS[self.engine_var.get()]

    def _refresh_format_choices(self, _event: Any = None) -> None:
        supported = ENGINE_FORMATS[self._engine()]
        input_formats = ["auto", *(fmt for fmt in FORMAT_SUFFIXES if fmt in supported)]
        output_formats = [fmt for fmt in FORMAT_SUFFIXES if fmt in supported]
        self.input_combo.configure(values=tuple(FORMAT_LABELS[fmt] for fmt in input_formats))
        self.output_combo.configure(values=tuple(FORMAT_LABELS[fmt] for fmt in output_formats))
        input_code = self._format_code(self.input_format_var.get())
        if input_code != "auto" and input_code not in supported:
            self.input_format_var.set(FORMAT_LABELS["auto"])
        if self._format_code(self.output_format_var.get()) not in supported:
            fallback = "parquet" if "parquet" in supported else output_formats[0]
            self.output_format_var.set(FORMAT_LABELS[fallback])

    @staticmethod
    def _format_code(label: str) -> str:
        return next((code for code, text in FORMAT_LABELS.items() if text == label), "auto")

    def _active_formats(self) -> frozenset[str]:
        """Source formats the current engine/READ choice would actually convert."""
        supported = ENGINE_FORMATS[self._engine()]
        code = self._format_code(self.input_format_var.get())
        return supported if code == "auto" else frozenset({code})

    def _on_settings_changed(self) -> None:
        self.engine_hint_var.set(ENGINE_HINTS.get(self._engine(), ""))
        self._render_sources()
        self._update_layout_hint()
        self._update_ready()

    def _update_layout_hint(self) -> None:
        out = self.output_dir_var.get().strip() or "OUTPUT"
        name = self.sources[0].name if self.sources else "SOURCE_NAME"
        self.layout_var.set(f"Files are written to  {out}/{name}/…  with subfolders kept.")

    def _update_ready(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        formats = self._active_formats()
        total = sum((s.files(formats) for s in self._current_summaries()), 0)
        size = sum((s.size(formats) for s in self._current_summaries()), 0)
        pending = len(self._current_summaries()) < len(self.sources)
        if self.sources:
            plus = "+" if any(s.truncated for s in self._current_summaries()) else ""
            self.summary_var.set(
                f"{len(self.sources)} folder{'s' if len(self.sources) != 1 else ''} · "
                + ("scanning…" if pending else f"{total:,}{plus} files · {human_size(size)}")
            )
        else:
            self.summary_var.set("")
        ready = bool(self.sources) and bool(self.output_dir_var.get().strip())
        state = "normal" if ready else "disabled"
        self.convert_button.configure(state=state)
        self.preview_button.configure(state=state)
        if not self.sources:
            self._set_status("Add source folders to begin.")
        elif not self.output_dir_var.get().strip():
            self._set_status("Choose an output folder to continue.", "warning")
        elif not pending and total == 0:
            self._set_status("No convertible files match the current READ format.", "warning")
        elif not pending:
            self._set_status(
                f"Ready — {total:,} file(s) will become "
                f"{FORMAT_LABELS[self._format_code(self.output_format_var.get())]}."
            )

    # ---- sources ---------------------------------------------------------
    def _current_summaries(self) -> list[FolderSummary]:
        recursive = self.recursive_var.get()
        return [s for p in self.sources if (s := self.summaries.get((p, recursive)))]

    def _add_sources(self) -> None:
        dialog = FolderPicker(self.root, self.fonts, self.settings,
                              formats=self._active_formats(), recursive=self.recursive_var.get())
        chosen = dialog.show()
        if chosen:
            self._add_paths(chosen)

    def _show_recent(self) -> None:
        recents = self.settings.recent("recent_sources")
        menu = tk.Menu(self.root, tearoff=False, font=self.fonts.ui_font(10))
        if not recents:
            menu.add_command(label="No recent folders yet", state="disabled")
        for path in recents:
            menu.add_command(label=f"{path.name or path}    {path.parent}",
                             command=lambda p=path: self._add_paths([p]))
        x = self.recent_button.winfo_rootx()
        y = self.recent_button.winfo_rooty() + self.recent_button.winfo_height()
        menu.tk_popup(x, y)

    def _add_paths(self, paths: list[Path]) -> None:
        added, skipped = 0, []
        names = {p.name for p in self.sources}
        for raw in paths:
            path = raw.expanduser().resolve()
            if not path.is_dir() or path in self.sources:
                continue
            if path.name in names:
                skipped.append(path.name)
                continue
            self.sources.append(path)
            names.add(path.name)
            self.settings.remember(path)
            added += 1
            self._scan_async(path)
            if self.output_auto and not self.output_dir_var.get():
                self.output_dir_var.set(str(path.parent / "df2pq_output"))
                self.output_auto = True
        self.settings.save()
        if skipped:
            messagebox.showwarning(
                "Duplicate folder names",
                "Folders need unique names so their outputs don't collide. Skipped: "
                + ", ".join(skipped), parent=self.root)
        self._render_sources()
        self._update_layout_hint()
        self._update_ready()

    def _scan_async(self, path: Path) -> None:
        recursive = self.recursive_var.get()
        if (path, recursive) in self.summaries:
            return

        def work() -> None:
            self.events.put(("summary", (path, recursive, summarize_folder(
                path, recursive=recursive, max_files=500_000))))

        threading.Thread(target=work, name="df2pq-scan", daemon=True).start()

    def _rescan_all(self) -> None:
        for path in self.sources:
            self._scan_async(path)
        self._render_sources()
        self._update_ready()

    def _remove_sources(self) -> None:
        selected = sorted((int(item) for item in self.source_tree.selection()), reverse=True)
        for index in selected:
            del self.sources[index]
        self._render_sources()
        self._update_layout_hint()
        self._update_ready()

    def _clear_sources(self) -> None:
        self.sources.clear()
        self._render_sources()
        self._update_layout_hint()
        self._update_ready()

    def _render_sources(self) -> None:
        self.source_tree.delete(*self.source_tree.get_children())
        formats = self._active_formats()
        recursive = self.recursive_var.get()
        for index, source in enumerate(self.sources):
            summary = self.summaries.get((source, recursive))
            if summary is None:
                values, tags = ("scanning…", "", ""), ()
            elif summary.files(formats) == 0:
                values, tags = ("none", "", summary.format_text() or "no data files"), ("empty",)
            else:
                values = (f"{summary.files(formats):,}{'+' if summary.truncated else ''}",
                          human_size(summary.size(formats)), summary.format_text())
                tags = ()
            self.source_tree.insert("", tk.END, iid=str(index),
                                    text=f"  {source.name or source}", values=values, tags=tags)
        if self.sources:
            self.empty_state.place_forget()
        else:
            self.empty_state.place(relx=0.5, rely=0.5, anchor="center")
            self.empty_state.lift()

    def _choose_output(self) -> None:
        start = Path(self.output_dir_var.get()).expanduser() if self.output_dir_var.get() else None
        dialog = FolderPicker(
            self.root, self.fonts, self.settings, formats=None, recursive=False,
            title="Choose the output root folder", multiple=False, action_label="Use",
            start=start if start and start.is_dir() else None)
        chosen = dialog.show()
        if chosen:
            self.output_auto = False
            self.output_dir_var.set(str(chosen[0]))

    def _toggle_advanced(self) -> None:
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.advanced_frame.grid()
            self.advanced_toggle.configure(text="▾  Advanced options")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_toggle.configure(text="▸  Advanced options")

    # ---- running ---------------------------------------------------------
    def _start(self, *, dry_run: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.sources or not self.output_dir_var.get().strip():
            return

        limit_text = self.limit_var.get().strip()
        try:
            limit = int(limit_text) if limit_text else None
            if limit is not None and limit < 1:
                raise ValueError("The file limit must be a positive integer.")
            for label, value in (("Input separator", self.input_separator_var.get()),
                                 ("Output separator", self.output_separator_var.get())):
                if value and len(value) != 1:
                    raise ValueError(f"{label} must be a single character.")
        except ValueError as exc:
            messagebox.showerror("Check conversion settings", str(exc), parent=self.root)
            return

        engine = self._engine()
        input_format = self._format_code(self.input_format_var.get())
        output_format = self._format_code(self.output_format_var.get())
        options = IOOptions(
            input_separator=self.input_separator_var.get() or None,
            output_separator=self.output_separator_var.get() or None,
            header=self.header_var.get(),
            json_lines=self.json_lines_var.get(),
        )
        self.settings.set("engine", engine)
        self.settings.set("output_format", output_format)
        self.settings.set("output_dir", self.output_dir_var.get().strip())
        self.settings.save()

        self.cancel_event.clear()
        self.close_when_done = False
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")
        self.current_file_var.set("")
        self._set_running(True)
        self._set_status("Scanning source folders…", "info")
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        self.progress.start(12)
        self.run_started = time.monotonic()

        self.worker = threading.Thread(
            target=self._worker_main,
            args=(tuple(self.sources), Path(self.output_dir_var.get()).expanduser(), input_format,
                  output_format, engine, options, self.recursive_var.get(), limit,
                  self.overwrite_var.get(), dry_run),
            name="df2pq-conversion",
            daemon=True,
        )
        self.worker.start()

    def _worker_main(self, sources: tuple[Path, ...], output: Path, input_format: str,
                     output_format: str, engine: str, options: IOOptions, recursive: bool,
                     limit: int | None, overwrite: bool, dry_run: bool) -> None:
        try:
            jobs = build_jobs(list(sources), output, input_format, output_format, engine,
                              recursive=recursive, limit=limit)
            self.events.put(("plan", len(jobs)))
            if dry_run:
                for index, job in enumerate(jobs, start=1):
                    if self.cancel_event.is_set():
                        self.events.put(("cancelled", (0, 0, 0)))
                        return
                    self.events.put(("preview", (index, len(jobs), job, output_format)))
                self.events.put(("done", (0, 0, 0, True)))
                return

            backend = _load_engine(engine)
            fallback = None
            if engine == "cudf":
                try:
                    fallback = ("pandas", _load_engine("pandas"))
                except RuntimeError:
                    pass
            converted = skipped = failed = 0
            for index, job in enumerate(jobs, start=1):
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", (converted, skipped, failed)))
                    return
                try:
                    result = convert_one(job, output_format, engine, options, backend,
                                         overwrite=overwrite, fallback=fallback)
                    if result == "skipped":
                        skipped += 1
                    else:
                        converted += 1
                    self.events.put(("file", (index, len(jobs), job, result, None)))
                except Exception as exc:
                    failed += 1
                    self.events.put(("file", (index, len(jobs), job, "failed",
                                              f"{type(exc).__name__}: {exc}")))
            self.events.put(("done", (converted, skipped, failed, False)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            getattr(self, f"_on_{kind}")(payload)
        try:
            self.root.after(80, self._poll_events)
        except tk.TclError:
            pass

    def _on_gpu(self, status: GpuStatus) -> None:
        self._on_gpu_probed(status)

    def _on_summary(self, payload: tuple[Path, bool, FolderSummary]) -> None:
        path, recursive, summary = payload
        self.summaries[(path, recursive)] = summary
        if path in self.sources and recursive == self.recursive_var.get():
            self._render_sources()
            self._update_ready()

    def _on_plan(self, total: int) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(total, 1), value=0)
        self._set_status(f"Processing {total:,} file(s)…", "info")

    def _on_preview(self, payload: tuple) -> None:
        index, total, job, output_format = payload
        self.progress.configure(value=index)
        self.current_file_var.set(f"Preview {index:,}/{total:,}  ·  {job.source.name}")
        self._append_log(f"{job.source}  →  {job.target}  ({job.input_format} → {output_format})",
                         "info")

    def _on_file(self, payload: tuple) -> None:
        index, total, job, result, error = payload
        self.progress.configure(value=index)
        elapsed = time.monotonic() - self.run_started
        eta = elapsed / index * (total - index)
        self.current_file_var.set(
            f"{index:,}/{total:,}  ·  {job.source.name}  ·  {_duration(elapsed)} elapsed"
            + (f"  ·  ~{_duration(eta)} left" if index < total else ""))
        if result == "failed":
            self._append_log(f"FAILED   {job.source}\n         {error}", "fail")
        elif result == "skipped":
            self._append_log(f"SKIPPED  {job.target}", "skip")
        elif result == "converted_cpu":
            self._append_log(f"DONE·CPU {job.target}  (GPU failed on this file)", "cpu")
        else:
            self._append_log(f"DONE     {job.target}", "done")

    def _finish_run(self, message: str, tone: str) -> None:
        self.progress.stop()
        self._append_log(message, "info")
        self.current_file_var.set("")
        self._set_running(False)
        self._set_status(message, tone)
        self._finish_close_if_requested()

    def _on_done(self, payload: tuple) -> None:
        converted, skipped, failed, dry_run = payload
        if dry_run:
            self._finish_run("Preview complete. No files were converted.", "info")
            return
        took = _duration(time.monotonic() - self.run_started)
        self._finish_run(
            f"Complete in {took} — {converted:,} converted, {skipped:,} skipped, "
            f"{failed:,} failed.", "success" if failed == 0 else "error")

    def _on_cancelled(self, payload: tuple) -> None:
        converted, skipped, failed = payload
        self._finish_run(
            f"Cancelled — {converted:,} converted, {skipped:,} skipped, {failed:,} failed.",
            "warning")

    def _on_error(self, message: str) -> None:
        self.progress.stop()
        self._append_log(f"ERROR  {message}", "fail")
        self._set_running(False)
        self._set_status("Could not start conversion.", "error")
        messagebox.showerror("Conversion could not start", message, parent=self.root)
        self._finish_close_if_requested()

    # ---- small helpers ----------------------------------------------------
    def _set_status(self, message: str, tone: str = "normal") -> None:
        colors = {"normal": C["ink"], "info": C["blue"], "success": C["accent"],
                  "warning": C["amber"], "error": C["red"]}
        icon = {"success": "✓  ", "warning": "!  ", "error": "✕  "}.get(tone, "")
        self.status_var.set(icon + message)
        ttk.Style(self.root).configure("Status.TLabel", foreground=colors.get(tone, C["ink"]))

    def _append_log(self, text: str, tag: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "101.0")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        for widget in self.config_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state="disabled" if running else "normal")
        self.source_tree.state(["disabled"] if running else ["!disabled"])
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.preview_button.configure(state="disabled")
            self.convert_button.configure(state="disabled")
        else:
            self._update_ready_after_run()

    def _update_ready_after_run(self) -> None:
        ready = bool(self.sources) and bool(self.output_dir_var.get().strip())
        state = "normal" if ready else "disabled"
        self.preview_button.configure(state=state)
        self.convert_button.configure(state=state)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self._set_status("Cancel requested — finishing the current file…", "warning")

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if messagebox.askyesno(
                "Conversion in progress",
                "Cancel the batch and close after the current file finishes?",
                parent=self.root,
            ):
                self.close_when_done = True
                self.cancel_event.set()
            return
        self.settings.save()
        self.root.destroy()

    def _finish_close_if_requested(self) -> None:
        if self.close_when_done:
            self.settings.save()
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
