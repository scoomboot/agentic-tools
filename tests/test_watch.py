"""The server's watch: gone, busy or stuck, told apart by a ping on a second
connection. Each row of row of the crash table, from a
fake kit or from the real kit in fake Modo. Timings are cut from seconds to
tenths so the suite stays quick; the rules are the same."""

import asyncio
import json
import os
import socket
import sys
import threading
import time
import unittest

import support
from test_server import InThread, free_port
from modo_tools import bridge as bridge_mod
from modo_tools import server, watch, window


def quick_watch(port, **kw):
    # windows=None: can't see Modo's windows, so the real Modo on a dev
    # machine doesn't leak into the tests.
    timings = dict(first_s=0.2, every_s=0.2, answer_s=0.3, stuck_s=0.3,
                   window_s=0.05, windows=lambda: None)
    timings.update(kw)
    return watch.Watch("127.0.0.1", port, **timings)


def quick_bridge(port, **kw):
    return bridge_mod.Bridge(port=port, connect_timeout=0.3,
                             watch=quick_watch(port, **kw))


class SilentKit(object):
    """Accepts connections and reads them, but answers nothing after the
    first `hellos` hellos, and leaves the first `silent_pings` pings
    unanswered: Python in Modo has stopped running, or stalled for a moment."""

    def __init__(self, hellos=0, silent_pings=99):
        self.hellos = hellos
        self.silent_pings = silent_pings
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                op = json.loads(line).get("op")
                if op == "hello" and self.hellos > 0:
                    self.hellos -= 1
                    conn.sendall(b'{"id": "hello", "ok": true, "result": '
                                 b'{"protocol": 1, "kit": "fake"}}\n')
                elif op == "ping":
                    if self.silent_pings > 0:
                        self.silent_pings -= 1
                    else:
                        conn.sendall(b'{"id": "p", "ok": true, "result": '
                                     b'{"job": null, "queued": 0, "pump": '
                                     b'"idle", "last_tick_s": 0.1}}\n')

    def close(self):
        self.sock.close()
        for conn in self.conns:
            conn.close()


def write_log(lines):
    path = os.path.join(os.environ["MODO_TOOLS_HOME"], "ops.log")
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


class StuckHardTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)

    def test_no_answer_to_hello_or_ping_is_stuck_hard(self):
        kit = SilentKit(hellos=0)
        self.addCleanup(kit.close)
        started = time.time()
        text = asyncio.run(server.mt_status_text(quick_bridge(kit.port)))
        self.assertTrue(text.startswith("stuck_hard:"), text)
        self.assertIn("Task Manager", text)
        self.assertLess(time.time() - started, 3)

    def test_a_call_that_goes_silent_is_stuck_hard_after_two_pings(self):
        kit = SilentKit(hellos=1)  # the call's own hello, then nothing
        self.addCleanup(kit.close)
        b = quick_bridge(kit.port)
        started = time.time()
        text = asyncio.run(server.mt_run_command_text("item.create mesh", bridge=b))
        took = time.time() - started
        self.assertTrue(text.startswith("stuck_hard:"), text)
        self.assertIn("mt_status", text)
        # first_s, then two pings answer_s apart: well under the call's 30 s.
        self.assertGreater(took, 0.2 + 0.3 + 0.3 - 0.05)
        self.assertLess(took, 3)

    def test_one_silent_ping_is_not_enough(self):
        # Python in Modo stalls past one ping, then answers the second.
        kit = SilentKit(hellos=99, silent_pings=1)
        self.addCleanup(kit.close)
        seen = asyncio.run(quick_watch(kit.port).look("r-1"))
        self.assertEqual(seen.kind, "waiting")


class FakeWindows(object):
    """What Windows shows of Modo: alive, hung, and the dialogs open."""

    def __init__(self, alive=True, hung=False, dialogs=(), pids=()):
        self.alive = alive
        self.hung = hung
        self.titles = list(dialogs)
        self.pids = set(pids)
        self.closed = []

    def __call__(self):
        return window.ModoWindows(
            alive=self.alive, hung=self.hung,
            dialogs=[window.Dialog(t, lambda t=t: self._close(t))
                     for t in self.titles],
            pids=set(self.pids))

    def _close(self, title):
        self.titles.remove(title)
        self.closed.append(title)
        return True


