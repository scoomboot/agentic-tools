"""One undo step per call.

The fake undo stack behaves as the probe found Modo 17.1 on 2026-11-08:
every modoTools.exec is one step, even when its job changed nothing.
"""

import unittest

import support


class UndoTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.n = 0
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})

    def ask(self, op, payload):
        """Through the socket and the idle pump, as in Modo."""
        self.n += 1
        asked = support.Asked(self.client, {"id": "u-%d" % self.n, "op": op,
                                            "payload": payload})
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

    # -- one step per call ------------------------------------------------------

    def test_five_commands_are_one_step(self):
        answer = self.py("for _ in range(5):\n    lx.eval('test.cube')")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(self.fake.undo_steps), 1)
        self.assertEqual(answer["undo"], {"step": 1})
        self.fake.undo()
        self.assertEqual(self.meshes(), 1)

    def test_the_kit_opens_no_command_block(self):
        # On 17.1 a block lost a channel set() to undo .
        self.py("lx.eval('test.cube')")
        self.ask("set_channels", {"item": "mesh001", "channels": {"pos.X": 1.0}})
        self.assertEqual(self.fake.blocks, [])
        self.assertEqual(self.fake.untracked, [])

    def test_a_job_that_raises_is_still_one_step(self):
        # Its half edit is one step: one Ctrl+Z. The kit rolls a failed job
        # back; a marker that doesn't reverse makes it put the step back.
        self.fake.silent_markers = True
        answer = self.py("lx.eval('test.cube')\nraise ValueError('half way')")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(len(self.fake.undo_steps), 1)
        self.fake.undo()
        self.assertEqual(self.meshes(), 1)

    def test_a_job_that_changes_nothing_is_still_a_step(self):
        # Modo keeps a step for every modoTools.exec. The answer says so.
        answer = self.py("RESULT = 1")
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(len(self.fake.undo_steps), 1)

    def test_reads_add_no_step_and_no_undo_key(self):
        answer = self.ask("scene_summary", {})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.undo_steps, [])
        self.assertNotIn("undo", answer)

    def test_status_names_the_way(self):
        self.assertEqual(self.ask("status", {})["result"]["undo"], "exec command")

    # -- set_channels ---------------------------------------------------------------

    def test_set_channels_is_one_step_and_undoes(self):
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"pos.X": 2.0}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.pos_x(), 2.0)
        self.assertEqual(len(self.fake.undo_steps), 1)
        self.assertEqual(answer["undo"], {"step": 1})
        self.fake.undo()
        self.assertEqual(self.pos_x(), 0.0)

    # -- read_only ------------------------------------------------------------------

    def test_a_read_only_job_runs_as_a_read_and_adds_no_step(self):
        answer = self.py("RESULT = len(modo.Scene().meshes)", read_only=True)
        self.assertTrue(answer["ok"], answer)
        self.assertIsNone(answer["undo"])
        self.assertEqual(self.fake.undo_steps, [])
        self.assertEqual(self.fake.blocks, [])
        self.assertIn("modoTools.execRead", self.fake.ran)
        self.assertNotIn("modoTools.exec", self.fake.ran)

    def test_an_edit_after_reads_is_still_one_ctrl_z(self):
        self.ask("set_channels", {"item": "mesh001", "channels": {"pos.X": 2.0}})
        for _ in range(5):
            self.py("RESULT = 1", read_only=True)
        self.fake.undo()
        self.assertEqual(self.pos_x(), 0.0)

    def test_a_failing_read_only_job_hints_at_dropping_it(self):
        answer = self.py("raise RuntimeError('no')", read_only=True)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertIn("read_only", answer["error"]["hint"])
        self.assertIsNone(answer["undo"])

    # On 17.1 Modo doesn't refuse edits in a read-only job (2026-11-08): a
    # channel set() there left no undo step at all. So the kit refuses them.

    def test_a_read_only_job_cant_run_a_command(self):
        answer = self.py("lx.eval('test.cube')", read_only=True)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertIn("read_only", answer["error"]["message"])
        self.assertIn("without read_only", answer["error"]["hint"])
        self.assertEqual(self.meshes(), 1)
        self.assertEqual(self.fake.untracked, [])

    def test_a_read_only_job_cant_run_a_command_by_lx_command(self):
        answer = self.py("lx.command('test.cube')", read_only=True)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(self.meshes(), 1)

    def test_a_read_only_job_cant_set_a_channel(self):
        answer = self.py("modo.Scene().item('translation005')"
                         ".channel('pos.X').set(4.0)", read_only=True)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(self.pos_x(), 0.0)
        self.assertEqual(self.fake.untracked, [])

    def test_a_read_only_job_cant_write_mesh_edits(self):
        answer = self.py("modo.MeshGeometry().setMeshEdits()", read_only=True)
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(self.fake.untracked, [])

    def test_a_read_only_job_can_query(self):
        self.fake.scene.time = 1.5
        answer = self.py("RESULT = lx.eval('select.time ?')", read_only=True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["result"], 1.5)

    def test_after_a_read_only_job_edits_work_again(self):
        self.py("RESULT = 1", read_only=True)
        answer = self.py("modo.Scene().item('translation005')"
                         ".channel('pos.X').set(4.0)\nlx.eval('test.cube')")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.pos_x(), 4.0)

    def test_read_only_must_be_true_or_false(self):
        answer = self.py("RESULT = 1", read_only="yes")
        self.assertEqual(answer["error"]["type"], "bad_request")


if __name__ == "__main__":
    unittest.main()
