# python
# modo-tools journal 1: 2026-11-08_1845-untitled
# Modo 17.1v1, kit 0.2.0. Start from File > New (the journal began in
# Untitled, not saved). Run with @ this file: it writes no file and opens no
# scene.
# 9 entries: 7 in this file, 1 rolled back, 1 not replayed (a save).
# Untraced, run as their code: 2 (3, 6).
# Failed and not rolled back, their trace only: 1 (5).
# Warning: entry 9's rollback was unsure; its trace is in, as if it stuck.
# Checks: start, after 3, after 9.
# Not exact: 3 (run as its code), 6 (run as its code).

import re

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


_START = 'File > New'
START = [
    {'name': 'Mesh', 'id': 'mesh002', 'points': 0, 'polygons': 0, 'box': None},
]
FACTS_3 = [
    {'name': 'Mesh', 'id': 'mesh002', 'points': 12, 'polygons': 10, 'box': [[-0.5, -0.5, -0.5], [0.5, 0.75, 0.5]]},
]
FACTS_9 = [
    {'name': 'Mesh', 'id': 'mesh002', 'points': 12, 'polygons': 10, 'box': [[-0.5, -0.5, -0.5], [0.5, 0.75, 0.5]]},
    {'name': 'Mesh (2)', 'id': 'mesh027', 'points': 0, 'polygons': 0, 'box': None},
]

_check(START, after=0)

# 1  run_command  prim.cube
lx.eval('!!prim.cube sizeX:1')

# 2  run_python  "Bevel the top"  rolled back

# 3  run_python  "Raise the top"  untraced: MeshPolygon.select, run as its code
_code(r'''for line in LINES:
    lx.eval(line)''', 3)

_check(FACTS_3, after=3)

# 4  set_channels  Mesh
_set('translation028', 'pos.X', 2.0)

# 5  run_python  "A locator, then a bad bevel"  failed, not rolled back: its trace
lx.eval('!!item.create locator')
# failed: !!tool.attr poly.bevel shift abc

# 6  run_python  "Name the locator"  untraced: Channel.set, run as its code
_code("import modo\nloc = modo.Scene().item('Locator')\nloc.name = '''Pin'''\nlx.eval('scene.saveAs filename:C:/models/x.lxo')\n", 6)
# saved: C:/models/x.lxo

# 7  run_python  "Export and tidy"
# not replayed: !!scene.saveAs filename:C:/models/x.fbx format:fbx
lx.eval('!!item.create locator')
# not replayed: !!scene.new
# not replayed: !!scene.open C:/models/y.lxo
# not replayed: !!scene.close
# not replayed: !!scene.revert
# not replayed: !!app.quit
# not replayed: !!scene.save
lx.eval('!!loaderOptions.fbx export:true')

# 8  save_scene  C:/models/x.lxo  not replayed
# saved: C:/models/x.lxo

# 9  run_command  item.create  rollback unsure: replayed as if it stuck
lx.eval('!!item.create mesh')

_check(FACTS_9, after=9)

# ended: another scene

_done(7)
