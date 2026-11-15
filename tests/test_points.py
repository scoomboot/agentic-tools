"""Points and set_points in the kit. Read a mesh's vertex
positions by index; write new ones through a layer scan, every index and
value checked first, as one undo step the journal's replay can run."""

import json
import math
import os
import sys
import unittest

import fakelx
import support
from test_meshmath import CUBE
from test_mesh import cube
from test_scene import job


def positions(item):
    return [list(p) for p in item.mesh.positions]


class Base(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        scene = self.fake.scene
        self.cube = cube(scene)
        # Moved and turned: the answer is still the mesh's own space.
        self.cube.world = (((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)),
                           (3.0, 0.0, 0.0))
        self.locator = scene.add("locator003", "Null", "locator")
        self.before = positions(self.cube)

    def points(self, payload=None):
        return job(self.kit, "points", payload)

    def set_points(self, payload=None):
        return job(self.kit, "set_points", payload)

    def nothing_written(self):
        self.assertEqual(self.fake.layer_scans, [])
        self.assertEqual(positions(self.cube), self.before)
        self.assertEqual(self.fake.mesh_writes, [])


# -- 1. refusals ------------------------------------------------------------------

class RefusalTest(Base):
    def refused(self, payload, kind="bad_request", words=()):
        answer = self.set_points(payload)
        self.assertFalse(answer["ok"], answer)
        err = answer["error"]
        self.assertEqual(err["type"], kind, err)
        for word in words:
            self.assertIn(word, err["message"] + " " + err.get("hint", ""))
        self.nothing_written()
        return err

    def test_an_item_that_isnt_a_mesh(self):
        err = self.refused({"item": "Null", "points": {"0": [0, 0, 0]}},
                           "not_a_mesh")
        self.assertEqual(err["message"], "Null is a locator, not a mesh")

    def test_no_such_item(self):
        self.refused({"item": "nothing999", "points": {"0": [0, 0, 0]}},
                     "no_such_item")

    def test_no_item(self):
        self.refused({"points": {"0": [0, 0, 0]}})

    def test_no_points(self):
        for points in (None, {}, [], "0 0 0", 5):
            self.refused({"item": "Cube", "points": points})

    def test_an_index_out_of_range(self):
        err = self.refused({"item": "Cube", "points": {"1": [0, 0, 0],
                                                       "8": [0, 0, 0]}},
                           words=("8", "0 to 7"))
        self.assertIn("Nothing was written", err["hint"])

    def test_a_negative_or_not_whole_index(self):
        for key in ("-1", "a", "1.5", "", " 2"):
            self.refused({"item": "Cube", "points": {key: [0, 0, 0]}},
                         words=(repr(key),))

    def test_one_index_twice(self):
        self.refused({"item": "Cube", "points": {"1": [0, 0, 0],
                                                 "01": [1, 1, 1]}},
                     words=("twice",))

    def test_a_value_that_isnt_three_finite_numbers(self):
        for value in ([0, 0], [0, 0, 0, 0], [0, 0, "1"], [0, 0, None],
                      [0, True, 0], [0, 0, float("nan")],
                      [0, float("inf"), 0], "0 0 0", {"x": 0}, 5):
            self.refused({"item": "Cube", "points": {"2": value}},
                         words=("point 2",))

    def test_a_bad_value_after_good_ones_writes_nothing(self):
        self.refused({"item": "Cube", "points": {
            "0": [1, 1, 1], "1": [2, 2, 2], "7": [0, "x", 0]}})

    def test_a_list_of_the_wrong_length(self):
        self.refused({"item": "Cube", "points": [[0, 0, 0]] * 7},
                     words=("7", "8"))
        self.refused({"item": "Cube", "points": [[0, 0, 0]] * 9},
                     words=("9", "8"))

    def test_a_bad_value_in_a_list(self):
        values = [list(p) for p in CUBE["positions"]]
        values[5] = [0, 0]
        self.refused({"item": "Cube", "points": values}, words=("point 5",))

    def test_the_reads_own_shape_is_named(self):
        # The review: the likeliest slip is to send back what mt_points
        # answered, [[index, [x, y, z]], ...]; some of them, or all.
        pairs = [[i, list(p)] for i, p in enumerate(CUBE["positions"])]
        for points in (pairs[:2], pairs):
            err = self.refused({"item": "Cube", "points": points},
                               words=("[index, [x, y, z]]", "mt_points",
                                      '{"0": [x, y, z]'))
            self.assertIn("Nothing was written", err["hint"])

    def test_a_whole_number_too_large_for_a_float(self):
        # The review: JSON can hold it, float() can't; a refusal, not a
        # job_exception.
        err = self.refused({"item": "Cube", "points": {"2": [0, 10 ** 400, 0]}},
                           words=("point 2",))
        self.assertLess(len(err["message"]), 200)
        values = [list(p) for p in CUBE["positions"]]
        values[5] = [-10 ** 400, 0, 0]
        self.refused({"item": "Cube", "points": values}, words=("point 5",))

    def test_reads_are_refused_too(self):
        for payload, kind in (({"item": "Null"}, "not_a_mesh"),
                              ({"item": "nothing999"}, "no_such_item"),
                              ({}, "bad_request"),
                              ({"item": "Cube", "start": -1}, "bad_request"),
                              ({"item": "Cube", "start": 9}, "bad_request"),
                              ({"item": "Cube", "start": "a"}, "bad_request"),
                              ({"item": "Cube", "count": -2}, "bad_request"),
                              ({"item": "Cube", "count": True}, "bad_request"),
                              ({"item": "Cube", "polygons": "yes"}, "bad_request")):
            answer = self.points(payload)
            self.assertFalse(answer["ok"], payload)
            self.assertEqual(answer["error"]["type"], kind, payload)


# -- 2. the read -------------------------------------------------------------------

class ReadTest(Base):
    def test_every_point_by_index_in_the_meshs_own_space(self):
        answer = self.points({"item": "Cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"], {
            "item": "mesh002", "name": "Cube", "count": 8, "start": 0,
            "points": [[i, list(p)] for i, p in enumerate(CUBE["positions"])],
            "space": "mesh", "units": "m"})

    def test_polygons_as_vertex_index_lists(self):
        got = self.points({"item": "Cube", "polygons": True})["result"]
        self.assertEqual(got["polygons"], CUBE["polygons"])
        self.assertEqual(got["polygon_count"], 6)

    def test_start_and_count(self):
        got = self.points({"item": "mesh002", "start": 3, "count": 2})["result"]
        self.assertEqual(got["points"], [[3, list(CUBE["positions"][3])],
                                         [4, list(CUBE["positions"][4])]])
        self.assertEqual((got["start"], got["count"]), (3, 8))
        self.assertNotIn("cut", got)

    def test_a_count_past_the_end_stops_at_it(self):
        got = self.points({"item": "Cube", "start": 6, "count": 50})["result"]
        self.assertEqual([p[0] for p in got["points"]], [6, 7])
        self.assertNotIn("cut", got)

    def test_start_at_the_end_reads_nothing(self):
        got = self.points({"item": "Cube", "start": 8})["result"]
        self.assertEqual(got["points"], [])

    def test_positions_are_rounded_to_a_micrometre(self):
        self.cube.mesh.positions[0] = (0.1234567891, -2.0000004, 1e-9)
        got = self.points({"item": "Cube", "count": 1})["result"]
        self.assertEqual(got["points"], [[0, [0.123457, -2.0, 0.0]]])

    def test_from_the_edit_action_at_the_current_time(self):
        self.fake.scene.time = 1.5
        self.cube.deformed = fakelx.FakeMesh([(9, 9, 9)], [], [], [])
        got = self.points({"item": "Cube"})["result"]
        self.assertEqual(len(got["points"]), 8)
        self.assertEqual(set(self.fake.channel_reads), {("edit", 1.5)})

    def test_a_read_writes_nothing_and_leaves_no_undo_step(self):
        jobs = sys.modules["modotools.jobs"]
        self.assertTrue(jobs.is_read("points"))
        self.points({"item": "Cube", "polygons": True})
        self.nothing_written()
        self.assertEqual(self.fake.undo_steps, [])

    def test_past_the_room_with_no_count_is_too_large(self):
        answer = self.points({"item": "Cube", "max_chars": 1000})
        self.assertFalse(answer["ok"])
        err = answer["error"]
        self.assertEqual(err["type"], "too_large")
        self.assertIn("8 points", err["message"])
        self.assertIn("count", err["hint"])
        self.assertIn("start", err["hint"])

    def test_past_the_room_with_a_count_is_cut_and_says_so(self):
        ops_mesh = sys.modules["modotools.ops_mesh"]
        room = ops_mesh.point_room(1000 + 3 * ops_mesh.POINT_CHARS, 0)
        self.assertEqual(room, 3)
        got = self.points({"item": "Cube", "start": 2, "count": 6,
                           "max_chars": 1000 + 3 * ops_mesh.POINT_CHARS})["result"]
        self.assertEqual([p[0] for p in got["points"]], [2, 3, 4])
        self.assertEqual(got["cut"], {"shown": 3, "asked": 6, "next_start": 5})

    def test_polygons_that_dont_fit_are_too_large(self):
        answer = self.points({"item": "Cube", "polygons": True, "count": 1,
                              "max_chars": 1000})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "too_large")
        self.assertIn("polygons", answer["error"]["message"])

    def test_polygons_that_leave_no_room_for_a_point_are_too_large(self):
        # The review: a page of no points, next_start where it began, sent
        # a paging agent round for ever.
        ops_mesh = sys.modules["modotools.ops_mesh"]
        polygon_chars = 6 * (ops_mesh.POLYGON_CHARS
                             + 4 * ops_mesh.POLYGON_VERTEX_CHARS)
        answer = self.points({"item": "Cube", "polygons": True, "count": 8,
                              "max_chars": 1000 + polygon_chars + 10})
        self.assertFalse(answer["ok"], answer)
        err = answer["error"]
        self.assertEqual(err["type"], "too_large")
        self.assertIn("polygons", err["message"] + err["hint"])
        self.assertIn("without polygons", err["hint"])

    def test_no_room_for_a_point_is_too_large_with_a_count_too(self):
        answer = self.points({"item": "Cube", "count": 4, "max_chars": 1000})
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["type"], "too_large")

    def test_room_for_all_answers_all(self):
        got = self.points({"item": "Cube", "polygons": True,
                           "max_chars": 40000})["result"]
        self.assertEqual(len(got["points"]), 8)
        self.assertNotIn("cut", got)


