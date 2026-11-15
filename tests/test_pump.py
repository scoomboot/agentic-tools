"""The queue and the idle pump, through the real modoTools.exec commands."""

import sys
import time
import unittest

import support


def jobs():
    return sys.modules["modotools.jobs"]


class PumpTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.ran = []
        self.addCleanup(jobs().OPS.pop, "t_record", None)
        self.addCleanup(jobs().OPS.pop, "t_reenter", None)

        @jobs().op("t_record")
        def record(payload, kit):
            self.ran.append(payload.get("n"))
            return {"n": payload.get("n")}

    def submit(self, n, timeout_s=5.0):
        """Queue a job from a caller thread, as the socket thread would."""
        import threading
        out = {}

        def caller():
            out["result"], out["info"] = self.kit.pump.submit(
                {"id": "r-%d" % n, "op": "t_record", "payload": {"n": n}},
                timeout_s)

        thread = threading.Thread(target=caller)
        thread.daemon = True
        thread.start()
        return thread, out

    def wait_queued(self, count):
        end = time.time() + 2
        while self.kit.pump.queue.pending() < count and time.time() < end:
            time.sleep(0.005)
        self.assertEqual(self.kit.pump.queue.pending(), count)

    def test_the_idle_pump_is_armed(self):
        self.assertEqual(self.kit.pump.strategy_name, "idle")
        self.assertEqual(len(self.fake.idle), 1)

    def test_two_queued_jobs_take_two_ticks(self):
        first, _ = self.submit(1)
        self.wait_queued(1)
        second, _ = self.submit(2)
        self.wait_queued(2)
        self.fake.tick()
        self.assertEqual(self.ran, [1])
        self.fake.tick()
        self.assertEqual(self.ran, [1, 2])
        first.join(2)
        second.join(2)
        self.assertEqual(self.fake.ran, ["modoTools.start", "modoTools.exec",
                                         "modoTools.exec"])

    def test_a_read_runs_through_execRead(self):
        thread, out = None, {}
        import threading

        def caller():
            out["result"], out["info"] = self.kit.pump.submit(
                {"id": "r-s", "op": "status", "payload": {}}, 5.0)

        thread = threading.Thread(target=caller)
        thread.start()
        self.wait_queued(1)
        self.fake.tick()
        thread.join(2)
        self.assertTrue(out["result"]["ok"], out)
        self.assertEqual(self.fake.ran[-1], "modoTools.execRead")

    def test_a_tick_during_a_job_runs_nothing(self):
        @jobs().op("t_reenter")
        def reenter(payload, kit):
            # Modo servicing its event loop mid-job fires the idle callback.
            self.fake.tick()
            return {}

        import threading
        out = {}

        def caller(op, n):
            out[n] = self.kit.pump.submit(
                {"id": "r-%d" % n, "op": op, "payload": {"n": n}}, 5.0)

        a = threading.Thread(target=caller, args=("t_reenter", 1))
        a.start()
        self.wait_queued(1)
        b = threading.Thread(target=caller, args=("t_record", 2))
        b.start()
        self.wait_queued(2)
        self.fake.tick()
        self.assertEqual(self.ran, [], "the nested tick ran a job")
        self.assertEqual(self.fake.nested, [])
        self.fake.tick()
        self.assertEqual(self.ran, [2])
        a.join(2)
        b.join(2)

    def test_a_job_abandoned_before_its_tick_never_runs(self):
        thread, out = self.submit(7, timeout_s=0.05)
        thread.join(2)
        self.assertIsNone(out["result"])
        self.assertFalse(out["info"]["started"])
        for _ in range(3):
            self.fake.tick()
        self.assertEqual(self.ran, [])
        self.assertEqual(self.kit.pump.queue.pending(), 0)

    # -- a drawn frame ---------------------------------------------
    #
    # The probe (2026-11-14): on 17.1 gl.snapshot grabs the last drawn frame,
    # and Modo draws only when no job runs for a moment. A shot sent straight
    # after an edit was the old frame 10 of 10 times; after a 0.05 s gap, the
    # new one 10 of 10. No call inside the job helped.

    def drawn_op(self):
        self.addCleanup(jobs().OPS.pop, "t_shot", None)
        self.addCleanup(jobs().DRAWN.discard, "t_shot")

        @jobs().op("t_shot", read=True, drawn=True)
        def shot(payload, kit):
            self.ran.append("shot")
            return {}

    def submit_op(self, op, n):
        import threading
        out = {}

        def caller():
            out["result"], out["info"] = self.kit.pump.submit(
                {"id": "r-%d" % n, "op": op, "payload": {"n": n}}, 5.0)

        thread = threading.Thread(target=caller)
        thread.daemon = True
        thread.start()
        return thread, out

    def clock(self, start=100.0):
        now = [start]
        self.kit.pump.clock = lambda: now[0]
        return now

    def idle(self, now, seconds, step=0.005):
        """Modo idle for this long: the idle visits come, a step apart (in
        Modo about 10 microseconds apart). A little over, for the sums."""
        for _ in range(int(round(seconds / step)) + 2):
            now[0] += step
            self.fake.tick()

    def test_a_shot_straight_after_a_job_waits_for_a_draw(self):
        self.drawn_op()
        now = self.clock()
        first, _ = self.submit(1)
        self.wait_queued(1)
        second, out = self.submit_op("t_shot", 2)
        self.wait_queued(2)
        self.fake.tick()
        self.assertEqual(self.ran, [1])
        self.idle(now, 0.05)
        self.assertEqual(self.ran, [1], "the shot ran before Modo could draw")
        self.assertEqual(self.kit.pump.queue.pending(), 1)
        self.idle(now, self.kit.pump.draw_gap_s - 0.05)
        self.assertEqual(self.ran, [1, "shot"])
        first.join(2)
        second.join(2)
        self.assertTrue(out["result"]["ok"], out)

    def test_a_shot_after_a_quiet_moment_runs_at_once(self):
        self.drawn_op()
        now = self.clock()
        first, _ = self.submit(1)
        self.wait_queued(1)
        self.fake.tick()
        self.idle(now, self.kit.pump.draw_gap_s)
        second, _ = self.submit_op("t_shot", 2)
        self.wait_queued(1)
        self.fake.tick()
        self.assertEqual(self.ran, [1, "shot"])
        first.join(2)
        second.join(2)

    def test_other_jobs_are_not_held(self):
        self.clock()
        first, _ = self.submit(1)
        self.wait_queued(1)
        second, _ = self.submit(2)
        self.wait_queued(2)
        self.fake.tick()
        self.fake.tick()
        self.assertEqual(self.ran, [1, 2])
        first.join(2)
        second.join(2)

    def test_the_screenshot_op_waits_for_a_draw(self):
        self.assertTrue(jobs().wants_drawn({"op": "screenshot"}))
        self.assertFalse(jobs().wants_drawn({"op": "status"}))
        self.assertFalse(jobs().wants_drawn({"op": "set_points"}))

    # -- a large mesh is drawn late --------------------------
    #
    # 17.1, 2026-11-14: after an edit Modo drew a 1 m cube at once, a mesh of
    # 101,402 points about 0.25 s later and one of 400,000 about 1.2 s later;
    # a view change, or an edit of a small mesh in the same scene, at once.
    # So the wait after a job that can change the scene comes from the kit
    # (gap_after_change), and a read between the edit and the shot must not
    # shorten it.

    def read_op(self):
        self.addCleanup(jobs().OPS.pop, "t_read", None)

        @jobs().op("t_read", read=True)
        def read(payload, kit):
            self.ran.append("read")
            return {}

    def run_one(self, op="t_record", n=1):
        thread, _ = self.submit_op(op, n)
        self.wait_queued(1)
        self.fake.tick()
        thread.join(2)

    def queue_shot(self):
        thread, out = self.submit_op("t_shot", 9)
        self.wait_queued(1)
        self.addCleanup(thread.join, 2)
        return out

    def test_a_shot_after_a_change_waits_the_change_gap(self):
        self.drawn_op()
        now = self.clock()
        self.kit.pump.gap_after_change = lambda: 0.5
        self.run_one()
        self.queue_shot()
        self.idle(now, 0.2)
        self.assertEqual(self.ran, [1], "the shot ran before a large mesh drew")
        self.idle(now, 0.3)
        self.assertEqual(self.ran, [1, "shot"])

    def test_a_read_between_does_not_shorten_the_wait(self):
        self.drawn_op()
        self.read_op()
        now = self.clock()
        self.kit.pump.gap_after_change = lambda: 0.5
        self.run_one()
        self.idle(now, 0.05)
        self.run_one("t_read", 2)
        self.queue_shot()
        self.idle(now, 0.15)
        self.assertEqual(self.ran, [1, "read"])
        self.idle(now, 0.3)
        self.assertEqual(self.ran, [1, "read", "shot"])

    def test_a_shot_after_a_read_alone_waits_the_short_gap(self):
        self.drawn_op()
        self.read_op()
        now = self.clock()
        self.kit.pump.gap_after_change = lambda: 5.0
        self.run_one("t_read", 1)
        self.queue_shot()
        self.fake.tick()
        self.assertEqual(self.ran, ["read"])
        self.idle(now, self.kit.pump.draw_gap_s)
        self.assertEqual(self.ran, ["read", "shot"])

    def test_an_undo_job_is_a_change(self):
        # undo_last and rollback run outside a command and are registered as
        # reads, but they change the scene.
        self.drawn_op()
        self.addCleanup(jobs().OPS.pop, "t_direct", None)
        self.addCleanup(jobs().DIRECT.discard, "t_direct")

        @jobs().op("t_direct", direct=True)
        def direct(payload, kit):
            self.ran.append("direct")
            return {}

        now = self.clock()
        self.kit.pump.gap_after_change = lambda: 0.5
        self.run_one("t_direct", 1)
        self.queue_shot()
        self.idle(now, 0.2)
        self.assertEqual(self.ran, ["direct"])
        self.idle(now, 0.3)
        self.assertEqual(self.ran, ["direct", "shot"])

    def test_a_failed_jobs_rollback_is_a_change(self):
        # settle() undoes a failed job after the job ended: the wait counts
        # from the end of the undo.
        self.drawn_op()
        now = self.clock()
        pump = self.kit.pump
        pump.gap_after_change = lambda: 0.5

        def rollback(op, req_id, marker, answer, can_undo):
            now[0] += 1.0
            return {"rolled_back": True}

        pump._rollback = rollback
        done = sys.modules["modotools.pump"].Job({"id": "r-1", "op": "t_record"})
        pump._held.append((done, {"ok": False}, object()))
        pump.settle()
        self.assertTrue(done.done.is_set())
        self.queue_shot()
        self.idle(now, 0.2)
        self.assertEqual(self.ran, [])
        self.idle(now, 0.3)
        self.assertEqual(self.ran, ["shot"])

    def test_time_modo_is_busy_is_not_time_to_draw(self):
        # 17.1, 2026-11-14: after a heavy delete Modo was busy for 0.6 and
        # 1.35 s after a job, with no idle visit; the shot then came 1 visit
        # later, was not held, and showed the old frame. Only time in which
        # the idle visits come counts.
        self.drawn_op()
        now = self.clock()
        self.run_one()
        now[0] += 1.35
        self.queue_shot()
        self.fake.tick()
        self.fake.tick()
        self.assertEqual(self.ran, [1], "a busy Modo is not a drawn viewport")
        self.idle(now, self.kit.pump.draw_gap_s)
        self.assertEqual(self.ran, [1, "shot"])

    def test_a_gap_that_cannot_be_read_is_the_short_gap(self):
        self.drawn_op()
        now = self.clock()

        def broken():
            raise RuntimeError("no scene")

        self.kit.pump.gap_after_change = broken
        self.run_one()
        self.queue_shot()
        self.idle(now, self.kit.pump.draw_gap_s)
        self.assertEqual(self.ran, [1, "shot"])

    def test_manual_pump_when_idle_cannot_be_armed(self):
        support.stop_running_kit()
        self.fake.idle_supported = False
        self.fake.idle = []
        self.fake.run_command("modoTools.start port:0")
        kit = sys.modules["modotools"].running()
        self.assertEqual(kit.pump.strategy_name, "manual")
        self.assertTrue(any("modoTools.pump" in line for line in self.fake.event_log))
        self.kit = kit
        thread, out = self.submit(3)
        self.wait_queued(1)
        self.fake.run_command("modoTools.pump")
        thread.join(2)
        self.assertEqual(self.ran, [3])


if __name__ == "__main__":
    unittest.main()
