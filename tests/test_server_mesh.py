"""mt_describe_mesh on the server side: its arguments, and many meshes cut
whole, in order, to the size budget."""

import asyncio
import inspect
import json
import os
import unittest
from unittest import mock

import support
from modo_tools import bridge as bridge_mod
from modo_tools import server
from modotools import meshmath
from test_mesh import cube, split_cube
from test_server import InThread


def many_meshes(count):
    return {"meshes": [{"id": "mesh%04d" % i, "name": "Part %d" % i,
                        "vertices": 8, "edges": 12, "polygons": 6,
                        "box": [[i - 0.5, -0.5, -0.5], [i + 0.5, 0.5, 0.5]]}
                       for i in range(count)],
            "space": "world", "units": "m"}


class FitTest(unittest.TestCase):
    def test_200_meshes_are_cut_whole_in_order(self):
        text = server.fit_meshes(many_meshes(200), 10000)
        self.assertLessEqual(len(text), 10000)
        shown = json.loads(text)
        kept = shown["meshes"]
        self.assertGreater(len(kept), 0)
        self.assertLess(len(kept), 200)
        self.assertEqual([m["id"] for m in kept],
                         ["mesh%04d" % i for i in range(len(kept))])
        for mesh in kept:
            self.assertEqual(set(mesh), {"id", "name", "vertices", "edges",
                                         "polygons", "box"})
        self.assertEqual(shown["truncated"],
                         "%d of 200 meshes shown; pass items= to ask for "
                         "the rest by id" % len(kept))
        self.assertEqual(shown["space"], "world")

    def test_a_few_meshes_are_left_whole(self):
        few = many_meshes(3)
        self.assertEqual(json.loads(server.fit_meshes(few, 10000)), few)


def far_entries():
    """The longest entries meshmath can give: far from the origin, every
    digit used, indices in the millions."""
    far = {"positions": [(-1234.567891, -1234.567891, -1234.567891),
                         (-1234.123457, -1234.987654, -1234.567891),
                         (-1234.555555, -1234.333333, -1234.111111)],
           "polygons": [[0, 1, 2]], "types": ["face"],
           "edges": [(0, 1), (1, 2), (0, 2)]}
    entries = [meshmath.polygon_entry(far, 0), meshmath.edge_entry(far, (0, 1)),
               meshmath.vertex_entry(far, 0)]
    for entry in entries:
        key = next(iter(entry))
        entry[key] = [9999998, 9999999] if key == "edge" else 9999999
    return entries


def with_elements(count, entry):
    return {"meshes": [{"id": "mesh002", "name": "Cube", "vertices": 8,
                        "selection": {"mode": "polygon", "count": count,
                                      "elements": [entry] * count}}],
            "space": "world", "units": "m"}


class MaxElementsTest(unittest.TestCase):
    def test_the_budget_over_150(self):
        with mock.patch.dict(os.environ, {"MODO_TOOLS_MAX_CHARS": "12000"}):
            self.assertEqual(server.max_elements(), 80)
        with mock.patch.dict(os.environ, {"MODO_TOOLS_MAX_CHARS": ""}):
            self.assertEqual(server.max_elements(), 266)

    def test_each_entry_is_one_line_under_150(self):
        text = server.render_meshes(with_elements(1, far_entries()[0]))
        for entry in far_entries():
            line = [l for l in server.render_meshes(
                with_elements(2, entry)).splitlines() if "999999" in l][0]
            self.assertLessEqual(len(line), 150, line)
            self.assertEqual(json.loads(line.strip().rstrip(",")), entry)
        self.assertEqual(json.loads(text), with_elements(1, far_entries()[0]))

    def test_max_elements_of_the_longest_entries_fit_the_budget(self):
        with mock.patch.dict(os.environ, {"MODO_TOOLS_MAX_CHARS": ""}):
            room = server.max_elements()
        for entry in far_entries():
            text = server.fit_meshes(with_elements(room, entry), 40000)
            self.assertEqual(len(json.loads(text)["meshes"][0]["selection"]
                                 ["elements"]), room)

    def test_max_elements_fit_beside_a_full_summary(self):
        # Every summary field at its widest, with the selection's box and
        # centroid: what ENTRY_CHARS leaves room for.
        wide = [-9999.123457] * 3
        full = {"id": "mesh0123", "name": "A mesh with a longer name",
                "vertices": 9999999, "edges": 9999999, "polygons": 9999999,
                "types": {"face": 9999990, "subd": 9},
                "sides": {"1-2": 1, "3": 999, "4": 9999990, "5+": 9},
                "poles": {"1-2": 1, "3": 999, "5+": 999},
                "boundary_loops": 999, "boundary_edges": 99999,
                "nonmanifold_edges": 9, "lone_vertices": 9, "zero_area": 9,
                "parts": 999, "box": [wide, wide], "area": 1.23457e+08,
                "volume": None}
        with mock.patch.dict(os.environ, {"MODO_TOOLS_MAX_CHARS": ""}):
            room = server.max_elements()
        for entry in far_entries():
            mesh = dict(full, selection={"mode": "polygon", "count": room,
                                         "box": [wide, wide], "centroid": wide,
                                         "elements": [entry] * room})
            text = server.fit_meshes({"meshes": [mesh], "space": "world",
                                      "units": "m"}, 40000)
            self.assertFalse(text.startswith("too_large"), text[:200])
            self.assertEqual(len(json.loads(text)["meshes"][0]["selection"]
                                 ["elements"]), room)

    def test_a_mesh_whose_entries_dont_fit_is_refused_not_cut(self):
        entry = far_entries()[0]
        text = server.fit_meshes(with_elements(400, entry), 40000)
        self.assertTrue(text.startswith("too_large:"), text[:200])
        self.assertIn("400 polygons", text)

    def test_meshes_with_elements_are_cut_whole(self):
        two = with_elements(200, far_entries()[0])
        two["meshes"].append(dict(two["meshes"][0], id="mesh003"))
        shown = json.loads(server.fit_meshes(two, 40000))
        self.assertEqual([m["id"] for m in shown["meshes"]], ["mesh002"])
        self.assertEqual(len(shown["meshes"][0]["selection"]["elements"]), 200)
        self.assertIn("1 of 2 meshes shown", shown["truncated"])


