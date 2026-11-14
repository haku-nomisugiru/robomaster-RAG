"""Exercise the Windows ZIP from a clean extraction using its public launcher."""

import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile

from build_windows import COMMIT, CRT_FILES, NAME


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


def verify_acquisition(installation, env, system, base, hit):
    reuse = get(base, hit["reuse_href"])
    plan = reuse["acquisition"]
    assert plan["status"] == "author_download" and plan["definition_id"] == "0:1:1:7"
    assert plan["cad_sha256"] == hit["cad_source"]["sha256"] and plan["card_id"] == hit["id"]
    href = reuse["artifacts"]["file"]["href"]
    try:
        get(base, href)
    except HTTPError as exc:
        with exc:
            assert exc.code == 404 and json.load(exc)["error"] == "cad_not_installed"
    else:
        raise AssertionError("The clean installation unexpectedly contained CAD")
    command = [str(system / "cmd.exe"), "/d", "/c", "get-cad.cmd", hit["id"]]
    def acquire():
        return json.loads(subprocess.check_output(command, cwd=installation, env=env, timeout=420))
    obtained = acquire()
    assert obtained["verified"] and obtained["card_id"] == hit["id"] and obtained["definition_id"] == plan["definition_id"]
    assert sha(Path(obtained["path"])) == plan["cad_sha256"]
    assert not obtained["attachment_cached"]
    assert acquire()["attachment_cached"]
    with urlopen(base + href, timeout=35) as response:
        assert response.headers["X-Content-SHA256"] == plan["cad_sha256"]
        assert file_digest(response, "sha256").hexdigest() == plan["cad_sha256"]
    assert get(base, hit["reuse_href"])["acquisition"]["status"] == "local"
    return {"card_id": hit["id"], "definition_id": plan["definition_id"],
            "attachment_sha256": plan["attachment"]["sha256"], "cad_sha256": plan["cad_sha256"],
            "cache_reused": True, "http_verified": True}


def exercise(installation, log, *, use_default):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}" if not use_default else None
    system = Path(os.environ["SystemRoot"]) / "System32"
    pid_file = log.with_suffix(".pid")
    env = dict(os.environ)
    env.update(PATH=str(system), PYTHONHOME=str(installation / "missing-python-home"),
               PYTHONPATH=str(installation / "missing-python-path"), RAG_NO_PAUSE="1",
               RAG_ACCEPTANCE_PID_FILE=str(pid_file))

    with log.open("wb") as stream:
        arguments = [] if use_default else ["--port", str(port)]
        process = subprocess.Popen([str(system / "cmd.exe"), "/d", "/c", "start-rag.cmd", *arguments],
                                   cwd=installation, env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            for _ in range(160):
                if process.poll() is not None:
                    raise RuntimeError(f"Launcher exited {process.returncode}. See {log}")
                if base is None:
                    log_text = log.read_text(encoding="ascii", errors="replace")
                    match = re.search(r"Listening on (http://127\.0\.0\.1:(\d+))", log_text)
                    if not match:
                        time.sleep(0.25)
                        continue
                    assert match[2] != "8765" and "Port 8765 is busy" in log_text
                    base = match[1]
                try:
                    health = get(base, "/health")
                    break
                except (OSError, URLError):
                    time.sleep(0.25)
            else:
                raise RuntimeError(f"Launcher did not listen on port {port}. See {log}")
            assert health["models"] == 3468
            assert get(base, "/openapi.json")["info"]["version"] == "0.5.1"
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
            cad = verify_acquisition(installation, env, system, base, best) if use_default else None
            return {
                "health": health["models"], "model_top": top["id"],
                "fullwidth_exact_ids": sorted(row["id"] for row in exact["items"]),
                "near_model_nonmatch": "P15.76",
                "hybrid_top": best["id"], "hybrid_channels": best["retrieval_channels"],
                "vector_status": hybrid["vector_status"],
                "default_port_conflict_handled": use_default,
                "cad_acquisition": cad,
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


def check_crt(installation, manifest):
    runtime = installation / "_runtime/python"
    for name in CRT_FILES:
        assert sha(runtime / name) == manifest[name]
    probe = """
import ctypes, json
from pathlib import Path
import chromadb, onnxruntime
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32")
kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel.GetModuleHandleW.restype = wintypes.HMODULE
kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel.GetModuleFileNameW.restype = wintypes.DWORD
modules = {}
for name in %r:
    handle = kernel.GetModuleHandleW(name)
    buffer = ctypes.create_unicode_buffer(32768)
    assert handle and kernel.GetModuleFileNameW(handle, buffer, len(buffer)), name
    modules[name] = str(Path(buffer.value).resolve().parent)
print(json.dumps(modules))
""" % (CRT_FILES,)
    loaded = json.loads(subprocess.check_output([runtime / "python.exe", "-B", "-c", probe]))
    assert set(loaded) == set(CRT_FILES) and all(Path(path) == runtime for path in loaded.values()), loaded


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
    check_crt(installation, manifest["app_local_msvc_sha256"])
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
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            occupied.bind(("127.0.0.1", 8765))
        except OSError as exc:
            if exc.winerror not in {10013, 10048}:
                raise
        else:
            occupied.listen()
        runs = [exercise(installation, destination / "launch-1.log", use_default=True)]
    runs.append(exercise(installation, destination / "launch-2.log", use_default=False))
    for path, expected in snapshot["files"].items():
        if path != sqlite_file.name:
            assert sha(chroma / path) == expected, f"Persisted HNSW index changed: {path}"
    assert sqlite_count(sqlite_file, "embeddings") == 3468
    assert persisted_state(sqlite_file) == before, "Persisted Chroma data changed"
    report = {
        "status": "passed", "commit": COMMIT, "archive_sha256": sha(archive),
        "geometry_bindings": geometry, "persisted_embeddings": 3468,
        "app_local_msvc_loaded": True,
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
