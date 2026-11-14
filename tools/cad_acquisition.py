"""Install the exact CAD referenced by a library card without rebuilding any index."""

import argparse
from collections import defaultdict
from contextlib import contextmanager
from hashlib import sha256
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from component_scope import excluded_component, package_available
from ingest_forum import digest_file, encoded_url
from range_fetch import DownloadError, resumable_download


ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT_HOST = "hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_author_attachment(url):
    location = urlsplit(url or "")
    return (location.scheme == "https" and location.netloc == ATTACHMENT_HOST
            and not location.query and not location.fragment)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(url, target, record):
    if not is_author_attachment(url):
        raise ValueError("The author attachment URL is outside the approved source host")
    if record["bytes"] <= 64 * 1024**2:
        request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "RoboMasterCADLibrary/0.5.1"})
        try:
            return install(lambda: build_opener(NoRedirect()).open(request, timeout=30), target, record)
        except (OSError, HTTPException):
            if target.exists():
                raise
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="fetch-", dir=target.parent) as directory:
        staged = Path(directory) / "attachment"
        result = resumable_download(url, staged, expected_size=record["bytes"], max_bytes=record["bytes"])
        if result["sha256"] != record["sha256"]:
            raise ValueError("Author attachment SHA-256 differs from the release catalog")
        return install(lambda: staged.open("rb"), target, record)


def checked_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(
        part == ".." or any(ch in part for ch in '\\:*?"<>|') for part in path.parts
    ):
        raise ValueError("Unsafe CAD path in catalog")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError("CAD path escapes the installation directory")
    return target


def matches(path, record):
    return (path.is_file() and path.stat().st_size == record["bytes"]
            and digest_file(path) == record["sha256"])