# -- 3. the write ------------------------------------------------------------------

class WriteTest(Base):
    def test_only_the_named_points_move(self):
        answer = self.set_points({"item": "Cube", "points": {
            "1": [0.6, -0.5, -0.5], "6": [0.5, 0.75, 0.5]}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"], {"item": "mesh002", "name": "Cube",
                                            "written": 2, "count": 8,
                                            "space": "mesh"})
        want = list(self.before)
        want[1], want[6] = [0.6, -0.5, -0.5], [0.5, 0.75, 0.5]
        self.assertEqual(positions(self.cube), want)

    def test_whole_numbers_and_int_keys(self):
        # JSON makes every key a string; a Python caller may not.
        answer = self.set_points({"item": "Cube", "points": {3: [1, 2, 3]}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(positions(self.cube)[3], [1.0, 2.0, 3.0])

    def test_a_full_list_in_index_order(self):
        moved = [[x * 2, y * 2, z * 2] for x, y, z in CUBE["positions"]]
        answer = self.set_points({"item": "Cube", "points": moved})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["written"], 8)
        self.assertEqual(positions(self.cube), moved)

    def test_one_layer_scan_on_that_item(self):
        self.fake.scene.selected = [self.locator]
        self.set_points({"item": "Cube", "points": {"0": [0, 0, 0]}})
        [(ident, flags, scan)] = self.fake.layer_scans
        self.assertEqual((ident, flags), ("mesh002", 273))
        # POSITION and POINTS (17.1): with POSITION alone,
        # a write of all points, a surface read, a write of some points and
        # two undos left Modo's cached mesh (the kit's read, the TD SDK's)
        # with only the second write's neighbourhood put back.
        self.assertEqual(scan.changes, [(0, 3)])
        self.assertEqual(scan.applied, 1)

    def test_a_point_set_to_where_it_is_is_still_written(self):
        answer = self.set_points({"item": "Cube", "points": {
            "0": list(CUBE["positions"][0])}})
        self.assertEqual(answer["result"]["written"], 1)


class WriteUndoTest(unittest.TestCase):
    """Through the socket and the pump, as in Modo: one undo step, and a
    failure after the checks rolled back."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.cube = cube(self.fake.scene)
        self.before = positions(self.cube)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.n = 0

    def ask(self, op, payload):
        self.n += 1
        asked = support.Asked(self.client, {"id": "p-%d" % self.n, "op": op,
                                            "payload": payload})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        return asked.answer

    def test_one_undo_step_puts_every_point_back(self):
        points = {str(i): [i, i, i] for i in range(8)}
        answer = self.ask("set_points", {"item": "Cube", "points": points})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(positions(self.cube), [[i, i, i] for i in range(8)])
        self.assertTrue(self.fake.undo())
        self.assertEqual(positions(self.cube), self.before)

    def test_a_write_that_fails_is_rolled_back(self):
        self.fake.scan_apply_fails = True
        answer = self.ask("set_points", {"item": "Cube",
                                         "points": {"0": [5, 5, 5]}})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertEqual(answer["undo"], {"rolled_back": True})
        self.assertEqual(positions(self.cube), self.before)

    def test_a_refusal_in_an_action_leaves_it_open(self):
        # A refusal is not a failure; the action and its steps stay.
        self.assertTrue(self.ask("begin", {"label": "fit"})["ok"])
        self.assertTrue(self.ask("set_points", {"item": "Cube",
                                                "points": {"0": [5, 5, 5]}})["ok"])
        answer = self.ask("set_points", {"item": "Cube",
                                         "points": {"9": [1, 1, 1]}})
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertTrue(answer["undo"].get("refused"), answer["undo"])
        self.assertEqual(positions(self.cube)[0], [5.0, 5.0, 5.0])

    def test_not_a_mesh_in_an_action_leaves_it_open(self):
        # A locator for a mesh is refused before anything is written, as
        # bad_request is: the action keeps its steps.
        self.fake.scene.add("locator003", "Null", "locator")
        self.assertTrue(self.ask("begin", {"label": "fit"})["ok"])
        self.assertTrue(self.ask("set_points", {"item": "Cube",
                                                "points": {"0": [5, 5, 5]}})["ok"])
        answer = self.ask("set_points", {"item": "Null",
                                         "points": {"0": [1, 1, 1]}})
        self.assertEqual(answer["error"]["type"], "not_a_mesh")
        self.assertTrue(answer["undo"].get("refused"), answer["undo"])
        self.assertEqual(positions(self.cube)[0], [5.0, 5.0, 5.0])

    def test_a_number_too_large_in_an_action_leaves_it_open(self):
        # The review: it was a job_exception, which rolled the action back.
        self.assertTrue(self.ask("begin", {"label": "fit"})["ok"])
        self.assertTrue(self.ask("set_points", {"item": "Cube",
                                                "points": {"0": [5, 5, 5]}})["ok"])
        answer = self.ask("set_points", {"item": "Cube",
                                         "points": {"1": [0, 10 ** 400, 0]}})
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertTrue(answer["undo"].get("refused"), answer["undo"])
        self.assertEqual(positions(self.cube)[0], [5.0, 5.0, 5.0])

    # -- 4. journal and replay ------------------------------------------------------

    def journal(self):
        name = self.kit.journal.name
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "journals",
                            name + ".jsonl")
        with open(path) as handle:
            return name, [json.loads(line) for line in handle]

    def test_the_entry_holds_the_values_and_the_replay_sets_them(self):
        from modo_tools import journal as replay
        self.ask("set_points", {"item": "Cube", "points": {
            "2": [0.25, 0.5, -0.125], "5": [1, 2, 3]}})
        moved = positions(self.cube)
        name, records = self.journal()
        [entry] = [r for r in records if r.get("event") == "entry"]
        self.assertEqual(entry["tool"], "set_points")
        self.assertEqual(entry["untraced"], [])
        self.assertTrue(entry["exact"])
        text = replay.build(name, records)
        self.assertIn("_points('mesh002', ", text)
        self.assertIn("# Exact: yes.", text)
        # As in a new scene: the cube as it was, then the replay.
        self.cube.mesh.positions[:] = [tuple(p) for p in self.before]
        exec(compile(text, name + ".py", "exec"), {"__name__": "__main__"})
        self.assertEqual(positions(self.cube), moved)

    def test_a_full_list_replays_too(self):
        from modo_tools import journal as replay
        moved = [[x, y + 1, z] for x, y, z in CUBE["positions"]]
        self.ask("set_points", {"item": "Cube", "points": moved})
        name, records = self.journal()
        self.cube.mesh.positions[:] = [tuple(p) for p in self.before]
        exec(compile(replay.build(name, records), name + ".py", "exec"),
             {"__name__": "__main__"})
        self.assertEqual(positions(self.cube), moved)

    def test_a_refusal_has_no_entry(self):
        answer = self.ask("set_points", {"item": "Cube",
                                         "points": {"8": [0, 0, 0]}})
        self.assertFalse(answer["ok"])
        self.assertNotIn("journal", answer)

    def test_a_read_has_no_entry(self):
        answer = self.ask("points", {"item": "Cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("journal", answer)
        self.assertNotIn("undo", answer)


if __name__ == "__main__":
    unittest.main()
