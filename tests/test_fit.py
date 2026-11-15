"""Fit.py, the fit's arithmetic with nothing from Modo. Masks
from images, slices in metres, the cast, the targets' smoothing, the solve
against Modo's own SubD point rule, and the overlap (IoU) of a surface's
outline with a mask."""

import math
import unittest

import support  # noqa: F401  (puts the kit on sys.path)

from modotools import fit

WHITE = (255, 255, 255, 255)
RED = (200, 30, 30, 255)
CLEAR = (0, 0, 0, 0)


def rows_of(width, height, colour_at):
    """RGBA rows, top first, as Modo's Image.GetLine gives them."""
    out = []
    for y in range(height):
        line = []
        for x in range(width):
            line.extend(colour_at(x, y))
        out.append(line)
    return out


def ellipse_rows(width, height, rx, ry, background=WHITE):
    """An ellipse of radii rx, ry pixels at the image's centre."""
    cx, cy = width / 2.0, height / 2.0

    def colour(x, y):
        u, v = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
        return RED if u * u + v * v <= 1.0 else background
    return rows_of(width, height, colour)


def backdrop(view, rows, width, height, pixel, center=(0.0, 0.0, 0.0),
             name=None):
    return fit.Backdrop(name or view, view, fit.mask(width, height, rows),
                        pixel, list(center))


def sphere(n=4, radius=1.0, center=(0.0, 0.0, 0.0)):
    """A cube of n x n quads a side, pushed out to a sphere: (positions,
    polygons)."""
    index, points, polygons = {}, [], []

    def point(p):
        key = tuple(round(c, 9) for c in p)
        if key not in index:
            index[key] = len(points)
            points.append(p)
        return index[key]

    for axis in range(3):
        for side in (-1.0, 1.0):
            a, b = [i for i in range(3) if i != axis]
            for i in range(n):
                for j in range(n):
                    quad = []
                    for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                        p = [0.0, 0.0, 0.0]
                        p[axis] = side
                        p[a] = -1.0 + 2.0 * (i + di) / n
                        p[b] = -1.0 + 2.0 * (j + dj) / n
                        quad.append(point(p))
                    polygons.append(quad)
    # Wind every quad one way: its normal out from the centre.
    for n_, quad in enumerate(polygons):
        a, b, c = (points[i] for i in quad[:3])
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                  u[0] * v[1] - u[1] * v[0]]
        if sum(normal[k] * a[k] for k in range(3)) < 0:
            polygons[n_] = quad[::-1]
    out = []
    for p in points:
        length = math.sqrt(sum(c * c for c in p))
        out.append([center[k] + radius * p[k] / length for k in range(3)])
    return out, polygons


# -- masks ------------------------------------------------------------------------

class MaskTest(unittest.TestCase):
    def test_a_two_colour_image_is_its_shape(self):
        rows = rows_of(6, 5, lambda x, y: RED if 1 <= x <= 3 and 2 <= y <= 3
                       else WHITE)
        m = fit.mask(6, 5, rows)
        self.assertEqual(m.area, 6)
        self.assertEqual([list(r) for r in m.bits],
                         [[0] * 6, [0] * 6, [0, 1, 1, 1, 0, 0],
                          [0, 1, 1, 1, 0, 0], [0] * 6])

    def test_clear_pixels_are_background(self):
        rows = rows_of(4, 4, lambda x, y: RED if x == 1 and y == 1 else CLEAR)
        self.assertEqual(fit.mask(4, 4, rows).area, 1)

    def test_white_inside_the_shape_is_shape(self):
        # A highlight in the photo isn't a hole in the model.
        def colour(x, y):
            if 1 <= x <= 5 and 1 <= y <= 5:
                return WHITE if (x, y) == (3, 3) else RED
            return WHITE
        self.assertEqual(fit.mask(7, 7, rows_of(7, 7, colour)).area, 25)

    def test_only_the_largest_piece_is_kept(self):
        def colour(x, y):
            if 1 <= x <= 3 and 1 <= y <= 3:
                return RED
            return RED if (x, y) == (6, 6) else WHITE
        m = fit.mask(8, 8, rows_of(8, 8, colour))
        self.assertEqual(m.area, 9)
        self.assertEqual(m.bits[6][6], 0)

    def test_a_wide_mask_blurs_to_its_own_size(self):
        # Modo check: real backdrops were 231 x 247 px.
        rows = rows_of(9, 4, lambda x, y: RED if 2 <= x <= 6 and 1 <= y <= 2
                       else WHITE)
        grid = fit.blurred(fit.mask(9, 4, rows), 1)
        self.assertEqual((len(grid), len(grid[0])), (4, 9))
        self.assertGreater(grid[1][4], grid[0][0])

    def test_an_image_with_no_shape_has_area_0(self):
        self.assertEqual(fit.mask(3, 3, rows_of(3, 3, lambda x, y: WHITE)).area, 0)


