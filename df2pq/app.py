"""Cross-platform Tkinter desktop interface for df2pq."""

from __future__ import annotations

import importlib.util
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .converter import (
    ENGINE_FORMATS,
    FORMAT_SUFFIXES,
    IOOptions,
    _load_engine,
    build_jobs,
    convert_one,
)


ENGINE_LABELS = {
    "Pandas · CPU": "pandas",
    "cuDF · GPU": "cudf",
    "PyArrow": "pyarrow",
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

COLORS = {
    "background": "#F3F6FB",
    "surface": "#FFFFFF",
    "surface_alt": "#F8FAFD",
    "ink": "#18263B",
    "muted": "#53647A",
    "border": "#D7E0EC",
    "blue": "#1D4ED8",
    "blue_hover": "#1E40AF",
    "blue_soft": "#E8F0FF",
    "green": "#16715F",
    "green_soft": "#E7F5F0",
    "amber": "#925B08",
    "amber_soft": "#FFF4D8",
    "red": "#B42318",
    "red_soft": "#FEECEB",
}


def _available_engine_labels() -> tuple[str, ...]:
    """Only offer optional backends that exist in this Python installation."""
    available = []
    for label, engine in ENGINE_LABELS.items():
        if engine == "cudf":
            try:
                if importlib.util.find_spec("cudf") is None:
                    continue
            except (ImportError, ValueError):
                continue
        available.append(label)
    return tuple(available)


class ConverterApp:
    """Desktop UI that keeps Tk responsive while batches are converted."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("df2pq — Batch Data Converter")
        self.root.geometry("1040x820")
        self.root.minsize(900, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.sources: list[Path] = []
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.close_when_done = False
        self.advanced_open = False
        self.config_widgets: list[tk.Widget] = []
        self.engine_choices = _available_engine_labels()

        self.engine_var = tk.StringVar(value="Pandas · CPU")
        self.input_format_var = tk.StringVar(value=FORMAT_LABELS["auto"])
        self.output_format_var = tk.StringVar(value=FORMAT_LABELS["parquet"])
        self.output_dir_var = tk.StringVar()
        self.input_separator_var = tk.StringVar()
        self.output_separator_var = tk.StringVar()
        self.limit_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.header_var = tk.BooleanVar(value=True)
        self.json_lines_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.source_count_var = tk.StringVar(value="0 folders")
        self.status_var = tk.StringVar(value="Add source folders to begin.")
        self.current_file_var = tk.StringVar(value="")

        self._configure_theme()
        self._build_ui()
        self._refresh_format_choices()
        self.root.after(100, self._poll_events)

    def _configure_theme(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        c = COLORS
        style.configure("App.TFrame", background=c["background"])
        style.configure("Card.TFrame", background=c["surface"])
        style.configure("TLabel", background=c["background"], foreground=c["ink"])
        style.configure("Card.TLabel", background=c["surface"], foreground=c["ink"])
        style.configure("Muted.TLabel", background=c["surface"], foreground=c["muted"])
        style.configure("PageTitle.TLabel", background=c["background"], foreground=c["ink"],
                        font=("TkDefaultFont", 22, "bold"))
        style.configure("Eyebrow.TLabel", background=c["background"], foreground=c["blue"],
                        font=("TkDefaultFont", 9, "bold"))
        style.configure("Badge.TLabel", background=c["green_soft"], foreground=c["green"],
                        padding=(9, 5), font=("TkDefaultFont", 9, "bold"))
        style.configure("Count.TLabel", background=c["blue_soft"], foreground=c["blue"],
                        padding=(8, 4), font=("TkDefaultFont", 9, "bold"))

        style.configure("Card.TLabelframe", background=c["surface"], foreground=c["ink"],
                        bordercolor=c["border"], relief="solid", borderwidth=1, padding=12)
        style.configure("Card.TLabelframe.Label", background=c["surface"], foreground=c["ink"],
                        font=("TkDefaultFont", 10, "bold"))
        style.configure("TEntry", padding=(8, 7), fieldbackground=c["surface"],
                        foreground=c["ink"], insertcolor=c["ink"], bordercolor=c["border"])
        style.map("TEntry", fieldbackground=[("disabled", c["surface_alt"])],
                  bordercolor=[("focus", c["blue"]), ("disabled", c["border"])])
        style.configure("TCombobox", padding=(8, 6), fieldbackground=c["surface"],
                        foreground=c["ink"], arrowcolor=c["muted"], bordercolor=c["border"])
        style.map("TCombobox", fieldbackground=[("readonly", c["surface"]),
                                                ("disabled", c["surface_alt"])],
                  bordercolor=[("focus", c["blue"]), ("disabled", c["border"])])
        style.configure("TCheckbutton", background=c["surface"], foreground=c["ink"],
                        padding=(2, 4))
        style.map("TCheckbutton", foreground=[("disabled", c["muted"])])

        style.configure("Primary.TButton", background=c["blue"], foreground="#FFFFFF",
                        padding=(18, 10), bordercolor=c["blue"], font=("TkDefaultFont", 10, "bold"))
        style.map("Primary.TButton",
                  background=[("disabled", "#AAB9D0"), ("pressed", c["blue_hover"]),
                              ("active", c["blue_hover"])],
                  foreground=[("disabled", "#FFFFFF")],
                  bordercolor=[("focus", c["amber"])])
        style.configure("Secondary.TButton", background=c["surface"], foreground=c["ink"],
                        padding=(12, 8), bordercolor=c["border"])
        style.map("Secondary.TButton",
                  background=[("disabled", c["surface_alt"]), ("pressed", c["blue_soft"]),
                              ("active", c["surface_alt"])],
                  foreground=[("disabled", c["muted"])],
                  bordercolor=[("focus", c["amber"])])
        style.configure("Quiet.TButton", background=c["surface"], foreground=c["muted"],
                        padding=(9, 7), bordercolor=c["surface"])
        style.map("Quiet.TButton", background=[("active", c["surface_alt"])],
                  bordercolor=[("focus", c["amber"])])

        style.configure("Folders.Treeview", background=c["surface"], fieldbackground=c["surface"],
                        foreground=c["ink"], rowheight=30, borderwidth=0)
        style.configure("Folders.Treeview.Heading", background=c["surface_alt"],
                        foreground=c["muted"], font=("TkDefaultFont", 9, "bold"), relief="flat")
        style.map("Folders.Treeview", background=[("selected", c["blue_soft"])],
                  foreground=[("selected", c["ink"])])
        style.configure("Accent.Horizontal.TProgressbar", troughcolor=c["surface_alt"],
                        background=c["blue"], bordercolor=c["border"], lightcolor=c["blue"],
                        darkcolor=c["blue"], thickness=10)
        style.configure("Status.TLabel", background=c["surface"], foreground=c["ink"],
                        font=("TkDefaultFont", 10, "bold"))
        style.configure("Success.TLabel", background=c["green_soft"], foreground=c["green"],
                        padding=(8, 5), font=("TkDefaultFont", 10, "bold"))
        style.configure("Warning.TLabel", background=c["amber_soft"], foreground=c["amber"],
                        padding=(8, 5), font=("TkDefaultFont", 10, "bold"))
        style.configure("Error.TLabel", background=c["red_soft"], foreground=c["red"],
                        padding=(8, 5), font=("TkDefaultFont", 10, "bold"))

    def _build_ui(self) -> None:
        page = ttk.Frame(self.root, padding=(22, 18), style="App.TFrame")
        page.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        page.rowconfigure(4, weight=1)

        header = ttk.Frame(page, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        title_block = ttk.Frame(header, style="App.TFrame")
        title_block.grid(row=0, column=0, sticky="w")
        ttk.Label(title_block, text="DF2PQ  /  DATA WORKFLOW", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 3)
        )
        ttk.Label(title_block, text="Batch data converter", style="PageTitle.TLabel").grid(
            row=1, column=0, sticky="w"
        )
        ttk.Label(
            title_block,
            text="Convert folders of tabular data while keeping your originals safe.",
            style="TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))
        ttk.Label(header, text="ORIGINALS STAY UNCHANGED", style="Badge.TLabel").grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )

        self._build_sources_card(page)
        self._build_destination_and_format_cards(page)
        self._build_run_card(page)
        self._build_activity_card(page)

    def _build_sources_card(self, page: ttk.Frame) -> None:
        card = ttk.LabelFrame(page, text="01  ·  Source folders", style="Card.TLabelframe")
        card.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(card, style="Card.TFrame")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(
            toolbar,
            text="Add each input folder. Its name and subfolder structure will be preserved.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(toolbar, textvariable=self.source_count_var, style="Count.TLabel").grid(
            row=0, column=1, sticky="e", padx=(8, 12)
        )
        self.add_source_button = ttk.Button(
            toolbar, text="Add folder", style="Secondary.TButton", command=self._add_source
        )
        self.add_source_button.grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.remove_source_button = ttk.Button(
            toolbar, text="Remove", style="Quiet.TButton", command=self._remove_sources
        )
        self.remove_source_button.grid(row=0, column=3, sticky="e", padx=(0, 4))
        self.clear_source_button = ttk.Button(
            toolbar, text="Clear", style="Quiet.TButton", command=self._clear_sources
        )
        self.clear_source_button.grid(row=0, column=4, sticky="e")

        self.source_tree = ttk.Treeview(
            card,
            columns=("folder", "location"),
            show="headings",
            selectmode="extended",
            height=4,
            style="Folders.Treeview",
        )
        self.source_tree.heading("folder", text="FOLDER")
        self.source_tree.heading("location", text="LOCATION")
        self.source_tree.column("folder", width=180, minwidth=130, stretch=False)
        self.source_tree.column("location", width=640, minwidth=180, stretch=True)
        self.source_tree.grid(row=1, column=0, sticky="nsew")
        self.config_widgets.extend(
            [self.add_source_button, self.remove_source_button, self.clear_source_button]
        )
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=self.source_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(
            card, orient="horizontal", command=self.source_tree.xview
        )
        horizontal_scrollbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.source_tree.configure(
            yscrollcommand=scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )

    def _build_destination_and_format_cards(self, page: ttk.Frame) -> None:
        body = ttk.Frame(page, style="App.TFrame")
        body.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)

        destination = ttk.LabelFrame(
            body, text="02  ·  Destination", style="Card.TLabelframe"
        )
        destination.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        destination.columnconfigure(0, weight=1)
        ttk.Label(destination, text="Output root folder", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        self.output_entry = ttk.Entry(destination, textvariable=self.output_dir_var)
        self.output_entry.grid(row=1, column=0, sticky="ew", padx=(0, 7))
        self.output_button = ttk.Button(
            destination,
            text="Browse…",
            style="Secondary.TButton",
            command=self._choose_output,
        )
        self.output_button.grid(row=1, column=1, sticky="e")
        ttk.Label(
            destination,
            text="Output becomes  DESTINATION / SOURCE NAME / …",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.config_widgets.extend([self.output_entry, self.output_button])

        formats = ttk.LabelFrame(
            body, text="03  ·  Conversion format", style="Card.TLabelframe"
        )
        formats.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        for column in range(3):
            formats.columnconfigure(column, weight=1)

        ttk.Label(formats, text="ENGINE", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 5)
        )
        ttk.Label(formats, text="READ", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=(0, 5)
        )
        ttk.Label(formats, text="WRITE", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", pady=(0, 5)
        )
        self.engine_combo = ttk.Combobox(
            formats,
            textvariable=self.engine_var,
            values=self.engine_choices,
            state="readonly",
        )
        self.engine_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.engine_combo.bind("<<ComboboxSelected>>", self._refresh_format_choices)
        self.input_combo = ttk.Combobox(
            formats, textvariable=self.input_format_var, state="readonly"
        )
        self.input_combo.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.output_combo = ttk.Combobox(
            formats, textvariable=self.output_format_var, state="readonly"
        )
        self.output_combo.grid(row=1, column=2, sticky="ew")
        ttk.Label(
            formats,
            text="Auto-detect handles mixed input formats; choices vary by engine.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.config_widgets.extend([self.engine_combo, self.input_combo, self.output_combo])

        self.advanced_toggle = ttk.Button(
            formats,
            text="Advanced options  +",
            style="Quiet.TButton",
            command=self._toggle_advanced,
        )
        self.advanced_toggle.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.advanced_frame = ttk.Frame(formats, style="Card.TFrame")
        self.advanced_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        for column in range(3):
            self.advanced_frame.columnconfigure(column, weight=1)

        self.recursive_check = ttk.Checkbutton(
            self.advanced_frame, text="Scan subfolders", variable=self.recursive_var
        )
        self.recursive_check.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.header_check = ttk.Checkbutton(
            self.advanced_frame, text="Input has a header", variable=self.header_var
        )
        self.header_check.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=3)
        self.json_lines_check = ttk.Checkbutton(
            self.advanced_frame, text="JSON Lines", variable=self.json_lines_var
        )
        self.json_lines_check.grid(row=0, column=2, sticky="w", pady=3)
        self.overwrite_check = ttk.Checkbutton(
            self.advanced_frame,
            text="Overwrite existing outputs",
            variable=self.overwrite_var,
        )
        self.overwrite_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)

        self.input_separator_entry = self._advanced_entry(
            self.advanced_frame, "Input separator", self.input_separator_var, 1, 2
        )
        self.output_separator_entry = self._advanced_entry(
            self.advanced_frame, "Output separator", self.output_separator_var, 2, 0
        )
        self.limit_entry = self._advanced_entry(
            self.advanced_frame, "File limit (blank = all)", self.limit_var, 2, 1
        )
        ttk.Label(
            self.advanced_frame,
            text="Blank separators use comma for CSV and tab for TSV.",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.config_widgets.extend(
            [
                self.advanced_toggle,
                self.recursive_check,
                self.header_check,
                self.json_lines_check,
                self.overwrite_check,
                self.input_separator_entry,
                self.output_separator_entry,
                self.limit_entry,
            ]
        )
        self.advanced_frame.grid_remove()

    def _advanced_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> ttk.Entry:
        cell = ttk.Frame(parent, style="Card.TFrame")
        cell.grid(row=row, column=column, sticky="ew", padx=(0, 8), pady=3)
        cell.columnconfigure(0, weight=1)
        ttk.Label(cell, text=label, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(cell, textvariable=variable, width=12)
        entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        return entry

    def _build_run_card(self, page: ttk.Frame) -> None:
        card = ttk.LabelFrame(page, text="04  ·  Run batch", style="Card.TLabelframe")
        card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(0, weight=1)

        controls = ttk.Frame(card, style="Card.TFrame")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(0, weight=1)
        status_block = ttk.Frame(controls, style="Card.TFrame")
        status_block.grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(
            status_block, textvariable=self.status_var, style="Status.TLabel"
        )
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(
            status_block, textvariable=self.current_file_var, style="Muted.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(controls, style="Card.TFrame")
        buttons.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.preview_button = ttk.Button(
            buttons,
            text="Preview plan",
            style="Secondary.TButton",
            command=lambda: self._start(dry_run=True),
        )
        self.preview_button.grid(row=0, column=0, padx=(0, 7))
        self.convert_button = ttk.Button(
            buttons,
            text="Convert files",
            style="Primary.TButton",
            command=lambda: self._start(dry_run=False),
        )
        self.convert_button.grid(row=0, column=1, padx=(0, 7))
        self.cancel_button = ttk.Button(
            buttons,
            text="Cancel",
            style="Quiet.TButton",
            command=self._cancel,
            state="disabled",
        )
        self.cancel_button.grid(row=0, column=2)

        self.progress = ttk.Progressbar(
            card, mode="determinate", style="Accent.Horizontal.TProgressbar"
        )
        self.progress.grid(row=1, column=0, sticky="ew")

    def _build_activity_card(self, page: ttk.Frame) -> None:
        card = ttk.LabelFrame(page, text="Activity", style="Card.TLabelframe")
        card.grid(row=4, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self.log = tk.Text(
            card,
            height=6,
            wrap="word",
            state="disabled",
            background=COLORS["surface_alt"],
            foreground=COLORS["ink"],
            insertbackground=COLORS["ink"],
            selectbackground=COLORS["blue_soft"],
            selectforeground=COLORS["ink"],
            relief="flat",
            padx=10,
            pady=8,
            font=("TkDefaultFont", 9),
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _toggle_advanced(self) -> None:
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.advanced_frame.grid()
            self.advanced_toggle.configure(text="Advanced options  −")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_toggle.configure(text="Advanced options  +")

    def _refresh_format_choices(self, _event: Any = None) -> None:
        engine = ENGINE_LABELS[self.engine_var.get()]
        supported = ENGINE_FORMATS[engine]
        input_formats = ["auto", *(fmt for fmt in FORMAT_SUFFIXES if fmt in supported)]
        output_formats = [fmt for fmt in FORMAT_SUFFIXES if fmt in supported]
        self.input_combo.configure(values=tuple(FORMAT_LABELS[fmt] for fmt in input_formats))
        self.output_combo.configure(values=tuple(FORMAT_LABELS[fmt] for fmt in output_formats))

        input_code = self._format_code(self.input_format_var.get())
        if input_code != "auto" and input_code not in supported:
            self.input_format_var.set(FORMAT_LABELS["auto"])
        output_code = self._format_code(self.output_format_var.get())
        if output_code not in supported:
            fallback = "parquet" if "parquet" in supported else output_formats[0]
            self.output_format_var.set(FORMAT_LABELS[fallback])

    @staticmethod
    def _format_code(label: str) -> str:
        return next((code for code, text in FORMAT_LABELS.items() if text == label), "auto")

    def _add_source(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="Choose a source folder", mustexist=True
        )
        if not selected:
            return
        path = Path(selected).expanduser().resolve()
        if path in self.sources:
            return
        self.sources.append(path)
        self._refresh_source_rows()
        self._set_status(f"{len(self.sources)} source folder(s) selected.")

    def _remove_sources(self) -> None:
        selected = sorted((int(item) for item in self.source_tree.selection()), reverse=True)
        for index in selected:
            del self.sources[index]
        self._refresh_source_rows()
        self._set_status(f"{len(self.sources)} source folder(s) selected.")

    def _clear_sources(self) -> None:
        self.sources.clear()
        self._refresh_source_rows()
        self._set_status("Add source folders to begin.")

    def _refresh_source_rows(self) -> None:
        self.source_tree.delete(*self.source_tree.get_children())
        for index, source in enumerate(self.sources):
            self.source_tree.insert(
                "", tk.END, iid=str(index), values=(source.name or str(source), str(source))
            )
        count = len(self.sources)
        self.source_count_var.set(f"{count} folder" + ("s" if count != 1 else ""))

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="Choose the output root folder"
        )
        if selected:
            self.output_dir_var.set(str(Path(selected).expanduser()))

    def _start(self, *, dry_run: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.sources:
            messagebox.showerror("Add source folders", "Choose at least one source folder.",
                                 parent=self.root)
            return
        if not self.output_dir_var.get().strip():
            messagebox.showerror("Choose a destination", "Choose the output root folder.",
                                 parent=self.root)
            return

        limit_text = self.limit_var.get().strip()
        try:
            limit = int(limit_text) if limit_text else None
            if limit is not None and limit < 1:
                raise ValueError("The file limit must be a positive integer.")
            for label, value in (
                ("Input separator", self.input_separator_var.get()),
                ("Output separator", self.output_separator_var.get()),
            ):
                if value and len(value) != 1:
                    raise ValueError(f"{label} must be a single character.")
        except ValueError as exc:
            messagebox.showerror("Check conversion settings", str(exc), parent=self.root)
            return

        engine = ENGINE_LABELS[self.engine_var.get()]
        input_format = self._format_code(self.input_format_var.get())
        output_format = self._format_code(self.output_format_var.get())
        options = IOOptions(
            input_separator=self.input_separator_var.get() or None,
            output_separator=self.output_separator_var.get() or None,
            header=self.header_var.get(),
            json_lines=self.json_lines_var.get(),
        )

        self.cancel_event.clear()
        self.close_when_done = False
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")
        self.current_file_var.set("")
        self._set_running(True)
        self._set_status("Scanning source folders…")
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        self.progress.start(12)

        self.worker = threading.Thread(
            target=self._worker_main,
            args=(
                tuple(self.sources),
                Path(self.output_dir_var.get()).expanduser(),
                input_format,
                output_format,
                engine,
                options,
                self.recursive_var.get(),
                limit,
                self.overwrite_var.get(),
                dry_run,
            ),
            name="df2pq-conversion",
            daemon=True,
        )
        self.worker.start()

    def _worker_main(
        self,
        sources: tuple[Path, ...],
        output: Path,
        input_format: str,
        output_format: str,
        engine: str,
        options: IOOptions,
        recursive: bool,
        limit: int | None,
        overwrite: bool,
        dry_run: bool,
    ) -> None:
        try:
            jobs = build_jobs(
                list(sources),
                output,
                input_format,
                output_format,
                engine,
                recursive=recursive,
                limit=limit,
            )
            self.events.put(("plan", len(jobs)))
            if self.cancel_event.is_set():
                self.events.put(("cancelled", (0, 0, 0)))
                return
            if dry_run:
                for index, job in enumerate(jobs, start=1):
                    if self.cancel_event.is_set():
                        self.events.put(("cancelled", (0, 0, 0)))
                        return
                    self.events.put(("preview", (index, len(jobs), job, output_format)))
                self.events.put(("done", (0, 0, 0, True)))
                return

            backend = _load_engine(engine)
            converted = skipped = failed = 0
            for index, job in enumerate(jobs, start=1):
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", (converted, skipped, failed)))
                    return
                try:
                    result = convert_one(
                        job,
                        output_format,
                        engine,
                        options,
                        backend,
                        overwrite=overwrite,
                    )
                    if result == "converted":
                        converted += 1
                    else:
                        skipped += 1
                    self.events.put(("file", (index, len(jobs), job, result, None)))
                except Exception as exc:
                    failed += 1
                    self.events.put(
                        (
                            "file",
                            (index, len(jobs), job, "failed", f"{type(exc).__name__}: {exc}"),
                        )
                    )
            self.events.put(("done", (converted, skipped, failed, False)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "plan":
                total = int(payload)
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=max(total, 1), value=0)
                self._set_status(f"Ready — {total:,} file(s) found.")
            elif kind == "preview":
                index, total, job, output_format = payload
                self.progress.configure(value=index)
                self.current_file_var.set(f"Preview {index:,}/{total:,}  ·  {job.source.name}")
                self._append_log(
                    f"{job.source}  →  {job.target}  ({job.input_format} → {output_format})"
                )
            elif kind == "file":
                index, total, job, result, error = payload
                self.progress.configure(value=index)
                self.current_file_var.set(f"{index:,}/{total:,}  ·  {job.source.name}")
                if result == "failed":
                    self._append_log(f"FAILED  {job.source}\n        {error}")
                elif result == "skipped":
                    self._append_log(f"SKIPPED  {job.target}")
                else:
                    self._append_log(f"DONE  {job.target}")
            elif kind == "done":
                converted, skipped, failed, dry_run = payload
                self.progress.stop()
                if dry_run:
                    message = "Preview complete. No files were converted."
                    self._set_status(message)
                else:
                    message = f"Complete — {converted:,} converted, {skipped:,} skipped, {failed:,} failed."
                    self._set_status(message, "success" if failed == 0 else "error")
                self.current_file_var.set("")
                self._append_log(message)
                self._set_running(False)
                self._finish_close_if_requested()
            elif kind == "cancelled":
                converted, skipped, failed = payload
                self.progress.stop()
                message = (
                    f"Cancelled — {converted:,} converted, {skipped:,} skipped, {failed:,} failed."
                )
                self._set_status(message, "warning")
                self.current_file_var.set("")
                self._append_log(message)
                self._set_running(False)
                self._finish_close_if_requested()
            elif kind == "error":
                self.progress.stop()
                self._set_status("Could not start conversion.", "error")
                self._append_log(f"ERROR  {payload}")
                self._set_running(False)
                messagebox.showerror("Conversion could not start", str(payload), parent=self.root)
                self._finish_close_if_requested()
        try:
            self.root.after(100, self._poll_events)
        except tk.TclError:
            pass

    def _set_status(self, message: str, tone: str = "normal") -> None:
        styles = {
            "normal": "Status.TLabel",
            "success": "Success.TLabel",
            "warning": "Warning.TLabel",
            "error": "Error.TLabel",
        }
        self.status_var.set(message)
        self.status_label.configure(style=styles.get(tone, "Status.TLabel"))

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log.delete("1.0", "101.0")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in self.config_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state=state)
        self.source_tree.state(["disabled"] if running else ["!disabled"])
        self.preview_button.configure(state=state)
        self.convert_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")

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
        self.root.destroy()

    def _finish_close_if_requested(self) -> None:
        if self.close_when_done:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
