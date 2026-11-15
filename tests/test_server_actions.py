"""On the server: mt_begin, mt_commit and mt_rollback, the
session in hello, the six names refused before sending, and the answers'
text."""

import asyncio
import json
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import protocol, server
from test_server import InThread

LABEL = "Raise the cube's top"
CUBE = "lx.eval('test.cube')"


class ServerActionTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def run_calls(self, make):
        """make(bridge) -> a coroutine; run on a helper thread while this
        thread ticks Modo."""
        job = InThread(make(self.bridge))
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_begin_commit_text(self):
        async def calls(bridge):
            begun = await server.mt_begin_text(LABEL, bridge=bridge)
            one = await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            committed = await server.mt_commit_text(bridge=bridge)
            return begun, one, committed
        begun, one, committed = self.run_calls(calls)
        self.assertEqual(json.loads(begun), {"action": LABEL})
        self.assertEqual(json.loads(one)["undo"], {"action": LABEL, "step": 1})
        self.assertEqual(json.loads(committed), {
            "action": LABEL, "steps": 1, "presses": 1,
            "undo": "1 Ctrl+Z undoes the whole action"})

    def test_rollback_text(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            return await server.mt_rollback_text(bridge=bridge)
        shown = json.loads(self.run_calls(calls))
        self.assertEqual(shown, {"action": LABEL, "rolled_back": 1, "left": 0,
                                 "scene": "as before mt_begin"})

    def test_a_failed_call_says_the_action_is_rolled_back(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            return await server.mt_run_python_text(
                CUBE + "\nraise ValueError('half')", "half", bridge=bridge)
        text = self.run_calls(calls)
        self.assertTrue(text.startswith("python_error:"), text)
        self.assertIn("\nRolled back: '%s' (2 steps) is undone and closed. The "
                      "scene is as it was before mt_begin." % LABEL, text)

    def test_a_refused_call_says_the_action_is_still_open(self):
        # A bad_channel in a test rolled back nine steps.
        self.fake.scene.add("mesh001", "Cube")
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            text = await server.call_text(
                "set_channels", {"item": "mesh001", "channels": {"nope": 1}},
                bridge=bridge)
            return text, bridge.action
        text, still = self.run_calls(calls)
        self.assertTrue(text.startswith("bad_channel:"), text)
        self.assertIn("\nNothing was written. The action '%s' is still open "
                      "(2 steps)." % LABEL, text)
        self.assertNotIn("Rolled back", text)
        self.assertEqual(still, LABEL)

    def test_a_kept_refused_step_doesnt_read_as_nothing_written(self):
        # Where the call's own step couldn't be undone
        # (the manual pump, rollback off), a run_python refused half way
        # has written.
        line = server.rollback_line({"action": LABEL, "refused": True,
                                     "steps": 2, "kept": True})
        self.assertNotIn("Nothing was written", line)
        self.assertIn("'%s' is still open (2 steps)" % LABEL, line)
        self.assertIn("couldn't be undone", line)
        self.assertIn("mt_rollback", line)

    def test_begin_says_which_calls_leave_the_action_open(self):
        text = server.BEGIN_DESCRIPTION
        for words in ("bad_request", "unknown_command", "in_action",
                      "bad_channel", "no_such_item", "ambiguous_item",
                      "leaves the action open", "refused",
                      "after changing the scene rolls back the whole action"):
            self.assertIn(words, text)

    def test_the_six_names_are_refused_before_sending(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            jobs = self.kit.pump.jobs_run
            texts = [await server.mt_run_command_text(name, bridge=bridge)
                     for name in ("app.undo", "app.redo", "scene.new",
                                  "scene.close", "scene.revert")]
            texts.append(await server.mt_run_python_text(
                "lx.eval('scene.open C:/a.lxo')", "open", bridge=bridge))
            # Not one reached the kit.
            self.assertEqual(self.kit.pump.jobs_run, jobs)
            await server.mt_commit_text(bridge=bridge)
            after = await server.mt_run_command_text("scene.new", bridge=bridge)
            return texts, after
        ran = len(self.fake.ran)
        texts, after = self.run_calls(calls)
        for text in texts:
            self.assertTrue(text.startswith("in_action:"), text)
            self.assertIn(LABEL, text)
        sent = self.fake.ran[ran:]
        for name in ("app.undo", "app.redo", "scene.close", "scene.revert",
                     "scene.open"):
            self.assertNotIn(name, sent)
        # Committed: scene.new runs again, with its lock note.
        self.assertIn("Note:", after)
        self.assertIn("scene.new", sent)

    def test_the_session_holds_across_reconnects_and_tells_servers_apart(self):
        other = bridge_mod.Bridge(port=self.kit.port)
        self.assertNotEqual(self.bridge.session, other.session)

        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await bridge.close()            # reconnects on the next call
            mine = await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            theirs = await server.mt_run_python_text(CUBE, "cube", bridge=other)
            await other.close()
            return mine, theirs
        mine, theirs = self.run_calls(calls)
        self.assertEqual(json.loads(mine)["undo"], {"action": LABEL, "step": 1})
        self.assertTrue(theirs.startswith("in_action:"), theirs)

    def test_status_names_the_action(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            return await server.mt_status_text(bridge=bridge)
        status = json.loads(self.run_calls(calls))
        self.assertEqual(status["action"]["label"], LABEL)

    def test_an_idle_commit_is_said(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            self.kit.action.idle_s = 0.0
            await asyncio.sleep(0.2)        # ticks commit it
            return await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
        text = self.run_calls(calls)
        self.assertIn("Action '%s' was committed as it stood after it sat "
                      "idle: 1 Ctrl+Z in Modo undo it." % LABEL, text)
        self.assertEqual(json.loads(text.split("\n\n")[0])["undo"], {"step": 1})
        self.assertEqual(self.bridge.action, None)

    def test_an_action_the_kit_closed_unseen_stops_refusing(self):
        # After modoTools.stop (or a crash) committed the
        # action, the server went on refusing the six names for good.
        async def begin(bridge):
            return await server.mt_begin_text(LABEL, bridge=bridge)
        self.run_calls(begin)
        self.kit.action.stop()
        # A new loop needs a new connection; same server, same session.
        session, action = self.bridge.session, self.bridge.action
        self.bridge = bridge_mod.Bridge(port=self.kit.port)
        self.bridge.session, self.bridge.action = session, action

        async def calls(bridge):
            one = await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            new = await server.mt_run_command_text("scene.new", bridge=bridge)
            return one, new
        one, new = self.run_calls(calls)
        self.assertEqual(json.loads(one)["undo"], {"step": 1})
        self.assertIsNone(self.bridge.action)
        self.assertFalse(new.startswith("in_action:"), new)

    def test_a_commit_with_no_action_open_stops_refusing(self):
        self.bridge.action = LABEL           # what a stale answer left

        async def calls(bridge):
            return await server.mt_commit_text(bridge=bridge)
        text = self.run_calls(calls)
        self.assertTrue(text.startswith("bad_request:"), text)
        self.assertIsNone(self.bridge.action)

    def test_status_with_no_action_stops_refusing(self):
        self.bridge.action = LABEL

        async def calls(bridge):
            return await server.mt_status_text(bridge=bridge)
        self.run_calls(calls)
        self.assertIsNone(self.bridge.action)

    def test_a_refusal_inside_the_action_keeps_it(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            return await server.mt_run_python_text(
                "lx.eval('app.' + 'undo')", "sneak", bridge=bridge)
        text = self.run_calls(calls)
        self.assertTrue(text.startswith("in_action:"), text)
        self.assertEqual(self.bridge.action, LABEL)

    def test_action_undone_text(self):
        async def calls(bridge):
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            return None
        self.run_calls(calls)
        self.fake.undo()                    # the user's Ctrl+Z

        async def again(bridge):
            return await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)
        self.bridge.session = self.kit.action.open.session
        text = self.run_calls(again)
        self.assertTrue(text.startswith("action_undone:"), text)
        self.assertIn("mt_describe_mesh", text)


class CrashTest(unittest.TestCase):
    def test_a_crash_mid_action_names_the_action_and_step(self):
        lines = [json.dumps(e) for e in (
            {"event": "session"},
            {"event": "action_begin", "label": LABEL},
            {"event": "op_start", "op": "run_python", "id": "r-1",
             "preview": "x", "action": LABEL, "step": 1},
            {"event": "op_ok", "op": "run_python", "id": "r-1"},
            {"event": "op_start", "op": "run_python", "id": "r-2",
             "preview": "poly.bevel", "action": LABEL, "step": 2})]
        self.assertEqual(protocol.oplog_ending(lines),
                         "Modo stopped during run_python: poly.bevel "
                         "(in action '%s', step 2)" % LABEL)
        self.assertEqual(protocol.oplog_ending(lines[:4]),
                         "Modo stopped during action '%s' (step 1)" % LABEL)
        # A failed call's rollback of the whole action.
        rolling = lines[:3] + [json.dumps(e) for e in (
            {"event": "op_error", "op": "run_python", "id": "r-1"},
            {"event": "rollback", "op": "run_python", "id": "r-1"},
            {"event": "action_rollback", "label": LABEL, "steps": 1})]
        self.assertEqual(protocol.oplog_ending(rolling),
                         "Modo stopped during the rollback of run_python: x")
        committed = lines[:4] + [json.dumps({"event": "action_commit"})]
        self.assertEqual(protocol.oplog_ending(committed), "ended cleanly")


class SchemaTest(unittest.TestCase):
    def tools(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        app = server.build_server()

        async def tools():
            return {t.name: t for t in await app.list_tools()}
        return asyncio.run(tools())

    def test_the_three_tools(self):
        tools = self.tools()
        schema = tools["mt_begin"].input_schema
        self.assertEqual(schema["required"], ["label"])
        self.assertEqual(tools["mt_commit"].input_schema.get("required", []), [])
        self.assertEqual(tools["mt_rollback"].input_schema.get("required", []), [])
        self.assertIn("presses", tools["mt_commit"].description)
        self.assertIn("left", tools["mt_rollback"].description)


if __name__ == "__main__":
    unittest.main()


class ServerUndoLastTest(unittest.TestCase):
    """Mt_undo_last's text, and the two descriptions that point
    at actions."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def run_calls(self, make):
        job = InThread(make(self.bridge))
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_undo_last_text_names_the_call(self):
        async def calls(bridge):
            await server.mt_run_python_text(CUBE, "fit the body", bridge=bridge)
            return await server.mt_undo_last_text(bridge=bridge)
        self.assertEqual(self.run_calls(calls),
                         'Undone: journal 1 "fit the body" (run_python). The '
                         "scene is as it was before it; the user's Ctrl+Shift+Z in "
                         "Modo puts it back.")

    def test_undo_last_refusals_come_back_typed(self):
        async def calls(bridge):
            empty = await server.mt_undo_last_text(bridge=bridge)
            await server.mt_begin_text(LABEL, bridge=bridge)
            await server.mt_run_python_text(CUBE, "cube", bridge=bridge)
            in_action = await server.mt_undo_last_text(bridge=bridge)
            return empty, in_action
        empty, in_action = self.run_calls(calls)
        self.assertTrue(empty.startswith("bad_request:"), empty)
        self.assertTrue(in_action.startswith("in_action:"), in_action)
        self.assertIn("mt_rollback", in_action)
        # The refusal doesn't close the server's idea of the action.
        self.assertEqual(self.bridge.action, LABEL)

    def test_the_descriptions_point_at_actions(self):
        for text in (server.RUN_PYTHON_DESCRIPTION,
                     server.RUN_COMMAND_DESCRIPTION):
            self.assertIn("mt_begin", text)
            self.assertIn("mt_rollback", text)
            self.assertIn("mt_undo_last", text)
        self.assertIn("undone by the agent", server.JOURNAL_DESCRIPTION)
        # The review: only this session's own calls.
        self.assertIn("this session's newest call", server.UNDO_LAST_DESCRIPTION)
        self.assertIn("another agent session's call",
                      server.UNDO_LAST_DESCRIPTION)
