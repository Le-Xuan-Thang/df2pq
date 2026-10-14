"""Tkinter desktop interface for the df2pq batch converter package."""

from __future__ import annotations

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
    "Pandas (CPU)": "pandas",
    "cuDF (GPU)": "cudf",
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


class ConverterApp:
    """Desktop UI that runs conversion work off the Tk event thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("df2pq — Batch Data Converter")
        self.root.minsize(760, 600)
        self.root.geometry("940x760")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.sources: list[Path] = []
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.close_when_done = False

        self.engine_var = tk.StringVar(value="Pandas (CPU)")
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
        self.status_var = tk.StringVar(value="Add one or more source folders to begin.")

        self._build_ui()
        self._refresh_format_choices()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="df2pq", font=("TkDefaultFont", 20, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="Batch-convert data files and mirror each source folder under your destination.",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        sources_frame = ttk.LabelFrame(frame, text="Source folders", padding=10)
        sources_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        sources_frame.columnconfigure(0, weight=1)
        sources_frame.rowconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.source_list = tk.Listbox(
            sources_frame,
            height=6,
            selectmode=tk.EXTENDED,
            exportselection=False,
        )
        self.source_list.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        source_scroll = ttk.Scrollbar(
            sources_frame, orient="vertical", command=self.source_list.yview
        )
        source_scroll.grid(row=0, column=1, rowspan=3, sticky="ns")
        self.source_list.configure(yscrollcommand=source_scroll.set)
        ttk.Button(sources_frame, text="Add folder…", command=self._add_source).grid(
            row=0, column=2, sticky="ew", pady=(0, 5)
        )
        ttk.Button(sources_frame, text="Remove selected", command=self._remove_sources).grid(
            row=1, column=2, sticky="ew", pady=5
        )
        ttk.Button(sources_frame, text="Clear list", command=self._clear_sources).grid(
            row=2, column=2, sticky="ew", pady=(5, 0)
        )

        output_frame = ttk.LabelFrame(frame, text="Destination", padding=10)
        output_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        output_frame.columnconfigure(0, weight=1)
        ttk.Entry(output_frame, textvariable=self.output_dir_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(output_frame, text="Choose folder…", command=self._choose_output).grid(
            row=0, column=1, sticky="e"
        )

        settings = ttk.LabelFrame(frame, text="Conversion settings", padding=10)
        settings.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        for column in range(6):
            settings.columnconfigure(column, weight=1 if column % 2 else 0)

        ttk.Label(settings, text="Backend").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.engine_combo = ttk.Combobox(
            settings,
            textvariable=self.engine_var,
            values=tuple(ENGINE_LABELS),
            state="readonly",
            width=17,
        )
        self.engine_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=4)
        self.engine_combo.bind("<<ComboboxSelected>>", self._refresh_format_choices)

        ttk.Label(settings, text="Input format").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=4
        )
        self.input_combo = ttk.Combobox(
            settings, textvariable=self.input_format_var, state="readonly", width=17
        )
        self.input_combo.grid(row=0, column=3, sticky="ew", padx=(0, 14), pady=4)

        ttk.Label(settings, text="Output format").grid(
            row=0, column=4, sticky="w", padx=(0, 6), pady=4
        )
        self.output_combo = ttk.Combobox(
            settings, textvariable=self.output_format_var, state="readonly", width=17
        )
        self.output_combo.grid(row=0, column=5, sticky="ew", pady=4)

        ttk.Label(settings, text="Input separator").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(settings, textvariable=self.input_separator_var, width=8).grid(
            row=1, column=1, sticky="ew", padx=(0, 14), pady=4
        )
        ttk.Label(settings, text="Output separator").grid(
            row=1, column=2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(settings, textvariable=self.output_separator_var, width=8).grid(
            row=1, column=3, sticky="ew", padx=(0, 14), pady=4
        )
        ttk.Label(settings, text="Limit files").grid(
            row=1, column=4, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(settings, textvariable=self.limit_var, width=10).grid(
            row=1, column=5, sticky="ew", pady=4
        )
        ttk.Label(
            settings,
            text="Leave separators blank to use comma for CSV and tab for TSV; leave limit blank for all files.",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(0, 6))

        ttk.Checkbutton(
            settings, text="Scan subfolders", variable=self.recursive_var
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            settings, text="Input has a header row", variable=self.header_var
        ).grid(row=3, column=2, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            settings, text="JSON Lines", variable=self.json_lines_var
        ).grid(row=3, column=4, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(
            settings,
            text="Overwrite existing output files",
            variable=self.overwrite_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=3)

        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        self.preview_button = ttk.Button(
            actions, text="Preview", command=lambda: self._start(dry_run=True)
        )
        self.preview_button.pack(side="left")
        self.convert_button = ttk.Button(
            actions, text="Convert", command=lambda: self._start(dry_run=False)
        )
        self.convert_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(
            actions, text="Cancel", command=self._cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(16, 0))

        ttk.Label(frame, textvariable=self.status_var).grid(
            row=6, column=0, sticky="ew", pady=(0, 5)
        )
        log_frame = ttk.LabelFrame(frame, text="Activity", padding=8)
        log_frame.grid(row=7, column=0, sticky="nsew")
        frame.rowconfigure(7, weight=2)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

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
        selected = filedialog.askdirectory(title="Choose a source folder")
        if not selected:
            return
        path = Path(selected).resolve()
        if path in self.sources:
            return
        self.sources.append(path)
        self.source_list.insert(tk.END, str(path))
        self.status_var.set(f"{len(self.sources)} source folder(s) selected.")

    def _remove_sources(self) -> None:
        for index in reversed(self.source_list.curselection()):
            self.source_list.delete(index)
            del self.sources[index]
        self.status_var.set(f"{len(self.sources)} source folder(s) selected.")

    def _clear_sources(self) -> None:
        self.sources.clear()
        self.source_list.delete(0, tk.END)
        self.status_var.set("Add one or more source folders to begin.")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Choose the output folder")
        if selected:
            self.output_dir_var.set(selected)

    def _start(self, *, dry_run: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.sources:
            messagebox.showerror("Missing sources", "Add at least one source folder.")
            return
        if not self.output_dir_var.get().strip():
            messagebox.showerror("Missing destination", "Choose an output folder.")
            return

        limit_text = self.limit_var.get().strip()
        try:
            limit = int(limit_text) if limit_text else None
            if limit is not None and limit < 1:
                raise ValueError("Limit must be a positive integer.")
            for label, value in (
                ("Input separator", self.input_separator_var.get()),
                ("Output separator", self.output_separator_var.get()),
            ):
                if value and len(value) != 1:
                    raise ValueError(f"{label} must be a single character.")
        except ValueError as exc:
            messagebox.showerror("Invalid setting", str(exc))
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
        self._set_running(True)
        self.status_var.set("Scanning source folders…")
        self.progress.configure(mode="indeterminate")
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
                self.status_var.set(f"Found {total} file(s).")
            elif kind == "preview":
                index, total, job, output_format = payload
                self.progress.configure(value=index)
                self.status_var.set(f"Preview {index}/{total}: {job.source.name}")
                self._append_log(
                    f"{job.source}  →  {job.target}  ({job.input_format} → {output_format})"
                )
            elif kind == "file":
                index, total, job, result, error = payload
                self.progress.configure(value=index)
                self.status_var.set(f"{index}/{total}: {job.source.name} — {result}")
                if result == "failed":
                    self._append_log(f"FAILED: {job.source}\n  {error}")
                elif result == "skipped":
                    self._append_log(f"Skipped (already exists): {job.target}")
            elif kind == "done":
                converted, skipped, failed, dry_run = payload
                self.progress.stop()
                if dry_run:
                    message = "Preview complete. No files were converted."
                else:
                    message = (
                        f"Complete — converted {converted}, skipped {skipped}, failed {failed}."
                    )
                self.status_var.set(message)
                self._append_log(message)
                self._set_running(False)
                self._finish_close_if_requested()
            elif kind == "cancelled":
                converted, skipped, failed = payload
                self.progress.stop()
                message = (
                    f"Cancelled — converted {converted}, skipped {skipped}, failed {failed}."
                )
                self.status_var.set(message)
                self._append_log(message)
                self._set_running(False)
                self._finish_close_if_requested()
            elif kind == "error":
                self.progress.stop()
                self.status_var.set("Could not start conversion.")
                self._append_log(f"ERROR: {payload}")
                self._set_running(False)
                messagebox.showerror("Conversion error", str(payload))
                self._finish_close_if_requested()
        try:
            self.root.after(100, self._poll_events)
        except tk.TclError:
            # The user may have closed the window while the worker was exiting.
            pass

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.preview_button.configure(state=state)
        self.convert_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("Cancellation requested; finishing the current file…")

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if messagebox.askyesno(
                "Conversion running",
                "Cancel the batch and close after the current file finishes?",
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
