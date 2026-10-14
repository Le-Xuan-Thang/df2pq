"""Run a conversion batch on a worker thread and report progress as events."""

from __future__ import annotations

from pathlib import Path
import queue
import threading
from typing import Any

from .converter import IOOptions, _load_engine, build_jobs, convert_one


class BatchRunner:
    """Events put on ``events``: plan, preview, file, done, cancelled, error."""

    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self.cancel_event.set()

    def start(self, **kwargs: Any) -> bool:
        if self.running:
            return False
        self.cancel_event.clear()
        self._thread = threading.Thread(
            target=self._run, kwargs=kwargs, name="df2pq-conversion", daemon=True
        )
        self._thread.start()
        return True

    def _run(
        self,
        *,
        sources: list[Path],
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
        put = self.events.put
        try:
            jobs = build_jobs(sources, output, input_format, output_format, engine,
                              recursive=recursive, limit=limit)
            put(("plan", len(jobs)))
            if dry_run:
                for index, job in enumerate(jobs, start=1):
                    if self.cancel_event.is_set():
                        put(("cancelled", (0, 0, 0)))
                        return
                    put(("preview", (index, len(jobs), job, output_format)))
                put(("done", (0, 0, 0, True)))
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
                    put(("cancelled", (converted, skipped, failed)))
                    return
                try:
                    result = convert_one(job, output_format, engine, options, backend,
                                         overwrite=overwrite, fallback=fallback)
                    if result == "skipped":
                        skipped += 1
                    else:
                        converted += 1
                    put(("file", (index, len(jobs), job, result, None)))
                except Exception as exc:
                    failed += 1
                    put(("file", (index, len(jobs), job, "failed",
                                  f"{type(exc).__name__}: {exc}")))
            put(("done", (converted, skipped, failed, False)))
        except Exception as exc:
            put(("error", str(exc)))
