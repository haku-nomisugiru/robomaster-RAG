"""Normalize read-only SolidWorks occurrence snapshots against the local inventory."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath

from component_scope import excluded_component
SUPPRESSION = {0: "suppressed", 1: "lightweight", 2: "fully_resolved",
               3: "resolved", 4: "fully_lightweight", 5: "internal_id_mismatch"}
REPORT = Path("reports/native-occurrences.jsonl")


def windows_key(path):
    return str(PureWindowsPath(path)).casefold() if path else ""


def hash_file(path, cache):
    if path not in cache:
        with path.open("rb") as stream:
            cache[path] = hashlib.file_digest(stream, "sha256").hexdigest()
    return cache[path]


def world_transform_mm(values):
    """Convert a rigid SolidWorks row-vector transform in metres to a column-vector matrix in mm."""
    if not isinstance(values, list) or len(values) != 16 or not all(map(math.isfinite, values)):
        return None
    if not math.isclose(values[12], 1, rel_tol=1e-9):
        return None
    return [[values[row + 3 * column] for column in range(3)] + [values[9 + row] * 1000]
            for row in range(3)] + [[0, 0, 0, 1]]


def configuration_lookup(geometry, bom):
    """Keep BOM representatives distinct from a count-matched, single-configuration group."""
    rows = bom.get("rows") or []
    direct = {(windows_key(row.get("file")), row.get("component")): row["configuration"]
              for row in rows if row.get("configuration") and row.get("file")}
    grouped = defaultdict(list)
    for row in rows:
        if row.get("file"):
            grouped[windows_key(row["file"])].append(row)
    resolved_counts = Counter(windows_key(row.get("path")) for row in geometry if row.get("suppression") in (2, 3))
    agreed_groups = {
        key: values[0]["configuration"] for key,values in grouped.items()
        if len(values) == 1 and values[0].get("configuration")
        and values[0].get("quantity") == resolved_counts[key]
    }
    return direct, agreed_groups


def reference_for(root, path, parent, files, cache):
    if not path:
        return {"status": "no_file_path_returned"}
    key = windows_key(path)
    target = files.get(key)
    if not target:
        reported = PureWindowsPath(path)
        if "^" in reported.name:
            return {"status": "virtual_component_path"}
        folders = [part.casefold() for part in reported.parts[:-1]]
        if "temp" in folders and any(part.startswith("swx") for part in folders):
            return {"status": "solidworks_temporary_path"}
        asset_dir = root / "assets" / str(parent["thread_id"]) / parent["asset_id"].rsplit("-", 1)[-1]
        if reported.is_relative_to(PureWindowsPath(asset_dir)):
            relative = reported.relative_to(PureWindowsPath(root)).as_posix()
            actual = (root / relative).resolve()
            if actual.is_relative_to(asset_dir.resolve()):
                return {"status": "not_in_inventory" if actual.is_file() else "missing_in_asset"}
        return {"status": "outside_asset" if reported.is_absolute() else "unqualified_path"}
    if target["asset_id"] != parent["asset_id"]:
        return {"status": "other_asset"}
    actual = (root / target["path"]).resolve()
    if not actual.is_relative_to(root) or not actual.is_file():
        return {"status": "missing_in_asset"}
    if hash_file(actual, cache) != target["sha256"]:
        return {"status": "hash_mismatch"}
    return {"status": "in_asset_verified", "path": target["path"],
            "sha256": target["sha256"], "asset_id": target["asset_id"]}


def normalize_snapshot(root, row, snapshot, files, cache):
    expected = (root / row["path"]).resolve()
    if not expected.is_relative_to(root) or hash_file(expected, cache) != row["sha256"]:
        raise ValueError(f"Native source differs from inventory: {row['path']}")
    configuration = snapshot.get("config") or {}
    geometry = snapshot.get("geometry") or {}
    report = {
        "schema_version": "1.0", "path": row["path"], "sha256": row["sha256"],
        "asset_id": row["asset_id"], "model_id": snapshot["model_id"],
        "inspected_at_utc": datetime.now(timezone.utc).isoformat(),
        "backend": "SolidWorks COM via read-only desktop inspection",
        "read_scope": snapshot["scope"], "status": "not_observed",
        "save_requested": False, "dependency_closure_verified": False,
        "native_mates_verified": False, "interfaces_verified": False, "occurrences": [],
        "closed_after_read": (snapshot.get("closed") or {}).get("status") == "ok",
    }
    matching = all(windows_key(result.get("document", {}).get("path")) == windows_key(expected)
                   for result in (configuration, geometry))
    if not matching or geometry.get("status") != "ok" or geometry.get("save_requested") is not False:
        result = geometry or snapshot.get("opened") or {}
        report["reason"] = result.get("error_code") or result.get("status") or "no_matching_active_document"
        return report
    active = configuration.get("active_configuration")
    if not active:
        report["reason"] = "active_configuration_not_returned"
        return report
    components = [
        component for component in geometry.get("components", [])
        if component.get("name") and not excluded_component(row["thread_id"], component["name"])
    ]
    if not components:
        report["reason"] = "no_in_scope_occurrences"
        return report
    by_name = {component["name"]: component for component in components}
    if len(by_name) != len(components):
        raise ValueError(f"Duplicate native instance paths for {row['path']}")
    direct_configs, group_configs = configuration_lookup(components, snapshot.get("bom") or {})
    for name, component in by_name.items():
        parent, _, leaf = name.rpartition("/")
        key = windows_key(component.get("path"))
        referenced = direct_configs.get((key, name))
        basis = "bom_representative" if referenced else None
        code = component.get("suppression")
        if not referenced and code in (2, 3) and key in group_configs:
            referenced = group_configs[key]
            basis = "count_matched_bom_group"
        reference = reference_for(root, component.get("path"), row, files, cache)
        reference["path_semantics"] = "resolved_component_path" if code in (2, 3) else "saved_or_unresolved_path"
        raw_transform = component.get("transform")
        report["occurrences"].append({
            "occurrence_id": name, "name": leaf, "name_path": name.split("/"),
            "parent_occurrence_id": parent if parent in by_name else None,
            "parent_in_snapshot": not parent or parent in by_name,
            "suppression_code": code, "suppression": SUPPRESSION.get(code, "unknown"),
            "referenced_configuration": referenced, "configuration_evidence": basis,
            "reference": reference,
            "world_transform_mm": world_transform_mm(raw_transform),
            "solidworks_transform_array": raw_transform,
        })
    report.update(
        status="observed",
        active_configuration=active, configuration_names=configuration.get("configurations") or [],
        observed_instance_count=len(report["occurrences"]),
        source_instance_count=geometry.get("component_count_before_filter", len(components)),
        reference_counts=dict(Counter(item["reference"]["status"] for item in report["occurrences"])),
        suppression_counts=dict(Counter(item["suppression"] for item in report["occurrences"])),
        referenced_configuration_count=sum(item["referenced_configuration"] is not None for item in report["occurrences"]),
    )
    return report


def link_live_occurrences(modules, reports):
    by_path = {module["entry"]["path"]:module for module in modules
               if module["selection_scope"] == "file"}
    for path, report in reports.items():
        module = by_path.get(path)
        if (not module or report["sha256"] != module["entry"]["sha256"]
                or report["status"] != "observed"):
            continue
        locator = module["component_locator"]
        complete_scope = (report["read_scope"] == "active_configuration"
                          and report["observed_instance_count"] == report["source_instance_count"])
        locator.update(
            native_tree_status="live_configuration_observed" if complete_scope else "live_branch_observed",
            native_live_report=REPORT.as_posix(),
            active_configuration=report["active_configuration"],
            occurrence_count=report["observed_instance_count"],
            occurrence_count_scope=report["read_scope"] if complete_scope else "observed_subset",
        )
        module["native_configurations"] = {
            "status": "active_configuration_inspected", "values": report["configuration_names"],
            "active": report["active_configuration"], "checked": [report["active_configuration"]],
        }
        module["dependency_scope"]["live_reference_counts"] = report["reference_counts"]
        module["verification"]["native_active_configuration_read"] = True
        linked = Counter()
        examples = []
        for occurrence in report["occurrences"]:
            reference = occurrence["reference"]
            target = by_path.get(reference.get("path"))
            target_id = None
            if target and target["entry"]["sha256"] == reference.get("sha256"):
                target_id = target["id"]
                if occurrence["suppression_code"] in (2, 3):
                    linked[target_id] += 1
            examples.append((
                0 if target_id and target["card_tier"] != "component" else 1,
                {
                    "id": occurrence["occurrence_id"],
                    "parent_occurrence_id": occurrence["parent_occurrence_id"],
                    "name_path": occurrence["name_path"],
                    "referenced_configuration": occurrence["referenced_configuration"],
                    "world_transform_mm": occurrence["world_transform_mm"],
                    "reference_status": reference["status"],
                    "path_semantics": reference["path_semantics"],
                    "target_card_id": target_id,
                },
            ))
        locator["occurrence_examples"] = [item for _,item in sorted(examples,key=lambda pair:pair[0])[:5]]
        locator["live_reference_targets"] = [{"card_id": mid, "instance_count": count}
                                             for mid,count in linked.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Raw read-only CAD tool snapshots")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    baseline = json.loads((root / "reports/MVP_BASELINE.json").read_text(encoding="utf-8"))
    by_id = {sample["model_id"]:sample for sample in baseline["samples"]}
    inventory = json.loads((root / "catalog/inventory.json").read_text(encoding="utf-8"))
    by_path = {row["path"]:row for row in inventory}
    files = {windows_key(root / row["path"]):row for row in inventory}
    snapshots = json.loads(args.snapshot.read_text(encoding="utf-8"))
    cache = {}
    reports = []
    for snapshot in snapshots:
        entry = by_id[snapshot["model_id"]]["entry"]
        row = by_path[entry["path"]]
        if entry["sha256"] != row["sha256"]:
            raise ValueError("Native input differs from frozen baseline")
        reports.append(normalize_snapshot(root,row,snapshot,files,cache))
    target = root / REPORT
    pending = target.with_suffix(".jsonl.part")
    pending.write_text("".join(json.dumps(record, ensure_ascii=False)+"\n" for record in reports),
                       encoding="utf-8")
    pending.replace(target)
    for record in reports:
        print(json.dumps({key: value for key,value in record.items() if key in
                         {"model_id","status","reason","read_scope","active_configuration","observed_instance_count",
                          "source_instance_count","reference_counts","suppression_counts","referenced_configuration_count"}},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
