"""Persist exact CAD descriptors and bind them to the existing catalog cards."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from ingest_forum import digest_file, write_json
from inspect_step import SUPPORTED_EXTENSIONS
from library import connect


DB_PATH = Path("catalog/geometry.sqlite3")
SCHEMA = "1"


def catalog_cards(root):
    with closing(connect(root, read_only=True)) as db:
        return {mid: json.loads(data) for mid, data in db.execute("SELECT id,json FROM modules")}


def geometry_sources(card):
    original = card["cad_source"]
    if not original["file_available"]:
        return
    if original["extension"] in SUPPORTED_EXTENSIONS:
        yield {"kind": "original_step", "path": original["path"], "sha256": original["sha256"],
               "definition_id": original["definition_id"], "configuration": None}
        return
    seen = set()
    for evidence in card.get("interfaces", {}).get("measurements", []):
        exported = evidence["measurement_input"]
        config = evidence["target"]["configuration"]
        key = (exported["path"], exported["sha256"], exported.get("definition_id"), config)
        if (exported["kind"] != "solidworks_step_export" or key in seen
                or evidence["target"]["sha256"] != original["sha256"]):
            continue
        seen.add(key)
        yield {"kind": "solidworks_step_export", "path": exported["path"], "sha256": exported["sha256"],
               "definition_id": exported.get("definition_id"), "configuration": config,
               "evidence_path": evidence["record_path"]}


def file_state(path):
    stat = path.stat()
    if not path.is_file():
        raise ValueError(f"Not a CAD file: {path}")
    return [stat.st_size, stat.st_mtime_ns]


def checked_file(root, source, cache):
    path = (root / source["path"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("CAD path is outside the library")
    state = file_state(path)
    if (path, state[0], state[1]) not in cache:
        cache[(path, state[0], state[1])] = digest_file(path)
    if cache[(path, state[0], state[1])] != source["sha256"]:
        raise ValueError("CAD SHA-256 differs from the catalog")
    return state


def worker_extract(path, targets, timeout):
    command = [sys.executable, "-B", str(Path(__file__).with_name("geometry_search.py")), "_extract", str(path)]
    try:
        result = subprocess.run(command, input=json.dumps(targets), capture_output=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        completed = {}
        for line in output.splitlines():
            if line.startswith("descriptor "):
                try:
                    key, record = json.loads(line.removeprefix("descriptor "))
                    completed[key] = record
                except json.JSONDecodeError:
                    continue
        return {"descriptors": completed, "failures": [
            {"definition_id": definition, "reason": f"OCP source timeout after {timeout}s"}
            for key, definition in targets.items() if key not in completed
        ]}
    if result.returncode:
        raise ValueError(result.stderr.strip().splitlines()[-1] if result.stderr.strip()
                         else f"OCP worker exited with code {result.returncode}")
    if not result.stdout.strip():
        raise ValueError("OCP worker produced no descriptors or status")
    return json.loads(result.stdout.splitlines()[-1])


def build(root, timeout=420):
    import OCP

    if timeout < 1:
        raise ValueError("OCP worker timeout must be positive")
    version = f"{SCHEMA}:{OCP.__version__}"
    path = root / DB_PATH
    cards = catalog_cards(root)
    bindings, failures, file_hashes = [], [], {}
    groups = defaultdict(dict)
    for card in cards.values():
        for source in geometry_sources(card):
            original = card["cad_source"]
            try:
                cad_state = checked_file(root, original, file_hashes)
                step_state = checked_file(root, source, file_hashes)
            except (OSError, ValueError) as exc:
                failures.append({"card_id": card["id"], "path": source["path"], "reason": str(exc)})
                continue
            key = f"{version}:{source['sha256']}:{source['definition_id'] or ''}"
            bindings.append({"card_id": card["id"], "key": key, "cad_sha256": original["sha256"],
                             "cad_path": original["path"], "cad_state": cad_state, "step_state": step_state,
                             "source": source})
            group = groups[source["sha256"]]
            group.setdefault("path", source["path"])
            group.setdefault("targets", {})[key] = source["definition_id"]
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS descriptors (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bindings (
                card_id TEXT NOT NULL, key TEXT NOT NULL, configuration TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(card_id, key, configuration)
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        known = {key for (key,) in db.execute("SELECT key FROM descriptors")}
        additions = {}
        for sha256, group in groups.items():
            targets = {key: definition for key, definition in group["targets"].items() if key not in known}
            if not targets:
                continue
            name = group["path"]
            source_path = root / name
            print(f"OCP {len(targets):>3}  {source_path.name}", file=sys.stderr, flush=True)
            try:
                state_before = checked_file(root, {"path": name, "sha256": sha256}, file_hashes)
                outcome = worker_extract(source_path, targets, timeout)
                if file_state(source_path) != state_before:
                    raise ValueError("STEP file changed during extraction")
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                failures.append({"path": name, "definitions": len(targets), "reason": str(exc)})
                continue
            with db:
                db.executemany("INSERT OR REPLACE INTO descriptors VALUES (?,?)",
                               ((key, json.dumps(value)) for key, value in outcome["descriptors"].items()))
            additions.update(outcome["descriptors"])
            failures.extend({"path": name, **issue} for issue in outcome["failures"])
        active_keys = known | additions.keys()
        indexed = [binding for binding in bindings if binding["key"] in active_keys]
        if not indexed:
            raise ValueError("No readable CAD shapes were available for indexing")
        with db:
            db.execute("DELETE FROM bindings")
            db.executemany("INSERT OR REPLACE INTO bindings VALUES (?,?,?,?)",
                           ((item["card_id"], item["key"], item["source"]["configuration"] or "",
                             json.dumps(item, ensure_ascii=False)) for item in indexed))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('version',?)", (version,))
    report = {"built_at_utc": datetime.now(timezone.utc).isoformat(), "engine": f"OCP {OCP.__version__}",
              "cards": len({item["card_id"] for item in indexed}),
              "unique_descriptors": len({item["key"] for item in indexed}), "new_descriptors": len(additions),
              "step_files": len({item["source"]["path"] for item in indexed if item["source"]["kind"] == "original_step"}),
              "native_export_cards": len({item["card_id"] for item in indexed
                                          if item["source"]["kind"] == "solidworks_step_export"}),
              "failures": failures}
    write_json(root / "reports/GEOMETRY_INDEX.json", report)
    return report


def current_bindings(root, cards):
    path = root / DB_PATH
    if not path.is_file():
        raise ValueError("Run geometry_search.py build to create the CAD geometry index")
    seen = {}
    file_hashes = {}

    def unchanged(relative, expected_sha, expected_state):
        key = (relative, expected_sha, tuple(expected_state))
        if key not in seen:
            path = (root / relative).resolve()
            try:
                seen[key] = path.is_relative_to(root) and (
                    file_state(path) == expected_state
                    or bool(checked_file(root, {"path": relative, "sha256": expected_sha}, file_hashes)))
            except (OSError, ValueError):
                seen[key] = False
        return seen[key]

    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        version = db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
        if version is None or not version[0].startswith(SCHEMA + ":"):
            raise ValueError("Rebuild the CAD geometry index for the current schema")
        bindings = []
        for card_id, raw, descriptor in db.execute("""
            SELECT b.card_id, b.data, d.data FROM bindings b JOIN descriptors d ON d.key=b.key
            ORDER BY b.card_id,b.configuration
        """):
            card = cards.get(card_id)
            if card is None or not card["cad_source"]["file_available"]:
                continue
            binding = json.loads(raw)
            original = card["cad_source"]
            source = binding["source"]
            if (original["path"] == binding["cad_path"] and original["sha256"] == binding["cad_sha256"]
                    and source in geometry_sources(card)
                    and unchanged(binding["cad_path"], binding["cad_sha256"], binding["cad_state"])
                    and unchanged(source["path"], source["sha256"], binding["step_state"])):
                bindings.append({**binding, "geometry": json.loads(descriptor)})
    return bindings
