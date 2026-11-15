"""command_info and find_commands."""

import sys
import unittest

import support


def job(kit, op, payload):
    jobs = sys.modules["modotools.jobs"]
    return jobs.run_job({"id": "t-1", "op": op, "payload": payload}, kit)


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.fake.describe("edge.bevel", "Edge Bevel", "Bevels edges", args=[
            ("width", "distance", 0, "Width"),
            ("segments", "integer", 1, "Segments"),
            ("mode", "string", 1 | 2 | 64, "Mode"),
        ])
        self.fake.tools["poly.bevel"] = [("inset", "distance", 0.0),
                                         ("shift", "distance", 0.0),
                                         ("segs", "integer", 1)]

    def test_command_info_lists_arguments_with_flags_decoded(self):
        answer = job(self.kit, "command_info", {"name": "edge.bevel"})
        self.assertTrue(answer["ok"], answer)
        info = answer["result"]
        self.assertEqual(info["kind"], "command")
        self.assertEqual(info["username"], "Edge Bevel")
        self.assertEqual(info["description"], "Bevels edges")
        args = info["arguments"]
        self.assertEqual([a["name"] for a in args], ["width", "segments", "mode"])
        self.assertFalse(args[0]["optional"])
        self.assertTrue(args[1]["optional"])
        self.assertEqual(args[1]["type"], "integer")
        self.assertTrue(args[2]["query"])
        self.assertTrue(args[2]["hidden"])
        self.assertEqual(self.fake.crashes, [])

    def test_command_info_on_a_tool_lists_attributes(self):
        answer = job(self.kit, "command_info", {"name": "poly.bevel"})
        info = answer["result"]
        self.assertEqual(info["kind"], "tool")
        self.assertEqual([a["name"] for a in info["attributes"]],
                         ["inset", "shift", "segs"])
        self.assertEqual(info["attributes"][2], {"name": "segs", "type": "integer",
                                                 "default": 1})
        self.assertIn("tool.set poly.bevel on", info["usage"])
        self.assertEqual(self.fake.crashes, [])

    def test_command_info_on_an_unknown_name(self):
        answer = job(self.kit, "command_info", {"name": "edge.bevle"})
        self.assertEqual(answer["error"]["type"], "unknown_command")
        self.assertIn("edge.bevel", answer["error"]["hint"])
        self.assertEqual(self.fake.crashes, [])

    def test_find_commands_by_part_of_a_name(self):
        answer = job(self.kit, "find_commands", {"query": "bevel", "limit": 10})
        result = answer["result"]
        self.assertEqual(result["commands"], [{"name": "edge.bevel",
                                               "username": "Edge Bevel"}])
        self.assertEqual(result["tools"], ["poly.bevel"])

    def test_find_commands_limit(self):
        for n in range(30):
            self.fake.describe("test.many%02d" % n, "Many %d" % n)
        answer = job(self.kit, "find_commands", {"query": "many", "limit": 5})
        self.assertEqual(len(answer["result"]["commands"]), 5)
        self.assertEqual(answer["result"]["total"], 30)

    # An argument's words, from Modo's own hints.

    PROJECTION = [(0, "top"), (1, "bot"), (2, "bck"), (3, "fnt"),
                  (4, "rgt"), (5, "lft"), (6, "psp"), (0, "=view-projection")]

    def test_a_hinted_argument_lists_its_words_with_their_integers(self):
        self.fake.describe("view3d.projection", "Projection", args=[
            ("type", "integer", 2, "Type")],
            hints={"type": self.PROJECTION})
        answer = job(self.kit, "command_info", {"name": "view3d.projection"})
        arg = answer["result"]["arguments"][0]
        # The "=view-projection" entry names the hint set; it isn't a word.
        self.assertEqual(arg["values"], {"top": 0, "bot": 1, "bck": 2, "fnt": 3,
                                         "rgt": 4, "lft": 5, "psp": 6})
        self.assertEqual(self.fake.crashes, [])

    def test_an_argument_without_hints_has_no_values(self):
        answer = job(self.kit, "command_info", {"name": "edge.bevel"})
        for arg in answer["result"]["arguments"]:
            self.assertNotIn("values", arg)

    def test_a_popup_lists_its_words(self):
        self.fake.describe("item.create", "Create Item", args=[
            ("type", "string", 0, "Type"), ("name", "string", 1, "Name")],
            popups={"type": ["group", "locator", "mesh"], "name": []})
        args = job(self.kit, "command_info",
                   {"name": "item.create"})["result"]["arguments"]
        self.assertEqual(args[0]["values"], ["group", "locator", "mesh"])
        self.assertNotIn("values", args[1])

    def test_a_long_popup_is_cut_and_says_how_many(self):
        ops_lookup = sys.modules["modotools.ops_lookup"]
        words = ["type%04d" % n for n in range(ops_lookup.VALUES_MAX + 7)]
        self.fake.describe("item.create", "Create Item", args=[
            ("type", "string", 0, "Type")], popups={"type": words})
        arg = job(self.kit, "command_info",
                  {"name": "item.create"})["result"]["arguments"][0]
        self.assertEqual(arg["values"], words[:ops_lookup.VALUES_MAX])
        self.assertEqual(arg["values_total"], len(words))

    def test_a_hint_read_that_raises_leaves_values_out(self):
        self.fake.describe("view3d.shadingStyle", "Shading", args=[
            ("style", "integer", 2, "Style"), ("bgmesh", "integer", 1, "Mesh")],
            hints={"style": "raise", "bgmesh": [(0, "active"), (1, "inactive")]},
            popups={"style": "raise"})
        answer = job(self.kit, "command_info", {"name": "view3d.shadingStyle"})
        self.assertTrue(answer["ok"], answer)
        style, bgmesh = answer["result"]["arguments"]
        self.assertNotIn("values", style)
        self.assertEqual(bgmesh["values"], {"active": 0, "inactive": 1})

    def test_a_tool_attribute_lists_its_words(self):
        self.fake.tools["prim.sphere"] = [
            ("radius", "distance", 0.5),
            ("method", "integer", 0, [(0, "globe"), (1, "qball"), (2, "tess"),
                                      (0, "=prim-sphere-method")])]
        attrs = job(self.kit, "command_info",
                    {"name": "prim.sphere"})["result"]["attributes"]
        self.assertNotIn("values", attrs[0])
        self.assertEqual(attrs[1]["values"], {"globe": 0, "qball": 1, "tess": 2})

    # On Modo 17.1 (2026-11-10): three reads Modo doesn't survive.

    def test_a_variable_arguments_hints_are_never_read(self):
        # tool.attr's value has no type until tool and attr are given.
        # Reading its hints from the bare command opens a dialog and Modo
        # never comes back.
        self.fake.describe("tool.attr", "Tool Attribute", args=[
            ("tool", "string", 0, "Tool"), ("attr", "string", 0, "Attribute"),
            ("value", "", 8 | 2, "Value")],
            hints={"value": "hang"}, popups={"value": "crash"})
        answer = job(self.kit, "command_info", {"name": "tool.attr"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.crashes, [])
        self.assertNotIn("values", answer["result"]["arguments"][2])

    def test_a_popup_known_to_crash_modo_is_never_read(self):
        # scene.tagAdd's taglist: PopCount crashed Modo, twice.
        self.fake.describe("scene.tagAdd", "Add Tag", args=[
            ("taglist", "string", 0, "Tags")], popups={"taglist": "crash"})
        answer = job(self.kit, "command_info", {"name": "scene.tagAdd"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.crashes, [])
        self.assertNotIn("values", answer["result"]["arguments"][0])

    def test_a_range_is_not_a_word(self):
        # clip.newSequence's width: [(1, "&min"), (65536, "&max")]. Bounds,
        # as "%min" and "%maxDegrees" are, not words the argument takes.
        self.fake.describe("clip.newSequence", "New Sequence", args=[
            ("type", "integer", 1, "Format"), ("width", "integer", 1, "Width"),
            ("step", "float", 1, "Step")], hints={
                "type": [(0, "Grey"), (1, "RGB"), (5, "&max")],
                "width": [(1, "&min"), (65536, "&max")],
                "step": [(1, "%min")]})
        kind, width, step = job(self.kit, "command_info", {
            "name": "clip.newSequence"})["result"]["arguments"]
        self.assertEqual(kind["values"], {"Grey": 0, "RGB": 1})
        self.assertNotIn("values", width)
        self.assertNotIn("values", step)

    def test_lookups_are_reads(self):
        jobs = sys.modules["modotools.jobs"]
        for name in ("command_info", "find_commands", "event_log"):
            self.assertTrue(jobs.is_read(name), name)
        for name in ("run_command", "run_python", "set_channels", "save_scene"):
            self.assertFalse(jobs.is_read(name), name)


if __name__ == "__main__":
    unittest.main()
