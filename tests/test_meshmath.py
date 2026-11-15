"""meshmath on meshes built by hand as lists: no Modo, no fake lx. The
numbers in each test were worked out on paper first."""

import json
import math
import unittest

import support  # noqa: F401  (puts the kit's modules on sys.path)
from modotools import meshmath

# The unit cube at the origin, as prim.cube builds it on 17.1 (2026-11-08):
# the same vertex order, polygons and edges.
CUBE = {
    "positions": [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, -0.5, 0.5),
                  (-0.5, -0.5, 0.5), (-0.5, 0.5, -0.5), (0.5, 0.5, -0.5),
                  (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)],
    "polygons": [[0, 1, 2, 3], [4, 5, 1, 0], [5, 6, 2, 1], [6, 7, 3, 2],
                 [7, 4, 0, 3], [4, 7, 6, 5]],
    "types": ["face"] * 6,
    "edges": [(0, 1), (1, 2), (2, 3), (0, 3), (4, 5), (1, 5), (0, 4), (5, 6),
              (2, 6), (6, 7), (3, 7), (4, 7)],
}

EMPTY = {"positions": [], "polygons": [], "types": [], "edges": []}

IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class CountsTest(unittest.TestCase):
    def test_cube(self):
        self.assertEqual(meshmath.counts(CUBE),
                         {"vertices": 8, "edges": 12, "polygons": 6})

    def test_empty(self):
        self.assertEqual(meshmath.counts(EMPTY),
                         {"vertices": 0, "edges": 0, "polygons": 0})


class TypesTest(unittest.TestCase):
    def test_all_faces_is_none(self):
        self.assertIsNone(meshmath.types(CUBE["types"]))

    def test_empty_is_none(self):
        self.assertIsNone(meshmath.types([]))

    def test_any_other_type_lists_every_type(self):
        self.assertEqual(meshmath.types(["face", "subd", "subd", "curve"]),
                         {"face": 1, "subd": 2, "curve": 1})


