"""Loopback-only, read-only CAD catalog HTTP service. No third-party dependencies."""
import argparse
import hashlib
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
import library

class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.modules = {m["id"]: m for m in map(json.loads, (self.root / "catalog/modules.jsonl").read_text(encoding="utf-8").splitlines())}
        self.assets = {a["asset_id"]: a for a in json.loads((self.root / "catalog/downloads.json").read_text(encoding="utf-8")) if a.get("status") == "downloaded"}
        self.sources = {s["source_id"]: s for s in json.loads((self.root / "catalog/sources.json").read_text(encoding="utf-8"))}
        self.db = self.root / "catalog/library.sqlite3"
        if not self.db.is_file(): raise ValueError("Build the catalog first")

    def detail(self, mid):
        if mid not in self.modules: raise KeyError(mid)
        result = dict(self.modules[mid])
        prefix = "/v1/models/" + quote(mid, safe="")
        asset = self.assets.get(result["entry"]["asset_id"])
        result["downloads"] = {"model": prefix + "/file", "package": prefix + "/package" if asset else None, "package_sha256": asset.get("sha256") if asset else None, "package_name": asset.get("name") if asset else None}
        result["source"] = {k: self.sources[result["source_id"]].get(k) for k in ["source_id", "url", "title", "snapshot_kind", "body_path", "page_sha256"]}
        result["usage"] = {"selection_scope": result["selection_scope"], "definition_id": result.get("definition_id"), "package_contains_dependencies": "not_verified", "ready_for_assembly": False}
        return result

    def search(self, q, section, limit):
        # Reuse ranking but connect read-only; the HTTP server never rebuilds the index.
        hits = library.search(self.root, q, limit, section)
        return [dict(self.detail(hit["id"]), score=hit["score"]) for hit in hits]

    def file_record(self, mid, kind):
        m = self.detail(mid)
        record = m["entry"] if kind == "file" else self.assets.get(m["entry"]["asset_id"])
        if not record: raise KeyError(mid)
        path = (self.root / record["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file(): raise FileNotFoundError()
        return path, record["sha256"]

class Handler(BaseHTTPRequestHandler):
    server_version = "RoboMasterCAD/0.2"

    def json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        try:
            # Reject cross-origin browser access and DNS-rebinding hosts.
            host = self.headers.get("Host", "").split(":")[0].lower()
            if host not in {"127.0.0.1", "localhost"} or self.headers.get("Origin"):
                return self.json_response(403, {"error": "local_native_clients_only"})
            url = urlsplit(self.path)
            args = parse_qs(url.query, keep_blank_values=True)
            if any(len(v) != 1 for v in args.values()): raise ValueError("Duplicate query parameter")
            catalog = self.server.catalog
            if url.path == "/health":
                return self.json_response(200, {"status": "ok", "models": len(catalog.modules), "retrieval": "lexical", "api_version": "1"})
            if url.path == "/openapi.json":
                return self.json_response(200, openapi(self.server.server_port))
            if url.path == "/v1/sections":
                return self.json_response(200, {"items": [dict(id=k, label=v["label"], count=sum(m.get("section") == k for m in catalog.modules.values())) for k,v in library.SECTIONS.items()]})
            if url.path == "/v1/search":
                if set(args) - {"q", "section", "limit"}: raise ValueError("Unknown query parameter")
                q = args.get("q", [""])[0].strip()
                section = args.get("section", [None])[0]
                limit = int(args.get("limit", ["5"])[0])
                if not q or len(q) > 256: raise ValueError("q must contain 1..256 characters")
                if not 1 <= limit <= 50: raise ValueError("limit must be 1..50")
                if section is not None and section not in library.SECTIONS: raise ValueError("Invalid section")
                items = catalog.search(q, section, limit)
                return self.json_response(200, {"query": q, "section": section, "count": len(items), "retrieval": "lexical", "items": items})
            match = re.fullmatch(r"/v1/models/([a-zA-Z0-9_-]+)(?:/(file|package))?", url.path)
            if match:
                mid, kind = match.groups()
                if not kind: return self.json_response(200, catalog.detail(mid))
                path, expected = catalog.file_record(mid, kind)
                with path.open("rb") as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""): digest.update(chunk)
                    if digest.hexdigest() != expected:
                        return self.json_response(409, {"error": "file_hash_changed", "message": "Revalidate the catalog before downloading"})
                    size = stream.tell(); stream.seek(0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
                    self.send_header("ETag", '"' + expected + '"')
                    self.send_header("X-Content-SHA256", expected)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if self.command != "HEAD":
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""): self.wfile.write(chunk)
                return
            self.json_response(404, {"error": "not_found"})
        except (ValueError, TypeError) as exc:
            self.json_response(400, {"error": "invalid_request", "message": str(exc)})
        except (KeyError, FileNotFoundError):
            self.json_response(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self.log_error("Internal request error")
            self.json_response(500, {"error": "internal_error"})

    def do_POST(self): self.json_response(405, {"error": "read_only_service"})
    do_PUT = do_POST
    do_DELETE = do_POST


def openapi(port):
    def operation(name, parameters=None, binary=False):
        return {"get": {"operationId": name, "parameters": parameters or [], "responses": {"200": {"description": "Successful response; verification fields describe evidence, not assembly readiness", "content": {"application/octet-stream" if binary else "application/json": {"schema": {"type": "string", "format": "binary"} if binary else {"type": "object"}}}}, "400": {"description": "Invalid request"}, "404": {"description": "Not found"}, "409": {"description": "File hash changed"}}}}
    mid = [{"name": "model_id", "in": "path", "required": True, "schema": {"type": "string"}}]
    query = [{"name": "q", "in": "query", "required": True, "schema": {"type": "string", "minLength": 1, "maxLength": 256}}, {"name": "section", "in": "query", "schema": {"type": "string", "enum": list(library.SECTIONS)}}, {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}}]
    return {"openapi": "3.0.3", "info": {"title": "RoboMaster CAD Local Retrieval", "version": "0.2.0", "description": "Local callers only. Search existing CAD before designing. Cite source.url and limitations. Unknown verification never means verified. Download the source package for native assemblies; dependencies may remain unresolved."}, "servers": [{"url": f"http://127.0.0.1:{port}"}], "paths": {"/health": operation("health"), "/v1/sections": operation("listSections"), "/v1/search": operation("searchModels", query), "/v1/models/{model_id}": operation("getModel", mid), "/v1/models/{model_id}/file": operation("downloadModel", mid, True), "/v1/models/{model_id}/package": operation("downloadPackage", mid, True)}}


def make_server(root, port=8765):
    catalog = Catalog(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.catalog = catalog
    server.daemon_threads = True
    return server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.root, args.port)
    print(f"Listening on http://127.0.0.1:{server.server_port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
