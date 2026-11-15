"""The backdrop op, a reference image behind the model in one
call, on fake lx as a probe found Modo 17.1."""

import os
import sys
import unittest

import fakelx
import support


def job(kit, op, payload=None):
    jobs = sys.modules["modotools.jobs"]
    return jobs.run_job({"id": "t-1", "op": op, "payload": payload or {}}, kit)


class BackdropTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.home = os.environ["MODO_TOOLS_HOME"]
        ops_view = sys.modules["modotools.ops_view"]
        real = ops_view.folder
        ops_view.folder = lambda: os.path.join(self.home, "temp")
        self.addCleanup(setattr, ops_view, "folder", real)
        self.mesh = self.fake.scene.add("mesh001", "Body")
        self.fake.scene.selected = [self.mesh]
        self.side = self.image("side.png", 231, 270)

    def image(self, name, width, height):
        path = os.path.join(self.home, name).replace("\\", "/")
        with open(path, "wb") as handle:
            handle.write(fakelx.png(width, height))
        return path

    def backdrop(self, **payload):
        base = {"path": self.side, "view": "left", "width_m": 0.231}
        base.update(payload)
        return job(self.kit, "backdrop", base)

    def made(self, kind):
        return [i for i in self.fake.scene.items if i.type == kind]

    def links(self):
        return [(f.id, t.id) for f, t in self.fake.links.get("backdropClip", [])]

    # -- the item and its link -------------------------------------------------

    def test_makes_a_clip_and_a_backdrop_linked_from_the_backdrop(self):
        answer = self.backdrop()
        self.assertTrue(answer["ok"], answer)
        [clip] = self.made("videoStill")
        [item] = self.made("backdrop")
        self.assertEqual(clip.channel("filename").get(), self.side)
        # The probe: backdrop.add image:<clip> and item.link link nothing;
        # the backdropClip graph from the backdrop to the clip does.
        self.assertEqual(self.links(), [(item.id, clip.id)])
        result = answer["result"]
        self.assertEqual((result["item"], result["clip"]), (item.id, clip.id))

    def test_the_projection_is_the_view(self):
        for view in ("left", "right", "front", "back", "top", "bottom"):
            answer = self.backdrop(view=view, name=view)
            self.assertTrue(answer["ok"], answer)
            item = self.fake.scene.find(answer["result"]["item"])
            self.assertEqual(item.channel("projection").get(), view)

    def test_a_view_is_matched_without_case(self):
        answer = self.backdrop(view="Front")
        item = self.fake.scene.find(answer["result"]["item"])
        self.assertEqual(item.channel("projection").get(), "front")

    # -- size and place ---------------------------------------------------------

    def test_the_pixel_size_comes_from_width_m(self):
        answer = self.backdrop(width_m=0.142)
        item = self.fake.scene.find(answer["result"]["item"])
        self.assertAlmostEqual(item.channel("pixelSize").get(), 0.142 / 231)

    def test_it_sits_at_center_hidden_in_perspective(self):
        answer = self.backdrop(center=[0, 0.0577, 0.0067], opacity=0.65)
        item = self.fake.scene.find(answer["result"]["item"])
        move = item.transforms["translation"]
        self.assertEqual([move.channel("pos." + a).get() for a in "XYZ"],
                         [0.0, 0.0577, 0.0067])
        self.assertTrue(item.channel("hidePersp").get())
        self.assertAlmostEqual(item.channel("transparency").get(), 0.35)

    def test_the_answer_says_the_camera_view_still_shows_it(self):
        # No backdrop channel hides it in the Camera
        # view on 17.1 (hidePersp is the Perspective view's alone);
        # visible off hides it in every view, and view3d.showBackdrop is
        # the whole viewport's.
        result = self.backdrop()["result"]
        self.assertTrue(result["hidden_in_perspective"])
        self.assertIs(result["hidden_in_camera"], False)
        note = result["note"]
        for words in ("Camera view", '"visible": "off"', "view3d.showBackdrop"):
            self.assertIn(words, note)

    def test_the_answer_gives_the_rectangle_in_metres(self):
        # Left: the image's width runs along Z and its height along Y,
        # about center (the probe, 2026-11-10).
        result = self.backdrop(width_m=0.231, center=[0, 0.2, 0.1])["result"]
        self.assertEqual(result["size_m"], [0.231, 0.27])
        self.assertEqual(result["rect"]["min"], [0.0, 0.065, -0.0155])
        self.assertEqual(result["rect"]["max"], [0.0, 0.335, 0.2155])
        result = self.backdrop(view="top", width_m=0.231)["result"]
        self.assertEqual(result["rect"]["min"], [-0.1155, 0.0, -0.135])

    def test_its_name_is_the_images_or_the_one_given(self):
        result = self.backdrop()["result"]
        self.assertEqual(self.fake.scene.find(result["item"]).name, "side")
        result = self.backdrop(name="ref side")["result"]
        self.assertEqual(self.fake.scene.find(result["item"]).name, "ref side")

    def test_the_selection_is_put_back(self):
        # item.create selects the backdrop; a mesh tool after the call would
        # act on nothing.
        self.assertTrue(self.backdrop()["ok"])
        self.assertIn("select.drop", self.fake.ran)
        self.assertEqual(self.fake.scene.selected, [self.mesh])

    def test_every_change_is_a_command_but_the_link(self):
        # So the journal holds them and its replay runs them.
        self.backdrop()
        names = [name.lstrip("!") for name, _args in self.fake.command_calls]
        for name in ("clip.addStill", "item.create", "item.channel",
                     "transform.channel"):
            self.assertIn(name, names)

    def test_no_line_names_the_backdrop_by_id(self):
        # The journal's replay runs these lines in a new scene, where Modo
        # may give the backdrop another id: they act on the selection
        # item.create leaves, the new backdrop alone.
        item = self.backdrop()["result"]["item"]
        for name, args in self.fake.command_calls:
            self.assertNotIn(item, " ".join(map(str, args.values())), name)

    # -- clips and files -----------------------------------------------------

    def test_the_same_image_again_shares_its_clip(self):
        # 17.1: clip.addStill for a file the scene has a clip for makes no
        # second clip. An earlier version answered command_failed there.
        first = self.backdrop(view="left")
        second = self.backdrop(view="right", name="side right")
        self.assertTrue(second["ok"], second)
        [clip] = self.made("videoStill")
        self.assertEqual(second["result"]["clip"], clip.id)
        self.assertEqual(self.links(), [
            (first["result"]["item"], clip.id),
            (second["result"]["item"], clip.id)])
        self.assertIn("already had", second["result"]["note"])
        self.assertNotIn("already had", first["result"]["note"])

    def test_the_answer_names_the_file_the_clip_shows(self):
        self.assertEqual(self.backdrop()["result"]["file"], self.side)
        result = self.backdrop(width_m=0.142, height_m=0.150)["result"]
        self.assertEqual(result["file"], result["stretched"])

    def test_the_selection_mode_is_put_back(self):
        # 17.1: item.create and select.drop item leave Modo in item mode, so
        # a polygon tool after the call would act on the whole mesh.
        self.fake.scene.select_mode = "polygon"
        self.assertTrue(self.backdrop()["ok"])
        self.assertEqual(self.fake.scene.select_mode, "polygon")
        self.assertEqual(self.fake.scene.selected, [self.mesh])

    def test_item_mode_needs_no_mode_command(self):
        self.assertTrue(self.backdrop()["ok"])
        self.assertNotIn("select.typeFrom", self.fake.ran)
        self.assertEqual(self.fake.scene.select_mode, "item")

    def test_two_images_of_one_name_dont_share_a_copy(self):
        # side.png and side.jpg in one folder, stretched to one size.
        other = self.image("side.jpg", 231, 270)
        one = self.backdrop(width_m=0.142, height_m=0.150)["result"]
        two = self.backdrop(path=other, width_m=0.142, height_m=0.150)["result"]
        self.assertNotEqual(one["stretched"], two["stretched"])

    def test_the_replays_link_finds_the_clip_by_its_file(self):
        # A backdrop of an image the scene already shows: its clip is not
        # the newest one, so the replay links by the clip's file.
        from modo_tools import journal as replay
        other = self.image("front.png", 182, 208)
        self.backdrop()
        self.backdrop(path=other, view="front")
        last = self.backdrop(view="right")["result"]
        self.fake.links["backdropClip"][:] = []
        exec(compile(replay.link_code(last["file"]), "link", "exec"), {})
        self.assertEqual(self.links(), [(last["item"], last["clip"])])
        # A journal written before the review holds no file: the newest.
        self.fake.links["backdropClip"][:] = []
        exec(compile(replay.link_code(None), "link", "exec"), {})
        [(_item, clip)] = self.links()
        self.assertTrue(clip.startswith("front"), clip)

    # -- the stretch ------------------------------------------------------------

    def test_a_height_at_the_images_aspect_makes_no_copy(self):
        answer = self.backdrop(width_m=0.231, height_m=0.2702)
        self.assertTrue(answer["ok"], answer)
        self.assertIsNone(answer["result"]["stretched"])
        self.assertEqual(self.fake.image_saves, [])
        [clip] = self.made("videoStill")
        self.assertEqual(clip.channel("filename").get(), self.side)

    def test_another_height_shows_a_stretched_copy(self):
        answer = self.backdrop(width_m=0.142, height_m=0.150)
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        # 231 px wide at 0.142 m: 0.150 m is 244 px.
        [(path, fmt, size)] = self.fake.image_saves
        self.assertEqual((fmt, size), ("PNG", (231, 244)))
        self.assertEqual(result["stretched"], path.replace("\\", "/"))
        self.assertTrue(path.startswith(
            os.path.join(self.home, "temp").replace("\\", "/")))
        self.assertIn("side", os.path.basename(path))
        self.assertIn("231x244", os.path.basename(path))
        [clip] = self.made("videoStill")
        self.assertEqual(clip.channel("filename").get(), result["stretched"])
        self.assertEqual(result["pixels"], [231, 244])
        self.assertEqual(result["image"], [231, 270])
        self.assertIn("stretched", result["note"])

    def test_the_stretch_is_resampled_from_the_image(self):
        self.backdrop(width_m=0.142, height_m=0.150)
        [path] = self.fake.image_loads[-1:]
        self.assertEqual(path, self.side)

    def test_a_copy_that_cant_be_written_is_bad_request_naming_it(self):
        self.fake.image_save_fails = True
        answer = self.backdrop(width_m=0.142, height_m=0.150)
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn(os.path.join(self.home, "temp").replace("\\", "/"),
                      answer["error"]["message"].replace("\\", "/"))
        self.assertEqual(self.made("backdrop") + self.made("videoStill"), [])

    # -- refusals -------------------------------------------------------------

    def refused(self, **payload):
        answer = self.backdrop(**payload)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["type"], "bad_request", answer)
        self.assertEqual(self.made("backdrop") + self.made("videoStill"), [])
        self.assertNotIn("clip.addStill", self.fake.ran)
        return answer["error"]

    def test_a_missing_file_is_refused(self):
        # Said before Modo is asked to load it, not as an unreadable image.
        error = self.refused(path=os.path.join(self.home, "nope.png"))
        self.assertTrue(error["message"].startswith("no file at"), error)
        self.assertIn("nope.png", error["message"])
        self.assertEqual(self.fake.image_loads, [])

    def test_a_file_modo_cant_read_is_refused(self):
        path = os.path.join(self.home, "notes.png")
        with open(path, "w") as handle:
            handle.write("not an image")
        self.assertIn("can't read", self.refused(path=path)["message"])

    def test_a_view_not_in_the_list_is_refused(self):
        error = self.refused(view="perspective")
        self.assertIn("left", error["hint"])

    def test_a_size_not_above_0_is_refused(self):
        self.refused(width_m=0)
        self.refused(width_m=-1)
        self.refused(width_m="wide")
        self.refused(width_m=True)
        self.refused(height_m=0)
        self.refused(width_m=float("inf"))
        self.refused(height_m=float("nan"))

    def test_a_bad_center_or_opacity_is_refused(self):
        self.refused(center=[0, 1])
        self.refused(center="middle")
        self.refused(opacity=1.5)
        self.refused(opacity=-0.1)
        self.refused(center=[0, float("inf"), 0])

    def test_no_path_is_refused(self):
        self.assertEqual(self.refused(path="")["message"], "no path given")


