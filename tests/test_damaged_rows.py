"""Damaged CSV rows, hidden files, unreadable formats in auto mode, exact floats, JSON output.

The damaged-file shapes come from interrupted recordings seen in real data: a last
line cut mid-row without a line break, CRLF line endings, and zero-byte padding.
"""
from pathlib import Path
import json
import tempfile
import unittest

import pandas as pd
import pyarrow
import pyarrow.parquet as pq

from df2pq.converter import ConversionJob, IOOptions, build_jobs, check_text_rows, convert_one

ROWS = "".join(f"{i}.5;1.25;2.5\r\n" for i in range(5))
CUT = ROWS + "5.5;1.2"          # last row cut after two fields, no line break
PADDED = CUT.encode() + b"\0" * 4096


def options(policy):
    return IOOptions(input_separator=";", header=False, bad_rows=policy)


class DamagedRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def convert(self, name, data, engine, policy, backend):
        source = self.root / name
        source.write_bytes(data if isinstance(data, bytes) else data.encode())
        target = self.root / "out" / f"{engine}-{policy}-{source.stem}.parquet"
        job = ConversionJob(self.root, source, "csv", target)
        notes = []
        result = convert_one(job, "parquet", engine, options(policy), backend, notes=notes)
        return result, pq.read_table(job.target).to_pandas(), notes

    def test_every_policy_behaves_the_same_on_each_cpu_engine(self):
        for engine, backend in (("pandas", pd), ("pyarrow", pyarrow)):
            for name, data in (("cut.csv", CUT), ("padded.csv", PADDED)):
                with self.subTest(engine=engine, file=name):
                    result, table, notes = self.convert(name, data, engine, "drop", backend)
                    self.assertEqual((result, len(table)), ("converted", 5))
                    self.assertEqual(table.isna().sum().sum(), 0)
                    self.assertIn("dropped 1 damaged row(s) at line 6", notes[0])
                    if name == "padded.csv":
                        self.assertIn("removed 4,096 trailing zero bytes", notes[0])
                    self.assertEqual(table.iloc[-1].tolist(), [4.5, 1.25, 2.5])

                    result, table, notes = self.convert(name, data, engine, "keep", backend)
                    self.assertEqual(len(table), 6)
                    self.assertEqual(table.iloc[-1, :2].tolist(), [5.5, 1.2])
                    self.assertTrue(pd.isna(table.iloc[-1, 2]))
                    self.assertIn("kept 1 incomplete row(s)", notes[0])

                    with self.assertRaisesRegex(ValueError, "1 damaged row"):
                        self.convert(name, data, engine, "fail", backend)
                    self.assertFalse(
                        (self.root / "out" / f"{engine}-fail-{Path(name).stem}.parquet").exists())

    def test_zero_padding_is_reported(self):
        cleaned, note = check_text_rows(self._write("p.csv", PADDED), "csv", options("drop"))
        self.assertTrue(note.startswith("removed 4,096 trailing zero bytes"))
        self.assertTrue(cleaned.endswith(b"\n"))  # CRLF files must not end in a bare "\r"

    def test_clean_files_are_read_untouched(self):
        cleaned, note = check_text_rows(self._write("ok.csv", ROWS), "csv", options("fail"))
        self.assertEqual((cleaned, note), (None, ""))

    def test_overlong_rows_are_dropped_even_when_keeping(self):
        path = self._write("long.csv", ROWS + "1;2;3;4\r\n")
        cleaned, note = check_text_rows(path, "csv", options("keep"))
        self.assertIn("dropped 1 damaged row(s) at line 6", note)
        self.assertNotIn(b"1;2;3;4", cleaned)

    def test_quoted_files_are_left_to_the_parser(self):
        # A quoted separator must not be counted as an extra field.
        path = self._write("q.csv", 'id,name\n1,"a,b"\n2,c\n')
        self.assertEqual(check_text_rows(path, "csv", IOOptions()), (None, ""))

    def test_header_row_sets_the_expected_field_count(self):
        path = self._write("h.csv", "x,y\n1,2\n3\n")
        _, note = check_text_rows(path, "csv", IOOptions())
        self.assertIn("line 3", note)

    def _write(self, name, data):
        path = self.root / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


class DiscoveryTests(unittest.TestCase):
    def test_hidden_files_and_folders_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            (src / ".tool").mkdir(parents=True)
            (src / "a.csv").write_text("x\n1\n")
            (src / "._a.csv").write_bytes(b"\0\5\26\7Mac OS X")
            (src / ".tool" / "log.jsonl").write_text('{"x": 1}\n')
            jobs = build_jobs([src], Path(tmp) / "out", "auto", "parquet", "pandas")
            self.assertEqual([j.source.name for j in jobs], ["a.csv"])

    def test_auto_mode_lists_unreadable_formats_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "a.csv").write_text("x\n1\n")
            (src / "b.xlsx").write_bytes(b"not read")
            ignored = []
            jobs = build_jobs([src], Path(tmp) / "out", "auto", "parquet", "pyarrow",
                              ignored=ignored)
            self.assertEqual([j.source.name for j in jobs], ["a.csv"])
            self.assertEqual([(p.name, r) for p, r in ignored],
                             [("b.xlsx", "pyarrow cannot read excel files")])
            with self.assertRaisesRegex(ValueError, "cannot read excel"):
                build_jobs([src], Path(tmp) / "out", "excel", "parquet", "pyarrow")


class ValueTests(unittest.TestCase):
    def test_pandas_reads_decimal_text_exactly(self):
        # The default pandas parser returns ...537 for this value.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.csv"
            source.write_text("11.707061553539539;0.1\n")
            job = ConversionJob(Path(tmp), source, "csv", Path(tmp) / "a.parquet")
            convert_one(job, "parquet", "pandas", options("drop"), pd)
            self.assertEqual(pq.read_table(job.target).column(0)[0].as_py(),
                             float("11.707061553539539"))

    def test_json_lines_option_only_affects_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.json"
            source.write_text('{"x": 1}\n{"x": 2}\n')
            job = ConversionJob(Path(tmp), source, "json", Path(tmp) / "out.json")
            convert_one(job, "json", "pandas", IOOptions(json_lines=True), pd)
            self.assertEqual(json.loads(job.target.read_text()), [{"x": 1}, {"x": 2}])


if __name__ == "__main__":
    unittest.main()
