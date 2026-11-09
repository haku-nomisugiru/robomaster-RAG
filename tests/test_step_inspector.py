"""Meaningful STEP tests: units, instancing, transforms, limits, and bad inputs."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from inspect_step import inspect_step, paths_from_inventory, main

HAS_OCP = importlib.util.find_spec("OCP") is not None


@unittest.skipUnless(HAS_OCP, "cadquery-ocp is not installed")
class StepInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, *, units="MM", nested=False):
        from OCP.BRep import BRep_Builder
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Interface import Interface_Static
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDataStd import TDataStd_Name
        from OCP.TDocStd import TDocStd_Document
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS_Compound
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.gp import gp_Trsf, gp_Vec, gp_Ax1, gp_Pnt, gp_Dir

        doc = TDocStd_Document(TCollection_ExtendedString("XDE"))
        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        box = tool.AddShape(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), False)
        TDataStd_Name.Set_s(box, TCollection_ExtendedString("SameBoxDefinition"))
        compound = TopoDS_Compound()
        BRep_Builder().MakeCompound(compound)
        assembly = tool.AddShape(compound, True)
        TDataStd_Name.Set_s(assembly, TCollection_ExtendedString("RepeatedBoxAssembly"))
        first = tool.AddComponent(assembly, box, TopLoc_Location())
        TDataStd_Name.Set_s(first, TCollection_ExtendedString("BoxOne"))
        translation = gp_Trsf()
        translation.SetTranslation(gp_Vec(20, 0, 0))
        second = tool.AddComponent(assembly, box, TopLoc_Location(translation))
        TDataStd_Name.Set_s(second, TCollection_ExtendedString("BoxTwo"))
        if nested:
            compound = TopoDS_Compound()
            BRep_Builder().MakeCompound(compound)
            top = tool.AddShape(compound, True)
            TDataStd_Name.Set_s(top, TCollection_ExtendedString("OuterAssembly"))
            translation = gp_Trsf()
            translation.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.pi / 2)
            translation.SetTranslationPart(gp_Vec(0, 50, 0))
            tool.AddComponent(top, assembly, TopLoc_Location(translation))
        tool.UpdateAssemblies()
        path = self.root / f"fixture-{units}-{nested}.step"
        writer = STEPCAFControl_Writer()
        writer.SetNameMode(True)
        self.assertTrue(Interface_Static.SetCVal_s("write.step.unit", units))
        try:
            self.assertTrue(writer.Transfer(doc, STEPControl_AsIs))
            self.assertEqual(writer.Write(str(path)), IFSelect_RetDone)
        finally:
            Interface_Static.SetCVal_s("write.step.unit", "MM")
        return path

    def test_definitions_instances_and_world_bounds(self):
        path = self.fixture()
        result = inspect_step(path, root=self.root)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(result["definition_count"], 2)
        self.assertEqual(result["occurrence_count"], 3)
        self.assertEqual(result["geometry_world_mm"]["solid_occurrence_count"], 2)
        self.assertAlmostEqual(result["geometry_world_mm"]["volume_mm3"], 2000, places=5)
        box_instances = [row for row in result["occurrences"] if not row["children"]]
        self.assertEqual(len({row["definition_id"] for row in box_instances}), 1)
        self.assertEqual(sorted(row["world_transform_mm"][0][3] for row in box_instances), [0, 20])
        self.assertAlmostEqual(result["geometry_world_mm"]["aabb_mm"]["size"][0], 30, places=5)
        self.assertFalse(result["native_mates_verified"])

    def test_nested_world_transforms(self):
        path = self.fixture(nested=True)
        result = inspect_step(path, root=self.root)
        self.assertEqual(result["status"], "ok", result["errors"])
        leaves = [row for row in result["occurrences"] if not row["children"]]
        self.assertTrue(all(abs(row["world_transform_mm"][0][3]) < 1e-8 for row in leaves))
        self.assertEqual(sorted(round(row["world_transform_mm"][1][3], 6) for row in leaves), [50, 70])
        self.assertAlmostEqual(leaves[0]["world_transform_mm"][0][1], -1, places=6)
        self.assertAlmostEqual(leaves[0]["world_transform_mm"][1][0], 1, places=6)
        self.assertAlmostEqual(result["geometry_world_mm"]["aabb_mm"]["min"][1], 50, places=5)

    def test_inch_export_normalizes_to_mm(self):
        path = self.fixture(units="INCH")
        self.assertIn("INCH", path.read_text(encoding="ascii").upper())
        result = inspect_step(path, root=self.root)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertAlmostEqual(result["geometry_world_mm"]["aabb_mm"]["size"][0], 30, places=5)
        self.assertAlmostEqual(result["geometry_world_mm"]["volume_mm3"], 2000, places=4)

    def test_node_limit_is_explicit(self):
        result = inspect_step(self.fixture(), root=self.root, max_nodes=1)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["occurrence_count"], 1)

    def test_bad_file_does_not_stop_batch(self):
        good = self.fixture()
        bad = self.root / "bad.step"
        bad.write_text("not a STEP file", encoding="utf-8")
        output = self.root / "result.jsonl"
        code = main(["--root", str(self.root), "--input", str(bad), "--input", str(good),
                     "--output", str(output)])
        rows = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual([row["status"] for row in rows], ["error", "ok"])

    def test_native_cad_is_not_claimed_parsed(self):
        native = self.root / "fake.SLDASM"
        native.write_bytes(b"dummy")
        result = inspect_step(native, root=self.root)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["native_mates_verified"])


class InventoryTests(unittest.TestCase):
    def test_inventory_dedup_filter_and_root_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            rows = [{"path": "assets/a.step"}, {"local_path": "assets/a.step"},
                    {"relative_path": "assets/native.SLDASM"}, "assets/b.stp"]
            self.assertEqual(paths_from_inventory({"files": rows}, root),
                             [root / "assets/a.step", root / "assets/b.stp"])
            with self.assertRaises(ValueError):
                paths_from_inventory([{"path": "../escape.step"}], root)


if __name__ == "__main__":
    unittest.main()