class ArgumentsTest(unittest.TestCase):
    def test_the_text_function_takes_items_detail_mode(self):
        params = inspect.signature(server.mt_describe_mesh_text).parameters
        self.assertEqual(params["items"].default, None)
        self.assertEqual(params["detail"].default, "summary")
        self.assertEqual(params["mode"].default, None)
        self.assertEqual(params["surface"].default, False)
        self.assertNotIn("surface_level", params)

    def test_the_tool_schema(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        app = server.build_server()

        async def tool():
            return [t for t in await app.list_tools()
                    if t.name == "mt_describe_mesh"][0]

        found = asyncio.run(tool())
        # The mcp SDK 2.x spells these in snake case, 1.x in camel case.
        schema = getattr(found, "input_schema", None) or found.inputSchema
        self.assertEqual(sorted(schema["properties"]),
                         ["detail", "items", "mode", "surface"])
        self.assertEqual(schema.get("required", []), [])
        self.assertTrue(getattr(found.annotations, "read_only_hint", None)
                        or getattr(found.annotations, "readOnlyHint", None))


class ThroughTheKitTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def ask(self, coro):
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_the_cube(self):
        text = self.ask(server.mt_describe_mesh_text("Cube", bridge=self.bridge))
        shown = json.loads(text)
        self.assertEqual(shown["meshes"][0]["vertices"], 8)
        self.assertEqual(shown["units"], "m")

    def test_max_elements_rides_with_the_request(self):
        self.fake.scene.select_mode = "polygon"
        self.cube.mesh.select("polygon", [0, 1, 2])
        with mock.patch.object(server, "max_elements", return_value=2):
            text = self.ask(server.mt_describe_mesh_text(
                "Cube", detail="selection", bridge=self.bridge))
        self.assertTrue(text.startswith("too_large: 3 polygons selected; 2 fit"),
                        text)
        # A bridge belongs to one event loop, and each ask runs its own.
        text = self.ask(server.mt_describe_mesh_text(
            "Cube", detail="selection",
            bridge=bridge_mod.Bridge(port=self.kit.port)))
        shown = json.loads(text)["meshes"][0]["selection"]
        self.assertEqual([e["polygon"] for e in shown["elements"]], [0, 1, 2])

    def test_surface_rides_with_the_request(self):
        self.cube.mesh.types = ["subd"] * 6
        self.cube.surface = split_cube()
        text = self.ask(server.mt_describe_mesh_text(
            "Cube", surface=True, bridge=self.bridge))
        got = json.loads(text)["meshes"][0]["surface"]
        self.assertEqual((got["level"], got["triangles"]), (1, 48))

    def test_not_a_mesh(self):
        self.fake.scene.add("camera009", "Camera", "camera")
        text = self.ask(server.mt_describe_mesh_text(["Camera"],
                                                     bridge=self.bridge))
        self.assertTrue(text.startswith("not_a_mesh:"), text)


if __name__ == "__main__":
    unittest.main()
