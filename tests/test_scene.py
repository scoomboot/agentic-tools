"""scene_summary, scene_graph, get_item, get_selection and screenshot in the
kit, on a fake scene: a locator holding two meshes, and a camera."""

import os
import sys
import unittest

import fakelx
import support


def job(kit, op, payload=None):
    jobs = sys.modules["modotools.jobs"]
    return jobs.run_job({"id": "t-1", "op": op, "payload": payload or {}}, kit)


def make_scene(scene):
    """Car (locator) > Body, Wheel (meshes); Camera. As in Modo, transform
    items have no parent and aren't in the Items list."""
    car = scene.add("locator001", "Car", "locator", channels={"visible": "default"})
    body = scene.add("mesh002", "Body", "mesh", parent=car,
                     channels={"visible": "default", "render": "default",
                               "drawShape": "default"})
    wheel = scene.add("mesh003", "Wheel", "mesh", parent=car,
                      channels={"visible": "default"})
    camera = scene.add("camera004", "Camera", "camera")
    body.transforms["translation"] = scene.add(
        "translation005", "Translation", "translation",
        channels={"pos.X": 1.0, "pos.Y": 2.0, "pos.Z": 3.0})
    return car, body, wheel, camera


class SummaryAndGraphTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.car, self.body, self.wheel, self.camera = make_scene(self.fake.scene)

    def test_summary(self):
        self.fake.scene.filename = "C:/models/striker/striker.lxo"
        self.fake.scene.time = 2.0
        self.fake.scene.selected = [self.body]
        answer = job(self.kit, "scene_summary")
        self.assertTrue(answer["ok"], answer)
        got = answer["result"]
        self.assertEqual(got["file"], "C:/models/striker/striker.lxo")
        self.assertEqual(got["frames"], [0, 120])
        self.assertEqual(got["frame"], 48)
        self.assertEqual(got["fps"], 24.0)
        self.assertEqual(got["items"], {"locator": 1, "mesh": 2, "camera": 1,
                                        "translation": 1})
        self.assertEqual(got["selected"], [{"id": "mesh002", "name": "Body",
                                            "type": "mesh"}])

    def test_graph_nests_the_items_list(self):
        got = job(self.kit, "scene_graph")["result"]
        self.assertEqual(got["items"], [
            {"name": "Car", "id": "locator001", "type": "locator", "children": [
                {"name": "Body", "id": "mesh002", "type": "mesh"},
                {"name": "Wheel", "id": "mesh003", "type": "mesh"}]},
            {"name": "Camera", "id": "camera004", "type": "camera"}])
        self.assertEqual(got["count"], 4)

    def test_graph_depth_one_counts_what_it_leaves_out(self):
        got = job(self.kit, "scene_graph", {"depth": 1})["result"]
        self.assertEqual(got["items"][0], {"name": "Car", "id": "locator001",
                                           "type": "locator", "more": 2})
        self.assertEqual(got["count"], 2)

    def test_graph_from_a_root(self):
        got = job(self.kit, "scene_graph", {"root": "Car"})["result"]
        self.assertEqual([n["id"] for n in got["items"]], ["mesh002", "mesh003"])

    def test_graph_by_type_lifts_items_to_the_nearest_shown_parent(self):
        got = job(self.kit, "scene_graph", {"types": ["mesh"]})["result"]
        self.assertEqual([n["id"] for n in got["items"]], ["mesh002", "mesh003"])

    def test_graph_all_types(self):
        got = job(self.kit, "scene_graph", {"types": ["all"]})["result"]
        self.assertIn("translation005", [n["id"] for n in got["items"]])


class ItemAndSelectionTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.car, self.body, self.wheel, self.camera = make_scene(self.fake.scene)

    def test_item_without_channels_lists_none(self):
        got = job(self.kit, "get_item", {"item": "Body"})["result"]
        self.assertEqual(got["id"], "mesh002")
        self.assertEqual(got["type"], "mesh")
        self.assertEqual(got["parent"], {"id": "locator001", "name": "Car"})
        self.assertEqual(got["pos"], [1.0, 2.0, 3.0])
        # No rotation or scale item yet: Modo's defaults, and none is made.
        self.assertEqual(got["rot"], [0.0, 0.0, 0.0])
        self.assertEqual(got["scl"], [1.0, 1.0, 1.0])
        self.assertNotIn("channels", got)
        self.assertEqual(sorted(self.body.transforms), ["translation"])

    def test_item_with_one_channel(self):
        got = job(self.kit, "get_item", {"item": "mesh002",
                                         "channels": ["visible"]})["result"]
        self.assertEqual(got["channels"], {"visible": "default"})

    def test_item_with_all_channels(self):
        got = job(self.kit, "get_item", {"item": "mesh002",
                                         "channels": "all"})["result"]
        self.assertEqual(sorted(got["channels"]), ["drawShape", "render", "visible"])

    def test_a_matrix_channel_reads_as_its_type(self):
        # On 17.1 a matrix channel's get() is an lx.object.Unknown.
        self.body.channels["localMatrix"] = fakelx.FakeChannel(
            self.body, "localMatrix", object())
        self.body.channels["localMatrix"].storageType = "matrix4"
        got = job(self.kit, "get_item", {"item": "mesh002",
                                         "channels": ["localMatrix"]})["result"]
        self.assertEqual(got["channels"], {"localMatrix": "<matrix4>"})

    def test_pos_rot_scl_by_name_read_the_transform_items(self):
        # mt_set_channels takes pos.Y on a mesh; mt_get_item must too.
        got = job(self.kit, "get_item", {"item": "Body",
                                         "channels": ["pos.Y", "rot.X", "visible"]})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["result"]["channels"],
                         {"pos.Y": 2.0, "rot.X": 0.0, "visible": "default"})
        # rot.X has no rotation item: the default, and none is made.
        self.assertEqual(sorted(self.body.transforms), ["translation"])

    def test_pos_on_an_item_that_is_no_locator_is_bad_channel(self):
        self.fake.scene.add("mat008", "Paint", "advancedMaterial",
                            channels={"diffCol.R": 0.5})
        answer = job(self.kit, "get_item", {"item": "Paint", "channels": ["pos.Y"]})
        self.assertEqual(answer["error"]["type"], "bad_channel")

    def test_an_unknown_channel_is_bad_channel(self):
        answer = job(self.kit, "get_item", {"item": "mesh002",
                                            "channels": ["visible", "nope"]})
        self.assertEqual(answer["error"]["type"], "bad_channel")
        self.assertIn("nope", answer["error"]["message"])

    def test_two_items_named_alike_are_ambiguous(self):
        self.fake.scene.add("mesh006", "Wheel", "mesh", parent=self.car)
        answer = job(self.kit, "get_item", {"item": "Wheel"})
        err = answer["error"]
        self.assertEqual(err["type"], "ambiguous_item")
        self.assertIn("mesh003", err["message"])
        self.assertIn("mesh006", err["message"])

    def test_a_shown_name_finds_its_item(self):
        second = self.fake.scene.add("mesh006", "Wheel (2)", "mesh", parent=self.car)
        second.baseName = "Wheel"
        got = job(self.kit, "get_item", {"item": "Wheel (2)"})["result"]
        self.assertEqual(got["id"], "mesh006")
        # "Wheel" is mesh003's shown name, so it isn't ambiguous.
        got = job(self.kit, "get_item", {"item": "Wheel"})["result"]
        self.assertEqual(got["id"], "mesh003")

    def test_a_base_name_two_items_share_is_ambiguous(self):
        # As on 17.1: two meshes called Wheel show as Wheel (2) and Wheel (3).
        for item_id, shown in (("mesh006", "Wheel (2)"), ("mesh007", "Wheel (3)")):
            self.fake.scene.add(item_id, shown, "mesh").baseName = "Wheel"
        self.wheel.name = self.wheel.baseName = "Front"
        err = job(self.kit, "get_item", {"item": "Wheel"})["error"]
        self.assertEqual(err["type"], "ambiguous_item")
        self.assertIn("mesh006 (Wheel (2))", err["message"])
        self.assertIn("mesh007 (Wheel (3))", err["message"])

    def test_an_unknown_item(self):
        answer = job(self.kit, "get_item", {"item": "Nope"})
        self.assertEqual(answer["error"]["type"], "no_such_item")

    def test_selected_items(self):
        self.fake.scene.selected = [self.wheel, self.camera]
        got = job(self.kit, "get_selection")["result"]
        self.assertEqual(got, {"type": "item", "items": [
            {"id": "mesh003", "name": "Wheel", "type": "mesh"},
            {"id": "camera004", "name": "Camera", "type": "camera"}]})

    def test_selected_polygons_on_a_mesh(self):
        self.fake.scene.selected = [self.body]
        self.body.geometry.select("polygons", [3, 4, 7])
        got = job(self.kit, "get_selection", {"type": "polygon"})["result"]
        self.assertEqual(got, {"type": "polygon", "meshes": [
            {"id": "mesh002", "name": "Body", "indices": [3, 4, 7]}]})

    def test_selected_edges_are_vertex_pairs(self):
        self.fake.scene.selected = [self.body]
        self.body.geometry.select("edges", [(0, 1), (1, 2)])
        got = job(self.kit, "get_selection", {"type": "edge"})["result"]
        self.assertEqual(got["meshes"][0]["indices"], [[0, 1], [1, 2]])

    def test_an_unknown_selection_type(self):
        answer = job(self.kit, "get_selection", {"type": "face"})
        self.assertEqual(answer["error"]["type"], "bad_request")


class ScreenshotTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    def test_writes_a_png_to_the_temp_folder(self):
        answer = job(self.kit, "screenshot")
        self.assertTrue(answer["ok"], answer)
        path = answer["result"]["path"]
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.fake.command_calls[-1][0], "!!gl.snapshot")

    def test_no_viewport(self):
        self.fake.scene.viewport = False
        answer = job(self.kit, "screenshot")
        err = answer["error"]
        self.assertEqual(err["type"], "no_viewport")
        self.assertIn("gl.snapshot: no 3D viewport to capture", err["event_log"])

    # -- size ------------------------------------------------------

    def shot(self, payload=None):
        answer = job(self.kit, "screenshot", payload)
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        for key in ("path", "original"):
            path = result.get(key)
            if path:
                self.addCleanup(lambda p=path: os.path.exists(p) and os.remove(p))
        return result

    def test_a_bad_size_is_refused_before_the_capture(self):
        for size in (0, 10, 63, 1.5, -800, "800", True):
            answer = job(self.kit, "screenshot", {"size": size})
            self.assertFalse(answer["ok"], size)
            self.assertEqual(answer["error"]["type"], "bad_request", size)
            self.assertIn("size", answer["error"]["message"])
        self.assertNotIn("!!gl.snapshot", [c[0] for c in self.fake.command_calls])

    def test_a_size_scales_the_longest_side(self):
        self.fake.scene.viewport_size = (1320, 1260)
        result = self.shot({"size": 800})
        self.assertEqual(result["size"], [800, 764])
        self.assertEqual(result["viewport"], [1320, 1260])
        with open(result["path"], "rb") as handle:
            data = handle.read(24)
        self.assertEqual((int.from_bytes(data[16:20], "big"),
                          int.from_bytes(data[20:24], "big")), (800, 764))
        # Both files are the server's to delete: shot_*.png in the folder.
        for key in ("path", "original"):
            name = os.path.basename(result[key])
            self.assertTrue(name.startswith("shot_") and name.endswith(".png"))
        self.assertTrue(os.path.isfile(result["original"]))

    def test_a_tall_viewport_scales_its_height(self):
        self.fake.scene.viewport_size = (600, 1200)
        self.assertEqual(self.shot({"size": 300})["size"], [150, 300])

    def test_a_size_at_or_over_the_viewport_is_the_full_picture(self):
        self.fake.scene.viewport_size = (1320, 1260)
        for size in (1320, 4000, None):
            result = self.shot({"size": size} if size else {})
            self.assertEqual(result["size"], [1320, 1260])
            self.assertEqual(result["viewport"], [1320, 1260])
            self.assertNotIn("original", result)
        self.assertEqual(self.fake.image_saves, [])

    def test_a_failed_scaling_gives_the_full_picture_and_says_so(self):
        self.fake.scene.viewport_size = (1320, 1260)
        self.fake.image_save_fails = True
        result = self.shot({"size": 800})
        self.assertEqual(result["size"], [1320, 1260])
        self.assertIn("couldn't be scaled", result["note"])
        with open(result["path"], "rb") as handle:
            self.assertEqual(int.from_bytes(handle.read(24)[16:20], "big"), 1320)


    # -- a large mesh is drawn late --------------------------

    def big_mesh(self, points, item_id="mesh009"):
        item = self.fake.scene.add(item_id, "Big", "mesh")
        item.mesh = fakelx.FakeMesh([(0.0, 0.0, 0.0)] * points, [], [], [])
        return item

    def test_the_draw_gap_follows_the_largest_mesh(self):
        ops_view = sys.modules["modotools.ops_view"]
        self.assertEqual(ops_view.draw_gap(), 0.0)
        self.big_mesh(1000, "mesh008")
        self.big_mesh(100000)
        # 17.1: 101,402 points drew 0.25 s after the edit; 0.3 s was not
        # always enough, 0.5 s was.
        self.assertAlmostEqual(ops_view.draw_gap(), 0.5)
        self.assertAlmostEqual(self.kit.pump.gap_after_change(), 0.5)

    def test_the_draw_gap_has_a_limit_and_the_shot_says_so(self):
        ops_view = sys.modules["modotools.ops_view"]
        self.big_mesh(100000)
        self.assertNotIn("note", self.shot())
        limit = ops_view.DRAW_GAP_MAX_S
        self.addCleanup(setattr, ops_view, "DRAW_GAP_MAX_S", limit)
        ops_view.DRAW_GAP_MAX_S = 0.2
        self.assertAlmostEqual(ops_view.draw_gap(), 0.2)
        note = self.shot()["note"]
        self.assertIn("100,000 points", note)
        self.assertIn("call again", note)


class ReadsAreReadsTest(unittest.TestCase):
    """Every scene read runs through modoTools.execRead, never exec."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        make_scene(self.fake.scene)

    def test_no_read_runs_through_exec(self):
        client = support.Client(self.kit.port)
        self.addCleanup(client.close)
        client.hello()
        for number, (op, payload) in enumerate([
                ("scene_summary", {}), ("scene_graph", {}),
                ("get_item", {"item": "mesh002"}), ("get_selection", {}),
                ("screenshot", {})]):
            del self.fake.ran[:]
            asked = support.Asked(client, {"id": "r-%d" % number, "op": op,
                                           "payload": payload})
            self.assertTrue(support.pump_until(self.fake, asked.done))
            self.assertTrue(asked.answer["ok"], asked.answer)
            self.assertIn("modoTools.execRead", self.fake.ran, op)
            self.assertNotIn("modoTools.exec", self.fake.ran, op)
            if op == "screenshot":
                os.remove(asked.answer["result"]["path"])


if __name__ == "__main__":
    unittest.main()
