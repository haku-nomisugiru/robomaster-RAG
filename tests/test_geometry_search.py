from importlib.util import find_spec
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from geometry_brep import compare, describe


@unittest.skipUnless(find_spec("numpy"), "Geometry search dependencies are optional for the text catalog")
class GeometryDescriptorTest(unittest.TestCase):
    def test_rotation_translation_and_uniform_scale(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

        base = BRepPrimAPI_MakeBox(20, 35, 7).Shape()
        descriptor = describe(base, "part")
        rigid = gp_Trsf()
        rigid.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 2, 3)), 0.63)
        rigid.SetTranslationPart(gp_Vec(45, -120, 25))
        moved = describe(BRepBuilderAPI_Transform(base, rigid, True).Shape(), "part")
        self.assertLess(sum(compare(descriptor, moved, "size").values()), 1e-8)
        scaled = gp_Trsf()
        scaled.SetScale(gp_Pnt(), 2)
        doubled = describe(BRepBuilderAPI_Transform(base, scaled, True).Shape(), "part")
        self.assertLess(sum(compare(descriptor, doubled, "shape").values()), 1e-8)
        self.assertAlmostEqual(compare(descriptor, doubled, "size")["physical_size"], 0.75 * math.log(2))

    def test_same_bounds_but_different_brep(self):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

        base = BRepPrimAPI_MakeBox(20, 35, 7).Shape()
        cutter = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 17, -1), gp_Dir(0, 0, 1)), 2, 9).Shape()
        solid = describe(base, "part")
        drilled = describe(BRepAlgoAPI_Cut(base, cutter).Shape(), "part")
        self.assertEqual(solid["aabb_mm"], drilled["aabb_mm"])
        self.assertAlmostEqual(solid["closed_volume_mm3"] - drilled["closed_volume_mm3"], math.pi * 4 * 7)
        self.assertGreater(sum(compare(solid, drilled, "size").values()), 0.01)
