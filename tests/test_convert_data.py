"""Tests for the general-purpose multi-folder converter."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest

import pandas as pd
import pyarrow.parquet as pq

from df2pq.converter import main


class ConvertDataTests(unittest.TestCase):
    def test_converts_multiple_sources_and_preserves_folder_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source_one = temporary / "source_one"
            source_two = temporary / "source_two"
            (source_one / "nested").mkdir(parents=True)
            source_two.mkdir()
            pd.DataFrame({"time": [0.0, 0.1], "value": [1.5, 2.5]}).to_csv(
                source_one / "nested" / "first.csv", index=False
            )
            pd.DataFrame({"time": [0.2], "value": [3.5]}).to_csv(
                source_two / "second.csv", index=False
            )
            output = temporary / "converted"
            arguments = [
                "--source", str(source_one),
                "--source", str(source_two),
                "--output", str(output),
                "--input-format", "csv",
                "--output-format", "parquet",
                "--engine", "pandas",
            ]

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)

            first_output = output / "source_one" / "nested" / "first.parquet"
            second_output = output / "source_two" / "second.parquet"
            self.assertTrue(first_output.is_file())
            self.assertTrue(second_output.is_file())
            pd.testing.assert_frame_equal(
                pd.read_parquet(first_output),
                pd.DataFrame({"time": [0.0, 0.1], "value": [1.5, 2.5]}),
            )
            self.assertEqual(pq.read_table(second_output).num_rows, 1)

            # Existing destinations are skipped unless --overwrite is requested.
            pd.DataFrame({"time": [9.9], "value": [9.9]}).to_csv(
                source_two / "second.csv", index=False
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)
            self.assertEqual(pd.read_parquet(second_output)["value"].tolist(), [3.5])

    def test_pyarrow_reads_headerless_semicolon_csv_and_dry_run_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source = temporary / "source_csv"
            source.mkdir()
            (source / "record.csv").write_text("0.00;11.70\n0.01;11.71\n", encoding="utf-8")
            output = temporary / "parquet"
            arguments = [
                "--source", str(source),
                "--output", str(output),
                "--input-format", "csv",
                "--output-format", "parquet",
                "--engine", "pyarrow",
                "--input-separator", ";",
                "--no-header",
            ]

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*arguments, "--dry-run"]), 0)
            self.assertFalse(output.exists())

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)
            table = pq.read_table(output / "source_csv" / "record.parquet")
            self.assertEqual(table.column_names, ["f0", "f1"])
            self.assertEqual(table.column("f1").to_pylist(), [11.70, 11.71])

    def test_pyarrow_ipc_output_can_be_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source = temporary / "source"
            source.mkdir()
            (source / "record.csv").write_text("time,value\n0.0,1.5\n", encoding="utf-8")
            arrow_output = temporary / "arrow_output"
            base_arguments = [
                "--input-format", "csv",
                "--output-format", "ipc",
                "--engine", "pyarrow",
            ]

            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main([
                        "--source", str(source),
                        "--output", str(arrow_output),
                        *base_arguments,
                    ]),
                    0,
                )

            restored_output = temporary / "restored_output"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main([
                        "--source", str(arrow_output / "source"),
                        "--output", str(restored_output),
                        "--input-format", "ipc",
                        "--output-format", "parquet",
                        "--engine", "pyarrow",
                    ]),
                    0,
                )
            restored = pq.read_table(restored_output / "source" / "record.parquet")
            self.assertEqual(restored.column("value").to_pylist(), [1.5])

    def test_auto_detects_json_lines_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source = temporary / "source"
            source.mkdir()
            (source / "events.jsonl").write_text(
                '{"event":"a","value":1}\n{"event":"b","value":2}\n',
                encoding="utf-8",
            )
            output = temporary / "output"

            with redirect_stdout(io.StringIO()):
                result = main([
                    "--source", str(source),
                    "--output", str(output),
                    "--output-format", "json",
                    "--engine", "pandas",
                ])

            self.assertEqual(result, 0)
            converted = pd.read_json(output / "source" / "events.json")
            self.assertEqual(converted["event"].tolist(), ["a", "b"])
            self.assertEqual(converted["value"].tolist(), [1, 2])

    @unittest.skipUnless(
        os.environ.get("DF2PQ_TEST_CUDF") == "1",
        "Set DF2PQ_TEST_CUDF=1 to run the optional GPU integration test.",
    )
    def test_cudf_csv_to_parquet_when_a_gpu_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            previous_directory = Path.cwd()
            try:
                # cuFile writes its diagnostic log to the current directory.
                # Keep this optional GPU test's generated log inside the temp tree.
                os.chdir(temporary)
                try:
                    import cudf

                    cudf.DataFrame({"probe": [1]}).to_pandas()
                except Exception as exc:
                    self.skipTest(f"cuDF/GPU is unavailable: {exc}")

                source = temporary / "source"
                source.mkdir()
                (source / "record.csv").write_text(
                    "time,value\n0.0,1.5\n", encoding="utf-8"
                )
                output = temporary / "output"
                with redirect_stdout(io.StringIO()):
                    result = main([
                        "--source", str(source),
                        "--output", str(output),
                        "--input-format", "csv",
                        "--output-format", "parquet",
                        "--engine", "cudf",
                    ])

                self.assertEqual(result, 0)
                self.assertEqual(
                    pd.read_parquet(output / "source" / "record.parquet")["value"].tolist(),
                    [1.5],
                )
            finally:
                os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
