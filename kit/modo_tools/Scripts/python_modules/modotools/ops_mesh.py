# python
"""describe_mesh: facts about meshes, read through Modo's mesh API; points
and set_points: vertex positions by index, read and written.

The mesh is the mesh item's `mesh` channel, read through the edit action
at the current time: the base mesh as edits leave it, never opened for
writing and never a layer selected, so the read stays inside execRead's
promise of no undo entry and no change (checked on 17.1, 2026-11-08). Not
layerservice, which costs a command per value and selects a layer.

read_mesh copies what the facts need into plain lists; meshmath computes
every fact from those lists and imports nothing from Modo.
"""

import math
import re

from . import errors, meshmath, protocol
from .jobs import op
from .ops_scene import resolve

DETAILS = ("summary", "selection")
MODES = (None,) + meshmath.MODES

# What detail="selection" answers when a request doesn't say: the server's
# default budget, 40,000 characters, over 150 a rounded entry.
DEFAULT_MAX_ELEMENTS = 266

PLURALS = {"vertex": "vertices", "edge": "edges", "polygon": "polygons"}

# Past this many points in the whole scene, the journal takes no facts
# record: every point is one Pos() call, and a test timed a full
# read at 1.9 s for 500,000 polygons. Points are counted before any is read.
FACTS_MAX_POINTS = 1000000

# surface=true: past this many sampled points over the meshes
# asked, refused, and the sample stops at the first point past it: each
# point is one Python call from Modo's sampler, on the main thread, and the
# facts are Python too. On a test PC (2026-11-09) 2.5 million points (a
# 101,400-polygon SubD cube at level 2) took 18 s in all; this many, 3.5 s.
SURFACE_MAX_POINTS = 500000

# Modo's polygon type codes (lx.symbol.iPTYP_*) to the names the answer uses.
_TYPE_SYMBOLS = (("iPTYP_FACE", "face"), ("iPTYP_SUBD", "subd"),
                 ("iPTYP_PSUB", "psub"), ("iPTYP_CURVE", "curve"),
                 ("iPTYP_BEZIER", "bezier"), ("iPTYP_BSPLINE", "bspline"),
                 ("iPTYP_LINE", "line"), ("iPTYP_TEXT", "text"),
                 ("iPTYP_PATCH", "patch"), ("iPTYP_SPCH", "spline_patch"),
                 ("iPTYP_HAIR", "hair"), ("iPTYP_CRVFIL", "curve_fill"),
                 ("iPTYP_OPNT", "point"))


def _type_names():
    import lx
    names = {}
    for symbol, name in _TYPE_SYMBOLS:
        code = getattr(lx.symbol, symbol, None)
        if code is not None:
            names.setdefault(code, name)
    return names


def _type_name(code, names):
    name = names.get(code)
    if name is None:
        # A type this list doesn't know: its four letters, as Modo spells it.
        try:
            name = int(code).to_bytes(4, "big").decode("ascii").strip().lower()
        except (ValueError, OverflowError, UnicodeDecodeError):
            name = str(code)
    return name


def _base_mesh(item):
    """(scene, time, the base lx.object.Mesh) of a mesh item."""
    import lx
    scene = lx.object.Scene(item.Context())
    time = lx.service.Selection().GetTime()
    edit = scene.Channels(lx.symbol.s_ACTIONLAYER_EDIT, time)
    mesh = lx.object.Mesh(edit.ValueObj(
        item, item.ChannelLookup(lx.symbol.sICHAN_MESH_MESH)))
    return scene, time, mesh


def read_mesh(item):
    """The base mesh of a mesh item as plain lists, positions in world
    space: {"positions", "polygons", "types", "edges"}. Main thread."""
    import lx
    scene, time, mesh = _base_mesh(item)
    # The world matrix is only in the evaluated read: the edit action's
    # read raises LookupError for it on 17.1.
    matrix, offset = lx.object.Locator(item).WorldTransform(
        scene.Channels(None, time))

    points = mesh.PointAccessor()
    index, local = {}, []
    for i in range(mesh.PointCount()):
        points.SelectByIndex(i)
        index[points.ID()] = i
        local.append(points.Pos())

    names = _type_names()
    polygon = mesh.PolygonAccessor()
    polygons, types = [], []
    for i in range(mesh.PolygonCount()):
        polygon.SelectByIndex(i)
        polygons.append([index[polygon.VertexByIndex(j)]
                         for j in range(polygon.VertexCount())])
        types.append(_type_name(polygon.Type(), names))

    edge = mesh.EdgeAccessor()
    edges = []
    for i in range(mesh.EdgeCount()):
        edge.SelectByIndex(i)
        a, b = edge.Endpoints()
        edges.append([index[a], index[b]])

    return {"positions": meshmath.to_world(local, matrix, offset),
            "polygons": polygons, "types": types, "edges": edges}


def _surface_too_large():
    return errors.OpError(
        protocol.TOO_LARGE, "the surface has more than {:,} points".format(
            SURFACE_MAX_POINTS),
        hint="Ask for fewer meshes in items, or lower the mesh's render "
             "level (Render Subdivision Level in its properties; a Pixar "
             "SubD mesh has its own).")


