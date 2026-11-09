"""Inspect saved direct references through an already running SolidWorks session.

No CAD document is opened, activated, closed, rebuilt, or saved. Searchflag is
always False: enabling it would change SolidWorks' current directory.
Official contract:
https://help.solidworks.com/2023/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.ISldWorks~GetDocumentDependencies2.html
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
from typing import Any

API_REFERENCE = (
    "https://help.solidworks.com/2023/english/api/sldworksapi/"
    "SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks."
    "ISldWorks~GetDocumentDependencies2.html"
)


class EnvironmentUnavailable(RuntimeError):
    pass


@contextmanager
def active_solidworks():
    """Attach via the ROT only; never create a COM application/session."""
    try:
        import pythoncom
        import pywintypes
        from win32com.client import dynamic
    except ImportError as error:
        raise EnvironmentUnavailable(f"pywin32 is unavailable: {error}") from error
    initialized = False
    app = None
    try:
        pythoncom.CoInitialize()
        initialized = True
        try:
            # dynamic.Dispatch wraps an EXISTING IDispatch pointer; unlike
            # Dispatch('SldWorks.Application') it cannot launch SolidWorks.
            clsid = pywintypes.IID("SldWorks.Application")
            unknown = pythoncom.GetActiveObject(clsid)
            dispatch = unknown.QueryInterface(pythoncom.IID_IDispatch)
            app = dynamic.Dispatch(dispatch)
            app._FlagAsMethod("GetDocumentDependencies2")
        except Exception as error:
            raise EnvironmentUnavailable(
                f"No accessible running SolidWorks COM session: {type(error).__name__}: {error}"
            ) from error
        yield app
    finally:
        app = None
        if initialized:
            pythoncom.CoUninitialize()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_rows(data: Any) -> list[dict]:
    if isinstance(data, dict):
        for key in ("files", "assets", "documents", "inventory"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError("Expected an inventory list of file objects.")
    return data


def local_inventory_path(row: dict, root: Path) -> Path:
    path = Path(row["path"])
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Inventory path escapes library root.")
    return path


def asset_scope(row: dict, rows: list[dict], root: Path) -> tuple[Path, list[dict]]:
    """Restrict all matching to this source AND this downloaded asset."""
    source = row.get("source_id")
    asset = row.get("asset_id")
    if not source or not asset:
        raise ValueError("Native references require source_id and asset_id.")
    candidates = [item for item in rows if item.get("source_id") == source
                  and item.get("asset_id") == asset]
    entry = local_inventory_path(row, root)
    if row.get("asset_root"):
        directory = (root / row["asset_root"]).resolve()
    else:
        parts = entry.relative_to(root).parts
        # Ingest layout: assets/<thread id>/<download SHA prefix>/<members>.
        if len(parts) >= 4 and parts[0] == "assets":
            directory = root.joinpath(*parts[:3]).resolve()
        else:
            directory = Path(os.path.commonpath([
                str(local_inventory_path(item, root).parent) for item in candidates
            ])).resolve()
    if not directory.is_relative_to(root) or not entry.is_relative_to(directory):
        raise ValueError("Invalid asset scope.")
    for item in candidates:
        if not local_inventory_path(item, root).is_relative_to(directory):
            raise ValueError("Inventory mixes multiple physical asset scopes.")
    return directory, candidates


def classify_reference(name: str, saved_path: str, *, rows: list[dict],
                       directory: Path, root: Path) -> dict:
    """Keep a basename relocation hypothesis separate from a resolved path."""
    record = {
        "original_name": name,
        "original_path": saved_path,
        "original_path_status": "relative_or_empty",
        "match_status": "missing",
        "resolved_local_path": None,
        "potential_local_paths": [],
    }
    # Do not stat or hash paths outside the asset (including author-machine
    # drives and UNC shares). Lexical normalization happens before resolution.
    raw = Path(saved_path) if saved_path else None
    if raw is not None and raw.is_absolute():
        lexical = Path(os.path.abspath(os.path.normpath(str(raw))))
        if lexical.is_relative_to(directory):
            actual = lexical.resolve()
            if actual.is_relative_to(directory) and actual.is_file():
                record.update(original_path_status="resolved_local",
                              match_status="resolved_local",
                              resolved_local_path=actual.relative_to(root).as_posix())
                return record
            record["original_path_status"] = "missing"
        else:
            record["original_path_status"] = "outside_asset"
    elif saved_path and (ntpath.isabs(saved_path) or saved_path.startswith("\\\\")):
        record["original_path_status"] = "outside_asset"

    basename = ntpath.basename(saved_path) or ntpath.basename(name)
    local = []
    missing_inventory_paths = []
    for item in rows:
        path = local_inventory_path(item, root)
        if path.name.casefold() != basename.casefold():
            continue
        if path.is_file():
            local.append(path.relative_to(root).as_posix())
        else:
            missing_inventory_paths.append(path.relative_to(root).as_posix())
    record["potential_local_paths"] = sorted(set(local))
    if len(record["potential_local_paths"]) == 1:
        record["match_status"] = "potential_local_match"
        record["matching_method"] = "unique_same_source_same_asset_basename; not CAD resolution"
    elif len(record["potential_local_paths"]) > 1:
        record["match_status"] = "ambiguous"
    elif record["original_path_status"] == "outside_asset":
        record["match_status"] = "outside_asset"
    if missing_inventory_paths:
        record["inventory_candidates_missing_on_disk"] = missing_inventory_paths
    return record


def inspect_native(row: dict, rows: list[dict], root: Path, *, app=None,
                   environment_error: str | None = None) -> dict:
    result = {
        "schema_version": "1.0",
        "source_id": row.get("source_id"),
        "asset_id": row.get("asset_id"),
        "path": row.get("path"),
        "sha256": row.get("sha256"),
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "status": "not_run",
        "read_scope": "saved_direct_dependencies",
        "api": "ISldWorks.GetDocumentDependencies2",
        "api_reference": API_REFERENCE,
        "api_flags": {"Traverseflag": False, "Searchflag": False, "AddReadOnlyInfo": False},
        "original_references_raw": None,
        "references": [],
        "reference_counts": {},
        "dependency_closure_verified": False,
        "native_mates_verified": False,
        "native_configurations_verified": False,
        "cad_document_mutation_performed": False,
        "errors": [],
    }
    if environment_error or app is None:
        result["status"] = "environment_unavailable"
        result["errors"].append(environment_error or "No running SolidWorks session supplied.")
        return result
    try:
        path = local_inventory_path(row, root)
        if path.suffix.casefold() != ".sldasm":
            raise ValueError("Only .sldasm is supported by this native reference inspector.")
        actual_hash = digest_file(path)
        result["sha256"] = actual_hash
        result["inventory_sha256"] = row.get("sha256")
        if row.get("sha256") and actual_hash != row["sha256"]:
            result["status"] = "input_changed"
            result["errors"].append("Assembly content differs from inventory SHA256; CAD was not queried.")
            return result
        directory, candidates = asset_scope(row, rows, root)
        result["asset_scope_path"] = directory.relative_to(root).as_posix()
    except Exception as error:
        result["status"] = "file_or_inventory_error"
        result["errors"].append(f"{type(error).__name__}: {error}")
        return result
    try:
        raw = app.GetDocumentDependencies2(str(path), False, False, False)
        if raw is None or raw == ():
            result["original_references_raw"] = None if raw is None else []
            result["status"] = "no_references_returned"
            result["notes"] = [
                "The API returned no references; this does not prove a complete or loadable assembly."
            ]
            return result
        if not isinstance(raw, (tuple, list)) or any(not isinstance(value, str) for value in raw):
            result["original_references_raw"] = repr(raw)
            result["status"] = "api_result_unrecognized"
            result["errors"].append("Expected string pairs (document name, complete saved path).")
            return result
        result["original_references_raw"] = list(raw)
        if len(raw) % 2:
            result["status"] = "api_result_unrecognized"
            result["errors"].append("Odd number of reference strings; pair boundaries are unknown.")
            return result
        result["references"] = [
            classify_reference(raw[index], raw[index + 1], rows=candidates,
                               directory=directory, root=root)
            for index in range(0, len(raw), 2)
        ]
        result["reference_counts"] = dict(Counter(
            reference["match_status"] for reference in result["references"]
        ))
        result["status"] = "snapshot_read"
    except Exception as error:
        result["status"] = "cad_api_error"
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--inventory", type=Path, default=Path("catalog/inventory.json"))
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("reports/native-dependencies.jsonl"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    inventory = args.inventory if args.inventory.is_absolute() else root / args.inventory
    if inventory.suffix.lower() == ".jsonl":
        rows = inventory_rows([json.loads(line) for line in
                               inventory.read_text(encoding="utf-8-sig").splitlines() if line.strip()])
    else:
        rows = inventory_rows(json.loads(inventory.read_text(encoding="utf-8-sig")))
    native = [row for row in rows if Path(row["path"]).suffix.casefold() == ".sldasm"]
    if args.input:
        chosen = set()
        for path in args.input:
            path = (root / path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_relative_to(root):
                parser.error("--input must be within --root.")
            chosen.add(path)
        native = [row for row in native if local_inventory_path(row, root) in chosen]
        if {local_inventory_path(row, root) for row in native} != chosen:
            parser.error("Each --input must be a .sldasm present in the inventory.")
    if not native:
        parser.error("No native assemblies selected.")
    output = args.output if args.output.is_absolute() else root / args.output
    output = output.resolve()
    if not output.is_relative_to(root):
        parser.error("Output must stay within --root.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    failures = 0

    def emit(app, environment_error=None):
        nonlocal failures
        with temporary.open("w", encoding="utf-8") as stream:
            for row in native:
                result = inspect_native(row, rows, root, app=app,
                                        environment_error=environment_error)
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
                print(json.dumps({"path": result["path"], "status": result["status"],
                                  "reference_counts": result["reference_counts"],
                                  "errors": result["errors"]}, ensure_ascii=False), flush=True)
                failures += result["status"] not in {"snapshot_read", "no_references_returned"}
    try:
        with active_solidworks() as app:
            emit(app)
    except EnvironmentUnavailable as error:
        emit(None, str(error))
    temporary.replace(output)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
