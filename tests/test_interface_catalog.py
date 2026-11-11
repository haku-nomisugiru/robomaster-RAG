import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from import_all_local import import_local
from ingest_forum import write_json
from library import build, connect, search, validate


class InterfaceIntakeTest(unittest.TestCase):
    def test_intake_preserves_identity_and_stale_geometry_is_removed_from_index(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            raw = root / "raw/downloads/123"
            raw.mkdir(parents=True)
            (root / "reports").mkdir()
            catalog = root / "catalog"
            write_json(catalog / "sources.json", [{
                "source_id": "bbs-123", "url": "https://bbs.robomaster.com/article/123",
                "title": "Fixture", "seed": {"team": "Fixture"}, "page_sha256": "0" * 64,
            }])
            for filename in ("inventory.json", "downloads.json", "open-cad-2025.json"):
                write_json(catalog / filename, [])
            (raw / "test-frame.sldprt").write_bytes(b"version 1")
            import_local(root, None)
            build(root)
            with contextlib.closing(connect(root, read_only=True)) as db:
                card = json.loads(db.execute("SELECT json FROM modules").fetchone()[0])
            entry = card["entry"]
            export = root / "reports/fixture.step"
            export.write_bytes(b"derivative fixture")
            record = {
                "id": "if-fixture", "schema_version": "1.0", "evidence_type": "cad_geometry_measured",
                "label": "Fixture", "target": {
                    "card_id": card["id"], "path": entry["path"], "sha256": entry["sha256"],
                    "configuration": "Default", "definition_id": None,
                },
                "measurement_input": {
                    "kind": "solidworks_step_export", "path": "reports/fixture.step",
                    "sha256": hashlib.sha256(export.read_bytes()).hexdigest(), "native_configuration": "Default",
                },
                "geometry": {"mounting_face": {}, "hole_pattern": {
                    "openings": [{"id": "h1", "diameter_mm": 6.2, "uv_mm": [0, 0]}],
                }},
            }
            write_json(catalog / "interface-evidence/if-fixture.json", record)
            (raw / "test-bracket.sldprt").write_bytes(b"unrelated new component")
            import_local(root, None)
            build(root)
            validate(root)
            added, = search(root, "test-bracket.sldprt", mode="model")
            self.assertEqual(added["model_matches"][0]["role"], "cad_name")
            added_by_alias, = search(root, "", alias="test-bracket.sldprt")
            self.assertEqual(added_by_alias["id"], added["id"])
            self.assertEqual(added_by_alias["cad_source"]["sha256"], added["entry"]["sha256"])
            measured, = search(root, "", specs=["opening_diameter_mm:6.2"])
            self.assertEqual(measured["id"], card["id"])
            with contextlib.closing(connect(root, read_only=True)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM components").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT state,component_id FROM interface_evidence").fetchone()[:],
                                 ("active", card["component_id"]))
                self.assertEqual(db.execute("SELECT diameter_mm FROM interface_openings").fetchone()[0], 6.2)

            path = root / entry["path"]
            path.write_bytes(b"replacement revision")
            inventory_file = catalog / "inventory.json"
            inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
            changed = next(row for row in inventory if row["path"] == entry["path"])
            changed.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            write_json(inventory_file, inventory)
            build(root)
            validate(root)
            self.assertEqual(search(root, "", specs=["opening_diameter_mm:6.2"]), [])
            with contextlib.closing(connect(root, read_only=True)) as db:
                state, component_id, data = db.execute("SELECT state,component_id,json FROM interface_evidence").fetchone()
                self.assertEqual((state, component_id), ("stale", card["component_id"]))
                self.assertEqual(json.loads(data)["stale_reason"], "target_revision_changed")
                self.assertEqual(db.execute("SELECT count(*) FROM interface_openings").fetchone()[0], 0)
                current = json.loads(db.execute("SELECT json FROM modules WHERE id=?", (card["id"],)).fetchone()[0])
                self.assertEqual(current["interfaces"]["status"], "stale_evidence")
                self.assertIsNone(current["interfaces"]["hole_patterns"])
