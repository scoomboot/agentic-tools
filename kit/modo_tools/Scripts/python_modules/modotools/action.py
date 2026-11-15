# python
"""Actions: several calls the agent can roll back together.

An action is a run of steps. A probe (2026-11-08) found that a command block
can't stay open from one call into the next, so each changing call stays its
own undo step, as for one call, and its undo marker is numbered in the action. begin
and commit are read ops: they add no step.

Rollback is the idle undo, in a loop. A probe (2026-11-08): app.undo from the
idle callback steps over execRead entries as Ctrl+Z does, and each undo
reversed its own step's marker. A probe: app.undo then app.redo in one idle visit
put back an edit that wasn't the kit's. So the loop undoes the action's top
step that isn't already reversed, checks its marker, and goes on; when no
marker of ours reversed, the step was the user's, so it redoes it at once and
stops. Nothing is read between that undo and its redo: on 17.1 a read there
seems to end the redo list.

The action's state lives on the main thread only: begin, commit, every job,
every marker, the idle limit checked each pump tick, and the rollback in the
idle visit. The action is never written to disk; the op log only names it.
"""

import os
import time

from . import errors, guard, protocol
from .jobs import op
from .undo import quiet

# Refused while an action is open (guard.py holds the list).
REFUSED = guard.ACTION_REFUSED

# A call refused before it changed anything leaves the action open.
REFUSALS = protocol.REFUSALS

IDLE_ENV = "MODO_TOOLS_ACTION_IDLE_S"
IDLE_S = 300.0

FIRST = "Commit or roll back first (mt_commit or mt_rollback), then call again."


def idle_limit():
    try:
        return max(1.0, float(os.environ.get(IDLE_ENV, IDLE_S)))
    except ValueError:
        return IDLE_S


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


class Action(object):
    def __init__(self, label, session):
        self.label = label
        self.session = session
        self.steps = []         # marker step numbers, step 1 first
        self.told = set()       # step numbers already said undone by the user
        self.last_call = time.time()


