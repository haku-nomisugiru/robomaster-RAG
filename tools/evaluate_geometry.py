"""Check geometric search on the catalog's real suspension parts and generated STEP references."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import urlopen

from geometry_brep import compare, read_shape
from geometry_index import catalog_cards, current_bindings
from geometry_search import find_reference, search, write_preview
from http_service import make_server
from ingest_forum import digest_file, write_json
from library import ROOT


PLATE = "rm-1935523-cc08defa62bc17d10"
ASSEMBLY = "rm-1935523-022"
FRONT_PLATE = "rm-1935523-c1ab8d2a2c8db5352"
NATIVE_OUTER = "rm-803685-ce2bee0af698aa86a"
EXAMPLES = Path("reports/geometry-examples")


def transform_step(shape, target, transform):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    result = BRepBuilderAPI_Transform(shape, transform, True).Shape()
    if writer.Transfer(result, STEPControl_AsIs) != IFSelect_RetDone:
        raise RuntimeError("Transformed B-Rep could not be transferred to STEP")
    if writer.Write(str(target)) != IFSelect_RetDone:
        raise RuntimeError("Transformed STEP could not be written")


def target_rank(items, card_id):
    return next((index for index, row in enumerate(items, 1) if card_id in row["copy_card_ids"]), None)


def verify_http(root):
    server = make_server(root, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    params = urlencode({"reference_id": PLATE, "limit": 3, "include_reference": "true"})
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/v1/similar?{params}", timeout=20) as response:
            result = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    top = result["items"][0]
    return {"passed": PLATE in top["copy_card_ids"] and top["same_step_bytes_and_definition"],
            "id": top["id"], "details": top["details"]}


def evaluate(root):
    import numpy as np
    import trimesh
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    examples = root / EXAMPLES
    examples.mkdir(parents=True, exist_ok=True)
    cards = catalog_cards(root)
    bindings = current_bindings(root, cards)
    plate = find_reference(bindings, PLATE, None)
    document, (shape, _, _) = read_shape(root / plate["source"]["path"], plate["source"]["definition_id"])
    rotated_path = examples / "suspension-plate-rotated.step"
    scaled_path = examples / "suspension-plate-scaled.step"
    transform = gp_Trsf()
    transform.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 2, 3)), 0.73)
    transform.SetTranslationPart(gp_Vec(95, -37, 48))
    transform_step(shape, rotated_path, transform)
    transform.SetScale(gp_Pnt(0, 0, 0), 1.5)
    transform_step(shape, scaled_path, transform)

    rotated = search(root, reference_file=rotated_path, cards=cards)
    scaled = search(root, reference_file=scaled_path, mode="shape", cards=cards)
    scaled_size_distance = sum(compare(scaled["reference"]["geometry"], plate["geometry"], "size").values())
    strict_rotation = (target_rank(rotated["items"], PLATE) == 1 and rotated["items"][0]["distance"] < 0.001
                       and not rotated["items"][0]["same_step_bytes_and_definition"])
    scale_invariant = (target_rank(scaled["items"], PLATE) == 1 and scaled["items"][0]["distance"] < 0.001
                       and scaled_size_distance > 0.25)

    assembly = search(root, card_id=ASSEMBLY, include_reference=True, limit=1, cards=cards)
    assembly_match = assembly["items"][0]
    native = search(root, card_id=NATIVE_OUTER, configuration="Mirror默认", include_reference=True,
                    limit=1, cards=cards)
    native_match = native["items"][0]
    sibling = search(root, card_id=FRONT_PLATE, limit=5, cards=cards)
    sibling_rank = target_rank(sibling["items"], "rm-1935523-cdd7be764df9ebc03")
    if sibling_rank is None:
        sibling_rank = target_rank(sibling["items"], "rm-1935523-c846ca1789a577ea6")
    preview = write_preview(root, SimpleNamespace(
        card=PLATE, file=None, configuration=None, definition=None, deflection_mm=0.4,
        output=examples / "suspension-plate.glb", points=examples / "suspension-plate.ply", sample_points=2048))
    scene = trimesh.load_scene(preview["preview"])
    cloud = trimesh.load(preview["points"])
    mesh_box = np.asarray(scene.extents) * 1000
    mesh_ok = (len(cloud.vertices) == 2048 and np.isfinite(cloud.vertices).all()
               and np.allclose(mesh_box, plate["geometry"]["aabb_mm"], atol=0.4))
    sampled = search(root, reference_file=rotated_path, limit=3, sample_points=512, cards=cards)
    sample_ok = (target_rank(sampled["items"], PLATE) == 1
                 and sampled["items"][0].get("surface_sample_status") == "sampled"
                 and "surface_sample" in sampled["items"][0]["distance_components"])
    http = verify_http(root)

    def cad_verified(result):
        source = result["cad_source"]
        return digest_file(root / source["path"]) == source["sha256"]

    checks = {
        "rotated_reference": strict_rotation,
        "scaled_reference": scale_invariant,
        "assembly_duplicate_collapse": len(assembly_match["copy_card_ids"]) >= 2 and assembly_match["distance"] == 0,
        "native_original_cad": (native_match["id"] == NATIVE_OUTER and native_match["distance"] == 0
                                and native_match["geometry_input"]["configuration"] == "Mirror默认"
                                and native_match["cad_source"]["extension"] == ".sldprt"),
        "returned_cad_hashes": all(cad_verified(row) for row in
                                  (rotated["items"][0], scaled["items"][0], assembly_match, native_match)),
        "related_hub_plate": sibling_rank is not None,
        "trimesh_glb_and_ply": bool(mesh_ok),
        "trimesh_reranking": sample_ok,
        "http": http["passed"],
    }
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "passed": all(checks.values()),
            "checks": checks, "rotated": rotated, "scaled": scaled, "scaled_size_distance": scaled_size_distance,
            "assembly": assembly_match, "native": native_match, "sibling": sibling, "sibling_rank": sibling_rank,
            "preview": preview, "sampled": sampled, "http": http}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    result = evaluate(root)
    write_json(root / "reports/GEOMETRY_ACCEPTANCE.json", result)
    print(json.dumps({"passed": result["passed"], "checks": result["checks"],
                      "sibling_rank": result["sibling_rank"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
