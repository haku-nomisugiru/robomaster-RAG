"""Start the packaged service with an ASCII relative path for native Chroma."""

import os
from pathlib import Path
import runpy
import sys

import hybrid_search


ROOT = Path(__file__).resolve().parents[1]
open_collection = hybrid_search.open_collection


def open_portable_collection(path):
    return open_collection(path.relative_to(ROOT))


if __name__ == "__main__":
    os.chdir(ROOT)
    hybrid_search.open_collection = open_portable_collection
    if pid_file := os.environ.get("RAG_ACCEPTANCE_PID_FILE"):
        Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
    explicit_port = any(arg == "--port" or arg.startswith("--port=") for arg in sys.argv[1:])
    try:
        runpy.run_module("http_service", run_name="__main__")
    except OSError as exc:
        if explicit_port or exc.winerror != 10048:
            raise
        print("Port 8765 is busy. Using an available port.", flush=True)
        sys.argv.extend(("--port", "0"))
        runpy.run_module("http_service", run_name="__main__")
