"""Reconcile local CAD archives and files with the library inventory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from component_scope import EXCLUDED_THREADS, excluded_component
from ingest_forum import digest_file, safe_member_path, write_json


ROOT = Path(__file__).resolve().parents[1]
FORMATS = {
    ".sldprt", ".sldasm", ".slddrw", ".step", ".stp", ".easm", ".eprt",
    ".igs", ".iges", ".x_t", ".x_b", ".stl", ".obj", ".3mf", ".sat",
    ".dxf", ".dwg", ".prt", ".asm", ".f3d", ".f3z", ".fcstd", ".ipt", ".iam",
}
ARCHIVES = {".zip", ".rar", ".7z"}
VOLUME = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)
MEMBER_LIMIT = 2 * 1024**3
ASSET_LIMIT = 5 * 1024**3


def select_members(members, destination, thread_id):
    selected = {}
    omitted = 0
    total = 0
    for name, size, regular in members:
        name = name.replace("\\", "/")
        path = PurePosixPath(name)
        if path.suffix.casefold() not in FORMATS or path.name.startswith("~$"):
            continue
        if excluded_component(thread_id, name):
            omitted += 1
            continue
        if not regular or any(re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((" ", "."))
                              for part in path.parts):
            raise ValueError(f"Unsupported archive member: {name}")
        target = safe_member_path(destination, name)
        if size > MEMBER_LIMIT or total + size > ASSET_LIMIT:
            raise ValueError("Selected CAD exceeds extraction limits")
        if target in selected and selected[target][0] != name:
            raise ValueError("Archive members collide on disk")
        selected[target] = (name, size)
        total += size
    return selected, omitted


def run_unrar(unrar, args):
    result = subprocess.run([unrar, *args], stdin=subprocess.DEVNULL, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=300)
    output = result.stdout.decode("utf-8-sig", errors="replace")
    if result.returncode:
        detail = (output + result.stderr.decode("utf-8", errors="replace")).strip().splitlines()
        raise ValueError(f"UnRAR returned {result.returncode}: {' '.join(detail[-4:])}")
    return output


def rar_members(unrar, archive):
    output = run_unrar(unrar, ["lt", "-v", "-p-", "-scf", str(archive)])
    # A member split across volumes can appear more than once in the listing.
    members = {}
    for block in re.split(r"(?m)^\s*Name: ", output)[1:]:
        lines = block.splitlines()
        name = lines[0].strip()
        fields = dict(line.strip().split(": ", 1) for line in lines[1:] if ": " in line)
        if fields.get("Type") == "Directory":
            continue
        if fields.get("Size", "").isdigit():
            size = int(fields["Size"])
            regular = fields.get("Type") == "File" and "Target" not in fields
            members[name] = (name, size, regular)
    if "\ufffd" in output:
        raise ValueError("Archive member names were not decoded losslessly")
    return members.values()


def copy_verified(incoming, target, size):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with temporary.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024**2)
        if temporary.stat().st_size != size:
            raise ValueError(f"Archive member size mismatch: {target.name}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def extract_local(root, archive, destination, thread_id, unrar):
    if archive.suffix.casefold() == ".zip":
        with zipfile.ZipFile(archive, metadata_encoding="gbk") as z:
            members = [(item.filename, item.file_size, not stat.S_ISLNK(item.external_attr >> 16))
                       for item in z.infolist() if not item.is_dir()]
            selected, omitted = select_members(members, destination, thread_id)
            for target, (name, size) in selected.items():
                if not target.is_file() or target.stat().st_size != size:
                    with z.open(name) as incoming:
                        copy_verified(incoming, target, size)
    elif archive.suffix.casefold() == ".rar":
        if not unrar:
            raise ValueError("UnRAR is required for local RAR archives")
        selected, omitted = select_members(rar_members(unrar, archive), destination, thread_id)
        pending = [name for target, (name, size) in selected.items()
                   if not target.is_file() or target.stat().st_size != size]
        if pending:
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt",
                                             dir=root / "runtime", delete=False) as listing:
                listing.write("\n".join(name.replace("/", "\\") for name in pending) + "\n")
            listing_path = Path(listing.name)
            try:
                run_unrar(unrar, ["x", "-o+", "-y", "-p-", "-scf", str(archive),
                                  "@" + str(listing_path), str(destination) + os.sep])
            finally:
                listing_path.unlink()
    elif archive.suffix.casefold() in FORMATS:
        selected, omitted = select_members([(archive.name, archive.stat().st_size, True)],
                                           destination, thread_id)
        for target, (_, size) in selected.items():
            if not target.is_file() or target.stat().st_size != size:
                with archive.open("rb") as incoming:
                    copy_verified(incoming, target, size)
    else:
        raise ValueError(f"Unsupported local archive format: {archive.suffix}")
    for target, (_, size) in selected.items():
        if not target.is_file() or target.stat().st_size != size or target.is_symlink():
            raise ValueError(f"Extracted CAD failed verification: {target.name}")
    return selected, omitted


def volume_paths(archive):
    match = VOLUME.search(archive.name)
    if not match:
        return []
    prefix = archive.name[:match.start()]
    volumes = {}
    for part in archive.parent.glob("*.rar"):
        segment = VOLUME.search(part.name)
        if segment and part.name[:segment.start()] == prefix:
            volumes[int(segment.group(1))] = part
    if set(volumes) != set(range(1, max(volumes) + 1)):
        raise ValueError("RAR volume sequence is incomplete")
    return [volumes[index] for index in sorted(volumes)]


def make_source(root, thread_id, manifest):
    page = root / "raw" / "pages" / str(thread_id) / "open-cad-page.html"
    published = manifest.get("createAt")
    if isinstance(published, str):
        published = published[:10]
    elif isinstance(published, (float, int)):
        published = datetime.fromtimestamp(published / 1000 if published > 10**11 else published).date().isoformat()
    return {
        "source_id": f"bbs-{thread_id}", "thread_id": thread_id,
        "title": manifest["title"], "url": f"https://bbs.robomaster.com/article/{thread_id}",
        "snapshot_kind": "forum_article_snapshot",
        "body_path": page.relative_to(root).as_posix() if page.is_file() else None,
        "page_sha256": digest_file(page) if page.is_file() else None,
        "seed": {"team": "待核对", "published_date": published, "caveats": [],
                 "notice": "使用范围以原帖及附件声明为准。"},
    }


def import_local(root, unrar):
    catalog = root / "catalog"
    sources = {row["source_id"]: row for row in json.loads((catalog / "sources.json").read_text(encoding="utf-8"))}
    downloads = json.loads((catalog / "downloads.json").read_text(encoding="utf-8"))
    inventory = {row["path"]: row for row in json.loads((catalog / "inventory.json").read_text(encoding="utf-8"))}
    manifest = {row["thread_id"]: row for row in json.loads((catalog / "open-cad-2025.json").read_text(encoding="utf-8"))}
    file_records = {file["local"]: file for row in manifest.values() for file in row.get("files", []) if file.get("local")}
    seen = {row["asset_id"] for row in inventory.values() if row["content_type"] == "cad"}
    (root / "runtime").mkdir(exist_ok=True)
    statuses = []
    restricted_assets = 0
    for archive in sorted((root / "raw" / "downloads").glob("*/*")):
        if not archive.is_file() or not archive.parent.name.isdecimal():
            continue
        extension = archive.suffix.casefold()
        if extension not in FORMATS | ARCHIVES:
            continue
        thread_id = int(archive.parent.name)
        match = VOLUME.search(archive.name)
        if match and int(match.group(1)) != 1:
            continue
        if thread_id in EXCLUDED_THREADS or excluded_component(thread_id, archive.name):
            restricted_assets += 1
            continue
        relative = archive.relative_to(root).as_posix()
        source_id = f"bbs-{thread_id}"
        try:
            if source_id not in sources and thread_id not in manifest:
                raise ValueError("No local source record")
            volumes = volume_paths(archive)
            hashes = {}
            for part in volumes or [archive]:
                rel = part.relative_to(root).as_posix()
                hashes[rel] = digest_file(part)
                saved = file_records.get(rel)
                if saved and (not saved.get("ok") or saved.get("sha256") != hashes[rel]):
                    raise ValueError(f"Local attachment disagrees with the manifest: {part.name}")
            sha = hashes[relative]
            asset_id = source_id + "-" + sha[:16]
            if asset_id in seen:
                statuses.append({"path": relative, "status": "already_indexed"})
                continue
            destination = root / "assets" / str(thread_id) / sha[:16]
            selected, omitted = extract_local(root, archive, destination, thread_id, unrar)
            if not selected:
                statuses.append({"path": relative, "status": "no_indexable_cad", "omitted_members": omitted})
                continue
            records = []
            for target in selected:
                path = target.relative_to(root).as_posix()
                records.append({
                    "source_id": source_id, "thread_id": thread_id, "asset_id": asset_id,
                    "archive_path": relative, "path": path, "name": target.name,
                    "extension": target.suffix.casefold(), "bytes": target.stat().st_size,
                    "sha256": digest_file(target), "content_type": "cad",
                })
            if source_id not in sources:
                sources[source_id] = make_source(root, thread_id, manifest[thread_id])
            saved = file_records.get(relative, {})
            asset = {
                "source_id": source_id, "thread_id": thread_id, "asset_id": asset_id,
                "name": archive.name, "path": relative, "status": "downloaded", "sha256": sha,
                "bytes": archive.stat().st_size, "origin": "local_cad_expansion",
                "url": saved.get("url") or sources[source_id]["url"], "omitted_members": omitted,
            }
            if volumes:
                asset["volume_paths"] = hashes
            downloads = [row for row in downloads
                         if row.get("asset_id") != asset_id and row.get("path") != relative] + [asset]
            inventory.update((row["path"], row) for row in records)
            seen.add(asset_id)
            statuses.append({"path": relative, "status": "indexed", "cad_files": len(records),
                             "omitted_members": omitted})
            print(f"INDEXED {thread_id}: {archive.name} ({len(records)} CAD)", flush=True)
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            statuses.append({"path": relative, "status": "error", "error": str(error)})
            print(f"ERROR {thread_id}: {error}", flush=True)
    write_json(catalog / "sources.json", list(sources.values()))
    write_json(catalog / "downloads.json", downloads)
    write_json(catalog / "inventory.json", list(inventory.values()))
    write_json(root / "reports" / "local-coverage.json",
               {"attachments": statuses, "excluded_attachment_count": restricted_assets})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    installed = Path(r"C:\Program Files\WinRAR\UnRAR.exe")
    parser.add_argument("--unrar", default=shutil.which("unrar") or (str(installed) if installed.is_file() else None))
    args = parser.parse_args()
    import_local(args.root.resolve(), args.unrar)


if __name__ == "__main__":
    main()
