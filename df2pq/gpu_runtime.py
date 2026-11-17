"""User-owned GPU environment for both source and frozen installations.

Never import its extension modules into PyInstaller's Python. A standalone
worker exchanges JSON over stdin/stdout and receives paths, not file contents.
"""
from __future__ import annotations

import atexit
from dataclasses import asdict
import hashlib
import io
import json
import os
import queue
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
from typing import Callable
from urllib.request import urlopen

UV_VERSION = "0.13.5"
_lock = threading.Lock()


def data_dir() -> Path:
    base = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            if sys.platform == "win32" else
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")))
    return base / "df2pq" / "gpu"


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    # PyInstaller changes the loader search path. Restore it for external tools.
    for key in ("LD_LIBRARY_PATH", "LIBPATH"):
        original = env.pop(key + "_ORIG", None)
        env.pop(key, None)
        if original:
            env[key] = original
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def execute(command, *, input=None, timeout=60):
    result = subprocess.run(command, input=input, capture_output=True, text=True,
                            timeout=timeout, env=child_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or
                            f"Command exited with {result.returncode}")[-3000:].strip())
    return result.stdout.strip()


class Runtime:
    def __init__(self):
        self.wsl = sys.platform == "win32"
        if sys.platform not in ("linux", "win32"):
            raise RuntimeError("Automatic GPU setup supports Linux and Windows with WSL2.")
        self.prefix = ["wsl.exe", "--exec"] if self.wsl else []
        if self.wsl:
            try:
                kernel = execute(self.prefix + ["uname", "-r"])
                if "wsl2" not in kernel.lower() and "microsoft-standard" not in kernel.lower():
                    raise RuntimeError("WSL2 is required")
                home = execute(self.prefix + ["printenv", "HOME"])
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                raise RuntimeError("GPU support requires WSL2 with a Linux distribution. "
                                   "Install Ubuntu with 'wsl --install -d Ubuntu', restart "
                                   "Windows if requested, and complete Ubuntu setup.") from exc
            self.root = home + "/.local/share/df2pq/gpu"
        else:
            self.root = str(data_dir())
        self.python = self.root + "/env/bin/python"

    def run(self, args, **kwargs):
        return execute(self.prefix + list(args), **kwargs)

    def path(self, path: Path) -> str:
        resolved = str(path.resolve())
        return self.run(["wslpath", "-a", "-u", resolved]) if self.wsl else resolved

    def exists(self, path: str) -> bool:
        if not self.wsl:
            return Path(path).is_file()
        try:
            self.run(["test", "-f", path])
            return True
        except RuntimeError:
            return False

    def write_sources(self):
        # The converter is shipped as data as well as code in frozen builds.
        source = Path(__file__).parent
        if self.wsl:
            self.run(["mkdir", "-p", self.root + "/worker"])
        else:
            Path(self.root, "worker").mkdir(parents=True, exist_ok=True)
        for name in ("converter.py", "gpu_worker.py"):
            content = (source / name).read_text(encoding="utf-8")
            target = self.root + "/worker/" + name
            if self.wsl:
                self.run(["tee", target], input=content)
            else:
                Path(target).write_text(content, encoding="utf-8")

    def worker(self, request, *, timeout=None):
        """Send one request to this session's long-lived GPU worker."""
        command = self.prefix + [self.python, self.root + "/worker/gpu_worker.py"]
        with _servers_lock:
            server = _servers.get(tuple(command))
            if server is None or not server.alive():
                server = _servers[tuple(command)] = WorkerServer(command)
        return server.request(request, timeout)

    def convert_job(self, job, output_format, options, *, overwrite):
        payload = asdict(job)
        for key in ("source_root", "source", "target"):
            payload[key] = str(Path(payload[key]).resolve())
        # The worker translates Windows paths itself, without a wsl.exe call per path.
        return self.worker({"action": "convert", "job": payload, "wsl": self.wsl,
                            "output_format": output_format, "options": asdict(options),
                            "overwrite": overwrite})


