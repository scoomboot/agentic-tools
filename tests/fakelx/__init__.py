"""A small stand-in for Modo's lx, lxifc and lxu, for the kit's tests.

Not an emulation of Modo. It has a command registry (lx.bless / lx.eval,
eval1, evalN, lx.command), the platform service's idle callback, lx.out into an
event log and the log service that reads it back, the version queries, the
command service's queries, the host service's tools, and a small scene in a
fake `modo` module. Each feature adds what it needs.

What it checks that Modo would not tell us about until it crashed or hung:

* Every lx call must come from the main thread (the thread that called
  install()). A call from any other thread is recorded in `off_thread` and
  raises, so a test can assert the socket thread never touched lx.
* A command run while another is running is recorded in `nested`.
* A command that fails without the !! prefix would open a modal dialog in
  Modo: it is recorded in `dialogs`.
* A per-command command service query for a name that isn't a command
  crashes Modo 17.1: it is recorded in `crashes`.
"""

import re
import shlex
import sys
import threading
import types


class OffMainThread(RuntimeError):
    pass


class FakeModo(object):
    def __init__(self):
        self.main_thread = threading.current_thread()
        self.event_log = []
        self.commands = {}      # commands blessed by the kit
        self.builtin = {}       # Modo's own commands, as far as tests need
        self.idle = []          # visitors waiting for the next idle tick
        self.off_thread = []    # lx calls made from another thread
        self.nested = []        # commands run inside another command
        self.ran = []           # every command name lx.eval ran, in order
        self.version = (17, 1, 1)
        self.build = 712345
        self.idle_supported = True
        self._depth = 0
        self.evals = []         # every string given to lx.eval/eval1/evalN
        self.command_calls = [] # every lx.command(name, **args) call
        self.dialogs = []       # commands that failed without !!
        self.crashes = []       # queries that would have crashed Modo 17.1
        # name -> {"username", "desc", "args": [(name, type, flags, username)]}
        # for the command service. Blessed commands get a plain entry.
        self.described = {}
        # tool name -> [(attribute, type name, default)], for the host service
        self.tools = {}
        self.scene = FakeScene()
        self.mesh_writes = []   # write access to a fake mesh: never allowed
        self.channel_reads = [] # (action, time) of every scene.Channels
        self.packet_reads = 0   # selection packets the kit read
        self.surface_samples = []  # (item id, action) of every Sample
        self.sampled_points = 0    # points the sampler handed out
        # The undo stack, as the probe found it on 17.1 (2026-11-08,
        # 5): every undoable command run from the top is one step, even one
        # that changed nothing; what it runs inside is part of that step; a
        # channel set() is undoable; a command block still open when the
        # command ends is ended by Modo; app.undo is refused inside a command.
        self.undo_steps = []    # [UndoStep], oldest first
        self.redo_steps = []    # [UndoStep], the last one undone last
        # Markers (lxifc.Undo objects given to the undo service's Record)
        # are called as probes found on 17.1: undo_Reverse when their
        # step is undone, undo_Forward when it is redone, with or without a
        # block. silent_markers makes app.undo skip them, as if the step it
        # undid were someone else's.
        self.silent_markers = False
        self.marker_records = 0  # every Record call, in a step or not
        self._step = None       # the step the running command records into
        self.blocks = []        # the name of every block opened, in order
        self.open_blocks = 0
        # Changes made inside a command that keeps no step (a UI command).
        self.untracked = []
        # Scene item listeners (lxifc.SceneItemListener objects given to the
        # listener service), told of item and channel events as a probe found on
        # 17.1 (2026-11-08): on the main thread, during jobs and hand edits
        # alike, and during undo and redo with the undo service's State() 0.
        self.listeners = []
        self.undo_state = 1     # iUNDO_ACTIVE; 0 while undo or redo plays
        # Graph links (name -> [(from, to)]), the image
        # service's loads and saves, and commands made to fail by name.
        self.links = {}
        self.image_loads = []
        self.image_saves = []   # (path, format, (width, height))
        self.image_save_fails = False
        # An image's pixels by path, rows top first, each
        # r, g, b, a bytes; a loaded path not here has no pixels to read.
        self.image_pixels = {}
        self.failing = set()
        # Every layer scan allocated, as (item id, flags, the
        # scan), and a scan whose Apply raises.
        self.layer_scans = []
        self.scan_apply_fails = False

    # -- checks ------------------------------------------------------------

    def check_thread(self, what):
        if threading.current_thread() is not self.main_thread:
            self.off_thread.append(what)
            raise OffMainThread("%s called off the main thread" % what)

    # -- what Modo does ----------------------------------------------------

    def tick(self):
        """One idle tick: fire each armed visitor once, as Modo does."""
        self.check_thread("tick")
        visitors, self.idle = self.idle, []
        for visitor in visitors:
            visitor.vis_Evaluate()
        return len(visitors)

    def run_command(self, text):
        """lx.eval: run a command or answer a query. A failure raises, and
        without !! it is also recorded as the dialog Modo would show."""
        self.check_thread("lx.eval")
        self.evals.append(text)
        suppressed = text.lstrip().startswith("!!")
        try:
            return self._run(text)
        except Exception as exc:
            if not suppressed:
                self.dialogs.append("%s: %s" % (text, exc))
            raise

    def _run(self, text):
        name, positional, named = _parse(text)
        if name == "query":
            return self._query(positional)
        cls = self.commands.get(name) or self.builtin.get(name)
        if cls is None:
            raise RuntimeError("Unknown command: %s" % name)
        if self._depth:
            self.nested.append(name)
        self.ran.append(name)
        if name in self.failing:
            raise RuntimeError("Command failed: %s" % name)
        cmd = cls()
        if "?" in positional and type(cmd).cmd_Query is not _BasicCommand.cmd_Query:
            # A query: Modo asks the command for the argument's value.
            values = _ValueArray()
            cmd.cmd_Query(positional.index("?"), values)
            return values.values[0] if len(values.values) == 1 else values.values
        cmd._fill(positional, named)
        top = not self._depth
        if top and name not in ("app.undo", "app.redo"):
            # A check on 17.1: a read (execRead) between Ctrl+Z and
            # Ctrl+Shift+Z left "out of redos". Any command from the top
            # ends the redo list here.
            self.redo_steps = []
        if top and cmd.cmd_Flags() & _Symbol.fCMD_UNDO:
            self._step = UndoStep(name)
        self._depth += 1
        try:
            return cmd.basic_Execute(None, 0)
        finally:
            self._depth -= 1
            if top:
                # Kept even when the command raised or changed nothing.
                if self._step is not None:
                    self.undo_steps.append(self._step)
                self._step = None
                self.open_blocks = 0

    # -- the undo stack ------------------------------------------------------

    def record(self, undo_fn, what, channel=False, redo_fn=None):
        """A change: undo_fn() puts it back, redo_fn() makes it again. Into
        the running command's step, or, inside a command that keeps none, into
        `untracked`. A channel set() inside a command block is lost to undo
        too: on 17.1 the step undid, and the channel stayed (the 5.1 Modo
        check, 2026-11-08)."""
        if channel and self.open_blocks:
            self.untracked.append(what)
        elif self._step is not None:
            self._step.changes.append((undo_fn, redo_fn))
        elif self._depth:
            self.untracked.append(what)

    def record_marker(self, marker):
        """The undo service's Record: into the running command's step."""
        self.marker_records += 1
        if self._step is not None:
            self._step.changes.append((marker.undo_Reverse, marker.undo_Forward))
            self._step.markers += 1

    def hand_edit(self, item_id, channel, value):
        """The user sets a channel in Modo's Properties: their own step, with no
        marker of the kit's in it. Ends the redo list, as any edit does."""
        self.check_thread("hand edit")
        item = self.scene.find(item_id)
        target = item.channel(channel)
        old = target.value
        target.value = value
        step = UndoStep("hand edit")
        step.changes.append((lambda: setattr(target, "value", old),
                             lambda: setattr(target, "value", value)))
        self.undo_steps.append(step)
        self.redo_steps = []
        self.scene_event("sil_ChannelValue", "edit", item, 0)

    def hand_add(self, item_id, name, item_type="locator"):
        """The user adds an item by hand (an ItemAdd per item made)."""
        self.check_thread("hand add")
        item = self.scene.add(item_id, name, item_type)
        step = UndoStep("hand add")
        step.changes.append((lambda: self.scene.items.remove(item),
                             lambda: self.scene.items.append(item)))
        self.undo_steps.append(step)
        self.redo_steps = []
        self.scene_event("sil_ItemAdd", item)
        return item

    def scene_event(self, method, *args):
        """Tell every scene item listener, as Modo does: what one raises
        doesn't reach the caller."""
        for listener in list(self.listeners):
            handler = getattr(listener, method, None)
            if handler is not None:
                try:
                    handler(*args)
                except Exception:
                    pass

    def undo(self, silent=False):
        """The user's Ctrl+Z: the top step, reversed. False if there is none."""
        self.check_thread("undo")
        if not self.undo_steps:
            return False
        step = self.undo_steps.pop()
        self.undo_state = 0
        try:
            for undo_fn, _redo in reversed(step.changes):
                if not (silent and getattr(undo_fn, "__name__", "") == "undo_Reverse"):
                    undo_fn()
            self.scene_event("sil_ChannelValue", "edit", None, 0)
        finally:
            self.undo_state = 1
        self.redo_steps.append(step)
        return True

    def close_scene(self):
        """The user's File > Close (Don't Save): the scene's undo stack goes
        with it. A probe (2026-11-08): no marker on it is called, and a Ctrl+Z
        in the scene Modo shows next doesn't reach them."""
        self.check_thread("close scene")
        self.undo_steps = []
        self.redo_steps = []

    def redo(self):
        """Ctrl+Shift+Z: the last step undone, made again."""
        self.check_thread("redo")
        if not self.redo_steps:
            return False
        step = self.redo_steps.pop()
        self.undo_state = 0
        try:
            for _undo, redo_fn in step.changes:
                if redo_fn is not None:
                    redo_fn()
            self.scene_event("sil_ChannelValue", "edit", None, 0)
        finally:
            self.undo_state = 1
        self.undo_steps.append(step)
        return True

    def command_names(self):
        return sorted(set(self.commands) | set(self.builtin) | set(self.described))

    def _query(self, words):
        # query <service> <attribute> ? [target]
        if len(words) >= 3 and words[0] == "sceneservice":
            return self._scene_query(words[1])
        if len(words) < 3 or words[0] != "commandservice":
            raise RuntimeError("ScriptQuery: unknown service")
        attribute = words[1]
        target = words[3] if len(words) > 3 else None
        if attribute == "commands":
            return self.command_names()
        if not attribute.startswith("command."):
            raise RuntimeError("ScriptQuery: Unknown attribute")
        target = (target or "").strip("{}")
        if target not in self.command_names():
            self.crashes.append(" ".join(["query"] + list(words)))
            raise RuntimeError("Modo crashed")
        info = self.described.get(target) or {"username": target, "desc": "",
                                               "args": []}
        args = info.get("args", [])
        table = {
            "command.username": [info.get("username", target)],
            "command.desc": [info.get("desc", "")],
            "command.argNames": [a[0] for a in args],
            "command.argTypeNames": [a[1] for a in args],
            "command.argFlags": [a[2] for a in args],
            "command.argUsernames": [a[3] if len(a) > 3 else a[0] for a in args],
        }
        if attribute not in table:
            raise RuntimeError("ScriptQuery: Unknown attribute")
        return table[attribute]

    def _scene_query(self, attribute):
        """The current scene, as a probe found Modo 17.1 tells it (2026-11-08):
        its name (unique among open scenes), its file, and a change count
        that a save sets to 0."""
        scene = self.scene
        if scene.unreadable:
            raise RuntimeError("ScriptQuery: no current scene")
        table = {"scene.name": scene.name, "scene.file": scene.filename,
                 "scene.changed": scene.changes}
        if attribute not in table:
            raise RuntimeError("ScriptQuery: Unknown attribute")
        return table[attribute]

    def describe(self, name, username, desc="", args=(), hints=None,
                 popups=None):
        """Give the command service a command with arguments
        [(name, type name, flags, username)], and register a command that
        accepts them (and fails if `fails` is set in its class).
        hints: {argument: [(int, word), ...]}, as Attributes.Hints answers
        on 17.1, "=set-name" entry included; the string
        "raise" makes the read raise. popups: {argument: [word, ...]}, the
        words its UIValueHints lists."""
        self.described[name] = {"username": username, "desc": desc,
                                "args": list(args), "hints": hints or {},
                                "popups": popups or {}}

    def command(self, *args, **kwargs):
        """lx.command: a command with named arguments, no string to join.
        The command is positional only, as in Modo: an argument may be
        called `name`."""
        name = args[0]
        self.check_thread("lx.command")
        self.command_calls.append((name, dict(kwargs)))
        text = name + "".join(" %s:{%s}" % (k, v) for k, v in kwargs.items())
        return self.run_command(text)

    def out(self, *parts):
        self.check_thread("lx.out")
        self.event_log.append(" ".join(str(p) for p in parts))


