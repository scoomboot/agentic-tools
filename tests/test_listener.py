import sys
import time
import unittest

import support


def jobs():
    return sys.modules["modotools.jobs"]


class ListenerTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)

    def test_a_wrong_token_gets_bad_token_and_a_closed_socket(self):
        answer = self.client.hello(token="not-the-token")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "bad_token")
        self.assertTrue(self.client.is_closed())

    def test_a_job_before_hello_gets_bad_token_and_a_closed_socket(self):
        answer = self.client.ask({"id": "r-1", "op": "status", "payload": {}})
        self.assertEqual(answer["error"]["type"], "bad_token")
        self.assertTrue(self.client.is_closed())

    def test_protocol_2_gets_protocol_mismatch(self):
        answer = self.client.hello(protocol=2)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "protocol_mismatch")
        self.assertIn("Update the kit", answer["error"]["hint"])

    def test_hello_answers_versions(self):
        answer = self.client.hello()
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        self.assertEqual(result["protocol"], 1)
        self.assertEqual(result["kit"], "0.4.0")
        self.assertEqual(result["modo"], "17.1v1")
        self.assertRegex(result["python"], r"^\d+\.\d+\.\d+$")

    def test_unknown_op(self):
        self.client.hello()
        answer = self.client.ask({"id": "r-1", "op": "no_such_op", "payload": {}})
        self.assertEqual(answer["error"]["type"], "unknown_op")
        self.assertEqual(answer["id"], "r-1")

    def test_status_runs_on_the_main_thread(self):
        self.client.hello()
        asked = support.Asked(self.client, {"id": "r-1", "op": "status",
                                            "payload": {}})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        answer = asked.answer
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["port"], self.kit.port)
        self.assertEqual(answer["result"]["pump"], "idle")
        self.assertEqual(answer["result"]["last_session"], "first session")
        self.assertEqual(self.fake.off_thread, [])

    def test_the_socket_thread_never_touches_lx(self):
        self.client.hello()
        self.client.ask({"id": "r-0", "op": "no_such_op"})
        for n in range(3):
            asked = support.Asked(self.client, {"id": "r-%d" % n, "op": "status"})
            support.pump_until(self.fake, asked.done)
        self.assertEqual(self.fake.off_thread, [])

    def test_a_job_that_never_starts_times_out(self):
        self.client.hello()
        answer = self.client.ask({"id": "r-1", "op": "status",
                                  "payload": {}, "timeout_ms": 50})
        self.assertEqual(answer["error"]["type"], "timeout")
        self.assertIn("never started", answer["error"]["hint"])
        self.fake.tick()
        self.assertEqual(self.kit.pump.jobs_run, 0)

    def test_still_running_then_late(self):
        release = {"go": False}

        @jobs().op("t_slow")
        def slow(payload, kit):
            end = time.time() + 5
            while not release["go"] and time.time() < end:
                time.sleep(0.01)
            return {"slow": "done"}

        self.addCleanup(jobs().OPS.pop, "t_slow", None)
        self.client.hello()
        asked = support.Asked(self.client, {"id": "r-slow", "op": "t_slow",
                                            "timeout_ms": 200})
        # The job starts on this thread and holds it; let the caller time out.
        import threading
        end = time.time() + 2
        while self.kit.pump.queue.pending() == 0 and time.time() < end:
            time.sleep(0.005)
        threading.Timer(0.6, lambda: release.__setitem__("go", True)).start()
        self.fake.tick()
        end = time.time() + 3
        while not asked.done() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(asked.answer["error"]["type"], "still_running")

        asked = support.Asked(self.client, {"id": "r-next", "op": "status"})
        support.pump_until(self.fake, asked.done)
        late = asked.answer.get("late")
        self.assertEqual(late and late[0]["id"], "r-slow")
        self.assertEqual(late[0]["result"], {"slow": "done"})

    def test_ping_needs_hello(self):
        answer = self.client.ask({"id": "p-1", "op": "ping"})
        self.assertEqual(answer["error"]["type"], "bad_token")

    def test_ping_when_idle(self):
        self.client.hello()
        self.fake.tick()
        answer = self.client.ask({"id": "p-1", "op": "ping"})
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        self.assertIsNone(result["job"])
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["pump"], "idle")
        self.assertLess(result["last_tick_s"], 1.0)
        self.assertEqual(self.kit.pump.jobs_run, 0)

    def test_ping_is_answered_mid_job(self):
        release = {"go": False}

        @jobs().op("t_slow")
        def slow(payload, kit):
            end = time.time() + 5
            while not release["go"] and time.time() < end:
                time.sleep(0.01)
            return {"slow": "done"}

        self.addCleanup(jobs().OPS.pop, "t_slow", None)
        self.client.hello()
        asked = support.Asked(self.client, {"id": "r-slow", "op": "t_slow"})
        end = time.time() + 2
        while self.kit.pump.queue.pending() == 0 and time.time() < end:
            time.sleep(0.005)
        pinger = support.Client(self.kit.port)
        self.addCleanup(pinger.close)
        pinger.hello()
        seen = {}

        def ping():
            time.sleep(0.3)
            seen["ping"] = pinger.ask({"id": "p-1", "op": "ping"})
            release["go"] = True

        import threading
        threading.Thread(target=ping, daemon=True).start()
        # The job holds this thread, as a long job holds Modo's main thread.
        self.fake.tick()
        support.pump_until(self.fake, asked.done)
        result = seen["ping"]["result"]
        self.assertEqual(result["job"]["op"], "t_slow")
        self.assertEqual(result["job"]["id"], "r-slow")
        self.assertGreaterEqual(result["job"]["running_s"], 0.2)
        self.assertTrue(asked.answer["ok"], asked.answer)
        self.assertEqual(self.fake.off_thread, [])

    def test_last_tick_ages_while_the_pump_is_idle(self):
        self.client.hello()
        self.fake.tick()
        time.sleep(0.3)
        answer = self.client.ask({"id": "p-1", "op": "ping"})
        self.assertGreaterEqual(answer["result"]["last_tick_s"], 0.3)
        self.fake.tick()
        answer = self.client.ask({"id": "p-2", "op": "ping"})
        self.assertLess(answer["result"]["last_tick_s"], 0.3)

    def test_stop_fails_queued_jobs_and_closes(self):
        self.client.hello()
        asked = support.Asked(self.client, {"id": "r-1", "op": "status"})
        end = time.time() + 2
        while self.kit.pump.queue.pending() == 0 and time.time() < end:
            time.sleep(0.005)
        self.fake.run_command("modoTools.stop")
        end = time.time() + 3
        while not asked.done() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(asked.answer["error"]["type"], "stopped")
        self.assertEqual(asked.answer["id"], "r-1")
        with self.assertRaises(OSError):
            support.Client(self.kit.port, timeout=1)


if __name__ == "__main__":
    unittest.main()