# -- masks in metres --------------------------------------------------------------

class ThinTest(unittest.TestCase):
    """Found 2026-11-11: a large image is read every nth
    pixel, and each pixel kept stays where the whole image had it."""

    def test_the_step(self):
        self.assertEqual(fit.thin(231, 247, 512), 1)
        self.assertEqual(fit.thin(512, 100, 512), 1)
        self.assertEqual(fit.thin(513, 100, 512), 2)
        self.assertEqual(fit.thin(2310, 2470, 512), 5)

    def test_a_kept_pixel_stays_where_it_was(self):
        # 10 x 7 px read every 3rd pixel: columns 1, 4, 7 and lines 1, 4.
        rows = rows_of(10, 7, lambda x, y: RED)
        for view in fit.VIEWS:
            whole = backdrop(view, rows, 10, 7, 0.5, (10.0, 20.0, 30.0))
            columns, lines, pixel, center = fit.thinned(
                view, (10.0, 20.0, 30.0), 0.5, 10, 7, 3)
            self.assertEqual((columns, lines), ([1, 4, 7], [1, 4]))
            self.assertEqual(pixel, 1.5)
            small = backdrop(view, rows_of(3, 2, lambda x, y: RED), 3, 2,
                             pixel, center)
            for i, x in enumerate(columns):
                for j, y in enumerate(lines):
                    got, want = small.world(i, j), whole.world(x, y)
                    self.assertAlmostEqual(got[0], want[0], msg=view)
                    self.assertAlmostEqual(got[1], want[1], msg=view)

    def test_a_step_of_one_changes_nothing(self):
        columns, lines, pixel, center = fit.thinned(
            "left", (1.0, 2.0, 3.0), 0.25, 4, 3, 1)
        self.assertEqual((columns, lines, pixel, center),
                         ([0, 1, 2, 3], [0, 1, 2], 0.25, [1.0, 2.0, 3.0]))


class PlaceTest(unittest.TestCase):
    def test_a_pixel_centre_in_each_view(self):
        # 4 x 2 px at 0.5 m a pixel, centred at (10, 20, 30): pixel (0, 0)
        # is the top-left one, a quarter of a pixel in from the corner.
        rows = rows_of(4, 2, lambda x, y: RED)
        cases = {"front": (0, 1, -0.75 + 10, 0.25 + 20),
                 "back": (0, 1, 0.75 + 10, 0.25 + 20),
                 "left": (2, 1, -0.75 + 30, 0.25 + 20),
                 "right": (2, 1, 0.75 + 30, 0.25 + 20),
                 "top": (0, 2, -0.75 + 10, -0.25 + 30),
                 "bottom": (0, 2, -0.75 + 10, 0.25 + 30)}
        for view, (au, av, u, v) in cases.items():
            b = backdrop(view, rows, 4, 2, 0.5, (10.0, 20.0, 30.0))
            self.assertEqual(b.axes, (au, av), view)
            got = b.world(0, 0)
            self.assertAlmostEqual(got[0], u, msg=view)
            self.assertAlmostEqual(got[1], v, msg=view)

    def test_a_shape_spans_its_rect(self):
        # A 10 x 10 px square filling the image: front view, 0.1 m a pixel,
        # centred at (1, 2, 0): x from 0.5 to 1.5, y from 1.5 to 2.5.
        rows = rows_of(10, 10, lambda x, y: RED if 0 < x < 9 and 0 < y < 9
                       else WHITE)
        front = backdrop("front", rows, 10, 10, 0.1, (1.0, 2.0, 0.0))
        left = backdrop("left", rows, 10, 10, 0.1, (0.0, 2.0, 0.0))
        shape = fit.Shape(front, left, blur=0)
        self.assertEqual(shape.axis, 1)
        lo, hi = shape.span
        self.assertAlmostEqual(lo, 1.6, places=6)
        self.assertAlmostEqual(hi, 2.4, places=6)
        (ca, ha, cb, hb), = shape.pairs(2.0)
        self.assertAlmostEqual(ca, 1.0, places=6)
        self.assertAlmostEqual(ha, 0.4, places=6)
        self.assertAlmostEqual(cb, 0.0, places=6)
        self.assertAlmostEqual(hb, 0.4, places=6)

    def test_two_views_must_share_one_axis(self):
        rows = ellipse_rows(10, 10, 4, 4)
        for a, b in (("front", "front"), ("front", "back"), ("left", "right"),
                     ("top", "bottom")):
            with self.assertRaises(fit.FitError) as caught:
                fit.Shape(backdrop(a, rows, 10, 10, 0.1),
                          backdrop(b, rows, 10, 10, 0.1, name=b + "2"))
            self.assertIn("view", str(caught.exception))
        for a, b, axis in (("front", "left", 1), ("front", "top", 0),
                           ("left", "top", 2), ("right", "bottom", 2)):
            shape = fit.Shape(backdrop(a, rows, 10, 10, 0.1),
                              backdrop(b, rows, 10, 10, 0.1))
            self.assertEqual(shape.axis, axis, (a, b))


