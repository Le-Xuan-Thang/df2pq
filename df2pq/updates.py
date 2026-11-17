"""Version info, changelog, and update checking (GitHub releases)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import metadata, resources
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
import urllib.error
import urllib.request

REPO = "Le-Xuan-Thang/df2pq"
RELEASES_URL = f"https://github.com/{REPO}/releases"
# DF2PQ_RELEASE_API points the update check at another server, for testing updates.
_API_LATEST = os.environ.get("DF2PQ_RELEASE_API",
                             f"https://api.github.com/repos/{REPO}/releases/latest")
CHECKSUMS = "SHA256SUMS"
MAX_PACKAGE_BYTES = 1024**3


class UpdateError(Exception):
    """A user-presentable failure while checking for or applying an update."""


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    url: str
    published: str
    assets: tuple[tuple[str, str], ...] = ()  # (file name, download URL)

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
        assets=tuple((str(a.get("name", "")), str(a.get("browser_download_url", "")))
                     for a in data.get("assets") or [] if isinstance(a, dict)),
    )


def install_kind() -> str:
    """How this copy was installed.

    'rpm' or 'deb' (bundle installed by the system package manager), 'portable'
    (other frozen bundle), 'git' (source checkout) or 'package' (pip/uv install).
    """
    if getattr(sys, "frozen", False):
        return system_package_kind() or "portable"
    if (git_root() / ".git").exists():
        return "git"
    return "package"


def system_env() -> dict[str, str]:
    """Environment for system tools: a frozen bundle overrides the library path."""
    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "LIBPATH"):
        original = env.pop(key + "_ORIG", None)
        env.pop(key, None)
        if original:
            env[key] = original
    return env


def system_package_kind(executable: str | None = None, run=subprocess.run) -> str | None:
    """'rpm' or 'deb' when the system package manager owns this executable."""
    path = str(Path(executable or sys.executable).resolve())
    for kind, command in (("rpm", ["rpm", "-qf", "--qf", "%{NAME}", path]),
                          ("deb", ["dpkg-query", "-S", path])):
        if shutil.which(command[0]) is None:
            continue
        try:
            result = run(command, capture_output=True, text=True, timeout=15,
                         env=system_env())
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and "df2pq" in result.stdout:
            return kind
    return None


def package_asset(release: Release, kind: str, machine: str | None = None) -> tuple[str, str]:
    """The release file for this kind of install and processor."""
    machine = (machine or platform.machine()).lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    suffix = (f".{arch}.rpm" if kind == "rpm" else
              f"_{ {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(arch, arch) }.deb")
    for name, url in release.assets:
        if name.startswith("df2pq") and name.endswith(suffix):
            return name, url
    raise UpdateError(f"Release {release.tag} has no {kind.upper()} package for {arch}.")


def _download(url: str, opener, timeout: float = 60.0):
    request = urllib.request.Request(url, headers={"User-Agent": "df2pq-update"})
    try:
        return opener(request, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def download_package(release: Release, kind: str, folder: Path,
                     progress: Callable[[int, int], None] = lambda done, total: None,
                     opener=urllib.request.urlopen, machine: str | None = None) -> Path:
    """Download this system's package and verify it against the release's SHA256SUMS."""
    name, url = package_asset(release, kind, machine)
    sums_url = next((u for n, u in release.assets if n == CHECKSUMS), None)
    if sums_url is None:
        raise UpdateError(f"Release {release.tag} publishes no {CHECKSUMS}, so the download "
                          "cannot be verified. Install it from the release page instead.")
    with _download(sums_url, opener) as response:
        sums = {}
        for line in response.read(1024 * 1024).decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1].lstrip("*")] = parts[0].lower()
    expected = sums.get(name)
    if expected is None:
        raise UpdateError(f"{CHECKSUMS} has no entry for {name}.")
    target = folder / name
    digest = hashlib.sha256()
    done = 0
    with _download(url, opener) as response, target.open("wb") as out:
        total = int(response.headers.get("Content-Length", 0) or 0)
        while chunk := response.read(1024 * 1024):
            done += len(chunk)
            if done > MAX_PACKAGE_BYTES:
                raise UpdateError("The package is larger than expected; download stopped.")
            digest.update(chunk)
            out.write(chunk)
            progress(done, total)
    if digest.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise UpdateError(f"{name} does not match its published checksum; it was deleted.")
    return target


def can_prompt_for_admin() -> bool:
    """True when pkexec can show a password dialog in this desktop session."""
    return (shutil.which("pkexec") is not None
            and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def install_command(kind: str, package: Path) -> list[str]:
    if kind == "rpm":
        return ["pkexec", "dnf", "install", "-y", str(package)]
    return ["pkexec", "apt-get", "install", "-y", str(package)]


def manual_install_command(kind: str, release: Release, machine: str | None = None) -> str:
    try:
        name = package_asset(release, kind, machine)[0]
    except UpdateError:
        name = "df2pq-<version>." + ("rpm" if kind == "rpm" else "deb")
    return f"sudo {'dnf' if kind == 'rpm' else 'apt'} install ./{name}"


def apply_package_update(release: Release, kind: str,
                         progress: Callable[[int, int], None] = lambda done, total: None,
                         run=subprocess.run, opener=urllib.request.urlopen,
                         machine: str | None = None) -> list[str]:
    """Download, verify and install the new package; the system asks for a password."""
    folder = Path(tempfile.mkdtemp(prefix="df2pq-update-"))
    try:
        package = download_package(release, kind, folder, progress, opener, machine)
        log = [f"Downloaded and verified {package.name}."]
        result = run(install_command(kind, package), capture_output=True, text=True,
                     timeout=1800, env=system_env())
        if result.returncode == 126:
            raise UpdateError("Installation was cancelled at the password prompt.")
        if result.returncode == 127:
            raise UpdateError("Not authorised: the password was not accepted, or no password "
                              "dialog is available in this session.")
        if result.returncode != 0:
            raise UpdateError("The package manager reported an error:\n"
                              + (result.stderr or result.stdout or "")[-1500:])
        log.append(f"Installed {package.name}.")
        return log
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def restart_command() -> list[str]:
    """Start this installation again (the bundle's executable, or the module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "df2pq"]


def git_root() -> Path:
    return Path(__file__).resolve().parent.parent


def update_command(kind: str) -> str:
    return {
        "git": "git pull --ff-only && uv sync --inexact",
        "package": "uv pip install --upgrade git+https://github.com/" + REPO,
        "portable": "",
        "rpm": "",
        "deb": "",
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
