import json
import os
import sys
import threading
import unittest

import support


def jobs():
    return sys.modules["modotools.jobs"]


def read_log():
    path = os.path.join(os.environ["MODO_TOOLS_HOME"], "ops.log")
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class OpLogTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.addCleanup(jobs().OPS.pop, "t_peek", None)
        self.addCleanup(jobs().OPS.pop, "t_fail", None)

    def run_op(self, op, payload=None):
        out = {}

        def caller():
            out["r"], _ = self.kit.pump.submit(
                {"id": "r-1", "op": op, "payload": payload or {}}, 5.0)

        thread = threading.Thread(target=caller)
        thread.start()
        support.pump_until(self.fake, lambda: not thread.is_alive())
        thread.join(1)
        return out["r"]

    def test_op_start_is_on_disk_before_the_job_runs(self):
        @jobs().op("t_peek")
        def peek(payload, kit):
            # The job reads the file: its own op_start must be there already.
            return {"last": read_log()[-1]}

        answer = self.run_op("t_peek", {"x": 1})
        self.assertTrue(answer["ok"], answer)
        last = answer["result"]["last"]
        self.assertEqual(last["event"], "op_start")
        self.assertEqual(last["op"], "t_peek")
        self.assertEqual(last["id"], "r-1")
        self.assertIn('"x": 1', last["preview"])
        self.assertEqual(read_log()[-1]["event"], "op_ok")

    def test_op_error_carries_the_type(self):
        @jobs().op("t_fail")
        def fail(payload, kit):
            print("before the error")
            raise KeyError("nope")

        answer = self.run_op("t_fail")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "job_exception")
        self.assertIn("KeyError", answer["error"]["message"])
        self.assertIn("Traceback", answer["error"]["traceback"])
        self.assertEqual(answer["stdout"], "before the error\n")
        # A failed job is rolled back after its op_error line.
        last = [e for e in read_log() if e["event"] == "op_error"][-1]
        self.assertEqual(last["type"], "KeyError")

    def test_last_session(self):
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "other.log")
        log = oplog.OpLog(path)
        self.assertEqual(log.open_session(), "first session")
        log.op_start("status", "r-1", "status")
        log.op_ok("status", "r-1", 3)
        self.assertEqual(oplog.OpLog(path).open_session(), "ended cleanly")
        log.op_start("test_sleep", "r-2", 'test_sleep {"seconds": 20}')
        # Modo dies here: no op_ok.
        self.assertEqual(oplog.OpLog(path).open_session(),
                         'Modo stopped during test_sleep: {"seconds": 20}')

    def test_last_session_names_a_rollback_modo_died_in(self):
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "other.log")
        log = oplog.OpLog(path)
        log.open_session()
        log.op_start("run_python", "r-1", "raise ValueError()")
        log.op_error("run_python", "r-1", "ValueError", "", 2)
        log.rollback("run_python", "r-1")
        # Modo dies in app.undo: no rollback_done.
        self.assertEqual(oplog.OpLog(path).last_session(),
                         "Modo stopped during the rollback of run_python: "
                         "raise ValueError()")
        log.rollback_done("run_python", "r-1", True)
        self.assertEqual(oplog.OpLog(path).last_session(), "ended cleanly")

    def test_last_session_names_the_code(self):
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "other.log")
        log = oplog.OpLog(path)
        log.open_session()
        payload = {"code": "import modo\ns = 1\n", "purpose": "p"}
        log.op_start("run_python", "r-1", oplog.preview("run_python", payload))
        self.assertEqual(oplog.OpLog(path).last_session(),
                         "Modo stopped during run_python: import modo | s = 1")
        log.open_session()
        log.op_start("run_command", "r-2",
                     oplog.preview("run_command", {"command": "item.create mesh"}))
        self.assertEqual(oplog.OpLog(path).last_session(),
                         "Modo stopped during run_command: item.create mesh")

    def test_a_long_preview_keeps_its_ellipsis_in_the_log(self):
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "other.log")
        log = oplog.OpLog(path)
        log.open_session()
        log.op_start("run_python", "r-1",
                     oplog.preview("run_python", {"code": "x = 1\n" * 200}))
        ending = oplog.OpLog(path).last_session()
        self.assertTrue(ending.endswith("..."), ending[-20:])

    def test_a_log_written_before_3_4_still_reads(self):
        # Older kits wrote the op name in front of the preview.
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "other.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"event": "session"}\n'
                         '{"event": "op_start", "op": "test_sleep", "id": "r-1", '
                         '"preview": "test_sleep {\\"seconds\\": 20}"}\n')
        self.assertEqual(oplog.OpLog(path).last_session(),
                         'Modo stopped during test_sleep: {"seconds": 20}')

    def test_a_log_over_1_mb_rotates(self):
        oplog = sys.modules["modotools.oplog"]
        path = os.path.join(os.environ["MODO_TOOLS_HOME"], "big.log")
        log = oplog.OpLog(path)
        log.open_session()
        log.op_start("test_sleep", "r-1", "test_sleep")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("x" * 1023 + "\n") * 1025)  # junk lines are skipped
        # Modo died mid-job in a session whose log is now over 1 MB.
        self.assertEqual(oplog.OpLog(path).open_session(),
                         "Modo stopped during test_sleep")
        self.assertTrue(os.path.getsize(path + ".1") > oplog.MAX_BYTES)
        self.assertLess(os.path.getsize(path), 1000)
        # Between jobs too, and the new file still knows its session.
        log.op_ok("x", "r-0", 1)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("x" * 1023 + "\n") * 1025)
        log.op_start("status", "r-2", "status")
        self.assertLess(os.path.getsize(path), 1000)
        self.assertEqual(oplog.OpLog(path).last_session(),
                         "Modo stopped during status")

    def test_the_kit_reports_the_last_session(self):
        self.assertEqual(self.kit.last_session, "first session")
        self.fake.run_command("modoTools.start port:0")
        kit = sys.modules["modotools"].running()
        self.assertEqual(kit.last_session, "ended cleanly")


if __name__ == "__main__":
    unittest.main()