class BoxTest(unittest.TestCase):
    def test_cube(self):
        self.assertEqual(meshmath.box(CUBE["positions"]),
                         [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]])

    def test_empty_is_none(self):
        self.assertIsNone(meshmath.box([]))

    def test_scaled_by_2_and_moved_to_x_3(self):
        scale2 = ((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0))
        world = meshmath.to_world(CUBE["positions"], scale2, (3.0, 0.0, 0.0))
        self.assertEqual(meshmath.box(world), [[2.0, -1.0, -1.0], [4.0, 1.0, 1.0]])

    def test_rotated_30_about_y_as_modo_gives_the_matrix(self):
        # The matrix Modo 17.1 gave for rot.Y = 30 degrees (2026-11-08): it
        # applies to a column vector, so +X goes to (cos, 0, -sin).
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        rot = ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
        world = meshmath.to_world([(1.0, 0.0, 0.0)], rot, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(world[0][0], c)
        self.assertAlmostEqual(world[0][2], -s)
        # The cube's box grows to cos 30 + sin 30 = 1.366 across, halved.
        half = round((c + s) / 2, 6)
        cube = meshmath.to_world(CUBE["positions"], rot, (0.0, 0.0, 0.0))
        self.assertEqual(meshmath.box(cube),
                         [[-half, -0.5, -half], [half, 0.5, half]])

    def test_identity_leaves_positions_alone(self):
        self.assertEqual(meshmath.to_world(CUBE["positions"], IDENTITY, (0, 0, 0)),
                         [list(p) for p in CUBE["positions"]])


class RoundingTest(unittest.TestCase):
    def test_six_places_and_no_negative_zero(self):
        self.assertEqual(meshmath.box([(-0.0, 1e-9, 0.1234567)]),
                         [[0.0, 0.0, 0.123457], [0.0, 0.0, 0.123457]])
        self.assertEqual(str(meshmath.box([(-0.0, 0.0, 0.0)])[0][0]), "0.0")


# --- shape facts. Every mesh below is built from the cube or by hand,
# and every number was worked out on paper first.

def moved(mesh, by):
    """mesh with every position shifted by `by` (x, y, z)."""
    return dict(mesh, positions=[tuple(p[k] + by[k] for k in range(3))
                                 for p in mesh["positions"]])


def joined(a, b):
    """Two meshes as one, b's indices after a's."""
    n = len(a["positions"])
    return {"positions": list(a["positions"]) + list(b["positions"]),
            "polygons": a["polygons"] + [[i + n for i in p] for p in b["polygons"]],
            "types": a["types"] + b["types"],
            "edges": list(a["edges"]) + [(x + n, y + n) for x, y in b["edges"]]}


BACKWARDS = dict(CUBE, polygons=[list(reversed(p)) for p in CUBE["polygons"]])

SCALED = dict(CUBE, positions=meshmath.to_world(
    CUBE["positions"], ((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0)),
    (3.0, 0.0, 0.0)))

# 2 by 2 quads of 0.5 m in the XZ plane: vertex r * 3 + c at
# (c / 2 - 0.5, 0, r / 2 - 0.5).
PLANE = {
    "positions": [(c * 0.5 - 0.5, 0.0, r * 0.5 - 0.5)
                  for r in range(3) for c in range(3)],
    "polygons": [[r * 3 + c, r * 3 + c + 1, (r + 1) * 3 + c + 1, (r + 1) * 3 + c]
                 for r in range(2) for c in range(2)],
    "types": ["face"] * 4,
    "edges": [(0, 1), (1, 2), (3, 4), (4, 5), (6, 7), (7, 8),
              (0, 3), (3, 6), (1, 4), (4, 7), (2, 5), (5, 8)],
}

TOPLESS = dict(CUBE, polygons=CUBE["polygons"][:5], types=["face"] * 5)

TWO_CUBES = joined(CUBE, moved(CUBE, (3.0, 0.0, 0.0)))

# Three unit quads hinged on the edge 0-1 along X, fanning up, out and down.
FIN = {
    "positions": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1),
                  (1, 0, 1), (0, -1, 0), (1, -1, 0)],
    "polygons": [[0, 1, 3, 2], [0, 1, 5, 4], [0, 1, 7, 6]],
    "types": ["face"] * 3,
    "edges": [(0, 1), (1, 3), (2, 3), (0, 2), (1, 5), (4, 5), (0, 4), (1, 7),
              (6, 7), (0, 6)],
}

# A triangle, a quad and a pentagon, apart, all in the XY plane.
MIXED = {
    "positions": [(0, 0, 0), (1, 0, 0), (0, 1, 0),
                  (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0),
                  (4, 0, 0), (5, 0, 0), (5, 1, 0), (4.5, 2, 0), (4, 1, 0)],
    "polygons": [[0, 1, 2], [3, 4, 5, 6], [7, 8, 9, 10, 11]],
    "types": ["face"] * 3,
    "edges": [],
}

# A quad whose last two vertices sit on each other: a triangle of area 0.5.
DOUBLED = {"positions": [(0, 0, 0), (1, 0, 0), (1, 0, 1), (1, 0, 1)],
           "polygons": [[0, 1, 2, 3]], "types": ["face"], "edges": []}

# A quad with all four vertices on a line: no area, no normal.
FLAT = {"positions": [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)],
        "polygons": [[0, 1, 2, 3]], "types": ["face"], "edges": []}

LONE = dict(CUBE, positions=list(CUBE["positions"]) + [(9.0, 9.0, 9.0)])

# The cube with a curve through two of its corners: a curve is in types
# and nowhere else.
CURVED = dict(CUBE, polygons=CUBE["polygons"] + [[0, 6]],
              types=CUBE["types"] + ["curve"])