# -- the cast ---------------------------------------------------------------------

def round_shape(rx_front, rx_left, blur=0, size=80):
    """front: an ellipse rx_front wide, 1 m tall (radii in metres);
    left: rx_left wide, 1 m tall; 1/40 m a pixel."""
    pixel = 2.0 / size * 1.25
    half = size / 2.0
    front = backdrop("front", ellipse_rows(size, size, rx_front / pixel,
                                           1.0 / pixel), size, size, pixel)
    left = backdrop("left", ellipse_rows(size, size, rx_left / pixel,
                                         1.0 / pixel), size, size, pixel)
    assert half
    return fit.Shape(front, left, blur=blur)


def ellipsoid_value(p, rx, ry, rz):
    return (p[0] / rx) ** 2 + (p[1] / ry) ** 2 + (p[2] / rz) ** 2


class CastTest(unittest.TestCase):
    def test_a_sphere_cast_to_a_circle_lands_on_it(self):
        shape = round_shape(1.0, 1.0)
        points, _polygons = sphere(4, 0.5)
        targets, uncast = fit.cast(points, shape)
        self.assertEqual(uncast, 0)
        for t in targets:
            # Within a pixel and a half of the unit sphere (the slices are
            # superellipses, a little fuller than a circle between axes).
            self.assertLess(abs(math.sqrt(sum(c * c for c in t)) - 1.0), 0.08)

    def test_a_sphere_cast_to_an_ellipse_takes_its_widths(self):
        shape = round_shape(0.5, 0.8)
        # The cage's height is the shape's; its widths are wrong.
        points = [[0.3 * x, y, 0.6 * z] for x, y, z in sphere(4, 1.0)[0]]
        targets, _uncast = fit.cast(points, shape)
        xs = [abs(t[0]) for t in targets]
        zs = [abs(t[2]) for t in targets]
        ys = [abs(t[1]) for t in targets]
        self.assertAlmostEqual(max(xs), 0.5, delta=0.04)
        self.assertAlmostEqual(max(zs), 0.8, delta=0.04)
        self.assertAlmostEqual(max(ys), 1.0, delta=0.04)

    def test_every_target_is_on_the_shapes_edge(self):
        shape = round_shape(0.5, 0.8)
        points = [[0.3 * x, y, 0.6 * z] for x, y, z in sphere(3, 1.0)[0]]
        targets, _uncast = fit.cast(points, shape)
        for t in targets:
            self.assertTrue(shape.inside(t), t)
            out = [c * 1.03 for c in t]
            self.assertFalse(shape.inside(out), t)

    def test_a_point_near_an_end_is_cast_from_inside_its_round(self):
        # Near the top, a point cast across its own slice would land on
        # the side, far below the top; cast from inside the round, it
        # lands near the top.
        shape = round_shape(0.5, 0.5)
        (target,), _uncast = fit.cast([[0.01, 0.95, 0.0]], shape)
        self.assertGreater(target[1], 0.98)

    def test_the_band_stops_short_of_both_ends(self):
        # Modo check: on a test model the band's top was the shape's top,
        # where the slice has no width at all.
        shape = round_shape(0.5, 0.5, blur=2)
        lo, hi = shape.span
        self.assertGreater(shape.band[0], lo + 0.3)
        self.assertLess(shape.band[1], hi - 0.3)

    def test_a_point_whose_centre_is_outside_stays(self):
        shape = round_shape(0.5, 0.5)
        far = [[5.0, 9.0, 0.0]]
        targets, uncast = fit.cast(far, shape, centres=[[4.0, 9.0, 0.0]])
        self.assertEqual(uncast, 1)
        self.assertEqual(targets, far)


