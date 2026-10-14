"""Folder browser that shows how much convertible data each folder holds."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .scan import FolderSummary, fuzzy_match as _fuzzy, human_size, places as _places, summarize_folder
from .settings import Settings
from .theme import C, Fonts


class FolderPicker(tk.Toplevel):
    """Modal dialog; ``result`` is a list of chosen folders, or None if cancelled."""

    def __init__(
        self,
        parent: tk.Misc,
        fonts: Fonts,
        settings: Settings,
        *,
        formats: frozenset[str] | None,
        recursive: bool,
        title: str = "Add source folders",
        multiple: bool = True,
        start: Path | None = None,
        action_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.fonts, self.settings = fonts, settings
        self.formats, self.recursive, self.multiple = formats, recursive, multiple
        self.result: list[Path] | None = None
        self.current = Path.home()
        self.generation = 0
        self.summaries: dict[str, FolderSummary] = {}
        self.updates: queue.Queue[tuple[int, str, FolderSummary]] = queue.Queue()
        self.jobs: queue.Queue[tuple[int, Path] | None] = queue.Queue()
        self.stop = threading.Event()
        self.children_all: list[Path] = []
        self.action_label = action_label or "Add"

        self.title(title)
        self.configure(background=C["bg"])
        self.geometry(f"{fonts.px(980)}x{fonts.px(640)}")
        self.minsize(fonts.px(760), fonts.px(480))
        self.transient(parent.winfo_toplevel())
        self._build()
        threading.Thread(target=self._worker, name="df2pq-scan", daemon=True).start()

        start_dir = start or (settings.recent("recent_browse") or [Path.home()])[0]
        self._goto(start_dir if start_dir.is_dir() else Path.home())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(60, self._poll)
        self.wait_visibility()
        self.grab_set()
        self.tree.focus_set()

    # ---- construction -------------------------------------------------
    def _build(self) -> None:
        f = self.fonts
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        side = tk.Frame(self, background=C["card"], width=f.px(190))
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        tk.Label(side, text="PLACES", background=C["card"], foreground=C["faint"],
                 font=f.ui_font(8, "bold"), anchor="w").pack(fill="x", padx=14, pady=(14, 4))
        for label, path in _places():
            self._side_button(side, label, path)
        recents = self.settings.recent("recent_sources")
        if recents:
            tk.Label(side, text="RECENT SOURCES", background=C["card"], foreground=C["faint"],
                     font=f.ui_font(8, "bold"), anchor="w").pack(fill="x", padx=14, pady=(16, 4))
            for path in recents[:6]:
                self._side_button(side, path.name or str(path), path, tip=str(path))

        main = ttk.Frame(self, style="App.TFrame", padding=(16, 14))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        bar = ttk.Frame(main, style="App.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.up_button = ttk.Button(bar, text="↑ Up", style="Secondary.TButton",
                                    command=self._go_up)
        self.up_button.grid(row=0, column=0, padx=(0, 8))
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(bar, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew")
        self.path_entry.bind("<Return>", self._on_path_enter)
        self.path_entry.bind("<Tab>", self._complete_path)

        tools = ttk.Frame(main, style="App.TFrame")
        tools.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        tools.columnconfigure(0, weight=1)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render())
        self.filter_entry = ttk.Entry(tools, textvariable=self.filter_var)
        self.filter_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.filter_entry.bind("<Down>", lambda _e: (self.tree.focus_set(), "break")[1])
        self.data_only_var = tk.BooleanVar(value=False)
        self.hidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="Only folders with data", variable=self.data_only_var,
                        command=self._render, style="Bar.TCheckbutton").grid(row=0, column=1)
        ttk.Checkbutton(tools, text="Hidden", variable=self.hidden_var,
                        command=self._relist, style="Bar.TCheckbutton").grid(row=0, column=2,
                                                                          padx=(10, 0))
        self.placeholder = ttk.Label(tools, text="Type to filter folders — e.g. 'cmp07' matches "
                                     "campaign_07_23", style="BgMuted.TLabel")
        self.placeholder.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        wrap = tk.Frame(main, background=C["border"], padx=1, pady=1)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            wrap, columns=("files", "size", "formats"), show="tree headings",
            selectmode="extended" if self.multiple else "browse", style="Data.Treeview",
        )
        self.tree.heading("#0", text="FOLDER", anchor="w")
        self.tree.heading("files", text="DATA FILES", anchor="e")
        self.tree.heading("size", text="SIZE", anchor="e")
        self.tree.heading("formats", text="FORMATS", anchor="w")
        self.tree.column("#0", width=f.px(330), minwidth=f.px(160), stretch=True)
        self.tree.column("files", width=f.px(100), minwidth=f.px(80), stretch=False, anchor="e")
        self.tree.column("size", width=f.px(90), minwidth=f.px(70), stretch=False, anchor="e")
        self.tree.column("formats", width=f.px(190), minwidth=f.px(100), stretch=False, anchor="w")
        self.tree.tag_configure("data", foreground=C["ink"])
        self.tree.tag_configure("empty", foreground=C["faint"])
        self.tree.tag_configure("pending", foreground=C["muted"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_enter)
        self.tree.bind("<BackSpace>", lambda _e: self._go_up())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_footer())
        self.empty_note = ttk.Label(wrap, text="", style="Empty.TLabel", anchor="center")

        foot = ttk.Frame(main, style="App.TFrame")
        foot.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        foot.columnconfigure(0, weight=1)
        self.info_var = tk.StringVar()
        ttk.Label(foot, textvariable=self.info_var, style="BgMuted.TLabel").grid(
            row=0, column=0, sticky="w")
        if self.multiple:
            self.all_button = ttk.Button(foot, text="Add every subfolder with data",
                                         style="Secondary.TButton", command=self._add_all_data)
            self.all_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(foot, text="Cancel", style="Quiet.TButton", command=self._cancel).grid(
            row=0, column=2, padx=(0, 8))
        self.ok_button = ttk.Button(foot, text=self.action_label, style="Primary.TButton",
                                    command=self._accept)
        self.ok_button.grid(row=0, column=3)

    def _side_button(self, parent: tk.Misc, label: str, path: Path, tip: str = "") -> None:
        button = tk.Label(parent, text=label, background=C["card"], foreground=C["ink"],
                          font=self.fonts.ui_font(10), anchor="w", padx=14, pady=5,
                          cursor="hand2")
        button.pack(fill="x")
        button.bind("<Button-1>", lambda _e: self._goto(path))
        button.bind("<Enter>", lambda _e: button.configure(background=C["card_alt"]))
        button.bind("<Leave>", lambda _e: button.configure(background=C["card"]))

    # ---- navigation ---------------------------------------------------
    def _goto(self, path: Path) -> None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            return
        if not path.is_dir():
            return
        self.current = path
        self.path_var.set(str(path))
        self.filter_var.set("")
        self._relist()

    def _go_up(self) -> None:
        if self.current.parent != self.current:
            self._goto(self.current.parent)

    def _on_path_enter(self, _event: tk.Event) -> None:
        text = os.path.expandvars(self.path_var.get().strip().strip('"'))
        target = Path(text).expanduser()
        if target.is_dir():
            self._goto(target)
            self.tree.focus_set()
        else:
            self.info_var.set(f"Not a folder: {text}")

    def _complete_path(self, _event: tk.Event) -> str:
        text = os.path.expanduser(self.path_var.get())
        base, _, prefix = text.rpartition(os.sep)
        try:
            names = [n for n in os.listdir(base or os.sep)
                     if n.casefold().startswith(prefix.casefold())
                     and os.path.isdir(os.path.join(base or os.sep, n))]
        except OSError:
            return "break"
        if names:
            common = os.path.commonprefix(sorted(names))
            completed = os.path.join(base or os.sep, common)
            if len(names) == 1:
                completed += os.sep
            self.path_var.set(completed)
            self.path_entry.icursor(tk.END)
            if len(names) > 1:
                self.info_var.set("Matches: " + ", ".join(sorted(names)[:6]))
        return "break"

    def _relist(self) -> None:
        self.generation += 1
        self.summaries.clear()
        try:
            show_hidden = self.hidden_var.get()
            self.children_all = sorted(
                (Path(e.path) for e in os.scandir(self.current)
                 if e.is_dir() and (show_hidden or not e.name.startswith("."))),
                key=lambda p: p.name.casefold(),
            )
        except OSError as exc:
            self.children_all = []
            self.info_var.set(f"Cannot read folder: {exc.strerror or exc}")
        for path in [self.current, *self.children_all]:
            self.jobs.put((self.generation, path))
        self._render()

    # ---- scanning (background) ----------------------------------------
    def _worker(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.3)
            except queue.Empty:
                continue
            if job is None:
                return
            generation, path = job
            if generation != self.generation:
                continue
            summary = summarize_folder(path, recursive=self.recursive, max_files=30_000,
                                       time_budget=1.5, cancel=self.stop)
            self.updates.put((generation, str(path), summary))

    def _poll(self) -> None:
        changed = False
        try:
            while True:
                generation, key, summary = self.updates.get_nowait()
                if generation == self.generation:
                    self.summaries[key] = summary
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._render(keep_selection=True)
        if self.winfo_exists() and not self.stop.is_set():
            self.after(80, self._poll)

    # ---- rendering ----------------------------------------------------
    def _row_values(self, path: Path) -> tuple[tuple[str, str, str], str]:
        summary = self.summaries.get(str(path))
        if summary is None:
            return ("…", "", ""), "pending"
        count = summary.files(self.formats)
        if count == 0:
            return ("—", "", ""), "empty"
        plus = "+" if summary.truncated else ""
        return (f"{count:,}{plus}", human_size(summary.size(self.formats)),
                summary.format_text()), "data"

    def _render(self, keep_selection: bool = False) -> None:
        selected = set(self.tree.selection()) if keep_selection else set()
        self.tree.delete(*self.tree.get_children())
        query = self.filter_var.get()
        self.placeholder.grid() if not query else self.placeholder.grid_remove()
        shown = 0
        for path in self.children_all:
            if query and not _fuzzy(query, path.name):
                continue
            values, tag = self._row_values(path)
            if self.data_only_var.get() and tag == "empty":
                continue
            self.tree.insert("", tk.END, iid=str(path), text=f"  {path.name}",
                             values=values, tags=(tag,))
            shown += 1
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        if shown == 0:
            msg = "No matching folders." if self.children_all else "This folder has no subfolders."
            self.empty_note.configure(text=msg)
            self.empty_note.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty_note.place_forget()
        self._update_footer()

    def _update_footer(self) -> None:
        selected = self.tree.selection()
        here = self.summaries.get(str(self.current))
        if selected:
            self.ok_button.configure(text=f"{self.action_label} {len(selected)} selected")
            total = sum(self.summaries[s].files(self.formats) for s in selected
                        if s in self.summaries)
            self.info_var.set(f"{len(selected)} folder(s) selected · {total:,} data file(s)")
        else:
            self.ok_button.configure(text=f"{self.action_label} this folder")
            if here is None:
                self.info_var.set(f"Scanning {self.current.name or self.current}…")
            else:
                n = here.files(self.formats)
                self.info_var.set(
                    f"This folder: {n:,}{'+' if here.truncated else ''} data file(s), "
                    f"{human_size(here.size(self.formats))}" if n else
                    "No convertible files here — open a subfolder or pick another place.")
        if self.multiple:
            ready = sum(1 for p in self.children_all
                        if (s := self.summaries.get(str(p))) and s.files(self.formats))
            self.all_button.configure(state="normal" if ready else "disabled")

    # ---- actions ------------------------------------------------------
    def _on_double(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self._goto(Path(iid))

    def _on_enter(self, _event: tk.Event) -> str:
        selected = self.tree.selection()
        if len(selected) == 1 and not self.multiple:
            self._goto(Path(selected[0]))
        else:
            self._accept()
        return "break"

    def _finish(self, paths: list[Path]) -> None:
        self.settings.remember(self.current, "recent_browse")
        self.result = paths
        self._close()

    def _accept(self) -> None:
        selected = [Path(s) for s in self.tree.selection()]
        self._finish(selected or [self.current])

    def _add_all_data(self) -> None:
        paths = [p for p in self.children_all
                 if (s := self.summaries.get(str(p))) and s.files(self.formats)]
        self._finish(paths)

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        self.stop.set()
        self.jobs.put(None)
        self.grab_release()
        self.destroy()

    def show(self) -> list[Path] | None:
        self.wait_window()
        return self.result