def job_open():
    """The op log as it is while a job runs: started, no ending."""
    write_log([{"event": "session"},
               {"event": "op_start", "op": "run_command", "id": "r-8",
                "preview": "scene.open C:/nothing.lxo"}])


class DialogTest(unittest.TestCase):
    """A dialog that opens inside a job holds Python in Modo: the kit goes
    silent, as in a freeze, and then stops taking connections. Modo's
    windows tell it apart (an early test: a dialog was told as stuck_hard,
    then as unreachable)."""

    def setUp(self):
        support.temp_home(self)

    def silent(self):
        kit = SilentKit(hellos=99)
        self.addCleanup(kit.close)
        return kit

    def test_a_dialog_in_our_job_is_cancelled(self):
        job_open()
        shown = FakeWindows(dialogs=["Open File"])
        seen = asyncio.run(quick_watch(self.silent().port, windows=shown).look("r-8"))
        self.assertEqual(seen.kind, "answered")
        self.assertIn("Open File", seen.text)
        self.assertEqual(shown.closed, ["Open File"])

    def test_answering_can_be_turned_off(self):
        job_open()
        shown = FakeWindows(dialogs=["Open File"])
        seen = asyncio.run(quick_watch(self.silent().port, windows=shown,
                                       answer_dialogs=False).look("r-8"))
        self.assertEqual(seen.kind, "dialog")
        self.assertEqual(shown.closed, [])

    def test_a_dialog_with_no_job_running_is_left_alone(self):
        # Not ours: the user's own dialog, or their own script's.
        write_log([{"event": "session"}])
        shown = FakeWindows(dialogs=["Save Changes?"])
        seen = asyncio.run(quick_watch(self.silent().port, windows=shown).look("r-1"))
        self.assertEqual(seen.kind, "dialog")
        self.assertIn("Save Changes?", seen.text)
        self.assertEqual(shown.closed, [])

    def test_silent_but_responding_is_stuck_not_stuck_hard(self):
        seen = asyncio.run(quick_watch(self.silent().port,
                                       windows=FakeWindows()).look("r-1"))
        self.assertEqual(seen.kind, "stuck")
        self.assertIn("dialog", seen.text)

    def test_silent_and_not_responding_is_stuck_hard(self):
        seen = asyncio.run(quick_watch(self.silent().port,
                                       windows=FakeWindows(hung=True)).look("r-1"))
        self.assertEqual(seen.kind, "stuck_hard")
        self.assertIn("Not Responding", seen.text)

    def test_refused_while_a_dialog_is_open_is_not_unreachable(self):
        # The kit's listen queue filled while Python was held.
        b = quick_bridge(free_port(), windows=FakeWindows(dialogs=["Open File"]))
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertIn("Open File", text)
        self.assertNotIn("Modo is closed", text)

    def test_refused_with_modo_running_is_not_closed(self):
        job_open()
        b = quick_bridge(free_port(), windows=FakeWindows())
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertNotIn("Modo is closed", text)

    def test_refused_with_no_job_running_is_a_stopped_kit(self):
        write_log([{"event": "session"}])
        b = quick_bridge(free_port(), windows=FakeWindows())
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("unreachable:"), text)
        self.assertIn("modoTools.start", text)

    def test_refused_with_no_modo_window_is_unreachable(self):
        b = quick_bridge(free_port(), windows=FakeWindows(alive=False))
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("unreachable:"), text)

    def crashed_in_a_job(self):
        """The op log a Modo (pid 111) left when it died mid-job."""
        write_log([{"event": "session", "pid": 111},
                   {"event": "op_start", "op": "run_python", "id": "r-4",
                    "preview": "while True: pass"}])

    def test_a_job_left_by_a_crashed_modo_is_not_ours(self):
        # Modo restarted (pid 222), kit not started yet, a dialog open:
        # it is the user's, not a job's, and is left alone.
        self.crashed_in_a_job()
        shown = FakeWindows(dialogs=["Recover Scene?"], pids=[222])
        b = quick_bridge(free_port(), windows=shown)
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertIn("it is the user's", text)
        self.assertEqual(shown.closed, [])

    def test_refused_after_a_crash_and_restart_is_a_stopped_kit(self):
        self.crashed_in_a_job()
        b = quick_bridge(free_port(), windows=FakeWindows(pids=[222]))
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("unreachable:"), text)
        self.assertIn("modoTools.start", text)

    def test_a_job_in_the_running_modo_is_ours(self):
        self.crashed_in_a_job()
        shown = FakeWindows(dialogs=["Open File"], pids=[111])
        seen = asyncio.run(quick_watch(self.silent().port, windows=shown).look("r-4"))
        self.assertEqual(seen.kind, "answered")
        self.assertEqual(shown.closed, ["Open File"])

    def test_a_silent_hello_with_a_dialog_says_so(self):
        kit = SilentKit(hellos=0)
        self.addCleanup(kit.close)
        b = quick_bridge(kit.port, windows=FakeWindows(dialogs=["Save Changes?"]))
        text = asyncio.run(server.mt_status_text(b))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertIn("Save Changes?", text)


