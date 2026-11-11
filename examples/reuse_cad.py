"""Reference workflow for an AI caller of the local CAD catalog."""

import argparse
from contextlib import contextmanager
from hashlib import file_digest, sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode, urlsplit


@contextmanager
def request(path, port):
    url = urlsplit(path)
    if url.scheme or url.netloc or url.fragment or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Expected a local catalog endpoint")
    connection = HTTPConnection("127.0.0.1", port, timeout=45)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Catalog HTTP {response.status}: {response.read(4096).decode('utf-8', 'replace')}")
        yield response
    finally:
        connection.close()


def get_json(path, port=8765):
    with request(path, port) as response:
        return json.load(response)


def get_reuse(card_id, port=8765):
    return get_json(f"/v1/models/{quote(card_id, safe='')}/reuse", port)


def find_candidates(*, query=None, reference_id=None, mode="function", size_mode="size",
                    category=None, limit=5, port=8765):
    if (query is None) == (reference_id is None):
        raise ValueError("Choose a text query or an indexed reference card")
    if reference_id is not None:
        path = "/v1/similar"
        params = {"reference_id": reference_id, "mode": size_mode, "limit": limit}
    else:
        path = "/v1/search"
        params = {"q": query, "mode": mode, "limit": limit}
    if category:
        params["category"] = category
    return get_json(f"{path}?{urlencode(params)}", port)


def reuse_candidate(hits, card_id=None, port=8765):
    if not hits:
        return None
    if card_id is None:
        card_id = hits[0]["id"]
    if not any(card_id == hit["id"] or card_id in hit.get("copy_card_ids", []) for hit in hits):
        raise ValueError("The selected card is absent from the search results")
    return get_reuse(card_id, port)


def local_sha256(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def download_artifact(artifact, directory, port=8765):
    filename = artifact["filename"]
    expected = artifact["sha256"]
    if (not filename or filename in {".", ".."} or PureWindowsPath(filename).name != filename
            or Path(filename).name != filename or getattr(os.path, "isreserved", lambda _: False)(filename)):
        raise ValueError("Invalid artifact filename")
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Missing or invalid artifact SHA-256")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if target.exists():
        if not target.is_file() or local_sha256(target) != expected:
            raise FileExistsError(f"Existing file differs from the selected artifact: {target}")
        return {"path": str(target), "sha256": expected, "bytes": target.stat().st_size, "cached": True}

    temporary = None
    try:
        with request(artifact["href"], port) as response:
            if response.getheader("X-Content-SHA256") != expected:
                raise ValueError("Download SHA-256 header differs from the reuse evidence")
            digest = sha256()
            size = 0
            with NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    stream.write(chunk)
                    size += len(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Downloaded bytes differ from the selected CAD artifact")
        # On Windows rename already refuses to replace an existing destination.
        if os.name == "nt":
            temporary.rename(target)
        else:
            os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(target), "sha256": expected, "bytes": size, "cached": False}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    entry = parser.add_mutually_exclusive_group(required=True)
    entry.add_argument("--query")
    entry.add_argument("--reference")
    parser.add_argument("--mode", choices=["function", "model"], default="function")
    parser.add_argument("--size-mode", choices=["size", "shape"], default="size")
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--select", help="Select a card from the returned candidates. Defaults to the first.")
    parser.add_argument("--output", type=Path, help="Download the selected artifact into this directory.")
    parser.add_argument("--artifact", choices=["file", "package"], help="Override the preferred artifact.")
    args = parser.parse_args()
    found = find_candidates(query=args.query, reference_id=args.reference, mode=args.mode,
                            size_mode=args.size_mode, category=args.category, limit=args.limit, port=args.port)
    hits = found["items"]
    plan = reuse_candidate(hits, args.select, args.port)
    result = {"candidates": [{"id": hit["id"], "name": hit["name"], "reuse_href": hit["reuse_href"]}
                             for hit in hits], "reuse": plan}
    if plan and args.output:
        role = args.artifact or plan["preferred_artifact"]
        if role is None or role not in plan["artifacts"]:
            parser.error("The selected card has no requested download artifact")
        result["download"] = download_artifact(plan["artifacts"][role], args.output, args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
