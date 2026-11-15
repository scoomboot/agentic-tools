"""The guard run_job puts around every job: !! on every command, and no
per-command command service query for a name that isn't a command."""

import sys
import unittest

import support


def job(kit, op, payload):
    jobs = sys.modules["modotools.jobs"]
    return jobs.run_job({"id": "t-1", "op": op, "payload": payload}, kit)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        import lx
        self.lx = lx

    def test_bangs_added_during_a_job_and_removed_after(self):
        real = (self.lx.eval, self.lx.eval1, self.lx.evalN, self.lx.command)
        answer = job(self.kit, "run_python", {
            "code": "lx.eval('test.cube')\nlx.evalN('query commandservice commands ?')",
            "purpose": "make a cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertIn("!!test.cube", self.fake.evals)
        self.assertIn("!!query commandservice commands ?", self.fake.evals)
        self.assertEqual((self.lx.eval, self.lx.eval1, self.lx.evalN,
                          self.lx.command), real)

    def test_lx_eval_restored_after_a_failing_job(self):
        real = self.lx.eval
        answer = job(self.kit, "run_python", {"code": "raise ValueError('x')",
                                              "purpose": "fail"})
        self.assertFalse(answer["ok"])
        self.assertIs(self.lx.eval, real)

    def test_unknown_command_query_refused_from_python(self):
        answer = job(self.kit, "run_python", {
            "code": "lx.eval('query commandservice command.username ? {no.such}')",
            "purpose": "probe"})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "unknown_command")
        self.assertIn("mt_find_commands", answer["error"]["hint"])
        self.assertEqual(self.fake.crashes, [])

    def test_unknown_command_query_refused_even_if_python_catches_it(self):
        answer = job(self.kit, "run_python", {
            "code": ("try:\n"
                     "    lx.evalN('query commandservice command.argNames ? no.such')\n"
                     "except Exception:\n"
                     "    pass\n"),
            "purpose": "probe"})
        self.assertEqual(self.fake.crashes, [])
        self.assertTrue(answer["ok"], answer)
        # The code swallowed the refusal; the agent still hears of it.
        self.assertEqual(len(answer["result"]["refused"]), 1)
        self.assertIn("no.such", answer["result"]["refused"][0])

    def test_a_query_for_a_real_command_is_left_alone(self):
        answer = job(self.kit, "run_python", {
            "code": "RESULT = lx.eval1('query commandservice command.username ? {scene.save}')",
            "purpose": "look up"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["result"], "scene.save")
        self.assertEqual(self.fake.crashes, [])

    def test_unknown_command_query_refused_from_run_command(self):
        answer = job(self.kit, "run_command", {
            "command": "query commandservice command.desc ? {no.such}"})
        self.assertEqual(answer["error"]["type"], "unknown_command")
        self.assertEqual(self.fake.crashes, [])


if __name__ == "__main__":
    unittest.main()