class Actions(object):
    """The open action, if any. Kit.action."""

    def __init__(self, markers, oplog, log=None, idle_s=None):
        self.markers = markers
        self.oplog = oplog
        self.open = None
        self.idle_s = idle_limit() if idle_s is None else idle_s
        self.caller = None      # the running job's session (run_job sets it)
        self._log = log or (lambda msg: None)
        self._notes = {}        # session -> action_closed, said on its next answer
        self.journal = None     # Kit.journal: the action's records

    def tell(self, event, **fields):
        """The journal's copy of an op log action record. Never fails
        the action: the journal keeps its own write error."""
        if self.journal is None:
            return
        try:
            self.journal.action(event, **fields)
        except Exception as exc:
            self._log("modo-tools: journal %s failed: %s" % (event, exc))

    # -- what the action holds -------------------------------------------------

    def undone(self, action):
        """Step numbers whose marker the user's Ctrl+Z reversed."""
        return [n for n, marker in enumerate(action.steps, 1)
                if self.markers.state(marker) == "reversed"]

    def presses(self, action):
        return len(action.steps) - len(self.undone(action))

    def status(self):
        action = self.open
        if action is None:
            return None
        return {"label": action.label, "steps": len(action.steps),
                "idle_s": round(time.time() - action.last_call, 1)}

    def note_for(self, session):
        """An action the kit closed on its own, for its session's next answer."""
        return self._notes.pop(session, None)

    # -- each job --------------------------------------------------------------

    def next_step(self, request, changes):
        """(label, step) a changing job of the action's session will be, for
        the op log; (None, None) otherwise."""
        action = self.open
        if action is None or not changes or request.get("session") != action.session:
            return None, None
        return action.label, len(action.steps) + 1

    def enter(self, request, marker):
        """run_job, after the job's marker is recorded. Refuses a change from
        another session, or one after the user undid every step; otherwise numbers
        the step. Returns the answer's "undo", or None outside an action."""
        action = self.open
        if action is None:
            return None
        session = request.get("session")
        if session == action.session:
            action.last_call = time.time()
        if marker is None:
            return None         # reads run, from any session
        if session != action.session:
            raise errors.OpError(
                protocol.IN_ACTION,
                "action '%s' is open in another session, so this change wasn't "
                "made" % action.label,
                hint="Reads still run. The agent that began it commits or rolls "
                     "it back; it is committed on its own after %d s idle."
                     % self.idle_s)
        self.refuse_if_undone(action)
        undone = self.undone(action)
        action.steps.append(marker)
        answer = {"action": action.label, "step": len(action.steps)}
        new = [n for n in undone if n not in action.told]
        if new:
            answer["undone_by_user"] = new
            action.told.update(new)
        return answer

    def refuse_if_undone(self, action):
        if action.steps and len(self.undone(action)) == len(action.steps):
            self.close(action)
            self._log("mt > closed: %s, every step undone in Modo" % action.label)
            self.oplog.action_commit(action.label, len(action.steps), 0, "undone")
            self.tell("action_commit", label=action.label,
                      steps=len(action.steps), reason="undone")
            raise errors.OpError(
                protocol.ACTION_UNDONE,
                "The user undid '%s': every one of its %s is undone in Modo, so the "
                "action is closed and this call wasn't made"
                % (action.label, plural(len(action.steps), "step")),
                hint="Check the scene with mt_describe_mesh / mt_scene_graph "
                     "before going on; mt_begin opens a new action.")

    def require(self, session, what):
        """The open action, for commit or rollback by its own session."""
        action = self.open
        if action is None:
            raise errors.OpError(protocol.BAD_REQUEST,
                                 "no action is open to %s" % what,
                                 hint="mt_begin opens one.")
        if session != action.session:
            raise errors.OpError(
                protocol.IN_ACTION,
                "action '%s' was begun in another session" % action.label,
                hint="Only the agent that began it can %s it." % what)
        self.refuse_if_undone(action)
        return action

    def refuse_open(self, what, session=None):
        """undo_last undoes one call, and an open action's steps
        are the action's to undo. session: the caller's, since only the
        agent that began the action can roll it back or commit it."""
        action = self.open
        if action is None:
            return
        if session != action.session:
            raise errors.OpError(
                protocol.IN_ACTION,
                "action '%s' is open in another session, so %s wasn't done"
                % (action.label, what),
                hint="The agent that began it commits or rolls it back; it "
                     "is committed on its own after %d s idle." % self.idle_s)
        raise errors.OpError(
            protocol.IN_ACTION,
            "action '%s' is open, so %s wasn't done" % (action.label, what),
            hint="mt_rollback undoes the whole action. Or mt_commit keeps "
                 "it, and mt_undo_last then undoes its steps one call at "
                 "a time.")

    # -- begin, commit ---------------------------------------------------------

    def begin(self, label, session):
        if self.open is not None:
            raise errors.OpError(
                protocol.IN_ACTION,
                "action '%s' is open; actions don't nest" % self.open.label,
                hint=FIRST)
        self.open = Action(label, session)
        self._log("mt > begin: " + label)
        self.oplog.action_begin(label)
        self.tell("action_begin", label=label)
        return {"action": label}

    def commit(self, action, reason="commit"):
        steps, presses = len(action.steps), self.presses(action)
        undone = self.undone(action)
        self.close(action)
        line = "mt > commit: %s (%s)" % (action.label, plural(steps, "step"))
        if reason == "idle":
            line += ", idle for %d s" % self.idle_s
        elif reason == "stop":
            line += ", modoTools.stop"
        self._log(line)
        self.oplog.action_commit(action.label, steps, presses, reason)
        self.tell("action_commit", label=action.label, steps=steps,
                  reason=reason)
        answer = {"action": action.label, "steps": steps, "presses": presses,
                  "undo": "%d Ctrl+Z %s the whole action"
                          % (presses, "undoes" if presses == 1 else "undo")}
        if undone:
            answer["undone_by_user"] = undone
        return answer

    def close(self, action):
        if self.open is action:
            self.open = None

    def check_idle(self):
        """Each pump tick: commit an action left idle past the limit."""
        action = self.open
        if action is None or time.time() - action.last_call < self.idle_s:
            return
        answer = self.commit(action, reason="idle")
        self._notes[action.session] = {
            "action": action.label, "reason": "idle", "steps": answer["steps"],
            "presses": answer["presses"]}

    def stop(self):
        """modoTools.stop: commit the open action as it stands."""
        if self.open is not None:
            self.commit(self.open, reason="stop")

    # -- rollback --------------------------------------------------------------

    def roll_back(self, action):
        """Main thread, in the idle visit, outside any command: undo the
        action's steps from the top. Returns (rolled back, left, why it
        stopped: None, "not ours" or the app.undo error)."""
        self.rolling_back(action)
        self.close(action)
        pending = [m for m in reversed(action.steps)
                   if self.markers.state(m) != "reversed"]
        with quiet(self.markers):
            return self._undo(action, pending)

    def rolling_back(self, action):
        self.oplog.action_rollback(action.label, len(action.steps))
        self.tell("action_rollback", label=action.label,
                  steps=len(action.steps))

    def _undo(self, action, pending):
        import lx
        rolled = 0
        for i, marker in enumerate(pending):
            try:
                lx.eval("!!app.undo")
            except Exception as exc:
                self._log("modo-tools: app.undo failed rolling back '%s': %s"
                          % (action.label, exc))
                return rolled, len(pending) - i, str(exc) or "app.undo failed"
            if self.markers.state(marker) == "reversed":
                rolled += 1
                if self.journal is not None:
                    self.journal.rolled_step(marker)
                continue
            # No marker of ours reversed: the step on top was someone else's.
            # Put it back at once; nothing may be read in between.
            try:
                lx.eval("!!app.redo")
            except Exception as exc:
                self._log("modo-tools: app.redo failed: %s" % exc)
            return rolled, len(pending) - i, "not ours"
        return rolled, 0, None

    def step_of(self, action, marker):
        return action.steps.index(marker) + 1


