"""Relational projection of library cards and hash-matched CAD observations."""

from __future__ import annotations

import hashlib
import json
from retrieval import normalize


DROP_SQL = """
DROP TABLE IF EXISTS component_specs;
DROP TABLE IF EXISTS component_aliases;
DROP TABLE IF EXISTS interface_openings;
DROP TABLE IF EXISTS interface_evidence;
DROP TABLE IF EXISTS component_occurrences;
DROP TABLE IF EXISTS component_cards;
DROP TABLE IF EXISTS components;
"""

CREATE_SQL = """
CREATE TABLE components (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    asset_id TEXT NOT NULL,
    path TEXT NOT NULL REFERENCES files(path),
    sha256 TEXT NOT NULL,
    definition_key TEXT NOT NULL,
    UNIQUE(path, definition_key)
);
CREATE TABLE component_cards (
    card_id TEXT PRIMARY KEY REFERENCES modules(id),
    component_id TEXT NOT NULL REFERENCES components(id)
);
CREATE TABLE component_aliases (
    alias_key TEXT NOT NULL,
    component_id TEXT NOT NULL REFERENCES components(id),
    json TEXT NOT NULL,
    PRIMARY KEY(alias_key, component_id)
);
CREATE TABLE component_specs (
    component_id TEXT NOT NULL REFERENCES components(id),
    spec_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY(component_id, spec_key, scope, value)
);
CREATE INDEX component_spec_range ON component_specs(spec_key,value);
CREATE TABLE component_occurrences (
    root_card_id TEXT NOT NULL REFERENCES modules(id),
    occurrence_id TEXT NOT NULL,
    parent_occurrence_id TEXT,
    component_id TEXT REFERENCES components(id),
    root_configuration TEXT,
    referenced_configuration TEXT,
    evidence_type TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY(root_card_id, occurrence_id)
);
CREATE INDEX component_occurrence_target ON component_occurrences(component_id);
CREATE TABLE interface_evidence (
    id TEXT PRIMARY KEY,
    component_id TEXT REFERENCES components(id),
    state TEXT NOT NULL CHECK(state IN ('active','stale')),
    json TEXT NOT NULL
);
CREATE TABLE interface_openings (
    interface_id TEXT NOT NULL REFERENCES interface_evidence(id),
    opening_id TEXT NOT NULL,
    diameter_mm REAL NOT NULL CHECK(diameter_mm > 0),
    u_mm REAL NOT NULL,
    v_mm REAL NOT NULL,
    PRIMARY KEY(interface_id, opening_id)
);
CREATE INDEX interface_opening_diameter ON interface_openings(diameter_mm);
"""


def assign_component_ids(modules):
    for module in modules:
        entry = module["entry"]
        key = "\0".join((entry["asset_id"], entry["path"].casefold(), module["definition_id"] or ""))
        module["component_id"] = "cmp-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def occurrence_rows(modules, step_reports, native_reports):
    files = {m["entry"]["path"]:m for m in modules if m["selection_scope"] == "file"}
    definitions = {(m["entry"]["path"],m["definition_id"]):m for m in modules if m["definition_id"]}
    for path, root in files.items():
        report = step_reports.get(path)
        if report and report.get("sha256") == root["entry"]["sha256"] and report["status"] in {"ok","partial"}:
            for occurrence in report["occurrences"]:
                target = definitions.get((path,occurrence["definition_id"]))
                if not target and occurrence["definition_id"] in report["root_definition_ids"]:
                    target = root
                yield root, target, None, "step_xcaf", occurrence
        report = native_reports.get(path)
        if report and report["sha256"] == root["entry"]["sha256"] and report["status"] == "observed":
            for occurrence in report["occurrences"]:
                reference = occurrence["reference"]
                target = files.get(reference.get("path"))
                if (target and (target["entry"]["sha256"] != reference.get("sha256")
                                or occurrence["suppression_code"] not in (2,3))):
                    target = None
                yield root, target, report["active_configuration"], "solidworks_live", occurrence


def index_components(db, modules, steps, native, evidence):
    definitions = {module["component_id"]:module for module in modules}
    db.executemany("INSERT INTO components VALUES (?,?,?,?,?,?)", (
        (mid,m["source_id"],m["entry"]["asset_id"],m["entry"]["path"],
         m["entry"]["sha256"],m["definition_id"] or "") for mid,m in definitions.items()
    ))
    db.executemany("INSERT INTO component_cards VALUES (?,?)",
                   ((m["id"],m["component_id"]) for m in modules))
    db.executemany("INSERT OR IGNORE INTO component_aliases VALUES (?,?,?)", (
        (normalize(alias["value"]), m["component_id"], json.dumps(alias, ensure_ascii=False))
        for m in modules for alias in m["component_aliases"]
    ))
    db.executemany("INSERT OR IGNORE INTO component_specs VALUES (?,?,?,?,?,?)", (
        (m["component_id"], spec["key"], json.dumps(spec["scope"], sort_keys=True, ensure_ascii=False),
         spec["value"], spec["unit"], json.dumps(spec, ensure_ascii=False))
        for m in modules for spec in m["component_specs"]
    ))
    db.executemany("INSERT INTO component_occurrences VALUES (?,?,?,?,?,?,?,?)", (
        (root["id"],o["occurrence_id"],o["parent_occurrence_id"],
         target["component_id"] if target else None, config, o.get("referenced_configuration"),
         kind, json.dumps(o,ensure_ascii=False))
        for root,target,config,kind,o in occurrence_rows(modules,steps,native)
    ))
    db.executemany("INSERT INTO interface_evidence VALUES (?,?,?,?)", (
        (record["id"],record.get("component_id"),record["state"],json.dumps(record,ensure_ascii=False))
        for record in evidence
    ))
    db.executemany("INSERT INTO interface_openings VALUES (?,?,?,?,?)", (
        (record["id"],opening["id"],opening["diameter_mm"],*opening["uv_mm"])
        for record in evidence if record["state"] == "active"
        for opening in record["geometry"]["hole_pattern"]["openings"]
    ))
