from pathlib import Path
import tempfile
import unittest

import pandas as pd

from df2pq.converter import ConversionJob, IOOptions, convert_one
from df2pq.runner import BatchRunner
from df2pq.scan import fuzzy_match as _fuzzy, human_size, summarize_folder
from df2pq.settings import Settings


class _BrokenGpu:
    def __getattr__(self, name):
        raise MemoryError("simulated GPU out of memory")


class HelperTests(unittest.TestCase):
    def test_fuzzy_matches_subsequence_case_insensitively(self):
        self.assertTrue(_fuzzy("cmp07", "Campaign_07_23"))
        self.assertFalse(_fuzzy("cmp12", "campaign_07_23"))

    def test_summarize_folder_counts_formats_and_skips_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / ".hidden").mkdir()
            (root / "a.csv").write_text("x\n1\n")
            (root / "sub" / "b.CSV").write_text("x\n1\n")
            (root / "sub" / "c.parquet").write_bytes(b"x")
            (root / ".hidden" / "d.csv").write_text("x\n1\n")
            (root / "notes.txt").write_text("ignore")
            deep = summarize_folder(root)
            self.assertEqual(dict(deep.by_format), {"csv": 2, "parquet": 1})
            self.assertEqual(deep.files(frozenset({"csv"})), 2)
            self.assertEqual(summarize_folder(root, recursive=False).files(), 1)

    def test_human_size(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(3 * 1024 * 1024), "3.0 MB")

    def test_settings_roundtrip_and_recent_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            settings = Settings(path)
            settings.remember(Path("/a"))
            settings.remember(Path("/b"))
            settings.remember(Path("/a"))
            settings.save()
            self.assertEqual(Settings(path).recent(), [Path("/a"), Path("/b")])

    def test_gpu_failure_falls_back_to_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.csv").write_text("x,y\n1,2\n")
            job = ConversionJob(root, root / "a.csv", "csv", root / "out" / "a.parquet")
            result = convert_one(job, "parquet", "cudf", IOOptions(), _BrokenGpu(),
                                 fallback=("pandas", pd))
            self.assertEqual(result, "converted_cpu")
            self.assertEqual(pd.read_parquet(job.target).to_dict("list"), {"x": [1], "y": [2]})
            with self.assertRaises(MemoryError):
                convert_one(
                    ConversionJob(root, root / "a.csv", "csv", root / "out" / "b.parquet"),
                    "parquet", "cudf", IOOptions(), _BrokenGpu(),
                )

    def test_batch_runner_reports_events(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "in", Path(tmp) / "out"
            src.mkdir()
            (src / "a.csv").write_text("x\n1\n")
            runner = BatchRunner()
            self.assertTrue(runner.start(
                sources=[src], output=out, input_format="auto", output_format="parquet",
                engine="pandas", options=IOOptions(), recursive=True, limit=None,
                overwrite=False, dry_run=False))
            kinds = []
            deadline = time.time() + 20
            while "done" not in kinds and time.time() < deadline:
                try:
                    kinds.append(runner.events.get(timeout=1)[0])
                except Exception:
                    pass
            self.assertEqual(kinds, ["plan", "file", "done"])
            self.assertTrue((out / "in" / "a.parquet").exists())


if __name__ == "__main__":
    unittest.main()
