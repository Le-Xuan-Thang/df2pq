"""Version info, changelog, and update checking (GitHub releases)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata, resources
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "Le-Xuan-Thang/df2pq"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


class UpdateError(Exception):
    """A user-presentable failure while checking for or applying an update."""


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    url: str
    published: str

    @property
    def version(self) -> str:
        return self.tag.lstrip("vV")


def current_version() -> str:
    try:
        return metadata.version("df2pq")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def parse_version(text: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)*", text)
    return tuple(int(part) for part in match.group().split(".")) if match else (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def changelog_text() -> str:
    try:
        return resources.files("df2pq").joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return "# Changelog\n\nNo changelog is bundled with this installation."


def fetch_latest(timeout: float = 6.0, opener=urllib.request.urlopen) -> Release | None:
    """Return the newest published release, or None when the repo has no releases yet."""
    request = urllib.request.Request(
        _API_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "df2pq-update-check"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (403, 429):
            raise UpdateError("GitHub rate limit reached — try again later.") from exc
        raise UpdateError(f"GitHub answered with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("Could not reach GitHub — check your internet connection.") from exc
    except ValueError as exc:
        raise UpdateError("GitHub returned an unreadable response.") from exc
    return Release(
        tag=str(data.get("tag_name", "")),
        notes=str(data.get("body") or "").strip(),
        url=str(data.get("html_url") or RELEASES_URL),
        published=str(data.get("published_at") or "")[:10],
    )


def install_kind() -> str:
    """'portable' (frozen bundle), 'git' (source checkout), or 'package' (pip/uv install)."""
    if getattr(sys, "frozen", False):
        return "portable"
    if (git_root() / ".git").exists():
        return "git"
    return "package"


def git_root() -> Path:
    return Path(__file__).resolve().parent.parent


def update_command(kind: str) -> str:
    return {
        "git": "git pull --ff-only && uv sync --inexact",
        "package": "uv pip install --upgrade git+https://github.com/" + REPO,
        "portable": "",
    }[kind]


def apply_git_update(root: Path | None = None, run=subprocess.run) -> list[str]:
    """Fast-forward a clean git checkout and refresh dependencies; returns log lines."""
    root = root or git_root()

    def git(*args: str) -> str:
        result = run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise UpdateError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
        return result.stdout.strip()

    if shutil.which("git") is None:
        raise UpdateError("git is not installed.")
    if git("status", "--porcelain"):
        raise UpdateError("This checkout has local changes; commit or stash them, then retry.")
    log = [git("fetch", "--tags"), git("pull", "--ff-only")]
    uv = shutil.which("uv")
    if uv:
        # --inexact keeps optional extras (e.g. cuDF) that are already installed.
        result = run([uv, "sync", "--inexact"], cwd=root, capture_output=True, text=True,
                     timeout=900)
        if result.returncode != 0:
            raise UpdateError("Dependencies could not be refreshed:\n" + (result.stderr or "")[-600:])
        log.append("Dependencies are up to date.")
    else:
        log.append("uv was not found — run `uv sync --inexact` yourself to refresh dependencies.")
    return [line for line in log if line]