def read_surface(item, sampled):
    """(points, triangles) of the surface Modo draws for a mesh item, from
    its own sampler at the mesh's render level, points in world space.
    Modo's SubD isn't plain Catmull-Clark (17.1, 2026-11-09), so its surface
    is read, not computed. The base mesh, through the edit action, as
    read_mesh reads it. sampled[0] counts points over the call. Main
    thread; a read: no undo entry, no scene change (checked on 17.1)."""
    import lx
    import lxifc
    scene = lx.object.Scene(item.Context())
    time = lx.service.Selection().GetTime()
    edit = scene.Channels(lx.symbol.s_ACTIONLAYER_EDIT, time)
    matrix, offset = lx.object.Locator(item).WorldTransform(
        scene.Channels(None, time))

    class Soup(lxifc.TriangleSoup):
        def __init__(self):
            self.points, self.triangles = [], []
            self.first = 0

        def soup_TestBox(self, bbox):
            return True

        def soup_Segment(self, segment, kind):
            # A triangle names its points by their place in the segment,
            # from 0, whatever soup_Vertex returned (17.1, 2026-11-09).
            # Only triangle segments come from a mesh, curves and
            # two-point polygons give none, and what this returns is
            # ignored (17.1, 2026-11-10).
            self.first = len(self.points)
            return True

        def soup_Vertex(self, vertex):
            # Typed and sized before get(), or it holds nothing.
            vertex.setType("f")
            vertex.setSize(3)
            sampled[0] += 1
            if sampled[0] > SURFACE_MAX_POINTS:
                # Raising stops Modo's Sample; what comes out of it may be
                # Modo's error rather than this one, so sampled says why.
                raise _surface_too_large()
            self.points.append(vertex.get())
            return len(self.points) - 1

        def soup_Polygon(self, a, b, c):
            first = self.first
            self.triangles.append((a + first, b + first, c + first))

        def soup_Connect(self, kind):
            pass

    points, triangles = [], []
    surface = lx.object.Surface(lx.object.SurfaceItem(item).GetSurface(edit, 0))
    for i in range(surface.BinCount()):
        tableau = lx.object.TableauSurface(surface.BinByIndex(i))
        vertex = lx.object.TableauVertex(lx.service.Tableau().AllocVertex())
        vertex.AddFeature(lx.symbol.iTBLX_BASEFEATURE,
                          lx.symbol.sTBLX_FEATURE_POS)
        tableau.SetVertex(vertex)
        soup = Soup()
        try:
            tableau.Sample(tableau.Bound(), 0.0, soup)
        except Exception:
            if sampled[0] > SURFACE_MAX_POINTS:
                raise _surface_too_large()
            raise
        first = len(points)
        points.extend(soup.points)
        triangles.extend((a + first, b + first, c + first)
                         for a, b, c in soup.triangles)
    return meshmath.to_world(points, matrix, offset), triangles


def current_mode():
    """Modo's selection mode: "vertex", "edge" or "polygon", or None in item
    mode. CurrentType takes only a storage of ints on 17.1, ended by 0; a
    list is an invalid argument."""
    import lx
    service = lx.service.Selection()
    codes = {service.LookupType(getattr(lx.symbol, symbol)): mode
             for symbol, mode in (("sSELTYP_VERTEX", "vertex"),
                                  ("sSELTYP_EDGE", "edge"),
                                  ("sSELTYP_POLYGON", "polygon"),
                                  ("sSELTYP_ITEM", None))}
    asked = lx.object.storage("i", len(codes) + 1)
    asked.set(list(codes) + [0])
    return codes.get(service.CurrentType(asked))


# The selection service's type and packet translation for each mode.
_PACKETS = {"vertex": ("sSELTYP_VERTEX", "VertexPacketTranslation"),
            "edge": ("sSELTYP_EDGE", "EdgePacketTranslation"),
            "polygon": ("sSELTYP_POLYGON", "PolygonPacketTranslation")}


def read_selection(items, mode):
    """{item id: the selected elements of that mesh item in `mode`}:
    indices, or vertex index pairs in edge mode, in the order picked.

    Read from the selection service, one packet per selected element in
    the scene, so the size limit costs no more than the selection's own
    size. Not the select mark on each element: on 17.1 (2026-11-08) it
    marked all six polygons of a cube with one selected."""
    import lx
    symbol, translation = _PACKETS[mode]
    service = lx.service.Selection()
    kind = service.LookupType(getattr(lx.symbol, symbol))
    packets = getattr(lx.object, translation)(
        service.Allocate(getattr(lx.symbol, symbol)))
    found = {item.id: [] for item in items}
    for i in range(service.Count(kind)):
        packet = service.ByIndex(kind, i)
        ident = lx.object.Item(packets.Item(packet)).Ident()
        if ident not in found:
            continue
        mesh = lx.object.Mesh(packets.Mesh(packet))
        if mode == "polygon":
            polygon = mesh.PolygonAccessor()
            polygon.Select(packets.Polygon(packet))
            found[ident].append(polygon.Index())
            continue
        points = mesh.PointAccessor()
        if mode == "vertex":
            points.Select(packets.Vertex(packet))
            found[ident].append(points.Index())
            continue
        pair = []
        for point in packets.Vertices(packet):
            points.Select(point)
            pair.append(points.Index())
        found[ident].append(tuple(pair))
    return found


def _source(item):
    """A mesh instance's source mesh, or None."""
    import lx
    import modo
    try:
        scene = lx.object.Scene(item.Context())
        graph = lx.object.ItemGraph(scene.GraphLookup(lx.symbol.sGRAPH_MESHINST))
        if graph.RevCount(item):
            return modo.Item(graph.RevByIndex(item, 0))
    except (LookupError, TypeError, RuntimeError):
        pass
    return None


