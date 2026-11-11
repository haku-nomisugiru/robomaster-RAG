"""Start the packaged service with an ASCII relative path for native Chroma."""

import os
from pathlib import Path
import runpy

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
    runpy.run_module("http_service", run_name="__main__")
