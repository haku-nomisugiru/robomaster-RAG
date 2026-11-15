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
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from component_scope import excluded_component
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
        request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "RoboMasterCADLibrary/0.5.3"})
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
        for row in read_json(self.root / "catalog/inventory.json"):
            if (row["content_type"] == "cad"
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
        self.parts = {}
        source_files = {(article["thread_id"], row["sha256"]): row
                        for article in catalog for row in article.get("files", [])
                        if row.get("ok") and row.get("sha256")}
        for key, asset in self.assets.items():
            url = asset.get("url")
            if not is_author_attachment(url):
                url = urls.get((asset["thread_id"], asset["sha256"]))
            parts = []
            for path, fingerprint in (asset.get("volume_paths") or {asset["path"]: asset["sha256"]}).items():
                saved = source_files.get((asset["thread_id"], fingerprint), {})
                part_url = url if path == asset["path"] else saved.get("url")
                size = asset["bytes"] if path == asset["path"] else saved.get("bytes")
                if not is_author_attachment(part_url) or not isinstance(size, int):
                    break
                parts.append({"path": path, "sha256": fingerprint, "bytes": size,
                              "url": encoded_url(part_url), "name": PurePosixPath(path).name,
                              "asset_id": key})
            else:
                self.urls[key] = encoded_url(url)
                self.parts[key] = parts

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
            "volumes": self.parts.get(entry["asset_id"], []) if asset and asset.get("volume_paths") else [],
        }

    def group(self, row):
        # Native assembly references resolve relative to the original hierarchy.
        others = self.cad_groups[row["asset_id"]] if row["extension"] == ".sldasm" else ()
        return [row, *(item for item in others if item is not row)]

    def from_zip(self, path, rows):
        with ZipFile(path, metadata_encoding="gbk") as archive:
            members = defaultdict(list)
            for member in archive.infolist():
                if not member.is_dir():
                    members[member.filename.replace("\\", "/")].append(member)
            for item in rows:
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

    def install_archive(self, archive, asset, rows):
        if not matches(archive, asset):
            raise ValueError(f"Original attachment SHA-256 differs: {archive}")
        if archive.suffix.lower() == ".zip":
            self.from_zip(archive, rows)
        elif archive.suffix.lower() == ".rar":
            self.from_rar(archive, asset, rows)
        else:
            for item in rows:
                install(lambda: archive.open("rb"), checked_path(self.root, item["path"]), item)

    def from_rar(self, archive, asset, rows):
        pending = []
        for item in rows:
            target = checked_path(self.root, item["path"])
            if target.exists():
                if not matches(target, item):
                    raise ValueError(f"Existing file has a different fingerprint: {target}")
            else:
                pending.append(item)
        if not pending:
            return
        candidates = [self.root / "_runtime/unrar/UnRAR.exe"]
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(key):
                candidates.append(Path(os.environ[key]) / "WinRAR/UnRAR.exe")
        unrar = next((str(path) for path in candidates if path.is_file()), shutil.which("unrar"))
        if not unrar:
            if asset.get("volume_paths"):
                raise ValueError("Multi-volume RAR requires UnRAR. Use the Windows release with its bundled reader.")
            for item in pending:
                rar_member(archive, member_path(item), checked_path(self.root, item["path"]), item)
            return
        runtime = self.root / "runtime"
        runtime.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="rar-", dir=runtime) as directory:
            staging = Path(directory)
            listing = staging / "members.txt"
            listing.write_text("\n".join(member_path(item).replace("/", "\\") for item in pending) + "\n", encoding="utf-8")
            extracted = staging / "cad"
            extracted.mkdir()
            process = subprocess.run([unrar, "x", "-idq", "-y", "-o-", "-p-", "-scf",
                                      str(archive), "@" + str(listing), str(extracted) + os.sep],
                                     stdin=subprocess.DEVNULL, capture_output=True, timeout=1800,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.returncode:
                raise ValueError(f"UnRAR failed ({process.returncode}): " + process.stderr.decode("utf-8", errors="replace")[-1000:])
            for item in pending:
                source = checked_path(extracted, member_path(item))
                if source.is_symlink() or not source.is_file():
                    raise ValueError(f"RAR member is missing or is a link: {member_path(item)}")
                install(lambda: source.open("rb"), checked_path(self.root, item["path"]), item)

    def ensure_archive(self, asset, supplied=None, cached=None):
        downloaded = False
        for part in self.parts[asset["asset_id"]]:
            target = checked_path(self.root, part["path"])
            if target.exists():
                if not matches(target, part):
                    raise ValueError(f"Existing attachment has a different fingerprint: {target}")
                continue
            source = (supplied if part["path"] == asset["path"] else supplied.parent / part["name"]) if supplied else None
            if source is None and cached and part["sha256"] == asset["sha256"]:
                source = cached
            if source is not None:
                install(lambda: source.open("rb"), target, part)
            else:
                download(part["url"], target, part)
                downloaded = True
        return checked_path(self.root, asset["path"]), downloaded

    def package_record(self, asset):
        if not asset.get("volume_paths"):
            return asset
        relative = f"runtime/cad-packages/{asset['asset_id']}.zip"
        receipt = checked_path(self.root, relative + ".json")
        if receipt.is_file():
            record = read_json(receipt)
            if (record.get("path") == relative and record.get("source_sha256") == asset["sha256"]
                    and record.get("inventory_sha256") == self.package_inventory_sha(asset)):
                return record
        return None

    def package_inventory_sha(self, asset):
        entries = sorted((row["path"], row["sha256"], row["bytes"])
                         for row in self.cad_groups[asset["asset_id"]])
        return sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()

    def prepare_package(self, asset):
        if not asset.get("volume_paths"):
            return
        old = self.package_record(asset)
        if old and matches(checked_path(self.root, old["path"]), old):
            return
        relative = f"runtime/cad-packages/{asset['asset_id']}.zip"
        target = checked_path(self.root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
                for item in self.cad_groups[asset["asset_id"]]:
                    source = checked_path(self.root, item["path"])
                    if not matches(source, item):
                        raise ValueError(f"Cannot package unverified CAD: {item['path']}")
                    archive.write(source, member_path(item))
            record = {"path": relative, "name": f"{asset['asset_id']}-cad.zip", "bytes": temporary.stat().st_size,
                      "sha256": digest_file(temporary), "source_sha256": asset["sha256"],
                      "inventory_sha256": self.package_inventory_sha(asset), "scope": "catalog_cad_bundle"}
            temporary.replace(target)
            checked_path(self.root, relative + ".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        finally:
            temporary.unlink(missing_ok=True)

    def acquire_all(self):
        groups = defaultdict(list)
        cards_by_asset = defaultdict(list)
        for card in self.cards.values():
            if card["entry"]["path"] in self.files:
                cards_by_asset[card["entry"]["asset_id"]].append(card["id"])
        for asset_id in sorted(cards_by_asset.keys() & self.urls.keys()):
            asset = self.assets[asset_id]
            groups[asset["sha256"]].append(asset)

        all_parts = {part["sha256"]: part for assets in groups.values() for asset in assets
                     for part in self.parts[asset["asset_id"]]}
        unavailable = [{"card_id": card["id"], "source_url": card["source_url"]}
                       for card in self.cards.values()
                       if card["entry"]["path"] not in self.files or card["entry"]["asset_id"] not in self.urls]
        report = {
            "status": "running", "attachments_total": len(all_parts), "packages_total": len(groups),
            "download_bytes": sum(part["bytes"] for part in all_parts.values()),
            "cards_catalog_total": len(self.cards), "unavailable_cards": unavailable,
            "cards_total": sum(len(cards_by_asset[asset["asset_id"]])
                               for assets in groups.values() for asset in assets),
            "attachments": [],
        }
        report_path = checked_path(self.root, "runtime/cad-acquisition-report.json")

        def save_report():
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=report_path.parent,
                                    suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            try:
                temporary.replace(report_path)
            finally:
                temporary.unlink(missing_ok=True)

        save_report()
        for index, (fingerprint, assets) in enumerate(groups.items(), 1):
            primary = assets[0]
            result = {"name": primary["name"], "sha256": fingerprint,
                      "asset_ids": [asset["asset_id"] for asset in assets],
                      "status": "failed", "cad_files_installed": 0}
            print(f"[{index}/{len(groups)}] {primary['name']}", file=sys.stderr, flush=True)
            try:
                cached = next((checked_path(self.root, asset["path"]) for asset in assets
                               if matches(checked_path(self.root, asset["path"]), asset)), None)
                result["downloaded"] = False
                for asset in assets:
                    archive, downloaded = self.ensure_archive(asset, cached=cached)
                    cached = archive
                    result["downloaded"] |= downloaded
                    rows = self.cad_groups[asset["asset_id"]]
                    self.install_archive(archive, asset, rows)
                    self.prepare_package(asset)
                    result["cad_files_installed"] += len(rows)
                result["status"] = "complete"
            except (KeyError, OSError, ValueError, BadZipFile, DownloadError, HTTPException, subprocess.SubprocessError) as exc:
                result["error"] = str(exc)
                print(f"  Failed: {exc}", file=sys.stderr, flush=True)
            report["attachments"].append(result)
            save_report()
        report["failed"] = sum(item["status"] == "failed" for item in report["attachments"])
        report["status"] = "partial" if report["failed"] or unavailable else "complete"
        report["cad_files_installed"] = sum(item["cad_files_installed"] for item in report["attachments"])
        save_report()
        return {**report, "report_path": str(report_path)}

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
            elif asset and matches(supplied, asset):
                if asset.get("volume_paths"):
                    archive, downloaded = self.ensure_archive(asset, supplied=supplied)
                    archive_cached = not downloaded
                else:
                    archive_cached = not install(lambda: supplied.open("rb"), archive, asset)
            else:
                install(lambda: supplied.open("rb"), target, row)
        elif archive and plan["author_url"] and (not target.exists() or row["extension"] == ".sldasm"):
            archive, downloaded = self.ensure_archive(asset)
            archive_cached = not downloaded
        elif archive and archive.is_file():
            archive_cached = True

        if archive and archive.is_file() and (not target.exists() or row["extension"] == ".sldasm"):
            self.install_archive(archive, asset, self.cad_groups[asset["asset_id"]] if asset.get("volume_paths") else self.group(row))
            self.prepare_package(asset)
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
    parser.add_argument("card_id", nargs="?", help="Exact card ID returned by HTTP search")
    parser.add_argument("--all", action="store_true", help="Download and install every available author attachment")
    parser.add_argument("--from", dest="supplied", type=Path, help="Local original attachment, CAD file, or extracted directory")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.all and (args.card_id or args.supplied):
        parser.error("--all cannot be combined with a card ID or --from")
    if not args.all and not args.card_id:
        parser.error("Supply a card ID or --all")
    try:
        cards = {card["id"]: card for line in (args.root / "catalog/modules.jsonl").read_text(encoding="utf-8").splitlines()
                 if (card := json.loads(line))}
        acquisition = Acquisition(args.root, cards)
        result = acquisition.acquire_all() if args.all else acquisition.acquire(args.card_id, args.supplied)
    except (KeyError, OSError, ValueError, BadZipFile, DownloadError, HTTPException, subprocess.SubprocessError) as exc:
        parser.exit(2, f"CAD acquisition failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.all and result["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
