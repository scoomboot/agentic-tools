# python
"""Facts about a mesh, from plain lists. Imports nothing from Modo.

A mesh here is a dict of lists, as ops_mesh.read_mesh copies it out of Modo:

    positions  [(x, y, z), ...]  world space, metres
    polygons   [[vertex index, ...], ...]
    types      ["face", "subd", ...], one per polygon
    edges      [(a, b), ...]  vertex index pairs

summary() gives every fact of the default describe: counts, types and the
box, and the shape facts; selection() the selection's. Every number that leaves summary() is
rounded, so two describes of the same mesh are equal byte for byte.
"""


def rounded(value, places=6):
    """value to `places` decimals; -0.0 is written 0."""
    value = round(float(value), places)
    return 0.0 if value == 0 else value


def counts(mesh):
    return {"vertices": len(mesh["positions"]), "edges": len(mesh["edges"]),
            "polygons": len(mesh["polygons"])}


def types(names):
    """Polygons by type when any isn't a plain face; None when all are."""
    found = {}
    for name in names:
        found[name] = found.get(name, 0) + 1
    if not found or list(found) == ["face"]:
        return None
    return found


def to_world(positions, matrix, offset):
    """Each position as matrix * p + offset. Modo's world matrix applies to a
    column vector: row r of `matrix` gives world axis r."""
    (a, b, c), (d, e, f), (g, h, i) = matrix
    ox, oy, oz = offset
    return [[a * x + b * y + c * z + ox,
             d * x + e * y + f * z + oy,
             g * x + h * y + i * z + oz] for x, y, z in positions]


def box(positions):
    """[[min x, y, z], [max x, y, z]], or None for no positions."""
    if not positions:
        return None
    xs, ys, zs = zip(*positions)
    return [[rounded(min(xs)), rounded(min(ys)), rounded(min(zs))],
            [rounded(max(xs)), rounded(max(ys)), rounded(max(zs))]]


# --- Shape facts. Sides, area and volume count only the polygons
# with a surface; curves and the like are in `types` and nowhere else.
# Topology (borders, poles, non-manifold edges) is read off those same
# polygons' edges, so a curve never makes a border; a polygon of one or two
# points has sides but no edges here, since it bounds no surface.

SURFACES = ("face", "subd", "psub")

# Under this, in square metres, a polygon has no area and no normal.
ZERO_AREA = 1e-12

# A closed mesh whose volume is under this share of the sum of its
# tetrahedra's sizes encloses nothing (a two-sided card); what is left is
# float noise, and its sign would read as flipped normals.
FLAT_VOLUME = 1e-9


def significant(value, figures=6):
    """value to `figures` significant figures; -0.0 is written 0, None
    stays None."""
    if value is None:
        return None
    value = float("%.*g" % (figures, value))
    return 0.0 if value == 0 else value


def newell(points, poly):
    """(unit normal, area) of one polygon by Newell's method. Under
    ZERO_AREA the normal is (0, 0, 0) and the area 0, never NaN."""
    nx = ny = nz = 0.0
    count = len(poly)
    for k in range(count):
        x0, y0, z0 = points[poly[k]]
        x1, y1, z1 = points[poly[(k + 1) % count]]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length / 2 < ZERO_AREA:
        return (0.0, 0.0, 0.0), 0.0
    return (nx / length, ny / length, nz / length), length / 2


def _surfaces(mesh):
    return [p for p, t in zip(mesh["polygons"], mesh["types"]) if t in SURFACES]


def _uses(polygons):
    """{(low, high) vertex pair: how many polygon sides run along it}."""
    uses = {}
    for poly in polygons:
        count = len(poly)
        if count < 3:
            continue
        for k in range(count):
            a, b = poly[k], poly[(k + 1) % count]
            key = (a, b) if a < b else (b, a)
            uses[key] = uses.get(key, 0) + 1
    return uses


