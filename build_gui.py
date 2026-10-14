"""PyInstaller entry point for building the df2pq desktop application."""

import os
import sys

# A windowed build has no console, so stdout/stderr may be None; the web server
# and uvicorn's logging write to them.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))

from df2pq.web import main


if __name__ == "__main__":
    main()
