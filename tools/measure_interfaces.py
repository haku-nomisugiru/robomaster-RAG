"""Inspect STEP faces or measure selected interfaces from pinned CAD inputs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from ingest_forum import write_json
from interface_brep import GEOMETRY_TOL_MM, definition_shape, measurement, planar_faces
from interface_catalog import EVIDENCE_DIR, file_has_hash


def scan_faces(path, definition_id, face_index):
    _doc, definition, shape = definition_shape(path, definition_id)
    faces = []
    for face in planar_faces(shape):
        if face_index == face["face_index"]:
            face["openings"] = [{"index": i, **opening} for i, opening in enumerate(face["openings"], 1)]
            faces.append(face)
            break
        if face_index is None and face["openings"]:
            face["circular_opening_count"] = len(face.pop("openings"))
            faces.append(face)
    if face_index is not None and not faces:
        raise ValueError(f"Face {face_index} is not a planar face in this STEP definition")
    print(json.dumps({"definition_id": definition, "length_unit": "mm", "faces": faces}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--id", action="append")
    mode.add_argument("--scan", type=Path, metavar="STEP_PATH")
    parser.add_argument("--definition", metavar="XCAF_LABEL")
    parser.add_argument("--face", type=int, metavar="INDEX")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.scan:
        path = (root / args.scan).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in {".step", ".stp"} or not path.is_file():
            parser.error("--scan requires a STEP file in the library")
        scan_faces(path, args.definition, args.face)
        return
    if args.definition is not None or args.face is not None:
        parser.error("--definition and --face require --scan")
    manifest = json.loads((root / "catalog/interface-selections.json").read_text(encoding="utf-8"))
    import OCP

    if manifest["schema_version"] != "1.0":
        raise ValueError("Unsupported interface selection schema")
    selections = {row["id"]: row for row in manifest["selections"]}
    if len(selections) != len(manifest["selections"]):
        raise ValueError("Duplicate interface selection ID")
    wanted = args.id or list(selections)
    cache = {}
    pending = []
    for key in wanted:
        if not re.fullmatch(r"[a-z0-9-]+", key):
            raise ValueError(f"Invalid interface ID {key}")
        selection = selections[key]
        target = selection["target"]
        source = selection["measurement_input"]
        if not all(file_has_hash(root, record, cache) for record in (target,source)):
            raise ValueError(f"CAD revision changed for {key}; review the selector before measuring.")
        if source["kind"] == "solidworks_step_export":
            if not target["configuration"] or source.get("native_configuration") != target["configuration"]:
                raise ValueError(f"Native configuration differs for {key}")
        elif (source["kind"] != "original_step" or source["path"] != target["path"]
              or source["definition_id"] != target["definition_id"]):
            raise ValueError(f"Original STEP binding differs for {key}")
        geometry = measurement(root / source["path"], source.get("definition_id"), selection["selector"])
        pending.append({
            **selection, "schema_version": "1.0", "evidence_type": "cad_geometry_measured",
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "method": {"engine": "OCP/XCAF", "ocp_version": OCP.__version__,
                       "method": "planar face and analytic circular inner wires",
                       "numeric_geometry_tolerance_mm": GEOMETRY_TOL_MM},
            "units": {"length": "mm", "area": "mm2"},
            "manufacturing_tolerance_mm": None, "fit_to_target_verified": False,
            "geometry": geometry,
        })
    for record in pending:
        write_json(root / EVIDENCE_DIR / (record["id"] + ".json"), record)
        print(record["id"], len(record["geometry"]["hole_pattern"]["openings"]))


if __name__ == "__main__":
    main()