def _parse(text):
    text = text.strip()
    while text.startswith("!") or text.startswith("+"):
        text = text[1:]
    # Modo quotes a value with braces: {a value}.
    text = re.sub(r"\{([^{}]*)\}", lambda m: shlex.quote(m.group(1)), text)
    tokens = shlex.split(text)
    name, rest = tokens[0], tokens[1:]
    positional, named = _Positional(), {}
    for token in rest:
        if ":" in token:
            key, value = token.split(":", 1)
            named[key] = value
            positional.order.append((key, value))
        else:
            positional.append(token)
            positional.order.append((None, token))
    return name, positional, named


class _Positional(list):
    """A line's words with no name, and in `order` every word as written:
    [(name or None, value)]. Where a word goes depends on the word before
    it (see _BasicCommand._fill)."""

    def __init__(self, *args):
        list.__init__(self, *args)
        self.order = []


def _hint_int(raw, hints):
    """The integer 17.1 takes a word as: a hint matched
    without case, an integer as it is, any other word the first hint's."""
    words = [(n, w) for n, w in hints if not w.startswith("=")]
    for n, word in words:
        if raw.lower() == word.lower():
            return n
    if raw.lstrip("-").isdigit():
        return int(raw)
    return words[0][0]


def _operate(value, word):
    """A query operator applied to value: ?+n ?-n ?*n ?/n, a step ?+ or ?-
    (0.01, as a probe found at 3.0), ?(a|b) the choice after value's."""
    op = word[1:]
    if op.startswith("(") and op.endswith(")"):
        choices = op[1:-1].split("|")
        at = choices.index(value) + 1 if value in choices else 0
        return choices[at % len(choices)]
    if op in ("+", "-"):
        op += "0.01"
    sign, amount = op[0], float(op[1:])
    return {"+": value + amount, "-": value - amount, "*": value * amount,
            "/": value / amount}[sign]


class FakeChannel(object):
    def __init__(self, item, name, value):
        self.item = item
        self.name = name
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        old = self.value
        self.value = value
        if STATE is not None:
            STATE.record(lambda: setattr(self, "value", old),
                         "%s.%s" % (self.item.id, self.name), channel=True,
                         redo_fn=lambda: setattr(self, "value", value))


class UndoStep(object):
    def __init__(self, command):
        self.command = command
        self.changes = []       # (undo_fn, redo_fn or None), in order
        self.markers = 0        # markers recorded into this step


# Types that show in Modo's Items list (the locator super type).
LOCATOR_TYPES = ("mesh", "locator", "camera", "groupLocator", "sunLight",
                 "pointLight", "spotLight", "areaLight", "backdrop")

# Types the TD SDK hands back as a modo.Locator subclass, with .position,
# .rotation and .scale. A backdrop is a locator to Modo (mt_get_item reads
# its pos) but comes back a plain modo.Item (a test model, 2026-11-10).
TD_LOCATORS = tuple(t for t in LOCATOR_TYPES if t != "backdrop")


class FakeSelection(object):
    """A mesh's selected elements: .selected lists, as modo.MeshGeometry
    has them. A polygon or vertex has .index; an edge has .vertexIndices."""

    def __init__(self):
        self.polygons = types.SimpleNamespace(selected=())
        self.vertices = types.SimpleNamespace(selected=())
        self.edges = types.SimpleNamespace(selected=())

    def select(self, kind, indices):
        if kind == "edges":
            found = tuple(types.SimpleNamespace(vertexIndices=tuple(pair))
                          for pair in indices)
        else:
            found = tuple(types.SimpleNamespace(index=i) for i in indices)
        setattr(self, kind, types.SimpleNamespace(selected=found))


