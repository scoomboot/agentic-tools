"""Commands that have crashed or frozen Modo 17.1, checked by the server
before anything is sent.

Two lists:

* Crash. The first call is refused: "save the scene, then call again with
  acknowledge_risk=true". A crash loses all unsaved work, so one extra round
  trip is cheaper.
* Lock. On 17.1 these wait for scene evaluation while Python holds the GIL;
  with Python mesh ops in the scene (the QK Cable Kit) that can freeze Modo
  for good. It is a race and usually fine, so these run, with a note.
  scene.open was here until it froze Modo and opened a dialog in our own
  checks; now it is on the crash list.

Entries are what modo-mcp 0.3.0 observed on 17.1 (its risky.py); the code
and the wording are ours. Add an entry only for something seen to happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Crash:
    command: str
    seen: str
    advice: str


CRASH = (
    Crash("deformer.freeze",
          "has crashed Modo 17.1 when run from a script; it doesn't fail every "
          "time and the trigger isn't known",
          "Save the scene (mt_save_scene), then run it alone, not inside a "
          "longer script. Leaving the deformer live avoids it."),
    Crash("mesh.psubSubdiv",
          "crashed Modo 17.1 twice in scripted scene builds; fine in a quiet "
          "scene",
          "Set the level in an edit scan instead: lx.object.Mesh("
          "scan.MeshEdit(0)).PSUBSetLevel(n). If the command is needed, save "
          "first and run it alone."),
    Crash("mesh.psubRenderSubdiv",
          "crashed Modo 17.1 in scripted scene builds, like mesh.psubSubdiv; "
          "querying it is fine",
          "PSUBSetLevel(n) in an edit scan sets display and render level "
          "together. Otherwise save first and run it alone."),
    Crash("scene.open",
          "froze Modo 17.1 from a script on 2026-11-08 (Not Responding, "
          "ended in Task Manager); and with a path it can't use, it opens a "
          "dialog asking for the file, which holds the kit until someone "
          "answers it",
          "Ask the user to open the scene from File > Open. If it has to be "
          "scripted, save first and send it alone, with a path that exists."),
)

LOCK = ("scene.new", "scene.close", "select.time",
        "viewport.fitSelected", "viewport.fit", "item.delete")


def _pattern(name: str) -> re.Pattern:
    # The whole name, not part of a longer one, and not a query (name ?).
    return re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])(?!\s*\?)")


_CRASH = tuple((c, _pattern(c.command)) for c in CRASH)
_LOCK = tuple((name, _pattern(name)) for name in LOCK)


# Refused while this server has an action open: the kit's guard
# refuses the same names (guard.ACTION_REFUSED); here they never leave.
ACTION_REFUSED = ("app.undo", "app.redo", "scene.new", "scene.open",
                  "scene.close", "scene.revert", "cmds.undoTo",
                  "app.clearUndos", "app.undoSuspend", "app.undoLockout")
_ACTION = tuple((name, _pattern(name)) for name in ACTION_REFUSED)


def in_action(text: str) -> list[str]:
    """The names of ACTION_REFUSED a command line or Python source uses."""
    if not text:
        return []
    return [name for name, p in _ACTION if p.search(text)]


@dataclass
class Found:
    refused: list[Crash] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)


def scan(text: str) -> Found:
    """Risky commands named in a command line or in Python source."""
    found = Found()
    if not text:
        return found
    found.refused = [c for c, p in _CRASH if p.search(text)]
    found.locks = [name for name, p in _LOCK if p.search(text)]
    return found


def refusal(found: Found) -> str:
    lines = ["risky: not sent to Modo. This uses a command that has crashed it:"]
    for crash in found.refused:
        lines.append(f"- {crash.command} {crash.seen}. {crash.advice}")
    lines.append("What to do: save the scene first (mt_save_scene), then call "
                 "again with acknowledge_risk=true.")
    return "\n".join(lines)


def lock_note(found: Found, in_python: bool) -> str:
    names = ", ".join(found.locks)
    note = (f"Note: {names} waits for Modo's scene evaluation. On 17.1, with "
            "Python mesh operations in the scene, that can freeze Modo for "
            "good. Keep such a command alone and last: its own mt_run_command "
            "call, after saving.")
    if in_python:
        note += " Here it ran inside mt_run_python; next time send it on its own."
    return note
