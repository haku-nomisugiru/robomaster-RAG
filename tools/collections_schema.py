"""Three functional collections; source CAD files remain shared and immutable."""
import json
from urllib.parse import quote

SECTIONS = {
    "gimbal": {"label": "云台", "categories": ["gimbal"], "parameters": ["axis_count", "yaw_range_deg", "pitch_range_deg", "payload_kg", "base_mount", "payload_mount", "drive_model", "slip_ring"]},
    "chassis": {"label": "底盘", "categories": ["chassis"], "parameters": ["drive_layout", "wheelbase_mm", "track_width_mm", "ground_clearance_mm", "envelope_mm", "wheel_module_mount", "gimbal_mount", "suspension_travel_mm"]},
    "wheel_or_leg": {"label": "轮子或轮腿", "categories": ["wheel", "swerve", "wheel_leg", "suspension"], "parameters": ["wheel_diameter_mm", "wheel_width_mm", "steering_range_deg", "leg_link_lengths_mm", "suspension_travel_mm", "drive_model", "gear_ratio", "chassis_mount"]},
}

def classify(module):
    section = next((key for key, spec in SECTIONS.items() if module["category"] in spec["categories"]), None)
    module["section"] = section
    module["section_label"] = SECTIONS[section]["label"] if section else None
    module["collection_status"] = "primary" if section else "supplementary"
    module["subtype"] = module["category"]
    if section == "gimbal":
        name = module["name"].lower()
        module["subtype"] = "yaw_axis" if "yaw" in name else "pitch_axis" if "pitch" in name else "gimbal_assembly"
    module["engineering_parameters"] = {key: {"value": None, "status": "not_verified", "evidence": []} for key in SECTIONS[section]["parameters"]} if section else {}
    module["classification_evidence"] = {"basis": "curated_category_and_file_name", "status": "draft_for_engineering_review"}

def export(root, modules):
    destination = root / "catalog" / "sections"
    destination.mkdir(exist_ok=True)
    taxonomy = {"schema_version": "1.0", "sections": SECTIONS, "policy": "Each primary card has one functional section. Suspension belongs to wheel_or_leg. Generic gearboxes and manipulators remain supplementary; original CAD paths and IDs stay unchanged."}
    (root / "catalog" / "taxonomy.json").write_text(json.dumps(taxonomy, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 三部分机构样本库", "", "主库按云台、底盘、轮子或轮腿组织。参数空值表示未验证；分类依据文件名和已有来源，需工程复核。", ""]
    for section, spec in SECTIONS.items():
        selected = [m for m in modules if m["section"] == section]
        (destination / (section + ".jsonl")).write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in selected), encoding="utf-8")
        part = ["# " + spec["label"], "", f"机构卡片 {len(selected)} 张；不同 CAD 入口 {len({m['entry']['path'] for m in selected})} 个。", "", "| 机构 | 子类型 | 定位范围 | CAD入口 | 来源 |", "|---|---|---|---|---|"]
        part += [f"| {m['name']} | {m['subtype']} | {m['selection_scope']} | [{m['entry']['path']}]({(root / m['entry']['path']).as_posix()}) | [原帖]({m['source_url']}) |" for m in selected]
        part += ["", "## 工程参数与验证", "", ", ".join(spec["parameters"]), "",
                 "分类参数以卡片的验证状态为准。可筛选规格见对应组件卡，安装面和孔组量测见接口证据。"
                 "配合、运动与适配状态以各卡片为准。STEP 子定义仍引用完整源文件。", ""]
        (root / "reports" / ("SECTION_" + section + ".md")).write_text("\n".join(part), encoding="utf-8")
        lines += [f"## {spec['label']}（{len(selected)} 张）", "", f"[分类目录](SECTION_{section}.md) · [结构化卡片](../catalog/sections/{section}.jsonl)", ""]
    extra = [m for m in modules if m["section"] is None]
    (destination / "supplementary.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in extra), encoding="utf-8")
    lines += [f"## 补充资料（{len(extra)} 张）", "", "机械臂、抬升、抓取、通用减速箱和其他零组件保留在 supplementary.jsonl。", "", "所有分类共享原CAD文件，详细卡片保留来源、指纹和验证证据。", ""]
    profiles = [module for module in modules if module.get("retrieval_profile") and module.get("card_tier")!="component"]
    if profiles:
        lines += ["## 面向检索的机构卡片", "", "按模块整理原帖、作者附件和 CAD 证据。PDF 页码按文件页序计算。", ""]
        lines += [f"当前共 {len(modules)} 张卡片。此处展开 {len(profiles)} 张人工整理卡；"
                  "自动拆分卡片按文件或 STEP 定义定位，可通过检索和 [完整卡片](../catalog/modules.jsonl) 查看。",""]
    for module in profiles:
        profile = module["retrieval_profile"]
        lines += [
            f"### {module['name']} · {module['id']}", "",
            module["summary"], "",
            "**检索词**　" + "、".join(profile["aliases"] + profile["keywords"]), "",
        ]
        for fact in profile["facts"]:
            evidence = fact["evidence"]
            source_path = quote(evidence["path"])
            lines.append(f"- {fact['text']} [{evidence['locator']}](../{source_path}) · [原帖]({evidence['url']})")
        if profile["reuse_checks"]:
            lines += ["", "**复用前核验**　" + " ".join(profile["reuse_checks"])]
        lines.append("")
    (root / "reports" / "SAMPLE_CATALOG.md").write_text("\n".join(lines), encoding="utf-8")
