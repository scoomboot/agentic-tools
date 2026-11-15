"""describe_mesh and read_mesh in the kit, on fake meshes built from the
same lists meshmath's tests use."""

import json
import math
import sys
import unittest
from unittest import mock

import fakelx
import support
from test_meshmath import CUBE
from test_scene import job


def cube(scene, item_id="mesh002", name="Cube", parent=None):
    item = scene.add(item_id, name, "mesh", parent=parent)
    item.mesh = fakelx.FakeMesh(CUBE["positions"], CUBE["polygons"],
                                CUBE["types"], CUBE["edges"])
    return item


class ReadMeshTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)

    def read(self, item):
        return sys.modules["modotools.ops_mesh"].read_mesh(item)

    def test_lists_round_trip(self):
        got = self.read(self.cube)
        self.assertEqual(got["positions"], [list(p) for p in CUBE["positions"]])
        self.assertEqual(got["polygons"], CUBE["polygons"])
        self.assertEqual(got["types"], CUBE["types"])
        self.assertEqual(got["edges"], [list(e) for e in CUBE["edges"]])

    def test_positions_are_in_world_space(self):
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        self.cube.world = (((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c)),
                           (3.0, 0.0, 0.0))
        got = self.read(self.cube)["positions"]
        # Vertex 1 is (0.5, -0.5, -0.5): turned 30 degrees about Y, moved 3.
        x, y, z = got[1]
        self.assertAlmostEqual(x, 3.0 + 0.5 * c - 0.5 * s)
        self.assertAlmostEqual(y, -0.5)
        self.assertAlmostEqual(z, -0.5 * s - 0.5 * c)

    def test_the_base_mesh_not_the_deformed_one(self):
        self.cube.deformed = fakelx.FakeMesh([(9, 9, 9)], [], [], [])
        self.assertEqual(len(self.read(self.cube)["positions"]), 8)

    def test_polygon_types_by_name(self):
        self.cube.mesh.types = ["subd", "subd", "psub", "face", "curve", "text"]
        self.assertEqual(self.read(self.cube)["types"],
                         ["subd", "subd", "psub", "face", "curve", "text"])

    def test_reads_at_the_current_time(self):
        self.fake.scene.time = 2.5
        self.read(self.cube)
        self.assertEqual({t for _a, t in self.fake.channel_reads}, {2.5})

    def test_nothing_is_written(self):
        self.read(self.cube)
        self.assertEqual(self.fake.mesh_writes, [])


class DescribeMeshTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        scene = self.fake.scene
        self.cube = cube(scene)
        self.cube.world = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                           (3.0, 0.0, 0.0))
        self.locator = scene.add("locator003", "Null", "locator")
        self.camera = scene.add("camera004", "Camera", "camera")
        self.empty = scene.add("mesh005", "Empty", "mesh")
        self.empty.mesh = fakelx.FakeMesh([], [], [], [])

    def describe(self, payload=None):
        return job(self.kit, "describe_mesh", payload)

    def test_the_cube(self):
        answer = self.describe({"items": ["Cube"]})
        self.assertTrue(answer["ok"], answer)
        # Modo starts in item mode: no selection in the summary.
        self.assertEqual(answer["result"], {
            "meshes": [{"id": "mesh002", "name": "Cube", "vertices": 8,
                        "edges": 12, "polygons": 6, "sides": {"4": 6},
                        "poles": {"3": 8}, "boundary_loops": 0,
                        "boundary_edges": 0, "parts": 1,
                        "box": [[2.5, -0.5, -0.5], [3.5, 0.5, 0.5]],
                        "area": 6.0, "volume": 1.0}],
            "space": "world", "units": "m"})

    def test_the_same_cube_twice_is_the_same_text(self):
        first = json.dumps(self.describe({"items": ["Cube"]})["result"])
        self.assertEqual(json.dumps(self.describe({"items": ["Cube"]})["result"]),
                         first)

    def test_an_empty_mesh_has_no_volume(self):
        got = self.describe({"items": ["Empty"]})["result"]["meshes"][0]
        self.assertEqual((got["parts"], got["area"], got["volume"]),
                         (0, 0.0, None))

    def test_one_name_as_a_string(self):
        got = self.describe({"items": "mesh002"})["result"]
        self.assertEqual([m["id"] for m in got["meshes"]], ["mesh002"])

    def test_an_empty_mesh_has_no_box(self):
        got = self.describe({"items": ["Empty"]})["result"]["meshes"][0]
        self.assertEqual(got["vertices"], 0)
        self.assertIsNone(got["box"])

    def test_types_when_any_polygon_isnt_a_face(self):
        self.cube.mesh.types = ["subd"] * 6
        got = self.describe({"items": ["Cube"]})["result"]["meshes"][0]
        self.assertEqual(got["types"], {"subd": 6})

    def test_a_locator_is_not_a_mesh(self):
        err = self.describe({"items": ["Null"]})["error"]
        self.assertEqual(err["type"], "not_a_mesh")
        self.assertEqual(err["message"], "Null is a locator, not a mesh")

    def test_a_camera_is_not_a_mesh(self):
        err = self.describe({"items": ["Camera"]})["error"]
        self.assertEqual(err["message"], "Camera is a camera, not a mesh")

    def test_an_before_a_vowel(self):
        self.fake.scene.add("light007", "Area", "areaLight")
        err = self.describe({"items": ["Area"]})["error"]
        self.assertEqual(err["message"], "Area is an areaLight, not a mesh")

    def test_a_mesh_instance_names_its_source(self):
        inst = self.fake.scene.add("meshInst006", "Cube (2)", "meshInst")
        inst.source = self.cube
        err = self.describe({"items": ["meshInst006"]})["error"]
        self.assertEqual(err["type"], "not_a_mesh")
        self.assertEqual(err["message"], "Cube (2) is a mesh instance, not a mesh")
        self.assertIn("mesh002 (Cube)", err["hint"])

    def test_no_such_item(self):
        err = self.describe({"items": ["Nothing"]})["error"]
        self.assertEqual(err["type"], "no_such_item")

    def test_nothing_selected_means_every_mesh(self):
        got = self.describe()["result"]
        self.assertEqual([m["id"] for m in got["meshes"]], ["mesh002", "mesh005"])

    def test_the_selected_meshes(self):
        self.fake.scene.selected = [self.camera, self.empty]
        got = self.describe({"items": None})["result"]
        self.assertEqual([m["id"] for m in got["meshes"]], ["mesh005"])

    def test_an_item_asked_twice_is_described_once(self):
        got = self.describe({"items": ["Cube", "mesh002"]})["result"]
        self.assertEqual(len(got["meshes"]), 1)

    def test_an_unknown_detail_or_mode(self):
        self.assertEqual(self.describe({"detail": "all"})["error"]["type"],
                         "bad_request")
        self.assertEqual(self.describe({"mode": "face"})["error"]["type"],
                         "bad_request")

    def test_nothing_is_written(self):
        self.describe()
        self.assertEqual(self.fake.mesh_writes, [])