def _bucket(count):
    return "1-2" if count < 3 else "5+" if count > 4 else str(count)


def _tally(counts):
    found = {}
    for count in counts:
        key = _bucket(count)
        found[key] = found.get(key, 0) + 1
    return found


def sides(mesh):
    """Surface polygons by side count: {"1-2", "3", "4", "5+": n}, those
    present only."""
    return _tally(len(p) for p in _surfaces(mesh))


def _poles(uses):
    valence, border = {}, set()
    for (a, b), n in uses.items():
        valence[a] = valence.get(a, 0) + 1
        valence[b] = valence.get(b, 0) + 1
        if n == 1:
            border.update((a, b))
    return _tally(v for i, v in sorted(valence.items())
                  if v != 4 and i not in border)


def poles(mesh):
    """Vertices off the border whose edge count isn't 4, by that count."""
    return _poles(_uses(_surfaces(mesh)))


def _borders(uses):
    edges = [e for e, n in uses.items() if n == 1]
    links = {}
    for a, b in edges:
        links.setdefault(a, []).append(b)
        links.setdefault(b, []).append(a)
    loops, seen = 0, set()
    for start in links:
        if start in seen:
            continue
        loops += 1
        seen.add(start)
        stack = [start]
        while stack:
            for nxt in links[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return loops, len(edges)


def borders(mesh):
    """(loops, edges): edges with one polygon, and how many connected runs
    they chain into. Two loops that touch at a vertex count as one."""
    return _borders(_uses(_surfaces(mesh)))


def _nonmanifold(uses):
    return sum(1 for n in uses.values() if n > 2)


def nonmanifold(mesh):
    """Edges with three or more polygons."""
    return _nonmanifold(_uses(_surfaces(mesh)))


def lone(mesh):
    """Vertices in no polygon of any type."""
    used = set()
    for poly in mesh["polygons"]:
        used.update(poly)
    return len(mesh["positions"]) - len(used)


def parts(mesh):
    """Pieces joined by no shared vertex, over polygons of every type. A
    lone vertex is not a part; lone() counts it."""
    parent = {}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for poly in mesh["polygons"]:
        for i in poly:
            parent.setdefault(i, i)
        first = root(poly[0]) if poly else None
        for i in poly[1:]:
            other = root(i)
            if other != first:
                parent[other] = first
    return sum(1 for i in parent if parent[i] == i)


def _areas(mesh, polygons):
    points = mesh["positions"]
    return [newell(points, p)[1] for p in polygons]


def zero_area(mesh):
    """Surface polygons with no area."""
    return sum(1 for a in _areas(mesh, _surfaces(mesh)) if a == 0)


def area(mesh):
    """Total area of the surface polygons, square metres."""
    return sum(_areas(mesh, _surfaces(mesh)))


def _volume(points, polygons):
    """Signed volume: each polygon fanned from its first vertex, each
    triangle's tetrahedron with the origin. 0 when the total is float
    noise against the tetrahedra's sizes (FLAT_VOLUME)."""
    total = size = 0.0
    for poly in polygons:
        if len(poly) < 3:
            continue
        x0, y0, z0 = points[poly[0]]
        for k in range(1, len(poly) - 1):
            x1, y1, z1 = points[poly[k]]
            x2, y2, z2 = points[poly[k + 1]]
            tet = (x0 * (y1 * z2 - z1 * y2) + y0 * (z1 * x2 - x1 * z2)
                   + z0 * (x1 * y2 - y1 * x2))
            total += tet
            size += abs(tet)
    if abs(total) <= FLAT_VOLUME * size:
        return 0.0
    return total / 6


def volume(mesh):
    """Signed volume enclosed, cubic metres; negative when the normals
    point in. None when there is a border, a non-manifold edge, or no
    surface polygon of three or more points."""
    polygons = _surfaces(mesh)
    uses = _uses(polygons)
    if not uses or any(n != 2 for n in uses.values()):
        return None
    return _volume(mesh["positions"], polygons)


def summary(mesh):
    """Every summary fact of one mesh, rounded: the one place the shape
    facts are rounded on their way out."""
    polygons = _surfaces(mesh)
    uses = _uses(polygons)
    areas = _areas(mesh, polygons)
    loops, border_edges = _borders(uses)
    out = counts(mesh)
    kinds = types(mesh["types"])
    if kinds:
        out["types"] = kinds
    out["sides"] = _tally(len(p) for p in polygons)
    out["poles"] = _poles(uses)
    out["boundary_loops"] = loops
    out["boundary_edges"] = border_edges
    for name, value in (("nonmanifold_edges", _nonmanifold(uses)),
                        ("lone_vertices", lone(mesh)),
                        ("zero_area", sum(1 for a in areas if a == 0))):
        if value:
            out[name] = value
    out["parts"] = parts(mesh)
    out["box"] = box(mesh["positions"])
    out["area"] = significant(sum(areas))
    closed = uses and all(n == 2 for n in uses.values())
    out["volume"] = significant(
        _volume(mesh["positions"], polygons) if closed else None)
    return out


# --- The selection. Entries count the same surface polygons as the
# shape facts: an edge's polygons and a vertex's valence are read off the
# surface polygons' sides, so they agree with borders and poles.

MODES = ("vertex", "edge", "polygon")


def _point(p, places=6):
    return [rounded(v, places) for v in p]


def _sides(mesh):
    """{(low, high) vertex pair: [index of each surface polygon along it]}."""
    found = {}
    for index, (poly, kind) in enumerate(zip(mesh["polygons"], mesh["types"])):
        count = len(poly)
        if kind not in SURFACES or count < 3:
            continue
        for k in range(count):
            a, b = poly[k], poly[(k + 1) % count]
            found.setdefault((a, b) if a < b else (b, a), []).append(index)
    return found


def _centroid(points, indices):
    count = len(indices)
    return [sum(points[i][axis] for i in indices) / count for axis in range(3)]


def polygon_entry(mesh, index):
    """One polygon: its index, sides, unit normal, centroid (the mean of
    its vertices) and area, rounded. Normal and area are newell()'s, so
    the area is the one the summary adds up."""
    points, poly = mesh["positions"], mesh["polygons"][index]
    normal, size = newell(points, poly)
    return {"polygon": index, "sides": len(poly),
            "normal": _point(normal, 4),
            "centroid": _point(_centroid(points, poly)),
            "area": significant(size)}


def _key(edge):
    a, b = edge
    return (a, b) if a < b else (b, a)


def edge_entry(mesh, edge, sides=None):
    """One edge, lower vertex first: its length, how many surface polygons
    share it, and the angle in degrees between their normals; None unless
    there are exactly two, each with a normal."""
    import math
    points = mesh["positions"]
    a, b = _key(edge)
    found = (sides if sides is not None else _sides(mesh)).get((a, b), [])
    angle = None
    if len(found) == 2:
        n1, s1 = newell(points, mesh["polygons"][found[0]])
        n2, s2 = newell(points, mesh["polygons"][found[1]])
        if s1 and s2:
            dot = sum(x * y for x, y in zip(n1, n2))
            angle = rounded(math.degrees(math.acos(max(-1.0, min(1.0, dot)))), 2)
    length = sum((x - y) ** 2 for x, y in zip(points[a], points[b])) ** 0.5
    return {"edge": [a, b], "length": rounded(length), "polygons": len(found),
            "angle": angle}


def _links(sides):
    """{vertex: [valence, on a border]} over the surface polygons' sides."""
    found = {}
    for pair, polygons in sides.items():
        for i in pair:
            link = found.setdefault(i, [0, False])
            link[0] += 1
            link[1] = link[1] or len(polygons) == 1
    return found


def vertex_entry(mesh, index, links=None):
    """One vertex: its position, valence (edges of surface polygons that
    end at it) and whether it is on a border edge."""
    found = links if links is not None else _links(_sides(mesh))
    valence, border = found.get(index, (0, False))
    return {"vertex": index, "position": _point(mesh["positions"][index]),
            "valence": valence, "border": border}


def _touched(mesh, mode, selected):
    """The vertices the selection touches, each once."""
    if mode == "vertex":
        return set(selected)
    if mode == "edge":
        return {i for edge in selected for i in edge}
    return {i for p in selected for i in mesh["polygons"][p]}


def selection(mesh, mode, selected, elements=False):
    """The selection in one mode: {"mode", "count"}, and when it isn't
    empty the box and centroid of the vertices it touches, each counted
    once. elements=True adds one entry per element, ascending. selected
    holds indices, or vertex pairs in edge mode."""
    chosen = sorted({_key(e) for e in selected} if mode == "edge"
                    else set(selected))
    out = {"mode": mode, "count": len(chosen)}
    if chosen:
        points = mesh["positions"]
        touched = sorted(_touched(mesh, mode, chosen))
        out["box"] = box([points[i] for i in touched])
        out["centroid"] = _point(_centroid(points, touched))
    if elements:
        if mode == "polygon":
            out["elements"] = [polygon_entry(mesh, i) for i in chosen]
        elif mode == "edge":
            sides = _sides(mesh)
            out["elements"] = [edge_entry(mesh, e, sides) for e in chosen]
        else:
            links = _links(_sides(mesh))
            out["elements"] = [vertex_entry(mesh, i, links) for i in chosen]
    return out



# --- The surface. Modo draws a SubD mesh's surface inside its cage.
# Its own sampler hands the surface out as triangles (ops_mesh.read_surface);
# these turn them into the same facts the cage has. Modo's SubD is not plain
# Catmull-Clark (checked on 17.1, 2026-11-09), so nothing here subdivides.

def has_surface(mesh):
    """Whether Modo draws a surface other than the cage: any SubD or Pixar
    SubD polygon."""
    return any(kind in ("subd", "psub") for kind in mesh["types"])


def render_level(mesh, triangle_count):
    """The level Modo sampled at, from its triangle count. SubD level L
    cuts each side of a polygon into L, so an n-sided one is n * L * L
    quads; Pixar SubD level L halves each side L - 1 times, so it is
    n * 4 ** (L - 1) quads (the two agree only at levels 1 and 2). Two
    triangles a quad, and a plain n-gon n - 2 triangles. None when the mesh
    has no SubD polygon, when it has both kinds (each has a level of its
    own, and one count can't give two), or when no level gives that
    count."""
    sides = {"subd": 0, "psub": 0}
    plain = 0
    for poly, kind in zip(mesh["polygons"], mesh["types"]):
        if kind in sides:
            sides[kind] += len(poly)
        elif kind == "face" and len(poly) > 2:
            plain += len(poly) - 2
    if bool(sides["subd"]) == bool(sides["psub"]):
        return None
    kind = "subd" if sides["subd"] else "psub"
    count = sides[kind]
    for level in range(1, 65):
        cuts = level * level if kind == "subd" else 4 ** (level - 1)
        triangles = 2 * count * cuts + plain
        if triangles == triangle_count:
            return level
        if triangles > triangle_count:
            break
    return None


def surface(points, triangles, level, closed):
    """The sampled surface's facts, rounded as summary() rounds: {"level",
    "triangles", "box", "area", "volume"}. The sampler repeats each point
    once per polygon around it, and two patches' copies of a point differ
    in the last bits, so nothing here relies on shared points: closed says
    whether the cage is closed, which its surface then is too."""
    return {"level": level, "triangles": len(triangles), "box": box(points),
            "area": significant(sum(newell(points, t)[1] for t in triangles)),
            "volume": significant(_volume(points, triangles) if closed and
                                  triangles else None)}
