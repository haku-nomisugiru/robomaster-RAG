"""Searchable component aliases, numeric specifications, and original CAD provenance."""

from math import isfinite
from pathlib import PurePosixPath
import re

from retrieval import normalize


SPEC_UNITS = {
    "bbox_x_mm": "mm", "bbox_y_mm": "mm", "bbox_z_mm": "mm",
    "opening_diameter_mm": "mm", "opening_center_distance_mm": "mm",
    "opening_count": "count", "mounting_face_area_mm2": "mm2",
}


def decorate_component(module, source, inventory_row, definition):
    entry = module["entry"]
    path = PurePosixPath(entry["path"])
    loc = module["component_locator"]
    cad_source = {
        **entry, "source_id": source["source_id"], "source_url": source["url"],
        "archive_path": inventory_row["archive_path"], "snapshot_path": source.get("body_path"),
        "snapshot_sha256": source["page_sha256"],
        "definition_id": module["definition_id"], "occurrence_id": module["occurrence_path"],
        "locator_kind": loc["kind"], "locator_report": loc.get("report"),
        "file_available": loc.get("file_available", True),
    }
    if definition:
        cad_source["definition_name"] = definition.get("name")
        cad_source["definition_kind"] = definition["kind"]
    module["cad_source"] = cad_source
    aliases = {}

    def add_alias(label, kind):
        if label and (key := normalize(label)):
            aliases.setdefault(key, {"value": label, "kind": kind})

    for alias in module.get("retrieval_profile", {}).get("aliases", []):
        add_alias(alias, "reviewed_alias")
    add_alias(module["name"], "card_name")
    if definition:
        add_alias(definition.get("name"), "step_definition_name")
    else:
        add_alias(path.stem, "cad_stem")
        add_alias(path.name, "cad_filename")
        # Version suffixes in this library use a dotted year/month/day. Preserve the full name too.
        short_name = re.sub(r"[._ -]20\d{2}\.\d{1,2}\.\d{1,2}$", "", path.stem)
        if short_name != path.stem:
            add_alias(short_name, "cad_name_without_date")
    module["component_aliases"] = list(aliases.values())

    specs = []

    def add_spec(key, value, scope, evidence):
        if isinstance(value, (int, float)) and isfinite(value) and value >= 0:
            specs.append({"key": key, "value": round(value, 8), "unit": SPEC_UNITS[key],
                          "scope": scope, "evidence": evidence})

    def add_bounds(bounds, scope, evidence):
        for axis, size in zip("xyz", (bounds or {}).get("size") or []):
            add_spec(f"bbox_{axis}_mm", size, scope, evidence)

    step = module.get("geometry_summary", {})
    if step.get("metrics"):
        add_bounds(step["metrics"].get("aabb_mm"),
                   {"coordinate_system": step["coordinate_scope"], "truncated": step["truncated"]},
                   {"type": "step_inspection", "path": module["geometry_report"],
                    "cad_sha256": entry["sha256"], "definition_id": module["definition_id"]})
        envelope = module["engineering_parameters"].get("envelope_mm")
        if envelope is not None and not step["truncated"] and len(specs) == 3:
            bounds = specs[:3]
            envelope.update(value={item["key"]: item["value"] for item in bounds},
                            status="cad_geometry_measured", evidence=[bounds[0]["evidence"]])
    configs = set()
    for record in module["interfaces"].get("measurements", []):
        geometry = record["geometry"]
        scope = {"interface_id": record["id"], "configuration": record["target"]["configuration"],
                 "definition_id": module["definition_id"]}
        evidence = {"type": "cad_geometry_measured", "path": record["record_path"],
                    "cad_sha256": entry["sha256"]}
        configuration = scope["configuration"]
        if configuration and configuration not in configs:
            configs.add(configuration)
            add_bounds(geometry.get("envelope_mm"),
                       {"configuration": configuration, "coordinate_system": "native_export_mm"}, evidence)
        pattern = geometry["hole_pattern"]
        for diameter in sorted({round(item["diameter_mm"], 8) for item in pattern["openings"]}):
            add_spec("opening_diameter_mm", diameter, scope, evidence)
        for distance in sorted({round(item["distance_mm"], 8) for item in pattern.get("center_distances_mm", [])}):
            add_spec("opening_center_distance_mm", distance, scope, evidence)
        add_spec("opening_count", len(pattern["openings"]), scope, evidence)
        add_spec("mounting_face_area_mm2", geometry["mounting_face"].get("area_mm2"), scope, evidence)
    module["component_specs"] = specs


def parse_spec(raw):
    pieces = raw.split(":")
    if len(pieces) not in (2, 3) or pieces[0] not in SPEC_UNITS:
        raise ValueError("Use spec key:value or key:min:max with a documented specification key")
    key = pieces[0]
    if len(pieces) == 2:
        lo = hi = float(pieces[1])
    else:
        lo, hi = (float(value) if value else None for value in pieces[1:])
    if (lo is None and hi is None or any(v is not None and (not isfinite(v) or v < 0) for v in (lo, hi))
            or lo is not None and hi is not None and lo > hi):
        raise ValueError("Invalid specification bounds")
    return key, lo, hi


def component_candidates(db, alias, specs):
    candidates = None
    if alias is not None:
        candidates = {row[0] for row in db.execute(
            "SELECT component_id FROM component_aliases WHERE alias_key=?", (normalize(alias),))}
    by_family = {}
    for key, lo, hi in specs:
        family = "envelope" if key.startswith("bbox_") else "interface"
        matches = {tuple(row) for row in db.execute("""
            SELECT component_id, scope FROM component_specs
            WHERE spec_key=? AND (? IS NULL OR value>=?) AND (? IS NULL OR value<=?)
        """, (key, lo, lo - 1e-7 if lo is not None else None,
              hi, hi + 1e-7 if hi is not None else None))}
        by_family[family] = by_family[family] & matches if family in by_family else matches
    for matches in by_family.values():
        components = {component for component, _ in matches}
        candidates = components if candidates is None else candidates & components
    if candidates is None:
        return None
    return {row[0] for row in db.execute("SELECT card_id,component_id FROM component_cards")
            if row[1] in candidates}
