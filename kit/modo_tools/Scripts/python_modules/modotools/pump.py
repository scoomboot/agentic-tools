# python
# Copyright (c) 2026 Leonard Billeke
# Copyright (c) 2026 modo-tools contributors
#
# Adapted from modo-mcp 0.3.0, kit/mcp_bridge/Scripts/python_modules/
# mcpbridge/pump.py, under the MIT License:
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Running jobs on Modo's main thread.

Modo's API is not thread-safe. The socket thread only queues jobs; this
module runs them on the main thread and hands each result back.

Two ways to get onto the main thread, tried in order:

  idle    The platform service's "do when user is idle" callback, re-armed
          after every tick. Each tick fires modoTools.exec (or .execRead for a
          read), and that command runs one job. A command, because Modo only
          allows geometry edits inside one; an idle callback is not one.
  manual  No automatic trigger. modoTools.pump runs the queue when the
          artist fires it (bound to a key). Also what the tests use.

A failed exec job is rolled back before its caller hears. A job can't run app.undo, since Modo refuses it inside a command, so the
idle visit that fired modoTools.exec holds the failed job's answer, and once
exec has returned, runs the rollback and only then hands the answer back. No
event from the artist can come in between. The manual pump is a command
itself: its failed jobs answer that one Ctrl+Z undoes them.

One job per tick, and never one inside another: a long job can make Modo
service its event loop and fire the idle callback again, and nested Modo
commands crash it. run_pending refuses to re-enter.

Modo draws the viewport only when no job runs for a moment, so a
job that reads the drawn frame (a screenshot) waits for a later tick, until
Modo has been idle draw_gap_s since the last job ended. After a job that can
change the scene it waits longer when a mesh is large:
Modo draws a large mesh late, so the kit says how long (gap_after_change).
Only idle time counts: time in which the idle visits come. While Modo is busy
with its own work no visit comes and nothing is drawn.
"""

import threading
import time


class Job(object):
    """One request waiting for, or running on, the main thread."""

    __slots__ = ("request", "result", "done", "enqueued_at", "started_at",
                 "abandoned", "lock")

    def __init__(self, request):
        self.request = request
        self.result = None
        self.done = threading.Event()
        self.enqueued_at = time.time()
        self.started_at = None
        # Set when the caller gave up before the job started. Such a job is
        # dropped, never run late: modo-mcp's log showed timed-out requests
        # running minutes afterwards, after the agent had sent them again.
        self.abandoned = False
        self.lock = threading.Lock()

    def claim(self):
        """Main thread: mark the job started, unless it was abandoned."""
        with self.lock:
            if self.abandoned:
                return False
            self.started_at = time.time()
            return True

    def abandon(self):
        """Caller: give up. True if the job had not started (and now never
        will); False if it is already running."""
        with self.lock:
            if self.started_at is None:
                self.abandoned = True
                return True
            return False

    def complete(self, result):
        self.result = result
        self.done.set()

    def wait(self, timeout_s):
        return self.done.wait(timeout_s)


class JobQueue(object):
    """Thread-safe first in, first out."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []

    def put(self, job):
        with self._lock:
            self._items.append(job)

    def take(self, max_items=None):
        """Remove and return up to max_items jobs (all of them if None)."""
        with self._lock:
            if max_items is None or max_items >= len(self._items):
                items, self._items = self._items, []
            else:
                items = self._items[:max_items]
                self._items = self._items[max_items:]
        return items

    def pending(self):
        with self._lock:
            return len(self._items)

    def peek(self):
        """The next job's request, or None."""
        with self._lock:
            if not self._items:
                return None
            return self._items[0].request


class ManualStrategy(object):
    name = "manual"

    def start(self, pump):
        return True

    def stop(self):
        pass


class IdleStrategy(object):
    """A re-arming idle callback that fires a Modo command per tick."""

    name = "idle"

    def __init__(self, exec_command, read_command):
        self._exec = exec_command
        self._read = read_command
        self._visitor = None
        self._pump = None
        self._lx = None
        self._stopped = False

    def start(self, pump):
        try:
            import lx
            import lxifc
        except ImportError:
            return False
        self._pump = pump
        self._lx = lx
        outer = self

        class _PumpVisitor(lxifc.Visitor):
            def vis_Evaluate(self):
                if outer._stopped:
                    return
                outer._pump.ticked()
                try:
                    outer._fire_once()
                finally:
                    outer._arm()

        self._visitor = _PumpVisitor()
        return self._arm()

    def _arm(self):
        if self._stopped:
            return False
        try:
            self._lx.service.Platform().DoWhenUserIsIdle(self._visitor, 0)
            return True
        except Exception:
            return False

    def _fire_once(self):
        pump = self._pump
        if pump.is_busy():
            return
        request = pump.queue.peek()
        if request is None or pump.held_for_draw(request):
            return
        if pump.is_direct(request):
            # The rollback: run here, outside any command, where
            # app.undo is allowed.
            pump.in_idle_visit = pump.outside_command = True
            try:
                pump.run_pending(max_jobs=1)
            except Exception as exc:
                pump.log("modo-tools: %s failed: %s" % (request.get("op"), exc))
            finally:
                pump.in_idle_visit = pump.outside_command = False
            return
        command = self._read if pump.is_read(request) else self._exec
        pump.in_idle_visit = True
        try:
            self._lx.eval(command)
        except Exception as exc:
            pump.log("modo-tools: %s failed: %s" % (command, exc))
        finally:
            pump.in_idle_visit = False
        pump.settle()

    def stop(self):
        self._stopped = True
        self._visitor = None


