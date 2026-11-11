"""Generate evidence-bounded cards for individually addressable CAD components."""

from collections import defaultdict
import hashlib
from pathlib import PurePosixPath
import re

from component_scope import RESTRICTED_NAME, excluded_component


# Categories describe what the locator says. They do not certify function or fit.
CATEGORIES = (
    ("suspension", r"悬挂|避震|减震|气弹簧|suspension|shock[_ -]?absorber", ["悬挂", "减振"]),
    ("swerve", r"舵轮|swerve", ["舵轮", "转向轮组"]),
    ("wheel_leg", r"串联腿|轮腿|髋关节|膝关节|大腿|小腿|wheel[_ -]?leg", ["轮腿"]),
    ("chassis", r"底盘|车架|chassis|frame", ["车架"]),
    ("gearbox", r"减速箱|减速器|齿轮箱|gearbox|reducer", ["减速机构"]),
    ("lifting", r"抬升|升降|龙门架|elevator|lift", ["升降机构"]),
    ("gripper", r"抓取|夹爪|gripper", ["末端抓取"]),
    ("robotic_arm", r"机械臂|大臂|小臂|robotic[_ -]?arm", ["机械臂"]),
    ("gimbal", r"云台|(?<![a-z])(?:yaw|pitch)(?![a-z])", ["回转支架"]),
    ("wheel", r"轮组|轮胎|轮毂|全向轮|麦轮|(?<![a-z])wheel(?![a-z])", ["轮组"]),
    ("bearing", r"轴承|bearing", ["支承"]),
    ("fastener", r"螺[钉栓丝母]|垫圈|screw|bolt|washer|standoff", ["紧固件"]),
    ("motor", r"电机|(?<![a-z])motor(?![a-z])", ["驱动电机"]),
    ("gear", r"齿轮|链轮|同步轮|(?<![a-z])gear(?![a-z])", ["传动件"]),
    ("bracket", r"支架|固定板|安装板|法兰|转接板|bracket|flange", ["安装件"]),
)
RULES = [(category, re.compile(pattern, re.IGNORECASE), keywords) for category, pattern, keywords in CATEGORIES]
KINDS = {
    ".sldprt": ("native_part", "SolidWorks 原生零件"),
    ".sldasm": ("native_assembly", "SolidWorks 原生装配"),
    ".slddrw": ("drawing", "SolidWorks 工程图"),
    ".easm": ("preview", "eDrawings 装配预览"),
    ".eprt": ("preview", "eDrawings 零件预览"),
    ".step": ("exchange_file", "STEP 交换模型"),
    ".stp": ("exchange_file", "STEP 交换模型"),
}


def searchable_row(row):
    relative = "/".join(PurePosixPath(row["path"]).parts[3:])
    return (row["content_type"] == "cad" and row["bytes"] > 0 and not row["name"].startswith("~$")
            and not excluded_component(row.get("thread_id"), relative))


def address(pick):
    return pick["entry_path"], pick.get("definition_id"), pick.get("occurrence_path")


def name_for_definition(definition):
    label = str(definition.get("name") or "").strip()
    if label.lower().endswith((".step", ".stp", ".sldprt", ".sldasm")):
        label = PurePosixPath(label).stem
    return label or f"STEP 定义 {definition['definition_id']}"


def indexable_reports(steps, inventory):
    """Identify exchange files containing an out-of-scope subassembly."""
    blocked = set()
    for path, report in steps.items():
        row = inventory.get(path)
        if not row or report.get("sha256") != row["sha256"]:
            continue
        if any(excluded_component(row.get("thread_id"), str(node.get("name") or ""))
               for node in report.get("definitions", [])):
            blocked.add(path)
    return blocked


