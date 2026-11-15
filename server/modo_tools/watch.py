"""The watch: is Modo gone, busy or stuck?

modo-mcp tells a hang apart with macOS's `sample`, which Windows doesn't have.
We ask the kit instead. When a call has had no answer for 10 s, the watch
pings on a second connection; the kit's socket thread answers by itself, from
what the main thread last wrote :

    connection refused                     unreachable
    our job, or another, is running        busy: keep waiting
    a job queued, no idle tick for 10 s    stuck: a dialog, most likely
    connection opens, two pings unanswered stuck_hard: Python isn't running

A dialog that opens inside a job holds Python in Modo too, so the pings go
silent and, once the kit's listen queue fills, connections are refused. When
that happens the watch looks at Modo's windows (window.py) before it says
stuck_hard or unreachable:

    a dialog is open, our job is running   answered: cancel it, keep waiting
    a dialog is open, no job of ours       dialog: left for the user to answer
    Modo's window is Not Responding        stuck_hard
    Modo's window responds                 stuck: a dialog, most likely
    no Modo window                         unreachable

A dialog is ours when the op log has a job started and not ended, in a
session of the Modo that is running now: the kit writes op_start before it
runs anything, and the file can be read while Python in Modo is held. A job
left open by a Modo that crashed is not running, so a dialog in the Modo
restarted after it is the user's. Every command already runs with !!, which turns a
failure's dialog into an error; this catches the dialogs !! doesn't, such as
the one a command opens to ask for an argument it couldn't parse
(scene.open with a bad path, in an early test). MODO_TOOLS_ANSWER_DIALOGS=0
turns the cancelling off.

False alarms are worse than none: telling the user to end Modo when it is only
busy throws work away. So only stuck_hard ends a call early; busy and stuck
are told to the agent with the answer, and stuck_hard needs two silent pings
and, where the windows can be seen, a main window that is Not Responding.

Also here: reading ~/.modo_tools/ops.log, which the server does directly so
it works while Modo is down.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from . import protocol, window

FIRST_S = 10.0   # no answer for this long: start pinging
EVERY_S = 10.0   # then ping again this often
ANSWER_S = 5.0   # a ping unanswered this long is silent; two silent ones in a row
STUCK_S = 10.0   # a job queued and no idle tick for this long is stuck
WINDOW_S = 1.0   # look for a dialog in our job this often


def answer_dialogs_default() -> bool:
    return os.environ.get("MODO_TOOLS_ANSWER_DIALOGS", "1").strip().lower() \
        not in ("0", "off", "no", "false")


@dataclass
class Seen:
    """One look at the kit. kind is unreachable, busy, stuck, stuck_hard,
    manual, waiting, dialog (open, not ours) or answered (ours, cancelled)."""
    kind: str
    text: str
    at_s: float = 0.0

    def note(self) -> str:
        return f"at {self.at_s:.0f} s, {self.kind}: {self.text}"


def classify(ping: dict[str, Any] | None, our_id: str | None,
             stuck_s: float = STUCK_S) -> Seen:
    """What a ping answer means. None is two pings that went unanswered."""
    if ping is None:
        return Seen("stuck_hard", "Modo took the connection, but Python in "
                    "Modo didn't answer two pings")
    job = ping.get("job")
    queued = ping.get("queued") or 0
    tick = ping.get("last_tick_s") or 0.0
    if job:
        if job.get("id") == our_id:
            return Seen("busy", f"{job.get('op')} has run {job.get('running_s')} s")
        return Seen("busy", f"Modo is running {job.get('op')} "
                    f"({job.get('running_s')} s so far); this call waits behind it")
    if queued and ping.get("pump") == "manual":
        return Seen("manual", "the pump is manual: jobs run when modoTools.pump "
                    "fires in Modo")
    if queued and tick >= stuck_s:
        return Seen("stuck", f"a job is queued and Modo's main thread hasn't "
                    f"been idle for {tick:.0f} s")
    return Seen("waiting", f"{queued} job(s) queued; the pump ticked {tick} s ago")


class Watch:
    def __init__(self, host: str = protocol.DEFAULT_HOST,
                 port: int = protocol.DEFAULT_PORT, first_s: float = FIRST_S,
                 every_s: float = EVERY_S, answer_s: float = ANSWER_S,
                 stuck_s: float = STUCK_S, window_s: float = WINDOW_S,
                 windows: Callable[[], window.ModoWindows | None] | None = None,
                 answer_dialogs: bool | None = None):
        self.host = host
        self.port = port
        self.first_s = first_s
        self.every_s = every_s
        self.answer_s = answer_s
        self.stuck_s = stuck_s
        self.window_s = window_s
        # Returns None when Modo's windows can't be seen (not Windows).
        self.windows = windows or (lambda: window.find())
        if answer_dialogs is None:
            answer_dialogs = answer_dialogs_default()
        self.answer_dialogs = answer_dialogs

    async def ping(self) -> dict[str, Any] | None:
        """One ping on a fresh connection: the kit's answer, None if it
        stayed silent for answer_s. Raises OSError when nothing listens."""
        from .bridge import read_token
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port,
                                        limit=protocol.MAX_LINE_BYTES),
                timeout=self.answer_s)

            async def ask() -> dict[str, Any]:
                for request in (
                        {"id": "w-hello", "op": "hello",
                         "payload": {"token": read_token() or "",
                                     "protocol": protocol.PROTOCOL_VERSION}},
                        {"id": "w-ping", "op": "ping", "payload": {}}):
                    writer.write(protocol.encode(request))
                    await writer.drain()
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionResetError("the kit closed the connection")
                    answer = protocol.decode(raw)
                    if not answer.get("ok"):
                        # A wrong token, or an older kit with no ping: it
                        # answered, so Python runs; we just can't see more.
                        return {}
                return answer.get("result") or {}

            return await asyncio.wait_for(ask(), timeout=self.answer_s)
        except asyncio.TimeoutError:
            return None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
                except Exception:
                    pass

    async def look(self, our_id: str | None) -> Seen:
        """Ping, and if that one goes unanswered, once more, answer_s later.
        When the kit can't answer, Modo's windows say why."""
        for _ in range(2):
            try:
                ping = await self.ping()
            except (OSError, ValueError):
                return self.without_kit(refused=True)
            if ping is not None:
                seen = classify(ping, our_id, self.stuck_s)
                if seen.kind in ("busy", "stuck"):
                    return self.dialog() or seen
                return seen
        return self.without_kit(refused=False)

    def without_kit(self, refused: bool) -> Seen:
        """The kit refused the connection, or took it and stayed silent."""
        shown = self.windows()
        if shown is None or (not shown.alive and not refused):
            if refused:
                return Seen("unreachable", "nothing is listening on "
                            f"{self.port}: Modo has closed or the kit stopped")
            return classify(None, None, self.stuck_s)
        if not shown.alive:
            return Seen("unreachable", "no Modo window is open: Modo has closed")
        found = self.dialog(shown)
        if found:
            return found
        if refused and not shown.hung and not job_running(shown):
            # No job of ours holds Python, so nothing listens: the kit is
            # stopped.
            return Seen("unreachable", f"Modo is running, but nothing is "
                        f"listening on {self.port}: the kit is stopped")
        if shown.hung:
            return Seen("stuck_hard", "Python in Modo didn't answer two pings, "
                        "and Modo's window is Not Responding")
        what = ("the kit didn't take the connection" if refused
                else "Python in Modo didn't answer two pings")
        return Seen("stuck", f"Modo is running and its window responds, but "
                    f"{what}: most likely a dialog is open in Modo, or it is busy")

    def dialog(self, shown: window.ModoWindows | None = None) -> Seen | None:
        """A dialog open in Modo: cancelled when it is our job's, else told."""
        if shown is None:
            shown = self.windows()
        if shown is None or not shown.dialogs:
            return None
        found = shown.dialogs[0]
        running = job_running(shown)
        if running and len(running) > 80:
            running = running[:77] + "..."
        if running and self.answer_dialogs and found.close():
            return Seen("answered", f"a dialog, '{found.title}', opened in Modo "
                        f"during {running}; modo-tools cancelled it")
        if running:
            return Seen("dialog", f"a dialog, '{found.title}', is open in Modo "
                        f"during {running}; MODO_TOOLS_ANSWER_DIALOGS is off")
        return Seen("dialog", f"a dialog, '{found.title}', is open in Modo, "
                    "and no job of modo-tools is running: it is the user's")

    async def run(self, our_id: str, seen: list[Seen]) -> Seen:
        """Beside a waiting call: cancel a dialog in our job as soon as it
        shows (every window_s), and look at the kit every every_s after
        first_s. Returns only on stuck_hard; the caller cancels it when the
        answer comes."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_look = self.first_s
        while True:
            left = next_look - (loop.time() - started)
            await asyncio.sleep(max(0.0, min(self.window_s, left)))
            now = loop.time() - started
            if now < next_look:
                if self.answer_dialogs and job_running():
                    found = self.dialog()
                    if found is not None and found.kind == "answered":
                        found.at_s = now
                        seen.append(found)
                continue
            found = await self.look(our_id)
            found.at_s = loop.time() - started
            seen.append(found)
            if found.kind == "stuck_hard":
                return found
            next_look = found.at_s + self.every_s


# -- the op log ---------------------------------------------------------------

def _lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readlines()
    except OSError:
        return []


def job_running(shown: window.ModoWindows | None = None) -> str | None:
    """The kit's job in progress, from the op log ("run_command: scene.open
    ..."), else None. While Modo runs, a job Modo would have stopped in is
    the one running now, unless that session was another Modo process: one
    that crashed in the job, with Modo restarted since and the kit not yet
    started. shown's pids tell, when the windows can be seen."""
    lines = _lines(protocol.oplog_path())
    stopped = _stop(lines)
    if not stopped:
        return None
    pid = session_pid(lines)
    if shown is not None and shown.pids and pid is not None \
            and pid not in shown.pids:
        return None
    return stopped[len("Modo stopped during "):]