class CastAlongTest(unittest.TestCase):
    def test_a_spheres_normals_point_out(self):
        points, polygons = sphere(3, 1.0)
        for p, n in zip(points, fit.normals(points, polygons)):
            self.assertGreater(sum(a * b for a, b in zip(p, n)), 0.8)

    def test_normals_point_out_whichever_way_the_polygons_wind(self):
        points, polygons = sphere(3, 1.0)
        flipped = [p[::-1] for p in polygons]
        for p, n in zip(points, fit.normals(points, flipped)):
            self.assertGreater(sum(a * b for a, b in zip(p, n)), 0.8)

    def test_a_point_inside_moves_out_to_the_edge(self):
        shape = round_shape(0.5, 0.5)
        (t,), uncast = fit.cast_along([[0.0, 0.5, 0.0]], [[0.0, 1.0, 0.0]],
                                      shape, 1.0)
        self.assertEqual(uncast, 0)
        self.assertAlmostEqual(t[1], 1.0, delta=0.03)

    def test_a_point_outside_moves_in_to_the_edge(self):
        shape = round_shape(0.5, 0.5)
        (t,), _uncast = fit.cast_along([[0.9, 0.0, 0.0]], [[1.0, 0.0, 0.0]],
                                       shape, 1.0)
        self.assertAlmostEqual(t[0], 0.5, delta=0.03)

    def test_no_edge_within_reach_stays(self):
        shape = round_shape(0.5, 0.5)
        far = [[0.0, 0.0, 0.0]]
        targets, uncast = fit.cast_along(far, [[0.0, 1.0, 0.0]], shape, 0.2)
        self.assertEqual((targets, uncast), (far, 1))
        targets, uncast = fit.cast_along(far, [[0.0, 0.0, 0.0]], shape, 1.0)
        self.assertEqual((targets, uncast), (far, 1))


# -- smoothing, the rule and the solve -----------------------------------------------

class RuleTest(unittest.TestCase):
    def test_a_flat_patch_stays_flat(self):
        # 3 x 3 quads on z = 0: the inner points' surface points stay there.
        points = [[x, y, 0.0] for y in range(4) for x in range(4)]
        polygons = [[y * 4 + x, y * 4 + x + 1, (y + 1) * 4 + x + 1,
                     (y + 1) * 4 + x] for y in range(3) for x in range(3)]
        surface = fit.rule(points, polygons)
        for i in (5, 6, 9, 10):
            self.assertAlmostEqual(surface[i][2], 0.0)
            self.assertAlmostEqual(surface[i][0], points[i][0])
            self.assertAlmostEqual(surface[i][1], points[i][1])

    def test_a_cube_corner_comes_in(self):
        points, polygons = sphere(1, math.sqrt(3) / 2)   # a 1 m cube's corners
        surface = fit.rule(points, polygons)
        for p, s in zip(points, surface):
            for k in range(3):
                self.assertLess(abs(s[k]), abs(p[k]))
        # Every corner alike, by symmetry.
        self.assertAlmostEqual(abs(surface[0][0]), abs(surface[3][1]))

    def test_a_cube_corner_is_where_modo_draws_it(self):
        # Modo 17.1 sampled a 1 m SubD cube's corner at -0.236111119 on
        # each axis (2026-11-11); plain Catmull-Clark's limit is -0.2777...
        points, polygons = sphere(1, math.sqrt(3) / 2)
        surface = fit.rule(points, polygons)
        for p, s in zip(points, surface):
            for k in range(3):
                self.assertAlmostEqual(s[k], math.copysign(17 / 72.0, p[k]),
                                       places=7)

    def test_a_point_on_no_polygon_is_its_own(self):
        surface = fit.rule([[1.0, 2.0, 3.0]], [])
        self.assertEqual(surface, [[1.0, 2.0, 3.0]])