class SelectionTest(unittest.TestCase):
    """The selection read in a mode, and the size limit."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)
        self.mesh = self.cube.mesh
        self.fake.scene.select_mode = "polygon"

    def describe(self, payload=None):
        return job(self.kit, "describe_mesh", dict({"items": ["Cube"]},
                                                   **(payload or {})))

    def mesh_of(self, answer):
        self.assertTrue(answer["ok"], answer)
        return answer["result"]["meshes"][0]

    def test_the_cubes_top_in_the_summary(self):
        self.mesh.select("polygon", [5])
        got = self.mesh_of(self.describe())["selection"]
        self.assertEqual(got, {"mode": "polygon", "count": 1,
                               "box": [[-0.5, 0.5, -0.5], [0.5, 0.5, 0.5]],
                               "centroid": [0.0, 0.5, 0.0]})

    def test_the_cubes_top_as_an_element(self):
        self.mesh.select("polygon", [5])
        got = self.mesh_of(self.describe({"detail": "selection"}))
        self.assertEqual(got["selection"]["elements"],
                         [{"polygon": 5, "sides": 4, "normal": [0.0, 1.0, 0.0],
                           "centroid": [0.0, 0.5, 0.0], "area": 1.0}])

    def test_each_mode_reads_its_own_selection(self):
        self.mesh.select("polygon", [5])
        self.mesh.select("edge", [(1, 0)])
        self.mesh.select("vertex", [0, 6])
        for mode, count in (("polygon", 1), ("edge", 1), ("vertex", 2)):
            self.fake.scene.select_mode = mode
            got = self.mesh_of(self.describe())["selection"]
            self.assertEqual((got["mode"], got["count"]), (mode, count))

    def test_an_edge_by_its_vertices_lower_first(self):
        self.mesh.select("edge", [(5, 6)])
        got = self.mesh_of(self.describe({"detail": "selection",
                                          "mode": "edge"}))
        self.assertEqual(got["selection"]["elements"],
                         [{"edge": [5, 6], "length": 1.0, "polygons": 2,
                           "angle": 90.0}])

    def test_mode_overrides_modos(self):
        self.mesh.select("vertex", [0])
        got = self.mesh_of(self.describe({"detail": "selection",
                                          "mode": "vertex"}))
        self.assertEqual(got["selection"]["elements"],
                         [{"vertex": 0, "position": [-0.5, -0.5, -0.5],
                           "valence": 3, "border": False}])

    def test_nothing_selected(self):
        got = self.mesh_of(self.describe({"detail": "selection"}))
        self.assertEqual(got["selection"],
                         {"mode": "polygon", "count": 0, "elements": []})

    def test_item_mode_leaves_the_selection_out(self):
        self.fake.scene.select_mode = "item"
        self.mesh.select("polygon", [5])
        self.assertNotIn("selection", self.mesh_of(self.describe()))

    def test_item_mode_with_a_mode_reads_it(self):
        self.fake.scene.select_mode = "item"
        self.mesh.select("polygon", [5])
        got = self.mesh_of(self.describe({"mode": "polygon"}))["selection"]
        self.assertEqual(got["count"], 1)

    def test_item_mode_refuses_detail_selection_without_a_mode(self):
        self.fake.scene.select_mode = "item"
        err = self.describe({"detail": "selection"})["error"]
        self.assertEqual(err["type"], "bad_request")
        self.assertIn("item mode", err["message"])
        self.assertIn("mode", err["hint"])

    def test_the_selection_not_the_select_marks(self):
        # As on 17.1 (2026-11-08): every polygon marked, one selected.
        self.mesh.marks["polygon"] = set(range(6))
        self.mesh.select("polygon", [5])
        got = self.mesh_of(self.describe())["selection"]
        self.assertEqual(got["count"], 1)

    def test_another_meshs_selection_is_left_out(self):
        other = cube(self.fake.scene, "mesh009", "Other")
        other.mesh.select("polygon", [0, 1])
        self.mesh.select("polygon", [5])
        got = self.mesh_of(self.describe())["selection"]
        self.assertEqual(got["count"], 1)

    def test_too_large_before_any_entry_is_built(self):
        self.mesh.select("polygon", [0, 1, 2, 3])
        err = self.describe({"detail": "selection", "max_elements": 3})["error"]
        self.assertEqual(err["type"], "too_large")
        self.assertEqual(err["message"], "4 polygons selected; 3 fit")
        self.assertIn("detail=\"summary\"", err["hint"])
        # One selection packet per selected polygon and nothing else: no
        # position read, no polygon's vertices.
        self.assertEqual(self.fake.packet_reads, 4)
        self.assertNotIn("Point.Pos", self.mesh.calls)
        self.assertNotIn("Polygon.VertexByIndex", self.mesh.calls)

    def test_the_count_is_over_every_mesh(self):
        other = cube(self.fake.scene, "mesh009", "Other")
        other.mesh.select("polygon", [0, 1])
        self.mesh.select("polygon", [0, 1])
        err = self.describe({"items": ["Cube", "Other"], "detail": "selection",
                             "max_elements": 3})["error"]
        self.assertEqual(err["message"], "4 polygons selected; 3 fit")
        self.assertNotIn("Point.Pos", self.mesh.calls)
        self.assertNotIn("Point.Pos", other.mesh.calls)

    def test_exactly_max_elements_is_answered(self):
        self.mesh.select("polygon", [0, 1, 2])
        got = self.mesh_of(self.describe({"detail": "selection",
                                          "max_elements": 3}))
        self.assertEqual(len(got["selection"]["elements"]), 3)

    def test_one_vertex_too_many(self):
        self.mesh.select("vertex", [0, 1])
        err = self.describe({"detail": "selection", "mode": "vertex",
                             "max_elements": 1})["error"]
        self.assertEqual(err["message"], "2 vertices selected; 1 fits")

    def test_the_summary_is_never_refused(self):
        self.mesh.select("polygon", [0, 1, 2, 3])
        got = self.mesh_of(self.describe({"max_elements": 3}))
        self.assertEqual(got["selection"]["count"], 4)

    def test_nothing_is_written(self):
        self.mesh.select("edge", [(0, 1)])
        self.describe({"detail": "selection", "mode": "edge"})
        self.assertEqual(self.fake.mesh_writes, [])


def split_cube(scale=0.8):
    """A stand-in for a SubD cube's surface at level 1: each face of a cube
    `scale` m on a side cut into four quads, sampled as Modo samples, each
    point repeated per polygon and each face a segment of its own: 24
    quads, 96 points, 48 triangles."""
    h = scale / 2
    quads = []
    for axis in range(3):
        for sign in (-h, h):
            for a0, a1 in ((-h, 0.0), (0.0, h)):
                for b0, b1 in ((-h, 0.0), (0.0, h)):
                    # (a, b) runs along the next two axes in turn, so this
                    # order faces +axis; the face at -h is turned round.
                    corners = [(a0, b0), (a1, b0), (a1, b1), (a0, b1)]
                    if sign < 0:
                        corners.reverse()
                    quad = []
                    for a, b in corners:
                        p = [0.0, 0.0, 0.0]
                        p[axis] = sign
                        p[(axis + 1) % 3], p[(axis + 2) % 3] = a, b
                        quad.append(tuple(p))
                    quads.append(quad)
    segments = []
    for face in range(6):
        points, triangles = [], []
        for quad in quads[face * 4:face * 4 + 4]:
            first = len(points)
            points.extend(quad)
            triangles += [(first, first + 1, first + 2),
                          (first, first + 2, first + 3)]
        segments.append((points, triangles))
    return [segments]


class SurfaceTest(unittest.TestCase):
    """Surface=true adds the surface Modo's own sampler gives."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)
        self.cube.world = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                           (3.0, 0.0, 0.0))
        self.ops = sys.modules["modotools.ops_mesh"]

    def subd(self):
        self.cube.mesh.types = ["subd"] * 6
        self.cube.surface = split_cube()

    def describe(self, payload):
        return job(self.kit, "describe_mesh", dict({"items": ["Cube"]}, **payload))

    def test_a_subd_cube(self):
        self.subd()
        got = self.describe({"surface": True})["result"]["meshes"][0]
        # The cage's box, and inside it the surface's, moved 3 m in x by
        # the item: the sampler's points are in the item's own space.
        self.assertEqual(got["box"], [[2.5, -0.5, -0.5], [3.5, 0.5, 0.5]])
        self.assertEqual(got["surface"], {
            "level": 1, "triangles": 48,
            "box": [[2.6, -0.4, -0.4], [3.4, 0.4, 0.4]],
            "area": 3.84, "volume": 0.512})

    def test_the_base_mesh_is_sampled(self):
        self.subd()
        self.describe({"surface": True})
        self.assertEqual(self.fake.surface_samples, [("mesh002", "edit")])

    def test_a_plain_mesh_has_null_and_isnt_sampled(self):
        self.cube.surface = split_cube()
        got = self.describe({"surface": True})["result"]["meshes"][0]
        self.assertIn("surface", got)
        self.assertIsNone(got["surface"])
        self.assertEqual(self.fake.surface_samples, [])

    def test_left_out_without_asking(self):
        self.subd()
        got = self.describe({})["result"]["meshes"][0]
        self.assertNotIn("surface", got)
        self.assertEqual(self.fake.surface_samples, [])

    def test_a_level_cant_be_asked(self):
        self.subd()
        err = self.describe({"surface": True, "surface_level": 1})["error"]
        self.assertEqual(err["type"], "bad_request")
        self.assertIn("render level", err["hint"])

    def test_surface_is_true_or_false(self):
        self.subd()
        err = self.describe({"surface": "false"})["error"]
        self.assertEqual(err["type"], "bad_request")
        self.assertEqual(self.fake.surface_samples, [])

    def test_too_large_stops_the_sample(self):
        self.subd()
        with mock.patch.object(self.ops, "SURFACE_MAX_POINTS", 50):
            err = self.describe({"surface": True})["error"]
        self.assertEqual(err["type"], "too_large")
        self.assertIn("50", err["message"])
        self.assertIn("render level", err["hint"])
        # Stopped at the first point past the limit, not after all 96.
        self.assertEqual(self.fake.sampled_points, 51)

    def test_an_open_cage_gives_no_volume(self):
        self.subd()
        self.cube.mesh.polygons = self.cube.mesh.polygons[:5]
        self.cube.mesh.types = ["subd"] * 5
        got = self.describe({"surface": True})["result"]["meshes"][0]
        self.assertIsNone(got["surface"]["volume"])

    def test_exactly_at_the_limit(self):
        self.subd()
        with mock.patch.object(self.ops, "SURFACE_MAX_POINTS", 96):
            got = self.describe({"surface": True})
        self.assertTrue(got["ok"], got)

    def test_nothing_is_written(self):
        self.subd()
        self.describe({"surface": True})
        self.assertEqual(self.fake.mesh_writes, [])


class ThroughExecReadTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        cube(self.fake.scene)

    def test_describe_runs_through_exec_read(self):
        client = support.Client(self.kit.port)
        self.addCleanup(client.close)
        client.hello()
        del self.fake.ran[:]
        asked = support.Asked(client, {"id": "r-1", "op": "describe_mesh",
                                       "payload": {"items": None}})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        self.assertTrue(asked.answer["ok"], asked.answer)
        self.assertIn("modoTools.execRead", self.fake.ran)
        self.assertNotIn("modoTools.exec", self.fake.ran)
        self.assertEqual(self.fake.mesh_writes, [])


if __name__ == "__main__":
    unittest.main()
