"""Build and query the reference CAD index from STEP or catalog cards."""

import argparse
from collections import defaultdict
from math import isfinite
from pathlib import Path
import json
import sys

from geometry_brep import compare, describe, read_shape, sample_surface, step_shape, surface_signature, to_mesh
from geometry_index import build, catalog_cards, current_bindings
from ingest_forum import digest_file
from inspect_step import SUPPORTED_EXTENSIONS, read_xde
from library import MECHANISM_CATEGORIES, ROOT


MODES = ("size", "shape")
KINDS = ("same", "part", "assembly", "any")
QUALITY = {"curated": 0, "source_reviewed": 1}


def extract_worker(path):
    targets = json.load(sys.stdin)
    document, tool = read_xde(path)
    descriptors, failures = {}, []
    # Finish small definitions before their compound assemblies so a timeout retains useful geometry.
    ordered = []
    for key, definition in targets.items():
        try:
            shape, kind, _ = step_shape(document, tool, definition)
            ordered.append((kind == "assembly", definition is None, key, definition, shape, kind))
        except Exception as exc:
            failures.append({"definition_id": definition, "reason": str(exc)})
    ordered.sort(key=lambda item: item[:2])
    for _, _, key, definition, shape, kind in ordered:
        try:
            descriptor = describe(shape, kind)
            print("descriptor " + json.dumps([key, descriptor], allow_nan=False), flush=True)
            descriptors[key] = descriptor
        except Exception as exc:
            failures.append({"definition_id": definition, "reason": str(exc)})
    return {"descriptors": descriptors, "failures": failures}


def find_reference(bindings, card_id, configuration):
    choices = [binding for binding in bindings if binding["card_id"] == card_id
               and (configuration is None or binding["source"]["configuration"] == configuration)]
    if not choices:
        raise ValueError("Reference card has no current STEP geometry for this configuration")
    if len(choices) > 1:
        raise ValueError("Choose a configuration for this reference card")
    return choices[0]


