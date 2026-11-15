# python
"""run_job, and the op table.

Every job goes through run_job, on Modo's main thread. It is the one place
that writes the op log, catches what a job raises, collects its stdout, and
hands each changing job to the journal.

An op is a function op(payload, kit) -> plain data. It fails on purpose by
raising errors.OpError. Each feature adds ops to OPS; nothing else
changes. The socket thread reads OPS only to refuse an
unknown name; it never calls an op.
"""

import io
import sys
import time
import traceback

from . import errors, guard, oplog, protocol, undo

# On a failed job's answer, for the pump only: its marker's step number.
ROLLBACK_KEY = "_rollback_marker"

# name -> (function, read). A read op runs through modoTools.execRead, which
# adds no undo entry; every other op runs through modoTools.exec.
OPS = {}

# Ops the idle visit runs itself, outside any Modo command (the
# rollback: Modo refuses app.undo inside one). Registered as reads too.
DIRECT = set()

# Ops that need the viewport drawn since the last job: the pump
# holds them until no job has run for a moment, so Modo draws first.
DRAWN = set()


def op(name, read=False, direct=False, drawn=False):
    def register(fn):
        OPS[name] = (fn, read or direct)
        if direct:
            DIRECT.add(name)
        if drawn:
            DRAWN.add(name)
        return fn
    return register


def wants_drawn(request):
    return request.get("op") in DRAWN


def known(name):
    return name in OPS


def runs_outside_command(request):
    return request.get("op") in DIRECT


def is_read(name):
    entry = OPS.get(name)
    return bool(entry and entry[1])


def runs_as_read(request):
    """Whether this job goes through modoTools.execRead: a read op, or
    run_python with read_only. Every exec job is an undo step, even one that
    changes nothing; a read leaves none."""
    name = request.get("op")
    if is_read(name):
        return True
    payload = request.get("payload") or {}
    return name == "run_python" and payload.get("read_only") is True


# -- hooks: the undo marker, and the journal ----------------------------

def before_job(request, kit):
    """Record the job's undo marker, in its own step; returns the marker's
    step number, or None for a job that runs as a read. No undo group:
    modoTools.exec is already one step, and a command block loses channel
    sets (undo.py)."""
    if runs_as_read(request):
        return None
    payload = request.get("payload") or {}
    return kit.markers.record(about={
        "op": request.get("op"), "id": request.get("id"),
        "purpose": payload.get("purpose"),
        "session": request.get("session")})


def after_job(request, response, kit, scene=None, trace=None, marker=None):
    """An exec job's entry in the journal, and the answer's
    "journal". scene is the scene the job started in; trace is its guard;
    marker its undo marker's step."""
    if not (runs_as_read(request) or trace is None or kit.journal is None):
        written = kit.journal.entry(request, response, scene, trace, marker)
        if written is not None:
            response["journal"] = written
    result = response.get("result")
    if isinstance(result, dict):
        # What an op hands the journal alone (the points
        # fit_backdrops wrote) never goes out, journal or no journal.
        result.pop("_written", None)


# -- run_job -------------------------------------------------------------------

def run_job(request, kit):
    """Main thread. Run one job and return its answer."""
    req_id = request.get("id")
    name = request.get("op")
    payload = request.get("payload") or {}
    log = kit.oplog
    started = time.time()
    actions = kit.action
    actions.caller = request.get("session")
    in_action, step = actions.next_step(request, not runs_as_read(request))

    # On disk before the job's first line runs, so a crash names it.
    log.op_start(name, req_id, oplog.preview(name, payload),
                 action=in_action, step=step)

    from . import ops_log
    marked = ops_log.mark()

    marker = None
    in_step = None
    changes = not runs_as_read(request)
    # The scene the job starts in, for the journal's file.
    scene = kit.journal.before() if changes and kit.journal is not None else None
    trace = None
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    try:
        # !! on every command and no crashing query, for this job only.
        open_action = actions.open
        with guard.Guard(read_only=name == "run_python" and runs_as_read(request),
                         action=(open_action.label if open_action is not None
                                 and name not in DIRECT else None),
                         refuse=guard.ACTION_REFUSED,
                         trace=changes) as trace:
            marker = before_job(request, kit)
            # Numbers the step, or refuses the job, inside an action.
            in_step = actions.enter(request, marker)
            result = OPS[name][0](payload, kit)
        response = protocol.make_response(req_id, result)
    except errors.OpError as exc:
        response = protocol.make_error(
            req_id, exc.type, exc.message, traceback_text=exc.traceback,
            event_log=exc.event_log, hint=exc.hint)
    except (Exception, SystemExit) as exc:
        response = protocol.make_error(
            req_id, protocol.JOB_EXCEPTION,
            "%s: %s" % (type(exc).__name__, exc),
            traceback_text=traceback.format_exc())
        response["error"]["exception"] = type(exc).__name__
    finally:
        sys.stdout = saved_stdout

    # Every job that may change the scene says what it left on the undo
    # stack; so does run_python with read_only, as null.
    if not is_read(name):
        response["undo"] = in_step or undo.answer(runs_as_read(request))
    closed = actions.note_for(request.get("session"))
    if closed:
        response["action_closed"] = closed
    # A failed job whose marker is in its step: the pump rolls it back, and
    # takes this key off before the answer goes out (pump.py).
    if not response["ok"] and marker is not None:
        response[ROLLBACK_KEY] = marker

    if not response["ok"] and not response["error"].get("event_log"):
        response["error"]["event_log"] = ops_log.since(marked)

    try:
        after_job(request, response, kit, scene, trace, marker)
    except Exception as exc:
        response["journal"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

    ms = int((time.time() - started) * 1000)
    response["stdout"] = captured.getvalue()
    response["ms"] = ms
    if response["ok"]:
        log.op_ok(name, req_id, ms)
    else:
        err = response["error"]
        log.op_error(name, req_id, err.get("exception") or err["type"],
                     err["message"], ms)
    return response


# -- the first ops --------------------------------------------------------

@op("status", read=True)
def status(payload, kit):
    return {
        "port": kit.port,
        "pump": kit.pump.strategy_name,
        "queue": kit.pump.queue.pending(),
        "last_session": kit.last_session,
        "undo": undo.WAY,
        "rollback": kit.rollback.mode(kit.pump.strategy_name),
        "action": kit.action.status(),
        "journal": kit.journal.status() if kit.journal is not None else None,
        "listen": kit.listen.status() if kit.listen is not None else None,
    }


@op("test_sleep")
def test_sleep(payload, kit):
    """Holds the main thread for `seconds` (at most 60). For the Modo check:
    two kits' long jobs at once must run one after the other."""
    seconds = max(0.0, min(float(payload.get("seconds", 1)), 60.0))
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(0.1, max(0.0, end - time.time())))
    return {"slept": seconds}