class FakeItem(object):
    def __init__(self, item_id, name, item_type, channels=None):
        self.id = item_id
        self.name = name
        self.type = item_type
        self.channels = {k: FakeChannel(self, k, v)
                         for k, v in (channels or {}).items()}
        self.transforms = {}    # "translation" -> FakeItem
        # Modo's name is the unique one the Items list shows, "Wheel (2)";
        # baseName is what the artist typed, "Wheel".
        self.baseName = name
        self.parent = None
        self._children = []
        self.geometry = FakeSelection()
        self.mesh = None        # FakeMesh: the base mesh, on a mesh item
        self.deformed = None    # FakeMesh: what an evaluated read gives
        # The world transform: a 3x3 matrix applied to a column vector, and
        # the offset, as Modo 17.1's Locator.WorldTransform returns them.
        self.world = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                      (0.0, 0.0, 0.0))
        self.source = None      # a mesh instance's source mesh
        # What Modo's surface sampler hands out: one list per
        # bin of segments, each (points, triangles), points in the item's
        # own space and repeated once per polygon around them, triangles
        # indexing the segment's own points from 0, as on 17.1. None: no
        # surface.
        self.surface = None

    # -- lx.object.Item, as far as the kit reads it -------------------------

    def Ident(self):
        return self.id

    def Context(self):
        return ("scene", STATE)

    def ChannelLookup(self, name):
        # As on 17.1: a channel the item doesn't have is a LookupError.
        if name == _Symbol.sICHAN_MESH_MESH and self.mesh is not None:
            return 1
        raise LookupError("channel not found: %s" % name)

    def channel(self, name):
        return self.channels.get(name)

    @property
    def channelNames(self):
        return list(self.channels)

    def children(self):
        return list(self._children)

    def isLocatorSuperType(self):
        return self.type in LOCATOR_TYPES

    def _xfrm(self, kind):
        if self.type not in TD_LOCATORS and self.type in LOCATOR_TYPES:
            raise AttributeError("'Item' object has no attribute %r" % kind)
        found = self.transforms.get(kind)
        if found is None:
            raise LookupError("no %s item" % kind)
        return found

    position = property(lambda self: self._xfrm("translation"))
    rotation = property(lambda self: self._xfrm("rotation"))
    scale = property(lambda self: self._xfrm("scale"))


class FakeMesh(object):
    """lx.object.Mesh, built from the lists meshmath reads: positions,
    polygons as vertex index lists, their type names, and edges as vertex
    pairs. Point ids are not indices, as in Modo, so a kit that mixes them
    up reads the wrong points. Anything but the read API it records in
    STATE.mesh_writes and refuses."""

    READS = {"PointCount", "EdgeCount", "PolygonCount", "PointAccessor",
             "PolygonAccessor", "EdgeAccessor"}

    def __init__(self, positions, polygons, types=None, edges=()):
        self.positions = [tuple(p) for p in positions]
        self.polygons = [list(p) for p in polygons]
        self.types = list(types or ["face"] * len(self.polygons))
        self.edges = [tuple(e) for e in edges]
        # What Modo's selection service holds for this mesh, by mode, in
        # the order the elements were picked: indices, or vertex pairs.
        self.selected = {"vertex": [], "edge": [], "polygon": []}
        # The select mark, by mode. On 17.1 (2026-11-08) it disagreed with
        # the selection: all six of a cube's polygons marked, one selected.
        self.marks = {"vertex": set(), "edge": set(), "polygon": set()}
        # "Point.Pos" -> how many times the kit called it.
        self.calls = {}

    def select(self, mode, elements):
        """Select these elements, as clicks in Modo do."""
        self.selected[mode] = [tuple(e) if mode == "edge" else e
                               for e in elements]

    def __getattr__(self, name):
        STATE.mesh_writes.append("Mesh.%s" % name)
        raise AttributeError("the fake mesh refuses %s" % name)

    def PointCount(self):
        return len(self.positions)

    def EdgeCount(self):
        return len(self.edges)

    def PolygonCount(self):
        return len(self.polygons)

    def PointAccessor(self):
        return _Accessor(self, "Point")

    def PolygonAccessor(self):
        return _Accessor(self, "Polygon")

    def EdgeAccessor(self):
        return _Accessor(self, "Edge")


def point_id(index):
    return 9000 + 7 * index


def polygon_id(index):
    return 7000 + 11 * index


class _Accessor(object):
    """A point, polygon or edge accessor: moved with SelectByIndex (or a
    point's Select by id), read with the getters below. Anything else is a
    write: recorded and refused."""

    READS = {"Point": {"SelectByIndex", "Select", "Index", "ID", "Pos",
                       "TestMarks"},
             "Polygon": {"SelectByIndex", "Select", "Index", "VertexCount",
                         "VertexByIndex", "Type", "TestMarks"},
             "Edge": {"SelectByIndex", "Endpoints", "TestMarks"}}
    MODES = {"Point": "vertex", "Polygon": "polygon", "Edge": "edge"}

    def __init__(self, mesh, kind):
        self._mesh = mesh
        self._kind = kind
        self._at = None

    def __getattribute__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        if name not in _Accessor.READS[object.__getattribute__(self, "_kind")]:
            STATE.mesh_writes.append("%s.%s" % (self._kind, name))
            raise AttributeError("the fake accessor refuses %s" % name)
        calls = object.__getattribute__(self, "_mesh").calls
        key = "%s.%s" % (object.__getattribute__(self, "_kind"), name)
        calls[key] = calls.get(key, 0) + 1
        return object.__getattribute__(self, name)

    def SelectByIndex(self, index):
        self._at = index

    def Select(self, element_id):
        self._at = ((element_id - 9000) // 7 if self._kind == "Point"
                    else (element_id - 7000) // 11)

    def Index(self):
        return self._at

    def ID(self):
        return point_id(self._at)

    def Pos(self):
        return self._mesh.positions[self._at]

    def VertexCount(self):
        return len(self._mesh.polygons[self._at])

    def VertexByIndex(self, index):
        return point_id(self._mesh.polygons[self._at][index])

    def Type(self):
        return _TYPE_CODES[self._mesh.types[self._at]]

    def Endpoints(self):
        a, b = self._mesh.edges[self._at]
        return point_id(a), point_id(b)

    def TestMarks(self, mode):
        if mode != SELECT_MARK:
            raise LookupError("the fake mesh only has the select mark")
        kind = _Accessor.MODES[self._kind]
        at = self._mesh.edges[self._at] if kind == "edge" else self._at
        if kind == "edge":
            at = tuple(sorted(at))
        return at in self._mesh.marks[kind]


class _LayerScan(object):
    """lx.object.LayerScan from the layer service's ScanAllocateItem, as the
    A probe found it on 17.1 (2026-11-11): one layer, the item's, whether
    or not it is selected; MeshEdit(0) its mesh opened for writing; points
    moved with SetPos hold only once Apply runs, and the change is in the
    running command's undo step."""

    def __init__(self, item, flags):
        self.item = item
        self.flags = flags
        self.moved = {}         # index -> position, until Apply
        self.changes = []       # SetMeshChange calls: (layer, flags)
        self.applied = 0

    def Count(self):
        return 1

    def MeshEdit(self, layer):
        if layer != 0:
            raise LookupError("no layer %r" % layer)
        return _EditMesh(self)

    def SetMeshChange(self, layer, flags):
        self.changes.append((layer, flags))

    def Apply(self):
        if STATE.scan_apply_fails:
            raise RuntimeError("bad result: FAILED")
        self.applied += 1
        positions = self.item.mesh.positions
        old = list(positions)
        for index, pos in self.moved.items():
            positions[index] = tuple(pos)
        new = list(positions)

        def put(values):
            positions[:] = values
        STATE.record(lambda: put(old), "points %s" % self.item.id,
                     redo_fn=lambda: put(new))


class _EditMesh(object):
    """The mesh a layer scan opened for writing: points only."""

    def __init__(self, scan):
        self._scan = scan

    def PointCount(self):
        return len(self._scan.item.mesh.positions)

    def PointAccessor(self):
        return _EditPoint(self._scan)


class _EditPoint(object):
    def __init__(self, scan):
        self._scan = scan
        self._at = None

    def SelectByIndex(self, index):
        if not 0 <= index < len(self._scan.item.mesh.positions):
            raise LookupError("no point %r" % index)
        self._at = index

    def Pos(self):
        return self._scan.moved.get(self._at,
                                    self._scan.item.mesh.positions[self._at])

    def SetPos(self, pos):
        self._scan.moved[self._at] = tuple(float(v) for v in pos)


def _make_layer_service(state):
    class Layer(object):
        def __init__(self):
            state.check_thread("lx.service.Layer")

        def ScanAllocateItem(self, item, flags):
            if getattr(item, "mesh", None) is None:
                raise LookupError("not a mesh layer")
            scan = _LayerScan(item, flags)
            state.layer_scans.append((item.id, flags, scan))
            return scan

    return Layer


# ModeCompose("select", None) on 17.1 (2026-11-08).
SELECT_MARK = 4096

# The selection service's type codes on 17.1 (2026-11-08), by sSELTYP_*.
SELECTION_TYPES = {"vertex": 1447383640, "edge": 1162102597,
                   "polygon": 1347374169, "item": 1230259533}


class _ChannelRead(object):
    """scene.Channels(action, time). The edit action gives the base mesh;
    None, the evaluated one, which is the only read WorldTransform takes."""

    def __init__(self, action, time):
        self.action = action
        self.time = time
        STATE.channel_reads.append((action, time))

    def ValueObj(self, item, index):
        if index != 1 or item.mesh is None:
            raise LookupError("no such channel")
        if self.action is None and item.deformed is not None:
            return item.deformed
        return item.mesh


class _SurfaceItem(object):
    """lx.object.SurfaceItem: GetSurface(chan, morph) is the item's surface,
    read through that channel read's action."""

    def __init__(self, item):
        self._item = item

    def GetSurface(self, chan, morph):
        if self._item.surface is None:
            raise LookupError("no surface")
        return _Surface(self._item, chan.action)


class _Surface(object):
    """lx.object.Surface, and its bins as lx.object.TableauSurface."""

    def __init__(self, item, action):
        self._item = item
        self._action = action
        self._vertex = None
        # A surface made from the mesh as it is now, so a read
        # after a write sees the write, as on 17.1.
        surface = item.surface
        self._bins = surface(item) if callable(surface) else surface

    def BinCount(self):
        return len(self._bins)

    def BinByIndex(self, index):
        return _Bin(self, self._bins[index])


class _Bin(object):
    def __init__(self, surface, data):
        self._surface = surface
        self._segments = data
        self._vertex = None

    def Bound(self):
        points = [p for segment, _ in self._segments for p in segment]
        xs, ys, zs = zip(*points) if points else ((0,), (0,), (0,))
        return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))

    def SetVertex(self, vertex):
        self._vertex = vertex

    def Sample(self, bound, scale, soup):
        STATE.check_thread("TableauSurface.Sample")
        if self._vertex is None or self._vertex.features != [
                (_Symbol.iTBLX_BASEFEATURE, _Symbol.sTBLX_FEATURE_POS)]:
            raise TypeError("invalid argument")
        STATE.surface_samples.append((self._surface._item.id,
                                      self._surface._action))
        for number, (points, triangles) in enumerate(self._segments):
            if not soup.soup_Segment(number, 3):
                continue
            # What soup_Vertex returns is ignored: a triangle names its
            # points by their place in the segment, from 0.
            for p in points:
                STATE.sampled_points += 1
                soup.soup_Vertex(_SoupVertex(p))
            for a, b, c in triangles:
                soup.soup_Polygon(a, b, c)


