"""The run tools on the server side: the risky list, the lock note, Python
switched off, and answers from the kit in fake Modo."""

import asyncio
import json
import os
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import risky, server
from test_server import InThread, free_port


class RiskyTest(unittest.TestCase):
    def test_deformer_freeze_is_risky(self):
        found = risky.scan("deformer.freeze")
        self.assertEqual([i.command for i in found.refused], ["deformer.freeze"])

    def test_a_query_is_not_risky(self):
        self.assertEqual(risky.scan("mesh.psubSubdiv ?").refused, [])

    def test_a_longer_name_is_not_a_match(self):
        self.assertEqual(risky.scan("deformer.freezeAll").refused, [])

    def test_scene_open_is_risky(self):
        # It froze Modo 17.1 on 2026-11-08, and with a bad path it opens a
        # dialog asking for the file.
        found = risky.scan("lx.eval('scene.open \"C:/a.lxo\"')")
        self.assertEqual([i.command for i in found.refused], ["scene.open"])
        self.assertIn("File > Open", found.refused[0].advice)

    def test_scene_new_gets_the_lock_note(self):
        found = risky.scan("lx.eval('scene.new')")
        self.assertEqual(found.refused, [])
        self.assertEqual(found.locks, ["scene.new"])


class RefusedBeforeSendingTest(unittest.TestCase):
    """A risky call is refused by the server: nothing reaches Modo, so
    nothing needs to be listening."""

    def setUp(self):
        support.temp_home(self)
        self.bridge = bridge_mod.Bridge(port=free_port())

    def test_deformer_freeze_refused(self):
        text = asyncio.run(server.mt_run_command_text(
            "deformer.freeze", bridge=self.bridge))
        self.assertTrue(text.startswith("risky:"), text)
        self.assertIn("acknowledge_risk=true", text)
        self.assertIn("save the scene", text.lower())

    def test_deformer_freeze_in_python_refused(self):
        text = asyncio.run(server.mt_run_python_text(
            "lx.eval('deformer.freeze')", purpose="bake", bridge=self.bridge))
        self.assertTrue(text.startswith("risky:"), text)

    def test_scene_open_refused(self):
        text = asyncio.run(server.mt_run_command_text(
            "scene.open C:/nothing.lxo", bridge=self.bridge))
        self.assertTrue(text.startswith("risky:"), text)

    def test_python_switched_off(self):
        os.environ["MODO_TOOLS_ALLOW_PYTHON"] = "0"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_ALLOW_PYTHON", None)
        text = asyncio.run(server.mt_run_python_text(
            "RESULT = 1", purpose="x", bridge=self.bridge))
        self.assertTrue(text.startswith("python_disabled:"), text)

    def test_python_needs_a_purpose(self):
        text = asyncio.run(server.mt_run_python_text(
            "RESULT = 1", purpose=" ", bridge=self.bridge))
        self.assertTrue(text.startswith("bad_request:"), text)


class ThroughTheKitTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def ask(self, coro):
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_acknowledged_deformer_freeze_runs(self):
        text = self.ask(server.mt_run_command_text(
            "deformer.freeze", acknowledge_risk=True, bridge=self.bridge))
        self.assertIn("deformer.freeze", self.fake.ran)
        self.assertFalse(text.startswith("risky"), text)

    def test_scene_new_runs_with_the_lock_note(self):
        text = self.ask(server.mt_run_command_text("scene.new", bridge=self.bridge))
        self.assertIn("scene.new", self.fake.ran)
        self.assertIn("Note:", text)
        self.assertIn("alone", text)

    def test_python_result_and_stdout(self):
        text = self.ask(server.mt_run_python_text(
            "print('hi')\nRESULT = 7", purpose="seven", bridge=self.bridge))
        shown = json.loads(text)
        # A changing call's answer names its journal entry.
        self.assertEqual(shown, {"result": 7, "stdout": "hi\n",
                                 "undo": {"step": 1}, "journal": 1})

    # -- the undo line ----------------------------------------------------

    def test_a_changing_answer_says_its_undo_step(self):
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation", channels={"pos.X": 0.0})
        text = self.ask(server.call_text(
            "set_channels", {"item": "mesh001", "channels": {"pos.X": 2.0}},
            bridge=self.bridge))
        self.assertEqual(json.loads(text)["undo"], {"step": 1})

    def test_read_only_is_sent_through(self):
        text = self.ask(server.mt_run_python_text(
            "RESULT = 1", purpose="count", read_only=True, bridge=self.bridge))
        self.assertEqual(json.loads(text), {"result": 1, "stdout": "",
                                            "undo": None})
        self.assertIn("modoTools.execRead", self.fake.ran)
        self.assertNotIn("modoTools.exec", self.fake.ran)

    # -- the rollback line --------------------------------------------

    def test_a_failed_call_says_it_was_rolled_back(self):
        text = self.ask(server.mt_run_python_text(
            "lx.eval('test.cube')\nraise ValueError('half')", purpose="half",
            bridge=self.bridge))
        self.assertTrue(text.startswith("python_error:"), text)
        self.assertIn("\nRolled back: the scene is as it was before this call."
                      '\nundo: {"rolled_back": true}', text)

    def test_a_failed_call_the_kit_couldnt_check_says_unsure(self):
        self.fake.silent_markers = True
        text = self.ask(server.mt_run_python_text(
            "raise ValueError('half')", purpose="half", bridge=self.bridge))
        self.assertIn("\nRollback unsure: check the scene.", text)
        self.assertIn('\nundo: {"step": 1, "rolled_back": "unsure"}', text)
        status = json.loads(self.ask(server.mt_status_text(
            bridge=bridge_mod.Bridge(port=self.kit.port))))
        self.assertEqual(status["rollback"], "off")

    def test_a_failed_call_after_rollback_turned_off_asks_for_one_press(self):
        self.kit.rollback.off = True
        text = self.ask(server.mt_run_python_text(
            "raise ValueError('half')", purpose="half", bridge=self.bridge))
        self.assertIn("\nNot rolled back: one Ctrl+Z in Modo undoes this "
                      "call's changes.", text)
        self.assertIn('"presses": 1', text)

    def test_a_failed_read_only_call_has_no_rollback_line(self):
        text = self.ask(server.mt_run_python_text(
            "raise ValueError('no')", purpose="x", read_only=True,
            bridge=self.bridge))
        self.assertNotIn("olled back", text)
        self.assertIn("\nundo: null", text)

    def test_status_says_rollback(self):
        text = self.ask(server.mt_status_text(bridge=self.bridge))
        self.assertEqual(json.loads(text)["rollback"], "idle")

    def test_a_read_has_no_undo_line(self):
        text = self.ask(server.mt_scene_summary_text(bridge=self.bridge))
        self.assertNotIn("undo", text)

    def test_status_names_the_undo_way(self):
        text = self.ask(server.mt_status_text(bridge=self.bridge))
        self.assertEqual(json.loads(text)["undo"], "exec command")

    def test_an_error_carries_type_and_hint(self):
        self.fake.tools["poly.bevel"] = [("inset", "distance", 0.0)]
        text = self.ask(server.mt_run_command_text("poly.bevel inset:0.01",
                                                   bridge=self.bridge))
        self.assertTrue(text.startswith("unknown_command:"), text)
        self.assertIn("is a tool, not a command", text)

    def test_command_info(self):
        self.fake.tools["poly.bevel"] = [("inset", "distance", 0.0)]
        text = self.ask(server.call_text("command_info", {"name": "poly.bevel"},
                                         bridge=self.bridge))
        self.assertEqual(json.loads(text)["kind"], "tool")


class ValuesDescriptionTest(unittest.TestCase):
    """The agent is told where an argument's words are, and that
    a word Modo didn't know comes back as a note."""

    def test_command_info_says_it_lists_values(self):
        self.assertIn("values", server.COMMAND_INFO_DESCRIPTION)
        self.assertIn("fnt", server.COMMAND_INFO_DESCRIPTION)

    def test_run_command_says_a_stray_word_is_a_note(self):
        self.assertIn("note", server.RUN_COMMAND_DESCRIPTION)
        self.assertIn("values", server.RUN_COMMAND_DESCRIPTION)

    def test_run_command_says_a_word_after_a_named_one_is_dropped(self):
        self.assertIn("after a named one", server.RUN_COMMAND_DESCRIPTION)


class ToolListTest(unittest.TestCase):
    def names(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        app = server.build_server()

        async def names():
            return sorted(t.name for t in await app.list_tools())

        return asyncio.run(names())

    def test_all_tools(self):
        self.assertEqual(self.names(), [
            "mt_backdrop", "mt_begin", "mt_command_info", "mt_commit", "mt_describe_mesh",
            "mt_event_log", "mt_find_commands", "mt_fit_backdrops",
            "mt_get_item", "mt_get_selection", "mt_journal", "mt_op_log",
            "mt_points", "mt_read_doc", "mt_rollback",
            "mt_run_command", "mt_run_python", "mt_save_scene", "mt_scene_graph",
            "mt_scene_summary", "mt_screenshot", "mt_search_docs",
            "mt_set_channels",
            "mt_set_points", "mt_status", "mt_undo_last"])

    def test_no_python_tool_when_switched_off(self):
        os.environ["MODO_TOOLS_ALLOW_PYTHON"] = "0"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_ALLOW_PYTHON", None)
        self.assertNotIn("mt_run_python", self.names())


if __name__ == "__main__":
    unittest.main()
