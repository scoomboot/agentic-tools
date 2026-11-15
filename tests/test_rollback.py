"""A failed call is rolled back before it answers.

The fake undo stack calls markers as probes found Modo 17.1 on
2026-11-08: undo_Reverse when their step is undone, undo_Forward when it is
redone, with no command block.
"""

import json
import os
import sys
import time
import unittest

import support


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.connect()
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})

    def connect(self):
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.n = 0

    def manual(self):
        """Restart the kit with the manual pump, as when idle can't be armed."""
        support.stop_running_kit()
        self.fake.idle_supported = False
        self.fake.idle = []
        self.fake.run_command("modoTools.start port:0")
        self.kit = sys.modules["modotools"].running()
        self.assertEqual(self.kit.pump.strategy_name, "manual")
        self.connect()

    def ask(self, op, payload):
        """Through the socket and the pump, as in Modo."""
        self.n += 1
        asked = support.Asked(self.client, {"id": "r-%d" % self.n, "op": op,
                                            "payload": payload})
        if self.kit.pump.strategy_name == "manual":
            def done():
                if self.kit.pump.queue.pending():
                    self.fake.run_command("modoTools.pump")
                return asked.done()
            self.assertTrue(support.pump_until(self.fake, done))
        else:
            self.assertTrue(support.pump_until(self.fake, asked.done))
        return asked.answer

    def py(self, code, **extra):
        payload = {"code": code, "purpose": "test"}
        payload.update(extra)
        return self.ask("run_python", payload)

    def meshes(self):
        return len([i for i in self.fake.scene.items if i.type == "mesh"])

    def pos_x(self):
        return self.fake.scene.find("translation005").channel("pos.X").get()

    def sent(self, command):
        return [e for e in self.fake.evals if e.lstrip("!") == command]

    def markers(self):
        return sys.modules["modotools"].running().markers

    HALF = ("lx.eval('test.cube')\n"
            "modo.Scene().item('translation005').channel('pos.X').set(2.0)\n"
            "raise ValueError('stop')")

    # -- the marker ---------------------------------------------------------

    def test_every_exec_job_records_one_marker(self):
        self.py("RESULT = 1")
        self.ask("set_channels", {"item": "mesh001", "channels": {"pos.X": 1.0}})
        self.assertEqual([s.markers for s in self.fake.undo_steps], [1, 1])

    def test_a_failed_job_records_its_marker_too(self):
        self.fake.silent_markers = True     # keep the step, to look at it
        self.py("raise ValueError('stop')")
        self.assertEqual(self.fake.marker_records, 1)

    def test_a_read_only_job_records_none(self):
        self.py("RESULT = 1", read_only=True)
        self.ask("scene_summary", {})
        self.assertEqual(self.fake.marker_records, 0)

    def test_the_marker_reports_reverse_and_forward(self):
        self.py("lx.eval('test.cube')")
        markers = self.markers()
        step = markers.last
        self.assertEqual(markers.state(step), "forward")
        self.fake.undo()
        self.assertEqual(markers.state(step), "reversed")
        self.fake.redo()
        self.assertEqual(markers.state(step), "forward")

    def test_a_marker_that_raises_never_stops_modos_undo(self):
        self.py("lx.eval('test.cube')")
        markers = self.markers()

        def broken(step, state):
            raise RuntimeError("broken")
        markers._note = broken
        self.fake.undo()                    # must not raise
        self.assertEqual(self.meshes(), 1)

    # -- the idle undo ----------------------------------------------------------

    def test_a_failed_job_is_rolled_back(self):
        answer = self.py(self.HALF)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(answer["undo"], {"rolled_back": True})
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(self.pos_x(), 0.0)
        self.assertEqual(len(self.sent("app.undo")), 1)
        self.assertEqual(self.fake.undo_steps, [])
        self.assertNotIn("app.undo", self.fake.nested)

    def test_a_failed_job_that_changed_nothing_leaves_no_step(self):
        self.ask("set_channels", {"item": "mesh001", "channels": {"pos.X": 3.0}})
        answer = self.py("raise ValueError('stop')")
        self.assertEqual(answer["undo"], {"rolled_back": True})
        # Only the earlier call's step is left, and it is still there.
        self.assertEqual(len(self.fake.undo_steps), 1)
        self.assertEqual(self.pos_x(), 3.0)
        self.fake.undo()
        self.assertEqual(self.pos_x(), 0.0)

    def test_a_failed_command_is_rolled_back(self):
        answer = self.ask("run_command", {"command": "test.fails"})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["undo"], {"rolled_back": True})
        self.assertEqual(self.fake.undo_steps, [])

    def test_a_successful_job_is_not_undone(self):
        answer = self.py("lx.eval('test.cube')")
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(self.sent("app.undo"), [])
        self.assertEqual(self.meshes(), 2)

    def test_a_failed_read_only_job_is_not_undone(self):
        answer = self.py("raise ValueError('stop')", read_only=True)
        self.assertIsNone(answer["undo"])
        self.assertEqual(self.sent("app.undo"), [])

    def test_the_answer_waits_for_the_check(self):
        seen = []
        real = self.fake.builtin["app.undo"]
        pump = self.kit.pump

        class Watching(real):
            def basic_Execute(inner, msg, flags):
                seen.append([job.done.is_set() for job, _r, _m in pump._held])
                return real.basic_Execute(inner, msg, flags)
        self.fake.builtin["app.undo"] = Watching
        answer = self.py(self.HALF)
        self.assertEqual(seen, [[False]])
        self.assertEqual(answer["undo"], {"rolled_back": True})

    def test_the_undo_runs_after_exec_returns(self):
        self.py(self.HALF)
        ran = self.fake.ran
        self.assertLess(ran.index("modoTools.exec"), ran.index("app.undo"))
        self.assertNotIn("app.undo", self.fake.nested)

    def test_a_marker_that_does_not_reverse_is_put_back_and_turns_rollback_off(self):
        self.fake.silent_markers = True
        answer = self.py(self.HALF)
        self.assertEqual(answer["undo"], {"step": 1, "rolled_back": "unsure"})
        self.assertEqual(len(self.sent("app.redo")), 1)
        # The step the undo took is back.
        self.assertEqual(len(self.fake.undo_steps), 1)
        self.assertEqual(self.meshes(), 2)
        self.assertTrue(any("rollback unsure" in line.lower()
                            for line in self.fake.event_log))
        self.assertEqual(self.ask("status", {})["result"]["rollback"], "off")
        # Later failures ask the user instead.
        answer = self.py("raise ValueError('again')")
        self.assertEqual(answer["undo"],
                         {"step": 1, "rolled_back": False, "presses": 1})
        self.assertEqual(len(self.sent("app.undo")), 1)

    def test_app_undo_that_raises_asks_for_one_press(self):
        class Refusing(self.fake.builtin["app.undo"]):
            def basic_Execute(inner, msg, flags):
                raise RuntimeError("refused")
        self.fake.builtin["app.undo"] = Refusing
        answer = self.py(self.HALF)
        self.assertEqual(answer["undo"],
                         {"step": 1, "rolled_back": False, "presses": 1})
        self.assertEqual(self.ask("status", {})["result"]["rollback"], "idle")

    def test_the_op_log_names_the_rollback(self):
        self.py(self.HALF)
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "ops.log")
        with open(path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        names = [e["event"] for e in events]
        self.assertEqual(names[-3:], ["op_error", "rollback", "rollback_done"])
        self.assertEqual(events[-2]["id"], events[-3]["id"])
        self.assertEqual(events[-1]["rolled_back"], True)

    # -- the manual pump --------------------------------------------------------

    def test_the_manual_pump_asks_for_one_press(self):
        self.manual()
        answer = self.py(self.HALF)
        self.assertEqual(answer["undo"],
                         {"step": 1, "rolled_back": False, "presses": 1})
        self.assertEqual(self.sent("app.undo"), [])
        self.fake.undo()
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(self.pos_x(), 0.0)

    def test_modoTools_pump_with_the_idle_pump_on_asks_too(self):
        # Fired by hand, the pump is a command: it can't run app.undo.
        self.n += 1
        asked = support.Asked(self.client, {"id": "p-1", "op": "run_python",
                                            "payload": {"code": self.HALF,
                                                        "purpose": "test"}})

        end = time.time() + 5
        while not self.kit.pump.queue.pending() and time.time() < end:
            time.sleep(0.005)
        self.fake.run_command("modoTools.pump")     # no idle tick
        self.assertTrue(support.pump_until(self.fake, asked.done))
        self.assertEqual(asked.answer["undo"],
                         {"step": 1, "rolled_back": False, "presses": 1})
        self.assertEqual(self.sent("app.undo"), [])

    # -- status -----------------------------------------------------------------

    def test_status_says_idle(self):
        self.assertEqual(self.ask("status", {})["result"]["rollback"], "idle")

    def test_status_says_ask_with_the_manual_pump(self):
        self.manual()
        self.assertEqual(self.ask("status", {})["result"]["rollback"], "ask")


if __name__ == "__main__":
    unittest.main()
