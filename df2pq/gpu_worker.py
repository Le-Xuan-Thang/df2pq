"""Standalone worker, run by the downloaded Python rather than the frozen app.

It stays alive for the whole session and serves one JSON request per stdin
line, answering with one JSON line on stdout. Importing cuDF and creating the
CUDA context costs seconds, so paying it once per session rather than once per
file is what makes the GPU engine faster than the CPU engines.
"""

import json
from pathlib import Path
import subprocess
import sys


def linux_path(path, wsl):
    # Windows hosts send Windows paths; translating here avoids a wsl.exe
    # round trip per path on the host side.
    if not wsl:
        return Path(path)
    return Path(subprocess.run(["wslpath", "-a", "-u", path], capture_output=True,
                               text=True, check=True).stdout.strip())


def handle(request, cudf):
    if request["action"] == "probe":
        if int(cudf.Series([1, 2, 3]).sum()) != 6:
            raise RuntimeError("GPU self-test returned an incorrect result")
        return "GPU ready"
    from converter import ConversionJob, IOOptions, convert_one

    job, wsl = request["job"], request.get("wsl", False)
    job = ConversionJob(linux_path(job["source_root"], wsl), linux_path(job["source"], wsl),
                        job["input_format"], linux_path(job["target"], wsl))
    notes = []
    status = convert_one(job, request["output_format"], "cudf",
                         IOOptions(**request["options"]), cudf,
                         overwrite=request["overwrite"], notes=notes)
    return {"status": status, "notes": notes}


def main():
    # Libraries may print diagnostics; reserve the real stdout for responses.
    responses = sys.stdout
    sys.stdout = sys.stderr
    import cudf

    cudf.set_option("spill", True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = {"ok": True, "result": handle(json.loads(line), cudf)}
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                     "damaged_rows": bool(getattr(exc, "damaged_rows", False))}
        responses.write(json.dumps(reply) + "\n")
        responses.flush()


if __name__ == "__main__":
    main()
