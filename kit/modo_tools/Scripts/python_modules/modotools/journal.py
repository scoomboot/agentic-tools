# python
"""The journal: ~/.modo_tools/journals/<start>-<scene>.jsonl.

Every call that may change the scene is one entry, written by run_job's
after_job and forced to disk before the answer goes out: the call as asked
(all of its code), the command lines the guard saw it send, and the modo
writes it couldn't see as commands. One file per scene; the server reads the
files for mt_journal. Two files with the op log, joined by the call's id
(Q5, 2026-11-08): the op log is the crash record, this is what the agent
changed.

How the kit tells one scene from another (Modo 17.1, 2026-11-08): the
scene service's name for the current scene, which Modo keeps unique among
open scenes ("Untitled", "Untitled (2)", "cube.lxo"), and its file. A save
gives the scene its file's name; when the agent's own call saved, the
journal goes on and writes a save record.

Main thread only: every record is written inside a job or the idle visit
that rolled one back. Writing never raises into a job: a write that fails is
the answer's "journal": {"error": ...} and mt_status's, and the next call
tries again.
"""

import json
import os
import re
import time

from . import protocol

FORMAT = 1
KEEP = 100

# Only files the kit named are ever deleted.
NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-[a-z0-9-]+(-\d+)?\.jsonl$")

# Text in a call's code that reaches Modo where the guard can't see it.
_SDK = (("lx.object", re.compile(r"\blx\.object\b")),
        ("lx.service", re.compile(r"\blx\.service\b")),
        ("lxu", re.compile(r"\blxu\b")))

# A line in the trace that saves the scene, so the agent's own call gave it
# a new name.
_SAVES = re.compile(r"^[!+]*\s*scene\.(save|saveAs)\b")

# Error types of a call that was refused before it ran: no entry
# (protocol.REFUSALS, shared with the action).
REFUSALS = protocol.REFUSALS


def folder():
    return protocol.journals_dir()


def slug(name):
    base = re.sub(r"\.lx[oa]$", "", str(name or "untitled"), flags=re.I)
    text = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return text[:40].strip("-") or "scene"


def same_file(path):
    """A scene file as one key: a probe found 17.1 gives a saved file with / and
    the same file opened with backslashes. Windows paths ignore case."""
    if not path:
        return None
    # Not os.path.normcase: on the test machines (Linux) it changes nothing.
    return str(path).replace("\\", "/").lower()


def key(scene):
    return (scene["name"], same_file(scene.get("file")))


def untraced(tool, payload, seen):
    """The writes an entry's trace can't hold: the modo writes the guard
    saw, then the SDK modules the code names. Only run_python's: the other
    tools' own modo writes (set_channels' Channel.set) are their arguments,
    and the entry holds those."""
    if tool != "run_python":
        return []
    out = list(seen)
    code = payload.get("code")
    if isinstance(code, str):
        out += [name for name, pattern in _SDK if pattern.search(code)]
    return out


def scene_now():
    """The current scene as a probe found 17.1 tells it, or None when it can't
    be read. Called outside the guard: its queries carry their own !!."""
    try:
        import lx

        def ask(attribute):
            return lx.eval("!!query sceneservice %s ? current" % attribute)
        name = ask("scene.name")
        if not name:
            return None
        changed = ask("scene.changed")
        return {"name": str(name), "file": ask("scene.file") or None,
                "unsaved": bool(changed)}
    except Exception:
        return None


def item_name(ident):
    """An item's name now, or None when it is gone."""
    try:
        import modo
        return modo.Scene().item(ident).name
    except Exception:
        return None


def _scene_facts():
    from . import ops_mesh
    return ops_mesh.scene_facts()


def prune(where=None, keep=KEEP):
    """At kit start: delete all but the `keep` newest files the kit named."""
    where = where or folder()
    try:
        names = sorted(n for n in os.listdir(where) if NAME.match(n))
    except OSError:
        return []
    gone = []
    for name in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(where, name))
            gone.append(name)
        except OSError:
            pass
    return gone