class _SoupVertex(object):
    """The storage a soup's Vertex gets: empty until typed and sized."""

    def __init__(self, point):
        self._point = point
        self._kind = self._size = None

    def setType(self, kind):
        self._kind = kind

    def setSize(self, size):
        self._size = size

    def get(self):
        if self._kind != "f" or not self._size:
            raise IndexError("tuple index out of range")
        return tuple(self._point[:self._size])


class _LineStorage(object):
    """What Image.GetLine returns: untyped until setType and setSize."""

    def __init__(self, row):
        self._row = bytes(row)
        self._kind = self._size = None

    def setType(self, kind):
        self._kind = kind

    def setSize(self, size):
        self._size = size

    def get(self):
        if self._kind is None:
            raise RuntimeError("Unknown storage type")
        return tuple(self._row[:self._size])


class _TableauVertex(object):
    def __init__(self):
        self.features = []

    def AddFeature(self, kind, name):
        self.features.append((kind, name))
        return len(self.features) - 1


class _TriangleSoup(object):
    """lxifc.TriangleSoup."""

    def soup_Segment(self, segment, kind):
        return True


def _make_tableau_service(state):
    class Tableau(object):
        def AllocVertex(self):
            return _TableauVertex()

    return Tableau


class _ItemGraph(object):
    """A graph: meshInst as the instances' sources; any other graph holds
    the links AddLink made (state.links[name], [(from, to)]), undoable as
    on 17.1 inside a command."""

    def __init__(self, name):
        self.name = name

    def _links(self):
        return STATE.links.setdefault(self.name, []) if STATE is not None else []

    def AddLink(self, item_from, item_to):
        links = self._links()
        pair = (item_from, item_to)
        links.append(pair)
        STATE.record(lambda: links.remove(pair), "link %s" % self.name,
                     redo_fn=lambda: links.append(pair))

    def FwdCount(self, item):
        return len([1 for f, _t in self._links() if f is item])

    def FwdByIndex(self, item, index):
        return [t for f, t in self._links() if f is item][index]

    def RevCount(self, item):
        if self.name == "meshInst":
            return 1 if item.source is not None else 0
        return len([1 for _f, t in self._links() if t is item])

    def RevByIndex(self, item, index):
        if self.name == "meshInst":
            return item.source
        return [f for f, t in self._links() if t is item][index]


class _SceneObject(object):
    """lx.object.Scene, from an item's Context()."""

    def __init__(self, context):
        self._context = context

    def Channels(self, action, time):
        return _ChannelRead(action, time)

    def GraphLookup(self, name):
        return _ItemGraph(name)


class _Image(object):
    """lx.object.Image, as far as the backdrop op reads one."""

    def __init__(self, width, height, source=None, pixels=None):
        self.width, self.height = width, height
        self.source = source    # the image Resample filled this one from
        self.pixels = pixels    # rows of RGBA bytes

    def GetLine(self, y, kind, buf):
        """As a probe found 17.1: the buffer passed in stays
        empty; the storage returned holds the line once typed and sized."""
        if self.pixels is None or kind != _Symbol.iIMV_RGBA:
            raise RuntimeError("bad result: FAILED")
        return _LineStorage(self.pixels[y])

    def Size(self):
        return (self.width, self.height)

    def Format(self):
        return 3                # iIMV_RGB... as 17.1 answered for an RGB PNG


def _make_image_service(state):
    """Load, Create, Resample and Save, as a probe found them on
    17.1: Load reads any image Modo knows (here, a PNG's size); Save writes
    the format named."""

    class Image(object):
        def __init__(self):
            state.check_thread("lx.service.Image")

        def LoadNoCache(self, path):
            state.image_loads.append(path)
            try:
                with open(path, "rb") as handle:
                    data = handle.read(32)
            except OSError:
                raise RuntimeError("bad result: NOTFOUND")
            if data[12:16] != b"IHDR":
                raise RuntimeError("bad result: FAILED")
            return _Image(int.from_bytes(data[16:20], "big"),
                          int.from_bytes(data[20:24], "big"),
                          pixels=state.image_pixels.get(path))

        Load = LoadNoCache

        def Create(self, width, height, kind, max_index):
            return _Image(width, height)

        def Resample(self, dest, source, hint):
            dest.source = source

        def Save(self, image, path, fmt, monitor):
            if state.image_save_fails:
                raise RuntimeError("bad result: FAILED")
            with open(path, "wb") as handle:
                handle.write(png(image.width, image.height))
            state.image_saves.append((path, fmt, image.Size()))

    return Image


class FakeScene(object):
    def __init__(self):
        self.items = []
        # Modo names an unsaved scene "Untitled", "Untitled (2)", ...
        self.name = "Untitled"
        self.changes = 0
        self.unreadable = False  # the scene service can't say 
        self.filename = None
        self.saved_to = []
        self.write_files = True
        self.dirty = False
        self.selected = []
        self.fps = 24.0
        self.range = (0, 120)
        self.time = 0.0         # seconds, as select.time reads it
        # Modo's selection mode: vertex, edge, polygon or item.
        self.select_mode = "item"
        self.viewport = True    # False: gl.snapshot writes nothing
        self.viewport_size = (1280, 720)    # what gl.snapshot writes

    def add(self, item_id, name, item_type="mesh", channels=None, parent=None):
        item = FakeItem(item_id, name, item_type, channels)
        self.items.append(item)
        if parent is not None:
            item.parent = parent
            parent._children.append(item)
        return item

    def find(self, ref):
        for item in self.items:
            if item.id == ref:
                return item
        return None