class Classify(unittest.TestCase):
    """The table as a function: ping answer -> what the agent is told."""

    def kind(self, ping, ours="r-1"):
        return watch.classify(ping, ours, stuck_s=10).kind

    def test_rows(self):
        ours = {"op": "run_python", "id": "r-1", "running_s": 12.0}
        other = {"op": "test_sleep", "id": "r-9", "running_s": 3.0}
        self.assertEqual(self.kind({"job": ours, "queued": 0, "pump": "idle",
                                    "last_tick_s": 12.0}), "busy")
        self.assertEqual(self.kind({"job": other, "queued": 1, "pump": "idle",
                                    "last_tick_s": 30.0}), "busy")
        self.assertEqual(self.kind({"job": None, "queued": 1, "pump": "idle",
                                    "last_tick_s": 10.5}), "stuck")
        # The pump ticked a moment ago: the job is about to run.
        self.assertEqual(self.kind({"job": None, "queued": 1, "pump": "idle",
                                    "last_tick_s": 2.0}), "waiting")
        # Manual pump: jobs wait for modoTools.pump. Not stuck.
        self.assertEqual(self.kind({"job": None, "queued": 1, "pump": "manual",
                                    "last_tick_s": 60.0}), "manual")
        self.assertEqual(watch.classify(None, "r-1", 10).kind, "stuck_hard")

    def test_a_run_of_the_same_look_shows_its_first_and_last(self):
        # The Modo check: a dialog open for a minute gave six stuck lines.
        seen = [watch.Seen("stuck", "idle %d s" % t, at_s=t) for t in (10, 20, 30, 40)]
        seen.append(watch.Seen("busy", "status has run 0.1 s", at_s=50))
        lines = watch.format_seen(seen, ok=True).strip().splitlines()
        self.assertEqual(lines, [
            "While waiting:",
            "  at 10 s, stuck: idle 10 s",
            "  ... (2 more like it)",
            "  at 40 s, stuck: idle 40 s",
            "  at 50 s, busy: status has run 0.1 s",
            "  then it ran."])


