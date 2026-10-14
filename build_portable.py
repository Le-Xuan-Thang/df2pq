"""Build the platform-native df2pq bundle with PyInstaller."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess
import sys


def main() -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "df2pq",
        "--hidden-import=openpyxl",
        "--hidden-import=xlrd",
        "--hidden-import=pyarrow.csv",
        "--hidden-import=pyarrow.parquet",
        "--hidden-import=pyarrow.feather",
        "--hidden-import=pyarrow.orc",
        "--hidden-import=pyarrow.ipc",
        str(Path(__file__).with_name("build_gui.py")),
    ]
    if platform.system() == "Darwin":
        command.extend(["--osx-bundle-identifier", "org.df2pq.app"])

    environment = os.environ.copy()
    if platform.system() == "Linux":
        python_library_dir = Path(sys.base_prefix) / "lib"
        current_library_path = environment.get("LD_LIBRARY_PATH")
        environment["LD_LIBRARY_PATH"] = str(python_library_dir)
        if current_library_path:
            environment["LD_LIBRARY_PATH"] += os.pathsep + current_library_path

    subprocess.run(command, check=True, env=environment)


if __name__ == "__main__":
    main()