class BackdropRollbackTest(unittest.TestCase):
    """Through the socket and the pump, as in Modo: one undo step, and a
    failure undone whole."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.home = os.environ["MODO_TOOLS_HOME"]
        ops_view = sys.modules["modotools.ops_view"]
        real = ops_view.folder
        ops_view.folder = lambda: os.path.join(self.home, "temp")
        self.addCleanup(setattr, ops_view, "folder", real)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.path = os.path.join(self.home, "front.png").replace("\\", "/")
        with open(self.path, "wb") as handle:
            handle.write(fakelx.png(182, 208))

    def ask(self, payload):
        asked = support.Asked(self.client, {"id": "r-1", "op": "backdrop",
                                            "payload": payload})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        return asked.answer

    def made(self):
        return [i for i in self.fake.scene.items
                if i.type in ("backdrop", "videoStill", "translation")]

    def test_one_undo_removes_the_backdrop_and_its_clip(self):
        answer = self.ask({"path": self.path, "view": "front", "width_m": 0.1})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(len(self.made()), 3)
        self.fake.undo()
        self.assertEqual(self.made(), [])
        self.assertEqual(self.fake.links.get("backdropClip"), [])

    def test_its_journal_entry_holds_the_link(self):
        # The replay makes the link no command makes.
        import json
        answer = self.ask({"path": self.path, "view": "front", "width_m": 0.1})
        name = self.kit.journal.name
        with open(os.path.join(self.home, "journals", name + ".jsonl")) as h:
            records = [json.loads(line) for line in h]
        [record] = [r for r in records if r.get("event") == "entry"]
        result = answer["result"]
        self.assertEqual(record["tool"], "backdrop")
        self.assertEqual(record["link"], [result["item"], result["clip"]])
        self.assertEqual(record["clip_file"], self.path)
        self.assertIn("clip.addStill", " ".join(record["commands"]))
        self.assertEqual(record["untraced"], [])

    def test_a_failure_leaves_nothing(self):
        self.fake.failing.add("transform.channel")
        answer = self.ask({"path": self.path, "view": "front", "width_m": 0.1})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["undo"], {"rolled_back": True})
        self.assertEqual(self.made(), [])
        self.assertEqual(self.fake.links.get("backdropClip"), [])


    # -- mt_set_channels moves a backdrop ---------------------------

    def test_set_channels_moves_a_backdrop_and_the_journal_has_it(self):
        import json
        made = self.ask({"path": self.path, "view": "front", "width_m": 0.1})
        item = made["result"]["item"]
        asked = support.Asked(self.client, {
            "id": "r-2", "op": "set_channels",
            "payload": {"item": item, "channels": {"pos.Z": 0.0068}}})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        answer = asked.answer
        self.assertTrue(answer["ok"], answer)
        [move] = [i for i in self.made() if i.type == "translation"]
        self.assertEqual(move.channel("pos.Z").get(), 0.0068)
        self.assertEqual(answer["result"]["channels"]["pos.Z"],
                         {"old": 0.0, "new": 0.0068, "item": move.id})
        name = self.kit.journal.name
        with open(os.path.join(self.home, "journals", name + ".jsonl")) as h:
            records = [json.loads(line) for line in h]
        tools = [r["tool"] for r in records if r.get("event") == "entry"]
        self.assertEqual(tools, ["backdrop", "set_channels"])
        # One Ctrl+Z puts it back.
        self.fake.undo()
        self.assertEqual(move.channel("pos.Z").get(), 0.0)


class BackdropToolTest(unittest.TestCase):
    """mt_backdrop on the server: the payload as the agent gave it, and the
    kit's answer with its undo step and journal entry."""

    def setUp(self):
        from modo_tools import bridge as bridge_mod
        self.fake, self.kit = support.start_kit(self)
        self.home = os.environ["MODO_TOOLS_HOME"]
        ops_view = sys.modules["modotools.ops_view"]
        real = ops_view.folder
        ops_view.folder = lambda: os.path.join(self.home, "temp")
        self.addCleanup(setattr, ops_view, "folder", real)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)
        self.path = os.path.join(self.home, "side.png").replace("\\", "/")
        with open(self.path, "wb") as handle:
            handle.write(fakelx.png(231, 270))

    def ask(self, coro):
        from test_server import InThread
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_the_tool_makes_the_backdrop(self):
        import json
        from modo_tools import server
        text = self.ask(server.mt_backdrop_text(
            self.path, "left", 0.142, height_m=0.150,
            center=[0, 0.0577, 0.0067], bridge=self.bridge))
        shown = json.loads(text)
        self.assertEqual(shown["view"], "left")
        self.assertEqual(shown["pixels"], [231, 244])
        self.assertIn("stretched", shown["note"])
        self.assertEqual(shown["undo"], {"step": 1})
        self.assertEqual(shown["journal"], 1)
        [item] = [i for i in self.fake.scene.items if i.type == "backdrop"]
        self.assertEqual(item.channel("projection").get(), "left")

    def test_left_out_arguments_are_the_kits_defaults(self):
        import json
        from modo_tools import server
        shown = json.loads(self.ask(server.mt_backdrop_text(
            self.path, "front", 0.231, bridge=self.bridge)))
        self.assertEqual(shown["rect"]["min"], [-0.1155, -0.135, 0.0])
        self.assertEqual(shown["opacity"], 0.65)
        self.assertIsNone(shown["stretched"])

    def test_a_refusal_reads_as_one(self):
        from modo_tools import server
        text = self.ask(server.mt_backdrop_text(
            self.path, "side", 0.1, bridge=self.bridge))
        self.assertTrue(text.startswith("bad_request:"), text)

    def test_the_description_says_what_it_does(self):
        from modo_tools import server
        text = server.BACKDROP_DESCRIPTION
        for words in ("left, right, front, back, top, bottom", "metres",
                      "stretched", "temp folder", "perspective", "One undo step",
                      "mt_undo_last", "Camera view", "visible"):
            self.assertIn(words, text)

    def test_set_channels_says_it_moves_a_backdrop(self):
        from modo_tools import server
        self.assertIn("backdrop", server.SET_CHANNELS_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
