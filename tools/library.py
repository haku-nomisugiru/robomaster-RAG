"""Local catalog builder/query baseline for phase 1; no HTTP or CAD mutation."""
from __future__ import annotations
import argparse,collections,hashlib,json,sqlite3,sys
from collections_schema import SECTIONS, classify, export
from component_catalog import CREATE_SQL, DROP_SQL, assign_component_ids, index_components
from component_cards import add_component_profile, add_locator, expand_picks, link_saved_references, occurrence_lookup, searchable_row
from component_metadata import component_candidates, decorate_component, parse_spec
from contextlib import closing
from datetime import datetime,timezone
from interface_catalog import attach_interface_evidence, read_interface_evidence
from mechanism_features import CATEGORIES as MECHANISM_CATEGORIES, attach_mechanism_profiles
from pathlib import Path
from native_tree import REPORT as NATIVE_LIVE_REPORT, link_live_occurrences
from retrieval import (SEARCH_MODES, apply_profile, exact_model_candidates, functional_fields, identifiers,
                       model_records, normalize, prepare_documents, rank_hit, search_fields, tokens)
ROOT=Path(__file__).resolve().parents[1]
MECHANISM_MANIFEST=Path("catalog/mechanism-features.json")
def load(path,default=None):
    if not path.exists():return default
    return json.loads(path.read_text(encoding="utf-8-sig"))
def read_observations(path, keys=("path",)):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        return {next(row[key] for key in keys if row.get(key)): row
                for row in (json.loads(line) for line in stream if line.strip())}
def read_step_observations(path):
    return read_observations(path, ("path","input_path","file_path","file"))
def connect(root,read_only=False):
    path=(root/"catalog"/"library.sqlite3").resolve()
    db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) if read_only else sqlite3.connect(path)
    db.row_factory=sqlite3.Row;return db