# a quad and its reverse on the same four vertices, turned
# and moved so the float sums don't cancel exactly. Closed, encloses nothing.
_C, _S = math.cos(0.3), math.sin(0.3)
CARD = {"positions": meshmath.to_world(
            [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)],
            ((_C, -_S, 0.0), (_S, _C, 0.0), (0.0, 0.0, 1.0)), (5.1, 7.3, -2.2)),
        "polygons": [[0, 1, 2, 3], [3, 2, 1, 0]], "types": ["face"] * 2,
        "edges": [(0, 1), (1, 2), (2, 3), (0, 3)]}

# A face polygon of two points: it has sides but no surface, so no edges
# for the topology and nothing to enclose.
TWO_POINT = {"positions": [(0, 0, 0), (1, 0, 0)], "polygons": [[0, 1]],
             "types": ["face"], "edges": [(0, 1)]}

FIXTURES = (CUBE, BACKWARDS, SCALED, PLANE, TOPLESS, TWO_CUBES, FIN, MIXED,
            DOUBLED, FLAT, LONE, CURVED, EMPTY, CARD, TWO_POINT)


class NewellTest(unittest.TestCase):
    def test_the_cubes_six_normals_and_areas(self):
        normals = [meshmath.newell(CUBE["positions"], p) for p in CUBE["polygons"]]
        self.assertEqual([n for n, _a in normals],
                         [(0.0, -1.0, 0.0), (0.0, 0.0, -1.0), (1.0, 0.0, 0.0),
                          (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)])
        self.assertEqual([a for _n, a in normals], [1.0] * 6)

    def test_two_vertices_on_each_other_is_a_triangle(self):
        normal, area = meshmath.newell(DOUBLED["positions"], [0, 1, 2, 3])
        self.assertEqual(area, 0.5)
        self.assertEqual(normal, (0.0, -1.0, 0.0))

    def test_four_on_a_line_has_a_zero_normal_not_nan(self):
        self.assertEqual(meshmath.newell(FLAT["positions"], [0, 1, 2, 3]),
                         ((0.0, 0.0, 0.0), 0.0))

    def test_fewer_than_three_vertices(self):
        self.assertEqual(meshmath.newell(FLAT["positions"], [0, 1]),
                         ((0.0, 0.0, 0.0), 0.0))


class AreaVolumeTest(unittest.TestCase):
    def test_cube(self):
        self.assertEqual(meshmath.area(CUBE), 6.0)
        self.assertEqual(meshmath.volume(CUBE), 1.0)

    def test_backwards_cube_has_negative_volume(self):
        self.assertEqual(meshmath.area(BACKWARDS), 6.0)
        self.assertEqual(meshmath.volume(BACKWARDS), -1.0)

    def test_scaled_by_2_and_moved(self):
        self.assertEqual(meshmath.area(SCALED), 24.0)
        self.assertEqual(meshmath.volume(SCALED), 8.0)

    def test_open_meshes_have_no_volume(self):
        self.assertEqual(meshmath.area(PLANE), 1.0)
        self.assertIsNone(meshmath.volume(PLANE))
        self.assertIsNone(meshmath.volume(TOPLESS))
        self.assertIsNone(meshmath.volume(FIN))

    def test_no_polygons_has_no_volume(self):
        self.assertEqual(meshmath.area(EMPTY), 0.0)
        self.assertIsNone(meshmath.volume(EMPTY))

    def test_two_cubes(self):
        self.assertEqual(meshmath.volume(TWO_CUBES), 2.0)

    def test_a_curve_is_left_out(self):
        self.assertEqual(meshmath.area(CURVED), 6.0)
        self.assertEqual(meshmath.volume(CURVED), 1.0)


    def test_a_closed_mesh_that_encloses_nothing_has_volume_0(self):
        self.assertEqual(meshmath.volume(CARD), 0.0)
        self.assertEqual(meshmath.summary(CARD)["volume"], 0.0)

    def test_a_tiny_closed_mesh_keeps_its_volume(self):
        tiny = dict(CUBE, positions=meshmath.to_world(
            CUBE["positions"], ((1e-3, 0, 0), (0, 1e-3, 0), (0, 0, 1e-3)),
            (100.0, 0.0, 0.0)))
        self.assertEqual(meshmath.summary(tiny)["volume"], 1e-9)

    def test_a_two_point_face_encloses_nothing(self):
        self.assertIsNone(meshmath.volume(TWO_POINT))
        self.assertEqual(meshmath.summary(TWO_POINT)["volume"], None)