class WorkerServer:
    """One cuDF process reused for every file: importing cuDF and creating the
    CUDA context take seconds, while converting a typical file takes milliseconds."""

    def __init__(self, command):
        log_path = data_dir() / "worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self.lock = threading.Lock()
        with log_path.open("w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.replies: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, name="df2pq-gpu-worker", daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.replies.put(line)
        except (OSError, ValueError):  # pipe closed by close()
            pass
        self.replies.put(None)

    def alive(self) -> bool:
        return self.process.poll() is None

    def close(self):
        try:
            self.process.stdin.close()  # end of input lets the worker exit cleanly
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()

    def _failure(self, message):
        self.process.kill()
        self.close()
        tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-1500:].strip()
        return RuntimeError(f"{message}. {tail}".strip())

    def request(self, payload, timeout=None):
        with self.lock:
            try:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
            except OSError:
                raise self._failure("GPU worker stopped unexpectedly") from None
            try:
                line = self.replies.get(timeout=timeout)
            except queue.Empty:
                raise self._failure(f"GPU worker did not answer within {timeout} seconds") from None
            if line is None:
                raise self._failure("GPU worker stopped unexpectedly")
        reply = json.loads(line)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]


_servers: dict[tuple[str, ...], WorkerServer] = {}
_servers_lock = threading.Lock()


def shutdown_workers():
    with _servers_lock:
        for server in _servers.values():
            server.close()
        _servers.clear()


atexit.register(shutdown_workers)


def managed_backend() -> Runtime | None:
    runtime = Runtime()
    if runtime.exists(runtime.root + "/ready"):
        runtime.write_sources()
        return runtime
    return None


def _download_uv(runtime: Runtime, progress: Callable[[str], None] = lambda message: None) -> str:
    """Download a versioned official binary and verify its published SHA256."""
    arch = runtime.run(["uname", "-m"])
    if arch not in ("x86_64", "aarch64"):
        raise RuntimeError(f"Unsupported GPU runtime architecture: {arch}")
    asset = f"uv-{arch}-unknown-linux-gnu.tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{asset}"
    with urlopen(url + ".sha256", timeout=60) as response:
        expected = response.read(1024).decode().split()[0]
    with urlopen(url, timeout=120) as response:
        total = int(response.headers.get("Content-Length", 0))
        chunks = []
        downloaded = 0
        while chunk := response.read(256 * 1024):
            chunks.append(chunk)
            downloaded += len(chunk)
            if downloaded > 100 * 1024 * 1024:
                raise RuntimeError("GPU installer archive exceeds the size limit")
            progress(byte_progress("GPU installer", downloaded, total))
        archive = b"".join(chunks)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise RuntimeError("GPU installer download failed SHA256 verification")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember(asset.removesuffix(".tar.gz") + "/uv")
        if not member.isfile():
            raise RuntimeError("Invalid GPU installer archive")
        binary = tar.extractfile(member).read()
    local_root = data_dir()
    local_root.mkdir(parents=True, exist_ok=True)
    local = local_root / "uv-download"
    local.write_bytes(binary)
    if runtime.wsl:
        runtime.run(["cp", runtime.path(local), runtime.root + "/uv"])
    else:
        local.replace(Path(runtime.root, "uv"))
    runtime.run(["chmod", "700", runtime.root + "/uv"])
    return runtime.root + "/uv"


def download_percent(message: str) -> int | None:
    match = re.search(r"\b(\d{1,3})%", message)
    return min(100, int(match[1])) if match else None


