# python
"""scene_summary, scene_graph, get_item, get_selection: reading the scene.

Every op here is a read, so it runs through modoTools.execRead and adds no
undo entry, and nothing here may change the scene. That rules out modo's
Locator.position and friends, which add a transform item when there is none
(modo/item.py, __getTransformitem); get_item asks lx.object.Locator, which
only looks.

Answers can be large. The kit sends them whole; the server cuts them to the
size budget, where it knows the budget and can say how to ask for less.
"""

from . import errors, protocol
from .jobs import op
from .ops_run import plain

# What get_selection takes, and the geometry list each one reads.
ELEMENTS = {"vertex": "vertices", "edge": "edges", "polygon": "polygons"}


def _short(item):
    return {"id": item.id, "name": item.name, "type": item.type}


def resolve(ref):
    """The item whose id, else whose name, else whose base name is `ref`.

    On 17.1 an item's name is the one the Items list shows, made unique by
    Modo: two meshes called Wheel show as "Wheel" and "Wheel (2)", and both
    have the base name "Wheel" (seen 2026-11-08). The shown name wins; a
    base name two items share is ambiguous_item, listing them.
    """
    import modo
    ref = str(ref or "").strip()
    if not ref:
        raise errors.OpError(protocol.BAD_REQUEST, "no item given",
                             hint="Pass an item id (mesh003) or name.")
    named, based = [], []
    for item in modo.Scene().items():
        if item.id == ref:
            return item
        if item.name == ref:
            named.append(item)
        elif getattr(item, "baseName", None) == ref:
            based.append(item)
    found = named or based
    if not found:
        raise errors.OpError(
            protocol.NO_SUCH_ITEM, "no item with id or name %r" % ref,
            hint="List items with mt_scene_graph; ids are unique, names aren't.")
    if len(found) > 1:
        raise errors.OpError(
            protocol.AMBIGUOUS_ITEM,
            "%d items are named %r: %s" % (len(found), ref, ", ".join(
                "%s (%s)" % (i.id, i.name) for i in found)),
            hint="Pass one of the ids instead of the name.")
    return found[0]


@op("scene_summary", read=True)
def scene_summary(payload, kit):
    import lx
    import modo
    scene = modo.Scene()
    counts = {}
    for item in scene.items():
        counts[item.type] = counts.get(item.type, 0) + 1
    fps = scene.fps
    try:
        frame = int(round(float(lx.eval("select.time ?")) * fps))
    except Exception:
        frame = None
    return {
        "file": scene.filename,
        "frames": [int(f) for f in scene.sceneRange],
        "frame": frame,
        "fps": fps,
        "items": counts,
        "selected": [_short(i) for i in scene.selected],
    }


def _shown(item, types):
    if types is None:
        return item.isLocatorSuperType()
    return "all" in types or item.type in types


def _nodes(items, types, depth, counter):
    """The shown items among `items`, nested `depth` levels deep. An item
    that isn't shown passes its shown descendants up to its parent."""
    out = []
    for item in items:
        if not _shown(item, types):
            out.extend(_nodes(item.children(), types, depth, counter))
            continue
        node = {"name": item.name, "id": item.id, "type": item.type}
        counter[0] += 1
        if depth > 1:
            children = _nodes(item.children(), types, depth - 1, counter)
            if children:
                node["children"] = children
        else:
            hidden = len(_nodes(item.children(), types, 1, [0]))
            if hidden:
                node["more"] = hidden
        out.append(node)
    return out


@op("scene_graph", read=True)
def scene_graph(payload, kit):
    """The item tree. Without types, what Modo's Items list shows (the
    locator super type); types=["all"] for every item, shader tree too."""
    import modo
    try:
        depth = max(1, int(payload.get("depth") or 99))
    except (TypeError, ValueError):
        depth = 99
    types = payload.get("types") or None
    if isinstance(types, str):
        types = [types]
    root = payload.get("root")
    if root:
        start = resolve(root).children()
    else:
        start = [i for i in modo.Scene().items() if i.parent is None]
    counter = [0]
    items = _nodes(start, types, depth, counter)
    return {"items": items, "count": counter[0]}


