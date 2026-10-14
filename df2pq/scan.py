"""Fast folder inspection used by the GUI to show what a folder would convert."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import threading
import time

from .converter import FORMAT_SUFFIXES

_SUFFIX_TO_FORMAT = {suffix: fmt for fmt, suffixes in FORMAT_SUFFIXES.items() for suffix in suffixes}


@dataclass
class FolderSummary:
    path: Path
    by_format: Counter = field(default_factory=Counter)
    size_by_format: Counter = field(default_factory=Counter)
    truncated: bool = False

    def files(self, formats: frozenset[str] | None = None) -> int:
        return sum(n for fmt, n in self.by_format.items() if formats is None or fmt in formats)

    def size(self, formats: frozenset[str] | None = None) -> int:
        return sum(n for fmt, n in self.size_by_format.items() if formats is None or fmt in formats)

    def format_text(self, limit: int = 3) -> str:
        parts = [f"{fmt} {n:,}" for fmt, n in self.by_format.most_common(limit)]
        if len(self.by_format) > limit:
            parts.append("…")
        return " · ".join(parts)


def summarize_folder(
    path: Path,
    *,
    recursive: bool = True,
    cancel: threading.Event | None = None,
    max_files: int = 200_000,
    time_budget: float | None = None,
) -> FolderSummary:
    """Count convertible files below ``path`` (hidden folders are skipped)."""
    summary = FolderSummary(path)
    deadline = None if time_budget is None else time.monotonic() + time_budget
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    summary.truncated = True
                    return summary
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive and not entry.name.startswith("."):
                            stack.append(Path(entry.path))
                        continue
                    if not entry.is_file():
                        continue
                    fmt = _SUFFIX_TO_FORMAT.get(Path(entry.name).suffix.casefold())
                    if fmt is None:
                        continue
                    summary.by_format[fmt] += 1
                    summary.size_by_format[fmt] += entry.stat().st_size
                except OSError:
                    continue
                seen += 1
                if seen >= max_files or (deadline is not None and time.monotonic() > deadline):
                    summary.truncated = True
                    return summary
    return summary


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def places() -> list[tuple[str, Path]]:
    home = Path.home()
    places = [("Home", home)]
    for name in ("Desktop", "Documents", "Downloads"):
        if (home / name).is_dir():
            places.append((name, home / name))
    roots: list[Path] = []
    if sys.platform == "win32":
        roots = [Path(f"{letter}:\\") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                 if Path(f"{letter}:\\").exists()]
    else:
        for base in (Path("/run/media") / home.name, Path("/media") / home.name,
                     Path("/Volumes"), Path("/mnt")):
            try:
                roots.extend(sorted(p for p in base.iterdir() if p.is_dir()))
            except OSError:
                pass
    places.extend((p.name or str(p), p) for p in roots)
    return places


def fuzzy_match(query: str, name: str) -> bool:
    """Case-insensitive subsequence match: 'cmp07' finds 'campaign_07_23'."""
    query, name = query.casefold().replace(" ", ""), name.casefold()
    position = 0
    for char in query:
        position = name.find(char, position)
        if position < 0:
            return False
        position += 1
    return True
