# python
# Copyright (c) 2026 Leonard Billeke
# Copyright (c) 2026 modo-tools contributors
#
# Adapted from modo-mcp 0.3.0, kit/mcp_bridge/Scripts/python_modules/
# mcpbridge/protocol.py, under the MIT License:
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
"""The message format between the kit (inside Modo) and the server.

This file lives in the kit. The server keeps a byte-for-byte copy at
server/modo_tools/protocol.py, and a test fails if the two differ. Edit the
kit's copy, then copy it over.

The kit runs on Modo's own Python, so this is standard library only.

One UTF-8 JSON object per line. A request:
    {"id": "r-12", "op": "status", "payload": {}, "timeout_ms": 30000}
An answer:
    {"id", "ok": true, "result", "stdout", "ms"}
    {"id", "ok": false, "error": {"type", "message", "traceback",
                                  "event_log", "hint"}, "stdout", "ms"}
A job that may change the scene adds "undo" to either answer: {"step": 1},
or null for run_python with read_only. A failed one is rolled back: {"rolled_back": true}; or {"step": 1, "rolled_back": false,
"presses": 1} when the user has to press Ctrl+Z; or {"step": 1, "rolled_back":
"unsure"} when the undo didn't reverse the job's marker and was redone.
Inside an action a changing answer's "undo" is {"action", "step"},
with "undone_by_user": [n, ...] when the user undid steps since the last call; a
failed one rolls back the whole action: {"action", "rolled_back": n, "left":
k}; one refused before it changed anything (REFUSALS) leaves it
open: {"action", "refused": true, "steps": n}, with "kept": true when
its own step could not be undone and stays in the action.
An answer after the kit closed an action on its own carries
"action_closed": {"action", "reason", "steps", "presses"}.
"""

import json
import os

PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15680
DEFAULT_TIMEOUT_MS = 30000

# A line longer than this is refused without parsing it.
MAX_LINE_BYTES = 8 * 1024 * 1024

# Error types. The server and the agent see these names.
BAD_TOKEN = "bad_token"
PROTOCOL_MISMATCH = "protocol_mismatch"
UNKNOWN_OP = "unknown_op"
TIMEOUT = "timeout"
STILL_RUNNING = "still_running"
UNREACHABLE = "unreachable"
JOB_EXCEPTION = "job_exception"
BAD_REQUEST = "bad_request"
STOPPED = "stopped"
# Running and looking up.
COMMAND_FAILED = "command_failed"
PYTHON_ERROR = "python_error"
UNKNOWN_COMMAND = "unknown_command"
RISKY = "risky"
NO_SUCH_ITEM = "no_such_item"
BAD_CHANNEL = "bad_channel"
PYTHON_DISABLED = "python_disabled"
# Reading the scene.
AMBIGUOUS_ITEM = "ambiguous_item"
NO_VIEWPORT = "no_viewport"
# Crashes. The server's watch decides these, never the kit.
BUSY = "busy"
STUCK = "stuck"
STUCK_HARD = "stuck_hard"
# Mesh facts.
NOT_A_MESH = "not_a_mesh"
# The selection.
TOO_LARGE = "too_large"
# Actions.
IN_ACTION = "in_action"
ACTION_UNDONE = "action_undone"
# The docs search.
NOT_FOUND = "not_found"

# Error types of a call refused before it changed anything (the
# journal's list). set_channels checks every channel and the item
# before it writes any ("Nothing was written"), so its bad_channel,
# no_such_item and ambiguous_item are refusals too. One tuple, for the
# journal (no entry) and the action (it stays open and whole).
# set_points checks its item is a mesh before it writes, so
# its not_a_mesh is one too.
REFUSALS = (BAD_REQUEST, UNKNOWN_COMMAND, IN_ACTION, ACTION_UNDONE,
            BAD_CHANNEL, NO_SUCH_ITEM, AMBIGUOUS_ITEM, NOT_A_MESH)


def data_dir():
    """~/.modo_tools, or MODO_TOOLS_HOME when set (the tests use that)."""
    return (os.environ.get("MODO_TOOLS_HOME")
            or os.path.join(os.path.expanduser("~"), ".modo_tools"))