_NONE = object()
_XFRM = {"pos.": ("iXFRM_POSITION", 0.0), "rot.": ("iXFRM_ROTATION", 0.0),
         "scl.": ("iXFRM_SCALE", 1.0)}


def _transform_value(item, name):
    """pos.X, rot.Y, scl.Z of a locator, from its transform item; a missing
    transform item reads as Modo's default and is not created. _NONE for any
    other name, or an item that isn't a locator."""
    import lx
    import modo
    prefix, axis = name[:4], name[4:]
    if (prefix not in _XFRM or axis not in ("X", "Y", "Z")
            or not item.isLocatorSuperType()):
        return _NONE
    symbol, default = _XFRM[prefix]
    try:
        found = modo.Item(lx.object.Locator(item).GetTransformItem(
            getattr(lx.symbol, symbol)))
    except (LookupError, TypeError, RuntimeError):
        return default
    channel = found.channel(name)
    return _value(channel) if channel is not None else default


def _value(channel):
    """A channel's value; for one that isn't a plain value, its type. On 17.1
    a matrix channel reads as an lx.object.Unknown, whose text is only an
    address."""
    value = channel.get()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return plain(value)
    return "<%s>" % (getattr(channel, "storageType", None) or type(value).__name__)


@op("get_item", read=True)
def get_item(payload, kit):
    item = resolve(payload.get("item"))
    parent = item.parent
    out = {
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "parent": {"id": parent.id, "name": parent.name} if parent else None,
    }
    if item.isLocatorSuperType():
        for key in ("pos", "rot", "scl"):
            out[key] = [_transform_value(item, "%s.%s" % (key, axis))
                        for axis in "XYZ"]
    wanted = payload.get("channels")
    if wanted:
        if isinstance(wanted, str) and wanted != "all":
            wanted = [wanted]
        names = list(item.channelNames) if wanted == "all" else [str(n) for n in wanted]
        found, missing = {}, []
        for name in names:
            channel = item.channel(name)
            if channel is not None:
                found[name] = _value(channel)
                continue
            # pos.Y on a mesh lives on its translation item, as in
            # mt_set_channels.
            value = _transform_value(item, name)
            if value is _NONE:
                missing.append(name)
            else:
                found[name] = value
        if missing:
            hint = "Ask with channels=\"all\" to see every channel's name."
            if any(n[:4] in ("pos.", "rot.", "scl.") for n in missing):
                hint += " pos, rot and scl are in the answer without asking."
            raise errors.OpError(
                protocol.BAD_CHANNEL, "%s has no channel %s" % (
                    item.id, ", ".join(repr(n) for n in missing)), hint=hint)
        out["channels"] = found
    return out


def _indices(geometry, kind):
    found = getattr(geometry, ELEMENTS[kind]).selected
    if kind == "edge":
        return [list(e.vertexIndices) for e in found]
    return [e.index for e in found]


@op("get_selection", read=True)
def get_selection(payload, kit):
    """Selected items, or the selected vertices, edges or polygons of each
    selected mesh, by index. """
    import modo
    kind = str(payload.get("type") or "item").strip().lower()
    if kind != "item" and kind not in ELEMENTS:
        raise errors.OpError(
            protocol.BAD_REQUEST, "no selection type %r" % kind,
            hint="Pass type as item, vertex, edge or polygon.")
    selected = modo.Scene().selected
    if kind == "item":
        return {"type": "item", "items": [_short(i) for i in selected]}
    meshes = []
    for item in selected:
        if item.type != "mesh":
            continue
        meshes.append({"id": item.id, "name": item.name,
                       "indices": _indices(item.geometry, kind)})
    return {"type": kind, "meshes": meshes}
