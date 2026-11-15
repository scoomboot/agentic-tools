# python
"""One undo step per call.

What the probe and the Modo check found on 17.1 (2026-11-08):

  * modoTools.exec is already one undo step, whatever its job runs: a
    prim.cube made of five tool commands, two new meshes and a move, a
    channel set() (probes). The 3 presses the first version
    counted were empty jobs sitting on top.
  * Every exec job is a step, even one that changed nothing. A failed
    job's half edit is a step too.
  * A command block (BlockBegin / BlockEnd) around the job loses channel
    set()s to undo: the step undoes and the channel stays where it was set.
    So the kit opens none.
  * A block can't stay open into the next call.
  * modoTools.execRead shows in Modo's undo list, but Ctrl+Z steps over it.

So a call that may change the scene runs through modoTools.exec and is one
step; a read, and run_python with read_only, run through execRead and are
none.

Rolling back a failed call. A probe (2026-11-08): a Python undo object
recorded straight inside exec, with no block, is called back when its step is
undone and redone, and an idle callback's app.undo undoes a cube and a channel
set with it. So every exec job records a Marker, which changes nothing and
only says "step N reversed" or "forward", and the pump undoes a failed job's
step from the idle visit that ran it, after exec has returned, then checks
the job's marker reversed. A job can't run app.undo itself: Modo refuses it
inside a command, and the manual pump is one.
"""

WAY = "exec command"

# Markers kept, newest last. Rollback of an action reads further back than rollback of one call.
KEEP = 500

ROLLED_BACK = {"rolled_back": True}
ASK = {"step": 1, "rolled_back": False, "presses": 1}
UNSURE = {"step": 1, "rolled_back": "unsure"}


def answer(read_only):
    """The answer's "undo": null for a read-only job, else its one step."""
    return None if read_only else {"step": 1}


class Markers(object):
    """The marker table: each exec job's marker, by step number. Main thread
    only: Modo calls Reverse and Forward there, and jobs run there."""

    def __init__(self, log=None):
        self._states = {}
        self._objects = {}      # kept alive while Modo holds them
        self._about = {}        # step -> the call it is (7.8: undo_last)
        self.last = 0
        self._log = log or (lambda msg: None)
        # Told every Reverse and Forward (the journal's marker),
        # except while the kit undoes its own steps (quiet): those are
        # rollbacks, which the rollback writes itself.
        self.listener = None
        self.quiet = False

    def record(self, about=None):
        """Record a new marker in the running command's undo step. Returns its
        step number. about: the call it is, {"op", "id", "purpose",
        "session"}, for undo_last's answer."""
        import lx
        self.last += 1
        step = self.last
        marker = _make_marker(self, step)
        lx.service.Undo().Record(marker)
        self._objects[step] = marker
        self._states[step] = "forward"
        if about is not None:
            self._about[step] = about
        for old in sorted(self._states)[:-KEEP]:
            self._states.pop(old, None)
            self._objects.pop(old, None)
            self._about.pop(old, None)
        return step

    def about(self, step):
        """The call a step is, as record() was told, or {}."""
        return dict(self._about.get(step) or {})

    def forward(self):
        """Every step whose marker isn't reversed."""
        return set(s for s, state in self._states.items() if state == "forward")

    def newest_forward(self):
        """The newest step whose marker isn't reversed: the agent call
        app.undo would reach if the top of the stack is ours. None when
        every step the table knows is undone, or there is none."""
        for step in sorted(self._states, reverse=True):
            if self._states[step] == "forward":
                return step
        return None

    def state(self, step):
        """"forward", "reversed", or None for a step the table doesn't know."""
        return self._states.get(step)

    def _note(self, step, state):
        if step in self._states:
            self._states[step] = state

    def called(self, step, state):
        """From Modo's undo: never raise into it."""
        try:
            self._note(step, state)
            if self.listener is not None and not self.quiet:
                self.listener(step, state)
        except Exception as exc:
            try:
                self._log("modo-tools: undo marker %d %s failed: %s"
                          % (step, state, exc))
            except Exception:
                pass


def _make_marker(table, step):
    import lxifc

    class Marker(lxifc.Undo):
        def undo_Forward(self):
            table.called(step, "forward")

        def undo_Reverse(self):
            table.called(step, "reversed")

    return Marker()


class quiet(object):
    """While the kit's own app.undo and app.redo run: the markers they
    reach aren't the user's undo."""

    def __init__(self, markers):
        self.markers = markers

    def __enter__(self):
        self.was = self.markers.quiet
        self.markers.quiet = True

    def __exit__(self, *exc):
        self.markers.quiet = self.was
        return False


class Rollback(object):
    """What the pump does with a failed exec job's step."""

    def __init__(self, markers, oplog, log=None):
        self.markers = markers
        self.oplog = oplog
        self.off = False
        self._log = log or (lambda msg: None)

    def mode(self, strategy_name):
        """status's "rollback": "idle", "ask" or "off"."""
        if self.off:
            return "off"
        return "idle" if strategy_name == "idle" else "ask"

    def run(self, name, req_id, step):
        """Main thread, in the idle visit, outside any command: undo the
        failed job's step once, and check its marker reversed. Returns the
        answer's "undo"."""
        if self.off:
            return dict(ASK)
        self.oplog.rollback(name, req_id)
        with quiet(self.markers):
            answer = self._undo(name, req_id, step)
        return answer

    def _undo(self, name, req_id, step):
        import lx
        try:
            lx.eval("!!app.undo")
        except Exception as exc:
            self._log("modo-tools: app.undo failed for %s: %s" % (name, exc))
            self.oplog.rollback_done(name, req_id, False)
            return dict(ASK)
        if self.markers.state(step) == "reversed":
            self.oplog.rollback_done(name, req_id, True)
            return dict(ROLLED_BACK)
        # The step on top wasn't this job's: put it back, and stop trusting
        # the stack for the rest of the session.
        try:
            lx.eval("!!app.redo")
        except Exception as exc:
            self._log("modo-tools: app.redo failed: %s" % exc)
        self.off = True
        self._log("modo-tools: rollback unsure after %s (%s): app.undo didn't "
                  "reverse its step, so it was redone. Rollback is off for "
                  "this session; check the scene." % (name, req_id))
        self.oplog.rollback_done(name, req_id, "unsure")
        return dict(UNSURE)