def _not_a_mesh(item):
    if item.type == "meshInst":
        source = _source(item)
        hint = ("It is an instance of %s (%s); describe that." % (
            source.id, source.name) if source is not None
                else "Describe the mesh it instances.")
        return errors.OpError(protocol.NOT_A_MESH, "%s is a mesh instance, not "
                              "a mesh" % item.name, hint=hint)
    article = "an" if item.type[:1].lower() in ("a", "e", "i", "o", "u") else "a"
    return errors.OpError(
        protocol.NOT_A_MESH, "%s is %s %s, not a mesh" % (
            item.name, article, item.type),
        hint="List the meshes with mt_scene_graph types=[\"mesh\"].")


def _meshes(refs):
    """The mesh items asked for, in order, each once. None: the selected
    meshes, or every mesh when none is selected."""
    import modo
    if refs is None:
        scene = modo.Scene()
        found = [i for i in scene.selected if i.type == "mesh"]
        return found or [i for i in scene.items() if i.type == "mesh"]
    if isinstance(refs, str):
        refs = [refs]
    found, seen = [], set()
    for ref in refs:
        item = resolve(ref)
        if item.type != "mesh":
            raise _not_a_mesh(item)
        if item.id not in seen:
            seen.add(item.id)
            found.append(item)
    return found


def _mode(payload, detail):
    mode = payload.get("mode")
    if mode not in MODES:
        raise errors.OpError(protocol.BAD_REQUEST, "no mode %r" % mode,
                             hint="Pass mode as vertex, edge or polygon.")
    if mode is None:
        mode = current_mode()
    if mode is None and detail == "selection":
        raise errors.OpError(
            protocol.BAD_REQUEST, "detail=\"selection\" needs a mode, and "
            "Modo is in item mode",
            hint="Pass mode as vertex, edge or polygon.")
    return mode


def _max_elements(payload):
    try:
        return max(0, int(payload.get("max_elements", DEFAULT_MAX_ELEMENTS)))
    except (TypeError, ValueError):
        raise errors.OpError(protocol.BAD_REQUEST, "max_elements isn't a "
                             "number", hint="Pass max_elements as an int.")


def _surface(payload):
    """Whether surface is asked. Modo samples at the mesh's render level,
    so a level can't be."""
    if "surface_level" in payload:
        raise errors.OpError(
            protocol.BAD_REQUEST, "surface_level can't be chosen",
            hint="Modo samples the surface at the mesh's render level "
                 "(Render Subdivision Level in its properties; a Pixar SubD "
                 "mesh has its own); the answer's level says which.")
    surface = payload.get("surface", False)
    if not isinstance(surface, bool):
        # "false" in a hand-written request would otherwise sample.
        raise errors.OpError(
            protocol.BAD_REQUEST, "surface must be true or false",
            hint="Pass surface=true for the surface Modo draws.")
    return surface


def _too_large(mode, count, room):
    def say(n, mode):
        return "{:,} {}".format(n, mode if n == 1 else PLURALS[mode])
    return errors.OpError(
        protocol.TOO_LARGE, "%s selected; %s" % (
            say(count, mode), "{:,} fit{}".format(room, "s" if room == 1 else "")),
        hint="Ask detail=\"summary\", whose selection still gives the count, "
             "box and centroid, or select fewer.")


@op("describe_mesh", read=True)
def describe_mesh(payload, kit):
    """Each mesh's counts, polygon types, world box, shape facts and
    selection. With detail="selection", one entry per selected element;
    past max_elements, refused once the selection is counted and before
    any mesh is read. With surface=true, the facts of the surface Modo
    draws for each mesh with SubD polygons; past SURFACE_MAX_POINTS sampled,
    refused."""
    detail = str(payload.get("detail") or "summary").strip().lower()
    if detail not in DETAILS:
        raise errors.OpError(protocol.BAD_REQUEST, "no detail %r" % detail,
                             hint="Pass detail as summary or selection.")
    mode = _mode(payload, detail)
    room = _max_elements(payload)
    items = _meshes(payload.get("items"))
    chosen = read_selection(items, mode) if mode else {}
    if detail == "selection":
        count = sum(len(c) for c in chosen.values())
        if count > room:
            raise _too_large(mode, count, room)
    surface = _surface(payload)
    meshes = []
    sampled = [0]
    for item in items:
        mesh = read_mesh(item)
        out = {"id": item.id, "name": item.name}
        out.update(meshmath.summary(mesh))
        if mode:
            out["selection"] = meshmath.selection(
                mesh, mode, chosen[item.id], elements=detail == "selection")
        if surface:
            out["surface"] = None
            if meshmath.has_surface(mesh):
                points, triangles = read_surface(item, sampled)
                out["surface"] = meshmath.surface(
                    points, triangles,
                    meshmath.render_level(mesh, len(triangles)),
                    closed=out["volume"] is not None)
        meshes.append(out)
    return {"meshes": meshes, "space": "world", "units": "m"}


# -- points and set_points ------------------------------------------

# What one point's line costs in the server's answer, at worst: an index in
# the millions and three coordinates to 10 km at six decimals, one line each.
POINT_CHARS = 64
# A polygon's line: its brackets, and each vertex index with its comma.
POLYGON_CHARS = 8
POLYGON_VERTEX_CHARS = 9
# Room the rest of the answer takes: item, name, counts, cut, undo, journal.
HEAD_CHARS = 1000
# Positions are answered to a micrometre: a fit needs no finer, and a full
# float costs three times the characters.
DIGITS = 6