class TopologyTest(unittest.TestCase):
    def test_sides(self):
        self.assertEqual(meshmath.sides(CUBE), {"4": 6})
        self.assertEqual(meshmath.sides(MIXED), {"3": 1, "4": 1, "5+": 1})
        self.assertEqual(meshmath.sides(CURVED), {"4": 6})
        self.assertEqual(meshmath.sides(dict(FLAT, polygons=[[0, 1]])),
                         {"1-2": 1})

    def test_poles(self):
        self.assertEqual(meshmath.poles(CUBE), {"3": 8})
        self.assertEqual(meshmath.poles(PLANE), {})
        # The top's four corners are on the border now.
        self.assertEqual(meshmath.poles(TOPLESS), {"3": 4})
        self.assertEqual(meshmath.poles(TWO_CUBES), {"3": 16})

    def test_borders(self):
        self.assertEqual(meshmath.borders(CUBE), (0, 0))
        self.assertEqual(meshmath.borders(PLANE), (1, 8))
        self.assertEqual(meshmath.borders(TOPLESS), (1, 4))
        self.assertEqual(meshmath.borders(joined(PLANE, moved(PLANE, (5, 0, 0)))),
                         (2, 16))

    def test_nonmanifold(self):
        self.assertEqual(meshmath.nonmanifold(CUBE), 0)
        self.assertEqual(meshmath.nonmanifold(FIN), 1)

    def test_lone(self):
        self.assertEqual(meshmath.lone(CUBE), 0)
        self.assertEqual(meshmath.lone(LONE), 1)

    def test_parts(self):
        self.assertEqual(meshmath.parts(CUBE), 1)
        self.assertEqual(meshmath.parts(TWO_CUBES), 2)
        self.assertEqual(meshmath.parts(MIXED), 3)
        self.assertEqual(meshmath.parts(LONE), 1)
        self.assertEqual(meshmath.parts(EMPTY), 0)

    def test_a_two_point_face_has_no_edges_for_the_topology(self):
        self.assertEqual(meshmath.sides(TWO_POINT), {"1-2": 1})
        self.assertEqual(meshmath.poles(TWO_POINT), {})
        self.assertEqual(meshmath.borders(TWO_POINT), (0, 0))

    def test_zero_area(self):
        self.assertEqual(meshmath.zero_area(DOUBLED), 0)
        self.assertEqual(meshmath.zero_area(FLAT), 1)
        self.assertEqual(meshmath.zero_area(CUBE), 0)


