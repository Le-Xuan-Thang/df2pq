"""Detect whether the cuDF (GPU) engine is actually usable on this machine."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import shutil
import subprocess


@dataclass(frozen=True)
class GpuStatus:
    available: bool
    detail: str
    device: str = ""


def gpu_device_name() -> str:
    """Return e.g. 'NVIDIA GeForce GTX 1650 · 4 GB', or '' when no NVIDIA GPU is visible."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    name, _, memory = out[0].partition(",")
    name = name.replace("NVIDIA ", "").replace("GeForce ", "").strip()
    try:
        return f"{name} · {round(int(memory) / 1024)} GB"
    except ValueError:
        return name


def probe_cudf() -> GpuStatus:
    """Import cuDF and run a tiny kernel; slow (seconds), so call from a thread."""
    device = gpu_device_name()
    try:
        if importlib.util.find_spec("cudf") is None:
            return GpuStatus(False, "cuDF is not installed (uv sync --extra gpu-cu13)", device)
    except (ImportError, ValueError):
        return GpuStatus(False, "cuDF is not installed (uv sync --extra gpu-cu13)", device)
    try:
        import cudf

        if int(cudf.Series([1, 2, 3]).sum()) != 6:
            raise RuntimeError("cuDF returned a wrong result")
    except Exception as exc:  # broken CUDA install, no driver, unsupported GPU...
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return GpuStatus(False, f"cuDF cannot run: {reason[:140]}", device)
    return GpuStatus(True, "GPU ready", device or "CUDA GPU")
