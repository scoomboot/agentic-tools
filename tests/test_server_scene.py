"""The scene tools on the server side: the size budget, and the screenshot
read, deleted and handed over as an image."""

import asyncio
import json
import os
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import budget, server
from test_scene import make_scene
from test_server import InThread


def big_graph(count):
    """`count` items: 50 locators of 39 meshes each, and the rest as roots."""
    items, made = [], 0
    while made < count:
        node = {"name": "Group %d" % made, "id": "locator%04d" % made,
                "type": "locator", "children": []}
        made += 1
        for _ in range(min(39, count - made)):
            node["children"].append({"name": "Part %d" % made,
                                     "id": "mesh%04d" % made, "type": "mesh"})
            made += 1
        if not node["children"]:
            del node["children"]
        items.append(node)
    return {"items": items, "count": count}


class BudgetTest(unittest.TestCase):
    def test_a_big_graph_is_cut_and_says_so(self):
        text = budget.fit_graph(big_graph(2000), 10000)
        self.assertLessEqual(len(text), 10000)
        shown = json.loads(text)
        self.assertRegex(shown["truncated"],
                         r"^\d+ of 2,000 items shown; ask with root= or types=")
        kept = sum(1 + len(n.get("children", [])) for n in shown["items"])
        self.assertIn("%d of 2,000" % kept, shown["truncated"])
        # The cut is at the end: whatever is shown is the start of the tree.
        self.assertEqual(shown["items"][0]["id"], "locator0000")

    def test_a_cut_parent_says_how_many_children_it_lost(self):
        text = budget.fit_graph(big_graph(2000), 3000)
        last = json.loads(text)["items"][-1]
        if "children" in last or "more" in last:
            self.assertEqual(len(last.get("children", [])) + last.get("more", 0), 39)

    def test_a_small_answer_is_left_whole(self):
        small = big_graph(10)
        self.assertEqual(json.loads(budget.fit_graph(small, 10000)), small)

    def test_channels_are_cut_and_say_so(self):
        item = {"id": "mesh002", "channels": {"ch%03d" % i: i for i in range(500)}}
        text = budget.fit_entries(item, "channels", 2000, "channels",
                                  "ask for channels by name")
        self.assertLessEqual(len(text), 2000)
        shown = json.loads(text)
        self.assertIn("of 500 channels shown; ask for channels by name",
                      shown["truncated"])
        self.assertEqual(shown["id"], "mesh002")

    def test_selected_indices_are_cut_and_say_so(self):
        found = {"type": "polygon", "meshes": [
            {"id": "mesh002", "name": "Body", "indices": list(range(3000))}]}
        text = budget.fit_selection(found, 2000)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("of 3,000 polygons shown", json.loads(text)["truncated"])

    def test_any_text_is_cut_last(self):
        text = budget.cut_text("x" * 5000, 1000)
        self.assertLessEqual(len(text), 1000)
        self.assertIn("cut at 1,000 characters", text)

    def test_the_budget_comes_from_the_environment(self):
        os.environ["MODO_TOOLS_MAX_CHARS"] = "12345"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_MAX_CHARS", None)
        self.assertEqual(budget.max_chars(), 12345)
        os.environ["MODO_TOOLS_MAX_CHARS"] = "lots"
        self.assertEqual(budget.max_chars(), budget.DEFAULT_MAX_CHARS)


class SceneToolsTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def ask(self, coro):
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_a_2000_item_scene_is_cut_at_the_budget(self):
        os.environ["MODO_TOOLS_MAX_CHARS"] = "10000"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_MAX_CHARS", None)
        scene = self.fake.scene
        for n in range(50):
            group = scene.add("locator%04d" % n, "Group %d" % n, "locator")
            for m in range(39):
                scene.add("mesh%02d%02d" % (n, m), "Part", "mesh", parent=group)
        text = self.ask(server.mt_scene_graph_text(bridge=self.bridge))
        self.assertLessEqual(len(text), 10000)
        self.assertIn("of 2,000 items shown", json.loads(text)["truncated"])

    def test_get_item_by_name(self):
        make_scene(self.fake.scene)
        text = self.ask(server.mt_get_item_text("Body", bridge=self.bridge))
        self.assertEqual(json.loads(text)["id"], "mesh002")

    def test_screenshot_is_read_and_its_file_deleted(self):
        png, text = self.ask(server.mt_screenshot_parts(bridge=self.bridge))
        self.assertEqual(png[:8], bytes([137, 80, 78, 71, 13, 10, 26, 10]))
        self.assertIn("1280x720", text)
        self.assertIn("viewport's size", text)
        from modotools import ops_view
        self.assertEqual([f for f in os.listdir(ops_view.folder())
                          if f.endswith(".png")], [])

    def test_no_viewport(self):
        self.fake.scene.viewport = False
        png, text = self.ask(server.mt_screenshot_parts(bridge=self.bridge))
        self.assertIsNone(png)
        self.assertTrue(text.startswith("no_viewport:"), text)

    def test_the_screenshot_tool_returns_image_content(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        png, _text = self.ask(server.mt_screenshot_parts(bridge=self.bridge))
        real = server.mt_screenshot_parts
        asked = []

        async def parts(size=None, bridge=None):
            asked.append(size)
            return png, "1280x720, the viewport's size"

        server.mt_screenshot_parts = parts
        self.addCleanup(setattr, server, "mt_screenshot_parts", real)
        result = asyncio.run(server.build_server().call_tool("mt_screenshot", {}))
        content = result[0] if isinstance(result, tuple) else result.content
        self.assertEqual([c.type for c in content], ["image", "text"])
        mime = getattr(content[0], "mime_type", None) or content[0].mimeType  # 2.x, 1.x
        self.assertEqual(mime, "image/png")
        asyncio.run(server.build_server().call_tool("mt_screenshot", {"size": 800}))
        self.assertEqual(asked, [None, 800])

    # -- size ------------------------------------------------------

    def test_a_size_gives_a_smaller_picture_and_deletes_both_files(self):
        self.fake.scene.viewport_size = (1320, 1260)
        png, text = self.ask(server.mt_screenshot_parts(size=800, bridge=self.bridge))
        self.assertEqual(server.png_size(png), (800, 764))
        self.assertIn("800x764, scaled from the viewport's 1320x1260", text)
        from modotools import ops_view
        self.assertEqual([f for f in os.listdir(ops_view.folder())
                          if f.endswith(".png")], [])

    def test_a_bad_size_is_bad_request(self):
        png, text = self.ask(server.mt_screenshot_parts(size=10, bridge=self.bridge))
        self.assertIsNone(png)
        self.assertTrue(text.startswith("bad_request:"), text)

    def test_a_failed_scaling_says_so(self):
        self.fake.scene.viewport_size = (1320, 1260)
        self.fake.image_save_fails = True
        png, text = self.ask(server.mt_screenshot_parts(size=800, bridge=self.bridge))
        self.assertEqual(server.png_size(png), (1320, 1260))
        self.assertIn("1320x1260, the viewport's size", text)
        self.assertIn("couldn't be scaled", text)


    def test_a_kit_that_ignores_size_says_so(self):
        # an older kit takes size and ignores it;
        # its answer has no size and no viewport.
        png, _text = self.ask(server.mt_screenshot_parts(bridge=self.bridge))
        from modotools import ops_view
        os.makedirs(ops_view.folder(), exist_ok=True)
        path = os.path.join(ops_view.folder(), "shot_old.png")
        for size, told in ((800, True), (None, False), (4000, False)):
            with open(path, "wb") as handle:
                handle.write(png)
            old = StubBridge({"id": "r-1", "ok": True, "result": {"path": path}})
            data, text = self.ask(server.mt_screenshot_parts(size=size, bridge=old))
            self.assertEqual(data, png)
            self.assertIn("1280x720, the viewport's size", text)
            self.assertEqual("older than the server" in text, told, text)


class StubBridge(object):
    """Answers every call with one canned response."""

    def __init__(self, response):
        self.response = response

    async def call(self, op, payload=None, timeout_ms=None):
        return dict(self.response)


class WholeAnswerBudgetTest(unittest.TestCase):
    """Every answer fits the budget, errors and late results included."""

    def setUp(self):
        os.environ["MODO_TOOLS_MAX_CHARS"] = "5000"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_MAX_CHARS", None)

    def test_an_error_with_a_huge_stdout_is_cut(self):
        response = {"id": "r-1", "ok": False, "stdout": "x" * 50000,
                    "error": {"type": "python_error", "message": "boom",
                              "traceback": "y" * 20000, "event_log": [],
                              "hint": ""}}
        text = asyncio.run(server.mt_run_python_text(
            "print('x')", purpose="p", bridge=StubBridge(response)))
        self.assertTrue(text.startswith("python_error: boom"), text[:80])
        self.assertLessEqual(len(text), 5000)
        self.assertIn("cut at", text)

    def test_huge_late_results_are_cut(self):
        late = [{"id": "r-%d" % n, "ok": True, "result": "z" * 3000}
                for n in range(10)]
        response = {"id": "r-11", "ok": True, "result": {"lines": ["a"]},
                    "stdout": "", "late": late}
        text = asyncio.run(server.call_text("event_log", {},
                                            bridge=StubBridge(response)))
        self.assertLessEqual(len(text), 5000)
        self.assertIn("Earlier calls", text)
        self.assertIn("cut at", text)


class PythonShapeTest(unittest.TestCase):
    def test_a_caught_refusal_reaches_the_agent(self):
        response = {"id": "r-1", "ok": True, "stdout": "",
                    "result": {"result": 1, "refused": ["refused 'q': no"]}}
        text = asyncio.run(server.mt_run_python_text(
            "RESULT = 1", purpose="p", bridge=StubBridge(response)))
        self.assertIn("refused 'q': no", text)


class PathTest(unittest.TestCase):
    def test_only_the_kits_own_shots_are_deleted(self):
        self.assertTrue(server.is_shot("C:/Users/someone/AppData/Local/Temp/"
                                       "modo_tools/shot_ab12.png"))
        self.assertFalse(server.is_shot("C:/models/striker/striker.lxo"))
        self.assertFalse(server.is_shot("C:/Users/someone/Pictures/shot_ab12.png"))


if __name__ == "__main__":
    unittest.main()