def last_stop() -> str | None:
    """'Modo stopped during …' when the last session in the op log has a job
    with no ending, else None."""
    return _stop(_lines(protocol.oplog_path()))


def _stop(lines: list[str]) -> str | None:
    ending = protocol.oplog_ending(lines)
    return ending if ending.startswith("Modo stopped") else None


def session_pid(lines: list[str]) -> int | None:
    """The Modo process the op log's last session ran in, if it says."""
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("event") == "session":
            pid = entry.get("pid")
            return pid if isinstance(pid, int) else None
    return None


def read_oplog(count: int) -> list[str] | None:
    """The last count lines of the op log, oldest first, reaching into
    ops.log.1 when ops.log has fewer. None when there is no log at all."""
    path = protocol.oplog_path()
    if not os.path.exists(path) and not os.path.exists(path + ".1"):
        return None
    lines = [line.rstrip("\n") for line in _lines(path) if line.strip()]
    if len(lines) < count:
        older = [line.rstrip("\n") for line in _lines(path + ".1") if line.strip()]
        lines = older + lines
    return lines[-count:] if count > 0 else []


def format_seen(seen: list[Seen], ok: bool) -> str:
    """What the watch saw while the call waited, for the end of the answer."""
    shown = [s for s in seen if s.kind != "waiting"]
    if not shown:
        return ""
    # A dialog left open for minutes is one line a minute: keep each run of
    # the same kind to its first and last look.
    lines: list[str] = []
    run: list[Seen] = []
    for s in shown + [Seen("", "")]:
        if run and s.kind != run[0].kind:
            lines.append("  " + run[0].note())
            if len(run) > 2:
                lines.append(f"  ... ({len(run) - 2} more like it)")
            if len(run) > 1:
                lines.append("  " + run[-1].note())
            run = []
        run.append(s)
    out = "\n\nWhile waiting:\n" + "\n".join(lines)
    if ok and any(s.kind in ("stuck", "manual", "dialog", "answered")
                  for s in shown):
        out += "\n  then it ran."
    return out

