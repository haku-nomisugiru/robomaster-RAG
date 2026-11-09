"""Local catalog builder/query baseline for phase 1; no HTTP or CAD mutation."""
from __future__ import annotations
import argparse,collections,hashlib,json,re,sqlite3,sys
from collections_schema import SECTIONS, classify, export
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(path,default=None):
    if not path.exists():return default
    return json.loads(path.read_text(encoding="utf-8-sig"))
CATEGORY_ALIASES={"wheel_leg":"轮腿 串联腿", "swerve":"舵轮", "robotic_arm":"机械臂", "gearbox":"减速箱", "suspension":"悬挂", "chassis":"底盘", "lifting":"抬升", "gimbal":"云台", "gripper":"抓取", "wheel":"轮组"}

def tokens(text):
    text=text.lower()
    words=re.findall(r"[a-z0-9_]+",text)
    for run in re.findall(r"[\u3400-\u9fff]+",text):
        words.extend(run)
        words.extend(run[i:i+2] for i in range(len(run)-1))
    return list(dict.fromkeys(words))
def connect(root,read_only=False):
    path=(root/"catalog"/"library.sqlite3").resolve()
    db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) if read_only else sqlite3.connect(path)
    db.row_factory=sqlite3.Row;return db
def index_catalog(root):
    modules_path = root / "catalog" / "modules.jsonl"
    modules = [
        json.loads(line)
        for line in modules_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sources = load(root / "catalog" / "sources.json", [])
    inventory = load(root / "catalog" / "inventory.json", [])
    write_index(root, modules, sources, inventory)
    print(f"Indexed {len(modules)} module cards")


def write_index(root, modules, sources, inventory):
    db=connect(root)
    db.executescript("DROP TABLE IF EXISTS modules_fts; DROP TABLE IF EXISTS modules; DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS sources; CREATE TABLE sources(id TEXT PRIMARY KEY,json TEXT NOT NULL); CREATE TABLE files(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,source_id TEXT NOT NULL,json TEXT NOT NULL); CREATE TABLE modules(id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,source_id TEXT NOT NULL,json TEXT NOT NULL); CREATE VIRTUAL TABLE modules_fts USING fts5(id UNINDEXED,terms,tokenize='unicode61');")
    for s in sources:db.execute("INSERT INTO sources VALUES (?,?)",(s["source_id"],json.dumps(s,ensure_ascii=False)))
    for f in inventory:db.execute("INSERT INTO files VALUES (?,?,?,?)",(f["path"],f["sha256"],f["source_id"],json.dumps(f,ensure_ascii=False)))
    for m in modules:
        db.execute("INSERT INTO modules VALUES (?,?,?,?,?)",(m["id"],m["name"],m["category"],m["source_id"],json.dumps(m,ensure_ascii=False)))
        text=" ".join([m["name"],m["category"],CATEGORY_ALIASES.get(m["category"],""),m["summary"],m["team"]," ".join(x["text"] for x in m["features"])])
        db.execute("INSERT INTO modules_fts VALUES (?,?)",(m["id"]," ".join(tokens(text))))
    db.commit();db.close()

def build(root):
    sources=load(root/"catalog"/"sources.json",[])
    inventory=load(root/"catalog"/"inventory.json",[])
    picks=load(root/"catalog"/"module-selections.json",[])
    step_path=root/"reports"/"step-inspection.jsonl"
    steps={}
    if step_path.exists():
        for line in step_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row=json.loads(line);steps[row.get("path") or row.get("input_path") or row.get("file_path") or row.get("file")]=row
    native_path=root/"reports"/"native-dependencies.jsonl"
    natives={row["path"]:row for row in (json.loads(line) for line in native_path.read_text(encoding="utf-8").splitlines() if line.strip())} if native_path.exists() else {}
    by_path={f["path"]:f for f in inventory}
    by_source={s["source_id"]:s for s in sources}
    modules=[];seen=set()
    for pick in picks:
        source=by_source[pick["source_id"]];entry=by_path.get(pick["entry_path"])
        if not entry:raise ValueError(f"Selected entry missing from inventory: {pick['entry_path']}")
        file=root/entry["path"]
        if not file.is_file():raise ValueError(f"Selected CAD entry does not exist: {file}")
        if pick["id"] in seen:raise ValueError("Duplicate module identity")
        seen.add(pick["id"])
        scope=pick.get("scope","file")
        evidence=[{"type":"source_snapshot","snapshot_path":source.get("body_path"),"snapshot_kind":source.get("snapshot_kind","forum_article_snapshot"),"source_id":source["source_id"],"url":source["url"],"snapshot_sha256":source["page_sha256"]},
                  {"type":"cad_file_inventory","path":entry["path"],"sha256":entry["sha256"],"bytes":entry["bytes"]}]
        step=steps.get(entry["path"])
        if step and step.get("sha256")!=entry["sha256"]: raise ValueError(f"Stale geometry report for {entry['path']}")
        if scope!="file" and step:
            defs={d["definition_id"]:d for d in step.get("definitions",[])}
            if pick.get("definition_id") not in defs: raise ValueError("Selected STEP definition does not exist")
            if pick.get("occurrence_path") and pick["occurrence_path"] not in {o["occurrence_id"] for o in step.get("occurrences",[])}: raise ValueError("Selected STEP occurrence does not exist")
        module={"schema_version":"0.2","id":pick["id"],"name":pick["name"],"source_id":source["source_id"],
                "source_url":source["url"],"source_title":source["title"],"team":source["seed"]["team"],
                "source_published_date":source["seed"].get("published_date"),"category":pick["category"],
                "summary":pick["summary"],"entry":{"path":entry["path"],"sha256":entry["sha256"],"extension":entry["extension"],"asset_id":entry["asset_id"]},
                "selection_scope":scope,"definition_id":pick.get("definition_id"),"occurrence_path":pick.get("occurrence_path"),
                "features":[{"text":v,"evidence_type":"author_statement","source_id":source["source_id"]} for v in pick.get("source_features",[])],
                "limitations":list(dict.fromkeys(source["seed"].get("caveats",[])+pick.get("limitations",[]))),
                "evidence":evidence,"geometry_report":str(step_path.relative_to(root)).replace("\\","/") if step else None,
                "native_configurations":{"status":"not_inspected","values":None},
                "interfaces":{"status":"not_measured","mounting_faces":None,"hole_patterns":None,"axes":None},
                "dependency_scope":{"asset_id":entry["asset_id"],"archive_path":entry["archive_path"],"resolution_status":"not_validated" if entry["extension"] in {".sldasm",".sldprt"} else "self_contained_exchange_file"},
                "verification":{"file_present":True,"file_hash_recorded":True,"cad_parse_status":step.get("status") if step else "not_run","native_mates_verified":False,"motion_verified":False,"fit_to_target_verified":False,"material_mass_verified":False},
                "curation":{"method":"assistant_review_of_source_and_cad_inventory","functional_boundary_status":"draft_for_engineering_review"},
                "rights":{"source_notice":source["seed"].get("notice"),"public_redistribution":"not_cleared","original_license_applies":True}}
        native=natives.get(entry["path"])
        if native and native.get("sha256")==entry["sha256"]:
            module["native_dependency_report"]=native_path.relative_to(root).as_posix()
            module["dependency_scope"]["saved_reference_read_status"]=native["status"]
            module["dependency_scope"]["reference_counts"]=native.get("reference_counts",{})
        if step and step.get("status")=="ok":
            module["geometry_summary"]={"units":step.get("units"),"truncated":step.get("truncated",False),"definition_count":step.get("definition_count"),"occurrence_count":step.get("occurrence_count"),"coordinate_scope":"definition_local_mm" if scope!="file" else "file_world_mm", "metrics":defs[pick["definition_id"]].get("geometry_definition_mm") if scope!="file" else step.get("geometry_world_mm")}
        if scope!="file":
            module["limitations"].append("子模块按原文件内定义定位；当前交付保留完整交换文件，未导出独立子装配。")
        classify(module)
        modules.append(module)
    out=root/"catalog"/"modules.jsonl";out.write_text("".join(json.dumps(m,ensure_ascii=False)+"\n" for m in modules),encoding="utf-8")
    write_index(root, modules, sources, inventory)
    summary={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"sources":len(sources),"sources_with_cad":len({f["source_id"] for f in inventory if f["content_type"]=="cad"}),"downloaded_assets":len([a for a in load(root/"catalog"/"downloads.json",[]) if a["status"]=="downloaded"]),"files":len(inventory),"cad_files":sum(f["content_type"]=="cad" for f in inventory),"modules":len(modules),"primary_sections":dict(collections.Counter(m["section"] for m in modules if m["section"])),"supplementary_modules":sum(m["section"] is None for m in modules),"module_categories":dict(collections.Counter(m["category"] for m in modules)),"module_boundary_review":"assistant_curated_drafts","http_service":"not_implemented_in_phase_1","retrieval":"local_lexical_baseline"}
    (root/"reports"/"library-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# 首批机构样本目录","","> 本目录可检索真实CAD；原生配合、安装接口及运动验证状态逐条保留，不视为已完成。","",f"机构卡片：{len(modules)}；CAD文件：{summary['cad_files']}；来源：{summary['sources_with_cad']}。","","| ID | 机构 | 类别 | CAD类型 | 原帖 |","|---|---|---|---|---|"]
    lines.extend(f"| {m['id']} | {m['name']} | {m['category']} | {m['entry']['extension']} | [原帖]({m['source_url']}) |" for m in modules)
    lines.extend(["","## 重要边界","","- 作者参数与本地几何数据分别存储；没有根据默认材料推算机构质量。","- 公开论坛附件可匿名读取，不代表模型获得统一再分发许可；原始资产默认留在队内样本库。","- 同一源文件内的子模块保留definition/occurrence定位，不复制成虚假的独立可运动装配。","- 查询是第一阶段的词法检索基线，HTTP服务与语义重排在后续阶段接入。",""])
    (root/"reports"/"SAMPLE_CATALOG.md").write_text("\n".join(lines),encoding="utf-8")
    export(root,modules)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
def search(root,query,limit=5,section=None):
    if section is not None and section not in SECTIONS:raise ValueError("Unknown section")
    ts=tokens(query)
    if not ts:return []
    db=connect(root,read_only=True)
    fts=" OR ".join('"'+t.replace('"','""')+'"' for t in ts)
    rows=db.execute("SELECT m.json,bm25(modules_fts) rank FROM modules_fts JOIN modules m ON m.id=modules_fts.id WHERE modules_fts MATCH ? AND (? IS NULL OR json_extract(m.json, '$.section') = ?) ORDER BY rank",(fts,section,section)).fetchall();db.close()
    scored=[]
    for r in rows:
        m=json.loads(r["json"]);title_tokens=set(tokens(m["name"]));body_tokens=set(tokens(m["summary"]+" "+m["category"]+" "+CATEGORY_ALIASES.get(m["category"],"")))
        score=sum(4 if t in title_tokens else 1 for t in ts if t in title_tokens or t in body_tokens)
        if query.lower() in m["name"].lower():score+=20
        if query.lower() in CATEGORY_ALIASES.get(m["category"],"").split():score+=20
        scored.append((score,m))
    scored.sort(key=lambda v:(-v[0],v[1]["id"]))
    return [{"id":m["id"],"name":m["name"],"category":m["category"],"section":m["section"],"subtype":m["subtype"],"score":score,"source_url":m["source_url"],"entry":m["entry"],"verification":m["verification"],"limitations":m["limitations"]} for score,m in scored[:limit]]
def validate(root):
    errors=[];mods=[json.loads(x) for x in (root/"catalog"/"modules.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    inventory=load(root/"catalog"/"inventory.json",[]);by_path={f["path"]:f for f in inventory}
    for m in mods:
        f=by_path.get(m["entry"]["path"])
        if not f:errors.append(m["id"]+": entry missing from inventory");continue
        p=(root/f["path"]).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():errors.append(m["id"]+": missing or unsafe path");continue
        h=hashlib.sha256()
        with p.open("rb") as stream:
            for block in iter(lambda:stream.read(4*1024*1024),b""):h.update(block)
        if h.hexdigest()!=f["sha256"]:errors.append(m["id"]+": content hash changed")
        if not m["evidence"] or not m["source_url"].startswith("https://bbs.robomaster.com/article/"):errors.append(m["id"]+": missing source evidence")
    report={"modules_checked":len(mods),"errors":errors,"passed":not errors}
    (root/"reports"/"catalog-validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if errors:raise SystemExit(1)
def main():
    p=argparse.ArgumentParser();p.add_argument("--root",type=Path,default=ROOT);sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("build");sub.add_parser("index");sub.add_parser("validate");s=sub.add_parser("search");s.add_argument("query");s.add_argument("--limit",type=int,default=5);s.add_argument("--section",choices=list(SECTIONS))
    a=p.parse_args();root=a.root.resolve()
    if a.action=="build":build(root)
    elif a.action=="index":index_catalog(root)
    elif a.action=="validate":validate(root)
    else: print(json.dumps(search(root,a.query,a.limit,a.section),ensure_ascii=False,indent=2))
if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8");main()
