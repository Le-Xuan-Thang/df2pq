"""Converting several files at once in worker processes (PyArrow and pandas)."""
from pathlib import Path
import tempfile
import unittest

import pyarrow.parquet as pq

from df2pq.converter import IOOptions
from df2pq.runner import BatchRunner


def drain(runner):
    events = []
    while not runner.events.empty():
        events.append(runner.events.get())
    return events


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()
        for i in range(10):
            (self.src / f"r{i}.csv").write_text("".join(f"{j}.5;{i}.25\n" for j in range(50)))
        (self.src / "r_cut.csv").write_text("1.5;2.5\n3.5;4.5\n5.5")  # last row cut off

    def tearDown(self):
        self.tmp.cleanup()

    def run_batch(self, runner, engine, workers):
        out = Path(self.tmp.name) / f"out-{engine}-{workers}"
        runner._run(sources=[self.src], output=out, input_format="csv",
                    output_format="parquet", engine=engine,
                    options=IOOptions(input_separator=";", header=False), recursive=True,
                    limit=None, overwrite=False, dry_run=False, workers=workers)
        return out, drain(runner)

    def test_parallel_matches_sequential_output_and_reports_every_file(self):
        for engine in ("pyarrow", "pandas"):
            with self.subTest(engine=engine):
                out, events = self.run_batch(BatchRunner(), engine, workers=4)
                files = [p for kind, p in events if kind == "file"]
                self.assertEqual(len(files), 11)
                self.assertEqual(sorted(i for i, *_ in files), list(range(1, 12)))
                self.assertEqual(events[-1], ("done", (11, 0, 0, False)))
                notes = {job.source.name: detail for _, _, job, _, detail in files}
                self.assertIn("dropped 1 damaged row(s) at line 3", notes["r_cut.csv"])
                sequential, _ = self.run_batch(BatchRunner(), engine, workers=1)
                for path in sorted((sequential / "src").glob("*.parquet")):
                    self.assertTrue(pq.read_table(path).equals(
                        pq.read_table(out / "src" / path.name)), path.name)

    def test_cancel_finishes_started_files_and_starts_no_more(self):
        runner = BatchRunner()
        runner.cancel_event.set()  # cancelled before the first file completes
        out, events = self.run_batch(runner, "pyarrow", workers=2)
        self.assertEqual(events[-1][0], "cancelled")
        written = list((out / "src").glob("*.parquet"))
        self.assertEqual(len(written), 4)  # the files already handed to the 2 workers
        self.assertEqual(list((out / "src").glob(".*")), [])  # no temporary files left


if __name__ == "__main__":
    unittest.main()
