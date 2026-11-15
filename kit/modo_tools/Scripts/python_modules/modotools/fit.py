# python
"""fit: the arithmetic of mt_fit_backdrops, with nothing from
Modo, so every step is tested on small images and meshes.

  mask      an image's shape: what isn't background (clear, or near-white
            and reached from the border), its largest piece
  Backdrop  a mask placed in metres, as the backdrop shows it in its view
  thin,     a large image read every nth pixel, each kept pixel where it
  thinned     was
  Shape     two masks as smooth slices along the axis their views share:
            at each height a superellipse whose widths the two views give
  cast      each cage point cast from a centre inside the shape to its edge
  normals,  the second pass: each surface point cast along its normal, which
  cast_along  reaches an end above or below every cage point (a tail)
  taubin    the targets smoothed without shrinking
  rule      the point on Modo's SubD surface each cage point gives
  solve     the cage moved until its surface points land on the targets
  iou       a surface's outline in a view against that view's mask

An agent (2026-11-10) did this in about 250 lines of agent code;
the slices, the cast and the smoothing follow it. The solve doesn't use
its Catmull-Clark limit formula: a test showed Modo's SubD isn't plain
Catmull-Clark, and a probe found the rule Modo's sampled surface
follows at each cage point (to 1e-8 m on a 706-point test cage): one
Catmull-Clark step, then each point the mean of its new faces' centroids.
The op measures Modo's own surface after it writes, so the rule only says
where to look.
"""

import collections
import math

# A clear pixel: alpha under this. A white one: each of r, g, b over this.
ALPHA = 128
WHITE = 235

# Each slice's cross-section: |a|^p + |b|^p <= 1. 2 is an ellipse; a test
# used 2.2, a little fuller.
SUPER = 2.2

# The world axis and its sign along the image's right (u) and up (v), as
# Modo shows a backdrop in each view (Y up; the left view looks along +X,
# so +Z is to the right; the top view looks down, -Z up the screen).
VIEWS = {"left": (2, 1, 1, 1), "right": (2, -1, 1, 1),
         "front": (0, 1, 1, 1), "back": (0, -1, 1, 1),
         "top": (0, 1, 2, -1), "bottom": (0, 1, 2, 1)}

# Ray steps over the shape's size, and bisections after the first step out.
STEPS = 200
BISECT = 30

# Taubin's two factors.
LAMBDA, MU = 0.5, -0.53


class FitError(ValueError):
    """A request the fit can't do; the op answers it as bad_request."""

    def __init__(self, message, hint=""):
        ValueError.__init__(self, message)
        self.hint = hint


class Diverged(RuntimeError):
    """The solve moved a point further than the limit: it isn't settling."""


# -- masks ---------------------------------------------------------------------------

class Mask(object):
    def __init__(self, width, height, bits):
        self.width, self.height, self.bits = width, height, bits
        self.area = sum(sum(row) for row in bits)


def mask(width, height, rows):
    """The shape in an image: rows top first, each r, g, b, a bytes. A pixel
    is background when clear, or near-white and joined to the border by
    background (so a highlight inside the shape stays shape); of the shape,
    the largest 4-connected piece."""
    pale = []
    for y in range(height):
        row = rows[y]
        line = bytearray(width)
        for x in range(width):
            r, g, b, a = row[4 * x:4 * x + 4]
            if a < ALPHA or (r > WHITE and g > WHITE and b > WHITE):
                line[x] = 1
        pale.append(line)
    background = [bytearray(width) for _ in range(height)]
    queue = collections.deque()
    for x in range(width):
        for y in (0, height - 1):
            queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        if not (0 <= x < width and 0 <= y < height):
            continue
        if background[y][x] or not pale[y][x]:
            continue
        background[y][x] = 1
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    # The largest piece of what isn't background.
    seen = [bytearray(width) for _ in range(height)]
    best = []
    for y0 in range(height):
        for x0 in range(width):
            if background[y0][x0] or seen[y0][x0]:
                continue
            piece, stack = [], [(x0, y0)]
            seen[y0][x0] = 1
            while stack:
                x, y = stack.pop()
                piece.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (0 <= nx < width and 0 <= ny < height
                            and not background[ny][nx] and not seen[ny][nx]):
                        seen[ny][nx] = 1
                        stack.append((nx, ny))
            if len(piece) > len(best):
                best = piece
    bits = [bytearray(width) for _ in range(height)]
    for x, y in best:
        bits[y][x] = 1
    return Mask(width, height, bits)


