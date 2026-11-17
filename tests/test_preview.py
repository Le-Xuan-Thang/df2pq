"""Preview reads a sample with the batch's options and flags suspicious settings."""
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from df2pq.converter import IOOptions
from df2pq.preview import preview_file

HEADERLESS = "0.0;11.7;11.6\n0.01;11.8;11.5\n0.02;11.9;11.4\n"


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def test_data_row_used_as_header_is_flagged(self):
        path = self.write("a.csv", HEADERLESS)
        p = preview_file(path, "csv", "parquet", IOOptions(input_separator=";"))
        self.assertEqual(p.after.columns, ["0.0", "11.7", "11.6"])
        self.assertEqual(len(p.after.rows), 2)  # the first row was consumed as names
        self.assertTrue(any("first row is probably data" in w for w in p.warnings))

    def test_correct_headerless_settings_are_clean(self):
        path = self.write("a.csv", HEADERLESS)
        p = preview_file(path, "csv", "parquet", IOOptions(input_separator=";", header=False))
        self.assertEqual(p.warnings, [])
        self.assertEqual(p.error, "")
        self.assertEqual(len(p.after.rows), 3)
        self.assertEqual(p.after.rows[0][0], "0.0")
        self.assertEqual(p.after.dtypes[1], "float64")
        self.assertEqual(p.raw_lines[0], "0.0;11.7;11.6")

    def test_wrong_separator_suggests_the_right_one(self):
        path = self.write("a.csv", HEADERLESS)
        p = preview_file(path, "csv", "parquet", IOOptions(header=False))
        self.assertEqual(len(p.after.columns), 1)
        self.assertTrue(any("“;”" in w for w in p.warnings))

    def test_missed_header_is_flagged(self):
        path = self.write("a.csv", "time,value\n1,2\n3,4\n")
        p = preview_file(path, "csv", "csv", IOOptions(header=False))
        self.assertTrue(any("looks like a header" in w for w in p.warnings))
        clean = preview_file(path, "csv", "csv", IOOptions())
        self.assertEqual(clean.warnings, [])
        self.assertEqual(clean.after.columns, ["time", "value"])

    def test_numeric_text_header_is_not_mistaken_for_data(self):
        # Names like "2023" are numeric; ordinary names must not be flagged.
        path = self.write("a.csv", "id,name\n1,x\n")
        self.assertEqual(preview_file(path, "csv", "parquet", IOOptions()).warnings, [])

    def test_only_the_first_rows_are_read(self):
        path = self.write("big.csv", "x\n" + "".join(f"{i}\n" for i in range(10_000)))
        p = preview_file(path, "csv", "parquet", IOOptions(), rows=5)
        self.assertEqual([r[0] for r in p.after.rows], ["0", "1", "2", "3", "4"])
        self.assertEqual(len(p.raw_lines), 6)

    def test_binary_input_shows_table_before(self):
        path = self.root / "a.parquet"
        pd.DataFrame({"x": [1, 2, 3]}).to_parquet(path)
        p = preview_file(path, "parquet", "csv", IOOptions(), rows=2)
        self.assertEqual(p.raw_lines, [])
        self.assertEqual(p.before.rows, [["1"], ["2"]])
        self.assertEqual(p.after.columns, ["x"])

    def test_unreadable_file_reports_error_instead_of_raising(self):
        # e.g. a macOS "._" metadata file picked up as CSV
        path = self.root / "._a.csv"
        path.write_bytes(b"\x00\x05\x16\x07\x00\x02Mac OS X\x00\x00")
        p = preview_file(path, "parquet", "csv", IOOptions())
        self.assertTrue(p.error.startswith("Could not read"))


if __name__ == "__main__":
    unittest.main()
