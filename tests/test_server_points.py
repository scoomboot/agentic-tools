"""On the server: mt_points and mt_set_points, one point per line
within the size budget, and what their descriptions promise."""

import json
import os
import unittest
from unittest import mock

import support
from modo_tools import bridge as bridge_mod
from modo_tools import budget, server
from test_mesh import cube
from test_meshmath import CUBE


def big_mesh(scene, count):
    import fakelx
    item = scene.add("mesh009", "Big", "mesh")
    item.mesh = fakelx.FakeMesh(
        [(-1234.567891 - i, -1234.567891, -1234.567891) for i in range(count)],
        [], [], [])
    return item


class PointsToolTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)
        self.before = [list(p) for p in self.cube.mesh.positions]
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def ask(self, coro):
        from test_server import InThread
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_every_point_one_line_each(self):
        text = self.ask(server.mt_points_text("Cube", bridge=self.bridge))
        shown = json.loads(text)
        self.assertEqual(shown["points"],
                         [[i, list(p)] for i, p in enumerate(CUBE["positions"])])
        self.assertEqual(shown["space"], "mesh")
        self.assertNotIn("undo", shown)
        self.assertIn('\n    [0, [-0.5, -0.5, -0.5]],\n', text)

    def test_polygons_one_line_each(self):
        text = self.ask(server.mt_points_text("Cube", polygons=True,
                                              bridge=self.bridge))
        self.assertEqual(json.loads(text)["polygons"], CUBE["polygons"])
        self.assertIn("\n    [%s],\n" % ", ".join(
            str(v) for v in CUBE["polygons"][0]), text)

    def test_the_kit_is_given_the_budget(self):
        calls = []
        real = self.bridge.call

        async def call(op, payload, **kw):
            calls.append((op, payload))
            return await real(op, payload, **kw)
        self.bridge.call = call
        self.ask(server.mt_points_text("Cube", start=2, count=3,
                                       bridge=self.bridge))
        self.assertEqual(calls, [("points", {
            "item": "Cube", "start": 2, "count": 3, "polygons": False,
            "max_chars": budget.max_chars()})])

    def test_the_worst_points_fit_the_budget(self):
        # The longest lines a point can have, as many as the kit says fit.
        big_mesh(self.fake.scene, 5000)
        with mock.patch.dict(os.environ, {"MODO_TOOLS_MAX_CHARS": "20000"}):
            text = self.ask(server.mt_points_text("Big", count=5000,
                                                  bridge=self.bridge))
        self.assertLessEqual(len(text), 20000)
        shown = json.loads(text)
        self.assertGreater(len(shown["points"]), 250)
        self.assertEqual(shown["cut"]["next_start"], len(shown["points"]))

    def test_too_many_with_no_count_is_refused(self):
        big_mesh(self.fake.scene, 5000)
        text = self.ask(server.mt_points_text("Big", bridge=self.bridge))
        self.assertTrue(text.startswith("too_large:"), text)
        self.assertIn("count", text)

    def test_set_points_writes_and_is_one_step(self):
        text = self.ask(server.mt_set_points_text(
            "Cube", {"3": [0.5, 0.75, -0.5]}, bridge=self.bridge))
        shown = json.loads(text)
        self.assertEqual(shown["written"], 1)
        self.assertEqual(shown["undo"], {"step": 1})
        self.assertEqual(shown["journal"], 1)
        self.assertEqual(list(self.cube.mesh.positions[3]), [0.5, 0.75, -0.5])

    def test_a_refusal_says_nothing_was_written(self):
        text = self.ask(server.mt_set_points_text(
            "Cube", {"8": [0, 0, 0]}, bridge=self.bridge))
        self.assertTrue(text.startswith("bad_request:"), text)
        self.assertIn("Nothing was written", text)
        self.assertEqual([list(p) for p in self.cube.mesh.positions], self.before)


class ToolSchemaTest(unittest.TestCase):
    """The review: the tool's types refused a wrong shape before the kit
    saw it, with the mcp package's validation text and no word of what to
    send. The kit checks every value, so the tool passes points on."""

    def test_a_wrong_shape_reaches_the_kit(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        import asyncio
        sent = []
        real = server.mt_set_points_text

        async def text(item, points, bridge=None):
            sent.append(points)
            return "seen"

        server.mt_set_points_text = text
        self.addCleanup(setattr, server, "mt_set_points_text", real)
        app = server.build_server()
        shapes = ([[0, [1, 1, 1]], [1, [2, 2, 2]]], {"0": [0, 0, None]},
                  {"0": [0, 0]}, [[0, 0, "a"]])
        for points in shapes:
            asyncio.run(app.call_tool("mt_set_points",
                                      {"item": "Cube", "points": points}))
        self.assertEqual(sent, list(shapes))


class DescriptionTest(unittest.TestCase):
    def test_points(self):
        for words in ("index", "mesh's own space", "metres", "polygons=true",
                      "start", "count", "too_large", "before deformers",
                      "mt_get_item", "no undo"):
            self.assertIn(words, server.POINTS_DESCRIPTION)

    def test_set_points(self):
        for words in ('{"12": [x, y, z], ...}',"index order", "checked before any",
                      "Nothing was written", "One undo step", "mt_undo_last",
                      "mt_begin", "mesh's own space", "mt_points",
                      "not the [index, [x, y, z]] pairs"):
            self.assertIn(words, server.SET_POINTS_DESCRIPTION)

    def test_run_python_points_to_them(self):
        for words in ("mt_points", "mt_set_points", "modo.MeshGeometry"):
            self.assertIn(words, server.RUN_PYTHON_DESCRIPTION)

    def test_the_journal_lists_set_points(self):
        self.assertIn("mt_set_points", server.JOURNAL_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