def build(root):
    sources=load(root/"catalog"/"sources.json",[])
    inventory=load(root/"catalog"/"inventory.json",[])
    picks=load(root/"catalog"/"module-selections.json",[])
    notes=load(root/"catalog"/"retrieval-notes.json",{})
    mechanics=load(root/MECHANISM_MANIFEST,{})
    profiles=notes.get("modules",{})
    declarations=notes.get("model_identifiers",{})
    documents=prepare_documents(root,{**notes.get("documents",{}),**mechanics.get("documents",{})})
    step_path=root/"reports"/"step-inspection.jsonl"
    steps=read_step_observations(step_path)
    native_path=root/"reports"/"native-dependencies.jsonl"
    natives=read_observations(native_path)
    live=read_observations(root/NATIVE_LIVE_REPORT)
    by_path={f["path"]:f for f in inventory}
    by_source={s["source_id"]:s for s in sources}
    picks, blocked = expand_picks(picks, inventory, steps)
    if unknown := profiles.keys() - {pick["id"] for pick in picks}:
        raise ValueError(f"Retrieval profiles have no selected module: {sorted(unknown)}")
    if unknown := declarations.keys() - {pick["id"] for pick in picks}:
        raise ValueError(f"Model identifiers have no selected module: {sorted(unknown)}")
    definitions_by_path = {path:{d["definition_id"]:d for d in step.get("definitions", [])}
                           for path,step in steps.items()}
    occurrences_by_path = {path:occurrence_lookup(step) for path,step in steps.items()}
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
        if step and step.get("sha256")!=entry["sha256"]:
            step=None
        defs=definitions_by_path.get(entry["path"],{}) if step else {}
        if scope!="file":
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
        if native and native.get("sha256")==entry["sha256"] and native.get("status")=="snapshot_read":
            module["native_dependency_report"]=native_path.relative_to(root).as_posix()
            module["dependency_scope"]["saved_reference_read_status"]=native["status"]
            module["dependency_scope"]["reference_counts"]=native.get("reference_counts",{})
        else:
            native=None
        if step and step.get("status") in {"ok","partial"}:
            module["geometry_summary"]={"units":step.get("units"),"truncated":step.get("truncated",False),"definition_count":step.get("definition_count"),"occurrence_count":step.get("occurrence_count"),"coordinate_scope":"definition_local_mm" if scope!="file" else "file_world_mm", "metrics":defs[pick["definition_id"]].get("geometry_definition_mm") if scope!="file" else step.get("geometry_world_mm")}
        if scope!="file":
            module["limitations"].append("子模块按原文件内定义定位；当前交付保留完整交换文件，未导出独立子装配。")
        classify(module)
        occurrences=occurrences_by_path.get(entry["path"],{}).get(module["definition_id"],[])
        add_locator(module,pick,source,step,defs.get(module["definition_id"]),occurrences,native)
        if pick.get("auto"):
            add_component_profile(module,pick,defs)
        if profile:=profiles.get(module["id"]):
            automatic=module.get("retrieval_profile",{})
            apply_profile(module,profile,documents)
            if pick.get("auto"):
                merged=module["retrieval_profile"]
                merged["facts"].extend(automatic["facts"])
                merged["keywords"]=list(dict.fromkeys(merged["keywords"]+automatic["keywords"]))
                module["card_tier"]="source_reviewed"
        modules.append(module)
    link_saved_references(modules,natives)
    link_live_occurrences(modules,live)
    assign_component_ids(modules)
    interfaces=read_interface_evidence(root,modules,steps,live)
    attach_interface_evidence(modules,interfaces)
    for module in modules:
        path=module["entry"]["path"]
        definition=definitions_by_path.get(path,{}).get(module["definition_id"])
        decorate_component(module,by_source[module["source_id"]],by_path[path],definition)
    attach_mechanism_profiles(modules,mechanics.get("cards",{}),documents,steps,live)
    with closing(connect(root)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("BEGIN IMMEDIATE;\n" + DROP_SQL + """
            DROP TABLE IF EXISTS model_lookup;
            DROP TABLE IF EXISTS modules_fts;
            DROP TABLE IF EXISTS modules_function_fts;
            DROP TABLE IF EXISTS modules;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS sources;
            CREATE TABLE sources(id TEXT PRIMARY KEY,json TEXT NOT NULL);
            CREATE TABLE files(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,source_id TEXT NOT NULL,json TEXT NOT NULL);
            CREATE TABLE modules(id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,source_id TEXT NOT NULL,json TEXT NOT NULL);
            CREATE TABLE model_lookup(lookup_key TEXT NOT NULL,card_id TEXT NOT NULL REFERENCES modules(id),
                                      json TEXT NOT NULL,PRIMARY KEY(lookup_key,card_id));
            CREATE VIRTUAL TABLE modules_fts USING fts5(id UNINDEXED,identity,keywords,description,tokenize='unicode61');
            CREATE VIRTUAL TABLE modules_function_fts USING fts5(id UNINDEXED,identity,keywords,description,mechanism,tokenize='unicode61');
        """ + CREATE_SQL)
        for s in sources:db.execute("INSERT INTO sources VALUES (?,?)",(s["source_id"],json.dumps(s,ensure_ascii=False)))
        for f in inventory:db.execute("INSERT INTO files VALUES (?,?,?,?)",(f["path"],f["sha256"],f["source_id"],json.dumps(f,ensure_ascii=False)))
        for m in modules:
            db.execute("INSERT INTO modules VALUES (?,?,?,?,?)",(m["id"],m["name"],m["category"],m["source_id"],json.dumps(m,ensure_ascii=False)))
            fields=[" ".join(tokens(" ".join(values),index=True)) for values in search_fields(m).values()]
            db.execute("INSERT INTO modules_fts VALUES (?,?,?,?)",(m["id"],*fields))
            mechanism=" ".join(tokens(" ".join(functional_fields(m)["mechanism"]),index=True))
            db.execute("INSERT INTO modules_function_fts VALUES (?,?,?,?,?)",(m["id"],*fields,mechanism))
            db.executemany("INSERT INTO model_lookup VALUES (?,?,?)", (
                (key,m["id"],json.dumps(record,ensure_ascii=False))
                for key,record in model_records(m,declarations.get(m["id"],[]),documents).items()
            ))
        index_components(db,modules,steps,live,interfaces)
        component_counts={
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("components","component_occurrences","interface_openings",
                          "component_aliases","component_specs")
        }
        component_counts["components_with_specs"]=db.execute("SELECT count(DISTINCT component_id) FROM component_specs").fetchone()[0]
    out=root/"catalog"/"modules.jsonl"
    pending=out.with_suffix(".jsonl.part")
    pending.write_text("".join(json.dumps(m,ensure_ascii=False)+"\n" for m in modules),encoding="utf-8")
    pending.replace(out)
    cad_files = [row for row in inventory if row["content_type"] == "cad"]
    eligible = {row["path"] for row in cad_files if searchable_row(row) and row["path"] not in blocked}
    covered = {module["entry"]["path"] for module in modules if module["selection_scope"] == "file"}
    step_files = [module for module in modules if module["selection_scope"] == "file"
                  and module["entry"]["extension"] in {".step", ".stp"}]
    summary={
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "sources":len(sources),"sources_with_cad":len({module["source_id"] for module in modules}),
        "downloaded_assets":sum(a["status"]=="downloaded" for a in load(root/"catalog"/"downloads.json",[])),
        "files":len(inventory),"cad_files":len(cad_files),"indexable_cad_paths":len(eligible),
        "indexed_cad_paths":len(covered),"excluded_cad_paths":len(cad_files)-len(eligible),
        "unindexed_eligible_paths":sorted(eligible-covered),
        "modules":len(modules),"curated_cards":sum(m["card_tier"]!="component" for m in modules),
        **component_counts,
        "interface_evidence":{"active":sum(record["state"]=="active" for record in interfaces),
                              "stale":sum(record["state"]=="stale" for record in interfaces)},
        "step_definition_cards":sum(m["selection_scope"]=="definition" for m in modules),
        "step_file_parse_status":dict(collections.Counter(
            m["component_locator"].get("parse_status","not_run") for m in step_files
        )),
        "native_assembly_status":dict(collections.Counter(
            m["component_locator"]["native_tree_status"] for m in modules
            if m["entry"]["extension"]==".sldasm"
        )),
        "primary_sections":dict(collections.Counter(m["section"] for m in modules if m["section"])),
        "supplementary_modules":sum(m["section"] is None for m in modules),
        "module_categories":dict(collections.Counter(m["category"] for m in modules)),
        "mechanism_profiles":dict(collections.Counter(
            m["mechanism_profile"]["review_state"] for m in modules if "mechanism_profile" in m)),
        "retrieval":"fielded_fts5",
    }
    (root/"reports"/"library-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    export(root,modules)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
def result_content_key(module):
    entry=module["entry"]
    # Native assemblies may resolve the same saved references differently in another package.
    context=entry["path"] if entry["extension"] in {".sldasm",".asm",".iam"} else None
    return entry["sha256"],module.get("definition_id"),module.get("occurrence_path"),context

def search(root,query,limit=5,section=None,mode="auto",*,alias=None,specs=(),category=None):
    if section is not None and section not in SECTIONS:raise ValueError("Unknown section")
    if mode not in SEARCH_MODES:raise ValueError("Unknown search mode")
    if category is not None and category not in MECHANISM_CATEGORIES:raise ValueError("Unknown mechanism category")
    if limit<=0:return []
    ts=tokens(query)
    filters=[parse_spec(raw) for raw in specs]
    if not ts and (query.strip() or (alias is None and category is None and not filters) or mode=="model"):return []
    functional=mode=="function"
    model_matches={}
    with closing(connect(root,read_only=True)) as db:
        candidate_ids=component_candidates(db,alias,filters)
        if mode=="model":
            model_matches,ts=exact_model_candidates(db,query)
            candidate_ids=set(model_matches) if candidate_ids is None else candidate_ids & model_matches.keys()
        if candidate_ids is not None and not candidate_ids:return []
        candidates=json.dumps(sorted(candidate_ids)) if candidate_ids is not None else None
        conditions="""
            (? IS NULL OR json_extract(m.json, '$.section') = ?)
            AND (? IS NULL OR ? IN (SELECT value FROM json_each(m.json, '$.mechanism_profile.categories')))
            AND (? IS NULL OR m.id IN (SELECT value FROM json_each(?)))
        """
        restrictions=(section,section,category,category,candidates,candidates)
        if ts:
            fts=" OR ".join(f'"{term}"' for term in ts)
            table,weights=("modules_function_fts","0, 8, 5, 2, 3") if functional else ("modules_fts","0, 8, 5, 2")
            rows=db.execute(f"""
                SELECT m.json, bm25({table}, {weights}) AS rank
                FROM {table} JOIN modules m ON m.id = {table}.id
                WHERE {table} MATCH ? AND {conditions}
            """,(fts,*restrictions)).fetchall()
        else:
            rows=db.execute(f"SELECT m.json, 0 AS rank FROM modules m WHERE {conditions}",restrictions).fetchall()
    required=identifiers(query) if mode=="auto" else set()
    query_terms=set(ts)
    normalized=normalize(query)
    scored=[]
    for r in rows:
        m=json.loads(r["json"])
        hit=rank_hit(m,normalized,query_terms,required,r["rank"],functional=functional) if normalized else ((0,0),[])
        if hit is not None:
            (exact,relevance),matches=hit
            if mode=="model" and not ts:
                exact=2 if any(record["role"] in {"card_id","cad_name","module_model","model_family"}
                               for record in model_matches[m["id"]]) else 1
            reviewed=m.get("card_tier") in {"curated","source_reviewed"}
            if functional and m.get("mechanism_profile",{}).get("review_state")=="source_reviewed":
                reviewed=True
            quality=0.25 if reviewed else 0
            score=exact+quality+0.7*relevance/(1+relevance)
            scored.append((exact,score,m,matches))
    scored.sort(key=lambda hit:(-hit[1],hit[2]["id"]))
    results=[]
    seen_content=set()
    for exact,score,m,matches in scored:
        key=result_content_key(m)
        if key in seen_content:
            continue
        seen_content.add(key)
        results.append({
            **{key:m[key] for key in ("id","name","category","section","subtype","summary","source_url","entry","verification","limitations")},
            "score":score,"exact_match":bool(exact),"matches":matches,
            "retrieval_profile":m.get("retrieval_profile"),
            "component_locator":m.get("component_locator"),"card_tier":m.get("card_tier"),
            "component_id":m.get("component_id"),"interfaces":m.get("interfaces"),
            "component_aliases":m["component_aliases"],"component_specs":m["component_specs"],
            "cad_source":m["cad_source"],
            "mechanism_profile":m.get("mechanism_profile"),
        })
        if mode=="model":
            results[-1]["model_matches"]=model_matches[m["id"]]
        if len(results)>=limit:
            break
    return results
def validate(root):
    errors=[];mods=[json.loads(x) for x in (root/"catalog"/"modules.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    notes=load(root/"catalog"/"retrieval-notes.json",{})
    mechanics=load(root/MECHANISM_MANIFEST,{})
    try:
        documents=prepare_documents(root,{**notes.get("documents",{}),**mechanics.get("documents",{})})
    except ValueError as exc:
        errors.append(str(exc))
    profiles=notes.get("modules",{})
    inventory=load(root/"catalog"/"inventory.json",[]);by_path={f["path"]:f for f in inventory}
    fingerprints={}
    for m in mods:
        if m["id"] in profiles and not m.get("retrieval_profile"):
            errors.append(m["id"]+": retrieval profile missing; rebuild catalog")
        f=by_path.get(m["entry"]["path"])
        if not f:errors.append(m["id"]+": entry missing from inventory");continue
        p=(root/f["path"]).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():errors.append(m["id"]+": missing or unsafe path");continue
        if p not in fingerprints:
            with p.open("rb") as stream:
                fingerprints[p]=hashlib.file_digest(stream,"sha256").hexdigest()
        if fingerprints[p]!=f["sha256"]:errors.append(m["id"]+": content hash changed")
        if not m["evidence"] or not m["source_url"].startswith("https://bbs.robomaster.com/article/"):errors.append(m["id"]+": missing source evidence")
        cad=m["cad_source"]
        if (any(cad[key]!=m["entry"][key] for key in ("path","sha256","extension","asset_id"))
                or cad["definition_id"]!=m["definition_id"] or cad["archive_path"]!=f["archive_path"]
                or cad["source_url"]!=m["source_url"]):
            errors.append(m["id"]+": original CAD locator disagrees with source inventory")
    with closing(connect(root,read_only=True)) as db:
        indexed={mid:json.loads(data) for mid,data in db.execute("SELECT id,json FROM modules")}
        fts_ids=[mid for (mid,) in db.execute("SELECT id FROM modules_fts")]
        feature_fts=dict(db.execute("SELECT id,mechanism FROM modules_function_fts"))
        component_cards={mid:component_id for mid,component_id in db.execute("SELECT card_id,component_id FROM component_cards")}
        component_rows={row[0]:tuple(row[1:]) for row in db.execute("SELECT * FROM components")}
        indexed_evidence={key:json.loads(data) for key,data in db.execute("SELECT id,json FROM interface_evidence")}
        indexed_openings={tuple(row) for row in db.execute("SELECT * FROM interface_openings")}
        indexed_models={(key,mid):json.loads(data) for key,mid,data in db.execute("SELECT * FROM model_lookup")}
        aliases={(key,cid) for key,cid in db.execute("SELECT alias_key,component_id FROM component_aliases")}
        specs={(cid,key,scope,value,unit) for cid,key,scope,value,unit in db.execute(
            "SELECT component_id,spec_key,scope,value,unit FROM component_specs")}
        if db.execute("PRAGMA foreign_key_check").fetchone():
            errors.append("Component catalog has invalid foreign keys")
    if indexed!={m["id"]:m for m in mods} or sorted(fts_ids)!=sorted(m["id"] for m in mods):
        errors.append("SQLite, FTS and JSONL cards disagree")
    if feature_fts!={m["id"]:" ".join(tokens(" ".join(functional_fields(m)["mechanism"]),index=True)) for m in mods}:
        errors.append("Functional mechanism index and JSONL cards disagree")
    if not errors:
        steps=read_step_observations(root/"reports/step-inspection.jsonl")
        native=read_observations(root/NATIVE_LIVE_REPORT)
        actual={m["id"]:m.pop("mechanism_profile",None) for m in mods}
        attach_mechanism_profiles(mods,mechanics.get("cards",{}),documents,steps,native)
        if actual!={m["id"]:m.get("mechanism_profile") for m in mods}:
            errors.append("Mechanical claims or CAD observations changed; rebuild catalog")
    if (component_cards!={m["id"]:m["component_id"] for m in mods}
            or component_rows!={m["component_id"]:(m["source_id"],m["entry"]["asset_id"],m["entry"]["path"],
                                                 m["entry"]["sha256"],m["definition_id"] or "") for m in mods}):
        errors.append("Component index and JSONL cards disagree")
    interfaces=read_interface_evidence(root,mods,read_step_observations(root/"reports/step-inspection.jsonl"),
                                       read_observations(root/NATIVE_LIVE_REPORT))
    if (indexed_evidence!={record["id"]:record for record in interfaces} or indexed_openings!={
            (record["id"],opening["id"],opening["diameter_mm"],*opening["uv_mm"])
            for record in interfaces if record["state"]=="active"
            for opening in record["geometry"]["hole_pattern"]["openings"]}):
        errors.append("Interface evidence changed; rebuild catalog")
    if not errors:
        declarations=notes.get("model_identifiers",{})
        expected_models={(key,m["id"]):record for m in mods
                         for key,record in model_records(m,declarations.get(m["id"],[]),documents).items()}
        if indexed_models!=expected_models or declarations.keys()-indexed.keys():
            errors.append("Model identifiers changed; rebuild catalog")
    if aliases!={(normalize(alias["value"]),m["component_id"]) for m in mods for alias in m["component_aliases"]}:
        errors.append("Component aliases disagree with JSONL cards")
    if specs!={(m["component_id"],spec["key"],json.dumps(spec["scope"],sort_keys=True,ensure_ascii=False),
               spec["value"],spec["unit"]) for m in mods for spec in m["component_specs"]}:
        errors.append("Component specifications disagree with JSONL cards")
    summary=load(root/"reports"/"library-summary.json",{})
    if summary.get("unindexed_eligible_paths"):
        errors.append("Eligible CAD files are missing file cards")
    report={"modules_checked":len(mods),"cad_paths_checked":len(fingerprints),
            "components_checked":len(component_rows),"interfaces_checked":len(interfaces),
            "model_keys_checked":len(indexed_models),
            "component_aliases_checked":len(aliases),"component_specs_checked":len(specs),
            "mechanism_profiles_checked":sum("mechanism_profile" in m for m in mods),
            "errors":errors,"passed":not errors}
    (root/"reports"/"catalog-validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if errors:raise SystemExit(1)
def main():
    p=argparse.ArgumentParser();p.add_argument("--root",type=Path,default=ROOT);sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("build");sub.add_parser("validate");s=sub.add_parser("search");s.add_argument("query",nargs="?",default="");s.add_argument("--limit",type=int,default=5);s.add_argument("--section",choices=list(SECTIONS));s.add_argument("--mode",choices=SEARCH_MODES,default="auto")
    s.add_argument("--alias");s.add_argument("--spec",action="append",default=[]);s.add_argument("--category",choices=MECHANISM_CATEGORIES)
    a=p.parse_args();root=a.root.resolve()
    if a.action=="build":build(root)
    elif a.action=="validate":validate(root)
    else: print(json.dumps(search(root,a.query,a.limit,a.section,a.mode,alias=a.alias,specs=a.spec,category=a.category),ensure_ascii=False,indent=2))
if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8");main()
