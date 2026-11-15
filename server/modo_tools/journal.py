"""The journal, read back.

The kit writes ~/.modo_tools/journals/<name>.jsonl, one per scene; this
reads the files in the server's own process, as watch.read_oplog reads the
op log, so mt_journal works with Modo closed. A line the kit is writing at
that moment is half there: it is skipped.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from . import protocol

NEWEST = 5


def names() -> list[str]:
    """Every journal's name, newest first (names begin with their start)."""
    try:
        found = [n[:-len(".jsonl")] for n in os.listdir(protocol.journals_dir())
                 if n.endswith(".jsonl")]
    except OSError:
        return []
    return sorted(found, reverse=True)


def path(name: str) -> str:
    return os.path.join(protocol.journals_dir(), name + ".jsonl")


def read(name: str) -> list[dict[str, Any]] | None:
    """The file's records, in order; None when there is no such journal."""
    try:
        with open(path(name), "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (OSError, IOError):
        return None
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


# What happened to an entry's step after it was written: the last of
# these records for its n wins. A step the kit rolled back that the user then
# redid is in the scene again.
_LATER = {"rolled_back": "out", "unsure": "unsure", "undone": "undone",
          "redone": None}


class Action:
    """An action as the journal saw it: its entries, and how it ended."""

    def __init__(self, k: int, label: str):
        self.k = k
        self.label = label
        self.ns: list[Any] = []
        self.end: str | None = None     # "committed", "rolled back", or None

    def outcome(self, open_action: str | None) -> str:
        """committed, rolled back, open (the kit says so), or open at the
        crash (no commit, and the kit doesn't hold it open)."""
        if self.end:
            return self.end
        return "open" if open_action == self.label else "open at the crash"


def actions(records: list[dict[str, Any]]) -> tuple[list[Action], dict[Any, Action]]:
    """The journal's actions in order, and each action entry's. Actions
    don't nest, so an entry belongs to the one open when it was written; an
    entry with an action and none open began in an earlier file."""
    found: list[Action] = []
    of: dict[Any, Action] = {}
    current: Action | None = None
    for record in records:
        event = record.get("event")
        if event == "action_begin":
            current = Action(len(found) + 1, str(record.get("label") or ""))
            found.append(current)
        elif event == "entry" and record.get("action"):
            if current is None or current.end is not None:
                current = Action(len(found) + 1, str(record["action"]))
                found.append(current)
            current.ns.append(record.get("n"))
            of[record.get("n")] = current
        elif event in ("action_commit", "action_rollback") and current is not None:
            if current.end is None:
                current.end = ("committed" if event == "action_commit"
                               else "rolled back")
    return found, of


def _plan(records: list[dict[str, Any]]) -> dict[Any, str]:
    """Each entry's state for the replay: in, out (rolled back), undone (by
    the user), open (in an action not committed), unsure, failed (its trace
    only), not (a save)."""
    later: dict[Any, str | None] = {}
    for record in records:
        if record.get("event") in _LATER:
            state = _LATER[record["event"]]
            if state == "undone" and record.get("by") == "agent":
                state = "agent"         # mt_undo_last
            later[record.get("n")] = state
    _, of = actions(records)
    plan = {}
    for record in records:
        if record.get("event") != "entry":
            continue
        n = record.get("n")
        action = of.get(n)
        if record.get("tool") == "save_scene":
            plan[n] = "not"
        elif later.get(n):
            plan[n] = later[n]
        elif action is not None and action.end is None:
            plan[n] = "open"
        elif not record.get("ok"):
            plan[n] = "failed"
        else:
            plan[n] = "in"
    return plan


_STATES = {"out": "rolled back", "unsure": "rollback unsure",
           "undone": "undone by the user", "agent": "undone by the agent",
           "failed": "failed, not rolled back",
           "in": "in the scene", "not": "in the scene"}


def entries(records: list[dict[str, Any]], open_action: str | None = None
            ) -> list[dict[str, Any]]:
    """Each entry with its state, from the records written after it, and
    its action's number."""
    plan = _plan(records)
    _, of = actions(records)
    out = []
    for record in records:
        if record.get("event") != "entry":
            continue
        n = record.get("n")
        action = of.get(n)
        if plan[n] == "open":
            state = "action " + action.outcome(open_action)
        else:
            state = _STATES[plan[n]]
        if state == "in the scene" and record.get("untraced"):
            state += ", untraced: " + ", ".join(record["untraced"])
        extra = {"state": state}
        if action is not None:
            extra["action_k"] = action.k
        out.append(dict(record, **extra))
    return out


def label(entry: dict[str, Any]) -> str:
    """What the entry did, in a few words."""
    args = entry.get("args") or {}
    tool = entry.get("tool")
    if tool == "run_command":
        words = str(args.get("command", "")).split()
        return words[0] if words else ""
    if tool == "run_python":
        return '"%s"' % (entry.get("purpose") or "")
    if tool in ("set_channels", "set_points", "fit_backdrops"):
        return str(args.get("item", ""))
    if tool == "save_scene":
        return str(args.get("path") or "its own file")
    if tool == "backdrop":
        return str(args.get("path") or "")
    return ""


def started(header: dict[str, Any] | None) -> str:
    scene = (header or {}).get("scene") or {}
    name = scene.get("name") or "an unknown scene"
    where = scene.get("file") or "not saved"
    text = "started in %s (%s)" % (name, where)
    if scene.get("unsaved"):
        text += " with unsaved changes"
    return text


# -- exactness ------------------------------------------------------

def _words(line: str) -> list[str]:
    """A command line's arguments after its name; a value in "..." or {...}
    is one word (the kit's guard.operator reads lines the same way)."""
    out, word, i, n = [], "", 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            if word:
                out.append(word)
            word = ""
            i += 1
        elif c == '"':
            close = line.find('"', i + 1)
            close = n if close < 0 else close + 1
            word += line[i:close]
            i = close
        elif c == "{":
            # Nested braces close together, as the kit reads them.
            depth, close = 0, i
            while close < n:
                depth += {"{": 1, "}": -1}.get(line[close], 0)
                close += 1
                if depth == 0:
                    break
            word += line[i:close]
            i = close
        else:
            word += c
            i += 1
    if word:
        out.append(word)
    return out[1:]


def operator_word(line: str) -> str | None:
    """The line's query operator (?+0.5, ?max, ?(a|b)), or None."""
    for word in _words(line):
        value = re.sub(r"^[A-Za-z_][\w.]*:", "", word)
        if value.startswith("?") and len(value) > 1:
            return value
    return None


# An item's id as Modo writes it: mesh039, translation005, sunLight018.
_ID = re.compile(r"\b[a-z][A-Za-z]*\d{3,}\b")
_BY_INDEX = re.compile(r"^[!+]*\s*select\.element\b")
_REPLAYED = ("in", "unsure", "failed")


def inexact(records: list[dict[str, Any]]) -> list[tuple[Any, str]]:
    """The replayed entries that can't promise the same counts and boxes,
    each with why: a query operator not read back, code run again, a
    selection by index, an item named by id after a step the replay leaves
    out (6.3: ids move)."""
    plan = _plan(records)
    out: list[tuple[Any, str]] = []
    left_out = False
    for record in records:
        if record.get("event") != "entry":
            continue
        n = record.get("n")
        state = plan[n]
        if state not in _REPLAYED:
            if state != "not":
                left_out = True
            continue
        why = []
        for op in record.get("operators") or []:
            if not op.get("read"):
                why.append("%s couldn't be read back"
                           % (operator_word(str(op.get("sent"))) or "an operator"))
        if (record.get("tool") == "run_python" and record.get("untraced")
                and state == "in"):
            why.append("run as its code")
        lines = [str(line) for line in record.get("commands") or []]
        if any(_BY_INDEX.match(line) for line in lines):
            why.append("selects by index")
        if left_out and any(_ID.search(line) for line in lines):
            why.append("names an item by id after a step left out")
        if why:
            out.append((n, "; ".join(why)))
    return out


def gaps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The gap records: the user's edits between two calls."""
    return [r for r in records if r.get("event") == "gap"]


def gap_text(gap: dict[str, Any]) -> str:
    """"3 events, 1 added: Position (Camera), locator040"."""
    k = gap.get("events") or 0
    text = "%d event%s" % (k, "" if k == 1 else "s")
    for key in ("added", "removed"):
        if gap.get(key):
            text += ", %d %s" % (gap[key], key)
    items = [str(i.get("name") or i.get("id")) for i in gap.get("items") or []
             if isinstance(i, dict)]
    if items:
        text += ": " + ", ".join(items)
    return text


def exact_line(records: list[dict[str, Any]]) -> str:
    """mt_journal's "exact: yes", or which entries aren't and the gaps."""
    found = inexact(records)
    holes = gaps(records)
    if not found and not holes:
        return "exact: yes"
    parts = []
    if found:
        parts.append(", ".join("%s (%s)" % (n, why) for n, why in found))
    if holes:
        parts.append("gaps before " + ", ".join(str(g.get("before"))
                                                for g in holes))
    return "exact: no: " + "; ".join(parts)


def _operators_said(entry: dict[str, Any]) -> str:
    said = []
    for op in entry.get("operators") or []:
        word = operator_word(str(op.get("sent"))) or "an operator"
        said.append("%s %s" % (word, "read back" if op.get("read")
                               else "couldn't be read back"))
    return " (%s)" % ", ".join(said) if said else ""


def render(name: str, records: list[dict[str, Any]], newest: list[str],
           limit: int | None = None, replay: list[str] | None = None,
           open_action: str | None = None) -> str:
    """The journal as text. Past `limit` characters the oldest entries go
    first, so the newest and the file line stay."""
    header = next((r for r in records if r.get("event") == "header"), None)
    listed = entries(records, open_action)
    count = "%d entr%s" % (len(listed), "y" if len(listed) == 1 else "ies")
    head = ["journal %s, %s, %s:" % (name, started(header), count)]
    holes: dict[Any, list[dict[str, Any]]] = {}
    for gap in gaps(records):
        holes.setdefault(gap.get("before"), []).append(gap)
    body = []
    for entry in listed:
        for gap in holes.pop(entry.get("n"), []):
            body.append("        gap: outside the agent, " + gap_text(gap))
        body.append("  %3s  %-12s %-28s %s" % (
            entry.get("n"), entry.get("tool"),
            label(entry) + (" (action %d)" % entry["action_k"]
                            if "action_k" in entry else ""),
            entry["state"] + _operators_said(entry)))
    for later in holes.values():
        body += ["        gap: outside the agent, after the last entry, "
                 + gap_text(gap) for gap in later]
    tail = []
    found, _ = actions(records)
    if found:
        tail.append("actions: " + "; ".join(
            '%d "%s" %s' % (a.k, a.label, a.outcome(open_action))
            for a in found))
    ended = [r for r in records if r.get("event") == "end"]
    if ended:
        tail.append("ended: %s" % ended[-1].get("why"))
    tail.append(exact_line(records))
    tail.append("file: %s" % path(name))
    tail += replay or []
    tail.append("newest: " + ", ".join(newest[:NEWEST]))

    def text(kept: list[str]) -> str:
        # Entries, not lines: a gap's line isn't an entry.
        left = sum(1 for line in body[:len(body) - len(kept)]
                   if not line.lstrip().startswith("gap:"))
        more = (["  ... %d earlier entries not shown" % left] if left else [])
        return "\n".join(head + more + kept + tail)

    if limit is None or len(text(body)) <= limit:
        return text(body)
    kept: list[str] = []
    for line in reversed(body):
        if len(text([line] + kept)) > limit:
            break
        kept.insert(0, line)
    return text(kept)


# -- the replay ----------------------------------------------------

# A line the replay never runs: it saves, opens, closes or reverts a scene,
# exports a file or quits. The replay's own _NEVER is the same pattern, for
# the lines an entry's code sends.
NEVER = re.compile(r"^[!+]*\s*(scene\.(save|saveAs|saveAsPreset|open|new|"
                   r"close|closeAll|revert)|app\.(quit|exit)|\S*export\S*)"
                   r"(\s|$)", re.I)

# The replay's helpers, written into every file: it imports only re and lx
# (and modo, lxu inside them), so it runs on a Modo without our kit.
PRELUDE = r'''import re

import lx

# Lines a replay never runs: they save, open, close or revert a scene, export
# a file or quit.
_NEVER = re.compile(r"^[!+]*\s*(scene\.(save|saveAs|saveAsPreset|open|new|"
                    r"close|closeAll|revert)|app\.(quit|exit)|\S*export\S*)"
                    r"(\s|$)", re.I)
# Boxes match to this many metres: the journal rounds them to six decimals.
_TOLERANCE = 1e-05
_checked = []
# The user's edits between calls, which a replay can't redo, and the checks
# after one that differed: those warn instead of stopping.
_gaps = []
_differed = []


def _gap(before, text):
    lx.out("replay: gap before entry %d, outside the agent: %s; not replayed"
           % (before, text))
    _gaps.append(before)


def _code(text, n):
    """An entry's code, run in a fresh namespace as mt_run_python ran it,
    skipping the lines a replay never runs."""
    saved = {}

    def skip(real):
        def call(*args, **kwargs):
            line = str(args[0]) if args else ""
            if _NEVER.match(line):
                lx.out("replay: entry %d: not replayed: %s" % (n, line))
                return None
            return real(*args, **kwargs)
        return call
    for name in ("eval", "eval1", "evalN", "command"):
        if hasattr(lx, name):
            saved[name] = getattr(lx, name)
            setattr(lx, name, skip(saved[name]))
    try:
        space = {"__name__": "__main__", "lx": lx}
        for module in ("modo", "lxu"):
            try:
                space[module] = __import__(module)
            except ImportError:
                pass
        exec(compile(text, "<journal entry %d>" % n, "exec"), space)
    finally:
        for name, real in saved.items():
            setattr(lx, name, real)


def _set(item, channel, value):
    """A channel, set as mt_set_channels set it: the raw value."""
    import modo
    modo.Scene().item(item).channel(channel).set(value)


def _points(item, points):
    """Vertex positions, set as mt_set_points set them: through a layer
    scan on the item, in the mesh's own space."""
    import modo
    item = modo.Scene().item(item)
    if isinstance(points, list):
        points = dict(enumerate(points))
    scan = lx.object.LayerScan(lx.service.Layer().ScanAllocateItem(
        lx.object.Item(item), lx.symbol.f_LAYERSCAN_EDIT_VERTS))
    mesh = lx.object.Mesh(scan.MeshEdit(0))
    accessor = mesh.PointAccessor()
    for index, pos in points.items():
        accessor.SelectByIndex(int(index))
        accessor.SetPos(tuple(float(v) for v in pos))
    # POINTS too, as the kit's write.
    scan.SetMeshChange(0, lx.symbol.f_MESHEDIT_POSITION
                       | lx.symbol.f_MESHEDIT_POINTS)
    scan.Apply()


def _mesh_facts(item):
    fact = {"name": item.name, "id": item.id, "points": 0, "polygons": 0,
            "box": None}
    scene = lx.object.Scene(item.Context())
    time = lx.service.Selection().GetTime()
    try:
        mesh = lx.object.Mesh(scene.Channels(
            lx.symbol.s_ACTIONLAYER_EDIT, time).ValueObj(
            item, item.ChannelLookup(lx.symbol.sICHAN_MESH_MESH)))
    except LookupError:
        return fact
    matrix, offset = lx.object.Locator(item).WorldTransform(
        scene.Channels(None, time))
    points = mesh.PointAccessor()
    low = high = None
    for i in range(mesh.PointCount()):
        points.SelectByIndex(i)
        local = points.Pos()
        world = [sum(m * v for m, v in zip(row, local)) + o
                 for row, o in zip(matrix, offset)]
        low = world if low is None else list(map(min, low, world))
        high = world if high is None else list(map(max, high, world))
    fact["points"] = mesh.PointCount()
    fact["polygons"] = mesh.PolygonCount()
    if low is not None:
        fact["box"] = [low, high]
    return fact


def _facts():
    import modo
    return [_mesh_facts(item) for item in modo.Scene().items("mesh")]


def _same_box(want, have):
    if want is None or have is None:
        return want is None and have is None
    return all(abs(a - b) <= _TOLERANCE
               for w, h in zip(want, have) for a, b in zip(w, h))


def _box(box):
    if box is None:
        return "empty"
    return " to ".join("(%s)" % ", ".join("%g" % v for v in corner)
                       for corner in box)


def _differences(want, have):
    out = []
    here = {mesh["id"]: mesh for mesh in have}
    # By id and name, then by name, then by id: a step left out of the
    # replay (rolled back, undone) used up ids in the scene, so a later mesh
    # comes back with another id and its name, and that id can be one the
    # journal gave another mesh.
    found = [None] * len(want)
    for i, mesh in enumerate(want):
        same = here.get(mesh["id"])
        if same is not None and same["name"] == mesh["name"]:
            found[i] = here.pop(mesh["id"])
    for i, mesh in enumerate(want):
        if found[i] is None:
            named = next((m for m in here.values()
                          if m["name"] == mesh["name"]), None)
            if named is not None:
                found[i] = here.pop(named["id"])
    for i, mesh in enumerate(want):
        if found[i] is None:
            found[i] = here.pop(mesh["id"], None)
    for mesh, found in zip(want, found):
        who = "%s (%s)" % (mesh["name"], mesh["id"])
        if found is None:
            out.append("%s is missing" % who)
            continue
        if found["name"] != mesh["name"]:
            out.append("%s is called %s" % (who, found["name"]))
        for key in ("points", "polygons"):
            if found[key] != mesh[key]:
                out.append("%s has %d %s, the journal says %d" % (
                    who, found[key], key, mesh[key]))
        if not _same_box(mesh["box"], found["box"]):
            out.append("%s's box is %s, the journal says %s" % (
                who, _box(found["box"]), _box(mesh["box"])))
    for mesh in here.values():
        out.append("%s (%s) isn't in the journal" % (mesh["name"], mesh["id"]))
    return out


def _say(meshes):
    if len(meshes) == 1:
        return "1 mesh, %s, %d points" % (meshes[0]["name"],
                                          meshes[0]["points"])
    return "%d meshes" % len(meshes)


def _check(facts, after):
    """Compare the scene with the journal's facts; stop the replay where
    they differ."""
    have = _facts()
    found = _differences(facts, have)
    if not found:
        _checked.append(after)
        return
    if after and _gaps:
        lx.out("replay: check after %d differs, after a gap: %s"
               % (after, "; ".join(found)))
        _differed.append(after)
        return
    if after == 0:
        text = ("replay stopped: this isn't the scene the journal began from "
                "(%s: %s; here: %s)" % (_START, _say(facts), _say(have)))
    else:
        text = "replay stopped: check after %d: %s" % (after, "; ".join(found))
    lx.out(text)
    raise RuntimeError(text)


def _done(run):
    said = ", ".join("start" if after == 0 else "after %d" % after
                     for after in _checked)
    parts = []
    if _checked:
        parts.append("%s %s: same counts and bounding boxes" % (
            "check" if len(_checked) == 1 else "checks", said))
    elif not _differed:
        parts.append("no checks")
    parts += ["check after %d differs after a gap" % after
              for after in _differed]
    if _gaps:
        parts.append("%d gap%s not replayed" % (
            len(_gaps), "" if len(_gaps) == 1 else "s"))
    lx.out("replay: %d entries run; %s" % (run, "; ".join(parts)))


'''


def replay_path(name: str) -> str:
    return os.path.join(protocol.journals_dir(), name + ".py")


def _flat(value: Any) -> str:
    """Text for a comment: a line break or a NUL in it would end the comment
    and run the rest as Python, or stop the file compiling."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


def _bangs(line: str) -> str:
    return line if line.lstrip().startswith("!!") else "!!" + line.lstrip("!+ ")


def _code_literal(code: str) -> str:
    """r'''...''' when it can hold the code, else Python's own quoting."""
    trailing = len(code) - len(code.rstrip("\\"))
    if ("'''" in code or trailing % 2 or "\r" in code or "\0" in code
            or code.endswith("'")):
        return repr(code)
    return "r'''%s'''" % code


def _facts_literal(meshes: list[dict[str, Any]] | None) -> str:
    if meshes is None:
        return "None"
    return "[\n" + "".join("    %r,\n" % m for m in meshes) + "]"


def _start(header: dict[str, Any] | None) -> tuple[str, str]:
    """(where to start, the header's sentence about it)."""
    scene = (header or {}).get("scene") or {}
    name = scene.get("name") or "an unknown scene"
    where = scene.get("file") or "File > New"
    said = "the journal began in %s, %s" % (
        name, "with unsaved changes the replay can't rebuild"
        if scene.get("unsaved") else
        ("not saved" if not scene.get("file") else "as saved"))
    return where, said


# The link from a backdrop to its clip, which no command makes
# on 17.1, made as the kit's backdrop op made it.
# By the newest backdrop, which the entry's lines just made, and the clip
# holding the entry's file: not by id, since Modo may give them other ids in
# the replay's scene, and not the newest clip, since clip.addStill
# makes none for a file the scene has a clip for. An entry
# with no file (written by older kits) links the newest clip.
LINK = ("import lx, modo\n"
        "FILE = %r\n"
        "scene = modo.Scene()\n"
        "same = lambda path: str(path).replace(chr(92), '/').lower()\n"
        "clips = scene.items('videoStill')\n"
        "shown = [c for c in clips\n"
        "         if FILE and same(c.channel('filename').get()) == same(FILE)]\n"
        "item, clip = scene.items('backdrop')[-1], (shown or clips)[-1]\n"
        "lx.object.ItemGraph(lx.object.Scene(item.Context()).GraphLookup(\n"
        "    'backdropClip')).AddLink(item, clip)\n")


def link_code(file: Any) -> str:
    """The replay's code linking the newest backdrop to file's clip."""
    return LINK % (str(file) if file else None,)


def _entry_lines(entry: dict[str, Any], state: str,
                 action: Action | None = None, outcome: str = "") -> list[str]:
    n, tool = entry.get("n"), entry.get("tool")
    title = "# %s  %s  %s" % (n, tool, _flat(label(entry)))
    untraced = [_flat(name) for name in entry.get("untraced") or []]
    # Only an entry that ran whole runs as its code: an unsure one failed,
    # and its code would raise again.
    as_code = tool == "run_python" and untraced and state == "in"
    if state in ("out", "open") and action is not None:
        return [title + '  (action "%s", %s)' % (
            _flat(action.label), "rolled back" if state == "out" else outcome)]
    if state == "out":
        return [title + "  rolled back"]
    if state == "undone":
        return [title + "  undone by the user"]
    if state == "agent":
        return [title + "  undone by the agent"]
    if state == "not":
        return [title + "  not replayed"]
    if state == "unsure":
        title += "  rollback unsure: replayed as if it stuck"
    elif state == "failed":
        title += "  failed, not rolled back: its trace"
    elif as_code:
        title += "  untraced: %s, run as its code" % ", ".join(untraced)
    out = [title]
    args = entry.get("args") or {}
    if as_code:
        out.append("_code(%s, %s)" % (_code_literal(str(args.get("code", ""))), n))
        return out
    if tool == "set_channels":
        values = entry.get("set")
        if not isinstance(values, dict):
            return out + ["# nothing read back to replay"]
        for channel, value in values.items():
            out.append("_set(%r, %r, %r)" % (
                value.get("item") or args.get("item"), channel, value.get("new")))
        return out
    if tool == "set_points":
        # The values are in the entry, so the replay is exact.
        return out + ["_points(%r, %r)" % (
            entry.get("points_item") or args.get("item"), args.get("points"))]
    if tool == "fit_backdrops":
        # The points the fit wrote, as values: the replay
        # needs no backdrop or image.
        points = entry.get("points")
        if not isinstance(points, dict):
            return out + ["# no points recorded to replay"]
        return out + ["_points(%r, %r)" % (
            entry.get("points_item") or args.get("item"), points)]
    sent = {op.get("i"): op for op in entry.get("operators") or []}
    for i, line in enumerate(entry.get("commands") or []):
        if NEVER.match(line):
            out.append("# not replayed: %s" % _flat(line))
            continue
        op = sent.get(i)
        if op is not None and op.get("read"):
            out.append("# as sent: %s" % _flat(op.get("sent")))
        elif op is not None:
            out.append("# couldn't be read back, replays relative to the "
                       "scene: %s" % _flat(op.get("sent")))
        out.append("lx.eval(%r)" % _bangs(line))
    link = entry.get("link")
    if tool == "backdrop" and isinstance(link, list) and len(link) == 2:
        out.append("_code(%s, %s)" % (
            _code_literal(link_code(entry.get("clip_file"))), n))
    for line in entry.get("failed") or []:
        out.append("# failed: %s" % _flat(line))
    if untraced:
        out.append("# untraced, not replayed: %s" % ", ".join(untraced))
    return out


# The later states that leave an entry out of the scene.
_OUT = ("out", "undone", "agent")


def checked_facts(records: list[dict[str, Any]], plan: dict[Any, str]
                  ) -> tuple[dict[Any, dict[str, Any]], dict[Any, Any]]:
    """(the facts record checked after each entry: the last one taken there;
    the checks left out, as after -> the entry that makes them stale).

    Facts are what the scene held when they were taken. An entry undone,
    rolled back or redone after that (mt_undo_last, the user's Ctrl+Z, an action
    rolled back or lost in a crash) is in the facts and out of the replay,
    or the other way round, so the check would stop a replay that is right."""
    facts: dict[Any, dict[str, Any]] = {}
    then: dict[Any, tuple[list[Any], dict[Any, str | None]]] = {}
    later: dict[Any, str | None] = {}
    seen: list[Any] = []
    for record in records:
        event = record.get("event")
        if event == "entry":
            seen.append(record.get("n"))
        elif event in _LATER:
            state = _LATER[event]
            if state == "undone" and record.get("by") == "agent":
                state = "agent"
            later[record.get("n")] = state
        elif event == "facts":
            facts[record.get("after")] = record
            then[record.get("after")] = (list(seen), dict(later))
    stale: dict[Any, Any] = {}
    for after, (ns, was) in then.items():
        for n in ns:
            now = plan.get(n)
            if now == "not":
                continue
            if (was.get(n) in _OUT) != (now not in _REPLAYED):
                stale[after] = n
                break
    return facts, stale


def _numbers(ns: list[Any]) -> str:
    return "%d (%s)" % (len(ns), ", ".join(str(n) for n in ns))


def summary(records: list[dict[str, Any]]) -> tuple[list[str], list[Any]]:
    """(the replay's checks, the entries it runs as their code)."""
    plan = _plan(records)
    header = next((r for r in records if r.get("event") == "header"), None)
    facts, stale = checked_facts(records, plan)
    afters = [after for after in facts if after not in stale]
    checks = (["start"] if (header or {}).get("facts") is not None else []
              ) + ["after %s" % n for n in afters]
    as_code = [r["n"] for r in records if r.get("event") == "entry"
               and r.get("tool") == "run_python" and r.get("untraced")
               and plan[r["n"]] == "in"]
    return checks, as_code


def _action_line(action: Action, plan: dict[Any, str], outcome: str) -> str:
    """The comment an action's entries go under."""
    steps = "%d step%s" % (len(action.ns), "" if len(action.ns) == 1 else "s")
    head = '# action "%s": ' % _flat(action.label)
    if outcome == "committed":
        return head + steps + ", committed"
    if outcome == "rolled back":
        left = sum(1 for n in action.ns if plan.get(n) in ("in", "failed", "unsure"))
        if not left:
            return head + "rolled back"
        return head + "rolled back; %d step%s it couldn't reach stay%s in" % (
            left, "" if left == 1 else "s", "s" if left == 1 else "")
    return head + "%s, %s: not replayed" % (steps, outcome)


def build(name: str, records: list[dict[str, Any]],
          open_action: str | None = None) -> str:
    """The replay of a journal, as text: plain Python that rebuilds the
    scene with @ and checks it where the journal took facts. Builds from the
    records alone; runs nothing. open_action: the label of the action the
    kit holds open now, when this is the current scene's journal."""
    header = next((r for r in records if r.get("event") == "header"), None)
    plan = _plan(records)
    _, of = actions(records)
    entries = [r for r in records if r.get("event") == "entry"]
    # The last facts record after each entry is the one checked there,
    # unless an entry before it was undone or redone since (stale).
    facts, stale = checked_facts(records, plan)
    start_facts = (header or {}).get("facts")
    checks, as_code = summary(records)

    where, said = _start(header)
    head = ["# python",
            "# modo-tools journal %s: %s" % ((header or {}).get("journal", 1), name)]
    head += _wrap("# ", "Modo %s, kit %s. Start from %s (%s). Run with @ this "
                  "file: it writes no file and opens no scene." % (
                      (header or {}).get("modo"), (header or {}).get("kit"),
                      _flat(where), _flat(said)))
    count = len(entries)
    parts = ["%d in this file" % sum(1 for s in plan.values()
                                     if s in ("in", "unsure", "failed"))]
    out_n = sum(1 for s in plan.values() if s == "out")
    if out_n:
        parts.append("%d rolled back" % out_n)
    for state, said in (("undone", "undone by the user"),
                        ("agent", "undone by the agent"),
                        ("open", "in an action not committed")):
        k = sum(1 for s in plan.values() if s == state)
        if k:
            parts.append("%d %s" % (k, said))
    not_n = sum(1 for s in plan.values() if s == "not")
    if not_n:
        parts.append("%d not replayed (a save)" % not_n)
    head.append("# %d entr%s: %s." % (count, "y" if count == 1 else "ies",
                                       ", ".join(parts)))
    if as_code:
        head.append("# Untraced, run as their code: %s." % _numbers(as_code))
    failed = [n for n, s in plan.items() if s == "failed"]
    if failed:
        head.append("# Failed and not rolled back, their trace only: %s."
                    % _numbers(failed))
    for n, s in plan.items():
        if s == "unsure":
            head.append("# Warning: entry %s's rollback was unsure; its trace "
                        "is in, as if it stuck." % n)
    if checks:
        head.append("# Checks: %s." % ", ".join(checks))
    elif stale:
        head.append("# Checks: none.")
    else:
        head.append("# Checks: none; the journal has no facts records.")
    if stale:
        head += _wrap("# ", "Not checked: %s. Those facts were taken before "
                      "an entry was undone, rolled back or redone, so they "
                      "aren't what the replay builds." % ", ".join(
                          "after %s (entry %s)" % pair
                          for pair in stale.items()))
    found = inexact(records)
    holes = gaps(records)
    if not found and not holes:
        head.append("# Exact: yes.")
    if found:
        head += _wrap("# ", "Not exact: %s." % ", ".join(
            "%s (%s)" % (n, _flat(why)) for n, why in found))
    if holes:
        head += _wrap("# ", "Gaps, edits outside the agent it can't redo: "
                      "before %s. A check after a gap that differs warns and "
                      "goes on." % ", ".join(str(g.get("before")) for g in holes))

    body = ["_START = %r" % where, "START = %s" % _facts_literal(start_facts)]
    for after, record in facts.items():
        if after in stale:
            continue
        body.append("FACTS_%s = %s" % (after, _facts_literal(record.get("meshes"))))
    body.append("")
    if start_facts is not None:
        body += ["_check(START, after=0)", ""]
    last = {id(record) for record in facts.values()}
    said: set[int] = set()
    for record in records:
        event = record.get("event")
        if event == "entry":
            action = of.get(record.get("n"))
            outcome = action.outcome(open_action) if action else ""
            if action is not None and action.k not in said:
                said.add(action.k)
                body.append(_action_line(action, plan, outcome))
            body += _entry_lines(record, plan[record.get("n")], action,
                                 outcome) + [""]
        elif event == "facts" and id(record) in last:
            after = record.get("after")
            if after in stale:
                body += ["# check after %s left out: entry %s was undone, "
                         "rolled back or redone after these facts were taken"
                         % (after, stale[after]), ""]
            else:
                body += ["_check(FACTS_%s, after=%s)" % (after, after), ""]
        elif event == "gap":
            body += ["# gap: outside the agent, not replayed",
                     "_gap(%r, %r)" % (record.get("before"),
                                       _flat(gap_text(record))), ""]
        elif event == "save":
            body[-1:] = ["# saved: %s" % _flat(record.get("path")), ""]
        elif event == "end":
            body += ["# ended: %s" % _flat(record.get("why")), ""]
    run = sum(1 for s in plan.values() if s in ("in", "unsure", "failed"))
    body.append("_done(%d)" % run)
    return "\n".join(head) + "\n\n" + PRELUDE + "\n".join(body) + "\n"


def _wrap(prefix: str, text: str, width: int = 78) -> list[str]:
    lines, line = [], prefix
    for word in text.split(" "):
        if len(line) + len(word) + (line != prefix) > width and line != prefix:
            lines.append(line)
            line = prefix
        line += ("" if line == prefix else " ") + word
    lines.append(line)
    return lines


def write_replay(name: str, records: list[dict[str, Any]],
                 open_action: str | None = None) -> str:
    """Build the replay and write it beside the journal, as <name>.py.
    Returns its path. The only file the builder writes."""
    target = replay_path(name)
    text = build(name, records, open_action)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target
