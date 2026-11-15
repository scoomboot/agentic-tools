"""Undo the last call (mt_undo_last).

The newest undo step is undone when it is an agent call's, and the answer
names the call; the journal marks the entry undone by the agent. The user's step
on top, an open action, an empty stack and a read-only call are refused.
The fake undo stack is built to probes, as in test_actions.
"""

import json
import os
import sys
import unittest

import support


def journal_records():
    folder = os.path.join(os.environ["MODO_TOOLS_HOME"], "journals")
    names = sorted(n for n in os.listdir(folder) if n.endswith(".jsonl"))
    with open(os.path.join(folder, names[-1]), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class UndoLastTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello(session="agent-a")
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})
        self.n = 0

    def ask(self, op, payload=None, client=None):
        self.n += 1
        asked = support.Asked(client or self.client,
                              {"id": "u-%d" % self.n, "op": op,
                               "payload": payload or {}})
        if self.kit.pump.strategy_name == "manual":
            def done():
                if self.kit.pump.queue.pending():
                    self.fake.run_command("modoTools.pump")
                return asked.done()
            self.assertTrue(support.pump_until(self.fake, done))
        else:
            self.assertTrue(support.pump_until(self.fake, asked.done))
        return asked.answer

    def cube(self, purpose="a cube"):
        answer = self.ask("run_python", {"code": "lx.eval('test.cube')",
                                         "purpose": purpose})
        self.assertTrue(answer["ok"], answer)
        return answer

    def undo_last(self):
        return self.ask("undo_last")

    def meshes(self):
        return len([i for i in self.fake.scene.items if i.type == "mesh"])

    def pos_x(self):
        return self.fake.scene.find("translation005").channel("pos.X").get()

    def sent(self, command):
        return [e for e in self.fake.evals if e.lstrip("!") == command]

    def undone_records(self):
        return [r for r in journal_records()
                if r["event"] in ("undone", "redone")]

    # -- the undo --------------------------------------------------------------

    def test_an_agent_step_is_undone_and_named(self):
        self.cube()
        self.cube("fit the body")
        answer = self.undo_last()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"], {
            "journal": 2, "op": "run_python", "purpose": "fit the body",
            "scene": "as it was before that call"})
        self.assertEqual(self.meshes(), 2)
        self.assertNotIn("undo", answer)

    def test_the_journal_marks_the_entry_undone_by_the_agent(self):
        self.cube()
        self.cube()
        self.undo_last()
        self.assertEqual(self.undone_records()[-1]["n"], 2)
        self.assertEqual(self.undone_records()[-1]["by"], "agent")
        self.assertEqual(len(self.undone_records()), 1)
        # The user's Ctrl+Shift+Z puts it back, as for their own undo.
        self.assertTrue(self.fake.redo())
        last = self.undone_records()[-1]
        self.assertEqual((last["event"], last["n"]), ("redone", 2))

    def test_two_calls_back_is_two_undos(self):
        self.cube("one")
        self.cube("two")
        first = self.undo_last()["result"]
        second = self.undo_last()["result"]
        self.assertEqual((first["purpose"], second["purpose"]), ("two", "one"))
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(len(self.sent("app.undo")), 2)

    def test_it_runs_outside_any_command(self):
        self.cube()
        ran = len(self.fake.ran)
        self.undo_last()
        self.assertEqual(self.fake.ran[ran:], ["app.undo"])

    def test_a_set_channels_step_is_named_by_its_op(self):
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"pos.X": 2.0}})
        self.assertTrue(answer["ok"], answer)
        result = self.undo_last()["result"]
        self.assertEqual((result["op"], result["purpose"]), ("set_channels", None))
        self.assertEqual(self.pos_x(), 0.0)

    def test_a_read_after_the_call_is_stepped_over(self):
        self.cube()
        self.ask("run_python", {"code": "RESULT = 1", "purpose": "read",
                                "read_only": True})
        self.ask("scene_summary")
        result = self.undo_last()["result"]
        self.assertEqual(result["journal"], 1)
        self.assertEqual(self.meshes(), 1)

    # -- refusals --------------------------------------------------------------

    def test_erics_step_on_top_is_refused_and_named(self):
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("isn't an agent call's", answer["error"]["message"])
        self.assertIn("the user's edit in Modo, to Translation",
                      answer["error"]["message"])
        self.assertIn('journal 1 "a cube" (run_python)', answer["error"]["hint"])
        # Undone and redone at once: their edit and the cube both stay.
        self.assertEqual(len(self.sent("app.undo")), 1)
        self.assertEqual(len(self.sent("app.redo")), 1)
        self.assertEqual(self.pos_x(), 3.0)
        self.assertEqual(self.meshes(), 2)
        self.assertEqual(self.undone_records(), [])

    def test_erics_step_heard_by_no_listener_is_still_refused(self):
        self.cube()
        self.kit.listen.events = 0     # say, a selection: no scene event
        self.kit.listen.items = []
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        self.kit.listen.events = 0
        self.kit.listen.items = []
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("a selection or an edit", answer["error"]["message"])
        self.assertEqual(self.pos_x(), 3.0)
        self.assertEqual(self.meshes(), 2)

    def test_a_redo_that_fails_says_his_step_is_still_undone(self):
        # The review: the answer said "Nothing was undone" whatever the
        # redo did.
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        self.fake.builtin["app.redo"] = self.fake.builtin["test.fails"]
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertIn("app.redo failed", answer["error"]["message"])
        self.assertIn("still undone", answer["error"]["message"])
        self.assertNotIn("Nothing was undone", answer["error"]["message"])
        self.assertIn("Ctrl+Shift+Z", answer["error"]["hint"])
        self.assertEqual(self.pos_x(), 0.0)
        self.assertEqual(self.undone_records(), [])

    def test_with_rollback_off_nothing_is_undone_and_the_call_is_named(self):
        self.cube("fit the body")
        self.kit.rollback.off = True
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("rollback is off", answer["error"]["message"])
        self.assertIn('journal 1 "fit the body" (run_python)',
                      answer["error"]["hint"])
        self.assertEqual(self.sent("app.undo"), [])
        self.assertEqual(self.meshes(), 2)

    def test_modos_event_log_says_what_was_undone(self):
        self.cube("fit the body")
        self.undo_last()
        self.assertIn('mt > undo: journal 1 "fit the body" (run_python)',
                      self.fake.event_log)

    def test_after_erics_own_undo_the_call_beneath_is_reached(self):
        self.cube("one")
        self.cube("two")
        self.assertTrue(self.fake.undo())   # the user's Ctrl+Z on "two"
        result = self.undo_last()["result"]
        self.assertEqual(result["purpose"], "one")
        self.assertEqual(self.meshes(), 1)

    def test_an_open_action_is_refused_with_the_hint(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "in_action")
        self.assertIn("mt_rollback", answer["error"]["hint"])
        self.assertEqual(self.meshes(), 2)
        self.assertEqual(self.sent("app.undo"), [])

    def other_session(self):
        client = support.Client(self.kit.port)
        self.addCleanup(client.close)
        client.hello(session="agent-b")
        return client

    def test_another_sessions_call_is_refused_and_put_back(self):
        # The review: two agents share one Modo, and each undoes its own.
        self.cube("fit the body")
        answer = self.ask("undo_last", client=self.other_session())
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("another agent session's call: journal 1 "
                      '"fit the body" (run_python)', answer["error"]["message"])
        self.assertIn("Nothing was undone", answer["error"]["message"])
        self.assertEqual(len(self.sent("app.undo")), 1)
        self.assertEqual(len(self.sent("app.redo")), 1)
        self.assertEqual(self.meshes(), 2)
        self.assertEqual(self.undone_records(), [])
        # Its own session still undoes it.
        self.assertEqual(self.undo_last()["result"]["journal"], 1)
        self.assertEqual(self.meshes(), 1)

    def test_another_sessions_open_action_is_refused_without_its_hint(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        answer = self.ask("undo_last", client=self.other_session())
        self.assertEqual(answer["error"]["type"], "in_action")
        self.assertIn("open in another session", answer["error"]["message"])
        self.assertNotIn("mt_rollback", answer["error"]["hint"])
        self.assertEqual(self.sent("app.undo"), [])

    def test_a_committed_actions_steps_go_one_at_a_time(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube("one")
        self.cube("two")
        self.ask("commit")
        self.assertEqual(self.undo_last()["result"]["purpose"], "two")
        self.assertEqual(self.meshes(), 2)

    def test_an_empty_stack_is_refused(self):
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("nothing to undo", answer["error"]["message"])
        self.assertEqual(self.sent("app.undo"), [])

    def test_a_read_only_call_leaves_nothing_to_undo(self):
        answer = self.ask("run_python", {"code": "RESULT = 1",
                                         "purpose": "read", "read_only": True})
        self.assertTrue(answer["ok"], answer)
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("read_only leave no step", answer["error"]["hint"])

    def test_a_rolled_back_call_leaves_nothing_to_undo(self):
        answer = self.ask("run_python", {"code": "lx.eval('test.cube')\n"
                                                 "raise ValueError('half')",
                                         "purpose": "half"})
        self.assertEqual(answer["undo"], {"rolled_back": True})
        answer = self.undo_last()
        self.assertIn("nothing to undo", answer["error"]["message"])

    def test_the_manual_pump_undoes_nothing_and_names_the_call(self):
        support.stop_running_kit()
        self.fake.idle_supported = False
        self.fake.idle = []
        self.fake.run_command("modoTools.start port:0")
        self.kit = sys.modules["modotools"].running()
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello(session="agent-a")
        self.cube("fit the body")
        answer = self.undo_last()
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("manual pump", answer["error"]["message"])
        self.assertIn('"fit the body"', answer["error"]["hint"])
        self.assertEqual(self.meshes(), 2)

    def test_the_op_log_names_it(self):
        self.cube()
        self.undo_last()
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "ops.log")
        with open(path, encoding="utf-8") as handle:
            ops = [json.loads(line).get("op") for line in handle]
        self.assertIn("undo_last", ops)


if __name__ == "__main__":
    unittest.main()
