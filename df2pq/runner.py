"""Run a conversion batch on a worker thread and report progress as events."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
import multiprocessing
from pathlib import Path
import queue
import threading
from typing import Any

from .converter import ConversionJob, IOOptions, _load_engine, build_jobs, convert_one

# Engines that may convert several files at once in separate processes. cuDF uses
# one GPU worker, so it always converts one file at a time.
PARALLEL_ENGINES = frozenset({"pyarrow", "pandas"})

_process_backends: dict[str, Any] = {}


def _init_process() -> None:
    # One file per process: let each process use one core instead of competing.
    try:
        import pyarrow

        pyarrow.set_cpu_count(1)
        pyarrow.set_io_thread_count(1)
    except Exception:
        pass


def _convert_in_process(job: ConversionJob, output_format: str, engine: str,
                        options: IOOptions, overwrite: bool) -> tuple[str, str | None]:
    """Runs in a worker process; returns (result, note or error)."""
    try:
        backend = _process_backends.get(engine)
        if backend is None:
            backend = _process_backends[engine] = _load_engine(engine)
        notes: list[str] = []
        result = convert_one(job, output_format, engine, options, backend,
                             overwrite=overwrite, notes=notes)
        return result, "; ".join(notes) or None
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"


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
        workers: int = 1,
    ) -> None:
        put = self.events.put
        try:
            ignored: list[tuple[Path, str]] = []
            jobs = build_jobs(sources, output, input_format, output_format, engine,
                              recursive=recursive, limit=limit, ignored=ignored)
            put(("plan", len(jobs)))
            for path, reason in ignored:
                put(("ignored", (path, reason)))
            if dry_run:
                for index, job in enumerate(jobs, start=1):
                    if self.cancel_event.is_set():
                        put(("cancelled", (0, 0, 0)))
                        return
                    put(("preview", (index, len(jobs), job, output_format)))
                put(("done", (0, 0, 0, True)))
                return

            if workers > 1 and engine in PARALLEL_ENGINES and len(jobs) > 1:
                self._run_parallel(jobs, output_format, engine, options, overwrite, workers)
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
                    notes: list[str] = []
                    result = convert_one(job, output_format, engine, options, backend,
                                         overwrite=overwrite, fallback=fallback, notes=notes)
                    if result == "skipped":
                        skipped += 1
                    else:
                        converted += 1
                    put(("file", (index, len(jobs), job, result, "; ".join(notes) or None)))
                except Exception as exc:
                    failed += 1
                    put(("file", (index, len(jobs), job, "failed",
                                  f"{type(exc).__name__}: {exc}")))
            put(("done", (converted, skipped, failed, False)))
        except Exception as exc:
            put(("error", str(exc)))

    def _run_parallel(self, jobs: list[ConversionJob], output_format: str, engine: str,
                      options: IOOptions, overwrite: bool, workers: int) -> None:
        """Convert several files at once; progress is reported in completion order.

        On cancel, files already being converted finish (each is published
        atomically) and the rest are not started.
        """
        put = self.events.put
        converted = skipped = failed = done = 0
        pending = iter(jobs)
        in_flight: dict[Future, ConversionJob] = {}
        # "spawn" never copies this multi-threaded process, and works the same on
        # Linux, macOS, Windows and in the packaged app.
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), mp_context=context,
                                 initializer=_init_process) as pool:

            def submit_next() -> None:
                job = next(pending, None)
                if job is not None:
                    future = pool.submit(_convert_in_process, job, output_format, engine,
                                         options, overwrite)
                    in_flight[future] = job

            for _ in range(min(workers, len(jobs)) * 2):
                submit_next()
            while in_flight:
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    job = in_flight.pop(future)
                    try:
                        result, detail = future.result()
                    except Exception as exc:  # e.g. a worker process was killed
                        result, detail = "failed", f"{type(exc).__name__}: {exc}"
                    done += 1
                    if result == "failed":
                        failed += 1
                    elif result == "skipped":
                        skipped += 1
                    else:
                        converted += 1
                    put(("file", (done, len(jobs), job, result, detail)))
                    if not self.cancel_event.is_set():
                        submit_next()
        if self.cancel_event.is_set() and done < len(jobs):
            put(("cancelled", (converted, skipped, failed)))
        else:
            put(("done", (converted, skipped, failed, False)))