class Journal(object):
    """The current scene's journal. Kit.journal."""

    # Lines forced to disk, all journals: the tests count them.
    synced = 0

    def __init__(self, info=None, scene=scene_now, where=None, facts=None,
                 log=None):
        self.info = info or {}
        self.where = where or folder()
        self.scene = scene      # () -> {"name", "file", "unsaved"} or None
        self.read_facts = facts or _scene_facts  # () -> (meshes, why)
        self._start = (None, None)  # the start facts, for a new file's header
        self.key = None         # (name, file) the open file is for
        self.name = None        # the open file's name, without .jsonl
        self.entries = 0
        self.error = None       # the last write's error, until one succeeds
        self.note = None        # a scene the kit couldn't read
        self._header = None     # written with the first entry
        # Marker step -> (file, entry n), or None for a step with
        # no entry (a refused call). Kept across files: the user's Ctrl+Z in a
        # scene the journal left is written to that scene's file.
        self._markers = {}
        self._unknown = set()   # marker steps already logged as unknown
        self._pending = []      # the open action's begin, until its first entry
        self._action_files = [] # the files holding the open action's entries
        self._log = log or (lambda msg: None)
        self.listen = None      # listen.Listen: the user's edits
        self.item_name = item_name

    # -- what run_job calls ----------------------------------------------------

    def before(self):
        """Before a changing job: the scene it starts in. When the job will
        open a new file, the scene's facts as they are now, for its header: the replay checks its start against them."""
        scene = self.scene()
        self._start = (None, None)
        if self.name is None or (scene is not None and key(scene) != self.key):
            self._start = self._take_facts()
        return scene

    def facts(self):
        """journal_facts: a facts record after the last entry."""
        if self.name is None:
            return {"facts": None, "note": "no journal is open: the first "
                    "call that changes this scene starts one"}
        scene = self.scene()
        if scene is None:
            return {"facts": None, "journal": self.name,
                    "note": "the scene couldn't be read"}
        if key(scene) != self.key:
            return {"facts": None, "journal": self.name,
                    "note": "this is another scene than the journal's (%s); "
                    "its journal starts with its first change" % self.key[0]}
        meshes, why = self._take_facts()
        if meshes is None:
            return {"facts": None, "journal": self.name, "note": why}
        if not self._write_header() or not self._write_gap() or not self._write(
                {"event": "facts", "after": self.entries, "meshes": meshes}):
            return {"facts": None, "journal": self.name, "error": self.error}
        return {"facts": self.entries, "meshes": len(meshes),
                "journal": self.name}

    def _take_facts(self):
        try:
            return self.read_facts()
        except Exception as exc:
            return None, "the facts couldn't be read: %s: %s" % (
                type(exc).__name__, exc)

    def entry(self, request, response, scene, guard, marker=None):
        """after_job: write the call's entry. Returns the answer's
        "journal": n, {"error": ...}, or None for a call that was refused
        before it ran. marker: the job's undo marker step, so the user's Ctrl+Z
        on it is written against this entry."""
        err = response.get("error") or {}
        if not response.get("ok") and err.get("type") in REFUSALS:
            self._remember(marker, None)
            return None
        payload = request.get("payload") or {}
        tool = request.get("op")
        self._follow(scene)
        if self._header is not None and self.listen is not None:
            # A new file: what the user did before it is in its header's facts.
            self.listen.take()
        record = {"event": "entry", "n": self.entries + 1,
                  "id": request.get("id"), "tool": tool,
                  "purpose": payload.get("purpose"),
                  "args": payload,
                  "commands": list(guard.commands),
                  "untraced": untraced(tool, payload, guard.untraced),
                  "ok": bool(response.get("ok")),
                  "undo": response.get("undo")}
        failed = list(getattr(guard, "failed", []))
        if failed:
            record["failed"] = failed
        # Commands hold the values operators set, read back; a
        # line that couldn't be read back stays as sent, and the entry isn't
        # exact.
        operators = list(getattr(guard, "operators", []))
        if operators:
            record["operators"] = operators
        record["exact"] = all(o["read"] for o in operators)
        result = response.get("result")
        if tool == "set_channels" and isinstance(result, dict):
            # The values it set, read back.
            record["set"] = result.get("channels")
        if tool == "set_points" and isinstance(result, dict):
            # The values are the entry's args; the item by the
            # id it resolved to, as set_channels' entries name it.
            record["points_item"] = result.get("item")
        if tool == "fit_backdrops" and isinstance(result, dict):
            # The points the fit wrote, in the mesh's own space,
            # so the replay sets them as values; taken out of the answer.
            record["points_item"] = result.get("item")
            record["points"] = result.pop("_written", None)
        if tool == "backdrop" and isinstance(result, dict) and result.get("link"):
            # The backdrop's link to its clip, which no command
            # makes on 17.1, so the replay makes it as the op did.
            record["link"] = result["link"]
            # the file the clip shows, so the replay finds
            # a clip the scene already had.
            record["clip_file"] = result.get("file")
        undo = response.get("undo")
        if isinstance(undo, dict) and "action" in undo:
            record["action"] = undo["action"]
        if not response.get("ok"):
            record["error"] = {"type": err.get("type"),
                               "message": err.get("message")}
        if not self._write_header() or not self._write_gap() or (
                "action" in record and not self._write_pending()):
            return {"error": self.error}
        if not self._write(record):
            return {"error": self.error}
        if "action" in record and self.name not in self._action_files:
            self._action_files.append(self.name)
        self.entries += 1
        self._remember(marker, (self.name, self.entries))
        if self._saved(tool, guard.commands):
            after = self.scene()
            # A save that failed left the scene as it was: no record. One
            # that worked before the call failed renamed it: the record, so
            # the journal goes on in this file.
            if after is not None and (response.get("ok")
                                      or key(after) != self.key):
                self._write({"event": "save", "path": after.get("file")})
                self.key = key(after)
        return self.entries

    def rolled(self, marker, undo):
        """The pump, after a failed call's rollback: rolled_back when
        its step was undone, unsure when that couldn't be told. By the
        call's marker step, which the kit counts, not by its id: every
        server counts its ids from r-1, so a second session's refused call
        marked the first one's entry."""
        where = self._markers.get(marker)
        if where is None or not isinstance(undo, dict) or "action" in undo:
            # No entry (a refused call), or inside an action, where the
            # loop wrote each step it undid.
            return
        name, n = where
        rolled = undo.get("rolled_back")
        if rolled == "unsure":
            self._write({"event": "unsure", "n": n}, name)
        elif rolled is True:
            self._write({"event": "rolled_back", "n": n}, name)

    # -- what happened to the steps -----------------------------

    def _remember(self, marker, where):
        if marker is None:
            return
        self._markers[marker] = where
        if len(self._markers) > 1000:
            self._markers.pop(next(iter(self._markers)))

    def marker(self, step, state):
        """A marker's Reverse or Forward that wasn't the kit's own undo:
        The user's Ctrl+Z or Ctrl+Shift+Z. Written as undone or redone against
        the step's entry, in the file that holds it. Called from inside
        Modo's undo: never raises."""
        try:
            if step not in self._markers:
                if step not in self._unknown:
                    self._unknown.add(step)
                    self._log("modo-tools: journal: undo marker %s has no "
                              "entry; nothing written" % step)
                return
            where = self._markers[step]
            if where is None:
                return
            name, n = where
            self._write({"event": "undone" if state == "reversed"
                         else "redone", "n": n}, name)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def entry_of(self, step):
        """The entry n a marker's step was written as, or None (a refused
        call, a write that failed, or a step from before the kit started)."""
        where = self._markers.get(step)
        return where[1] if where else None

    def agent_undone(self, step):
        """Undo_last undid this marker's step. Written as undone,
        by the agent, so the replay leaves it out and the user's Ctrl+Shift+Z
        (redone) puts it back. Never raises."""
        try:
            where = self._markers.get(step)
            if where is not None:
                self._write({"event": "undone", "n": where[1], "by": "agent"},
                            where[0])
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def rolled_step(self, step):
        """The action loop undid this marker's step: rolled_back. Never
        raises."""
        try:
            where = self._markers.get(step)
            if where is not None:
                self._write({"event": "rolled_back", "n": where[1]}, where[0])
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def action(self, event, **fields):
        """action_begin, action_commit, action_rollback, as the op log has
        them. A begin is held for the action's first entry and written into
        that entry's file; a commit or rollback goes to every file holding
        the action's entries, since the user can move to another scene mid-action. An action with no entry writes nothing."""
        record = dict(event=event, **fields)
        if event == "action_begin":
            self._pending = [record]
            self._action_files = []
            return
        self._pending = []
        for name in self._action_files:
            self._write(record, name)
        self._action_files = []

    def end(self, why):
        """Close the open file with an end record."""
        if self.name is not None and self._header is None:
            self._write({"event": "end", "why": why})
        self.key = self.name = None
        self.entries = 0
        self._header = None

    def status(self):
        return {"name": self.name, "entries": self.entries,
                "error": self.error, "note": self.note}

    # -- one file per scene ----------------------------------------------------

    def _follow(self, scene):
        """Open a new file when the call starts in another scene."""
        if scene is None:
            # A probe failure: stay with the open file, and say so.
            self.note = ("the scene couldn't be read before this call, so "
                         "it was written to the open journal")
            if self.name is not None:
                return
            scene = {"name": "untitled", "file": None, "unsaved": None}
        else:
            self.note = None
        if key(scene) == self.key and self.name is not None:
            return
        if self.name is not None:
            self.end("another scene")
        self.key = key(scene)
        self.name = self._new_name(scene["name"])
        meshes, why = self._start
        self._header = {"event": "header", "journal": FORMAT,
                        "kit": self.info.get("kit"),
                        "modo": self.info.get("modo"),
                        "scene": {"name": scene["name"],
                                  "file": scene.get("file"),
                                  "unsaved": scene.get("unsaved")},
                        "facts": meshes}
        if why:
            self._header["facts_note"] = why

    def _new_name(self, scene_name):
        base = "%s-%s" % (time.strftime("%Y-%m-%d_%H%M"), slug(scene_name))
        name, i = base, 1
        while os.path.exists(os.path.join(self.where, name + ".jsonl")):
            i += 1
            name = "%s-%d" % (base, i)
        return name

    @staticmethod
    def _saved(tool, commands):
        return tool == "save_scene" or any(_SAVES.match(c) for c in commands)

    # -- the file --------------------------------------------------------------

    def _write_header(self):
        if self._header is None:
            return True
        if not self._write(self._header):
            return False
        self._header = None
        return True

    def _write_gap(self):
        """What the listener heard outside the agent since the
        last entry, as a gap record before the next one."""
        heard = self.listen.take() if self.listen is not None else None
        if heard is None:
            return True
        heard["items"] = [{"id": i, "name": self.item_name(i)}
                          for i in heard["items"]]
        return self._write(dict(event="gap", before=self.entries + 1, **heard))

    def _write_pending(self):
        while self._pending:
            if not self._write(self._pending[0]):
                return False
            self._pending.pop(0)
        return True

    def _write(self, record, name=None):
        """Append one line to the open file, or to `name`'s, and force it to
        disk, as the op log does. False, with self.error set, when it
        couldn't be."""
        try:
            line = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"))
            line.update(record)
            text = json.dumps(line, default=str) + "\n"
            if not os.path.isdir(self.where):
                os.makedirs(self.where)
            with open(os.path.join(self.where, (name or self.name) + ".jsonl"), "a",
                      encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            Journal.synced += 1
            self.error = None
            return True
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return False