def make_pick(row, definition=None):
    path = PurePosixPath(row["path"])
    definition_id = definition["definition_id"] if definition else None
    name = name_for_definition(definition) if definition else path.stem.strip()
    kind, kind_label = KINDS.get(row["extension"], ("file", row["extension"].lstrip(".").upper() + " 文件"))
    if definition:
        kind = "step_assembly" if definition["kind"] == "assembly" else "step_shape"
        kind_label = "STEP 子装配" if kind == "step_assembly" else "STEP 几何定义"
    category, keywords = next(((category, keywords) for category, pattern, keywords in RULES if pattern.search(name)),
                              ("component", []))
    key = "\0".join((row["path"].casefold(), definition_id or ""))
    pick = {
        "id": f"rm-{row['thread_id']}-c{hashlib.sha256(key.encode()).hexdigest()[:16]}",
        "name": name, "entry_path": row["path"], "source_id": row["source_id"],
        "category": category, "scope": "definition" if definition else "file",
        "definition_id": definition_id,
        "summary": f"{kind_label}「{name}」。按{'STEP 定义标签' if definition else '包内路径'}定位。",
        "auto": {"kind": kind, "label": kind_label, "keywords": keywords},
    }
    return pick


def expand_picks(picks, inventory, steps):
    by_path = {row["path"]: row for row in inventory}
    blocked = indexable_reports(steps, by_path)
    selected = [pick for pick in picks
                if pick["entry_path"] not in blocked
                and (pick["entry_path"] not in by_path or searchable_row(by_path[pick["entry_path"]]))]
    used = {address(pick) for pick in selected}
    for row in sorted(inventory, key=lambda row: row["path"]):
        path = row["path"]
        if not searchable_row(row) or path in blocked:
            continue
        if (path, None, None) not in used:
            selected.append(make_pick(row))
        report = steps.get(path)
        if (not report or report.get("sha256") != row["sha256"]
                or report.get("status") not in {"ok", "partial"}):
            continue
        # The file card already represents each free root; child definitions need their own locator.
        roots = set(report.get("root_definition_ids", []))
        definitions = report.get("definitions", [])
        if not roots and len(definitions) == 1:
            roots = {definitions[0]["definition_id"]}
        for definition in definitions:
            definition_id = definition["definition_id"]
            if not (len(roots) == 1 and definition_id in roots) and (path, definition_id, None) not in used:
                selected.append(make_pick(row, definition))
    return selected, blocked


def occurrence_lookup(report):
    groups = defaultdict(list)
    for occurrence in report.get("occurrences", []):
        groups[occurrence["definition_id"]].append(occurrence)
    return groups


def add_locator(module, pick, source, step, definition, occurrences, native):
    entry = module["entry"]
    locator = {"kind": "step_definition" if module["definition_id"] else "cad_file",
               "path": entry["path"], "sha256": entry["sha256"], "asset_id": entry["asset_id"]}
    if module["definition_id"]:
        locator["definition_id"] = module["definition_id"]
        locator["report"] = module["geometry_report"]
        locator["occurrence_count"] = len(occurrences)
        locator["occurrence_count_scope"] = "parsed_subset" if step.get("truncated") else "report"
        locator["occurrence_examples"] = [
            {"id": row["occurrence_id"], "parent_occurrence_id": row.get("parent_occurrence_id"),
             "name_path": row["name_path"],
             "world_transform_mm": row.get("world_transform_mm")} for row in occurrences[:5]
        ]
    if step:
        locator["parse_status"] = step["status"]
        locator["parser"] = step.get("parser", {"engine": "OCP/XCAF", "report_schema": step.get("schema_version")})
        if definition:
            locator["child_definition_ids"] = definition.get("child_definition_ids", [])
        elif module["selection_scope"] == "file":
            locator["root_definition_ids"] = step.get("root_definition_ids", [])
            locator["definition_count"] = step.get("definition_count")
            locator["occurrence_count"] = step.get("occurrence_count")
            locator["occurrence_count_scope"] = "parsed_subset" if step.get("truncated") else "report"
    if entry["extension"] == ".sldasm":
        locator["native_tree_status"] = "saved_references_only" if native else "file_only"
        if native:
            locator["native_report"] = module["native_dependency_report"]
            locator["native_reference_counts"] = native.get("reference_counts", {})
            locator["file_available"] = not any(
                RESTRICTED_NAME.search(ref.get("original_name", ""))
                for ref in native.get("references", [])
            )
    module["component_locator"] = locator
    module["card_tier"] = "curated" if not pick.get("auto") else "component"
    module["source_context"] = {"title": source["title"], "url": source["url"]}