def byte_progress(package: str, downloaded: int, total: int) -> str:
    amount = f"{downloaded / 1024**2:.1f} MB"
    if total > 0:
        percent = min(100, downloaded * 100 // total)
        return f"Downloading {package}: {percent}% · {amount} / {total / 1024**2:.1f} MB"
    return f"Downloading {package}: {amount}"


class PackageProgress:
    """Interpret pip's raw byte counters, including resumed and cached downloads."""
    def __init__(self, progress: Callable[[str], None]):
        self.progress = progress
        self.package = "GPU package"

    def feed(self, line: str):
        line = line.strip()
        if line.startswith(("Downloading ", "Using cached ")):
            artifact = line.split()[2 if line.startswith("Using cached ") else 1]
            self.package = artifact.rsplit("/", 1)[-1].split("-")[0].replace("_", "-")
            if ".metadata" in line:
                self.progress(f"Resolving GPU packages: {self.package}…")
            elif line.startswith("Using cached "):
                self.progress(f"Using cached GPU package: {self.package}")
            else:
                self.progress(f"Downloading {self.package}: 0%")
        elif match := re.fullmatch(r"Progress (\d+) of (\d+)", line):
            self.progress(byte_progress(self.package, int(match[1]), int(match[2])))
        elif line.startswith("Installing collected packages:"):
            self.progress("Installing downloaded GPU packages…")


def stream_install(command, progress: Callable[[str], None], log_path: Path, *, timeout=7200):
    """Read flushed progress continuously; preserve full diagnostics on disk."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    parser = PackageProgress(progress)
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", bufsize=1,
                              env=child_env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            timed_out = threading.Event()

            def stop():
                timed_out.set()
                process.kill()

            timer = threading.Timer(timeout, stop)
            timer.daemon = True
            timer.start()
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    parser.feed(line)
                result = process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
    if timed_out.is_set():
        raise RuntimeError("GPU package download timed out. Retry GPU setup to continue.")
    if result:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
        raise RuntimeError(f"GPU package download failed. {tail}")


def cuda_major(driver: str, capability: str) -> str:
    version = tuple(int(n) for n in driver.strip().split("."))
    compute = float(capability)
    if compute < 7.0:
        raise RuntimeError("cuDF requires an NVIDIA GPU with compute capability 7.0 or newer.")
    if version >= (580, 65, 6) and compute >= 7.5:
        return "13"
    if version >= (525, 60, 13):
        return "12"
    raise RuntimeError("NVIDIA driver is too old. Update it to 525.60.13 or newer.")


def install(progress: Callable[[str], None]) -> Runtime:
    """Install once per user; errors leave a repairable environment and setup log."""
    with _lock:
        shutdown_workers()  # never keep a worker from the environment being repaired
        runtime = Runtime()
        runtime.write_sources()
        progress("Checking NVIDIA driver…")
        smi = "nvidia-smi"
        if runtime.wsl:
            smi = "/usr/lib/wsl/lib/nvidia-smi"
        info = runtime.run([smi, "--query-gpu=driver_version,compute_cap", "--format=csv,noheader"])
        driver, capability = info.splitlines()[0].split(",")
        cuda = cuda_major(driver, capability)
        # A failed repair must never leave the environment advertised as ready.
        if runtime.wsl:
            runtime.run(["rm", "-f", runtime.root + "/ready"])
        else:
            Path(runtime.root, "ready").unlink(missing_ok=True)
        progress("Downloading GPU installer…")
        uv = None if runtime.wsl else shutil.which("uv")
        uv = uv or (runtime.root + "/uv" if runtime.exists(runtime.root + "/uv") else
                    _download_uv(runtime, progress))
        progress("Preparing Python 3.12 for GPU support…")
        # uv downloads Python too; no system Python or administrator is required.
        if not runtime.exists(runtime.python):
            runtime.run([uv, "--no-config", "venv", "--python", "3.12", "--managed-python",
                         runtime.root + "/env"], timeout=600)
        progress("Preparing GPU package downloader…")
        runtime.run([uv, "--no-config", "pip", "install", "--python", runtime.python,
                     "--index-url", "https://pypi.org/simple", "pip>=25.2,<27"], timeout=600)
        progress(f"Resolving CUDA {cuda} and cuDF packages…")
        command = runtime.prefix + [runtime.python, "-u", "-m", "pip", "--isolated", "install",
                                   "--disable-pip-version-check", "--progress-bar", "raw",
                                   "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
                                   f"cudf-cu{cuda}==26.10.*",
                                   ("nvidia-cuda-nvrtc>=13,<14" if cuda == "13" else "nvidia-cuda-nvrtc-cu12"),
                                   ("nvidia-cuda-runtime>=13,<14" if cuda == "13" else "nvidia-cuda-runtime-cu12"),
                                   "pyarrow>=16", "pandas>=2.2"]
        stream_install(command, progress, data_dir() / "setup.log")
        progress("Testing GPU computation…")
        runtime.worker({"action": "probe"}, timeout=120)
        if runtime.wsl:
            runtime.run(["touch", runtime.root + "/ready"])
        else:
            Path(runtime.root, "ready").touch()
        return runtime