class Backdrop(object):
    """A mask as a backdrop shows it: in view, pixel metres a pixel, its
    image centred at center (world). axes: the world axes of the image's
    right and up."""

    def __init__(self, name, view, mask, pixel, center):
        if view not in VIEWS:
            raise FitError("%s is in the %r view" % (name, view),
                           hint="A backdrop's view is one of %s." % ", ".join(VIEWS))
        self.name, self.view, self.mask = name, view, mask
        self.pixel, self.center = float(pixel), [float(c) for c in center]
        au, su, av, sv = VIEWS[view]
        self.axes, self.signs = (au, av), (su, sv)

    def world(self, px, py):
        """(u, v) in world metres along axes of the centre of pixel (px, py),
        px from the left, py from the top; fractions lie between centres."""
        su, sv = self.signs
        u = self.center[self.axes[0]] + su * (px + 0.5 - self.mask.width / 2.0) * self.pixel
        v = self.center[self.axes[1]] + sv * (self.mask.height / 2.0 - (py + 0.5)) * self.pixel
        return u, v

    def pixel_of(self, u, v):
        """The fractional pixel index (px, py) whose centre is world (u, v)."""
        su, sv = self.signs
        px = (u - self.center[self.axes[0]]) / (su * self.pixel) + self.mask.width / 2.0 - 0.5
        py = self.mask.height / 2.0 - 0.5 - (v - self.center[self.axes[1]]) / (sv * self.pixel)
        return px, py


def thin(width, height, side):
    """The step that reads an image as no more than side pixels a side:
    every step-th pixel (1: all of them)."""
    return max(1, int(math.ceil(max(width, height) / float(side))))


