"""Strict, resumable downloads for the sample library's public asset hosts."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ALLOWED_HOSTS = frozenset({
    "bbs-web-static.robomaster.com",
    "hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com",
    "files.pythonhosted.org",
})
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RETRIES = 6
_SMALL_FILE_LIMIT = 8 * 1024**2
_TIMEOUT = 45
_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


class DownloadError(RuntimeError):
    """A download failed validation or could not be completed."""


class _Retryable(DownloadError):
    pass


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("Invalid URL port") from exc
    if (parsed.scheme.lower() != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise DownloadError("URL must use HTTPS and an approved public asset hostname")
    # Existing percent escapes stay intact; raw Unicode filenames are UTF-8 encoded.
    path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parsed.query, safe="=&%/:@!$'()*+,;?-._~")
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, query, ""))


class _AllowedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked = _validate_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, checked)


def _build_opener():
    # Tests inject their own opener; production has no localhost or HTTP exception.
    return urllib.request.build_opener(_AllowedRedirectHandler())


def _open(url: str, method="GET", headers=None):
    request_headers = {
        "User-Agent": "RoboMasterCADLibrary/0.1",
        "Accept-Encoding": "identity",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(_validate_url(url), method=method, headers=request_headers)
    response = _build_opener().open(req, timeout=_TIMEOUT)
    _validate_url(response.geturl())
    return response


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _retry(label, operation):
    for attempt in range(1, _RETRIES + 1):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if exc.code not in _TRANSIENT_HTTP:
                detail = " (authentication/access denied)" if exc.code in (401, 403) else ""
                raise DownloadError(f"{label}: HTTP {exc.code}{detail}") from exc
            last_error = f"HTTP {exc.code}"
        except (_Retryable, urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt == _RETRIES:
            raise DownloadError(f"{label}: failed after {_RETRIES} attempts: {last_error}")
        _log(f"{label}: retry {attempt}/{_RETRIES}: {last_error}")
        time.sleep(min(0.25 * 2 ** (attempt - 1), 4.0))


def _integer_header(headers, name):
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise DownloadError(f"Invalid {name} response header") from exc
    if value < 0:
        raise DownloadError(f"Invalid {name} response header")
    return value


def _check_type(headers):
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in ("text/html", "application/xhtml+xml", "application/json"):
        raise DownloadError(f"Server returned an error/login document ({content_type})")
    encoding = headers.get("Content-Encoding", "identity").lower().strip()
    if encoding not in ("", "identity"):
        raise DownloadError("Encoded response cannot be validated as byte ranges")


def _check_prefix(data):
    prefix = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:256].lower()
    if (prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<error"))
            or (prefix.startswith(b"<?xml") and b"<error" in prefix)):
        raise DownloadError("Server returned an HTML/XML error document, not an asset")


def _range_header(headers, start, end):
    match = _RANGE.fullmatch(headers.get("Content-Range", "").strip())
    if match is None:
        raise _Retryable("Missing/invalid Content-Range")
    actual_start, actual_end, total = map(int, match.groups())
    if actual_start != start or actual_end != end or total <= actual_end:
        raise _Retryable("Content-Range does not match the requested bytes")
    return total


def _head(url):
    with _open(url, method="HEAD") as response:
        if response.status not in (200, 204):
            raise DownloadError(f"Unexpected HEAD status {response.status}")
        _check_type(response.headers)
        return {
            "total": _integer_header(response.headers, "Content-Length"),
            "ranged": response.headers.get("Accept-Ranges", "").lower() == "bytes",
            "etag": response.headers.get("ETag", ""),
            "modified": response.headers.get("Last-Modified", ""),
        }


def _probe(url):
    try:
        response = _open(url, headers={"Range": "bytes=0-0"})
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 416, 501):
            return _head(url)
        raise
    with response:
        _check_type(response.headers)
        if response.status == 206:
            total = _range_header(response.headers, 0, 0)
            length = _integer_header(response.headers, "Content-Length")
            if length not in (None, 1):
                raise _Retryable("Probe Content-Length is not one byte")
            if len(response.read(2)) != 1:
                raise _Retryable("Probe body is truncated or longer than requested")
            return {"total": total, "ranged": True,
                    "etag": response.headers.get("ETag", ""),
                    "modified": response.headers.get("Last-Modified", "")}
        if response.status != 200:
            raise DownloadError(f"Unexpected probe status {response.status}")
        result = {"total": _integer_header(response.headers, "Content-Length"),
                  "ranged": False, "etag": response.headers.get("ETag", ""),
                  "modified": response.headers.get("Last-Modified", "")}
    if result["total"] is None:
        head = _head(url)
        result["total"] = head["total"]
    return result


def _read_to_file(response, temporary: Path, expected=None, limit=None, check_prefix=False):
    count = 0
    with temporary.open("xb") as out:
        while True:
            remaining = (expected if expected is not None else limit) - count
            block = response.read(min(64 * 1024, remaining + 1))
            if not block:
                break
            if count == 0 and check_prefix:
                _check_prefix(block)
            count += len(block)
            if expected is not None and count > expected:
                raise _Retryable("Body is longer than Content-Range")
            if limit is not None and count > limit:
                raise DownloadError("Body exceeds the bounded download size")
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if expected is not None and count != expected:
        raise _Retryable(f"Truncated body: expected {expected}, received {count}")
    return count


def _append_validated_range(response, partial: Path, prefix_size: int,
                            expected: int, check_prefix=False):
    """Append only bytes from an already validated 206 response.

    read1() preserves short reads on chunked responses; IncompleteRead.partial
    is also retained when the HTTP implementation supplies it.
    """
    received = 0
    reader = getattr(response, "read1", response.read)
    with partial.open("ab") as out:
        if out.tell() != prefix_size:
            raise DownloadError("Partial file changed during download")
        try:
            while True:
                interrupted = None
                try:
                    block = reader(min(64 * 1024, expected - received + 1))
                except http.client.IncompleteRead as exc:
                    block = exc.partial
                    interrupted = exc
                if block:
                    if received == 0 and check_prefix:
                        _check_prefix(block)
                    if received + len(block) > expected:
                        # An overlong response is not a valid prefix.
                        out.truncate(prefix_size)
                        raise _Retryable("Body is longer than Content-Range")
                    out.write(block)
                    received += len(block)
                if interrupted is not None:
                    raise _Retryable(f"HTTP body interrupted after {received} bytes")
                if not block:
                    break
        finally:
            out.flush()
            os.fsync(out.fileno())
    if received != expected:
        raise _Retryable(f"Truncated suffix: expected {expected}, received {received}")
    return received


def _hash_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _small_download(url, destination, total, max_bytes, validator=None):
    limit = min(max_bytes, _SMALL_FILE_LIMIT)
    if total is not None and total > limit:
        raise DownloadError("Server ignored Range; full-body fallback is limited to 8 MiB")
    temporary = destination.with_name(destination.name + ".tmp-" + uuid.uuid4().hex)

    def attempt():
        try:
            with _open(url) as response:
                if response.status != 200:
                    raise DownloadError(f"Unexpected full-body status {response.status}")
                _check_type(response.headers)
                declared = _integer_header(response.headers, "Content-Length")
                if declared is not None and declared > limit:
                    raise DownloadError("Full-body response exceeds the bounded download size")
                if total is not None and declared not in (None, total):
                    raise _Retryable("Full-body Content-Length changed")
                size = _read_to_file(response, temporary, expected=total,
                                     limit=limit, check_prefix=True)
                if declared is not None and size != declared:
                    raise _Retryable("Full-body response is truncated")
                if validator is not None:
                    actual = response.headers.get(validator[0])
                    if actual and actual != validator[1]:
                        raise DownloadError("Remote asset changed during download")
            if size == 0:
                raise DownloadError("Asset is empty")
            os.replace(temporary, destination)
            return size
        finally:
            temporary.unlink(missing_ok=True)

    size = _retry("small asset", attempt)
    return {"path": str(destination.resolve()), "size": size,
            "sha256": _hash_file(destination), "parts": 1, "reused_parts": 0,
            "range_supported": False}


def resumable_download(url, destination: Path, expected_size: int | None = None,
                       max_bytes=600 * 1024**2, workers=4,
                       chunk_bytes=4 * 1024**2) -> dict:
    """Download an approved public asset, reusing complete byte-range parts.

    A destination is replaced only after every part passes range/length checks.
    The CLI additionally restricts destinations to this project directory.
    """
    url = _validate_url(url)
    destination = Path(destination).resolve()
    for name, value in (("max_bytes", max_bytes), ("workers", workers),
                        ("chunk_bytes", chunk_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if workers > 16:
        raise ValueError("workers cannot exceed 16")
    if expected_size is not None:
        if (isinstance(expected_size, bool) or not isinstance(expected_size, int)
                or expected_size <= 0):
            raise ValueError("expected_size must be a positive integer or None")
        if expected_size > max_bytes:
            raise DownloadError("Expected file size exceeds max_bytes")
    probe = _retry("asset probe", lambda: _probe(url))
    total = probe["total"]
    if total is not None:
        if total <= 0:
            raise DownloadError("Asset is empty")
        if total > max_bytes:
            raise DownloadError("Remote file size exceeds max_bytes")
        if expected_size is not None and total != expected_size:
            raise DownloadError("Remote size does not match expected_size")
    else:
        total = expected_size
    validator = None
    if probe["etag"] and not probe["etag"].startswith("W/"):
        validator = ("ETag", probe["etag"])
    elif probe["modified"]:
        validator = ("Last-Modified", probe["modified"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not probe["ranged"]:
        return _small_download(url, destination, total, max_bytes, validator)
    if total is None:
        raise DownloadError("Range downloads require a known total file size")
    identity = hashlib.sha256(
        (url + "\n" + str(total) + "\n" + probe["etag"] + "\n" + probe["modified"]
         + "\n" + str(chunk_bytes)).encode("utf-8")
    ).hexdigest()[:32]
    parts_dir = destination.parent / ".parts" / identity
    parts_dir.mkdir(parents=True, exist_ok=True)
    spans = [(index, start, min(total - 1, start + chunk_bytes - 1))
             for index, start in enumerate(range(0, total, chunk_bytes))]

    def fetch(span):
        index, start, end = span
        size = end - start + 1
        part = parts_dir / f"{index:08d}.part"
        if part.is_file() and part.stat().st_size == size:
            return part, True
        partial = parts_dir / f"{index:08d}.partial"
        if partial.is_file() and partial.stat().st_size > size:
            partial.unlink()
        consecutive_failures = 0
        last_error = ""
        for request_number in range(1, 33):
            prefix_size = partial.stat().st_size if partial.is_file() else 0
            if prefix_size == size:
                os.replace(partial, part)
                return part, False
            request_start = start + prefix_size
            remaining = size - prefix_size
            try:
                headers = {"Range": f"bytes={request_start}-{end}"}
                if validator is not None:
                    headers["If-Range"] = validator[1]
                with _open(url, headers=headers) as response:
                    if response.status != 206:
                        raise DownloadError("Server stopped honoring Range or asset changed")
                    _check_type(response.headers)
                    if _range_header(response.headers, request_start, end) != total:
                        raise DownloadError("Remote total size changed during download")
                    if validator is not None:
                        actual = response.headers.get(validator[0])
                        if actual and actual != validator[1]:
                            raise DownloadError("Remote asset changed during download")
                    length = _integer_header(response.headers, "Content-Length")
                    if length not in (None, remaining):
                        raise _Retryable("Chunk Content-Length does not match range")
                    _append_validated_range(
                        response, partial, prefix_size, remaining,
                        check_prefix=(request_start == 0),
                    )
                if partial.stat().st_size != size:
                    raise _Retryable("Partial file length does not match the complete chunk")
                os.replace(partial, part)
                return part, False
            except urllib.error.HTTPError as exc:
                if exc.code not in _TRANSIENT_HTTP:
                    detail = " (authentication/access denied)" if exc.code in (401, 403) else ""
                    raise DownloadError(f"part {index}: HTTP {exc.code}{detail}") from exc
                last_error = f"HTTP {exc.code}"
                exc.close()
            except (_Retryable, urllib.error.URLError, http.client.HTTPException,
                    TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            retained = partial.stat().st_size if partial.is_file() else 0
            if retained > prefix_size:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            if consecutive_failures >= _RETRIES or request_number == 32:
                raise DownloadError(
                    f"part {index}: stopped after {request_number} requests, "
                    f"{consecutive_failures} consecutive failures; retained "
                    f"{retained}/{size} bytes: {last_error}"
                )
            _log(
                f"part {index}: retained {retained}/{size} bytes; "
                f"retry suffix (request {request_number}/32): {last_error}"
            )
            time.sleep(0.05 if retained > prefix_size else
                       min(0.25 * 2 ** max(0, consecutive_failures - 1), 4.0))
        raise DownloadError(f"part {index}: exhausted bounded requests")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        downloaded = list(executor.map(fetch, spans))
    combined = destination.with_name(destination.name + ".tmp-" + uuid.uuid4().hex)
    try:
        digest = hashlib.sha256()
        size = 0
        with combined.open("xb") as out:
            for part, _ in downloaded:
                with part.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(block)
                        digest.update(block)
                        out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if size != total:
            raise DownloadError("Combined part size does not match remote size")
        with combined.open("rb") as stream:
            _check_prefix(stream.read(4096))
        os.replace(combined, destination)
    finally:
        combined.unlink(missing_ok=True)
    return {"path": str(destination), "size": total, "sha256": digest.hexdigest(),
            "parts": len(spans),
            "reused_parts": sum(int(reused) for _, reused in downloaded),
            "range_supported": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-mib", type=int, default=600)
    args = parser.parse_args(argv)
    destination = args.destination.resolve()
    if not destination.is_relative_to(PROJECT_ROOT) or destination == PROJECT_ROOT:
        parser.error("--destination must be a file inside the project directory")
    try:
        result = resumable_download(args.url, destination,
                                    max_bytes=args.max_mib * 1024**2)
    except (DownloadError, ValueError) as exc:
        _log(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
