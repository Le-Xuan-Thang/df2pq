"""Detect whether the cuDF (GPU) engine is actually usable on this machine."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import shutil
import subprocess
from typing import Callable


@dataclass(frozen=True)
class GpuStatus:
    available: bool
    detail: str
    device: str = ""


def gpu_device_name() -> str:
    """Return e.g. 'NVIDIA GeForce GTX 1650 · 4 GB', or '' when no NVIDIA GPU is visible."""
    from .gpu_runtime import child_env

    exe = shutil.which("nvidia-smi")
    if exe is None:
        from pathlib import Path
        candidate = Path("C:/Windows/System32/nvidia-smi.exe")
        exe = str(candidate) if candidate.is_file() else None
    if exe is None:
        return ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
            env=child_env(),
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
    """Test an existing local or app-managed GPU backend without downloading."""
    from .gpu_runtime import managed_backend

    device = gpu_device_name()
    reason = "GPU packages have not been installed" if device else "No working NVIDIA GPU driver detected"
    try:
        runtime = managed_backend()
        if runtime is not None:
            try:
                runtime.worker({"action": "probe"}, timeout=120)
            except Exception as exc:
                return GpuStatus(False, str(exc), device)
            return GpuStatus(True, "GPU ready", device or "CUDA GPU")
    except Exception as exc:
        reason = str(exc)
    try:
        if importlib.util.find_spec("cudf") is not None:
            import cudf

            if int(cudf.Series([1, 2, 3]).sum()) != 6:
                raise RuntimeError("cuDF returned a wrong result")
            return GpuStatus(True, "GPU ready", device or "CUDA GPU")
    except Exception as exc:
        reason = str(exc)
    return GpuStatus(False, reason, device)


def initialize_gpu(progress: Callable[[str], None] = lambda message: None) -> GpuStatus:
    """Automatically provision GPU support when NVIDIA hardware is visible."""
    from .gpu_runtime import install

    status = probe_cudf()
    if status.available or not status.device:
        return status
    try:
        install(progress)
        return GpuStatus(True, "GPU ready", status.device)
    except Exception as exc:
        return GpuStatus(False, f"GPU setup failed: {exc}", status.device)