def point_room(max_chars, polygon_chars):
    """How many points fit in max_chars beside polygons of polygon_chars."""
    return max(0, (max_chars - HEAD_CHARS - polygon_chars) // POINT_CHARS)


def _bad(message, hint=""):
    return errors.OpError(protocol.BAD_REQUEST, message, hint=hint)


def _whole(payload, key, default, least=0):
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise _bad("%s must be a whole number from %d, not %r" % (
            key, least, value), hint="Pass %s as an int." % key)
    return value


def _mesh_item(payload):
    ref = payload.get("item")
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        raise _bad("no item given", hint="Pass item, a mesh's id or name.")
    item = resolve(ref)
    if item.type != "mesh":
        raise _not_a_mesh(item)
    return item


@op("points", read=True)
def points(payload, kit):
    """A mesh's vertex positions by index, in the mesh's own space (metres),
    from start, count of them (all, left out); with polygons=true, every
    polygon as its vertex indices. From the edit action, as describe_mesh
    reads: the base mesh, before deformers. max_chars is the room the answer
    has: past it, a read with no count is too_large and one with a count is
    cut and says where to go on."""
    item = _mesh_item(payload)
    start = _whole(payload, "start", 0)
    count = _whole(payload, "count", None)
    polygons = payload.get("polygons", False)
    if not isinstance(polygons, bool):
        raise _bad("polygons must be true or false",
                   hint="Pass polygons=true for the polygons' vertex indices.")
    max_chars = _whole(payload, "max_chars", None)
    _scene, _time, mesh = _base_mesh(item)
    total = mesh.PointCount()
    if start > total:
        raise _bad("start %d is past the end: %s has %d point%s" % (
            start, item.name, total, "" if total == 1 else "s"),
            hint="Pass start from 0 to %d." % total)

    accessor = mesh.PointAccessor()
    out = {"item": item.id, "name": item.name, "count": total, "start": start}
    polygon_chars = 0
    if polygons:
        index = {}
        for i in range(total):
            accessor.SelectByIndex(i)
            index[accessor.ID()] = i
        polygon = mesh.PolygonAccessor()
        lists = []
        for i in range(mesh.PolygonCount()):
            polygon.SelectByIndex(i)
            lists.append([index[polygon.VertexByIndex(j)]
                          for j in range(polygon.VertexCount())])
        polygon_chars = sum(POLYGON_CHARS + POLYGON_VERTEX_CHARS * len(p)
                            for p in lists)
        if max_chars is not None and polygon_chars > max_chars - HEAD_CHARS:
            raise errors.OpError(
                protocol.TOO_LARGE, "%s's {:,} polygons take about {:,} "
                "characters; {:,} fit".format(
                    len(lists), polygon_chars, max(0, max_chars - HEAD_CHARS))
                % item.name,
                hint="Ask without polygons=true; mt_describe_mesh gives "
                     "the counts and shape facts.")
        out["polygons"] = lists
        out["polygon_count"] = len(lists)

    end = total if count is None else min(total, start + count)
    if max_chars is not None:
        room = point_room(max_chars, polygon_chars)
        if end - start > room:
            if not room and (polygons or count is not None):
                # The review: a page of no points, its next_start where it
                # began, sends a paging agent round for ever.
                raise errors.OpError(
                    protocol.TOO_LARGE, "no room for a point of %s%s" % (
                        item.name, " beside its {:,} polygons".format(
                            len(out["polygons"])) if polygons else ""),
                    hint="Ask without polygons=true for the points, in "
                         "pages; the polygons are the same on every page."
                    if polygons else "The answer's size budget "
                    "(MODO_TOOLS_MAX_CHARS) is too small for one point.")
            if count is None:
                raise errors.OpError(
                    protocol.TOO_LARGE, "{:,} points from {:,} in {}; {:,} "
                    "fit".format(end - start, start, item.name, room),
                    hint="Pass count (at most {:,}) and start, and read in "
                         "pages.".format(room))
            out["cut"] = {"shown": room, "asked": count,
                          "next_start": start + room}
            end = start + room

    shown = []
    for i in range(start, end):
        accessor.SelectByIndex(i)
        shown.append([i, [round(v, DIGITS) + 0.0 for v in accessor.Pos()]])
    out["points"] = shown
    out["space"] = "mesh"
    out["units"] = "m"
    return out


_INDEX = re.compile(r"[0-9]+\Z")


def _position(value, index):
    """value as three floats, or bad_request naming the point."""
    if (isinstance(value, (list, tuple)) and len(value) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in value)):
        try:
            # A whole number past a float's range raises here (the review).
            pos = tuple(float(v) for v in value)
        except OverflowError:
            pos = (float("inf"),)
        if all(math.isfinite(v) for v in pos):
            return pos
    shown = repr(value)
    if len(shown) > 80:
        shown = shown[:77] + "..."
    raise _bad("point %d: %s isn't three finite numbers" % (index, shown),
               hint="Nothing was written. Pass each position as [x, y, z] "
                    "in metres.")


