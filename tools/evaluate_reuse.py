"""Run the AI CAD reuse workflow against the loopback HTTP API."""

import argparse
from datetime import datetime, timezone
from hashlib import file_digest
import json
from math import dist, isclose
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile

from geometry_brep import read_shape
from http_service import make_server
from ingest_forum import write_json
from interface_brep import measurement
from library import ROOT

sys.path.insert(0, str(ROOT / "examples"))
from reuse_cad import download_artifact, find_candidates, get_json, get_reuse, reuse_candidate


FLANGE = "rm-1935523-022"
SWERVE = "rm-803685-014"
FRONT_HUB = "rm-1935523-c1ab8d2a2c8db5352"
GRIPPER = "rm-816172-019"


def expect(condition, explanation):
    if not condition:
        raise RuntimeError(explanation)


def locate_step(download, plan, expected_kind):
    definition_id = plan["selection"]["definition_id"]
    document, (shape, kind, label) = read_shape(Path(download["path"]), definition_id)
    expect(definition_id == label and kind == expected_kind and not shape.IsNull(),
           f"Downloaded STEP did not resolve {definition_id} as {expected_kind}")
    return {"definition_id": label, "kind": kind}


def source_record(plan, artifact, download):
    return {"card_id": plan["id"], "name": plan["name"], "source_url": plan["source"]["url"],
            "cad_path": plan["cad_source"]["path"], "definition_id": plan["selection"]["definition_id"],
            "artifact": artifact, "artifact_sha256": download["sha256"], "bytes": download["bytes"],
            "required_checks": plan["required_checks"],
            "fit_to_target_verified": plan["verification"]["fit_to_target_verified"]}


