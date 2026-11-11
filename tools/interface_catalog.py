"""Attach interface measurements only when their CAD and context still match."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


EVIDENCE_DIR = Path("catalog/interface-evidence")


def file_has_hash(root, record, cache):
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False
    if path not in cache:
        with path.open("rb") as stream:
            cache[path] = hashlib.file_digest(stream, "sha256").hexdigest()
    return cache[path] == record["sha256"]


def context_is_current(context, record, cards, steps, native, root_dir, cache):
    assembly = cards.get(context["card_id"])
    root = cards.get(context["root_card_id"])
    if (not assembly or not root or assembly["entry"]["sha256"] != context["sha256"]
            or root["entry"]["sha256"] != context["root_sha256"]
            or assembly["definition_id"] != context.get("definition_id")
            or not file_has_hash(root_dir, root["entry"], cache)):
        return False
    path = root["entry"]["path"]
    target = record["target"]
    if context["kind"] == "step_xcaf":
        report = steps.get(path, {})
        if (path != target["path"] or assembly["entry"]["path"] != path
                or report.get("sha256") != target["sha256"]
                or report.get("status") not in {"ok", "partial"}):
            return False
        instances = {item["occurrence_id"]: item for item in report["occurrences"]}
        occurrence = instances.get(context["occurrence_id"])
        if not occurrence or occurrence["definition_id"] != target["definition_id"]:
            return False
        parent = instances.get(occurrence["parent_occurrence_id"])
        return not assembly["definition_id"] or (parent and parent["definition_id"] == assembly["definition_id"])
    if context["kind"] == "solidworks_live":
        report = native.get(path, {})
        if (assembly["id"] != root["id"] or report.get("sha256") != root["entry"]["sha256"]
                or report.get("status") != "observed"
                or report.get("active_configuration") != context["root_configuration"]):
            return False
        occurrence = next((item for item in report["occurrences"]
                           if item["occurrence_id"] == context["occurrence_id"]), None)
        if not occurrence or occurrence["suppression_code"] not in (1, 2, 3):
            return False
        if occurrence["suppression_code"] == 1 and (
                not occurrence.get("world_transform_mm")
                or occurrence.get("configuration_evidence") != "bom_representative"):
            return False
        reference = occurrence["reference"]
        return (reference.get("path") == target["path"] and reference.get("sha256") == target["sha256"]
                and occurrence.get("referenced_configuration") == target["configuration"]
                and reference.get("path_semantics") == context["path_semantics"])
    raise ValueError(f"Unknown interface context kind: {context['kind']}")


def read_interface_evidence(root, modules, steps, native):
    cards = {module["id"]: module for module in modules}
    records = []
    ids = set()
    cache = {}
    for path in sorted((root / EVIDENCE_DIR).glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record["schema_version"] != "1.0" or record["id"] != path.stem or record["id"] in ids
                or record["evidence_type"] != "cad_geometry_measured"):
            raise ValueError(f"Invalid interface evidence file {path.name}")
        ids.add(record["id"])
        record["record_path"] = path.relative_to(root).as_posix()
        target = record["target"]
        measurement = record["measurement_input"]
        if measurement["kind"] not in {"original_step", "solidworks_step_export"}:
            raise ValueError(f"Unknown measurement input in {path.name}")
        card = cards.get(target["card_id"])
        if card:
            record["component_id"] = card["component_id"]
        if not card:
            reason = "target_card_missing"
        elif (card["entry"]["path"] != target["path"] or card["entry"]["sha256"] != target["sha256"]
              or card["definition_id"] != target["definition_id"] or not file_has_hash(root, target, cache)):
            reason = "target_revision_changed"
        elif not file_has_hash(root, measurement, cache):
            reason = "measurement_input_changed"
        elif measurement["kind"] == "original_step" and (
                measurement["path"] != target["path"] or measurement["sha256"] != target["sha256"]
                or measurement["definition_id"] != target["definition_id"]
                or steps.get(target["path"], {}).get("sha256") != target["sha256"]
                or steps.get(target["path"], {}).get("parser", {}).get("version") != record["method"]["ocp_version"]):
            reason = "step_definition_binding_changed"
        elif measurement["kind"] == "solidworks_step_export" and (
                not target["configuration"] or measurement.get("native_configuration") != target["configuration"]):
            reason = "configuration_binding_changed"
        else:
            reason = None
        record["state"] = "stale" if reason else "active"
        if reason:
            record["stale_reason"] = reason
        record["contexts"] = [
            {**context, "state": "active" if not reason
             and context_is_current(context, record, cards, steps, native, root, cache) else "stale"}
            for context in record.get("contexts", [])
        ]
        records.append(record)
    return records


def attach_interface_evidence(modules, records):
    cards = {module["id"]: module for module in modules}
    by_component = {}
    for module in modules:
        by_component.setdefault(module["component_id"], []).append(module)
    for record in records:
        targets = by_component.get(record.get("component_id"), ())
        if record["state"] != "active":
            for target in targets:
                target["interfaces"].setdefault("stale_measurements", []).append({
                    "id": record["id"], "reason": record["stale_reason"], "record_path": record["record_path"],
                })
                if target["interfaces"]["status"] == "not_measured":
                    target["interfaces"]["status"] = "stale_evidence"
            continue
        geometry = record["geometry"]
        for target in targets:
            interface = target["interfaces"]
            interface["status"] = "cad_geometry_measured"
            if interface["mounting_faces"] is None:
                interface["mounting_faces"] = []
                interface["hole_patterns"] = []
            interface["mounting_faces"].append({"evidence_id": record["id"], **geometry["mounting_face"]})
            interface["hole_patterns"].append({"evidence_id": record["id"], **geometry["hole_pattern"]})
            interface.setdefault("measurements", []).append(record)
            target["verification"]["interface_geometry_measured"] = True
        for context in record["contexts"]:
            if context["state"] == "active":
                other = cards[context["card_id"]]["interfaces"]
                if other["status"] in {"not_measured", "stale_evidence"}:
                    other["status"] = "component_evidence_available"
                other.setdefault("component_measurements", []).append({
                    "id": record["id"], "component_card_id": record["target"]["card_id"],
                    "component_id": record["component_id"],
                    "label": record["label"], "role": context["role"],
                    "record_path": record["record_path"], "occurrence_id": context["occurrence_id"],
                })
