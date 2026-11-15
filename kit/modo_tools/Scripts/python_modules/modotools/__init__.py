# python
"""modotools: the half of modo-tools that runs inside Modo.

    protocol.py   the message format (the server keeps a byte-for-byte copy)
    token.py      the shared secret in ~/.modo_tools/token
    listener.py   the socket thread; never touches lx
    pump.py       the queue and the way onto Modo's main thread
    jobs.py       run_job and the op table
    undo.py       one undo step per call: what the kit relies on, and the answer
    action.py     actions: begin, commit, rollback over a run of steps
    oplog.py      ~/.modo_tools/ops.log, written before each job
    journal.py    ~/.modo_tools/journals/: what each changing call did
    listen.py     the user's edits between calls, counted for the journal
    errors.py     OpError: an op's failure, typed for the agent
    guard.py      !! on every command, and no query that crashes Modo
    ops_run.py    run_command, run_python, set_channels, save_scene, undo_last
    ops_lookup.py command_info, find_commands
    ops_log.py    event_log, and the lines a failed job caused
    ops_scene.py  scene_summary, scene_graph, get_item, get_selection
    ops_mesh.py   describe_mesh, points, set_points, and read_mesh: a mesh as plain lists
    meshmath.py   the facts, from those lists; no Modo imports
    ops_view.py   screenshot
    settings.py   the menu's flag files (Start with Modo), data folder, op log tail

Standard library only, and nothing imported at module level that touches
Modo: this package is imported on the first modoTools.* command, never while
Modo scans the kit.
"""

import sys

__version__ = "0.4.0"

# The running kit is kept on `sys`, not in a module global, so that reloading
# the package doesn't orphan a listener that still holds the port.
_STATE_KEY = "_modo_tools_state"


def _state():
    state = getattr(sys, _STATE_KEY, None)
    if state is None:
        state = {"kit": None}
        setattr(sys, _STATE_KEY, state)
    return state


class PortInUse(RuntimeError):
    pass


class Kit(object):
    """What a running kit holds. Ops get it as their second argument."""

    def __init__(self, pump, listener, oplog, last_session):
        self.pump = pump
        self.listener = listener
        self.oplog = oplog
        self.last_session = last_session
        self.markers = None     # undo.Markers
        self.rollback = None    # undo.Rollback
        self.action = None      # action.Actions
        self.journal = None     # journal.Journal
        self.listen = None      # listen.Listen

    @property
    def port(self):
        return self.listener.port


def running():
    return _state().get("kit")


def modo_info():
    """Modo's and Python's versions. Main thread only: called from start()."""
    version = None
    try:
        import lx
        platform = lx.service.Platform()
        try:
            version = "%d.%dv%d" % (platform.AppVersionMajor(),
                                    platform.AppVersionMinor(),
                                    platform.AppVersionSP())
        except Exception:
            number = int(platform.AppVersion())
            version = "%d.%d" % (number // 100, (number % 100) // 10)
    except Exception:
        version = None
    return {
        "kit": __version__,
        "modo": version,
        "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
    }


def start(port=None, log=None, allow_idle=True):
    """Main thread. Start listening; returns the Kit. A running kit is
    stopped first, so a second modoTools.start restarts it."""
    from . import jobs, journal, listen, listener, oplog, protocol, pump, token, undo
    from . import action  # registers begin, commit, rollback
    from . import ops_log, ops_lookup, ops_mesh, ops_run, ops_scene, ops_view  # noqa: F401  (register ops)

    log = log or (lambda msg: None)
    stop()

    secret = token.load_or_create(protocol.token_path())
    the_log = oplog.OpLog(protocol.oplog_path())
    last_session = the_log.open_session()
    hello_info = modo_info()

    kit = Kit(None, None, the_log, last_session)
    kit.markers = undo.Markers(log=log)
    kit.rollback = undo.Rollback(kit.markers, the_log, log=log)
    kit.action = action.Actions(kit.markers, the_log, log=log)
    journal.prune()
    kit.journal = journal.Journal(hello_info, log=log)
    # The user's Ctrl+Z and the actions, in the journal.
    kit.markers.listener = kit.journal.marker
    kit.action.journal = kit.journal
    # The user's edits between calls, as gaps.
    kit.listen = listen.Listen(log=log)
    kit.listen.start()
    kit.journal.listen = kit.listen
    the_pump = pump.MainThreadPump(
        lambda request: jobs.run_job(request, kit), jobs.runs_as_read, log=log,
        rollback=lambda op, req_id, marker, answer, can_undo:
            settle_failed(kit, op, req_id, marker, answer, can_undo),
        rollback_key=jobs.ROLLBACK_KEY, is_direct=jobs.runs_outside_command,
        on_tick=kit.action.check_idle, wants_drawn=jobs.wants_drawn,
        gap_after_change=ops_view.draw_gap)
    the_pump.start(allow_idle=allow_idle)
    the_listener = listener.Listener(
        the_pump, secret, hello_info,
        port=protocol.DEFAULT_PORT if port is None else port)
    try:
        the_listener.start()
    except OSError as exc:
        the_pump.stop()
        if getattr(exc, "errno", None) in (48, 98, 10048, 10013):
            raise PortInUse(
                "port %d is already in use. Run modoTools.stop if modo-tools "
                "is still running; otherwise start on another port with "
                "modoTools.start port:15681 and set MODO_TOOLS_PORT=15681 "
                "for the server." % the_listener.port)
        raise
    kit.pump = the_pump
    kit.listener = the_listener
    _state()["kit"] = kit
    return kit


def settle_failed(kit, op, req_id, marker, answer, can_undo):
    """The pump's rollback of a failed job (action.settle_failed), then its
    record in the journal: rolled_back, or unsure."""
    from . import action
    undo = action.settle_failed(kit, op, req_id, marker, answer, can_undo)
    try:
        kit.journal.rolled(marker, undo)
    except Exception:
        pass
    return undo


def stop():
    """Stop listening and fail every queued job. True if it was running."""
    from . import protocol
    kit = _state().get("kit")
    if kit is None:
        return False
    _state()["kit"] = None
    # An open action is committed as it stands.
    try:
        if kit.action is not None:
            kit.action.stop()
    except Exception:
        pass
    try:
        if kit.journal is not None:
            kit.journal.end("stop")
    except Exception:
        pass
    try:
        if kit.listen is not None:
            kit.listen.stop()
    except Exception:
        pass
    # The pump first, so every waiting caller gets its answer before the
    # listener closes the connections.
    try:
        kit.pump.stop(protocol.make_error(
            None, protocol.STOPPED, "the kit stopped before the job ran",
            hint="Run modoTools.start in Modo, then call again."))
    finally:
        kit.listener.stop()
    return True


def status():
    kit = running()
    if kit is None:
        return {"running": False}
    return {
        "running": True,
        "port": kit.port,
        "pump": kit.pump.strategy_name,
        "queue": kit.pump.queue.pending(),
        "jobs_run": kit.pump.jobs_run,
        "last_op": kit.pump.last_op,
        "last_session": kit.last_session,
    }


def pump_once(max_jobs=1):
    """Run queued jobs. Called from modoTools.exec, .execRead and .pump, so
    from inside a Modo command, where geometry edits are allowed."""
    kit = running()
    if kit is None:
        return 0
    return kit.pump.run_pending(max_jobs=max_jobs)