def file_reference(path, definition):
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS or not path.is_file():
        raise ValueError("Reference file must be a readable .step or .stp file")
    state = path.stat()
    sha = digest_file(path)
    document, (shape, kind, label) = read_shape(path, definition)
    after = path.stat()
    if (state.st_size, state.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Reference file changed during OCP extraction")
    record = {"geometry": describe(shape, kind), "source": {"kind": "reference_step", "path": str(path),
              "sha256": sha, "definition_id": definition, "xde_label": label, "configuration": None}}
    return record, (document, shape)


def candidate_groups(bindings, cards, kind, category, excluded_key):
    grouped = defaultdict(list)
    for binding in bindings:
        card = cards[binding["card_id"]]
        groups = card.get("mechanism_profile", {}).get("categories", [])
        if (binding["key"] != excluded_key and (kind == "any" or binding["geometry"]["kind"] == kind)
                and (category is None or category == card["category"] or category in groups)):
            grouped[binding["key"]].append(binding)
    for copies in grouped.values():
        copies.sort(key=lambda item: (QUALITY.get(cards[item["card_id"]]["card_tier"], 2), item["card_id"]))
        yield copies


def sample_rerank(root, reference, shape_holder, ranked, points, deflection):
    import numpy as np

    if shape_holder is None:
        document, (shape, _, _) = read_shape(root / reference["source"]["path"],
                                             reference["source"]["definition_id"])
        shape_holder = (document, shape)
    ref_signature = surface_signature(sample_surface(to_mesh(shape_holder[1], deflection), points))
    files = defaultdict(list)
    for row in ranked:
        files[row["binding"]["source"]["path"]].append(row)
    for path, rows in files.items():
        try:
            document, tool = read_xde(root / path)
        except Exception as exc:
            for row in rows:
                row["surface_sample_status"] = "unavailable"
                row["surface_sample_error"] = str(exc)
            continue
        for row in rows:
            try:
                source = row["binding"]["source"]
                shape, _, _ = step_shape(document, tool, source["definition_id"])
                signature = surface_signature(sample_surface(to_mesh(shape, deflection), points))
                error = float(np.mean(np.abs(ref_signature - signature)))
                row["distance_components"]["surface_sample"] = 0.35 * error
                row["distance"] += 0.35 * error
                row["surface_sample_status"] = "sampled"
            except Exception as exc:
                row["surface_sample_status"] = "unavailable"
                row["surface_sample_error"] = str(exc)


def search(root, *, card_id=None, reference_file=None, definition=None, configuration=None,
           mode="size", kind="same", category=None, limit=5, include_reference=False,
           sample_points=0, deflection_mm=0.4, cards=None):
    if (card_id is None) == (reference_file is None):
        raise ValueError("Choose exactly one reference CAD file or catalog card")
    if (mode not in MODES or kind not in KINDS or not 1 <= limit <= 50
            or category is not None and category not in MECHANISM_CATEGORIES):
        raise ValueError("Invalid similarity mode, kind, category or limit")
    if sample_points != 0 and not 128 <= sample_points <= 8192:
        raise ValueError("Surface sample count must be 0 or 128..8192")
    if not isfinite(deflection_mm) or not 0.01 <= deflection_mm <= 20:
        raise ValueError("Mesh deflection must be 0.01..20 mm")
    if (card_id is not None and definition is not None) or (reference_file is not None and configuration is not None):
        raise ValueError("Use a STEP definition with a file, or a configuration with a card")
    cards = cards if cards is not None else catalog_cards(root)
    bindings = current_bindings(root, cards)
    holder = None
    if card_id is not None:
        if card_id not in cards:
            raise KeyError(card_id)
        reference = find_reference(bindings, card_id, configuration)
    else:
        reference, holder = file_reference(Path(reference_file).resolve(), definition)
    matched_kind = reference["geometry"]["kind"] if kind == "same" else kind
    excluded = reference.get("key") if not include_reference else None
    ranked = []
    for copies in candidate_groups(bindings, cards, matched_kind, category, excluded):
        item = copies[0]
        delta = compare(reference["geometry"], item["geometry"], mode)
        exact_source = (reference["source"]["sha256"] == item["source"]["sha256"]
                        and reference["source"]["definition_id"] == item["source"]["definition_id"])
        ranked.append({"binding": item, "copy_card_ids": [copy["card_id"] for copy in copies],
                       "distance": sum(delta.values()), "distance_components": delta,
                       "same_step_bytes_and_definition": exact_source})

    def rank_key(row):
        return (not row["same_step_bytes_and_definition"], row["distance"], row["binding"]["card_id"])

    ranked.sort(key=rank_key)
    if sample_points and ranked:
        pool = ranked[:min(len(ranked), max(limit, 8))]
        sample_rerank(root, reference, holder, pool, sample_points, deflection_mm)
        pool.sort(key=rank_key)
        ranked[:len(pool)] = pool
    results = []
    for row in ranked[:limit]:
        item = row.pop("binding")
        card = cards[item["card_id"]]
        results.append({**row, "id": card["id"], "name": card["name"], "category": card["category"],
                        "cad_source": card["cad_source"], "geometry_input": item["source"],
                        "geometry": item["geometry"]})
    return {"mode": mode, "kind": matched_kind, "category": category,
            "reference": {"card_id": reference.get("card_id"), "geometry_input": reference["source"],
                          "geometry": reference["geometry"]},
            "indexed_candidates": len(ranked), "surface_sample_points": sample_points, "items": results}


def write_preview(root, args):
    import trimesh

    output = args.output
    if output.suffix.lower() != ".glb":
        raise ValueError("Preview output must have a .glb extension")
    if args.points and (args.points.suffix.lower() != ".ply" or not 128 <= args.sample_points <= 8192):
        raise ValueError("PLY output requires a .ply extension and 128..8192 sample points")
    if args.card:
        cards = catalog_cards(root)
        selected = find_reference(current_bindings(root, cards), args.card, args.configuration)
        document, (shape, _, _) = read_shape(root / selected["source"]["path"], selected["source"]["definition_id"])
    else:
        _, (document, shape) = file_reference(args.file, args.definition)
    mesh = to_mesh(shape, args.deflection_mm)
    preview = mesh.copy()
    preview.apply_scale(0.001)
    payloads = [(output, preview.export(file_type="glb"))]
    result = {"preview": str(output.resolve()), "preview_units": "m", "triangles": len(mesh.faces)}
    if args.points:
        cloud = trimesh.points.PointCloud(sample_surface(mesh, args.sample_points))
        payloads.append((args.points, cloud.export(file_type="ply")))
        result.update(points=str(args.points.resolve()), point_units="mm", samples=args.sample_points)
    for target, data in payloads:
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(target.suffix + ".part")
        pending.write_bytes(data)
        pending.replace(target)
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) == 3 and sys.argv[1] == "_extract":
        print(json.dumps(extract_worker(Path(sys.argv[2])), allow_nan=False))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    actions = parser.add_subparsers(dest="action", required=True)
    index = actions.add_parser("build")
    index.add_argument("--timeout", type=int, default=420)
    query = actions.add_parser("search")
    preview = actions.add_parser("preview")
    for command in (query, preview):
        reference = command.add_mutually_exclusive_group(required=True)
        reference.add_argument("--card")
        reference.add_argument("--file", type=Path)
        command.add_argument("--definition")
        command.add_argument("--configuration")
        command.add_argument("--deflection-mm", type=float, default=0.4)
    query.add_argument("--mode", choices=MODES, default="size")
    query.add_argument("--kind", choices=KINDS, default="same")
    query.add_argument("--category", choices=MECHANISM_CATEGORIES)
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--include-reference", action="store_true")
    query.add_argument("--sample-points", type=int, default=0)
    preview.add_argument("--output", type=Path, required=True)
    preview.add_argument("--points", type=Path)
    preview.add_argument("--sample-points", type=int, default=2048)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "build":
        result = build(root, args.timeout)
    elif args.action == "preview":
        if (not isfinite(args.deflection_mm) or not 0.01 <= args.deflection_mm <= 20
                or (args.card and args.definition) or (args.file and args.configuration)):
            parser.error("Invalid preview definition, configuration or mesh deflection")
        result = write_preview(root, args)
    else:
        result = search(root, card_id=args.card, reference_file=args.file, definition=args.definition,
                        configuration=args.configuration, mode=args.mode, kind=args.kind,
                        category=args.category, limit=args.limit, include_reference=args.include_reference,
                        sample_points=args.sample_points, deflection_mm=args.deflection_mm)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
