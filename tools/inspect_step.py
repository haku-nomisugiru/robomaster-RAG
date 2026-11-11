"""Read-only STEP/XDE inspection; all geometric quantities are normalized to mm.

STEP product structure is not evidence of native SolidWorks features, mates,
material density, manufacturing readiness, or motion reachability.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.1"
SUPPORTED_EXTENSIONS = {".step", ".stp"}


def _entry(label) -> str:
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TDF import TDF_Tool
    value = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, value)
    return value.ToCString()


def _name(label) -> str:
    from OCP.TDataStd import TDataStd_Name
    attribute = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return attribute.Get().ToExtString()
    return ""


def _matrix(transform) -> list[list[float]]:
    return [
        [float(transform.Value(row, column)) for column in range(1, 5)]
        for row in range(1, 4)
    ] + [[0.0, 0.0, 0.0, 1.0]]


def _shape_metrics(shape) -> dict[str, Any]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    if shape.IsNull():
        return {"shape_present": False, "solid_occurrence_count": 0}
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    metrics: dict[str, Any] = {"shape_present": True}
    if not box.IsVoid():
        bounds = [float(value) for value in box.Get()]
        metrics["aabb_mm"] = {
            "min": bounds[:3],
            "max": bounds[3:],
            "size": [bounds[i + 3] - bounds[i] for i in range(3)],
            "method": "OCCT BRepBndLib.AddOptimal; static geometry",
        }
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    solids = 0
    while explorer.More():
        solids += 1
        explorer.Next()
    metrics["solid_occurrence_count"] = solids
    if solids:
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, properties)
        metrics["volume_mm3"] = float(properties.Mass())
        metrics["volume_scope"] = (
            "algebraic geometric volume; component overlap is not subtracted; "
            "not material mass or an interference result"
        )
    else:
        metrics["volume_mm3"] = None
    return metrics


def read_xde(path: Path):
    """Transfer a STEP document into a millimetre XDE document and its shape tool."""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    if not Interface_Static.SetCVal_s("xstep.cascade.unit", "MM"):
        raise RuntimeError("OCCT could not configure millimetre transfer units.")
    status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"STEP ReadFile failed: {status}")
    reader.ChangeReader().SetSystemLengthUnit(1.0)
    doc = TDocStd_Document(TCollection_ExtendedString("XDE"))
    if not reader.Transfer(doc):
        raise RuntimeError("STEP transfer to XDE failed.")
    return doc, XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())


def inspect_step(path: Path | str, *, root: Path | str | None = None,
                 max_nodes: int = 20000, max_depth: int = 64,
                 max_metric_definitions: int = 2000,
                 world_metrics: bool = True) -> dict[str, Any]:
    """Inspect one STEP, isolating parse/geometry errors in the returned record."""
    path = Path(path).resolve()
    root = Path(root).resolve() if root is not None else path.parent
    try:
        source_path = path.relative_to(root).as_posix()
    except ValueError:
        source_path = path.as_posix()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "path": source_path,
        "format": "STEP",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "status": "error",
        "units": {"length": "mm", "volume": "mm3"},
        "geometry_scope": "static_configuration",
        "native_mates_verified": False,
        "native_features_verified": False,
        "material_mass_verified": False,
        "definitions": [],
        "occurrences": [],
        "root_definition_ids": [],
        "truncated": False,
        "errors": [],
        "warnings": [],
    }
    if max_nodes < 1 or max_depth < 0 or max_metric_definitions < 0:
        result["errors"].append("Inspection limits must be non-negative; max_nodes must be positive.")
        return result
    try:
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("Only .step/.stp are supported; native CAD is not inspected.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
        result["bytes"] = path.stat().st_size

        import OCP
        from OCP.BRep import BRep_Builder
        from OCP.TDF import TDF_Label, TDF_LabelSequence
        from OCP.TopoDS import TopoDS_Compound
        from OCP.gp import gp_Trsf

        result["parser"] = {"engine": "OCP/XCAF", "version": OCP.__version__,
                            "report_schema": SCHEMA_VERSION}

        doc, tool = read_xde(path)
        roots = TDF_LabelSequence()
        tool.GetFreeShapes(roots)
        if roots.Length() == 0:
            raise RuntimeError("STEP transfer yielded no free/root shapes.")

        definitions: dict[str, dict[str, Any]] = {}
        definitions_with_metrics = 0
        root_compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(root_compound)

        def resolve(label):
            current = label
            seen = set()
            while tool.IsReference_s(current):
                key = _entry(current)
                if key in seen:
                    raise ValueError("Cyclic XDE reference chain.")
                seen.add(key)
                target = TDF_Label()
                if not tool.GetReferredShape_s(current, target):
                    raise ValueError(f"Unresolved XDE reference: {key}")
                current = target
            return current

        def ensure_definition(label) -> str:
            nonlocal definitions_with_metrics
            key = _entry(label)
            if key in definitions:
                return key
            assembly = bool(tool.IsAssembly_s(label))
            record: dict[str, Any] = {
                "definition_id": key,
                "name": _name(label),
                "kind": "assembly" if assembly else "shape",
                "child_component_label_ids": [],
            }
            definitions[key] = record
            if definitions_with_metrics < max_metric_definitions:
                definitions_with_metrics += 1
                try:
                    record["geometry_definition_mm"] = _shape_metrics(tool.GetShape_s(label))
                except Exception as error:
                    record["geometry_error"] = f"{type(error).__name__}: {error}"
                    result["errors"].append(f"Geometry for {key}: {error}")
            else:
                record["geometry_not_computed"] = "max_metric_definitions"
            if assembly:
                children = TDF_LabelSequence()
                tool.GetComponents_s(label, children, False)
                record["child_component_label_ids"] = [
                    _entry(children.Value(i)) for i in range(1, children.Length() + 1)
                ]
            return key

        def visit(label, parent_id: str | None, parent_world, chain: tuple,
                  path_ids: tuple, path_names: tuple, depth: int):
            if len(result["occurrences"]) >= max_nodes:
                result["truncated"] = True
                return
            target = resolve(label)
            definition_id = ensure_definition(target)
            local = tool.GetLocation_s(label).Transformation()
            world = parent_world.Multiplied(local)
            label_id = _entry(label)
            occurrence_id = "/".join(path_ids + (label_id,))
            name = _name(label) or _name(target) or label_id
            occurrence = {
                "occurrence_id": occurrence_id,
                "component_label_id": label_id,
                "definition_id": definition_id,
                "parent_occurrence_id": parent_id,
                "name": name,
                "name_path": list(path_names + (name,)),
                "local_transform_mm": _matrix(local),
                "world_transform_mm": _matrix(world),
                "is_reference": bool(tool.IsReference_s(label)),
                "children": [],
            }
            result["occurrences"].append(occurrence)
            if parent_id is None:
                result["root_definition_ids"].append(definition_id)
            if definition_id in chain:
                occurrence["cycle_detected"] = True
                result["truncated"] = True
                result["errors"].append(f"Cyclic assembly expansion at {occurrence_id}")
                return
            if not tool.IsAssembly_s(target):
                return
            children = TDF_LabelSequence()
            tool.GetComponents_s(target, children, False)
            if depth >= max_depth and children.Length():
                occurrence["children_truncated"] = "max_depth"
                result["truncated"] = True
                return
            for i in range(1, children.Length() + 1):
                child = children.Value(i)
                if len(result["occurrences"]) >= max_nodes:
                    occurrence["children_truncated"] = "max_nodes"
                    result["truncated"] = True
                    break
                child_id = occurrence_id + "/" + _entry(child)
                occurrence["children"].append(child_id)
                try:
                    visit(child, occurrence_id, world, chain + (definition_id,),
                          path_ids + (label_id,), path_names + (name,), depth + 1)
                except Exception as error:
                    result["errors"].append(f"Occurrence {child_id}: {type(error).__name__}: {error}")

        for index in range(1, roots.Length() + 1):
            label = roots.Value(index)
            root_shape = tool.GetShape_s(label)
            if world_metrics and not root_shape.IsNull():
                builder.Add(root_compound, root_shape)
            if len(result["occurrences"]) >= max_nodes:
                result["truncated"] = True
                continue
            visit(label, None, gp_Trsf(), (), (), (), 0)

        by_instance = {row["occurrence_id"]: row for row in result["occurrences"]}
        occurrence_counts = Counter(row["definition_id"] for row in by_instance.values())
        child_definitions = defaultdict(dict)
        for row in by_instance.values():
            parent = by_instance.get(row["parent_occurrence_id"])
            if parent:
                child_definitions[parent["definition_id"]][row["definition_id"]] = None
        for definition_id, definition in definitions.items():
            definition["occurrence_count"] = occurrence_counts[definition_id]
            definition["child_definition_ids"] = list(child_definitions[definition_id])
        result["definitions"] = list(definitions.values())
        result["definition_count"] = len(definitions)
        result["occurrence_count"] = len(result["occurrences"])
        result["free_root_count"] = roots.Length()
        if world_metrics:
            result["geometry_world_mm"] = _shape_metrics(root_compound)
            if not result["geometry_world_mm"].get("shape_present"):
                raise RuntimeError("No transferred geometry.")
            result["geometry_world_mm"]["aabb_scope"] = "aabb_world_mm/static_configuration"
        else:
            result["warnings"].append("World metrics omitted for hierarchy-only inspection.")
        if definitions_with_metrics < len(definitions):
            result["warnings"].append("Some definition metrics omitted by max_metric_definitions.")
        result["status"] = "partial" if result["errors"] or result["truncated"] else "ok"
    except Exception as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    # The recursive closure otherwise retains the XDE document until cyclic GC,
    # which can accumulate a large CAD model between consecutive batch files.
    visit = None
    return result


def paths_from_inventory(data: Any, root: Path) -> list[Path]:
    if isinstance(data, dict):
        for key in ("files", "assets", "documents", "inventory"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError("Inventory requires a files/assets/documents/inventory list.")
    if not isinstance(data, list):
        raise ValueError("Inventory must be a list or an object containing a file list.")
    paths = []
    seen = set()
    for row in data:
        if isinstance(row, str):
            value = row
        elif isinstance(row, dict):
            value = next((row[key] for key in ("path", "local_path", "relative_path", "file_path")
                          if isinstance(row.get(key), str)), None)
        else:
            value = None
        if not value or Path(value).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        path = Path(value)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Inventory path escapes library root: {value}")
        normalized = str(path).casefold()
        if normalized not in seen:
            seen.add(normalized)
            paths.append(path)
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--max-depth", type=int, default=64)
    parser.add_argument("--max-metric-definitions", type=int, default=2000)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    paths: list[Path] = []
    for path in args.input:
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(root):
            parser.error("Input files must be inside --root.")
        paths.append(path)
    if args.inventory:
        inventory = args.inventory if args.inventory.is_absolute() else root / args.inventory
        if inventory.suffix.lower() == ".jsonl":
            data = [json.loads(line) for line in inventory.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        else:
            data = json.loads(inventory.read_text(encoding="utf-8-sig"))
        paths.extend(paths_from_inventory(data, root))
    paths = list(dict.fromkeys(paths))
    if not paths:
        parser.error("No STEP inputs found.")
    output = (root / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    if not output.is_relative_to(root):
        parser.error("Output must be inside --root.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    failures = 0
    with temporary.open("w", encoding="utf-8") as stream:
        for path in paths:
            record = inspect_step(path, root=root, max_nodes=args.max_nodes,
                                  max_depth=args.max_depth,
                                  max_metric_definitions=args.max_metric_definitions)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps({"path": record["path"], "status": record["status"],
                              "definitions": record.get("definition_count", 0),
                              "occurrences": record.get("occurrence_count", 0),
                              "errors": record["errors"]}, ensure_ascii=False), flush=True)
            failures += record["status"] != "ok"
    temporary.replace(output)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