def token_path():
    return os.path.join(data_dir(), "token")


def oplog_path():
    return os.path.join(data_dir(), "ops.log")


def journals_dir():
    """The journals: one JSONL file per scene."""
    return os.path.join(data_dir(), "journals")


def oplog_ending(lines):
    """How the last session in op log lines ended: "first session", "ended
    cleanly", or "Modo stopped during <op>: <what it was asked>". Here, not in
    oplog.py, because the server reads the same file while Modo is down."""
    open_ops = {}
    started = {}            # id -> op_start, to name a rollback
    rolling = None          # a rollback with no rollback_done yet
    action = None           # [label, step]: an action not committed or rolled back
    seen_session = False
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if event == "session":
            seen_session = True
            # A session continued after rotation keeps its open ops.
            if not entry.get("continued"):
                open_ops = {}
                rolling = None
                action = None
        elif event == "op_start":
            open_ops[entry.get("id")] = entry
            started = {entry.get("id"): entry}
            if action is not None and entry.get("step"):
                action[1] = entry["step"]
        elif event == "action_begin":
            action = [entry.get("label"), 0]
        elif event in ("action_commit", "action_rollback"):
            action = None
        elif event in ("op_ok", "op_error"):
            open_ops.pop(entry.get("id"), None)
        elif event == "rollback":
            rolling = started.get(entry.get("id")) or entry
        elif event == "rollback_done":
            rolling = None
    if not seen_session:
        return "first session"
    # An action still open names itself and its last step.
    during = ("" if action is None
              else " (in action '%s', step %d)" % (action[0], action[1]))
    if rolling is not None:
        # The pump's app.undo of a failed job, after its op_error.
        return _stopped("the rollback of ", rolling) + during
    if not open_ops:
        if action is not None:
            return "Modo stopped during action '%s' (step %d)" % tuple(action)
        return "ended cleanly"
    return _stopped("", list(open_ops.values())[-1]) + during


def _stopped(during, last):
    op = str(last.get("op") or "?")
    what = str(last.get("preview") or "")
    # Older kits wrote the op's name in front of the preview.
    if what == op or what.startswith(op + " "):
        what = what[len(op):].strip()
    if not what:
        return "Modo stopped during %s%s" % (during, op)
    return "Modo stopped during %s%s: %s" % (during, op, what)


def encode(obj):
    """One message as one line of bytes."""
    text = json.dumps(obj, ensure_ascii=False, default=_fallback)
    return (text + "\n").encode("utf-8")


def _fallback(value):
    # An odd Modo object must never break an answer.
    try:
        return str(value)
    except Exception:
        return "<unserialisable>"


def decode(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def make_request(req_id, op, payload=None, timeout_ms=None):
    req = {"id": req_id, "op": op, "payload": payload or {}}
    if timeout_ms is not None:
        req["timeout_ms"] = timeout_ms
    return req


def make_response(req_id, result=None, stdout="", ms=0):
    return {
        "id": req_id,
        "ok": True,
        "result": result if result is not None else {},
        "stdout": stdout,
        "ms": ms,
    }


def make_error(req_id, err_type, message, traceback_text="", event_log=None,
               hint="", stdout="", ms=0):
    return {
        "id": req_id,
        "ok": False,
        "error": {
            "type": err_type,
            "message": message,
            "traceback": traceback_text,
            "event_log": event_log or [],
            "hint": hint,
        },
        "stdout": stdout,
        "ms": ms,
    }


class LineReader(object):
    """Splits a byte stream into lines. Refuses a line over max_line_bytes."""

    def __init__(self, max_line_bytes=MAX_LINE_BYTES):
        self._buf = b""
        self._max = max_line_bytes

    def feed(self, chunk):
        """Add received bytes; return the complete lines (bytes, no newline)."""
        self._buf += chunk
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            if len(line) > self._max:
                self._buf = b""
                raise ValueError("a line was over %d bytes" % self._max)
            if line.strip():
                lines.append(line)
        if len(self._buf) > self._max:
            self._buf = b""
            raise ValueError("a line was over %d bytes" % self._max)
        return lines