def install(open_stream, target, record):
    if target.exists():
        if not matches(target, record):
            raise ValueError(f"Existing file has a different fingerprint: {target}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            digest = sha256()
            remaining = record["bytes"]
            with open_stream() as incoming:
                while chunk := incoming.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError(f"File exceeds the catalog size: {target.name}")
                    digest.update(chunk)
                    output.write(chunk)
        if remaining or digest.hexdigest() != record["sha256"]:
            raise ValueError(f"File does not match the catalog SHA-256: {target.name}")
        try:
            if os.name == "nt":
                temporary.rename(target)
            else:
                target.hardlink_to(temporary)
        except FileExistsError:
            if not matches(target, record):
                raise ValueError(f"Existing file has a different fingerprint: {target}")
            return False
        return True
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def member_path(row):
    return PurePosixPath(*PurePosixPath(row["path"]).parts[3:]).as_posix()


def rar_member(archive, name, target, record):
    if os.name == "nt":
        tar = Path(os.environ["SystemRoot"]) / "System32/tar.exe"
        tar = str(tar) if tar.is_file() else None
    else:
        tar = shutil.which("bsdtar")
    if not tar:
        raise ValueError("A RAR reader is required. Extract the archive and pass the CAD directory with --from.")
    @contextmanager
    def stream_member():
        with subprocess.Popen([tar, "-xOf", str(archive), "--", name], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env={**os.environ, "LANG": "en_US.UTF-8"},
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            try:
                yield process.stdout
                if process.wait() != 0:
                    raise ValueError("RAR member could not be read. Extract the CAD and use --from.")
            finally:
                if process.poll() is None:
                    process.kill()

    install(stream_member, target, record)


class Acquisition:
    def __init__(self, root, cards):
        self.root = root.resolve()
        self.cards = cards
        self.files = {}
        self.cad_groups = defaultdict(list)
        blocked = {card["entry"]["path"] for card in cards.values()
                   if not card["cad_source"].get("file_available", True)}
        for row in read_json(self.root / "catalog/inventory.json"):
            if (row["content_type"] == "cad" and row["path"] not in blocked
                    and not excluded_component(row["thread_id"], member_path(row))):
                self.files[row["path"]] = row
                self.cad_groups[row["asset_id"]].append(row)
        self.assets = {row["asset_id"]: row for row in read_json(self.root / "catalog/downloads.json")
                       if row.get("status") == "downloaded"}
        catalog = read_json(self.root / "catalog/open-cad-2025.json")
        supplemental = read_json(self.root / "reports/supplemental-downloads.json")
        urls = {(article["thread_id"], row["sha256"]): row["url"]
                for article in catalog for row in article.get("files", [])
                if row.get("ok") and row.get("sha256") and is_author_attachment(row.get("url"))}
        urls.update({(row["thread_id"], row["sha256"]): row["url"] for row in supplemental
                     if row.get("status") == "downloaded" and is_author_attachment(row.get("url"))})
        self.urls = {}
        for key, asset in self.assets.items():
            if package_available(asset):
                url = asset.get("url")
                if not is_author_attachment(url):
                    url = urls.get((asset["thread_id"], asset["sha256"]))
                if url:
                    self.urls[key] = encoded_url(url)

    def plan(self, card):
        entry = card["entry"]
        asset = self.assets.get(entry["asset_id"])
        local = checked_path(self.root, entry["path"]).is_file()
        approved = entry["path"] in self.files
        url = self.urls.get(entry["asset_id"]) if approved else None
        cached_archive = bool(asset and checked_path(self.root, asset["path"]).is_file())
        state = ("unavailable" if not approved else "local" if local else "cached_attachment" if cached_archive
                 else "author_download" if url else "manual_import")
        return {
            "status": state, "card_id": card["id"], "author_url": url,
            "source_url": card["source_url"],
            "attachment": {key: asset[key] for key in ("name", "bytes", "sha256")} if asset and approved else None,
            "cad_sha256": entry["sha256"], "cad_path": entry["path"],
            "definition_id": card.get("definition_id"),
        }

    def group(self, row):
        # Native assembly references resolve relative to the original hierarchy.
        others = self.cad_groups[row["asset_id"]] if row["extension"] == ".sldasm" else ()
        return [row, *(item for item in others if item is not row)]

    def from_zip(self, path, row):
        with ZipFile(path, metadata_encoding="gbk") as archive:
            members = defaultdict(list)
            for member in archive.infolist():
                if not member.is_dir():
                    members[member.filename.replace("\\", "/")].append(member)
            for item in self.group(row):
                name = member_path(item)
                options = members[name]
                if len(options) != 1:
                    raise ValueError(f"ZIP member is absent or ambiguous: {name}")
                member = options[0]
                if stat.S_ISLNK(member.external_attr >> 16) or member.file_size != item["bytes"]:
                    raise ValueError(f"ZIP member differs from the catalog: {name}")
                install(lambda: archive.open(member), checked_path(self.root, item["path"]), item)

    def from_directory(self, directory, row):
        source = checked_path(directory, member_path(row))
        if not source.is_file():
            raise ValueError(f"CAD member not found under the extracted directory: {member_path(row)}")
        for item in self.group(row):
            source = checked_path(directory, member_path(item))
            if source.is_file():
                install(lambda: source.open("rb"), checked_path(self.root, item["path"]), item)

    def acquire(self, card_id, supplied=None):
        card = self.cards[card_id]
        plan = self.plan(card)
        if plan["status"] == "unavailable":
            raise ValueError("This card has evidence only; its CAD is unavailable for installation")
        row = self.files[plan["cad_path"]]
        target = checked_path(self.root, row["path"])
        asset = self.assets.get(row["asset_id"])
        archive = checked_path(self.root, asset["path"]) if asset else None
        archive_cached = False
        if supplied is not None:
            supplied = supplied.resolve(strict=True)
            if supplied.is_dir():
                self.from_directory(supplied, row)
            elif asset and matches(supplied, asset) and not asset.get("volume_paths"):
                archive_cached = not install(lambda: supplied.open("rb"), archive, asset)
            else:
                install(lambda: supplied.open("rb"), target, row)
        elif not target.exists() and archive and not archive.exists() and plan["author_url"]:
            archive_cached = not download(plan["author_url"], archive, asset)
        elif archive and archive.is_file():
            archive_cached = True

        if archive and archive.is_file() and (not target.exists() or row["extension"] == ".sldasm"):
            if asset.get("volume_paths"):
                if not target.exists():
                    raise ValueError("Extract all volumes locally and use --from with the extracted directory")
            else:
                if not matches(archive, asset):
                    raise ValueError(f"Original attachment SHA-256 differs: {archive}")
                if archive.suffix.lower() == ".zip":
                    self.from_zip(archive, row)
                elif archive.suffix.lower() == ".rar":
                    for item in self.group(row):
                        rar_member(archive, member_path(item), checked_path(self.root, item["path"]), item)
                else:
                    install(lambda: archive.open("rb"), target, row)
        if not target.exists():
            raise ValueError(f"Get the original CAD from {plan['source_url']} and pass it with --from")
        if not matches(target, row):
            raise ValueError(f"Installed CAD SHA-256 differs: {target}")
        return {**self.plan(card), "path": str(target), "sha256": row["sha256"],
                "verified": True, "attachment_cached": archive_cached}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card_id", help="Exact card ID returned by HTTP search")
    parser.add_argument("--from", dest="supplied", type=Path, help="Local original attachment, CAD file, or extracted directory")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        cards = {card["id"]: card for line in (args.root / "catalog/modules.jsonl").read_text(encoding="utf-8").splitlines()
                 if (card := json.loads(line))}
        result = Acquisition(args.root, cards).acquire(args.card_id, args.supplied)
    except (KeyError, OSError, ValueError, BadZipFile, DownloadError) as exc:
        parser.exit(2, f"CAD acquisition failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
