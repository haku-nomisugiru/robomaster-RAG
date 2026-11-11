"""Version-bound mechanical feature claims and CAD observations for searchable components."""

from collections import Counter, defaultdict
from pathlib import PurePosixPath

from component_catalog import occurrence_rows
from retrieval import resolve_fact_evidence


CATEGORIES = {
    "chassis": "底盘", "suspension": "悬挂", "wheel_leg": "串联腿与轮腿",
    "swerve": "舵轮", "robotic_arm": "机械臂", "gripper": "抓取机构", "gearbox": "减速箱",
}
CLAIM_KINDS = {"structure", "principle", "advantage", "tradeoff", "application"}


def observed_children(modules, steps, native):
    """Keep STEP parent labels and SolidWorks observed configurations as separate contexts."""
    observations = defaultdict(dict)
    for root, target, config, kind, occurrence in occurrence_rows(modules, steps, native):
        observations[root["id"], config, kind][occurrence["occurrence_id"]] = (
            target, occurrence["parent_occurrence_id"], occurrence["name_path"])
    by_card = {module["id"]: module for module in modules}
    contexts = defaultdict(list)
    for (root_id, config, kind), occurrences in observations.items():
        by_parent = defaultdict(Counter)
        for target, parent_id, name_path in occurrences.values():
            if not target:
                continue
            if parent_id:
                parent = occurrences.get(parent_id, (None,))[0]
            elif kind == "solidworks_live" and len(name_path) == 1:
                parent = by_card[root_id]
            else:
                continue
            if parent and parent["id"] != target["id"]:
                by_parent[parent["id"]][target["id"]] += 1
        for mid, children in by_parent.items():
            contexts[mid].append({
                "root_card_id": root_id, "configuration": config, "evidence_type": kind,
                "report": "reports/step-inspection.jsonl" if kind == "step_xcaf" else "reports/native-occurrences.jsonl",
                "children": [{"card_id": cid, "name": by_card[cid]["name"], "observed_count": count}
                             for cid, count in sorted(children.items(), key=lambda pair: (-pair[1], pair[0]))],
            })
    return contexts


def attach_mechanism_profiles(modules, declarations, documents, steps, native):
    by_card = {module["id"]: module for module in modules}
    if unknown := declarations.keys() - by_card.keys():
        raise ValueError(f"Mechanical profiles have unknown card IDs: {sorted(unknown)}")
    contexts = observed_children(modules, steps, native)
    for mid, module in by_card.items():
        declaration = declarations.get(mid, {})
        active = bool(declaration) and declaration["cad_sha256"] == module["entry"]["sha256"]
        groups = list(dict.fromkeys([
            *([module["category"]] if module["category"] in CATEGORIES else []),
            *(declaration.get("groups", []) if active else []),
        ]))
        if not groups and not declaration:
            continue
        if set(groups) - CATEGORIES.keys():
            raise ValueError(f"Invalid mechanical category on {mid}")
        profile = {
            "categories": groups, "search_terms": declaration.get("search_terms", []) if active else [],
            "claims": [], "cad_observations": [], "assembly_contexts": contexts.get(mid, []),
            "review_state": "cad_only",
        }
        locator = module.get("component_locator", {})
        references = []
        for link in locator.get("saved_reference_links", []):
            candidates = [{"card_id": cid, "name": by_card[cid]["name"]}
                          for cid in link["candidate_card_ids"] if cid in by_card]
            if candidates:
                references.append({"status": link["status"], "candidates": candidates})
        if references:
            profile["saved_reference_candidates"] = {
                "report": module["native_dependency_report"], "links": references,
            }
        if declaration and not active:
            profile["review_state"] = "source_review_stale"
        for claim in declaration.get("claims", []) if active else ():
            if claim["kind"] not in CLAIM_KINDS or not claim["text"].strip() or not claim["locator"].strip():
                raise ValueError(f"Invalid mechanical claim on {mid}")
            evidence = resolve_fact_evidence(module, claim, documents)
            if claim["kind"] == "advantage" and evidence["type"] not in {"author_pdf", "forum_article_body"}:
                raise ValueError(f"Mechanical advantage requires author evidence on {mid}")
            profile["claims"].append({"kind": claim["kind"], "text": claim["text"], "evidence": evidence,
                                      "scope": claim.get("scope", "source_design")})
            if evidence["type"] in {"author_pdf", "forum_article_body"}:
                profile["review_state"] = "source_reviewed"
        source = module["cad_source"]
        extension = source["extension"]
        if source.get("definition_kind"):
            object_type = "STEP 子装配" if source["definition_kind"] == "assembly" else "STEP 零件定义"
        else:
            object_type = {".sldasm": "SolidWorks 装配", ".sldprt": "SolidWorks 零件",
                           ".step": "STEP 源文件", ".stp": "STEP 源文件"}.get(extension, "CAD 文件")
        file_name = source.get("definition_name") or PurePosixPath(source["path"]).name
        profile["cad_observations"].append({
            "kind": "cad_locator", "text": f"{object_type}「{file_name}」",
            "evidence": {"path": source["path"], "sha256": source["sha256"],
                         "definition_id": source["definition_id"], "source_url": source["source_url"]},
        })
        for item in module["interfaces"].get("measurements", []):
            count = len(item["geometry"]["hole_pattern"]["openings"])
            profile["cad_observations"].append({
                "kind": "interface_measurement", "text": f"{item['label']}，选定面有 {count} 个已量测圆形开口",
                "evidence": {"path": item["record_path"], "sha256": source["sha256"], "interface_id": item["id"]},
            })
        for item in module["interfaces"].get("component_measurements", []):
            profile["cad_observations"].append({
                "kind": "component_interface_measurement", "text": item["label"],
                "evidence": {"path": item["record_path"], "component_card_id": item["component_card_id"],
                             "interface_id": item["id"], "occurrence_id": item["occurrence_id"]},
            })
        module["mechanism_profile"] = profile