def add_component_profile(module, pick, definitions):
    auto = pick["auto"]
    locator = module["component_locator"]
    entry = module["entry"]
    evidence = {"url": module["source_url"], "type": "cad_inventory", "path": entry["path"],
                "sha256": entry["sha256"], "locator": "包内 CAD 路径"}
    facts = [{"kind": "locator", "text": module["summary"], "evidence": evidence}]
    if module["definition_id"]:
        definition = definitions[module["definition_id"]]
        step_evidence = {"url": module["source_url"], "type": "step_inspection",
                         "path": module["geometry_report"], "cad_sha256": entry["sha256"],
                         "definition_id": module["definition_id"],
                         "locator": f"STEP 定义 {module['definition_id']}"}
        facts[0]["evidence"] = step_evidence
        dims = (definition.get("geometry_definition_mm") or {}).get("aabb_mm", {}).get("size")
        if dims:
            formatted = " × ".join(f"{size:.2f}" for size in dims)
            facts.append({"kind": "static_geometry", "text": f"定义坐标下静态包围盒为 {formatted} mm。",
                          "evidence": step_evidence})
        reuse = "核对原 STEP 中的实例、位姿和安装接口。"
    elif auto["kind"] == "native_assembly":
        reuse = "按包内路径打开原生装配，核对配置、外部引用和安装接口。"
    elif auto["kind"] in {"drawing", "preview"}:
        reuse = "根据关联装配或零件确认尺寸与接口。"
    else:
        reuse = "打开对应 CAD 后核对尺寸、材料及实际安装接口。"
    path = PurePosixPath(entry["path"])
    parent = path.parent.name if len(path.parts) > 4 else ""
    if parent and not RESTRICTED_NAME.search(parent):
        module["source_context"]["directory"] = parent
    module["retrieval_profile"] = {"aliases": [], "keywords": auto["keywords"],
                                   "facts": facts, "reuse_checks": [reuse]}
    module["curation"] = {"method": "generated_from_cad_locator", "functional_boundary_status": "not_reviewed"}
    module["classification_evidence"] = {"basis": "cad_file_or_step_label", "status": "inferred"}
    module["component_kind"] = auto["kind"]
    if auto["kind"] in {"drawing", "preview"}:
        locator["view_only"] = True


def link_saved_references(modules, native_reports):
    by_path = {m["entry"]["path"]: m for m in modules if m["selection_scope"] == "file"}
    parents = defaultdict(dict)
    for assembly in by_path.values():
        locator = assembly["component_locator"]
        if locator.get("native_tree_status") != "saved_references_only":
            continue
        linked = []
        for reference in native_reports[assembly["entry"]["path"]].get("references", []):
            paths = [reference["resolved_local_path"]] if reference.get("resolved_local_path") else reference.get("potential_local_paths", [])
            targets = [by_path[path] for path in paths if path in by_path
                       and by_path[path]["entry"]["asset_id"] == assembly["entry"]["asset_id"]]
            if not targets:
                continue
            status = reference["match_status"]
            linked.append({"status": status, "candidate_card_ids": [target["id"] for target in targets]})
            for target in targets:
                parents[target["id"]][assembly["id"]] = status
        locator["saved_reference_links"] = linked[:40]
        locator["saved_reference_link_count"] = len(linked)
    for module in by_path.values():
        links = parents.get(module["id"])
        if links:
            locator = module["component_locator"]
            locator["referenced_by_count"] = len(links)
            locator["referenced_by"] = [{"card_id": mid, "status": status}
                                        for mid,status in list(links.items())[:12]]
