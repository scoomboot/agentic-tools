"""Actions. mt_begin, mt_commit and mt_rollback over a run of
steps.

The fake undo stack is built to probes (2026-11-08):
the idle app.undo steps over reads (execRead keeps no step), an idle app.redo
puts back a step it undid, and any command ends the redo list, as a read
between Ctrl+Z and Ctrl+Shift+Z did on 17.1.
"""

import json
import os
import sys
import time
import unittest

import support

LABEL = "Raise the cube's top"


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.client = self.connect("agent-a")
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})
        self.n = 0

    def connect(self, session):
        client = support.Client(self.kit.port)
        self.addCleanup(client.close)
        client.hello(session=session)
        return client

    def manual(self):
        support.stop_running_kit()
        self.fake.idle_supported = False
        self.fake.idle = []
        self.fake.run_command("modoTools.start port:0")
        self.kit = sys.modules["modotools"].running()
        self.assertEqual(self.kit.pump.strategy_name, "manual")
        self.client = self.connect("agent-a")

    def ask(self, op, payload=None, client=None):
        self.n += 1
        asked = support.Asked(client or self.client,
                              {"id": "r-%d" % self.n, "op": op,
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

    def py(self, code, client=None, **extra):
        payload = {"code": code, "purpose": "test"}
        payload.update(extra)
        return self.ask("run_python", payload, client)

    def cube(self, client=None):
        return self.py("lx.eval('test.cube')", client)

    def begin(self, label=LABEL, client=None):
        return self.ask("begin", {"label": label}, client)

    def meshes(self):
        return len([i for i in self.fake.scene.items if i.type == "mesh"])

    def pos_x(self):
        return self.fake.scene.find("translation005").channel("pos.X").get()

    def sent(self, command):
        return [e for e in self.fake.evals if e.lstrip("!") == command]

    def oplog(self):
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "ops.log")
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def status_action(self):
        return self.ask("status")["result"]["action"]

    # -- step 2: the session ------------------------------------------------------

    def test_the_kit_sees_two_sessions_apart(self):
        seen = []
        real = self.kit.pump._run_job

        def watching(request):
            seen.append(request.get("session"))
            return real(request)
        self.kit.pump._run_job = watching
        other = self.connect("agent-b")
        self.ask("status")
        self.ask("status", client=other)
        self.assertEqual(seen, ["agent-a", "agent-b"])

    def test_a_hello_without_a_session_gets_one_per_connection(self):
        seen = []
        real = self.kit.pump._run_job

        def watching(request):
            seen.append(request.get("session"))
            return real(request)
        self.kit.pump._run_job = watching
        one, two = support.Client(self.kit.port), support.Client(self.kit.port)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        one.hello()
        two.hello()
        self.ask("status", client=one)
        self.ask("status", client=two)
        self.assertTrue(all(seen))
        self.assertNotEqual(seen[0], seen[1])

    # -- step 3: begin and commit -------------------------------------------------

    def test_begin_answers_the_label_and_adds_no_step(self):
        answer = self.begin()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"], {"action": LABEL})
        self.assertEqual(self.fake.undo_steps, [])
        self.assertNotIn("modoTools.exec", self.fake.ran)
        self.assertIn("mt > begin: " + LABEL, self.fake.event_log)

    def test_three_calls_are_three_numbered_steps(self):
        self.begin()
        undos = [self.cube()["undo"] for _ in range(3)]
        self.assertEqual(undos, [{"action": LABEL, "step": n} for n in (1, 2, 3)])
        answer = self.ask("commit")
        self.assertEqual(answer["result"], {
            "action": LABEL, "steps": 3, "presses": 3,
            "undo": "3 Ctrl+Z undo the whole action"})
        self.assertEqual(len(self.fake.undo_steps), 3)
        self.assertIn("mt > commit: %s (3 steps)" % LABEL, self.fake.event_log)
        # Closed: the next call is a plain step again.
        self.assertEqual(self.cube()["undo"], {"step": 1})

    def test_reads_inside_an_action_are_not_counted(self):
        self.begin()
        self.cube()
        self.ask("scene_summary")
        self.assertIsNone(self.py("RESULT = 1", read_only=True)["undo"])
        self.assertEqual(self.cube()["undo"], {"action": LABEL, "step": 2})
        self.assertEqual(self.ask("commit")["result"]["presses"], 2)

    def test_begin_needs_a_label(self):
        answer = self.begin(label="  ")
        self.assertEqual(answer["error"]["type"], "bad_request")

    def test_a_second_begin_is_in_action(self):
        self.begin()
        answer = self.begin("Another")
        self.assertEqual(answer["error"]["type"], "in_action")
        self.assertIn(LABEL, answer["error"]["message"])
        self.assertIn("commit or roll back", answer["error"]["hint"].lower())
        self.assertEqual(self.status_action()["label"], LABEL)

    def test_commit_and_rollback_with_no_action_are_bad_requests(self):
        self.assertEqual(self.ask("commit")["error"]["type"], "bad_request")
        self.assertEqual(self.ask("rollback")["error"]["type"], "bad_request")

    def test_status_names_the_open_action(self):
        self.assertIsNone(self.status_action())
        self.begin()
        self.cube()
        action = self.status_action()
        self.assertEqual(action["label"], LABEL)
        self.assertEqual(action["steps"], 1)
        self.assertGreaterEqual(action["idle_s"], 0)

    def test_the_idle_limit_commits_and_the_next_call_is_told(self):
        self.begin()
        self.cube()
        self.kit.action.idle_s = 0.05
        time.sleep(0.1)
        self.fake.tick()
        self.assertTrue(any(line.startswith("mt > commit: " + LABEL)
                            and "idle" in line for line in self.fake.event_log))
        answer = self.cube()
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(answer["action_closed"],
                         {"action": LABEL, "reason": "idle", "steps": 1,
                          "presses": 1})
        # Told once.
        self.assertNotIn("action_closed", self.cube())

    def test_an_idle_commit_is_told_to_a_begin_too(self):
        # The next call was told unless it was mt_begin.
        self.begin()
        self.cube()
        self.kit.action.idle_s = 0.05
        time.sleep(0.1)
        self.fake.tick()
        self.kit.action.idle_s = 300.0
        answer = self.begin("Next")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["action_closed"],
                         {"action": LABEL, "reason": "idle", "steps": 1,
                          "presses": 1})

    def test_stop_commits_the_open_action(self):
        self.begin()
        self.cube()
        sys.modules["modotools"].stop()
        self.assertTrue(any(line.startswith("mt > commit: " + LABEL)
                            for line in self.fake.event_log))
        events = [e for e in self.oplog() if e["event"].startswith("action_")]
        self.assertEqual([e["event"] for e in events],
                         ["action_begin", "action_commit"])
        self.assertEqual(events[-1]["reason"], "stop")

    def test_the_op_log_writes_begin_and_commit(self):
        self.begin()
        self.cube()
        self.ask("commit")
        events = [e for e in self.oplog() if e["event"].startswith("action_")]
        self.assertEqual([e["event"] for e in events],
                         ["action_begin", "action_commit"])
        self.assertEqual(events[0]["label"], LABEL)
        self.assertEqual(events[1]["steps"], 1)
        starts = [e for e in self.oplog() if e["event"] == "op_start"
                  and e["op"] == "run_python"]
        self.assertEqual((starts[-1]["action"], starts[-1]["step"]), (LABEL, 1))

    # -- step 4: refusals ---------------------------------------------------------

    def test_app_undo_in_python_mid_action_is_in_action_and_never_sent(self):
        self.begin()
        self.cube()
        answer = self.py("lx.eval('app.undo')")
        self.assertEqual(answer["error"]["type"], "in_action")
        self.assertIn(LABEL, answer["error"]["message"])
        self.assertNotIn("app.undo", self.fake.nested)
        # Only this call's own step was rolled back; the action goes on.
        self.assertEqual(self.meshes(), 2)
        self.assertEqual(self.status_action()["steps"], 1)
        self.assertEqual(self.cube()["undo"], {"action": LABEL, "step": 2})

    def test_the_six_commands_are_refused_mid_action(self):
        self.begin()
        for name in ("app.undo", "app.redo", "scene.new", "scene.open",
                     "scene.close", "scene.revert"):
            answer = self.ask("run_command", {"command": name})
            self.assertEqual(answer["error"]["type"], "in_action", name)
            self.assertNotIn(name, self.fake.nested)
        self.assertEqual(self.status_action()["label"], LABEL)

    def test_the_undo_list_commands_are_refused_mid_action(self):
        # Each would undo, clear or stop the steps the
        # action counts on (mt_find_commands "undo" on 17.1, 2026-11-08).
        self.begin()
        for name in ("cmds.undoTo", "app.clearUndos", "app.undoSuspend",
                     "app.undoLockout"):
            answer = self.ask("run_command", {"command": name})
            self.assertEqual(answer["error"]["type"], "in_action", name)
            self.assertNotIn(name, self.fake.nested)

    def test_outside_an_action_they_are_not_in_action(self):
        answer = self.ask("run_command", {"command": "scene.new"})
        self.assertTrue(answer["ok"], answer)

    def test_saving_is_allowed_mid_action(self):
        self.fake.scene.filename = "C:/scenes/a.lxo"
        self.begin()
        answer = self.ask("run_command", {"command": "scene.save"})
        self.assertNotEqual((answer.get("error") or {}).get("type"), "in_action")

    def test_another_sessions_change_is_refused_and_its_read_runs(self):
        self.begin()
        other = self.connect("agent-b")
        answer = self.cube(client=other)
        self.assertEqual(answer["error"]["type"], "in_action")
        self.assertIn(LABEL, answer["error"]["message"])
        self.assertEqual(self.meshes(), 1)
        self.assertTrue(self.ask("scene_summary", client=other)["ok"])
        self.assertEqual(self.ask("commit", client=other)["error"]["type"],
                         "in_action")
        # The action's own session goes on, its steps unchanged.
        self.assertEqual(self.cube()["undo"], {"action": LABEL, "step": 1})

    # -- step 5: rollback ---------------------------------------------------------

    def test_rollback_to_begin(self):
        self.ask("set_channels", {"item": "mesh001", "channels": {"pos.X": 5.0}})
        before = len(self.fake.undo_steps)
        self.begin()
        self.cube()
        self.ask("scene_summary")
        self.py("modo.Scene().item('translation005').channel('pos.X').set(2.0)")
        self.py("RESULT = 1", read_only=True)
        answer = self.ask("rollback")
        self.assertEqual(answer["result"], {
            "action": LABEL, "rolled_back": 2, "left": 0,
            "scene": "as before mt_begin"})
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(self.pos_x(), 5.0)
        self.assertEqual(len(self.fake.undo_steps), before)
        self.assertEqual(len(self.sent("app.undo")), 2)
        self.assertNotIn("app.undo", self.fake.nested)
        self.assertIsNone(self.status_action())

    def test_rollback_reverses_the_markers_from_the_top(self):
        self.begin()
        self.cube()
        self.cube()
        markers = self.kit.markers
        steps = [markers.last - 1, markers.last]
        order = []
        real = markers._note

        def note(step, state):
            order.append((step, state))
            real(step, state)
        markers._note = note
        self.ask("rollback")
        self.assertEqual(order, [(steps[1], "reversed"), (steps[0], "reversed")])

    def test_rollback_runs_outside_any_command(self):
        self.begin()
        self.cube()
        ran = len(self.fake.ran)
        self.ask("rollback")
        self.assertEqual(self.fake.ran[ran:], ["app.undo"])

    def test_erics_step_on_top_is_undone_redone_and_stopped_at(self):
        self.begin()
        self.cube()
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        answer = self.ask("rollback")
        result = answer["result"]
        self.assertEqual((result["rolled_back"], result["left"]), (0, 2))
        self.assertIn("The user's edit is on top", result["note"])
        self.assertEqual(len(self.sent("app.undo")), 1)
        self.assertEqual(len(self.sent("app.redo")), 1)
        self.assertEqual(self.pos_x(), 3.0)
        self.assertEqual(self.meshes(), 3)
        self.assertIsNone(self.status_action())

    def test_erics_edit_between_calls_stops_the_rollback_beneath_it(self):
        self.begin()
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        self.cube()
        result = self.ask("rollback")["result"]
        self.assertEqual((result["rolled_back"], result["left"]), (1, 1))
        self.assertIn("Undo it, then 1 more Ctrl+Z", result["note"])
        self.assertEqual(self.pos_x(), 3.0)
        self.assertEqual(self.meshes(), 2)

    def test_a_failed_call_rolls_back_the_whole_action_and_closes_it(self):
        self.begin()
        self.cube()
        self.cube()
        answer = self.py("lx.eval('test.cube')\nraise ValueError('stop')")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(answer["undo"],
                         {"action": LABEL, "rolled_back": 2, "left": 0})
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(self.fake.undo_steps, [])
        self.assertIsNone(self.status_action())

    # -- a refusal keeps the action -----------------------------------

    REFUSED_CALLS = (
        ("bad_channel", "set_channels",
         {"item": "mesh001", "channels": {"nope": 1}}),
        ("no_such_item", "set_channels",
         {"item": "nothing999", "channels": {"pos.X": 1}}),
        ("bad_request", "set_channels", {"item": "mesh001"}),
        ("unknown_command", "run_command",
         {"command": "query commandservice command.desc ? {no.such}"}),
        # On 17.1 a mistyped name ("command could not be
        # found") was command_failed, and rolled the action back.
        ("unknown_command", "run_command", {"command": "no.suchCommand 1"}),
        # Row 7 says each refusal type.
        ("ambiguous_item", "set_channels",
         {"item": "Twin", "channels": {"pos.X": 1}}),
        ("in_action", "run_command", {"command": "app.undo"}),
    )

    def test_each_refusal_leaves_the_action_open_with_its_steps(self):
        self.fake.scene.add("locator901", "Twin", "locator")
        self.fake.scene.add("locator902", "Twin", "locator")
        for err, op_name, payload in self.REFUSED_CALLS:
            with self.subTest(err):
                self.begin()
                self.cube()
                self.cube()
                answer = self.ask(op_name, payload)
                self.assertEqual(answer["error"]["type"], err, answer)
                self.assertEqual(answer["undo"], {"action": LABEL,
                                                  "refused": True, "steps": 2})
                self.assertEqual(self.meshes(), 3)
                self.assertEqual(self.status_action()["steps"], 2)
                # The next step is numbered on from the two.
                self.assertEqual(self.cube()["undo"],
                                 {"action": LABEL, "step": 3})
                result = self.ask("rollback")["result"]
                self.assertEqual((result["rolled_back"], result["left"]), (3, 0))
                self.assertEqual(self.meshes(), 1)
                self.assertEqual(self.fake.undo_steps, [])

    def test_the_refusal_list_is_one_tuple(self):
        from modotools import action, journal, protocol
        self.assertIs(journal.REFUSALS, protocol.REFUSALS)
        self.assertIs(action.REFUSALS, protocol.REFUSALS)

    def test_the_manual_pumps_refusal_keeps_the_action_too(self):
        self.manual()
        self.begin()
        self.cube()
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"nope": 1}})
        self.assertEqual(answer["undo"]["action"], LABEL)
        self.assertTrue(answer["undo"]["refused"])
        self.assertNotIn("rolled_back", answer["undo"])
        self.assertEqual(self.status_action()["label"], LABEL)

    # -- later checks ----------------------------------------------------------

    HALF = ("lx.eval('test.cube')\n"
            "lx.eval('query commandservice command.desc ? {no.such}')")

    def test_a_refusal_after_a_change_is_undone_and_the_action_stays(self):
        # run_python can change the scene and then be refused: the call's
        # own step is undone, so nothing it wrote stays.
        self.begin()
        self.cube()
        answer = self.py(self.HALF)
        self.assertEqual(answer["error"]["type"], "unknown_command", answer)
        self.assertEqual(answer["undo"], {"action": LABEL, "refused": True,
                                          "steps": 1})
        self.assertEqual(self.meshes(), 2)

    def test_a_refused_step_that_cant_be_undone_says_it_is_kept(self):
        # The manual pump can't undo: the half edit stays, as a step of the
        # action, and the answer must not read as "nothing was written".
        self.manual()
        self.begin()
        self.cube()
        answer = self.py(self.HALF)
        self.assertEqual(answer["error"]["type"], "unknown_command", answer)
        self.assertEqual(answer["undo"], {"action": LABEL, "refused": True,
                                          "steps": 2, "kept": True})
        self.assertEqual(self.meshes(), 3)
        self.assertEqual(self.status_action()["steps"], 2)

    def test_a_refused_step_kept_after_an_unsure_undo_says_so(self):
        self.begin()
        self.cube()
        self.kit.rollback.off = True
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"nope": 1}})
        self.assertEqual(answer["undo"], {"action": LABEL, "refused": True,
                                          "steps": 2, "kept": True})

    def test_a_refused_call_still_says_what_eric_undid(self):
        # enter() counts the user's undo as told on this call, and the action
        # lives on, so the refusal's answer has to carry it.
        self.begin()
        self.cube()
        self.cube()
        self.fake.undo()                        # the user's Ctrl+Z
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"nope": 1}})
        self.assertEqual(answer["undo"], {"action": LABEL, "refused": True,
                                          "steps": 2, "undone_by_user": [2]})
        self.assertEqual(self.cube()["undo"], {"action": LABEL, "step": 3})

    def test_the_manual_pumps_rollback_sends_nothing_and_answers_presses(self):
        self.manual()
        self.begin()
        self.cube()
        self.cube()
        result = self.ask("rollback")["result"]
        self.assertEqual((result["rolled_back"], result["left"]), (0, 2))
        self.assertIn("Ctrl+Z", result["note"])
        self.assertEqual(self.sent("app.undo"), [])
        self.assertIsNone(self.status_action())

    def test_the_op_log_writes_the_rollback(self):
        self.begin()
        self.cube()
        self.ask("rollback")
        events = [e for e in self.oplog() if e["event"].startswith("action_")]
        self.assertEqual([e["event"] for e in events],
                         ["action_begin", "action_rollback"])
        self.assertEqual(events[-1]["steps"], 1)

    def test_a_failed_calls_rollback_of_the_action_is_in_the_op_log(self):
        # A crash in its app.undo read "ended cleanly".
        self.begin()
        self.cube()
        self.py("lx.eval('test.cube')\nraise ValueError('stop')")
        events = [e["event"] for e in self.oplog()
                  if e["event"] in ("op_error", "rollback", "action_rollback",
                                    "rollback_done")]
        self.assertEqual(events, ["op_error", "rollback", "action_rollback",
                                  "rollback_done"])

    # -- step 6: the user's undo ------------------------------------------------------

    def test_erics_undo_shortens_the_action_and_the_next_answer_says_so(self):
        self.begin()
        self.cube()
        self.cube()
        self.fake.undo()                        # the user's Ctrl+Z
        answer = self.cube()
        self.assertEqual(answer["undo"], {"action": LABEL, "step": 3,
                                          "undone_by_user": [2]})
        # Said once.
        self.assertEqual(self.cube()["undo"], {"action": LABEL, "step": 4})
        result = self.ask("rollback")["result"]
        self.assertEqual((result["rolled_back"], result["left"]), (3, 0))
        self.assertEqual(self.meshes(), 1)

    def test_commit_after_erics_undo_counts_the_steps_left(self):
        self.begin()
        self.cube()
        self.cube()
        self.fake.undo()
        result = self.ask("commit")["result"]
        self.assertEqual((result["steps"], result["presses"]), (2, 1))
        self.assertEqual(result["undone_by_user"], [2])

    def test_erics_undo_of_every_step_is_action_undone(self):
        self.begin()
        self.cube()
        self.cube()
        self.fake.undo()
        self.fake.undo()
        self.assertTrue(self.ask("scene_summary")["ok"])
        answer = self.cube()
        self.assertEqual(answer["error"]["type"], "action_undone")
        self.assertIn(LABEL, answer["error"]["message"])
        self.assertIn("mt_describe_mesh", answer["error"]["hint"])
        self.assertEqual(self.meshes(), 1)
        self.assertIsNone(self.status_action())


if __name__ == "__main__":
    unittest.main()