def stopped_note(action, left, why, top):
    """What rollback's "note" says when it couldn't reach every step."""
    if why == "not ours":
        return ("The user's edit is on top of step %d; it stays. Undo it, then %s "
                "more Ctrl+Z, to finish." % (top, left))
    return ("app.undo failed (%s): %s in Modo finish the rollback."
            % (why, plural(left, "Ctrl+Z press")))


def ask_note(left):
    return ("Not rolled back: the kit can't undo from inside a command (the "
            "manual pump). %s in Modo undo the action; with the manual pump, "
            "each modoTools.pump is a step too." % plural(left, "Ctrl+Z press"))


# -- ops ---------------------------------------------------------------------------

def _label(payload):
    label = " ".join(str(payload.get("label") or "").split())
    if not label:
        raise errors.OpError(
            protocol.BAD_REQUEST, "no label given",
            hint="Pass label: one line on what the action does, e.g. "
                 "\"Raise the cube's top\".")
    return label[:200]


@op("begin", read=True)
def begin(payload, kit):
    return kit.action.begin(_label(payload), kit.action.caller)


@op("commit", read=True)
def commit(payload, kit):
    actions = kit.action
    return actions.commit(actions.require(actions.caller, "commit"))


@op("rollback", read=True, direct=True)
def rollback(payload, kit):
    """Runs in the idle visit outside any command (pump.py), since Modo
    refuses app.undo inside one. Fired by hand through modoTools.pump, it is
    inside one, and answers the presses instead."""
    actions = kit.action
    action = actions.require(actions.caller, "roll back")
    out = {"action": action.label}
    if not kit.pump.outside_command or kit.rollback.off:
        left = actions.presses(action)
        actions.rolling_back(action)
        actions.close(action)
        out.update(rolled_back=0, left=left, note=ask_note(left))
        return out
    pending = [m for m in action.steps
               if actions.markers.state(m) != "reversed"]
    rolled, left, why = actions.roll_back(action)
    out.update(rolled_back=rolled, left=left)
    if left:
        top = actions.step_of(action, pending[left - 1])
        out["note"] = stopped_note(action, left, why, top)
    else:
        out["scene"] = "as before mt_begin"
    return out


def settle_failed(kit, op_name, req_id, marker, response, can_undo):
    """The pump, for a failed exec job with a marker. Outside an action, only
    the job's own step is rolled back. Inside one, a call refused
    before it changed anything (protocol.REFUSALS) leaves the action open
    and whole; any other failure rolls back the whole action, and it
    is closed."""
    actions = kit.action
    action = actions.open
    err_type = ((response or {}).get("error") or {}).get("type")
    if action is not None and marker in action.steps and err_type in REFUSALS:
        return _refused(kit, action, op_name, req_id, marker, can_undo,
                        (response or {}).get("undo"))
    if action is None or marker not in action.steps:
        if not can_undo:
            return {"step": 1, "rolled_back": False, "presses": 1}
        return kit.rollback.run(op_name, req_id, marker)
    if not can_undo or kit.rollback.off:
        presses = actions.presses(action)
        actions.rolling_back(action)
        actions.close(action)
        return {"action": action.label, "rolled_back": False, "presses": presses}
    pending = [m for m in action.steps if actions.markers.state(m) != "reversed"]
    # As for one call: if Modo dies in the undo, the op log names this call.
    actions.oplog.rollback(op_name, req_id)
    rolled, left, why = actions.roll_back(action)
    actions.oplog.rollback_done(op_name, req_id, not left)
    # The failed call's own step isn't counted: it is "this call".
    answer = {"action": action.label, "rolled_back": max(0, rolled - 1),
              "left": left}
    if left:
        answer["note"] = stopped_note(action, left, why,
                                      actions.step_of(action, pending[left - 1]))
    return answer


def _refused(kit, action, op_name, req_id, marker, can_undo, entered=None):
    """A refused call inside the action. Undo its own step, so the action's
    steps stay the ones before it, and so does the scene: run_python can
    change something before a line of it is refused. Where the step can't
    be undone (the manual pump, rollback off, or the undo unsure), it stays
    in the action, so a rollback or the presses count it, and the answer
    says "kept": the server must not call that "nothing was written". entered: the "undo" enter() gave the call; what it said
    the user undid is told once, so it is passed on."""
    rolled = False
    if can_undo and not kit.rollback.off:
        rolled = kit.rollback.run(op_name, req_id, marker).get("rolled_back")
    answer = {"action": action.label, "refused": True}
    if rolled is True:
        action.steps.remove(marker)
    else:
        answer["kept"] = True
    answer["steps"] = len(action.steps)
    if isinstance(entered, dict) and entered.get("undone_by_user"):
        answer["undone_by_user"] = entered["undone_by_user"]
    return answer
