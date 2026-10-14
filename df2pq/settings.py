"""Small JSON settings store (recent folders, last choices)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

MAX_RECENT = 12


def _settings_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "df2pq" / "settings.json"


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _settings_path()
        self.data: dict[str, Any] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def recent(self, key: str = "recent_sources") -> list[Path]:
        return [Path(p) for p in self.data.get(key, []) if isinstance(p, str)]

    def remember(self, path: Path, key: str = "recent_sources") -> None:
        items = [str(path)] + [p for p in self.data.get(key, []) if p != str(path)]
        self.data[key] = items[:MAX_RECENT]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass  # settings are a convenience; never block the app on them
