# python
"""The op log: ~/.modo_tools/ops.log.

A hard Modo crash takes the socket and the event log with it. This file
survives: run_job writes op_start and forces it to disk before the job runs,
then op_ok or op_error after. A failed job the pump rolls back adds rollback
before its app.undo, and rollback_done after. An action writes
action_begin, then action_commit or action_rollback, and each changing
op_start inside it names the action and its step. One JSON object per line,
rotated at 1 MB.

It is a crash record, not the journal. The idea is modo-mcp's
forensics.py; the code is ours.

Writing never raises into a job: the log must not cause the failure it is
there to record.
"""

import json
import os
import time

from . import protocol

MAX_BYTES = 1024 * 1024
PREVIEW_CHARS = 300


class OpLog(object):
    def __init__(self, path):
        self.path = path

    # -- sessions ------------------------------------------------------------

    def open_session(self):
        """Read how the last session ended, then start a new one.

        Returns "first session", "ended cleanly", or
        "Modo stopped during <op>: <preview>".
        """
        last = self.last_session()
        try:
            directory = os.path.dirname(self.path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            self._rotate()
        except Exception:
            pass
        self._write({"event": "session", "pid": os.getpid()})
        return last

    def last_session(self):
        """How the most recent session in the file ended."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except (OSError, IOError):
            return "first session"
        return protocol.oplog_ending(lines)

    # -- ops -----------------------------------------------------------------

    def op_start(self, op, req_id, preview="", action=None, step=None):
        # Rotate between jobs only, when no op is open, and mark the new file
        # as the same session so last_session() still reads it right.
        if self._rotate():
            self._write({"event": "session", "pid": os.getpid(),
                         "continued": True})
        record = {"event": "op_start", "op": op, "id": req_id,
                  "preview": (preview or "")[:PREVIEW_CHARS]}
        if action is not None:
            # A changing job inside an action, and the step it
            # will be, so a crash names both.
            record.update(action=action, step=step)
        self._write(record)

    def op_ok(self, op, req_id, ms):
        self._write({"event": "op_ok", "op": op, "id": req_id, "ms": ms})

    def op_error(self, op, req_id, err_type, message, ms):
        self._write({"event": "op_error", "op": op, "id": req_id,
                     "type": err_type, "message": (message or "")[:PREVIEW_CHARS],
                     "ms": ms})

    def rollback(self, op, req_id):
        """Before the pump's app.undo for a failed job: if Modo
        dies in the undo, this is the last line."""
        self._write({"event": "rollback", "op": op, "id": req_id})

    def rollback_done(self, op, req_id, rolled_back):
        self._write({"event": "rollback_done", "op": op, "id": req_id,
                     "rolled_back": rolled_back})

    # -- actions ---------------------------------------------------

    def action_begin(self, label):
        self._write({"event": "action_begin", "label": label})

    def action_commit(self, label, steps, presses, reason):
        """reason: "commit", "idle" or "stop"."""
        self._write({"event": "action_commit", "label": label, "steps": steps,
                     "presses": presses, "reason": reason})

    def action_rollback(self, label, steps):
        """Before the first app.undo of the action's rollback."""
        self._write({"event": "action_rollback", "label": label,
                     "steps": steps})

    # -- the file ------------------------------------------------------------

    def _rotate(self):
        try:
            if os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
                return True
        except OSError:
            pass
        return False

    def _write(self, record):
        """Append one line and force it to disk: without the fsync, a crash
        loses exactly the line that says what crashed."""
        try:
            line = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"))
            line.update(record)
            text = json.dumps(line, default=str) + "\n"
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            pass


def preview(op, payload):
    """What a job was asked to do, short, for the log: the code or the
    command line when there is one (its lines joined by " | "), else the
    payload as JSON."""
    payload = payload or {}
    text = payload.get("code") or payload.get("command")
    if isinstance(text, str):
        text = " | ".join(line.strip() for line in text.splitlines()
                          if line.strip())
    else:
        try:
            text = json.dumps(payload, default=str, sort_keys=True)
        except Exception:
            text = ""
        if text == "{}":
            text = ""
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS - 3] + "..."
