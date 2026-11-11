from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mechanism_features import attach_mechanism_profiles


class MechanicalEvidenceTest(unittest.TestCase):
    def test_new_cad_revision_excludes_old_mechanism_claims(self):
        module = {"id": "part", "category": "swerve", "name": "Test assembly",
                  "entry": {"path": "test.sldasm", "sha256": "new"},
                  "selection_scope": "file", "definition_id": None,
                  "cad_source": {"path": "test.sldasm", "sha256": "new", "extension": ".sldasm",
                                 "definition_id": None, "source_url": "https://example.org/source"},
                  "interfaces": {}}
        declarations = {"part": {"cad_sha256": "old", "groups": ["suspension"],
                                 "search_terms": ["unsupported old claim"],
                                 "claims": [{"kind": "advantage", "ref": "unavailable"}]}}
        attach_mechanism_profiles([module], declarations, {}, {}, {})
        profile = module["mechanism_profile"]
        self.assertEqual(profile["review_state"], "source_review_stale")
        self.assertEqual(profile["categories"], ["swerve"])
        self.assertEqual(profile["claims"], [])
        self.assertEqual(profile["search_terms"], [])
        self.assertEqual(profile["cad_observations"][0]["evidence"]["sha256"], "new")