class SolveTest(unittest.TestCase):
    def test_the_surface_lands_on_the_targets(self):
        points, polygons = sphere(4, 0.5)
        targets = [[c * 1.6 / 0.5 * 0.5 for c in p] for p in points]
        cage = fit.solve(points, polygons, targets, range(len(points)), 8)
        surface = fit.rule(cage, polygons)
        worst = max(math.dist(s, t) for s, t in zip(surface, targets))
        self.assertLess(worst, 0.01)

    def test_points_not_fitted_dont_move(self):
        points, polygons = sphere(2, 0.5)
        targets = [[c * 2 for c in p] for p in points]
        cage = fit.solve(points, polygons, targets, [0, 1], 6)
        for i in range(2, len(points)):
            self.assertEqual(cage[i], points[i])
        self.assertNotEqual(cage[0], points[0])

    def test_a_diverging_solve_is_an_error(self):
        points, polygons = sphere(2, 0.5)
        targets = [list(p) for p in points]
        # A rule that pushes points out each round never settles.
        with self.assertRaises(fit.Diverged):
            fit.solve(points, polygons, targets, range(len(points)), 50,
                      rule=lambda P, F: [[-3 * c for c in p] for p in P],
                      limit=10.0)

    def test_smoothing_takes_out_a_spike(self):
        points, polygons = sphere(4, 1.0)
        neighbours = fit.neighbours(polygons, len(points))
        spiked = [list(p) for p in points]
        spiked[0] = [c * 1.5 for c in spiked[0]]
        smooth = fit.taubin(spiked, neighbours, range(len(points)), 3)
        self.assertLess(math.sqrt(sum(c * c for c in smooth[0])), 1.35)
        far = max(range(len(points)), key=lambda i: math.dist(points[i], points[0]))
        self.assertAlmostEqual(math.dist(smooth[far], points[far]), 0.0, delta=0.05)

    def test_to_local_undoes_the_world_transform(self):
        matrix = ((0.0, 0.0, 2.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0))
        offset = (3.0, 0.25, -1.0)
        local = [[1.0, 2.0, 3.0], [-0.5, 0.0, 0.25]]
        world = [[sum(matrix[r][k] * p[k] for k in range(3)) + offset[r]
                  for r in range(3)] for p in local]
        back = fit.to_local(world, matrix, offset)
        for a, b in zip(back, local):
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y)
        with self.assertRaises(fit.FitError):
            fit.to_local(world, ((0, 0, 0), (0, 1, 0), (0, 0, 1)), offset)

    def test_floor_holds(self):
        points = [[0.0, -1.0, 0.0], [0.0, 2.0, 0.0]]
        self.assertEqual(fit.floored(points, 0.25, [0, 1]),
                         [[0.0, 0.25, 0.0], [0.0, 2.0, 0.0]])
        self.assertEqual(fit.floored(points, None, [0, 1]), points)
        self.assertEqual(fit.floored(points, 0.25, [1]), points)


# -- the overlap --------------------------------------------------------------------

class IoUTest(unittest.TestCase):
    def test_a_square_over_its_own_mask_is_1(self):
        rows = rows_of(10, 10, lambda x, y: RED if 2 <= x <= 7 and 2 <= y <= 7
                       else WHITE)
        front = backdrop("front", rows, 10, 10, 1.0)
        # Two triangles covering x, y in [-3, 3]: pixels 2..7 exactly.
        points = [[-3, -3, 0], [3, -3, 0], [3, 3, 0], [-3, 3, 0]]
        triangles = [(0, 1, 2), (0, 2, 3)]
        self.assertAlmostEqual(fit.iou(points, triangles, front), 1.0)

    def test_half_over_is_a_third(self):
        rows = rows_of(10, 10, lambda x, y: RED if 2 <= x <= 7 and 2 <= y <= 7
                       else WHITE)
        front = backdrop("front", rows, 10, 10, 1.0)
        points = [[0, -3, 0], [6, -3, 0], [6, 3, 0], [0, 3, 0]]
        self.assertAlmostEqual(fit.iou(points, [(0, 1, 2), (0, 2, 3)], front),
                               18 / 48.0)

    def test_no_surface_is_0(self):
        rows = rows_of(4, 4, lambda x, y: RED)
        self.assertEqual(fit.iou([], [], backdrop("front", rows, 4, 4, 1.0)), 0.0)


if __name__ == "__main__":
    unittest.main()