class SummaryTest(unittest.TestCase):
    def test_cube(self):
        self.assertEqual(meshmath.summary(CUBE), {
            "vertices": 8, "edges": 12, "polygons": 6, "sides": {"4": 6},
            "poles": {"3": 8}, "boundary_loops": 0, "boundary_edges": 0,
            "parts": 1, "box": [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            "area": 6.0, "volume": 1.0})

    def test_the_rare_facts_only_when_not_0(self):
        self.assertEqual(meshmath.summary(FIN)["nonmanifold_edges"], 1)
        self.assertEqual(meshmath.summary(LONE)["lone_vertices"], 1)
        self.assertEqual(meshmath.summary(FLAT)["zero_area"], 1)
        for name in ("nonmanifold_edges", "lone_vertices", "zero_area"):
            self.assertNotIn(name, meshmath.summary(CUBE))

    def test_types_when_any_isnt_a_face(self):
        self.assertEqual(meshmath.summary(CURVED)["types"], {"face": 6, "curve": 1})

    def test_open_plane(self):
        got = meshmath.summary(PLANE)
        self.assertEqual((got["vertices"], got["edges"], got["polygons"]), (9, 12, 4))
        self.assertEqual((got["poles"], got["boundary_loops"],
                          got["boundary_edges"], got["area"], got["volume"]),
                         ({}, 1, 8, 1.0, None))

    def test_empty(self):
        got = meshmath.summary(EMPTY)
        self.assertEqual((got["sides"], got["parts"], got["box"], got["area"],
                          got["volume"]), ({}, 0, None, 0.0, None))


    def test_summary_agrees_with_each_fact_alone(self):
        for mesh in FIXTURES:
            got = meshmath.summary(mesh)
            loops, edges = meshmath.borders(mesh)
            self.assertEqual(
                (got["sides"], got["poles"], got["boundary_loops"],
                 got["boundary_edges"], got.get("nonmanifold_edges", 0),
                 got.get("lone_vertices", 0), got.get("zero_area", 0),
                 got["parts"], got["area"], got["volume"]),
                (meshmath.sides(mesh), meshmath.poles(mesh), loops, edges,
                 meshmath.nonmanifold(mesh), meshmath.lone(mesh),
                 meshmath.zero_area(mesh), meshmath.parts(mesh),
                 meshmath.significant(meshmath.area(mesh)),
                 meshmath.significant(meshmath.volume(mesh))))


class SignificantTest(unittest.TestCase):
    def test_six_significant_figures(self):
        self.assertEqual(meshmath.significant(1234.5678), 1234.57)
        self.assertEqual(meshmath.significant(0.000123456789), 0.000123457)
        self.assertEqual(meshmath.significant(6.0000000001), 6.0)

    def test_negative_zero_is_written_0(self):
        self.assertEqual(str(meshmath.significant(-0.0)), "0.0")
        self.assertEqual(str(meshmath.significant(-1e-30 * 0)), "0.0")

    def test_none_stays_none(self):
        self.assertIsNone(meshmath.significant(None))

    def test_the_same_mesh_twice_is_the_same_text(self):
        # A cube turned 30 degrees and moved, where the sums don't come out
        # exact in floats.
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        turned = dict(CUBE, positions=meshmath.to_world(
            CUBE["positions"], ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c)),
            (0.1, 0.2, 0.3)))
        first = json.dumps(meshmath.summary(turned), sort_keys=True)
        self.assertEqual(json.dumps(meshmath.summary(turned), sort_keys=True), first)
        got = meshmath.summary(turned)
        self.assertEqual((got["area"], got["volume"]), (6.0, 1.0))



