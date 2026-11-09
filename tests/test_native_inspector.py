"""Tests use a deliberately minimal fake CAD object; no real CAD call occurs."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from inspect_native import inspect_native, active_solidworks


class SavedReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.scope = self.root / "assets" / "123" / "hash"
        self.scope.mkdir(parents=True)
        self.rows = []
        self.assembly = self.make_file("assembly.sldasm", b"fake saved assembly")

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self, relative, content=b"fake", *, source_id="bbs-123", asset_id="bbs-123-hash"):
        scope = self.scope if asset_id == "bbs-123-hash" else self.root / "assets" / "999" / "other"
        path = scope / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        row = {"source_id": source_id, "asset_id": asset_id,
               "path": path.relative_to(self.root).as_posix(),
               "sha256": hashlib.sha256(content).hexdigest()}
        self.rows.append(row)
        return row

    def inspect(self, references, row=None):
        class FakeApp:
            def __init__(self):
                self.calls = []
            def GetDocumentDependencies2(self, *args):
                self.calls.append(args)
                return references
            def __getattr__(self, key):
                raise AssertionError(f"Unexpected CAD API method: {key}")
        app = FakeApp()
        result = inspect_native(row or self.assembly, self.rows, self.root, app=app)
        if result["status"] != "input_changed":
            self.assertEqual(len(app.calls), 1)
            self.assertEqual(app.calls[0][1:], (False, False, False))
        return result, app

    def test_actual_same_asset_path_resolves_without_search(self):
        part = self.make_file("nested/part.SLDPRT")
        path = str(self.root / part["path"])
        result, _ = self.inspect(("part", path))
        ref = result["references"][0]
        self.assertEqual(ref["match_status"], "resolved_local")
        self.assertEqual(ref["resolved_local_path"], part["path"])
        self.assertFalse(result["dependency_closure_verified"])
        self.assertFalse(result["native_mates_verified"])

    def test_unique_basename_is_only_a_potential_match(self):
        part = self.make_file("nested/part.SLDPRT")
        result, _ = self.inspect(("part", r"C:\Author\Desktop\part.SLDPRT"))
        ref = result["references"][0]
        self.assertEqual(ref["match_status"], "potential_local_match")
        self.assertEqual(ref["original_path_status"], "outside_asset")
        self.assertEqual(ref["potential_local_paths"], [part["path"]])
        self.assertIsNone(ref["resolved_local_path"])

    def test_ambiguous_same_asset_and_cross_asset_isolation(self):
        self.make_file("one/part.sldprt")
        self.make_file("two/part.sldprt")
        self.make_file("part.sldprt", source_id="bbs-999", asset_id="other")
        result, _ = self.inspect(("part", r"C:\Author\part.sldprt"))
        ref = result["references"][0]
        self.assertEqual(ref["match_status"], "ambiguous")
        self.assertEqual(len(ref["potential_local_paths"]), 2)
        self.assertTrue(all("assets/123/hash/" in path for path in ref["potential_local_paths"]))

    def test_outside_asset_is_not_resolved_even_if_file_exists(self):
        other = self.make_file("unique.sldprt", source_id="bbs-999", asset_id="other")
        result, _ = self.inspect(("unique", str(self.root / other["path"])))
        self.assertEqual(result["references"][0]["match_status"], "outside_asset")

    def test_missing_inside_asset_is_preserved(self):
        result, _ = self.inspect(("missing", str(self.scope / "missing.sldprt")))
        ref = result["references"][0]
        self.assertEqual(ref["match_status"], "missing")
        self.assertEqual(ref["original_path_status"], "missing")

    def test_hash_mismatch_prevents_cad_call(self):
        row = dict(self.assembly, sha256="0" * 64)
        result, app = self.inspect((), row=row)
        self.assertEqual(result["status"], "input_changed")
        self.assertEqual(app.calls, [])

    def test_empty_and_malformed_api_output_do_not_claim_completeness(self):
        empty, _ = self.inspect(None)
        self.assertEqual(empty["status"], "no_references_returned")
        self.assertFalse(empty["dependency_closure_verified"])
        malformed, _ = self.inspect(("one", "path", "unpaired"))
        self.assertEqual(malformed["status"], "api_result_unrecognized")
        self.assertEqual(malformed["references"], [])

    def test_attach_rot_without_starting_session(self):
        calls = []
        dispatch_pointer = object()
        class Unknown:
            def QueryInterface(self, iid):
                calls.append(("query", iid))
                return dispatch_pointer
        class App:
            def _FlagAsMethod(self, method):
                calls.append(("flag_method", method))
        app = App()
        def wrap(pointer):
            self.assertIs(pointer, dispatch_pointer)
            calls.append(("wrap_existing",))
            return app
        pythoncom = SimpleNamespace(
            CoInitialize=lambda: calls.append(("initialize",)),
            CoUninitialize=lambda: calls.append(("uninitialize",)),
            CLSIDFromProgID=lambda progid: progid,
            GetActiveObject=lambda clsid: Unknown(),
            IID_IDispatch="IDispatch",
        )
        client = ModuleType("win32com.client")
        client.dynamic = SimpleNamespace(Dispatch=wrap)
        with patch.dict(sys.modules, {
            "pythoncom": pythoncom,
            "pywintypes": SimpleNamespace(IID=lambda progid: progid),
            "win32com": ModuleType("win32com"),
            "win32com.client": client,
        }):
            with active_solidworks() as attached:
                self.assertIs(attached, app)
        self.assertEqual(calls, [
            ("initialize",), ("query", "IDispatch"), ("wrap_existing",),
            ("flag_method", "GetDocumentDependencies2"), ("uninitialize",),
        ])

    def test_no_session_is_environment_unavailable(self):
        result = inspect_native(self.assembly, self.rows, self.root,
                                environment_error="No running session")
        self.assertEqual(result["status"], "environment_unavailable")
        self.assertEqual(result["errors"], ["No running session"])


if __name__ == "__main__":
    unittest.main()
