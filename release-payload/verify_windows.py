"""Exercise the Windows ZIP from a clean extraction using its public launcher."""

import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile

from build_windows import COMMIT, NAME


def sha(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def sqlite_count(path, table):
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def persisted_state(path):
    # Chroma records native write-lock acquisitions on read. Compare all other SQL data
    # and the HNSW files so normal housekeeping does not look like an embedding change.
    digest = sha256()
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'acquire_write' ORDER BY name"
        )]
        for name in tables:
            digest.update((name + "\n").encode("utf-8"))
            quoted = '"' + name.replace('"', '""') + '"'
            for row in sorted(repr(row) for row in db.execute("SELECT * FROM " + quoted)):
                digest.update((row + "\n").encode("utf-8"))
    return digest.hexdigest()


def get(base, path, **params):
    if params:
        path += "?" + urlencode(params)
    with urlopen(base + path, timeout=35) as response:
        return json.load(response)


def exercise(installation, log):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    system = Path(os.environ["SystemRoot"]) / "System32"
    pid_file = log.with_suffix(".pid")
    env = dict(os.environ)
    env.update(PATH=str(system), PYTHONHOME=str(installation / "missing-python-home"),
               PYTHONPATH=str(installation / "missing-python-path"), RAG_NO_PAUSE="1",
               RAG_ACCEPTANCE_PID_FILE=str(pid_file))

    with log.open("wb") as stream:
        process = subprocess.Popen([str(system / "cmd.exe"), "/d", "/c", "start-rag.cmd", "--port", str(port)],
                                   cwd=installation, env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            for _ in range(160):
                if process.poll() is not None:
                    raise RuntimeError(f"Launcher exited {process.returncode}. See {log}")
                try:
                    health = get(base, "/health")
                    break
                except (OSError, URLError):
                    time.sleep(0.25)
            else:
                raise RuntimeError(f"Launcher did not listen on port {port}. See {log}")
            assert health["models"] == 3468
            assert get(base, "/openapi.json")["info"]["version"] == "0.5.0"
            model = get(base, "/v1/search", q="3508 舵轮", mode="model", limit=5)
            assert model["retrieval"] == "lexical" and model["vector_status"] == "not_requested"
            top = model["items"][0]
            assert top["id"] == "rm-803685-014"
            assert any(m["code"] == "3508" and m["role"] == "used_component" for m in top["model_matches"])
            exact = get(base, "/v1/search", q="Ｐ１５．８", mode="model")
            assert {row["id"] for row in exact["items"]} == {"rm-1936175-017", "rm-1936175-018"}
            assert not get(base, "/v1/search", q="P15.76", mode="model")["items"]
            hybrid = get(base, "/v1/search", q="把轮组接到悬挂的机加工法兰",
                         mode="function", section="wheel_or_leg", limit=5)
            assert hybrid["retrieval"] == "hybrid" and hybrid["vector_status"] == "ready", hybrid["vector_status"]
            best = hybrid["items"][0]
            assert best["id"] == "rm-1935523-022"
            assert {"sqlite", "chroma"} <= best["retrieval_channels"].keys()
            return {
                "health": health["models"], "model_top": top["id"],
                "fullwidth_exact_ids": sorted(row["id"] for row in exact["items"]),
                "near_model_nonmatch": "P15.76",
                "hybrid_top": best["id"], "hybrid_channels": best["retrieval_channels"],
                "vector_status": hybrid["vector_status"],
            }
        finally:
            if pid_file.is_file():
                try:
                    os.kill(int(pid_file.read_text(encoding="ascii")), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def verify(archive, destination):
    destination.mkdir(parents=True)
    with ZipFile(archive) as z:
        z.extractall(destination)
    installation = destination / NAME
    assert not (installation / "assets").exists()
    assert not (installation / ".venv").exists()
    assert not (installation / "raw").exists()
    manifest = json.loads((installation / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_commit"] == COMMIT
    catalog = installation / "catalog"
    sql = json.loads((catalog / "snapshots/manifest.json").read_text(encoding="utf-8"))
    for name, expected in sql.items():
        assert sha(catalog / name) == expected["sha256"]
    assert sqlite_count(catalog / "library.sqlite3", "modules") == 3468
    geometry = sqlite_count(catalog / "geometry.sqlite3", "bindings")
    assert geometry > 0

    chroma = installation / "runtime/chroma"
    snapshot = json.loads((installation / "runtime/chroma-snapshot.json").read_text(encoding="utf-8"))
    for path, expected in snapshot["files"].items():
        assert sha(chroma / path) == expected, f"Packaged Chroma file differs: {path}"
    sqlite_file = chroma / "chroma.sqlite3"
    assert sqlite_count(sqlite_file, "embeddings") == 3468
    before = persisted_state(sqlite_file)
    runs = [exercise(installation, destination / f"launch-{run}.log") for run in (1, 2)]
    for path, expected in snapshot["files"].items():
        if path != sqlite_file.name:
            assert sha(chroma / path) == expected, f"Persisted HNSW index changed: {path}"
    assert sqlite_count(sqlite_file, "embeddings") == 3468
    assert persisted_state(sqlite_file) == before, "Persisted Chroma data changed"
    report = {
        "status": "passed", "commit": COMMIT, "archive_sha256": sha(archive),
        "geometry_bindings": geometry, "persisted_embeddings": 3468,
        "unchanged_hnsw_files": True, "unchanged_chroma_data": True, "chroma_data_sha256": before,
        "sqlite_housekeeping_changed": sha(sqlite_file) != snapshot["files"][sqlite_file.name],
        "launcher_runs": runs,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.archive.resolve(), args.destination.resolve())
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