class EntriesTest(unittest.TestCase):
    """The element entries. The cube's top is polygon 5, [4, 7, 6, 5]."""

    def test_the_cubes_top(self):
        self.assertEqual(meshmath.polygon_entry(CUBE, 5),
                         {"polygon": 5, "sides": 4, "normal": [0.0, 1.0, 0.0],
                          "centroid": [0.0, 0.5, 0.0], "area": 1.0})

    def test_a_cube_edge(self):
        # (0, 1) runs along the bottom [0, 1, 2, 3] and the back [4, 5, 1, 0].
        self.assertEqual(meshmath.edge_entry(CUBE, (1, 0)),
                         {"edge": [0, 1], "length": 1.0, "polygons": 2,
                          "angle": 90.0})

    def test_the_planes_border_edge(self):
        self.assertEqual(meshmath.edge_entry(PLANE, (0, 1)),
                         {"edge": [0, 1], "length": 0.5, "polygons": 1,
                          "angle": None})

    def test_the_planes_middle_edge(self):
        got = meshmath.edge_entry(PLANE, (1, 4))
        self.assertEqual((got["polygons"], got["angle"]), (2, 0.0))

    def test_a_fin_edge_has_no_angle(self):
        got = meshmath.edge_entry(FIN, (0, 1))
        self.assertEqual((got["polygons"], got["angle"]), (3, None))

    def test_an_edge_beside_a_degenerate_polygon_has_no_angle(self):
        # FLAT's one quad has all four points on a line: no normal.
        mesh = joined(FLAT, PLANE)
        mesh["polygons"].append([0, 1, 5])
        mesh["types"].append("face")
        got = meshmath.edge_entry(mesh, (0, 1))
        self.assertEqual((got["polygons"], got["angle"]), (2, None))

    def test_a_cube_vertex(self):
        self.assertEqual(meshmath.vertex_entry(CUBE, 0),
                         {"vertex": 0, "position": [-0.5, -0.5, -0.5],
                          "valence": 3, "border": False})

    def test_a_plane_corner_is_on_the_border(self):
        got = meshmath.vertex_entry(PLANE, 0)
        self.assertEqual((got["valence"], got["border"]), (2, True))

    def test_a_degenerate_polygon_has_a_zero_normal(self):
        got = meshmath.polygon_entry(FLAT, 0)
        self.assertEqual((got["normal"], got["area"]), ([0.0, 0.0, 0.0], 0.0))

    def test_an_entrys_area_is_the_summarys(self):
        for index in range(6):
            alone = dict(SCALED, polygons=[SCALED["polygons"][index]],
                         types=["face"])
            self.assertEqual(meshmath.polygon_entry(SCALED, index)["area"],
                             meshmath.summary(alone)["area"])


class SelectionTest(unittest.TestCase):
    def test_entries_come_back_ascending(self):
        got = meshmath.selection(CUBE, "polygon", [5, 2], elements=True)
        self.assertEqual([e["polygon"] for e in got["elements"]], [2, 5])

    def test_edges_come_back_ascending_lower_first(self):
        got = meshmath.selection(CUBE, "edge", [(7, 6), (1, 0)], elements=True)
        self.assertEqual([e["edge"] for e in got["elements"]],
                         [[0, 1], [6, 7]])

    def test_a_shared_vertex_counts_once(self):
        # Bottom [0, 1, 2, 3] and back [4, 5, 1, 0] share 0 and 1: six
        # vertices, so y is (-0.5 * 4 + 0.5 * 2) / 6 and z is
        # (-0.5 * 4 + 0.5 * 2) / 6. Counted twice it would be -0.25.
        got = meshmath.selection(CUBE, "polygon", [0, 1])
        self.assertEqual(got, {"mode": "polygon", "count": 2,
                               "box": [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
                               "centroid": [0.0, -0.166667, -0.166667]})

    def test_the_cubes_top(self):
        got = meshmath.selection(CUBE, "polygon", [5])
        self.assertEqual(got["box"], [[-0.5, 0.5, -0.5], [0.5, 0.5, 0.5]])
        self.assertEqual(got["centroid"], [0.0, 0.5, 0.0])

    def test_an_edge_and_a_vertex(self):
        self.assertEqual(meshmath.selection(CUBE, "edge", [(0, 1)])["centroid"],
                         [0.0, -0.5, -0.5])
        self.assertEqual(meshmath.selection(CUBE, "vertex", [6])["box"],
                         [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]])

    def test_nothing_selected(self):
        self.assertEqual(meshmath.selection(CUBE, "vertex", []),
                         {"mode": "vertex", "count": 0})
        self.assertEqual(meshmath.selection(CUBE, "vertex", [], elements=True),
                         {"mode": "vertex", "count": 0, "elements": []})


# --- Surface facts, from the triangles Modo's own sampler hands out.
# It repeats a point once per polygon around it, so the cube's eight
# corners come as 24 points, three per corner, in 12 triangles.