def _targets(given, item, total):
    """{index: (x, y, z)} from points as {index: [x, y, z]} or a list of
    every position in index order. Every index and value is checked here,
    before any is written."""
    span = ("%s has no points" % item.name if not total else
            "%s has %d point%s, 0 to %d" % (item.name, total,
                                             "" if total == 1 else "s", total - 1))
    if isinstance(given, list):
        if given and all(isinstance(e, (list, tuple)) and len(e) == 2
                         and isinstance(e[1], (list, tuple)) for e in given):
            # The review: what mt_points answers, sent back as it came.
            raise _bad("points is a list of [index, [x, y, z]], as mt_points "
                       "answers them; mt_set_points takes them by index",
                       hint='Nothing was written. Pass {"0": [x, y, z], ...} '
                            "by index, or a list of every position, "
                            "[x, y, z] each, in index order.")
        if len(given) != total:
            raise _bad("a list of %d positions; %s" % (len(given), span),
                       hint="Nothing was written. A list gives every point, in "
                            "index order; for some, pass {index: [x, y, z]}.")
        return {i: _position(v, i) for i, v in enumerate(given)}
    if not isinstance(given, dict) or not given:
        raise _bad("no points given",
                   hint='Pass points as {"12": [x, y, z]} or a list of every '
                        "position in index order.")
    found, keys = {}, {}
    for key, value in given.items():
        if isinstance(key, int) and not isinstance(key, bool) and key >= 0:
            index = key
        elif isinstance(key, str) and _INDEX.match(key):
            index = int(key)
        else:
            raise _bad("%r isn't a point index" % (key,),
                       hint="Nothing was written. Indices are whole numbers "
                            "from 0, as mt_points answers them.")
        if index in keys:
            raise _bad("point %d is given twice (%r and %r)" % (
                index, keys[index], key), hint="Nothing was written.")
        if index >= total:
            raise _bad("no point %d: %s" % (index, span),
                       hint="Nothing was written. Read the indices with "
                            "mt_points.")
        keys[index] = key
        found[index] = _position(value, index)
    return found


def write_points(item, targets):
    """Set each point's position through a layer scan on the item: one
    layer, the item's, selected or not (17.1, 2026-11-11), so not
    modo.MeshGeometry, which read stale positions in a test. Inside the
    job's command, so the job's undo step holds it."""
    import lx
    try:
        scan = lx.object.LayerScan(lx.service.Layer().ScanAllocateItem(
            lx.object.Item(item), lx.symbol.f_LAYERSCAN_EDIT_VERTS))
        mesh = lx.object.Mesh(scan.MeshEdit(0))
        accessor = mesh.PointAccessor()
        for index, pos in targets.items():
            accessor.SelectByIndex(index)
            accessor.SetPos(pos)
        # POSITION and POINTS, not POSITION alone (17.1,
        # 2026-11-11): with POSITION alone, a write of all points, a
        # surface read (7.7's sampler), a write of some points, then two
        # undos left the mesh Modo hands the edit action's read and the TD
        # SDK with only the second write's neighbourhood put back (65 of
        # 706 points); layerservice and a new layer scan had it right.
        # POINTS makes Modo rebuild that mesh on each undo.
        scan.SetMeshChange(0, lx.symbol.f_MESHEDIT_POSITION
                           | lx.symbol.f_MESHEDIT_POINTS)
        scan.Apply()
    except errors.OpError:
        raise
    except Exception as exc:
        raise errors.OpError(
            protocol.COMMAND_FAILED, "writing %s's points failed: %s: %s" % (
                item.name, type(exc).__name__, exc),
            hint="The call is rolled back; check the mesh with mt_points.")


@op("set_points")
def set_points(payload, kit):
    """Set vertex positions of one mesh, in its own space (metres): points
    as {index: [x, y, z]} or a list of every position in index order. Every
    index and value is checked before any is written; then one write
    through a layer scan, in the job's one undo step."""
    item = _mesh_item(payload)
    total = _base_mesh(item)[2].PointCount()
    targets = _targets(payload.get("points"), item, total)
    write_points(item, targets)
    return {"item": item.id, "name": item.name, "written": len(targets),
            "count": total, "space": "mesh"}


# -- fit_backdrops ---------------------------------------------------------

# The masks are blurred by smooth times this much of the image's larger
# side, and the targets get smooth times this many Taubin rounds: at 0.5,
# 2 px on a 247 px test image and 3 rounds, as an agent's own smoothing used.
FIT_BLUR = 0.02
FIT_TAUBIN = 6
FIT_MAX_ITERATIONS = 50
# After the write, Modo's surface is sampled and each fitted point's
# surface point found beside where fit.rule puts it. Where they part by
# more than this (a mesh the rule doesn't know, such as open edges), the
# solve goes on from Modo's own points, up to FIT_MEASURED_ROUNDS more
# writes in the same undo step.
FIT_RULE_MATCH = 1e-5
FIT_MEASURED_ROUNDS = 3
FIT_DIGITS = 4
# The second pass casts each surface point along its normal, at most this
# share of the shape's size either way. On a test model (2026-11-11) the
# centre cast alone scored side 0.947 with compare.py (the tail tip, above
# every cage point, out of its reach); with this pass, 0.958.
FIT_REACH = 0.25
# A whole cage whose height along the shared axis is off the shape's by
# more than this share of it gets a note: the cast keeps a point's height
# in the shape's middle, so a short blockout ends short.
FIT_HEIGHT_SLACK = 0.15
# Found 2026-11-11. An image larger than this a side is read
# every nth pixel: the masks are pure Python on Modo's main thread, and a
# 2310 x 2470 image held Modo for 25 s (a 247 px image took 1.1 s).
FIT_IMAGE_SIDE = 512
# A shape filling this share of its image is no shape: the image has no
# clear or near-white background (a model on black was fitted to a box).
FIT_NO_BACKGROUND = 0.98
# A backdrop's own matrix further than this from none means it is turned
# or scaled, and its image isn't where its view and position say.
FIT_BACKDROP_TURN = 1e-6


def _fit_number(payload, key, default, low, high):
    value = payload.get(key)
    if value is None:
        return default
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise _bad("%s must be a number from %g to %g, not %r" % (
            key, low, high, value), hint="Nothing was changed.")
    return float(value)


def _fit_floor(payload):
    value = payload.get("floor")
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise _bad("floor must be a height in metres, not %r" % (value,),
                   hint="Nothing was changed. Leave floor out for no floor.")
    return float(value)