def thinned(view, center, pixel, width, height, step):
    """(columns, lines, pixel, center) for an image read every step-th
    pixel (a 2310 x 2470 image held Modo for 25 s): the
    pixels kept, from the middle of each step; the size of one; and the
    centre that leaves each kept pixel where the whole image had it."""
    columns = list(range(step // 2, width, step))
    lines = list(range(step // 2, height, step))
    au, su, av, sv = VIEWS[view]

    def shift(whole, kept):
        return (step // 2 + 0.5 - step / 2.0 + kept * step / 2.0
                - whole / 2.0) * pixel

    moved = [float(c) for c in center]
    moved[au] += su * shift(width, len(columns))
    moved[av] -= sv * shift(height, len(lines))
    return columns, lines, pixel * step, moved


def blurred(m, radius):
    """The mask as floats, box-blurred twice by radius pixels each way (a
    near-Gaussian), which rounds the outline's pixel steps and small bumps."""
    grid = [[float(v) for v in row] for row in m.bits]
    if radius <= 0:
        return grid
    w, h = m.width, m.height

    def along_rows(g):
        out = []
        for row in g:
            sums = [0.0]
            for v in row:
                sums.append(sums[-1] + v)
            w = len(row)
            out.append([(sums[min(w, x + radius + 1)] - sums[max(0, x - radius)])
                        / (2 * radius + 1) for x in range(w)])
        return out

    def transpose(g, rows, cols):
        return [[g[r][c] for r in range(rows)] for c in range(cols)]

    for _ in range(2):
        grid = along_rows(grid)
        grid = transpose(along_rows(transpose(grid, h, w)), w, h)
    return grid


class Profile(object):
    """One backdrop's widths along the shared axis: for each line of pixels
    across it, the intervals (world metres, along the view's other axis)
    where the blurred mask is at least half."""

    def __init__(self, backdrop, axis, radius):
        self.backdrop = backdrop
        grid = blurred(backdrop.mask, radius)
        w, h = backdrop.mask.width, backdrop.mask.height
        self.by_column = backdrop.axes[0] == axis
        self.other = backdrop.axes[1] if self.by_column else backdrop.axes[0]
        count, across = (w, h) if self.by_column else (h, w)
        self.lines = []
        for k in range(count):
            values = ([grid[j][k] for j in range(across)] if self.by_column
                      else grid[k])
            self.lines.append(self._intervals(values, k))
        # World s of each line, and its step (signed).
        self.s0 = self._s(0)
        self.step = self._s(1) - self.s0
        filled = [k for k, line in enumerate(self.lines) if line]
        if filled:
            ends = (self._s(filled[0]), self._s(filled[-1]))
            half = abs(self.step) / 2.0
            self.span = (min(ends) - half, max(ends) + half)
        else:
            self.span = None

    def _s(self, k):
        if self.by_column:
            return self.backdrop.world(k, 0)[0]
        return self.backdrop.world(0, k)[1]

    def _across(self, j):
        """World metres along the other axis of fractional index j."""
        if self.by_column:
            return self.backdrop.world(0, j)[1]
        return self.backdrop.world(j, 0)[0]

    def _intervals(self, values, k):
        out, start = [], None
        n = len(values)
        for j in range(n + 1):
            v = values[j] if j < n else 0.0
            before = values[j - 1] if j > 0 else 0.0
            if (v >= 0.5) != (before >= 0.5):
                # Where the line through the two pixel centres crosses a half.
                t = (0.5 - before) / (v - before)
                edge = self._across(j - 1 + t)
                if v >= 0.5:
                    start = edge
                else:
                    out.append((min(start, edge), max(start, edge)))
        return out

    def at(self, s):
        """The intervals at world s: between two lines with as many
        intervals, their ends blended; else the nearer line's."""
        f = (s - self.s0) / self.step
        n = len(self.lines)
        if f < -0.5 or f > n - 0.5:
            return []
        k0 = int(math.floor(f))
        t = f - k0
        a = self.lines[k0] if 0 <= k0 < n else []
        b = self.lines[k0 + 1] if 0 <= k0 + 1 < n else []
        if a and b and len(a) == len(b):
            return [(p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
                    for p, q in zip(a, b)]
        return a if t < 0.5 else b


class Shape(object):
    """Two backdrops as slices along the axis their views share: at each
    s, for each pair of an interval from each view, a superellipse filling
    the rectangle the pair makes. blur: pixels the masks are blurred by."""

    def __init__(self, first, second, blur=0):
        shared = set(first.axes) & set(second.axes)
        if len(shared) != 1:
            raise FitError(
                "%s (%s view) and %s (%s view) see the same two axes" % (
                    first.name, first.view, second.name, second.view),
                hint="Fit to two backdrops in views at right angles: front or "
                     "back with left, right, top or bottom; left or right with "
                     "top or bottom.")
        self.axis = shared.pop()
        self.views = (first, second)
        self.a = Profile(first, self.axis, blur)
        self.b = Profile(second, self.axis, blur)
        if self.a.span is None or self.b.span is None:
            empty = first if self.a.span is None else second
            raise FitError("%s's image has no shape in it" % empty.name)
        lo = max(self.a.span[0], self.b.span[0])
        hi = min(self.a.span[1], self.b.span[1])
        if lo >= hi:
            raise FitError(
                "%s and %s don't overlap along %s" % (
                    first.name, second.name, "XYZ"[self.axis]),
                hint="Place the backdrops so the shape in each spans the same "
                     "heights (mt_backdrop's center).")
        self.span = (lo, hi)
        self.size = max(hi - lo, self._width())
        self._band()

    def _width(self):
        wide = 0.0
        for profile in (self.a, self.b):
            for line in profile.lines:
                for lo, hi in line:
                    wide = max(wide, hi - lo)
        return wide

    def pairs(self, s):
        """[(centre a, half a, centre b, half b)] at s, a along the first
        view's other axis and b the second's."""
        out = []
        for a0, a1 in self.a.at(s):
            for b0, b1 in self.b.at(s):
                out.append(((a0 + a1) / 2.0, (a1 - a0) / 2.0,
                            (b0 + b1) / 2.0, (b1 - b0) / 2.0))
        return out

    def inside(self, p):
        s = p[self.axis]
        if not self.span[0] <= s <= self.span[1]:
            return False
        pa, pb = p[self.a.other], p[self.b.other]
        for ca, ha, cb, hb in self.pairs(s):
            if ha <= 0 or hb <= 0:
                continue
            if (abs(pa - ca) / ha) ** SUPER + (abs(pb - cb) / hb) ** SUPER <= 1.0:
                return True
        return False

    def _band(self):
        """The heights the cast's centres keep to: from each end, in until
        the distance to the end is the slice's half width, so a point near
        an end is cast from inside the end's round, not along it."""
        lo, hi = self.span
        step = min(abs(self.a.step), abs(self.b.step)) / 2.0
        n = max(2, int((hi - lo) / step))
        heights = [lo + (hi - lo) * i / n for i in range(n + 1)]

        def radius(s):
            pairs = self.pairs(s)
            return max([min(ha, hb) for _ca, ha, _cb, hb in pairs] or [0.0])

        # A slice with no width at the very end isn't far enough in.
        radii = [(s, radius(s)) for s in heights]
        top = next((s for s, r in reversed(radii) if r > 0 and hi - s >= r), lo)
        bottom = next((s for s, r in radii if r > 0 and s - lo >= r), hi)
        if bottom > top:
            bottom = top = (bottom + top) / 2.0
        self.band = (bottom, top)

    def centre(self, p):
        """The point a cage point is cast from: on the slice at its height
        held to the band, the centre of the pair nearest it."""
        s = min(max(p[self.axis], self.band[0]), self.band[1])
        pairs = [q for q in self.pairs(s) if q[1] > 0 and q[3] > 0]
        if not pairs:
            return None
        pa, pb = p[self.a.other], p[self.b.other]
        ca, _ha, cb, _hb = min(pairs, key=lambda q: ((pa - q[0]) / q[1]) ** 2
                                + ((pb - q[2]) / q[3]) ** 2)
        c = [0.0, 0.0, 0.0]
        c[self.axis], c[self.a.other], c[self.b.other] = s, ca, cb
        return c


# -- the cast -------------------------------------------------------------------------

def cast(points, shape, centres=None):
    """(targets, uncast): each point cast from its centre (shape.centre, or
    centres given) along the line through it to where the line first leaves
    the shape. A point whose centre isn't inside the shape stays put and is
    counted. A point at its centre is cast along the shared axis, away from
    the shape's middle."""
    targets, uncast = [], 0
    step = shape.size / STEPS
    middle = (shape.span[0] + shape.span[1]) / 2.0
    for i, p in enumerate(points):
        c = centres[i] if centres is not None else shape.centre(p)
        if c is None or not shape.inside(c):
            targets.append(list(p))
            uncast += 1
            continue
        d = [p[k] - c[k] for k in range(3)]
        length = math.sqrt(sum(v * v for v in d))
        if length < 1e-12:
            d = [0.0, 0.0, 0.0]
            d[shape.axis] = 1.0 if p[shape.axis] >= middle else -1.0
        else:
            d = [v / length for v in d]
        t = 0.0
        while t < 4 * shape.size:
            nxt = t + step
            if not shape.inside([c[k] + d[k] * nxt for k in range(3)]):
                break
            t = nxt
        lo, hi = t, t + step
        for _ in range(BISECT):
            mid = (lo + hi) / 2.0
            if shape.inside([c[k] + d[k] * mid for k in range(3)]):
                lo = mid
            else:
                hi = mid
        targets.append([c[k] + d[k] * lo for k in range(3)])
    return targets, uncast


def normals(points, polygons):
    """Each point's normal: the sum of its polygons' (Newell's) normals,
    made unit, pointing out of a closed mesh (flipped all together when
    the polygons wind the other way). A point on no polygon: (0, 0, 0)."""
    sums = [[0.0, 0.0, 0.0] for _ in points]
    volume = 0.0
    for polygon in polygons:
        n = [0.0, 0.0, 0.0]
        m = len(polygon)
        for i in range(m):
            a, b = points[polygon[i]], points[polygon[(i + 1) % m]]
            n[0] += (a[1] - b[1]) * (a[2] + b[2])
            n[1] += (a[2] - b[2]) * (a[0] + b[0])
            n[2] += (a[0] - b[0]) * (a[1] + b[1])
        for v in polygon:
            for k in range(3):
                sums[v][k] += n[k]
        # Signed volume of the fan from the origin: its sign says which way
        # the polygons wind.
        p0 = points[polygon[0]]
        for i in range(1, m - 1):
            p1, p2 = points[polygon[i]], points[polygon[i + 1]]
            volume += (p0[0] * (p1[1] * p2[2] - p1[2] * p2[1])
                       - p0[1] * (p1[0] * p2[2] - p1[2] * p2[0])
                       + p0[2] * (p1[0] * p2[1] - p1[1] * p2[0]))
    sign = -1.0 if volume < 0 else 1.0
    out = []
    for s in sums:
        length = math.sqrt(sum(c * c for c in s))
        out.append([sign * c / length for c in s] if length > 0 else [0.0, 0.0, 0.0])
    return out


def cast_along(points, directions, shape, reach):
    """(targets, uncast): each point moved along its direction to the
    shape's edge: outward until it leaves the shape when it starts inside,
    inward until it enters when it starts outside, no further than reach
    either way. A point with no edge within reach (or no direction) stays
    and is counted."""
    targets, uncast = [], 0
    step = shape.size / STEPS
    for p, d in zip(points, directions):
        inside = shape.inside(p)
        sign = 1.0 if inside else -1.0
        t, found = 0.0, False
        while t < reach:
            nxt = t + step
            q = [p[k] + sign * d[k] * nxt for k in range(3)]
            if shape.inside(q) != inside:
                found = True
                break
            t = nxt
        if not found:
            targets.append(list(p))
            uncast += 1
            continue
        lo, hi = t, t + step
        for _ in range(BISECT):
            mid = (lo + hi) / 2.0
            q = [p[k] + sign * d[k] * mid for k in range(3)]
            if shape.inside(q) == inside:
                lo = mid
            else:
                hi = mid
        edge = lo if inside else hi
        targets.append([p[k] + sign * d[k] * edge for k in range(3)])
    return targets, uncast


# -- smoothing, Modo's rule and the solve ------------------------------------------------

def neighbours(polygons, count):
    """Each point's neighbours along polygon edges."""
    out = [set() for _ in range(count)]
    for polygon in polygons:
        n = len(polygon)
        for i, v in enumerate(polygon):
            out[v].add(polygon[(i + 1) % n])
            out[v].add(polygon[i - 1])
    return [sorted(s) for s in out]


def taubin(points, near, fitted, rounds):
    """Taubin's smoothing (a shrink, then an un-shrink) of the fitted
    points, rounds times; the others hold."""
    fitted = set(fitted)
    P = [list(p) for p in points]
    for _ in range(rounds):
        for factor in (LAMBDA, MU):
            Q = [p for p in P]
            for i in fitted:
                if not near[i]:
                    continue
                n = float(len(near[i]))
                avg = [sum(P[u][k] for u in near[i]) / n for k in range(3)]
                Q[i] = [P[i][k] + factor * (avg[k] - P[i][k]) for k in range(3)]
            P = Q
    return P


def rule(points, polygons):
    """Where Modo's SubD surface passes for each cage point (17.1, found by
    a probe): one Catmull-Clark step, then the point is the mean
    of the centroids of the new faces around it. A point on no polygon is
    its own."""
    n = len(points)
    P = points
    face = [[sum(P[v][k] for v in f) / float(len(f)) for k in range(3)]
            for f in polygons]
    edge_faces = {}
    vertex_faces = [[] for _ in range(n)]
    for fi, f in enumerate(polygons):
        m = len(f)
        for i, v in enumerate(f):
            vertex_faces[v].append(fi)
            e = (v, f[(i + 1) % m]) if v < f[(i + 1) % m] else (f[(i + 1) % m], v)
            edge_faces.setdefault(e, []).append(fi)
    edge = {}
    vertex_edges = [[] for _ in range(n)]
    for e, faces in edge_faces.items():
        a, b = e
        vertex_edges[a].append(e)
        vertex_edges[b].append(e)
        if len(faces) == 2:
            edge[e] = [(P[a][k] + P[b][k] + face[faces[0]][k] + face[faces[1]][k])
                       / 4.0 for k in range(3)]
        else:
            edge[e] = [(P[a][k] + P[b][k]) / 2.0 for k in range(3)]
    moved = []
    for v in range(n):
        faces = vertex_faces[v]
        m = len(faces)
        if not m:
            moved.append(list(P[v]))
            continue
        edges = vertex_edges[v]
        boundary = [e for e in edges if len(edge_faces[e]) != 2]
        if boundary:
            # An open edge: Catmull-Clark's crease rule.
            ends = [e[0] if e[1] == v else e[1] for e in boundary]
            moved.append([(6 * P[v][k] + sum(P[u][k] for u in ends)) /
                          (6.0 + len(ends)) for k in range(3)]
                         if len(ends) == 2 else list(P[v]))
            continue
        F = [sum(face[f][k] for f in faces) / m for k in range(3)]
        R = [sum((P[a][k] + P[b][k]) / 2.0 for a, b in edges) / len(edges)
             for k in range(3)]
        moved.append([(F[k] + 2 * R[k] + (m - 3) * P[v][k]) / m for k in range(3)])
    out = []
    for v in range(n):
        faces = vertex_faces[v]
        if not faces:
            out.append(list(P[v]))
            continue
        total = [0.0, 0.0, 0.0]
        for fi in faces:
            f = polygons[fi]
            m = len(f)
            i = f.index(v)
            e1 = (v, f[(i + 1) % m]) if v < f[(i + 1) % m] else (f[(i + 1) % m], v)
            e2 = (v, f[i - 1]) if v < f[i - 1] else (f[i - 1], v)
            for k in range(3):
                total[k] += (moved[v][k] + edge[e1][k] + edge[e2][k]
                             + face[fi][k]) / 4.0
        out.append([c / len(faces) for c in total])
    return out


def solve(points, polygons, targets, fitted, rounds, rule=rule, limit=None):
    """The cage whose surface points (rule) land on targets at the fitted
    points: each round, each fitted point moves by what its surface point
    misses by. The rule is a smoothing, so this settles; a point moved
    further than limit from where it began raises Diverged."""
    fitted = list(fitted)
    P = [list(p) for p in points]
    for _ in range(rounds):
        S = rule(P, polygons)
        for i in fitted:
            P[i] = [P[i][k] + targets[i][k] - S[i][k] for k in range(3)]
        if limit is not None:
            for i in fitted:
                if math.dist(P[i], points[i]) > limit:
                    raise Diverged("point %d moved %.4g m, past %.4g m" % (
                        i, math.dist(P[i], points[i]), limit))
    return P


def floored(points, floor, fitted, axis=1):
    """The points with each fitted one's height (axis) at least floor."""
    if floor is None:
        return points
    out = [list(p) for p in points]
    for i in fitted:
        if out[i][axis] < floor:
            out[i][axis] = floor
    return out


def to_local(points, matrix, offset):
    """World points back into an item's own space: the inverse of
    meshmath.to_world (matrix * p + offset)."""
    (a, b, c), (d, e, f), (g, h, i) = matrix
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-15:
        raise FitError("the mesh's transform is flat (scaled to 0)",
                       hint="Set the mesh's scale above 0 before fitting.")
    inv = ((e * i - f * h, c * h - b * i, b * f - c * e),
           (f * g - d * i, a * i - c * g, c * d - a * f),
           (d * h - e * g, b * g - a * h, a * e - b * d))
    out = []
    for p in points:
        q = [p[k] - offset[k] for k in range(3)]
        out.append([sum(inv[r][k] * q[k] for k in range(3)) / det for r in range(3)])
    return out


# -- the overlap ------------------------------------------------------------------------

def outline(points, triangles, backdrop):
    """The pixels of backdrop's grid whose centres the triangles cover,
    projected into its view."""
    w, h = backdrop.mask.width, backdrop.mask.height
    au, av = backdrop.axes
    flat = [backdrop.pixel_of(p[au], p[av]) for p in points]
    bits = [bytearray(w) for _ in range(h)]
    for a, b, c in triangles:
        (x0, y0), (x1, y1), (x2, y2) = flat[a], flat[b], flat[c]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if area == 0:
            continue
        lo_x = max(0, int(math.ceil(min(x0, x1, x2))))
        hi_x = min(w - 1, int(math.floor(max(x0, x1, x2))))
        lo_y = max(0, int(math.ceil(min(y0, y1, y2))))
        hi_y = min(h - 1, int(math.floor(max(y0, y1, y2))))
        for y in range(lo_y, hi_y + 1):
            row = bits[y]
            for x in range(lo_x, hi_x + 1):
                if row[x]:
                    continue
                d0 = (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)
                d1 = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
                d2 = (x0 - x2) * (y - y2) - (y0 - y2) * (x - x2)
                if area > 0:
                    if d0 >= 0 and d1 >= 0 and d2 >= 0:
                        row[x] = 1
                elif d0 <= 0 and d1 <= 0 and d2 <= 0:
                    row[x] = 1
    return bits


def iou(points, triangles, backdrop):
    """Overlap of the triangles' outline with backdrop's mask: shared pixels
    over pixels in either. 0 when neither has any."""
    drawn = outline(points, triangles, backdrop)
    both = either = 0
    for row, ref in zip(drawn, backdrop.mask.bits):
        for x, y in zip(row, ref):
            if x or y:
                either += 1
                if x and y:
                    both += 1
    return both / float(either) if either else 0.0
