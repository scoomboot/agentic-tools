"""The fit_backdrops op in the kit, on fake lx. Two backdrops
read from the scene (view, pixel size, position, the image their clip
shows), a SubD mesh's points fitted, written as one undo step, the answer's
overlap before and after."""

import importlib
import json
import math
import os
import sys
import unittest

import fakelx
import support
from test_fit import ellipse_rows, sphere, WHITE

from modotools import fit

PIXEL = 0.025
SIZE = 100          # image pixels a side: 2.5 m


def rule_surface(item):
    """The surface fake Modo samples: the cage's quads, each corner at the
    rule's point, as two triangles; one bin, one segment."""
    mesh = item.mesh
    points = fit.rule([list(p) for p in mesh.positions],
                      [p for p, t in zip(mesh.polygons, mesh.types) if t == "subd"])
    triangles = []
    for polygon in mesh.polygons:
        for i in range(1, len(polygon) - 1):
            triangles.append((polygon[0], polygon[i], polygon[i + 1]))
    return [[(points, triangles)]]


class Base(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        # The kit's own fit module, as the op will import it.
        importlib.import_module("modotools.fit")
        self.home = os.environ["MODO_TOOLS_HOME"]
        scene = self.fake.scene
        # The blockout: the shape's height (1 m up and down), the wrong
        # widths. The cast keeps a point's height in the shape's middle.
        positions, polygons = sphere(5, 1.0)
        positions = [[0.6 * x, y, 0.6 * z] for x, y, z in positions]
        self.ball = scene.add("mesh001", "Ball", "mesh")
        self.ball.mesh = fakelx.FakeMesh(positions, polygons,
                                         ["subd"] * len(polygons))
        self.ball.surface = rule_surface
        self.front = self.backdrop("backdrop010", "front", "front",
                                   ellipse_rows(SIZE, SIZE, 0.5 / PIXEL, 1.0 / PIXEL))
        self.side = self.backdrop("backdrop020", "side", "left",
                                  ellipse_rows(SIZE, SIZE, 0.8 / PIXEL, 1.0 / PIXEL))
        self.before = [tuple(p) for p in self.ball.mesh.positions]
        # Through the socket and the pump, as in Modo: one undo step, and a
        # failure after the write rolled back.
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.n = 0

    def backdrop(self, ident, name, view, rows, center=(0.0, 0.0, 0.0),
                 linked=True):
        path = os.path.join(self.home, name + ".png").replace("\\", "/")
        with open(path, "wb") as handle:
            handle.write(fakelx.png(len(rows[0]) // 4, len(rows)))
        self.fake.image_pixels[path] = [bytes(r) for r in rows]
        scene = self.fake.scene
        item = scene.add(ident, name, "backdrop",
                         channels={"projection": view, "pixelSize": PIXEL})
        item.world = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                      tuple(center))
        if linked:
            clip = scene.add(ident + "_clip", name + ".png", "videoStill",
                             channels={"filename": path})
            self.fake.links.setdefault("backdropClip", []).append((item, clip))
        return item

    def fit(self, payload=None):
        body = {"item": "Ball"}
        body.update(payload or {})
        self.n += 1
        asked = support.Asked(self.client, {"id": "f-%d" % self.n,
                                            "op": "fit_backdrops",
                                            "payload": body})
        self.assertTrue(support.pump_until(self.fake, asked.done, timeout=30))
        return asked.answer

    def positions(self):
        return [tuple(p) for p in self.ball.mesh.positions]

    def nothing_written(self):
        self.assertEqual(self.fake.layer_scans, [])
        self.assertEqual(self.positions(), self.before)


# -- 1. refusals ----------------------------------------------------------------------

class RefusalTest(Base):
    def refused(self, payload=None, kind="bad_request", words=()):
        answer = self.fit(payload)
        self.assertFalse(answer["ok"], answer)
        err = answer["error"]
        self.assertEqual(err["type"], kind, err)
        for word in words:
            self.assertIn(word, err["message"] + " " + err.get("hint", ""))
        self.nothing_written()
        return err

    def test_fewer_than_two_backdrops_in_the_scene(self):
        self.fake.scene.items.remove(self.side)
        self.refused(words=("1 backdrop", "two"))

    def test_fewer_than_two_named(self):
        self.refused({"backdrops": ["front"]}, words=("two",))

    def test_more_than_two_in_the_scene_and_none_named(self):
        self.backdrop("backdrop030", "top", "top",
                      ellipse_rows(SIZE, SIZE, 10, 10))
        self.refused(words=("3 backdrops", "backdrops"))

    def test_two_in_the_same_view(self):
        self.side.channels["projection"].set("front")
        self.refused(words=("view",))

    def test_two_in_opposite_views(self):
        self.side.channels["projection"].set("back")
        self.refused(words=("view",))

    def test_a_backdrop_with_no_image(self):
        self.fake.links["backdropClip"] = [
            (b, c) for b, c in self.fake.links["backdropClip"] if b is not self.side]
        self.refused(words=("side", "no image"))

    def test_an_image_with_no_shape(self):
        path = self.fake.links["backdropClip"][1][1].channels["filename"].get()
        self.fake.image_pixels[path] = [bytes(WHITE * SIZE)] * SIZE
        self.refused(words=("side", "no shape"))

    def test_a_mesh_with_no_subd_polygon(self):
        self.ball.mesh.types = ["face"] * len(self.ball.mesh.types)
        self.refused(words=("SubD",))

    def test_selected_polygons_none_subd(self):
        self.ball.mesh.types[0] = "face"
        self.fake.scene.select_mode = "polygon"
        self.ball.mesh.select("polygon", [0])
        self.refused(words=("SubD",))

    def test_no_such_item(self):
        self.refused({"item": "Nope"}, "no_such_item")

    def test_no_such_backdrop(self):
        self.refused({"backdrops": ["front", "Nope"]}, "no_such_item")

    def test_a_named_backdrop_that_isnt_one(self):
        self.refused({"backdrops": ["front", "Ball"]},
                     words=("Ball", "not a backdrop"))

    def test_an_item_that_isnt_a_mesh(self):
        self.refused({"item": "front"}, "not_a_mesh")

    def test_bad_numbers(self):
        for payload in ({"smooth": -0.1}, {"smooth": 1.5}, {"smooth": "x"},
                        {"iterations": 0}, {"iterations": 1.5},
                        {"iterations": 500}, {"floor": "low"},
                        {"floor": float("nan")}):
            self.fake.layer_scans[:] = []
            self.refused(payload, words=(list(payload)[0],))


# -- 2. the fit -------------------------------------------------------------------------

def ellipsoid(p):
    return (p[0] / 0.5) ** 2 + p[1] ** 2 + (p[2] / 0.8) ** 2


class FitTest(Base):
    def test_the_surface_lands_on_the_shape(self):
        answer = self.fit()
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        self.assertEqual(result["fitted"], len(self.before))
        surface = fit.rule([list(p) for p in self.positions()],
                           self.ball.mesh.polygons)
        for s in surface:
            # On the ellipsoid within a few percent (the slices are
            # superellipses, fuller than an ellipse between the axes).
            self.assertLess(abs(math.sqrt(ellipsoid(s)) - 1.0), 0.1, s)
        self.assertLess(result["off_target_m"], 0.001)

    def test_the_answer(self):
        result = self.fit()["result"]
        self.assertEqual(result["item"], "mesh001")
        self.assertEqual(result["backdrops"], ["backdrop010", "backdrop020"])
        self.assertEqual(sorted(result["iou"]), ["front", "left"])
        for view, (before, after) in result["iou"].items():
            self.assertGreater(after, before, view)
            self.assertGreater(after, 0.85, view)
        moved = max(math.dist(a, b) for a, b in zip(self.before, self.positions()))
        self.assertAlmostEqual(result["max_move_m"], moved, places=5)
        self.assertEqual(result["space"], "world")
        self.assertNotIn("_written", result)

    def test_one_undo_step_puts_the_cage_back(self):
        answer = self.fit()
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertNotEqual(self.positions(), self.before)
        self.fake.undo()
        self.assertEqual(self.positions(), self.before)

    def test_floor_holds(self):
        self.assertTrue(self.fit({"floor": -0.5})["ok"])
        self.assertGreaterEqual(min(p[1] for p in self.positions()), -0.5 - 1e-9)
        # Without it the bottom goes lower.
        self.fake.undo()
        self.fit()
        self.assertLess(min(p[1] for p in self.positions()), -0.9)

    def test_only_the_selected_polygons_points_move(self):
        self.fake.scene.select_mode = "polygon"
        self.ball.mesh.select("polygon", [0])
        result = self.fit()["result"]
        self.assertEqual(result["fitted"], 4)
        corners = set(self.ball.mesh.polygons[0])
        for i, (a, b) in enumerate(zip(self.before, self.positions())):
            if i not in corners:
                self.assertEqual(a, b)

    def test_a_moved_and_turned_mesh_fits_in_world_space(self):
        # Turned 90 degrees about Y and moved up 0.25: the mesh's own space
        # isn't the world's, and the fit is in the world's.
        self.ball.world = (((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
                           (0.0, 0.25, 0.0))
        answer = self.fit()
        self.assertTrue(answer["ok"], answer)
        # Written where the rule said, in the mesh's own space: Modo's
        # surface was where the solve put it, with no rounds to correct.
        self.assertNotIn("measured_rounds", answer["result"])
        self.assertLess(answer["result"]["off_target_m"], 0.005)
        (m, off) = self.ball.world
        world = fakelx_world(self.positions(), m, off)
        surface = fit.rule(world, self.ball.mesh.polygons)
        for s in surface:
            self.assertLess(abs(math.sqrt(ellipsoid(s)) - 1.0), 0.1, s)

    def test_a_blockout_shorter_than_the_shape_gets_a_note(self):
        # A point keeps its height in the shape's middle, so a short cage
        # ends short: the answer says so.
        self.ball.mesh.positions = [(x, 0.6 * y, z) for x, y, z in
                                    self.ball.mesh.positions]
        result = self.fit()["result"]
        self.assertIn("height", result["note"])
        self.assertIn("Y", result["note"])

    def test_a_second_pass_along_the_normals_reaches_past_the_cage(self):
        # The centre cast keeps a point's height; the second pass, along
        # each surface point's normal, lifts a short cage's top toward the
        # shape's (the tip of a tail).
        self.ball.mesh.positions = [(x, 0.6 * y, z) for x, y, z in
                                    self.ball.mesh.positions]
        self.fit()
        surface = fit_module().rule([list(p) for p in self.positions()],
                                    self.ball.mesh.polygons)
        # 0.57 from the first pass alone; 0.73 with the second.
        self.assertGreater(max(s[1] for s in surface), 0.65)

    def test_a_blockout_at_the_shapes_height_gets_none(self):
        self.assertNotIn("note", self.fit()["result"])

    def test_a_surface_off_the_rule_is_fitted_from_modos_own_points(self):
        # Where Modo's surface isn't where the rule says (here 2 mm up),
        # the solve goes on from Modo's sampled points.
        def shifted(item):
            ((points, triangles),), = rule_surface(item)
            return [[([[p[0], p[1] + 0.002, p[2]] for p in points],
                      triangles)]]
        self.ball.surface = shifted
        result = self.fit()["result"]
        self.assertGreaterEqual(result["measured_rounds"], 1)
        # Under the 2 mm shift: what's left is the solve's own.
        self.assertLess(result["off_target_m"], 0.001)

    def test_a_flat_transform_is_refused(self):
        self.ball.world = (((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                           (0.0, 0.0, 0.0))
        answer = self.fit()
        self.assertEqual(answer["error"]["type"], "bad_request", answer)
        self.assertIn("scale", answer["error"]["hint"])
        self.nothing_written()

    def test_the_journal_holds_the_points_written(self):
        answer = self.fit()
        folder = os.path.join(self.home, "journals")
        entries = []
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                entries += [json.loads(line) for line in handle if line.strip()]
        entry = [e for e in entries if e.get("tool") == "fit_backdrops"][-1]
        self.assertEqual(entry["points_item"], "mesh001")
        self.assertEqual(len(entry["points"]), answer["result"]["fitted"])
        for index, pos in entry["points"].items():
            self.assertEqual(tuple(pos), self.positions()[int(index)])


class ReplayTest(Base):
    def test_the_replay_sets_the_points_as_values(self):
        from modo_tools import journal as replay
        self.fit()
        fitted = self.positions()
        name = self.kit.journal.name
        path = os.path.join(self.home, "journals", name + ".jsonl")
        with open(path) as handle:
            records = [json.loads(line) for line in handle]
        [entry] = [r for r in records if r.get("event") == "entry"]
        self.assertEqual(entry["tool"], "fit_backdrops")
        self.assertEqual(replay.label(entry), "Ball")
        text = replay.build(name, records)
        self.assertIn("_points('mesh001', ", text)
        self.assertIn("# Exact: yes.", text)
        # As in a new scene: the ball as it was, then the replay.
        self.ball.mesh.positions[:] = list(self.before)
        exec(compile(text, name + ".py", "exec"), {"__name__": "__main__"})
        self.assertEqual(self.positions(), fitted)


def fit_module():
    return sys.modules["modotools.fit"]


def fakelx_world(points, matrix, offset):
    (a, b, c), (d, e, f), (g, h, i) = matrix
    return [[a * x + b * y + c * z + offset[0], d * x + e * y + f * z + offset[1],
             g * x + h * y + i * z + offset[2]] for x, y, z in points]


# -- 3. failure ---------------------------------------------------------------------------

class FailureTest(Base):
    def test_a_diverging_solve_writes_nothing(self):
        # The kit's own fit module: start_kit loads the kit afresh.
        fit = sys.modules["modotools.fit"]
        real = fit.solve

        def diverge(*args, **kwargs):
            raise fit.Diverged("point 0 moved 9 m, past 2 m")
        fit.solve = diverge
        self.addCleanup(setattr, fit, "solve", real)
        answer = self.fit()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertIn("didn't settle", answer["error"]["message"])
        self.nothing_written()

    def test_a_failure_after_the_write_is_rolled_back(self):
        ops_mesh = sys.modules["modotools.ops_mesh"]
        fit = sys.modules["modotools.fit"]
        real = ops_mesh.read_surface
        calls = []

        def second_fails(item, sampled):
            calls.append(1)
            if len(calls) > 1:
                raise fit.Diverged("the measured rounds moved a point 9 m")
            return real(item, sampled)
        ops_mesh.read_surface = second_fails
        self.addCleanup(setattr, ops_mesh, "read_surface", real)
        answer = self.fit()
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["undo"].get("rolled_back"), answer)
        self.assertEqual(self.positions(), self.before)


# -- 4. large images and dark backgrounds ---------------------------------------------------

DARK = (40, 40, 40, 255)


class ReviewTest(Base):
    def refused(self, payload=None, words=()):
        answer = self.fit(payload)
        self.assertFalse(answer["ok"], answer)
        err = answer["error"]
        self.assertEqual(err["type"], "bad_request", err)
        for word in words:
            self.assertIn(word, err["message"] + " " + err.get("hint", ""))
        self.nothing_written()

    def test_a_selection_left_from_before_doesnt_limit_the_fit(self):
        # In Modo (2026-11-11): 10 polygons selected, then item mode; the
        # fit moved 19 of 706 points and said nothing. Modo's tools take a
        # polygon selection in polygon mode only, and so does the fit.
        self.ball.mesh.select("polygon", [0])
        self.assertEqual(self.fake.scene.select_mode, "item")
        result = self.fit()["result"]
        self.assertEqual(result["fitted"], len(self.before))
        self.assertIn("1 polygon is selected", result["note"])
        self.assertIn("polygon mode", result["note"])

    def test_a_part_fit_says_how_many_of_how_many(self):
        self.fake.scene.select_mode = "polygon"
        self.ball.mesh.select("polygon", [0, 1])
        result = self.fit()["result"]
        self.assertEqual(result["polygons"], 2)
        self.assertIn("2 selected SubD polygons of %d" % len(
            self.ball.mesh.polygons), result["note"])

    def test_an_image_with_no_background_is_refused(self):
        # In Modo: a model on black, no alpha. The mask was the whole
        # image and the body was fitted to a box, with no word of it.
        path = self.fake.links["backdropClip"][1][1].channels["filename"].get()
        self.fake.image_pixels[path] = [bytes(r) for r in ellipse_rows(
            SIZE, SIZE, 0.8 / PIXEL, 1.0 / PIXEL, background=DARK)]
        self.refused(words=("side", "no background", "white"))

    def test_a_large_image_is_read_thinned(self):
        # In Modo: a 2310 x 2470 image held Modo for 25 s. Past
        # FIT_IMAGE_SIDE pixels a side, every nth pixel is read.
        ops_mesh = sys.modules["modotools.ops_mesh"]
        self.addCleanup(setattr, ops_mesh, "FIT_IMAGE_SIDE",
                        ops_mesh.FIT_IMAGE_SIDE)
        whole = self.fit()["result"]
        self.fake.undo()
        ops_mesh.FIT_IMAGE_SIDE = 34      # 100 px: every 3rd pixel
        read = []
        image = fakelx._Image
        real = image.GetLine

        def counted(this, y, kind, buf):
            read.append(y)
            return real(this, y, kind, buf)
        image.GetLine = counted
        self.addCleanup(setattr, image, "GetLine", real)
        answer = self.fit()
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        # 34 of each image's 100 lines, from the middle of each step.
        self.assertEqual(sorted(set(read)), list(range(1, SIZE, 3)))
        self.assertIn("every 3rd pixel", result["note"])
        self.assertIn("100 x 100", result["note"])
        # The same shape in the same place: the surface is where the whole
        # image put it, within a thinned pixel (0.075 m).
        surface = fit.rule([list(p) for p in self.positions()],
                           self.ball.mesh.polygons)
        for s in surface:
            self.assertLess(abs(math.sqrt(ellipsoid(s)) - 1.0), 0.1, s)
        for view in whole["iou"]:
            self.assertGreater(result["iou"][view][1], 0.85, view)

    def test_a_small_image_is_read_whole_and_gets_no_note(self):
        self.assertNotIn("note", self.fit()["result"])

    def test_a_turned_backdrop_is_refused(self):
        # The fit reads a backdrop's view, pixel size and position; an
        # item turned or scaled shows its image elsewhere.
        self.side.world = (((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0)),
                           (0.0, 0.0, 0.0))
        self.refused(words=("side", "scaled"))

    def test_the_written_points_never_reach_the_answer(self):
        # The journal takes them out; with no journal they still go.
        self.kit.journal = None
        answer = self.fit()
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("_written", answer["result"])


if __name__ == "__main__":
    unittest.main()
