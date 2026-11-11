"""Build the Windows x64 package from the source commit and locked vector data."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from time import sleep
from urllib.request import urlopen
from zipfile import ZipFile, ZIP_DEFLATED

COMMIT = "8a71a260bd71a4bfdf0a1eac5cfa25f8d6bd4a9d"
NAME = "RoboMaster-CAD-RAG-v0.5.0-win-x64"
PYTHON_URL = "https://www.python.org/ftp/python/3.14.6/python-3.14.6-embed-amd64.zip"
PYTHON_HASH = "df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, path, expected=None):
    if path.is_file() and (expected is None or digest(path) == expected):
        return
    pending = path.with_suffix(path.suffix + ".part")
    for attempt in range(3):
        try:
            with urlopen(url, timeout=90) as response, pending.open("wb") as out:
                shutil.copyfileobj(response, out)
            if expected and digest(pending) != expected:
                raise ValueError("SHA-256 mismatch for " + path.name)
            pending.replace(path)
            return
        except OSError:
            if attempt == 2:
                raise
            sleep(2 * (attempt + 1))
        finally:
            pending.unlink(missing_ok=True)


def command(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def stage(args):
    folder = args.out / NAME
    folder.mkdir(parents=True)
    with (args.out / "source.zip").open("wb") as target:
        subprocess.run(["git", "-C", str(args.checkout), "archive", "--format=zip", COMMIT],
                       stdout=target, check=True)
    with ZipFile(args.out / "source.zip") as z:
        z.extractall(folder)

    command(sys.executable, "-B", folder / "tools/restore_catalog.py", "--root", folder)
    runtime = folder / "_runtime/python"
    runtime.mkdir(parents=True)
    python_zip = args.out / "python-3.14.6-embed-amd64.zip"
    if args.python_archive:
        shutil.copyfile(args.python_archive, python_zip)
    fetch(PYTHON_URL, python_zip, PYTHON_HASH)
    with ZipFile(python_zip) as z:
        z.extractall(runtime)
    (runtime / "python314._pth").write_text(
        "python314.zip\n.\nLib/site-packages\n../../tools\nimport site\n", encoding="ascii")
    packages = runtime / "Lib/site-packages"
    shutil.copytree(args.installed, packages, ignore=shutil.ignore_patterns("__pycache__", "bin", "Scripts"))

    manifest = json.loads((args.payload / "chroma-data.json").read_text(encoding="utf-8"))
    chroma_zip = args.payload / "chroma-data.zip"
    if not chroma_zip.is_file():
        chroma_zip = args.out / chroma_zip.name
        parts = sorted(args.payload.glob("chroma-data.part[0-9][0-9]"))
        if not parts:
            raise FileNotFoundError("Chroma snapshot is missing")
        with chroma_zip.open("wb") as out:
            for part in parts:
                with part.open("rb") as stream:
                    shutil.copyfileobj(stream, out)
    if digest(chroma_zip) != manifest["archive_sha256"]:
        raise ValueError("Chroma archive does not match its receipt")
    chroma = folder / "runtime/chroma"
    chroma.mkdir(parents=True)
    with ZipFile(chroma_zip) as z:
        if sorted(z.namelist()) != sorted(manifest["files"]):
            raise ValueError("Unexpected files in the Chroma archive")
        z.extractall(chroma)
    for name, expected in manifest["files"].items():
        if digest(chroma / name) != expected:
            raise ValueError("Chroma file hash differs: " + name)

    model_root = folder / "runtime/embedding-models"
    model_root.mkdir()
    if args.model_cache:
        shutil.copytree(args.model_cache, model_root, dirs_exist_ok=True)
    command(runtime / "python.exe", "-B", folder / "tools/vector_search.py", "--root", folder, "prepare")

    for filename in ("start-rag.cmd", "START-HERE.md"):
        shutil.copyfile(args.payload / filename, folder / filename)
    shutil.copyfile(args.payload / "portable_start.py", folder / "tools/portable_start.py")
    shutil.copyfile(args.payload / "chroma-data.json", folder / "runtime/chroma-snapshot.json")
    shutil.copyfile(args.payload / "constraints-win-x64.txt", folder / "_runtime/constraints-win-x64.txt")
    model_license = folder / "_runtime/FlagEmbedding-LICENSE.txt"
    fetch("https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/LICENSE", model_license)
    (folder / "_runtime/NOTICE.txt").write_text(
        "CPython 3.14.6 embedded for Windows x64. See python/LICENSE.txt.\n"
        "Python dependencies and licenses are in python/Lib/site-packages.\n"
        "BGE-small-zh-v1.5 by BAAI, ONNX build by Qdrant, MIT.\n"
        "https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59\n"
        "See FlagEmbedding-LICENSE.txt.\n",
        encoding="utf-8")
    (folder / "release-manifest.json").write_text(json.dumps({
        "source_commit": COMMIT, "version": "0.5.0", "platform": "win-x64",
        "python": "3.14.6", "cards": 3468, "persisted_embeddings": manifest["count"],
        "chroma_snapshot_sha256": manifest["archive_sha256"],
    }, indent=2) + "\n", encoding="utf-8")

    archive = args.out / (NAME + ".zip")
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=5) as z:
        for file in sorted(folder.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                z.write(file, file.relative_to(args.out).as_posix())
    (args.out / (archive.name + ".sha256")).write_text(
        f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    print(archive, archive.stat().st_size, digest(archive), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python-archive", type=Path)
    parser.add_argument("--model-cache", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    stage(args)


if __name__ == "__main__":
    main()