class KitWatchTest(unittest.TestCase):
    """The real kit in fake Modo."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    def test_a_long_job_is_busy_then_answers(self):
        b = quick_bridge(self.kit.port)
        job = InThread(server.call_text("test_sleep", {"seconds": 1.0},
                                        timeout_ms=5000, bridge=b))
        self.assertTrue(support.pump_until(self.fake, job.done, timeout=8))
        self.assertIn('"slept": 1.0', job.value)
        self.assertIn("busy: test_sleep has run", job.value)

    def test_a_job_past_its_timeout_is_busy_not_frozen(self):
        b = quick_bridge(self.kit.port)
        job = InThread(server.call_text("test_sleep", {"seconds": 1.5},
                                        timeout_ms=600, bridge=b))
        self.assertTrue(support.pump_until(self.fake, job.done, timeout=8))
        self.assertTrue(job.value.startswith("still_running:"), job.value)
        self.assertIn("Modo is busy, not frozen", job.value)

    def test_a_job_behind_another_is_busy(self):
        first = InThread(server.call_text("test_sleep", {"seconds": 1.5},
                                          timeout_ms=5000,
                                          bridge=quick_bridge(self.kit.port)))
        end = time.time() + 2
        while self.kit.pump.queue.pending() == 0 and time.time() < end:
            time.sleep(0.005)
        second = InThread(server.call_text("status", {}, timeout_ms=700,
                                           bridge=quick_bridge(self.kit.port)))
        self.assertTrue(support.pump_until(
            self.fake, lambda: first.done() and second.done(), timeout=8))
        self.assertTrue(second.value.startswith("busy:"), second.value)
        self.assertIn("test_sleep", second.value)

    def test_a_pump_that_never_ticks_is_stuck(self):
        # Nobody ticks: a dialog holds Modo's main thread.
        text = asyncio.run(server.call_text("status", {}, timeout_ms=1200,
                                            bridge=quick_bridge(self.kit.port)))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertIn("dialog", text)
        self.assertEqual(self.kit.pump.jobs_run, 0)

    def test_stuck_then_the_dialog_closes(self):
        job = InThread(server.call_text("status", {}, timeout_ms=5000,
                                        bridge=quick_bridge(self.kit.port)))
        time.sleep(0.9)  # stuck for a while; then the user closes the dialog
        self.assertTrue(support.pump_until(self.fake, job.done))
        self.assertIn('"queue"', job.value)
        self.assertIn("stuck: ", job.value)
        self.assertIn("then it ran", job.value)


class KitDialogTest(unittest.TestCase):
    """The real kit in fake Modo, a dialog in Modo's windows."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    def test_a_dialog_during_our_job_is_cancelled_and_the_call_answers(self):
        shown = FakeWindows(dialogs=["Open File"])
        b = quick_bridge(self.kit.port, windows=shown)
        job = InThread(server.call_text("test_sleep", {"seconds": 0.6},
                                        timeout_ms=5000, bridge=b))
        self.assertTrue(support.pump_until(self.fake, job.done, timeout=8))
        self.assertIn('"slept": 0.6', job.value)
        self.assertEqual(shown.closed, ["Open File"])
        self.assertIn("answered:", job.value)
        self.assertIn("Open File", job.value)

    def test_a_dialog_with_the_job_still_queued_is_left_for_eric(self):
        # Nobody pumps: the job never starts, so the dialog isn't ours.
        shown = FakeWindows(dialogs=["Preferences"])
        text = asyncio.run(server.call_text(
            "status", {}, timeout_ms=1200,
            bridge=quick_bridge(self.kit.port, windows=shown)))
        self.assertTrue(text.startswith("stuck:"), text)
        self.assertIn("Preferences", text)
        self.assertEqual(shown.closed, [])


class UnreachableTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)

    def test_unreachable_names_the_job_modo_died_in(self):
        write_log([{"event": "session"},
                   {"event": "op_start", "op": "run_python", "id": "r-3",
                    "preview": "while True: pass"}])
        text = asyncio.run(server.mt_status_text(quick_bridge(free_port())))
        self.assertTrue(text.startswith("unreachable:"), text)
        self.assertIn("Modo stopped during run_python: while True: pass", text)

    def test_unreachable_after_a_clean_end_says_nothing_more(self):
        write_log([{"event": "session"},
                   {"event": "op_start", "op": "status", "id": "r-1"},
                   {"event": "op_ok", "op": "status", "id": "r-1"}])
        text = asyncio.run(server.mt_status_text(quick_bridge(free_port())))
        self.assertNotIn("stopped during", text)


class OpLogToolTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)

    def test_mt_op_log_reads_the_file_with_no_kit(self):
        lines = [{"event": "session"}] + [
            {"event": "op_ok", "op": "status", "id": "r-%d" % n} for n in range(9)]
        write_log(lines)
        text = server.mt_op_log_text(5)
        shown = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([e["id"] for e in shown], ["r-4", "r-5", "r-6", "r-7", "r-8"])

    def test_mt_op_log_reaches_into_the_rotated_file(self):
        path = write_log([{"event": "op_ok", "id": "new"}])
        with open(path + ".1", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "op_ok", "id": "old"}) + "\n")
        shown = [json.loads(l)["id"] for l in server.mt_op_log_text(5).splitlines()]
        self.assertEqual(shown, ["old", "new"])

    def test_mt_op_log_with_no_file(self):
        self.assertIn("no op log", server.mt_op_log_text(5))


if __name__ == "__main__":
    unittest.main()
