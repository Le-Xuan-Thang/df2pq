"""GPU setup and worker integration tests; no network or GPU required."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from df2pq.converter import ConversionJob, IOOptions, convert_one
from df2pq.gpu import GpuStatus, initialize_gpu
from df2pq.gpu_runtime import Runtime, child_env, cuda_major, PackageProgress, download_percent, stream_install


class GpuTests(unittest.TestCase):
    def test_download_progress_uses_real_bytes_and_resets_per_package(self):
        updates = []
        parser = PackageProgress(updates.append)
        parser.feed("Downloading https://example.org/nvidia_cublas-13.0-py3-none.whl (2.0 MB)")
        parser.feed("Progress 1048576 of 2097152")
        self.assertIn("nvidia-cublas: 50%", updates[-1])
        self.assertIn("1.0 MB / 2.0 MB", updates[-1])
        parser.feed("Progress 2097152 of 2097152")
        self.assertEqual(download_percent(updates[-1]), 100)
        parser.feed("Downloading cudf_cu13-26.10.2.whl (1.0 MB)")
        self.assertEqual(download_percent(updates[-1]), 0)
        parser.feed("Progress 1024 of 0")
        self.assertIsNone(download_percent(updates[-1]))
        parser.feed("Using cached cudf_cu13-26.10.2.whl (1.0 MB)")
        self.assertIn("Using cached", updates[-1])
        self.assertIsNone(download_percent(updates[-1]))
        parser.feed("Installing collected packages: cudf-cu13")
        self.assertIn("Installing downloaded", updates[-1])

    def test_install_stream_forwards_progress_and_preserves_errors(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "setup.log"
            updates = []
            script = ("print('Downloading example-1.0.whl'); "
                      "print('Progress 5 of 10'); print('Progress 10 of 10')")
            stream_install([sys.executable, "-u", "-c", script], updates.append, log)
            self.assertEqual(download_percent(updates[-1]), 100)
            self.assertIn("Progress 5 of 10", log.read_text())
            with self.assertRaisesRegex(RuntimeError, "network failed"):
                stream_install([sys.executable, "-c",
                                "import sys; print('network failed'); sys.exit(1)"],
                               updates.append, log)
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                stream_install([sys.executable, "-c", "import time; time.sleep(10)"],
                               updates.append, log, timeout=0.1)

    def test_cuda_selection_checks_driver_and_architecture(self):
        self.assertEqual(cuda_major("580.178.04", "8.9"), "13")
        self.assertEqual(cuda_major("550.54.14", "8.6"), "12")
        self.assertEqual(cuda_major("580.178.04", "7.0"), "12")
        with self.assertRaisesRegex(RuntimeError, "compute capability"):
            cuda_major("580.178.04", "6.1")
        with self.assertRaisesRegex(RuntimeError, "driver is too old"):
            cuda_major("510.10", "8.6")

    @patch("df2pq.gpu_runtime.install")
    @patch("df2pq.gpu.probe_cudf", return_value=GpuStatus(False, "No GPU"))
    def test_cpu_machine_does_not_download(self, probe, install):
        self.assertFalse(initialize_gpu().available)
        install.assert_not_called()

    @patch("df2pq.gpu_runtime.install")
    @patch("df2pq.gpu.probe_cudf", return_value=GpuStatus(True, "Ready", "RTX"))
    def test_existing_gpu_does_not_download(self, probe, install):
        self.assertTrue(initialize_gpu().available)
        install.assert_not_called()

    @patch("df2pq.gpu_runtime.install")
    @patch("df2pq.gpu.probe_cudf", return_value=GpuStatus(False, "Missing", "RTX"))
    def test_setup_and_errors_retain_device(self, probe, install):
        progress = Mock()
        self.assertTrue(initialize_gpu(progress).available)
        install.assert_called_once_with(progress)
        install.side_effect = RuntimeError("No space left on device")
        status = initialize_gpu(progress)
        self.assertFalse(status.available)
        self.assertEqual(status.device, "RTX")
        self.assertIn("No space", status.detail)

    def test_external_worker_does_not_inherit_frozen_loader_paths(self):
        with patch.dict("os.environ", {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/original",
                                       "PYTHONHOME": "/bundle", "PYTHONPATH": "/bundle"}):
            env = child_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/original")
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_managed_worker_protocol_and_cpu_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input with spaces.csv"
            source.write_text("x,y\n1,2\n")
            job = ConversionJob(root, source, "csv", root / "out.parquet")
            runtime = object.__new__(Runtime)
            runtime.wsl = False
            runtime.worker = Mock(side_effect=RuntimeError("GPU out of memory"))
            result = convert_one(job, "parquet", "cudf", IOOptions(), runtime,
                                 fallback=("pandas", pd))
            self.assertEqual(result, "converted_cpu")
            self.assertEqual(pd.read_parquet(job.target).to_dict("list"), {"x": [1], "y": [2]})
            payload = runtime.worker.call_args.args[0]
            self.assertEqual(payload["job"]["source"], str(source.resolve()))
            self.assertEqual(payload["options"]["header"], True)
            # Existing files must never launch a worker or get overwritten.
            runtime.worker.reset_mock()
            self.assertEqual(convert_one(job, "parquet", "cudf", IOOptions(), runtime), "skipped")
            runtime.worker.assert_not_called()

    @patch("df2pq.gpu_runtime.sys.platform", "win32")
    @patch("df2pq.gpu_runtime.execute")
    def test_windows_requires_wsl2_and_uses_linux_environment(self, execute):
        execute.side_effect = ["5.15-microsoft-standard-WSL2", "/home/test user"]
        runtime = Runtime()
        self.assertEqual(runtime.python, "/home/test user/.local/share/df2pq/gpu/env/bin/python")
        self.assertEqual(runtime.prefix, ["wsl.exe", "--exec"])
        execute.side_effect = RuntimeError("No distribution installed")
        with self.assertRaisesRegex(RuntimeError, "wsl --install"):
            Runtime()

    def test_standalone_worker_reads_and_writes_without_app_import(self):
        # A minimal cudf substitute exercises the actual standalone entry point
        # and copied converter, even on CI hosts without NVIDIA hardware.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("gpu_worker.py", "converter.py"):
                (root / name).write_bytes((Path(__file__).parents[1] / "df2pq" / name).read_bytes())
            (root / "cudf.py").write_text("from pandas import *\ndef set_option(*args): pass\n")
            source = root / "a.csv"
            source.write_text("x\n1\n2\n")
            payload = {"action": "convert", "job": {"source_root": str(root), "source": str(source),
                       "input_format": "csv", "target": str(root / "out.parquet")},
                       "output_format": "parquet", "options": {}, "overwrite": False}
            import sys
            second = dict(payload, job=dict(payload["job"], target=str(root / "b.parquet")))
            missing = dict(payload, job=dict(payload["job"], source=str(root / "missing.csv"),
                                             target=str(root / "c.parquet")))
            # One process serves many requests, one JSON line each, and survives errors.
            stdin = "".join(json.dumps(r) + "\n" for r in (payload, missing, second))
            result = subprocess.run([sys.executable, str(root / "gpu_worker.py")],
                                    input=stdin, capture_output=True, text=True, check=True)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(replies[0], {"ok": True, "result": "converted"})
            self.assertFalse(replies[1]["ok"])
            self.assertEqual(replies[2], {"ok": True, "result": "converted"})
            self.assertEqual(pd.read_parquet(root / "out.parquet")["x"].tolist(), [1, 2])
            self.assertEqual(pd.read_parquet(root / "b.parquet")["x"].tolist(), [1, 2])

    def test_runtime_reuses_one_worker_process_and_restarts_after_crash(self):
        import sys
        from df2pq import gpu_runtime
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "gpu_worker.py"
            script.write_text(
                "import json, os, sys\n"
                "for line in sys.stdin:\n"
                "    req = json.loads(line)\n"
                "    if req.get('crash'): os._exit(3)\n"
                "    print(json.dumps({'ok': True, 'result': os.getpid()}), flush=True)\n")
            (Path(tmp) / "worker").mkdir()
            script.rename(Path(tmp) / "worker" / "gpu_worker.py")
            runtime = object.__new__(Runtime)
            runtime.wsl, runtime.prefix = False, []
            runtime.root, runtime.python = tmp, sys.executable
            with patch.dict("os.environ", {"XDG_DATA_HOME": tmp}):
                try:
                    first = runtime.worker({}, timeout=30)
                    self.assertEqual(runtime.worker({}, timeout=30), first)
                    with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly"):
                        runtime.worker({"crash": True}, timeout=30)
                    self.assertNotEqual(runtime.worker({}, timeout=30), first)
                finally:
                    gpu_runtime.shutdown_workers()


if __name__ == "__main__":
    unittest.main()