def _make_modo(state):
    modo = types.ModuleType("modo")

    class Scene(object):
        def __init__(self):
            state.check_thread("modo.Scene")
            self._scene = state.scene

        @property
        def filename(self):
            return self._scene.filename

        def item(self, ref):
            found = self._scene.find(ref)
            if found is None:
                for item in self._scene.items:
                    if item.name == ref:
                        return item
                raise LookupError("item not found: %s" % ref)
            return found

        def iterItems(self, itype=None):
            return iter(list(self._scene.items))

        def items(self, itype=None):
            return [i for i in self._scene.items if itype in (None, i.type)]

        @property
        def selected(self):
            return list(self._scene.selected)

        @property
        def fps(self):
            return self._scene.fps

        @property
        def sceneRange(self):
            return self._scene.range

        @property
        def meshes(self):
            return [i for i in self._scene.items if i.type == "mesh"]

    class Locator(object):
        def __init__(self, item):
            # As on 17.1 for a mesh: the wrapper refuses it.
            if getattr(item, "type", None) == "mesh":
                raise TypeError("Locator item is required")
            self._item = item

        @property
        def position(self):
            return self._xfrm("translation")

        @property
        def rotation(self):
            return self._xfrm("rotation")

        @property
        def scale(self):
            return self._xfrm("scale")

        def _xfrm(self, kind):
            found = self._item.transforms.get(kind)
            if found is None:
                raise LookupError("no %s item" % kind)
            return found

    class MeshGeometry(object):
        """Only the write the kit guards against in read-only jobs."""

        def setMeshEdits(self, *args):
            state.record(lambda: None, "setMeshEdits")

    modo.Scene = Scene
    modo.Locator = Locator
    modo.MeshGeometry = MeshGeometry
    modo.channel = types.SimpleNamespace(Channel=FakeChannel)
    # modo.Item(raw) wraps what lx.object hands back; fake items need none.
    modo.Item = lambda raw: raw
    modo.scene = types.SimpleNamespace(current=Scene)
    return modo


