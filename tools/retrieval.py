"""Source-backed mechanism profiles and fields shared by indexing and ranking."""

import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata


CATEGORY_ALIASES = {
    "wheel_leg": "轮腿 串联腿", "swerve": "舵轮", "robotic_arm": "机械臂",
    "gearbox": "减速箱", "suspension": "悬挂", "chassis": "底盘",
    "lifting": "抬升", "gimbal": "云台", "gripper": "抓取", "wheel": "轮组",
}

LEXEMES = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*|[\u3400-\u9fff]+")
HAN = re.compile(r"[\u3400-\u9fff]")
MODEL_WORD = re.compile(r"[a-z]+[0-9]+(?:\.[0-9]+)*[a-z0-9]*|[0-9]{3,}")
SEARCH_MODES = ("auto", "function", "model")


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text, *, index=False):
    terms = []
    for word in LEXEMES.findall(normalize(text)):
        if HAN.match(word):
            if index or len(word) == 1:
                terms.extend(word)
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            # Keep model numbers such as P15.8 distinct from P15.76 in FTS5.
            terms.append(word.replace(".", "d"))
    return list(dict.fromkeys(terms))


def identifiers(query):
    return {
        word.replace(".", "d") for word in LEXEMES.findall(normalize(query))
        if re.fullmatch(r"[a-z]+[0-9]+(?:\.[0-9]+)*|[0-9]{3,}", word)
    }


def model_records(module, declarations, documents):
    entry = module["entry"]
    file_evidence = {"type": "cad_file_inventory", "path": entry["path"],
                     "sha256": entry["sha256"], "url": module["source_url"]}
    records = {normalize(module["id"]): {"code": module["id"], "role": "card_id",
               "evidence": {**file_evidence, "definition_id": module["definition_id"]}}}
    if module["selection_scope"] == "file":
        path = PurePosixPath(entry["path"])
        for name in (path.name, path.stem):
            records[normalize(name)] = {"code": name, "role": "cad_name", "evidence": file_evidence}
    declared = set()
    for declaration in declarations:
        code, role = declaration["code"], declaration["role"]
        key = normalize(code)
        if not key or key in declared or key == normalize(module["id"]):
            raise ValueError(f"Invalid or duplicate model code for {module['id']}: {code!r}")
        declared.add(key)
        if role == "cad_name":
            if module["selection_scope"] != "file" or key not in LEXEMES.findall(normalize(path.stem)):
                raise ValueError(f"Model code {code!r} is absent from the CAD filename for {module['id']}")
            evidence = file_evidence
        else:
            document = documents.get(declaration["ref"], {})
            if (role not in {"module_model", "model_family", "used_component", "reference_product"}
                    or document.get("source_id") != module["source_id"] or not declaration["locator"]):
                raise ValueError(f"Invalid source for model code {code!r} on {module['id']}")
            evidence = {name: document[name] for name in ("type", "path", "sha256")}
            evidence.update(url=module["source_url"], locator=declaration["locator"])
            if "thread_id" in document:
                evidence.update(thread_id=document["thread_id"], hash_scope="body_text")
        records[key] = {"code": code, "role": role, "evidence": evidence}
    return records


def exact_model_candidates(db, query):
    def lookup(key):
        rows = db.execute("SELECT card_id,json FROM model_lookup WHERE lookup_key=?", (key,))
        return {card_id: json.loads(record) for card_id, record in rows}

    normalized = normalize(query)
    if exact := lookup(normalized):
        return {key: [record] for key, record in exact.items()}, []
    if normalized.startswith("rm-"):
        return {}, []
    by_code = {}
    for word in dict.fromkeys(LEXEMES.findall(normalized)):
        if MODEL_WORD.fullmatch(word):
            by_code[word] = lookup(word)
        elif word.isascii() and word.isalpha():
            declared = {key: record for key, record in lookup(word).items()
                        if record["role"] not in {"cad_name", "card_id"}}
            if declared:
                by_code[word] = declared
    if not by_code:
        return {}, []
    common = set.intersection(*(set(match) for match in by_code.values()))
    code_terms = set(tokens(" ".join(by_code)))
    context = [term for term in tokens(query) if term not in code_terms]
    return {key: [match[key] for match in by_code.values()] for key in common}, context


