"""Loopback-only, read-only CAD catalog HTTP service with optional local Chroma search."""
import argparse
import hashlib
import json
import re
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlsplit
import library
from cad_acquisition import Acquisition
from component_metadata import component_candidates, parse_spec
from component_scope import package_available
from geometry_search import KINDS as GEOMETRY_KINDS, MODES as GEOMETRY_MODES, search as search_geometry
from hybrid_search import SemanticSearch, fuse

class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.modules = {m["id"]: m for m in map(json.loads, (self.root / "catalog/modules.jsonl").read_text(encoding="utf-8").splitlines())}
        self.assets = {a["asset_id"]: a for a in json.loads((self.root / "catalog/downloads.json").read_text(encoding="utf-8")) if a.get("status") == "downloaded"}
        self.sources = {s["source_id"]: s for s in json.loads((self.root / "catalog/sources.json").read_text(encoding="utf-8"))}
        self.acquisition = Acquisition(self.root, self.modules)
        self.db = self.root / "catalog/library.sqlite3"
        if not self.db.is_file(): raise ValueError("Build the catalog first")
        self.semantic = SemanticSearch(self.root, self.modules)

    def detail(self, mid):
        if mid not in self.modules: raise KeyError(mid)
        result = dict(self.modules[mid])
        prefix = "/v1/models/" + quote(mid, safe="")
        asset = self.assets.get(result["entry"]["asset_id"])
        if asset and asset.get("volume_paths"):
            result["dependency_scope"] = dict(result["dependency_scope"],
                                               local_archive_volumes=list(asset["volume_paths"]))
        if asset and not package_available(asset):
            asset = None
        file_available = result.get("component_locator", {}).get("file_available", True)
        result["downloads"] = {"model": prefix + "/file" if file_available else None, "package": prefix + "/package" if asset and file_available else None, "package_sha256": asset.get("sha256") if asset and file_available else None, "package_name": asset.get("name") if asset and file_available else None}
        result["source"] = {k: self.sources[result["source_id"]].get(k) for k in ["source_id", "url", "title", "snapshot_kind", "body_path", "page_sha256"]}
        result["usage"] = {"selection_scope": result["selection_scope"], "definition_id": result.get("definition_id"), "package_contains_dependencies": "not_verified", "ready_for_assembly": False}
        result["reuse_href"] = prefix + "/reuse"
        return result

    def reuse(self, mid):
        card = self.detail(mid)
        entry = card["entry"]
        downloads = card["downloads"]
        native_assembly = entry["extension"] in {".sldasm", ".asm", ".iam"}
        artifacts = {}
        if downloads["model"]:
            artifacts["file"] = {
                "href": downloads["model"], "sha256": entry["sha256"],
                "filename": PurePosixPath(entry["path"]).name,
                "scope": "source_cad_file",
            }
        if downloads["package"]:
            artifacts["package"] = {
                "href": downloads["package"], "sha256": downloads["package_sha256"],
                "filename": downloads["package_name"], "scope": "original_attachment",
            }
        preferred = "package" if native_assembly and "package" in artifacts else "file" if "file" in artifacts else None
        steps = []
        if card.get("definition_id"):
            steps.append("locate_step_definition")
        if native_assembly:
            steps.append("resolve_native_references")
        steps.extend(("review_interfaces", "verify_fit_in_target_assembly"))
        interfaces = card["interfaces"]
        profile = card.get("retrieval_profile") or {}
        return {
            "id": mid, "name": card["name"], "category": card["category"],
            "details": card["reuse_href"].removesuffix("/reuse"),
            "source": card["source"], "cad_source": card["cad_source"],
            "acquisition": self.acquisition.plan(card),
            "selection": {
                "scope": card["selection_scope"], "definition_id": card.get("definition_id"),
                "occurrence_path": card.get("occurrence_path"),
                "asset_relative_path": PurePosixPath(*PurePosixPath(entry["path"]).parts[3:]).as_posix(),
                "native_configurations": card["native_configurations"],
            },
            "artifacts": artifacts, "preferred_artifact": preferred,
            "dependency_scope": card["dependency_scope"],
            "interface_status": interfaces["status"],
            "interface_measurements": interfaces.get("measurements", []),
            "component_measurements": [
                {**item, "component_reuse_href": "/v1/models/" + quote(item["component_card_id"], safe="") + "/reuse"}
                for item in interfaces.get("component_measurements", [])
            ],
            "facts": profile.get("facts", []),
            "mechanism_claims": (card.get("mechanism_profile") or {}).get("claims", []),
            "verification": card["verification"],
            "reuse_checks": profile.get("reuse_checks", []), "required_checks": steps,
        }

    def search(self, q, section, limit, mode="auto", *, alias=None, specs=(), category=None):
        # Reuse ranking but connect read-only; the HTTP server never rebuilds the index.
        query_term_count = len(library.tokens(q)) if mode == "function" else 0
        functional = bool(query_term_count)
        depth = max(20, limit * 3) if functional else limit
        hits = library.search(self.root, q, depth, section, mode, alias=alias, specs=specs, category=category)
        retrieval = "lexical"
        vector_status = "not_requested"
        if functional:
            allowed = None
            if alias is not None or specs:
                with closing(library.connect(self.root, read_only=True)) as db:
                    allowed = component_candidates(db, alias, [parse_spec(raw) for raw in specs])
            if category is not None:
                category_ids = {mid for mid, card in self.modules.items()
                                if category in card.get("mechanism_profile", {}).get("categories", [])}
                allowed = category_ids if allowed is None else allowed & category_ids
            if allowed is None or allowed:
                semantic_hits = self.semantic.search(q, depth, section, candidate_ids=allowed)
                vector_status = self.semantic.status
                if semantic_hits is not None:
                    hits = fuse(hits, semantic_hits, self.modules, limit, query_term_count)
                    retrieval = "hybrid"
        response_keys = ("score", "exact_match", "matches", "model_matches", "retrieval_channels")
        return {"retrieval": retrieval, "vector_status": vector_status, "items": [
            {**self.detail(hit["id"]), **{key: hit[key] for key in response_keys if key in hit}}
            for hit in hits[:limit]
        ]}

    def file_record(self, mid, kind):
        m = self.detail(mid)
        download_key = "model" if kind == "file" else "package"
        if not m["downloads"][download_key]:
            raise KeyError(mid)
        record = m["entry"] if kind == "file" else self.assets.get(m["entry"]["asset_id"])
        if not record or (kind == "package" and not package_available(record)): raise KeyError(mid)
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
            if any(len(values) != 1 for key,values in args.items() if key != "spec"):
                raise ValueError("Duplicate query parameter")
            catalog = self.server.catalog
            if url.path == "/health":
                state = catalog.semantic.status
                return self.json_response(200, {"status": "ok", "models": len(catalog.modules),
                                                "retrieval": "hybrid" if state == "ready" else "lexical",
                                                "vector_status": state, "api_version": "1"})
            if url.path == "/openapi.json":
                return self.json_response(200, openapi(self.server.server_port))
            if url.path == "/v1/sections":
                return self.json_response(200, {"items": [dict(id=k, label=v["label"], count=sum(m.get("section") == k for m in catalog.modules.values())) for k,v in library.SECTIONS.items()]})
            if url.path == "/v1/similar":
                if set(args) - {"reference_id", "configuration", "mode", "kind", "category", "limit", "include_reference"}:
                    raise ValueError("Unknown geometry query parameter")
                reference_id = args.get("reference_id", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", reference_id):
                    raise ValueError("Supply a catalog reference_id")
                include_reference = args.get("include_reference", ["false"])[0]
                if include_reference not in {"true", "false"}:
                    raise ValueError("include_reference must be true or false")
                result = search_geometry(
                    catalog.root, cards=catalog.modules, card_id=reference_id,
                    configuration=args.get("configuration", [None])[0], mode=args.get("mode", ["size"])[0],
                    kind=args.get("kind", ["same"])[0], category=args.get("category", [None])[0],
                    limit=int(args.get("limit", ["5"])[0]), include_reference=include_reference == "true",
                )
                for item in result["items"]:
                    item["details"] = "/v1/models/" + quote(item["id"], safe="")
                    item["reuse_href"] = item["details"] + "/reuse"
                return self.json_response(200, result)
            if url.path == "/v1/search":
                if set(args) - {"q", "section", "limit", "mode", "alias", "spec", "category"}:
                    raise ValueError("Unknown query parameter")
                q = args.get("q", [""])[0].strip()
                section = args.get("section", [None])[0]
                limit = int(args.get("limit", ["5"])[0])
                mode = args.get("mode", ["auto"])[0]
                category = args.get("category", [None])[0]
                alias = args["alias"][0].strip() if "alias" in args else None
                specs = args.get("spec", [])
                if len(q) > 256 or (not q and (mode == "model" or not (alias or specs or category))):
                    raise ValueError("Supply q or a component filter")
                if alias is not None and not 1 <= len(alias) <= 256: raise ValueError("Invalid alias")
                if len(specs) > 8 or any(len(spec) > 96 for spec in specs): raise ValueError("Too many or too long specs")
                if not 1 <= limit <= 50: raise ValueError("limit must be 1..50")
                if section is not None and section not in library.SECTIONS: raise ValueError("Invalid section")
                if mode not in library.SEARCH_MODES: raise ValueError("Invalid search mode")
                results = catalog.search(q, section, limit, mode, alias=alias, specs=specs, category=category)
                return self.json_response(200, {"query": q, "section": section, "mode": mode,
                                                "category": category, "count": len(results["items"]), **results})
            match = re.fullmatch(r"/v1/models/([a-zA-Z0-9_-]+)(?:/(file|package|reuse))?", url.path)
            if match:
                mid, kind = match.groups()
                if not kind: return self.json_response(200, catalog.detail(mid))
                if kind == "reuse":
                    return self.json_response(200, catalog.reuse(mid))
                try:
                    path, expected = catalog.file_record(mid, kind)
                except FileNotFoundError:
                    return self.json_response(404, {"error": "cad_not_installed",
                                                    "reuse_href": f"/v1/models/{mid}/reuse"})
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
    def operation(name, parameters=None, binary=False, description="", schema=None):
        success = {
            "description": "Successful response",
            "content": {
                "application/octet-stream" if binary else "application/json": {
                    "schema": {"type": "string", "format": "binary"} if binary else schema or {"type": "object"}
                }
            },
        }
        if binary:
            success["headers"] = {"X-Content-SHA256": {"description": "SHA-256 of the returned file",
                                                       "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}
        return {"get": {"operationId": name, "description": description, "parameters": parameters or [],
                        "responses": {"200": success, "400": {"description": "Invalid request"},
                                      "404": {"description": "Not found"},
                                      "409": {"description": "File hash changed"}}}}
    mid = [{"name": "model_id", "in": "path", "required": True, "schema": {"type": "string"}}]
    query = [{"name": "q", "in": "query", "description": "Query or use a component filter", "schema": {"type": "string", "maxLength": 256}}, {"name": "section", "in": "query", "schema": {"type": "string", "enum": list(library.SECTIONS)}}, {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}}]
    query.append({"name": "mode", "in": "query", "description": "function uses SQLite and local Chroma; model uses exact SQLite lookup; auto preserves the original lexical mode", "schema": {"type": "string", "enum": list(library.SEARCH_MODES),
                                                          "default": "auto"}})
    query.extend([
        {"name": "category", "in": "query", "description": "Mechanical function group",
         "schema": {"type": "string", "enum": list(library.MECHANISM_CATEGORIES)}},
        {"name": "alias", "in": "query", "description": "Exact normalized component alias",
         "schema": {"type": "string", "minLength": 1, "maxLength": 256}},
        {"name": "spec", "in": "query", "style": "form", "explode": True,
         "description": "Numeric specification as key:value or key:min:max; an empty bound is open",
         "schema": {"type": "array", "maxItems": 8, "items": {"type": "string"}}},
    ])
    geometry = [
        {"name": "reference_id", "in": "query", "required": True, "schema": {"type": "string"}},
        {"name": "configuration", "in": "query", "schema": {"type": "string"}},
        {"name": "mode", "in": "query", "schema": {"type": "string", "enum": list(GEOMETRY_MODES), "default": "size"}},
        {"name": "kind", "in": "query", "schema": {"type": "string", "enum": list(GEOMETRY_KINDS), "default": "same"}},
        {"name": "category", "in": "query", "schema": {"type": "string", "enum": list(library.MECHANISM_CATEGORIES)}},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}},
        {"name": "include_reference", "in": "query", "schema": {"type": "boolean", "default": False}},
    ]
    reuse_schema = {
        "type": "object",
        "required": ["id", "selection", "artifacts", "preferred_artifact", "verification", "required_checks"],
        "properties": {
            "id": {"type": "string"},
            "acquisition": {"type": "object", "description": "CAD identity and the available acquisition method.",
                            "properties": {"status": {"type": "string", "enum": [
                                "local", "cached_attachment", "author_download", "manual_import", "unavailable"]},
                                "card_id": {"type": "string"}, "cad_path": {"type": "string"},
                                "cad_sha256": {"type": "string"}, "definition_id": {"type": "string", "nullable": True},
                                "source_url": {"type": "string"}, "author_url": {"type": "string", "nullable": True},
                                "attachment": {"type": "object", "nullable": True}}},
            "selection": {"type": "object", "properties": {
                "scope": {"type": "string"}, "definition_id": {"type": "string", "nullable": True},
                "asset_relative_path": {"type": "string"}, "native_configurations": {"type": "object"},
            }},
            "artifacts": {"type": "object", "additionalProperties": {
                "type": "object", "required": ["href", "sha256", "filename", "scope"],
                "properties": {key: {"type": "string"} for key in ("href", "sha256", "filename", "scope")},
            }},
            "preferred_artifact": {"type": "string", "nullable": True, "enum": ["file", "package", None]},
            "verification": {"type": "object"},
            "required_checks": {"type": "array", "items": {"type": "string"}},
            "component_measurements": {"type": "array", "items": {"type": "object"}},
            "mechanism_claims": {"type": "array", "items": {"type": "object"}},
        },
    }
    return {
        "openapi": "3.0.3", "info": {
            "title": "RoboMaster CAD Local Retrieval", "version": "0.5.1",
            "description": "Find CAD by function, model or reference geometry. Read the selected card's reuse "
                           "evidence before choosing an artifact. Check the download SHA-256 and preserve the "
                           "STEP definition or native configuration when reviewing the CAD.",
        },
        "servers": [{"url": f"http://127.0.0.1:{port}"}],
        "paths": {
            "/health": operation("health"), "/v1/sections": operation("listSections"),
            "/v1/search": operation("searchModels", query, description="Return candidates with reuse_href."),
            "/v1/similar": operation("searchSimilarCad", geometry,
                                     description="Rank geometric candidates for an indexed reference CAD card."),
            "/v1/models/{model_id}": operation("getModel", mid),
            "/v1/models/{model_id}/reuse": operation(
                "getReuseEvidence", mid, schema=reuse_schema,
                description="Get source evidence, CAD selection, artifact choices and checks for one candidate."),
            "/v1/models/{model_id}/file": operation("downloadModel", mid, True),
            "/v1/models/{model_id}/package": operation("downloadPackage", mid, True),
        },
    }


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
