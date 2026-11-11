"""Measure explicitly selected planar CAD interfaces from analytic B-Rep."""

from __future__ import annotations

from itertools import combinations
import math
from pathlib import Path

from inspect_step import _entry, _shape_metrics, read_xde


GEOMETRY_TOL_MM = 0.0001


def xyz(value):
    return [value.X(), value.Y(), value.Z()]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def explore(shape, kind):
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        yield explorer.Current()
        explorer.Next()


def definition_shape(path: Path, definition_id: str | None):
    from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool

    doc, tool = read_xde(path)
    if definition_id:
        label = TDF_Label()
        TDF_Tool.Label_s(doc.GetData(), definition_id, label, False)
        if label.IsNull():
            raise ValueError(f"Unknown STEP definition {definition_id}")
    else:
        roots = TDF_LabelSequence()
        tool.GetFreeShapes(roots)
        if roots.Length() != 1:
            raise ValueError("A STEP file with multiple roots requires an explicit definition.")
        label = roots.Value(1)
    if tool.IsAssembly_s(label) or tool.IsReference_s(label):
        raise ValueError("Measure a selected part definition, rather than an assembly compound.")
    shape = tool.GetShape_s(label)
    if shape.IsNull():
        raise ValueError("Selected STEP definition has no shape")
    return doc, _entry(label), shape


def circular_opening(wire, plane_normal, plane_point):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    center = None
    radius = None
    sweep = 0.0
    for edge in explore(wire, TopAbs_EDGE):
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        if curve.GetType() != GeomAbs_Circle:
            return None
        circle = curve.Circle()
        location = xyz(circle.Location())
        if abs(dot(xyz(circle.Axis().Direction()), plane_normal)) < 1 - 1e-8:
            return None
        if abs(dot([a - b for a, b in zip(location, plane_point)], plane_normal)) > GEOMETRY_TOL_MM:
            return None
        if center is not None and (math.dist(location, center) > GEOMETRY_TOL_MM
                                   or abs(circle.Radius() - radius) > GEOMETRY_TOL_MM):
            return None
        center = location
        radius = circle.Radius()
        sweep += abs(curve.LastParameter() - curve.FirstParameter())
    if center is None or not math.isclose(sweep, math.tau, abs_tol=1e-5):
        return None
    return {"center_mm": center, "diameter_mm": 2 * radius}


def planar_faces(shape):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    for index, raw_face in enumerate(explore(shape, TopAbs_FACE), start=1):
        face = TopoDS.Face_s(raw_face)
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Plane:
            continue
        plane = surface.Plane()
        normal = xyz(plane.Axis().Direction())
        if face.Orientation() == TopAbs_REVERSED:
            normal = [-value for value in normal]
        point = xyz(plane.Location())
        outer = BRepTools.OuterWire_s(face)
        if outer.IsNull():
            continue
        properties = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, properties)
        inner_count = 0
        openings = []
        for wire in explore(face, TopAbs_WIRE):
            if wire.IsSame(outer):
                continue
            inner_count += 1
            opening = circular_opening(wire, normal, point)
            if opening:
                openings.append(opening)
        yield {"face_index": index, "normal": normal, "plane_point_mm": point,
               "centroid_mm": xyz(properties.CentreOfMass()), "area_mm2": properties.Mass(),
               "inner_wires": inner_count, "openings": openings}


def measurement(path, definition_id, selector):
    doc, actual_definition, shape = definition_shape(path, definition_id)
    face = next((item for item in planar_faces(shape) if item["face_index"] == selector["face_index"]), None)
    if (face is None or math.dist(face["normal"], selector["normal"]) > 1e-6
            or abs(face["area_mm2"] - selector["area_mm2"]) > 0.01
            or math.dist(face["centroid_mm"], selector["centroid_mm"]) > 0.01):
        raise ValueError("The selected face no longer agrees with its saved geometric signature.")
    indices = selector["opening_indices"]
    if (not indices or len(indices) != len(set(indices))
            or any(type(index) is not int or not 1 <= index <= len(face["openings"]) for index in indices)):
        raise ValueError("Opening selection must contain distinct one-based indices from this face.")
    openings = [face["openings"][index - 1] for index in indices]
    origin = [sum(opening["center_mm"][i] for opening in openings) / len(openings) for i in range(3)]
    normal = face["normal"]
    axis = min(([1, 0, 0], [0, 1, 0], [0, 0, 1]), key=lambda value: round(abs(dot(normal, value)), 8))
    projection = [value - dot(normal, axis) * n for value, n in zip(axis, normal)]
    u = [value / math.hypot(*projection) for value in projection]
    v = cross(normal, u)
    for index, opening in zip(indices, openings):
        delta = [a - b for a, b in zip(opening["center_mm"], origin)]
        opening.update(id=f"h{index}", uv_mm=[dot(delta, u), dot(delta, v)])
    return {
        "step_definition_id": actual_definition,
        "datum": {"origin_mm": origin, "normal": normal, "u_axis": u, "v_axis": v},
        "mounting_face": {key: face[key] for key in
                          ("face_index", "normal", "plane_point_mm", "centroid_mm", "area_mm2", "inner_wires")},
        "hole_pattern": {"openings": openings, "center_distances_mm": [
            {"from": a["id"], "to": b["id"], "distance_mm": math.dist(a["center_mm"], b["center_mm"])}
            for a, b in combinations(openings, 2)
        ]},
        "envelope_mm": _shape_metrics(shape).get("aabb_mm"),
    }