def search_fields(module):
    profile = module.get("retrieval_profile", {})
    category = module["category"]
    return {
        "identity": [module["id"], module["name"]],
        "keywords": [
            category, *CATEGORY_ALIASES.get(category, "").split(),
            module.get("team") or "", *profile.get("aliases", []), *profile.get("keywords", []),
        ],
        "description": [
            module["summary"], *(feature["text"] for feature in module.get("features", [])),
            *(fact["text"] for fact in profile.get("facts", [])),
        ],
    }


def mechanism_text(module):
    profile = module.get("mechanism_profile", {})
    return [*profile.get("search_terms", []), *(claim["text"] for claim in profile.get("claims", []))]


def functional_fields(module):
    fields = search_fields(module)
    profile = module.get("mechanism_profile", {})
    fields["mechanism"] = [
        *mechanism_text(module),
        *(item["text"] for item in profile.get("cad_observations", [])),
        *(child["name"] for context in profile.get("assembly_contexts", [])
          for child in context["children"]),
    ]
    return fields


def prepare_documents(root, documents):
    checked = {}
    for key, document in documents.items():
        path = (root / document["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"Missing or unsafe retrieval evidence: {document['path']}")
        thread_id = document.get("thread_id")
        cache_key = (path, thread_id)
        if cache_key not in checked and thread_id is not None:
            rows = json.loads(path.read_text(encoding="utf-8-sig"))
            body = next((row["body_text"] for row in rows if row["thread_id"] == thread_id), None)
            if not body:
                raise ValueError(f"Missing article body {thread_id} in {document['path']}")
            checked[cache_key] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        elif cache_key not in checked:
            with path.open("rb") as stream:
                checked[cache_key] = hashlib.file_digest(stream, "sha256").hexdigest()
        if checked[cache_key] != document["sha256"]:
            raise ValueError(f"Retrieval evidence changed: {document['path']}")
    return documents


def resolve_fact_evidence(module, fact, documents):
    ref = fact["ref"]
    if ref == "cad":
        evidence = {"type": "cad_inventory", "path": module["entry"]["path"],
                    "sha256": module["entry"]["sha256"]}
    elif ref == "step" and module.get("geometry_summary"):
        evidence = {"type": "step_inspection", "path": module["geometry_report"],
                    "cad_sha256": module["entry"]["sha256"], "definition_id": module["definition_id"]}
    else:
        document = documents.get(ref)
        if not document or document["source_id"] != module["source_id"]:
            raise ValueError(f"Invalid retrieval evidence {ref!r} for {module['id']}")
        evidence = {key: document[key] for key in ("type", "path", "sha256")}
        if "thread_id" in document:
            evidence.update(thread_id=document["thread_id"], hash_scope="body_text")
    return {**evidence, "url": module["source_url"], "locator": fact["locator"]}


def apply_profile(module, profile, documents):
    if not profile:
        return
    facts = [{"kind": fact["kind"], "text": fact["text"],
              "evidence": resolve_fact_evidence(module, fact, documents)} for fact in profile["facts"]]
    module["summary"] = profile["summary"]
    module["retrieval_profile"] = {
        "aliases": profile["aliases"], "keywords": profile["keywords"], "facts": facts,
        "reuse_checks": profile.get("reuse_checks", []),
    }


def rank_hit(module, query, query_terms, required_ids, bm25, *, functional=False):
    fields = functional_fields(module) if functional else search_fields(module)
    term_hits = {
        field: set(tokens(" ".join(values), index=True)).intersection(query_terms)
        for field, values in fields.items()
    }
    if not required_ids.issubset(set().union(*term_hits.values())):
        return None
    if query in map(normalize, fields["identity"]):
        exact = 2
    else:
        keywords = fields["keywords"]
        if functional:
            keywords += module.get("mechanism_profile", {}).get("search_terms", [])
        exact = int(query in map(normalize, keywords))
    matches = [{"field": field, "terms": sorted(hits)} for field, hits in term_hits.items() if hits]
    # FTS5 returns a negative rank for stronger matches.
    return (exact, -bm25), matches