def _install_scene_commands(state):
    """item.channel, scene.save, scene.saveAs and prim.cube's stand-in."""

    class ItemChannel(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("name", "value", "mode", "item"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            # item: left out, the selected item, as on 17.1.
            item = (state.scene.find(self.dyna_String(3)) if self.dyna_IsSet(3)
                    else (state.scene.selected or [None])[0])
            if item is None:
                raise RuntimeError("item not found")
            channel = item.channel(self.dyna_String(0))
            if channel is None:
                raise RuntimeError("channel not found")
            raw = self.dyna_String(1)
            if raw == "?":
                return channel.get()
            if raw.startswith("?"):
                # A query operator, as a probe found them on 17.1 (2026-11-08).
                channel.set(_operate(channel.get(), raw))
                return None
            try:
                value = float(raw)
            except ValueError:
                value = raw
            channel.set(value)

    class SceneSave(_BasicCommand):
        def basic_Execute(self, msg, flags):
            if not state.scene.filename:
                raise RuntimeError("the scene has no file name")
            if not state.scene.dirty:
                # As on 17.1: nothing changed since the last save.
                raise RuntimeError("command:scene.save ")
            state.scene.saved_to.append(state.scene.filename)
            if state.scene.write_files:
                with open(state.scene.filename, "w") as handle:
                    handle.write("lxo")

    class SceneSaveAs(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("filename", "format", "export"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            path = self.dyna_String(0)
            if path.startswith("Z:"):
                raise RuntimeError("could not write %s" % path)
            state.scene.filename = path
            # A probe (2026-11-08): a save names the scene after its file.
            state.scene.name = path.replace("\\", "/").rsplit("/", 1)[-1]
            state.scene.changes = 0
            state.scene.saved_to.append(path)
            if state.scene.write_files:
                with open(path, "w") as handle:
                    handle.write("lxo")

    class Fails(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("value", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            state.out("Fails: the value is out of range")
            raise RuntimeError("Command failed")

    class Blind(_BasicCommand):
        """Takes a query operator and can't be asked for the value it set."""

        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("value", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            raw = self.dyna_String(0)
            if raw == "?":
                raise RuntimeError("Command failed")
            state.scene.blind = _operate(getattr(state.scene, "blind", 0.0), raw)

    class Noop(_BasicCommand):
        def basic_Execute(self, msg, flags):
            pass

    class Cube(_BasicCommand):
        def basic_Execute(self, msg, flags):
            n = len(state.scene.items) + 1
            item = state.scene.add("mesh%03d" % n, "Mesh %d" % n)
            state.record(lambda: state.scene.items.remove(item), item.id,
                         redo_fn=lambda: state.scene.items.append(item))
            state.scene_event("sil_ItemAdd", item)

    class AppUndo(_BasicCommand):
        def basic_Execute(self, msg, flags):
            # As on 17.1: refused from a script or inside another command.
            if state._depth > 1:
                raise RuntimeError("This command cannot be used inside a "
                                   "script or another command")
            state.undo(silent=state.silent_markers)

    class AppRedo(_BasicCommand):
        def basic_Execute(self, msg, flags):
            if state._depth > 1:
                raise RuntimeError("This command cannot be used inside a "
                                   "script or another command")
            state.redo()

    class SelectTime(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("time", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            if self.dyna_String(0) == "?":
                return state.scene.time
            state.scene.time = float(self.dyna_String(0))

    class Snapshot(_BasicCommand):
        """gl.snapshot as on 17.1: a file name, no size; the image is the
        viewport's size. With no 3D viewport, Modo writes nothing."""

        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("filename", "format"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            if not state.scene.viewport:
                state.out("gl.snapshot: no 3D viewport to capture")
                return
            with open(self.dyna_String(0), "wb") as handle:
                handle.write(png(*state.scene.viewport_size))

    class Projection(_BasicCommand):
        """view3d.projection as on 17.1: a word matched
        without case, or an integer as it is; any other word is taken as
        the first (frt became top). ? answers the word."""

        WORDS = ("top", "bot", "bck", "fnt", "rgt", "lft", "psp")

        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("type", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            raw = self.dyna_String(0)
            if raw == "?":
                return getattr(state.scene, "projection", "psp")
            low = raw.lower()
            if low in self.WORDS:
                state.scene.projection = low
            elif low.lstrip("-").isdigit():
                n = int(low)
                state.scene.projection = (self.WORDS[n] if 0 <= n < len(self.WORDS)
                                          else str(n))
            else:
                state.scene.projection = self.WORDS[0]

    def hinted(raw, hints, values):
        """A word as 17.1 takes it: a hint matched
        without case, an integer as it is, any other word the first."""
        words = [w for _n, w in hints if not w.startswith("=")]
        for word in words:
            if raw.lower() == word.lower():
                return word
        if raw.lstrip("-").isdigit():
            n = int(raw)
            return next((w for m, w in hints if m == n and not
                         w.startswith("=")), str(n))
        return words[0]

    class ShadingStyle(_BasicCommand):
        """view3d.shadingStyle style bgmesh: style can be asked with ?,
        bgmesh can't (command argument parsing failed, as on 17.1)."""

        STYLE = [(0, "wire"), (3, "shaded"), (5, "advgl"), (7, "shd1")]
        BGMESH = [(0, "active"), (1, "inactive")]

        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("style", _Symbol.sTYPE_STRING)
            self.dyna_Add("bgmesh", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            style = self.dyna_String(0)
            if self.dyna_IsSet(1) and self.dyna_String(1) == "?":
                raise RuntimeError("command argument parsing failed")
            if style == "?":
                return getattr(state.scene, "shading", "advgl")
            state.scene.shading = hinted(style, self.STYLE, None)
            if self.dyna_IsSet(1):
                state.scene.bgmesh = hinted(self.dyna_String(1), self.BGMESH, None)

    class ToolSet(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("tool", _Symbol.sTYPE_STRING)
            self.dyna_Add("mode", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            if self.dyna_String(1) == "on":
                state.tools_on.add(self.dyna_String(0))
            else:
                state.tools_on.discard(self.dyna_String(0))

    class ToolAttr(_BasicCommand):
        """tool.attr as on 17.1: with the tool off it
        fails for any word; with it on, a word is taken as hinted() says and
        ? answers the word."""

        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("tool", "attr", "value"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            tool, attr, raw = (self.dyna_String(i) for i in range(3))
            if tool not in state.tools_on:
                raise RuntimeError("command argument parsing failed")
            if raw == "?":
                return state.tool_attrs.get((tool, attr))
            entry = [a for a in state.tools[tool] if a[0] == attr][0]
            hints = entry[3] if len(entry) > 3 else []
            state.tool_attrs[(tool, attr)] = (hinted(raw, hints, None)
                                              if hints else raw)

    class Three(_BasicCommand):
        """test.three a b c: three optional arguments, none of which can be
        asked with ?. state.three is what each was given."""

        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("a", "b", "c"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            got = {name: self.dyna_String(i) for i, name in enumerate("abc")
                   if self.dyna_IsSet(i)}
            if "?" in got.values():
                raise RuntimeError("command argument parsing failed")
            state.three = got

    def added(item, also=()):
        """An item made inside the running command: one change in its step,
        and the item listener's ItemAdd."""
        made = [item] + list(also)

        def remove():
            for one in made:
                if one in state.scene.items:
                    state.scene.items.remove(one)

        def restore():
            for one in made:
                if one not in state.scene.items:
                    state.scene.items.append(one)
        state.record(remove, item.id, redo_fn=restore)
        state.scene_event("sil_ItemAdd", item)

    def selected(items):
        old = list(state.scene.selected)
        state.scene.selected = list(items)
        state.record(lambda: setattr(state.scene, "selected", old), "selection",
                     redo_fn=lambda: setattr(state.scene, "selected", list(items)))

    def mode(new):
        """The selection mode, changed inside the running command."""
        old = state.scene.select_mode
        state.scene.select_mode = new
        state.record(lambda: setattr(state.scene, "select_mode", old), "mode",
                     redo_fn=lambda: setattr(state.scene, "select_mode", new))

    class ClipAddStill(_BasicCommand):
        """clip.addStill as on 17.1: an image item
        "<stem>:videoStill001" named after the file, holding its path. For a
        path a clip already holds, spelled the same, no second clip (a
        probe)."""

        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("filename", _Symbol.sTYPE_STRING)
            self.dyna_Add("layered", _Symbol.sTYPE_BOOLEAN)

        def basic_Execute(self, msg, flags):
            import os
            path = self.dyna_String(0)
            if not os.path.isfile(path):
                raise RuntimeError("Could not load image: %s" % path)
            if any(i.type == "videoStill" and i.channel("filename").get() == path
                   for i in state.scene.items):
                return
            stem = os.path.splitext(os.path.basename(path))[0]
            n = len([i for i in state.scene.items if i.type == "videoStill"]) + 1
            item = state.scene.add("%s:videoStill%03d" % (stem, n), stem,
                                   "videoStill", channels={"filename": path})
            added(item)

    class ItemCreate(_BasicCommand):
        """item.create backdrop as on 17.1: the item,
        its transform items, the channels a backdrop has, and the new item
        selected. Only backdrop and locator."""

        BACKDROP = {"projection": "front", "pixelSize": 0.01, "aspect": 1,
                    "transparency": 0.0, "hidePersp": 0, "visible": "default"}

        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("type", "name"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            kind = self.dyna_String(0)
            if kind not in ("backdrop", "locator"):
                raise RuntimeError("item.create: no type %s in the fake" % kind)
            n = len(state.scene.items) + 1
            name = (self.dyna_String(1) if self.dyna_IsSet(1)
                    else {"backdrop": "Backdrop Item"}.get(kind, "Locator"))
            item = state.scene.add("%s%03d" % (kind, n), name, kind,
                                   channels=dict(self.BACKDROP) if kind == "backdrop"
                                   else {"visible": "default"})
            move = state.scene.add("translation%03d" % (n + 1),
                                   "Position (%s)" % name, "translation",
                                   channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})
            item.transforms["translation"] = move
            added(item, [move])
            selected([item])
            mode("item")    # the review's probe: item.create leaves item mode

    class TransformChannel(_BasicCommand):
        """transform.channel pos.X 0.5 item:<id>: the item's transform."""

        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("name", "value", "mode", "item"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            # item: left out, the selected item, as on 17.1.
            item = (state.scene.find(self.dyna_String(3)) if self.dyna_IsSet(3)
                    else (state.scene.selected or [None])[0])
            if item is None:
                raise RuntimeError("item not found")
            name = self.dyna_String(0)
            kind = {"pos": "translation", "rot": "rotation",
                    "scl": "scale"}[name.split(".")[0]]
            item.transforms[kind].channel(name).set(float(self.dyna_String(1)))

    class SelectDrop(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("type", _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            selected([])
            if self.dyna_String(0) == "item":
                mode("item")    # the review's probe; select.item add doesn't

    class SelectTypeFrom(_BasicCommand):
        """select.typeFrom polygon: the selection mode (its first type)."""

        def __init__(self):
            _BasicCommand.__init__(self)
            self.dyna_Add("typelist", _Symbol.sTYPE_STRING)
            self.dyna_Add("enable", _Symbol.sTYPE_BOOLEAN)

        def basic_Execute(self, msg, flags):
            first = self.dyna_String(0).split(";")[0]
            if first not in SELECTION_TYPES:
                raise RuntimeError("select.typeFrom: no type %s" % first)
            mode(first)

    class SelectItem(_BasicCommand):
        def __init__(self):
            _BasicCommand.__init__(self)
            for name in ("item", "mode"):
                self.dyna_Add(name, _Symbol.sTYPE_STRING)

        def basic_Execute(self, msg, flags):
            item = state.scene.find(self.dyna_String(0))
            if item is None:
                raise RuntimeError("item not found")
            mode = self.dyna_String(1) if self.dyna_IsSet(1) else "set"
            keep = list(state.scene.selected) if mode == "add" else []
            selected(keep + [item])

    state.builtin["clip.addStill"] = ClipAddStill
    state.builtin["item.create"] = ItemCreate
    state.builtin["transform.channel"] = TransformChannel
    state.builtin["select.drop"] = SelectDrop
    state.builtin["select.typeFrom"] = SelectTypeFrom
    state.builtin["select.item"] = SelectItem
    state.builtin["test.three"] = Three
    state.tools_on = set()
    state.builtin["tool.set"] = ToolSet
    state.builtin["view3d.shadingStyle"] = ShadingStyle
    state.tool_attrs = {}
    state.builtin["view3d.projection"] = Projection
    state.builtin["tool.attr"] = ToolAttr
    state.builtin["select.time"] = SelectTime
    state.builtin["gl.snapshot"] = Snapshot
    state.builtin["item.channel"] = ItemChannel
    state.builtin["scene.save"] = SceneSave
    state.builtin["scene.saveAs"] = SceneSaveAs
    state.builtin["test.fails"] = Fails
    state.builtin["test.blind"] = Blind
    state.builtin["deformer.freeze"] = Noop
    state.builtin["scene.open"] = Noop
    state.builtin["scene.new"] = Noop
    state.builtin["scene.close"] = Noop
    state.builtin["scene.revert"] = Noop
    state.builtin["test.cube"] = Cube
    state.builtin["app.undo"] = AppUndo
    state.builtin["app.redo"] = AppRedo


def _make_undo_service(state):
    class Undo(object):
        def __init__(self):
            state.check_thread("lx.service.Undo")

        def Record(self, undo_obj):
            state.record_marker(undo_obj)

        def State(self):
            return state.undo_state

    return Undo


def _make_listener_service(state):
    class Listener(object):
        def __init__(self):
            state.check_thread("lx.service.Listener")

        def AddListener(self, obj):
            state.listeners.append(obj)

        def RemoveListener(self, obj):
            # As on 17.1: only the object that was added.
            if obj not in state.listeners:
                raise LookupError("not found")
            state.listeners.remove(obj)

    return Listener


class _SceneItemListener(object):
    """lxifc.SceneItemListener: Modo calls only what a subclass defines."""


class _Undo(object):
    """lxifc.Undo."""

    def undo_Forward(self):
        pass

    def undo_Reverse(self):
        pass


def _make_command_service(state):
    """BlockBegin / BlockEnd, as a probe found them on 17.1."""

    class Command(object):
        def __init__(self):
            state.check_thread("lx.service.Command")

        def BlockBegin(self, name, flags):
            state.blocks.append(name)
            state.open_blocks += 1

        def BlockEnd(self):
            # As on 17.1: ending a block that isn't open fails so.
            if not state.open_blocks:
                raise RuntimeError("bad result: FAILED")
            state.open_blocks -= 1

        def SpawnFromString(self, text):
            """item.channel only, its value typed by channel as on 17.1: rot.* an angle, the rest a distance. Others are
            CMD_DATATYPE_UNAVAILABLE, as tool.attr is."""
            name, positional, named = _parse(text)
            if text in getattr(state, "unspawnable", ()):
                raise RuntimeError("bad result: CMD_DATATYPE_UNAVAILABLE")
            if name in state.described and name != "item.channel":
                info = state.described[name]
                key = "spawn:" + text
                # A word given in the line is parsed, not run: a hinted
                # argument holds the integer Modo took the word as (the
                # Shade 0, inactve 0).
                cmd = _BasicCommand()
                for a in info["args"]:
                    cmd.dyna_Add(a[0], _Symbol.sTYPE_STRING)
                cmd._fill(positional, named)
                rows = []
                for index, a in enumerate(info["args"]):
                    hints = info["hints"].get(a[0], [])
                    value = 0
                    if index in cmd._values and isinstance(hints, list) and hints:
                        value = _hint_int(cmd._values[index], hints)
                    rows.append((a[0], a[1], value, hints,
                                 info["popups"].get(a[0]), a[2]))
                state.tools[key] = rows
                return (0, 0, ("spawned", key))
            if name != "item.channel":
                raise RuntimeError("bad result: CMD_DATATYPE_UNAVAILABLE")
            channel = named.get("name") or (positional[0] if positional else "")
            kind = "angle" if channel.startswith("rot.") else "distance"
            key = "spawn:" + text
            state.tools[key] = [("name", "string", channel), ("value", kind, ""),
                                ("mode", "integer", 0), ("item", "&item", "")]
            # On 17.1 the command is the tuple's last item.
            return (0, 0, ("spawned", key))

    return Command


def png(width, height):
    """A valid PNG header and IHDR of that size; enough to read its size."""
    import struct
    import zlib
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    signature = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    return (signature + struct.pack(">I", 13) + b"IHDR" + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))


class _Locator(object):
    """lx.object.Locator: GetTransformItem raises LookupError when the item
    has no such transform yet. Reading never creates one."""

    def __init__(self, item):
        self._item = item

    def GetTransformItem(self, symbol):
        kind = {_Symbol.iXFRM_POSITION: "translation",
                _Symbol.iXFRM_ROTATION: "rotation",
                _Symbol.iXFRM_SCALE: "scale"}[symbol]
        found = self._item.transforms.get(kind)
        if found is None:
            raise LookupError("no %s item" % kind)
        return found

    def WorldTransform(self, chan):
        # As on 17.1 (2026-11-08): the edit action's read has no world
        # matrix and raises LookupError; the evaluated read (None) has it.
        if chan.action is not None:
            raise LookupError("not found")
        return self._item.world


def _make_selection(state):
    class Selection(object):
        def __init__(self):
            state.check_thread("lx.service.Selection")

        def GetTime(self):
            return state.scene.time

        def LookupType(self, name):
            return SELECTION_TYPES[name]

        def _packets(self, code):
            mode = {v: k for k, v in SELECTION_TYPES.items()}[code]
            return [(item, mode, element) for item in state.scene.items
                    if item.mesh is not None
                    for element in item.mesh.selected.get(mode, ())]

        def Count(self, code):
            return len(self._packets(code))

        def ByIndex(self, code, index):
            state.packet_reads += 1
            return self._packets(code)[index]

        def Allocate(self, name):
            return name

        def CurrentType(self, types):
            # As on 17.1: a list is refused, only a storage of ints works.
            if not isinstance(types, _Storage):
                raise TypeError("invalid argument")
            asked = [t for t in types.values if t]
            current = SELECTION_TYPES.get(state.scene.select_mode)
            return current if current in asked else 0

    return Selection


def _make_mesh_service(state):
    class Mesh(object):
        def __init__(self):
            state.check_thread("lx.service.Mesh")

        def ModeCompose(self, set_marks, clear_marks):
            if (set_marks, clear_marks) != ("select", None):
                raise LookupError("the fake knows only the select mark")
            return SELECT_MARK

    return Mesh


class _PacketTranslation(object):
    """lx.object.{Polygon,Edge,Vertex}PacketTranslation over the fake's
    packets, (item, mode, element). Elements come back as ids, as in Modo."""

    def __init__(self, allocated):
        self.allocated = allocated

    def Item(self, packet):
        return packet[0]

    def Mesh(self, packet):
        return packet[0].mesh

    def Polygon(self, packet):
        return polygon_id(packet[2])

    def Vertex(self, packet):
        return point_id(packet[2])

    def Vertices(self, packet):
        a, b = packet[2]
        return point_id(a), point_id(b)


class _Storage(object):
    """lx.object.storage(type, size): a typed C array."""

    def __init__(self, kind, size):
        self.kind = kind
        self.values = [0] * size

    def set(self, values):
        self.values = list(values)


def _make_log(state):
    """The shape checked on Modo 17.1 (2026-11-08): the event log is the
    primary subsystem, entries wrapped as lx.object.LogEntry."""

    class Primary(object):
        def Name(self):
            return "primary"

        def EntryCount(self):
            return len(state.event_log)

        def EntryByIndex(self, index):
            return ("entry", state.event_log[index])

    class Log(object):
        def __init__(self):
            state.check_thread("lx.service.Log")

        def PrimarySubSystem(self):
            return Primary()

    return Log


class _LogEntry(object):
    """An entry is a line, or (line, [children]): a probe found a script run with
    @ writes its own lines as children of the line naming the script."""

    def __init__(self, raw):
        value = raw[1]
        if isinstance(value, tuple):
            self._text, self._children = value[0], list(value[1])
        else:
            self._text, self._children = value, []

    def Message(self):
        return self._text

    def ChildCount(self):
        return len(self._children)

    def ChildByIndex(self, index):
        return ("entry", self._children[index])


def _make_host(state):
    class Factory(object):
        def __init__(self, name):
            self._name = name

        def Name(self):
            return self._name

        def UserName(self):
            return self._name.replace(".", " ").title()

        def Spawn(self):
            return ("tool", self._name)

    class Host(object):
        def __init__(self):
            state.check_thread("lx.service.Host")

        def NumServers(self, interface):
            return len(state.tools) if interface == _Symbol.u_TOOL else 0

        def ServerByIndex(self, interface, index):
            return Factory(sorted(state.tools)[index])

        def LookupServer(self, interface, name, flags):
            if name not in state.tools:
                raise LookupError(name)
            return Factory(name)

    return Host


class _Attributes(object):
    def __init__(self, spawned):
        self._attrs = STATE.tools[spawned[1]]

    def Count(self):
        return len(self._attrs)

    def Name(self, index):
        return self._attrs[index][0]

    def Lookup(self, name):
        for index, attr in enumerate(self._attrs):
            if attr[0] == name:
                return index
        raise LookupError(name)

    def TypeName(self, index):
        return self._attrs[index][1]

    def Type(self, index):
        value = self._attrs[index][2]
        if isinstance(value, int):
            return _Symbol.i_TYPE_INTEGER
        if isinstance(value, float):
            return _Symbol.i_TYPE_FLOAT
        return _Symbol.i_TYPE_STRING

    def Hints(self, index):
        """[(int, word)], the argument's text hints; [] when it has none."""
        attr = self._attrs[index]
        hints = attr[3] if len(attr) > 3 else []
        if hints == "raise":
            raise RuntimeError("bad result: FAILED")
        if hints == "hang":
            # 17.1: Hints on tool.attr's variable value
            # opens a dialog and Modo never comes back.
            STATE.crashes.append("Hints read on a variable argument")
            raise RuntimeError("Modo would hang here")
        return list(hints)

    def GetInt(self, index):
        return self._attrs[index][2]

    def GetFlt(self, index):
        return self._attrs[index][2]

    def GetString(self, index):
        return self._attrs[index][2]


class _Command(object):
    """lx.object.Command over a spawned command: its arguments' flags."""

    def __init__(self, spawned):
        self._attrs = STATE.tools[spawned[1]]

    def ArgFlags(self, index):
        attr = self._attrs[index]
        return attr[5] if len(attr) > 5 else 0


class _AttributesUI(object):
    """lx.object.AttributesUI over a spawned command or tool."""

    def __init__(self, spawned):
        self._attrs = STATE.tools[spawned[1]]

    def UIValueHints(self, index):
        attr = self._attrs[index]
        return _UIValueHints(attr[4] if len(attr) > 4 else None)


class _UIValueHints(object):
    """lx.object.UIValueHints: a popup's words. On 17.1 an argument with no
    popup still answers, with PopCount 0."""

    def __init__(self, words):
        self._words = words

    def PopCount(self):
        if self._words == "raise":
            raise RuntimeError("bad result: FAILED")
        if self._words == "crash":
            # 17.1: scene.tagAdd's taglist.
            STATE.crashes.append("PopCount on a popup that crashes Modo")
            raise RuntimeError("Modo would crash here")
        return len(self._words or [])

    def PopInternalName(self, index):
        return self._words[index]


class _Symbol(object):
    u_TOOL = "tool"
    iUNDO_INVALID = 0
    iUNDO_ACTIVE = 1
    iUNDO_SUSPEND = 2
    iXFRM_SCALE = 0
    iXFRM_ROTATION = 1
    iXFRM_POSITION = 2
    iIMV_RGBA = 4
    i_TYPE_INTEGER = 1
    i_TYPE_FLOAT = 2
    i_TYPE_STRING = 3
    fCMD_UI = 0x00000400
    fCMD_MODEL = 0x00000001
    fCMD_UNDO = 0x00000004
    fCMDARG_OPTIONAL = 0x00000010
    fCMDARG_QUERY = 0x00000020
    sTYPE_INTEGER = "integer"
    sTYPE_STRING = "string"
    sTYPE_BOOLEAN = "boolean"
    s_ACTIONLAYER_EDIT = "edit"
    sSELTYP_VERTEX = "vertex"
    sSELTYP_EDGE = "edge"
    sSELTYP_POLYGON = "polygon"
    sSELTYP_ITEM = "item"
    sICHAN_MESH_MESH = "mesh"
    sGRAPH_MESHINST = "meshInst"
    iTBLX_BASEFEATURE = 1
    iPROCESS_FAST = 0
    iPROCESS_MEDIUM = 1
    iPROCESS_ACCURATE = 2
    sTBLX_FEATURE_POS = "pos"
    # A probe on 17.1 (2026-11-11).
    f_LAYERSCAN_EDIT_VERTS = 273
    f_MESHEDIT_POSITION = 1
    f_MESHEDIT_POINTS = 2
    iPTYP_FACE = 1178682181
    iPTYP_SUBD = 1398096452
    iPTYP_PSUB = 1347638594
    iPTYP_CURV = 1129665110
    iPTYP_CURVE = 1129665110
    iPTYP_TEXT = 1413830740


# Polygon type names as the fake mesh is built with them, to Modo's codes.
_TYPE_CODES = {"face": _Symbol.iPTYP_FACE, "subd": _Symbol.iPTYP_SUBD,
               "psub": _Symbol.iPTYP_PSUB, "curve": _Symbol.iPTYP_CURVE,
               "text": _Symbol.iPTYP_TEXT}


class _Result(object):
    OK = 0


def _make_platform(state):
    class Platform(object):
        def __init__(self):
            state.check_thread("lx.service.Platform")

        def DoWhenUserIsIdle(self, visitor, flags):
            state.check_thread("DoWhenUserIsIdle")
            if not state.idle_supported:
                raise RuntimeError("not supported")
            state.idle.append(visitor)

        def AppVersion(self):
            major, minor, _sp = state.version
            return major * 100 + minor * 10

        def AppVersionMajor(self):
            return state.version[0]

        def AppVersionMinor(self):
            return state.version[1]

        def AppVersionSP(self):
            return state.version[2]

        def AppBuild(self):
            return state.build

    return Platform


class _Visitor(object):
    def vis_Evaluate(self):
        pass


class _ValueArray(object):
    """lx.object.ValueArray: what cmd_Query fills."""

    def __init__(self):
        self.values = []

    def set(self, other):
        # Modo wraps the query's array; here the wrapper is the array.
        self.values = other.values if isinstance(other, _ValueArray) else []
        self._target = other

    def AddInt(self, value):
        self._target.values.append(int(value))

    def AddString(self, value):
        self._target.values.append(str(value))


class _BasicCommand(object):
    def __init__(self):
        self._args = []     # [(name, type, flags)]
        self._values = {}

    def dyna_Add(self, name, arg_type):
        self._args.append([name, arg_type, 0])

    def basic_SetFlags(self, index, flags):
        self._args[index][2] = flags

    def _fill(self, positional, named):
        # As on 17.1 (2026-11-10): words with no name fill
        # the arguments in order until the first named word; after it a
        # word with no name is dropped (`select.symmetry 0 uvActive:0 u`
        # left uvAxis as it was). A word whose name isn't an argument is a
        # value with a colon in it.
        names = [a[0] for a in self._args]
        placed, at, named_seen = {}, 0, False
        for key, value in getattr(positional, "order", None) or (
                [(None, v) for v in positional]
                + [(k, v) for k, v in named.items()]):
            if key is not None and key in names:
                placed[names.index(key)] = value
                named_seen = True
                continue
            if key is not None:
                value = "%s:%s" % (key, value)
            if named_seen:
                continue
            if at < len(names):
                placed[at] = value
            at += 1
        for index, (name, arg_type, _flags) in enumerate(self._args):
            if index not in placed:
                continue
            raw = placed[index]
            if arg_type == _Symbol.sTYPE_INTEGER:
                self._values[index] = int(raw)
            elif arg_type == _Symbol.sTYPE_BOOLEAN:
                self._values[index] = str(raw).lower() in ("1", "true", "on", "yes")
            else:
                self._values[index] = raw

    def dyna_IsSet(self, index):
        return index in self._values

    def dyna_Int(self, index):
        return int(self._values[index])

    def dyna_String(self, index):
        return str(self._values[index])

    def dyna_Bool(self, index):
        return bool(self._values[index])

    def cmd_Flags(self):
        return 0

    def cmd_Query(self, index, values):
        return _Result.OK

    def basic_Execute(self, msg, flags):
        pass


STATE = None


def install():
    """Put fake lx, lxifc and lxu into sys.modules. Returns the FakeModo."""
    global STATE
    state = FakeModo()
    STATE = state

    lx = types.ModuleType("lx")
    lx.symbol = _Symbol
    lx.result = _Result
    lx.service = types.SimpleNamespace(Platform=_make_platform(state),
                                       Log=_make_log(state),
                                       Host=_make_host(state),
                                       Selection=_make_selection(state),
                                       Mesh=_make_mesh_service(state),
                                       Command=_make_command_service(state),
                                       Undo=_make_undo_service(state),
                                       Listener=_make_listener_service(state),
                                       Tableau=_make_tableau_service(state),
                                       Image=_make_image_service(state),
                                       Layer=_make_layer_service(state))
    lx.object = types.SimpleNamespace(Attributes=_Attributes, LogEntry=_LogEntry,
                                      AttributesUI=_AttributesUI,
                                      Command=_Command,
                                      UIValueHints=lambda obj: obj,
                                      Locator=_Locator,
                                      SurfaceItem=_SurfaceItem,
                                      Surface=lambda obj: obj,
                                      TableauSurface=lambda obj: obj,
                                      TableauVertex=lambda obj: obj,
                                      Scene=_SceneObject,
                                      Mesh=lambda obj: obj,
                                      LayerScan=lambda obj: obj,
                                      ItemGraph=lambda graph: graph,
                                      ValueArray=_ValueArray,
                                      storage=_Storage,
                                      Item=lambda item: item,
                                      Image=lambda image: image,
                                      Unknown=lambda obj: obj,
                                      PolygonPacketTranslation=_PacketTranslation,
                                      EdgePacketTranslation=_PacketTranslation,
                                      VertexPacketTranslation=_PacketTranslation)
    lx.out = state.out
    lx.eval = state.run_command

    def eval1(text):
        value = state.run_command(text)
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    def evalN(text):
        value = state.run_command(text)
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple)) else [value]

    lx.eval1 = eval1
    lx.evalN = evalN
    lx.command = state.command
    lx.bless = lambda cls, name: state.commands.__setitem__(name, cls)

    lxifc = types.ModuleType("lxifc")
    lxifc.Visitor = _Visitor
    lxifc.Undo = _Undo
    lxifc.SceneItemListener = _SceneItemListener
    lxifc.TriangleSoup = _TriangleSoup

    lxu = types.ModuleType("lxu")
    lxu_command = types.ModuleType("lxu.command")
    lxu_command.BasicCommand = _BasicCommand
    lxu.command = lxu_command

    sys.modules["lx"] = lx
    sys.modules["lxifc"] = lxifc
    sys.modules["lxu"] = lxu
    sys.modules["lxu.command"] = lxu_command
    sys.modules["modo"] = _make_modo(state)
    _install_scene_commands(state)
    return state


def uninstall():
    for name in ("lx", "lxifc", "lxu", "lxu.command", "modo"):
        sys.modules.pop(name, None)