def sampled(mesh):
    """mesh as the sampler gives it: each polygon fanned into triangles over
    points of its own."""
    points, triangles = [], []
    for poly in mesh["polygons"]:
        first = len(points)
        points.extend(tuple(mesh["positions"][i]) for i in poly)
        triangles.extend((first, first + k, first + k + 1)
                         for k in range(1, len(poly) - 1))
    return points, triangles


class SurfaceTest(unittest.TestCase):
    def test_the_cube_as_sampled(self):
        got = meshmath.surface(*sampled(CUBE), level=2, closed=True)
        self.assertEqual(got, {"level": 2, "triangles": 12,
                               "box": [[-0.5] * 3, [0.5] * 3],
                               "area": 6.0, "volume": 1.0})

    def test_copies_a_bit_apart_still_close_it(self):
        # Two patches' copies of a point differ in the last bits; the
        # volume doesn't notice.
        points, triangles = sampled(CUBE)
        points = [(x + 1e-8 * (i % 3), y, z) for i, (x, y, z) in enumerate(points)]
        got = meshmath.surface(points, triangles, level=2, closed=True)
        self.assertEqual(got["volume"], 1.0)

    def test_an_open_cage_has_no_volume(self):
        open_box = dict(CUBE, polygons=CUBE["polygons"][:5])
        got = meshmath.surface(*sampled(open_box), level=None, closed=False)
        self.assertEqual((got["area"], got["volume"]), (5.0, None))
        self.assertIsNone(got["level"])

    def test_nothing_sampled(self):
        got = meshmath.surface([], [], level=None, closed=True)
        self.assertEqual((got["triangles"], got["box"], got["area"], got["volume"]),
                         (0, None, 0.0, None))


def subd(mesh):
    return dict(mesh, types=["subd"] * len(mesh["polygons"]))


class RenderLevelTest(unittest.TestCase):
    """Modo's level L cuts each side of a SubD polygon into L, so an n-sided
    one is n * L * L quads, two triangles each; a plain n-gon is n - 2.
    The counts are the 1 m cube's, sampled on 17.1 (2026-11-09)."""

    def test_levels_one_two_three(self):
        cube = subd(CUBE)
        self.assertEqual(meshmath.render_level(cube, 48), 1)
        self.assertEqual(meshmath.render_level(cube, 192), 2)
        self.assertEqual(meshmath.render_level(cube, 432), 3)

    def test_a_mixed_cube(self):
        mixed = dict(CUBE, types=["face"] * 5 + ["subd"])
        self.assertEqual(meshmath.render_level(mixed, 42), 2)

    def test_pixar_subd_doubles(self):
        # The 1 m Pixar SubD cube on 17.1 (2026-11-10): 48, 192, 768 and
        # 3072 triangles at levels 1 to 4. 768 is SubD's level 4.
        cube = dict(CUBE, types=["psub"] * 6)
        for level, count in ((1, 48), (2, 192), (3, 768), (4, 3072)):
            self.assertEqual(meshmath.render_level(cube, count), level)
        self.assertIsNone(meshmath.render_level(cube, 432))
        self.assertEqual(meshmath.render_level(subd(CUBE), 768), 4)

    def test_pixar_beside_plain_faces(self):
        mixed = dict(CUBE, types=["face"] * 5 + ["psub"])
        self.assertEqual(meshmath.render_level(mixed, 10 + 128), 3)

    def test_both_kinds_have_no_one_level(self):
        both = dict(CUBE, types=["subd"] * 3 + ["psub"] * 3)
        self.assertIsNone(meshmath.render_level(both, 192))

    def test_a_count_no_level_makes(self):
        self.assertIsNone(meshmath.render_level(subd(CUBE), 100))

    def test_no_subd_no_level(self):
        self.assertIsNone(meshmath.render_level(CUBE, 12))

    def test_has_surface(self):
        self.assertFalse(meshmath.has_surface(CUBE))
        self.assertTrue(meshmath.has_surface(dict(CUBE, types=["face"] * 5 + ["psub"])))


if __name__ == "__main__":
    unittest.main()
