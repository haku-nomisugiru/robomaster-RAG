"""OCP B-Rep descriptors, with optional trimesh previews and surface samples."""

from collections import Counter
from math import isfinite, log, sqrt

from inspect_step import _entry, read_xde
from interface_brep import explore, xyz


def step_shape(document, tool, definition_id=None):
    from OCP.BRep import BRep_Builder
    from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
    from OCP.TopoDS import TopoDS_Compound

    if definition_id is not None:
        label = TDF_Label()
        TDF_Tool.Label_s(document.GetData(), definition_id, label, False)
        if label.IsNull() or not tool.IsShape_s(label) or tool.IsReference_s(label):
            raise ValueError(f"STEP label {definition_id} is not a shape definition")
    else:
        roots = TDF_LabelSequence()
        tool.GetFreeShapes(roots)
        if not roots.Length():
            raise ValueError("STEP has no free shapes")
        if roots.Length() > 1:
            builder = BRep_Builder()
            compound = TopoDS_Compound()
            builder.MakeCompound(compound)
            for index in range(1, roots.Length() + 1):
                shape = tool.GetShape_s(roots.Value(index))
                if not shape.IsNull():
                    builder.Add(compound, shape)
            return compound, "assembly", None
        label = roots.Value(1)
    shape = tool.GetShape_s(label)
    if shape.IsNull():
        raise ValueError("STEP definition has no B-Rep shape")
    return shape, "assembly" if tool.IsAssembly_s(label) else "part", _entry(label)


def read_shape(path, definition_id=None):
    document, tool = read_xde(path)
    return document, step_shape(document, tool, definition_id)


def describe(shape, kind):
    import numpy as np
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopoDS import TopoDS

    types = Counter(BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType().name.removeprefix("GeomAbs_")
                    for face in explore(shape, TopAbs_FACE))
    if not types:
        raise ValueError("Selected shape has no surfaces")
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface, False, False)
    area = surface.Mass()
    if not isfinite(area) or area <= 0:
        raise ValueError("Selected shape has no finite positive surface area")
    solids = sum(1 for _ in explore(shape, TopAbs_SOLID))
    volume = GProp_GProps()
    if solids:
        BRepGProp.VolumeProperties_s(shape, volume, True, False, False)
    signed_volume = volume.Mass()
    has_volume = isfinite(signed_volume) and abs(signed_volume) > 0
    properties = volume if has_volume else surface
    inertia = properties.MatrixOfInertia()
    matrix = np.array([[inertia.Value(i, j) for j in range(1, 4)] for i in range(1, 4)])
    matrix /= properties.Mass()
    if not np.isfinite(matrix).all():
        raise ValueError("Selected shape has invalid central moments")
    moments = np.maximum(np.linalg.eigvalsh((matrix + matrix.T) * 0.5), 0)
    trace = float(moments.sum())
    if not isfinite(trace) or trace <= 0:
        raise ValueError("Selected shape has no usable central moments")
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    bounds = box.Get() if not (box.IsVoid() or box.IsOpen()) else None
    return {
        "kind": kind, "aabb_mm": [bounds[i + 3] - bounds[i] for i in range(3)] if bounds else None,
        "area_mm2": area, "closed_volume_mm3": abs(signed_volume) if has_volume else None,
        "inertia_basis": "closed_volume" if has_volume else "surface_area",
        "inertia_length_mm": sqrt(trace), "principal_moment_ratios": (moments / trace).tolist(),
        "face_types": dict(types), "face_count": sum(types.values()), "solid_occurrences": solids,
    }


def compare(reference, candidate, mode):
    ref_length, candidate_length = (item["inertia_length_mm"] for item in (reference, candidate))
    ref_types, candidate_types = (item["face_types"] for item in (reference, candidate))
    weights = {
        "moments": 0.9 * sum(abs(a - b) for a, b in zip(reference["principal_moment_ratios"],
                                                       candidate["principal_moment_ratios"])),
        "normalized_area": 0.25 * abs(log((reference["area_mm2"] / ref_length**2)
                                           / (candidate["area_mm2"] / candidate_length**2))),
        "surface_types": 0.10 * sum(abs(ref_types.get(key, 0) / reference["face_count"]
                                       - candidate_types.get(key, 0) / candidate["face_count"])
                                    for key in ref_types.keys() | candidate_types.keys()),
        "face_count": 0.04 * abs(log(reference["face_count"] / candidate["face_count"])),
        "solid_count": 0.08 * abs(log((1 + reference["solid_occurrences"])
                                      / (1 + candidate["solid_occurrences"]))),
    }
    ref_volume, candidate_volume = (item["closed_volume_mm3"] for item in (reference, candidate))
    if ref_volume and candidate_volume:
        weights["normalized_volume"] = 0.20 * abs(log((ref_volume / ref_length**3)
                                                       / (candidate_volume / candidate_length**3)))
    elif bool(ref_volume) != bool(candidate_volume):
        weights["inertia_basis"] = 0.35
    if mode == "size":
        weights["physical_size"] = 0.75 * abs(log(ref_length / candidate_length))
    return weights


def to_mesh(shape, deflection_mm):
    import trimesh
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    mesher = BRepMesh_IncrementalMesh(shape, deflection_mm, False, 0.3, False)
    if not mesher.IsDone():
        raise ValueError("OCP meshing failed")
    vertices, triangles = [], []
    for raw_face in explore(shape, TopAbs_FACE):
        face = TopoDS.Face_s(raw_face)
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        transform = location.Transformation()
        offset = len(vertices)
        vertices.extend(xyz(triangulation.Node(i).Transformed(transform))
                        for i in range(1, triangulation.NbNodes() + 1))
        for i in range(1, triangulation.NbTriangles() + 1):
            a, b, c = (offset + index - 1 for index in triangulation.Triangle(i).Get())
            triangles.append((a, c, b) if face.Orientation() == TopAbs_REVERSED else (a, b, c))
    if not triangles:
        raise ValueError("OCP generated no triangles")
    return trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)


def sample_surface(mesh, count):
    import trimesh

    points, _ = trimesh.sample.sample_surface(mesh, count, seed=26)
    return points


def surface_signature(points):
    import numpy as np

    paired = len(points) // 2
    distances = np.linalg.norm(points[:paired] - points[paired:2 * paired], axis=1)
    rms = float(np.sqrt(np.mean(distances**2)))
    if rms <= 0:
        raise ValueError("Surface samples have no spatial extent")
    return np.quantile(distances / rms, np.linspace(0.1, 0.9, 9))