class MainThreadPump(object):
    """Owns the queue and the way onto the main thread."""

    def __init__(self, run_job, is_read, log=None,
                 exec_command="modoTools.exec",
                 read_command="modoTools.execRead",
                 rollback=None, rollback_key="_rollback_marker",
                 is_direct=None, on_tick=None, wants_drawn=None,
                 gap_after_change=None):
        self.queue = JobQueue()
        # Wants_drawn(request) is a job that reads the drawn
        # viewport. Modo 17.1 draws only when no job runs for a moment, so
        # the idle visit holds such a job until draw_gap_s after the last job
        # ended. The visits go on (no sleep); a held one just runs nothing.
        self.wants_drawn = wants_drawn or (lambda request: False)
        self.draw_gap_s = 0.1
        # gap_after_change() is the seconds Modo needs to
        # draw after a job that can change the scene (17.1 draws a large mesh
        # late: 101,402 points 0.25 s after the edit, 400,000 points 1.2 s).
        # Read once, when such a job ends, on the main thread. A read job
        # after it must not shorten the wait, so the two ends are kept apart.
        self.gap_after_change = gap_after_change
        # perf_counter: time.monotonic steps 16 ms on Windows (Python 3.10).
        self.clock = time.perf_counter
        # Idle time, not clock time: after a heavy delete
        # Modo was busy 0.6 and 1.35 s after a job with no idle visit, and a
        # shot one visit later was the old frame. The visits come about 10
        # microseconds apart while Modo is idle, so the time between two
        # visits counts only up to visit_gap_max_s. None: no job has run.
        self.visit_gap_max_s = 0.01
        self._last_visit = None
        self.idle_since_job = None
        self.idle_since_change = None
        self.last_change_gap = 0.0
        self._run_job = run_job
        self.is_read = is_read
        # is_direct(request): a job the idle visit runs outside any command.
        self.is_direct = is_direct or (lambda request: False)
        self.outside_command = False
        # on_tick(): main thread, each tick (the idle limit).
        self.on_tick = on_tick
        self.log = log or (lambda msg: None)
        self._exec_command = exec_command
        self._read_command = read_command
        # rollback(op, id, marker, answer, can_undo) -> the answer's "undo"
        # (action.settle_failed); can_undo is False inside a command.
        self._rollback = rollback
        self._rollback_key = rollback_key
        # True while the idle visitor's exec runs: a failed job is held for
        # settle() then. Anything else that runs a job (modoTools.pump) is a
        # command that can't undo, and asks.
        self.in_idle_visit = False
        self._held = []         # [(job, result, marker)]
        self._strategy = None
        self.strategy_name = "none"
        self._busy = False
        self.jobs_run = 0
        self.last_op = None
        self._current = None
        self._stopped = False
        self._stop_error = None
        # What ping reports. The main thread writes these, the socket thread
        # reads them; the lock keeps the job and its start time together.
        self._ping_lock = threading.Lock()
        self._last_tick = time.time()

    def start(self, allow_idle=True):
        strategies = [ManualStrategy()]
        if allow_idle:
            strategies.insert(0, IdleStrategy(self._exec_command,
                                              self._read_command))
        for strategy in strategies:
            if strategy.start(self):
                self._strategy = strategy
                self.strategy_name = strategy.name
                if strategy.name == "manual" and allow_idle:
                    self.log(
                        "modo-tools: the idle callback could not be armed on "
                        "this Modo build. Jobs only run when modoTools.pump "
                        "fires: bind it to a key.")
                return True
        return False

    def stop(self, error=None):
        self._stopped = True
        self._stop_error = error
        if self._strategy:
            self._strategy.stop()
            self._strategy = None
        # Unblock every caller still waiting, so no socket thread hangs.
        for job in self.queue.take():
            if job.abandon():
                job.complete(error)

    def submit(self, request, timeout_s):
        """Socket thread. Queue a job and wait for it.

        Returns (result, None) when it finished, or (None, info) when it did
        not finish in time: info["started"] is False when the job never ran
        and now never will, True when it is still running, in which case
        info["job"] is the job, to collect its result later.
        """
        if self._stopped:
            return self._stop_error, None
        job = Job(request)
        self.queue.put(job)
        if job.wait(timeout_s):
            return job.result, None
        if job.abandon():
            return None, {"started": False, "busy_with": self.current_op()}
        if job.wait(0.05):
            return job.result, None
        return None, {"started": True, "job": job,
                      "running_s": round(time.time() - job.started_at, 1)}

    def current_op(self):
        job = self._current
        if job is None or job.started_at is None:
            return None
        return {"op": job.request.get("op"),
                "running_s": round(time.time() - job.started_at, 1)}

    def ticked(self):
        """Main thread: the idle callback fired, so the main thread is free."""
        with self._ping_lock:
            self._last_tick = time.time()
        now = self.clock()
        if self._last_visit is not None:
            idle = min(now - self._last_visit, self.visit_gap_max_s)
            if idle > 0:
                if self.idle_since_job is not None:
                    self.idle_since_job += idle
                if self.idle_since_change is not None:
                    self.idle_since_change += idle
        self._last_visit = now
        if self.on_tick is not None and not self._busy:
            try:
                self.on_tick()
            except Exception as exc:
                self.log("modo-tools: tick failed: %s" % exc)

    def ping_info(self):
        """Socket thread: what the main thread is doing, without asking it."""
        now = time.time()
        with self._ping_lock:
            job = self._current
            last_tick = self._last_tick
            started = job.started_at if job is not None else None
            request = job.request if job is not None else None
        running = None
        if request is not None and started is not None:
            running = {"op": request.get("op"), "id": request.get("id"),
                       "running_s": round(now - started, 1)}
        return {"job": running, "queued": self.queue.pending(),
                "pump": self.strategy_name,
                "last_tick_s": round(now - last_tick, 1)}

    def run_pending(self, max_jobs=None):
        """Main thread only. Runs queued jobs in order; 0 if re-entered."""
        self.ticked()
        if self._busy:
            return 0
        self._busy = True
        try:
            return self._run(max_jobs)
        finally:
            self._busy = False

    def is_busy(self):
        return self._busy

    def held_for_draw(self, request):
        """Whether the idle visit leaves this job for a later one, so Modo
        draws the viewport first."""
        if not self.wants_drawn(request):
            return False
        if (self.idle_since_job is not None
                and self.idle_since_job < self.draw_gap_s):
            return True
        return (self.idle_since_change is not None
                and self.idle_since_change < self.last_change_gap)

    def _ended(self, changed):
        """Main thread: a job, or a failed job's rollback, just ended;
        changed is whether it can have changed the scene."""
        self._last_visit = self.clock()
        self.idle_since_job = 0.0
        if not changed:
            return
        gap = self.draw_gap_s
        if self.gap_after_change is not None:
            try:
                gap = max(gap, float(self.gap_after_change()))
            except Exception as exc:
                self.log("modo-tools: the draw gap couldn't be read: %s" % exc)
        self._last_visit = self.clock()
        self.idle_since_change = 0.0
        self.last_change_gap = gap

    def _changes(self, request):
        """A job that can change the scene: not a read; and an undo
        (direct), which is registered as a read."""
        return bool(self.is_direct(request) or not self.is_read(request))

    def _run(self, max_jobs):
        ran = 0
        for job in self.queue.take(max_jobs):
            if not job.claim():
                continue  # its caller gave up before it started: dropped
            with self._ping_lock:
                self._current = job
            self.last_op = job.request.get("op")
            self.jobs_run += 1
            ran += 1
            try:
                result = self._run_job(job.request)
            finally:
                self._ended(self._changes(job.request))
                with self._ping_lock:
                    self._current = None
                    self._last_tick = time.time()
            marker = None
            if isinstance(result, dict):
                marker = result.pop(self._rollback_key, None)
            if marker is None:
                job.complete(result)
            elif self.in_idle_visit and self._rollback is not None:
                self._held.append((job, result, marker))
            else:
                result["undo"] = self._ask(job, result, marker)
                job.complete(result)
        return ran

    def settle(self):
        """Main thread, in the idle visit, after exec returned: roll back each
        held job, then hand its answer back."""
        while self._held:
            job, result, marker = self._held[0]
            try:
                result["undo"] = self._rollback(
                    job.request.get("op"), job.request.get("id"), marker,
                    result, True)
            except Exception as exc:
                self.log("modo-tools: rollback failed: %s" % exc)
                result["undo"] = {"step": 1, "rolled_back": False, "presses": 1}
            self._ended(True)
            self._held.pop(0)
            job.complete(result)

    def _ask(self, job, result, marker):
        """A failed job run inside a command (modoTools.pump): nothing can be
        undone from here, so the answer asks the user."""
        if self._rollback is not None:
            try:
                return self._rollback(job.request.get("op"),
                                      job.request.get("id"), marker, result,
                                      False)
            except Exception as exc:
                self.log("modo-tools: rollback failed: %s" % exc)
        return {"step": 1, "rolled_back": False, "presses": 1}