def _image_rows(path):
    """(width, height, step, rows of RGBA bytes) of the image at path,
    through Modo's image service: GetLine's returned storage, typed and
    sized, holds the line (17.1); the buffer passed
    stays empty. An image over FIT_IMAGE_SIDE a side is read every
    step-th pixel (fit.thinned says which); width and height are the
    whole image's."""
    import lx
    from . import fit
    image = lx.object.Image(lx.service.Image().LoadNoCache(path))
    width, height = image.Size()
    step = fit.thin(width, height, FIT_IMAGE_SIDE)
    rgba = lx.symbol.iIMV_RGBA
    rows = []
    for y in range(step // 2, height, step):
        line = image.GetLine(y, rgba, lx.object.storage("b", 4 * width))
        line.setType("b")
        line.setSize(4 * width)
        data = bytes(line.get())
        if step > 1:
            data = b"".join(data[4 * x:4 * x + 4]
                            for x in range(step // 2, width, step))
        rows.append(data)
    return width, height, step, rows


def _nth(n):
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _read_backdrop(item):
    """(a backdrop item as fit.Backdrop: its view, pixel size, world
    position and the image its clip shows, as a mask; a note when the
    image was read thinned, else None)."""
    import lx
    import modo
    from . import fit
    if item.type != "backdrop":
        raise _bad("%s is a %s, not a backdrop" % (item.name, item.type),
                   hint="Name two backdrops (mt_scene_graph types="
                        "[\"backdrop\"] lists them), or leave backdrops out.")
    raw = lx.object.Item(item)
    scene = lx.object.Scene(raw.Context())
    graph = lx.object.ItemGraph(scene.GraphLookup("backdropClip"))
    if not graph.FwdCount(raw):
        raise _bad("%s has no image" % item.name,
                   hint="Make backdrops with mt_backdrop, which links the image.")
    clip = modo.Item(graph.FwdByIndex(raw, 0))
    channel = clip.channel("filename")
    path = channel.get() if channel is not None else None
    if not path:
        raise _bad("%s has no image file" % item.name,
                   hint="Its clip (%s) names no file." % clip.id)
    matrix, offset = lx.object.Locator(raw).WorldTransform(
        scene.Channels(None, lx.service.Selection().GetTime()))
    if any(abs(matrix[r][c] - (1.0 if r == c else 0.0)) > FIT_BACKDROP_TURN
           for r in range(3) for c in range(3)):
        raise _bad("%s is turned or scaled" % item.name,
                   hint="The fit reads a backdrop's view, pixel size and "
                        "position, as mt_backdrop sets them. Set its "
                        "rotation to 0 and its scale to 100% (and its "
                        "parents'), or make it again with mt_backdrop.")
    try:
        width, height, step, rows = _image_rows(path)
    except (RuntimeError, LookupError) as exc:
        raise _bad("%s's image %s can't be read: %s" % (item.name, path, exc),
                   hint="Check the file is there and Modo can open it.")
    shape = fit.mask(len(rows[0]) // 4 if rows else 0, len(rows), rows)
    background = ("The fit takes the shape as what isn't background: clear "
                  "pixels, or near-white ones joined to the image's border.")
    if not shape.area:
        raise _bad("%s's image has no shape in it" % item.name,
                   hint=background)
    if shape.area >= FIT_NO_BACKGROUND * shape.width * shape.height:
        raise _bad("%s's image has no background: its shape fills the "
                   "image" % item.name,
                   hint=background + " Give the image a clear or white "
                        "background (with a margin round the shape), and "
                        "make the backdrop again.")
    view = str(item.channel("projection").get())
    pixel, note = item.channel("pixelSize").get(), None
    if step > 1 and view in fit.VIEWS:
        _columns, _lines, pixel, offset = fit.thinned(
            view, offset, pixel, width, height, step)
        note = ("%s's image is %d x %d: every %s pixel was read (the fit "
                "reads at most %d a side)." % (
                    item.name, width, height, _nth(step), FIT_IMAGE_SIDE))
    try:
        return fit.Backdrop(item.id, view, shape, pixel, offset), note
    except fit.FitError as exc:
        raise _bad("%s: %s" % (item.name, exc), hint=exc.hint)


def _fit_backdrop_items(refs):
    import modo
    if refs is None:
        found = list(modo.Scene().items("backdrop"))
        if len(found) != 2:
            raise _bad("the scene has %d backdrop%s; the fit takes two" % (
                len(found), "" if len(found) == 1 else "s"),
                hint="Make one per view with mt_backdrop, or name two in "
                     "backdrops." if len(found) < 2 else
                     "Name the two to fit to in backdrops.")
        return found
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or len(refs) != 2:
        raise _bad("backdrops names %s; the fit takes two" % (
            len(refs) if isinstance(refs, list) else repr(refs)),
            hint="Pass backdrops as two ids or names, one per view.")
    return [resolve(ref) for ref in refs]


def _nearest(points, cell):
    """A lookup of the point nearest a position, by a grid of cell metres."""
    grid = {}
    for i, p in enumerate(points):
        grid.setdefault(tuple(int(math.floor(c / cell)) for c in p), []).append(i)

    def find(q):
        key = tuple(int(math.floor(c / cell)) for c in q)
        best, best_d = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for i in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                        d = math.dist(points[i], q)
                        if best_d is None or d < best_d:
                            best, best_d = points[i], d
        return best, best_d
    return find


def _measure(item, cage, polygons, fitted, sampled):
    """(surface points, triangles, {point: where Modo's surface passes for
    it}, worst gap from the rule): Modo's own surface, each fitted point's
    surface point the sample nearest fit.rule's."""
    from . import fit
    points, triangles = read_surface(item, sampled)
    predicted = fit.rule(cage, polygons)
    size = max((max(p[k] for p in points) - min(p[k] for p in points)
                for k in range(3)), default=1.0) if points else 1.0
    find = _nearest(points, max(size / 50.0, 1e-6))
    measured, worst = {}, 0.0
    for i in fitted:
        found, gap = find(predicted[i])
        if found is None:
            found, gap = predicted[i], float("inf")
        measured[i] = list(found)
        worst = max(worst, gap)
    return points, triangles, measured, worst


@op("fit_backdrops")
def fit_backdrops(payload, kit):
    """Fit a SubD mesh to the outlines of two backdrops' images: the points
    of the selected SubD polygons (all of them when none is selected) cast
    to the shape both masks allow, as smooth slices along the axis the two
    views share, smoothed, then the cage solved so Modo's surface lands on
    them; then a second pass, each surface point cast along its normal.
    The selection counts in polygon mode only. Every argument and both
    images are checked before anything is written; one write through a layer scan, in the job's one undo
    step; a fit that doesn't settle fails and is rolled back."""
    import lx
    from . import fit
    item = _mesh_item(payload)
    smooth = _fit_number(payload, "smooth", 0.5, 0.0, 1.0)
    iterations = payload.get("iterations", 6)
    if (isinstance(iterations, bool) or not isinstance(iterations, int)
            or not 1 <= iterations <= FIT_MAX_ITERATIONS):
        raise _bad("iterations must be a whole number from 1 to %d, not %r" % (
            FIT_MAX_ITERATIONS, iterations), hint="Nothing was changed.")
    floor = _fit_floor(payload)
    notes = []
    backdrops = []
    for found in _fit_backdrop_items(payload.get("backdrops")):
        backdrop, thinned = _read_backdrop(found)
        backdrops.append(backdrop)
        if thinned:
            notes.append(thinned)
    side = max(max(b.mask.width, b.mask.height) for b in backdrops)
    try:
        shape = fit.Shape(backdrops[0], backdrops[1],
                          blur=int(round(smooth * FIT_BLUR * side)))
    except fit.FitError as exc:
        raise _bad(str(exc), hint=exc.hint)

    mesh = read_mesh(item)
    world, polygons, types = mesh["positions"], mesh["polygons"], mesh["types"]
    subd = [i for i, t in enumerate(types) if t == "subd"]
    if not subd:
        raise _bad("%s has no SubD polygon" % item.name,
                   hint="The fit moves a SubD cage: make its polygons SubD "
                        "(Tab in Modo, or poly.convert face subpatch true). "
                        "Pixar SubD isn't fitted.")
    # A polygon selection counts in polygon mode only, as
    # for Modo's own tools. A mesh keeps its selection when Modo leaves
    # the mode, and one left from an earlier step fitted 19 of 706 points
    # with no word of it (17.1, 2026-11-11).
    picked = read_selection([item], "polygon")[item.id]
    if picked and current_mode() == "polygon":
        wanted = set(subd)
        chosen = [i for i in picked if i in wanted]
        if not chosen:
            raise _bad("none of %s's selected polygons is SubD" % item.name,
                       hint="Select SubD polygons, or none to fit all of them.")
        notes.append("Fitted the %d selected SubD polygon%s of %d; drop the "
                     "selection (select.drop polygon) to fit them all." % (
                         len(chosen), "" if len(chosen) == 1 else "s",
                         len(subd)))
    else:
        chosen = subd
        if picked:
            notes.append(
                "%d polygon%s selected, but Modo isn't in polygon mode, so "
                "all %d SubD polygons were fitted. To fit the selection "
                "alone, run select.typeFrom polygon first." % (
                    len(picked), " is" if len(picked) == 1 else "s are",
                    len(subd)))
    fitted = sorted(set(v for i in chosen for v in polygons[i]))
    cage_polygons = [polygons[i] for i in subd]

    scene = lx.object.Scene(item.Context())
    matrix, offset = lx.object.Locator(item).WorldTransform(
        scene.Channels(None, lx.service.Selection().GetTime()))
    try:
        # The write goes back through the transform's inverse: checked now.
        fit.to_local([[0.0, 0.0, 0.0]], matrix, offset)
    except fit.FitError as exc:
        raise _bad("%s: %s" % (item.name, exc), hint=exc.hint)
    sampled = [0]
    before_points, before_triangles = read_surface(item, sampled)
    before = {b.view: fit.iou(before_points, before_triangles, b)
              for b in backdrops}

    near = fit.neighbours(polygons, len(world))
    box = [max(p[k] for p in world) - min(p[k] for p in world) for k in range(3)]
    limit = max(math.sqrt(sum(v * v for v in box)), shape.size)

    def settle(start, cast):
        """Targets from cast (one per fitted point), smoothed and floored,
        and the cage solved from start so its surface lands on them."""
        full = [list(p) for p in start]
        for i, t in zip(fitted, cast):
            full[i] = t
        full = fit.taubin(full, near, fitted, int(round(smooth * FIT_TAUBIN)))
        aims = fit.floored(full, floor, fitted)
        moved = fit.solve(start, cage_polygons, aims, fitted, iterations,
                          limit=limit)
        return aims, fit.floored(moved, floor, fitted)

    try:
        # Pass 1: from centres inside the shape, each point at its height.
        cast, uncast = fit.cast([world[i] for i in fitted], shape)
        targets, cage = settle(world, cast)
        # Pass 2: each surface point along its normal, which reaches what
        # the first pass can't (an end above or below every cage point).
        surface = fit.rule(cage, cage_polygons)
        normal = fit.normals(cage, cage_polygons)
        cast, _stayed = fit.cast_along([surface[i] for i in fitted],
                                       [normal[i] for i in fitted], shape,
                                       FIT_REACH * shape.size)
        targets, cage = settle(cage, cast)
        local = fit.to_local([cage[i] for i in fitted], matrix, offset)
        write_points(item, {i: tuple(p) for i, p in zip(fitted, local)})
        points, triangles, measured, gap = _measure(
            item, cage, cage_polygons, fitted, [0])
        rounds = 0
        while gap > FIT_RULE_MATCH and rounds < FIT_MEASURED_ROUNDS:
            # Modo's surface isn't where the rule said: go on from Modo's.
            rounds += 1
            for i in fitted:
                cage[i] = [cage[i][k] + targets[i][k] - measured[i][k]
                           for k in range(3)]
                if math.dist(cage[i], world[i]) > limit:
                    raise fit.Diverged("point %d moved past %.4g m" % (i, limit))
            cage = fit.floored(cage, floor, fitted)
            local = fit.to_local([cage[i] for i in fitted], matrix, offset)
            write_points(item, {i: tuple(p) for i, p in zip(fitted, local)})
            points, triangles, measured, gap = _measure(
                item, cage, cage_polygons, fitted, [0])
    except fit.Diverged as exc:
        raise errors.OpError(
            protocol.COMMAND_FAILED, "the fit didn't settle: %s" % exc,
            hint="Nothing stays: the call is rolled back. Check the "
                 "backdrops show the shape at the mesh's size and place "
                 "(mt_backdrop's rect), or fit fewer polygons at a time.")
    after = {b.view: fit.iou(points, triangles, b) for b in backdrops}
    out = {
        "item": item.id, "name": item.name, "fitted": len(fitted),
        "polygons": len(chosen),
        "backdrops": [b.name for b in backdrops],
        "iou": {view: [round(before[view], FIT_DIGITS),
                       round(after[view], FIT_DIGITS)] for view in before},
        "max_move_m": round(max(math.dist(cage[i], world[i]) for i in fitted),
                            DIGITS),
        "off_target_m": round(max(math.dist(measured[i], targets[i])
                                  for i in fitted), DIGITS),
        "space": "world",
        # Popped by the journal: the replay sets these as plain values.
        "_written": {str(i): [float(v) for v in p]
                     for i, p in zip(fitted, local)},
    }
    if rounds:
        out["measured_rounds"] = rounds
    if chosen is subd:
        axis = shape.axis
        low = min(world[i][axis] for i in fitted)
        high = max(world[i][axis] for i in fitted)
        span = shape.span[1] - shape.span[0]
        if (abs(low - shape.span[0]) > FIT_HEIGHT_SLACK * span
                or abs(high - shape.span[1]) > FIT_HEIGHT_SLACK * span):
            notes.append(
                "The cage ran from %.4g to %.4g m along %s, the shape from "
                "%.4g to %.4g m. The first pass keeps a point's height in "
                "the shape's middle and the second reaches only so far, so "
                "block out near the shape's height first (a box of the "
                "backdrops' size), then fit." % (
                    low, high, "XYZ"[axis], shape.span[0], shape.span[1]))
    if uncast:
        out["uncast"] = uncast
        notes.append("%d point%s had no slice of the shape to cast from and "
                       "stayed where %s: the backdrops may not cover the mesh's "
                       "heights." % (uncast, "" if uncast == 1 else "s",
                                     "it was" if uncast == 1 else "they were"))
    if notes:
        out["note"] = " ".join(notes)
    return out

# -- the journal's facts -------------------------------------------

def mesh_facts(item):
    """One mesh's facts for the journal: name, id, points, polygons and the
    world box, rounded as describe_mesh rounds it."""
    import lx
    out = {"name": item.name, "id": item.id, "points": 0, "polygons": 0,
           "box": None}
    try:
        scene, time, mesh = _base_mesh(item)
    except LookupError:
        return out
    matrix, offset = lx.object.Locator(item).WorldTransform(
        scene.Channels(None, time))
    points = mesh.PointAccessor()
    local = []
    for i in range(mesh.PointCount()):
        points.SelectByIndex(i)
        local.append(points.Pos())
    out["points"] = len(local)
    out["polygons"] = mesh.PolygonCount()
    out["box"] = meshmath.box(meshmath.to_world(local, matrix, offset))
    return out


def largest_mesh_points():
    """The point count of the scene's largest mesh (0 with none). Counts
    only; cheap."""
    import modo
    most = 0
    for item in modo.Scene().items("mesh"):
        try:
            most = max(most, _base_mesh(item)[2].PointCount())
        except LookupError:
            pass
    return most


def scene_facts():
    """(every mesh's facts, None), or (None, why) past FACTS_MAX_POINTS."""
    import modo
    items = [i for i in modo.Scene().items("mesh")]
    total = 0
    for item in items:
        try:
            total += _base_mesh(item)[2].PointCount()
        except LookupError:
            pass
    if total > FACTS_MAX_POINTS:
        return None, ("the scene has {:,} points, past the {:,} the journal "
                      "reads for facts".format(total, FACTS_MAX_POINTS))
    return [mesh_facts(item) for item in items], None


@op("journal_facts", read=True)
def journal_facts(payload, kit):
    """A facts record in the current scene's journal: {"facts":
    the entry it follows, "meshes": k}, or "facts": null with a note."""
    if kit.journal is None:
        return {"facts": None, "note": "the kit has no journal"}
    return kit.journal.facts()