def evaluate(port, directory):
    paths = get_json("/openapi.json", port)["paths"]
    for endpoint, operation in (("/v1/search", "searchModels"), ("/v1/similar", "searchSimilarCad"),
                                ("/v1/models/{model_id}/reuse", "getReuseEvidence")):
        expect(paths[endpoint]["get"]["operationId"] == operation, f"Missing OpenAPI operation {operation}")

    results = {}
    text = "把轮组接到悬挂的机加工法兰"
    found = find_candidates(query=text, category="suspension", port=port)
    expect(found["retrieval"] == "hybrid" and found["items"][0]["id"] == FLANGE,
           "Functional search did not rank the intended suspension subassembly first")
    plan = reuse_candidate(found["items"], port=port)
    expect(plan["preferred_artifact"] == "file" and plan["facts"]
           and "locate_step_definition" in plan["required_checks"], "STEP reuse metadata is incomplete")
    downloaded = download_artifact(plan["artifacts"]["file"], directory / "step", port)
    located = locate_step(downloaded, plan, "assembly")
    evidence_link = plan["component_measurements"][0]
    component = get_json(evidence_link["component_reuse_href"], port)
    record = next(item for item in component["interface_measurements"] if item["id"] == evidence_link["id"])
    expect(record["state"] == "active" and component["cad_source"]["sha256"] == downloaded["sha256"],
           "Interface evidence points to a different CAD version")
    geometry = measurement(Path(downloaded["path"]), component["selection"]["definition_id"], record["selector"])
    openings = geometry["hole_pattern"]["openings"]
    locating_holes = [opening for opening in openings if isclose(opening["diameter_mm"], 6.0, abs_tol=1e-5)]
    expect(len(openings) == 9 and len(locating_holes) == 2, "Unexpected downloaded STEP hole pattern")
    span = dist(*(opening["center_mm"] for opening in locating_holes))
    expect(isclose(span, 55.0, abs_tol=1e-5), "STEP remeasurement differs from interface evidence")
    results["function"] = {
        "query": text, "retrieval": found["retrieval"], **source_record(plan, "file", downloaded),
        "ocp": located, "interface_id": record["id"], "interface_definition_id": geometry["step_definition_id"],
        "opening_count": len(openings), "two_6mm_opening_spacing_mm": span,
    }

    text = "3508 舵轮"
    found = find_candidates(query=text, mode="model", category="swerve", port=port)
    expect(found["vector_status"] == "not_requested" and found["items"][0]["id"] == SWERVE,
           "Exact model search returned an unexpected swerve result")
    matched_models = found["items"][0]["model_matches"]
    expect(any(model["code"] == "3508" and model["role"] == "used_component" for model in matched_models),
           "Swerve model evidence does not distinguish the component code")
    plan = reuse_candidate(found["items"], port=port)
    config = plan["selection"]["native_configurations"]
    expect(plan["preferred_artifact"] == "package" and config["active"] == "默认"
           and "resolve_native_references" in plan["required_checks"], "Native assembly metadata is incomplete")
    link = plan["component_measurements"][0]
    mounting_part = get_json(link["component_reuse_href"], port)
    evidence = next(item for item in mounting_part["interface_measurements"] if item["id"] == link["id"])
    expect(evidence["state"] == "active" and evidence["target"]["configuration"] == "Mirror默认",
           "Mounting evidence lost the referenced part configuration")
    downloaded = download_artifact(plan["artifacts"]["file"], directory / "swerve", port)
    results["model"] = {
        "query": text, **source_record(plan, "file", downloaded), "preferred_artifact": plan["preferred_artifact"],
        "assembly_configuration": config["active"], "interface_configuration": evidence["target"]["configuration"],
        "interface_id": evidence["id"], "model_matches": matched_models,
        "dependency_status": plan["dependency_scope"]["resolution_status"],
    }

    found = find_candidates(reference_id=FRONT_HUB, port=port)
    top = found["items"][0]
    expect(top["cad_source"]["definition_id"] == "0:1:1:20", "Unexpected nearest hub clamp plate")
    plan = reuse_candidate(found["items"], port=port)
    downloaded = download_artifact(plan["artifacts"]["file"], directory / "step", port)
    results["geometry"] = {
        "reference_id": FRONT_HUB, "distance": top["distance"], **source_record(plan, "file", downloaded),
        "ocp": locate_step(downloaded, plan, "part"),
    }

    text = "银矿抓取"
    found = find_candidates(query=text, category="gripper", port=port)
    plan = reuse_candidate(found["items"], GRIPPER, port)
    expect(plan["preferred_artifact"] == "package", "Native grip assembly did not select its source package")
    downloaded = download_artifact(plan["artifacts"]["package"], directory / "package", port)
    with ZipFile(downloaded["path"], metadata_encoding="gbk") as archive:
        member = plan["selection"]["asset_relative_path"]
        with archive.open(member) as source:
            member_sha = file_digest(source, "sha256").hexdigest()
    expect(member_sha == plan["cad_source"]["sha256"], "ZIP member differs from the selected native assembly")
    results["native_package"] = {
        "query": text, **source_record(plan, "package", downloaded),
        "zip_member": member, "zip_member_sha256": member_sha,
        "dependency_status": plan["dependency_scope"]["resolution_status"],
    }

    unknown = find_candidates(query="P15.76", mode="model", port=port)
    expect(reuse_candidate(unknown["items"], port=port) is None and unknown["vector_status"] == "not_requested",
           "Unknown model produced a reuse candidate")
    split = get_reuse("rm-761042-c2ed254897d8b3e8e", port)
    expect("package" not in split["artifacts"] and "resolve_native_references" in split["required_checks"],
           "Split archive was offered as a complete download")
    bad_directory = directory / "bad-hash"
    try:
        download_artifact({**plan["artifacts"]["file"], "sha256": "0" * 64}, bad_directory, port)
    except ValueError as exc:
        expect("SHA-256 header differs" in str(exc), "Hash rejection did not compare the HTTP header")
    else:
        raise RuntimeError("The client accepted an unexpected download hash")
    expect(not any(bad_directory.iterdir()), "Hash failure left a file on disk")
    for row in results.values():
        expect(row["fit_to_target_verified"] is False, "A candidate incorrectly claims verified assembly fit")
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "passed": True, "port": port,
            "cases": results, "negative_checks": {"unknown_model": True,
                "split_archive_not_offered_as_package": True, "mismatched_hash_rejected": True}}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, help="Use an already running service on this port.")
    args = parser.parse_args()
    root = args.root.resolve()
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    server = None
    try:
        port = args.port
        if port is None:
            server = make_server(root, 0)
            port = server.server_port
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
        with TemporaryDirectory(prefix="reuse-acceptance-", dir=runtime) as directory:
            result = evaluate(port, Path(directory))
        write_json(root / "reports/AI_REUSE_ACCEPTANCE.json", result)
        print(json.dumps({"passed": True, "cases": list(result["cases"]),
                          "negative_checks": result["negative_checks"]}, ensure_ascii=False, indent=2))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
