# python
"""screenshot: the active 3D viewport as a PNG.

The image doesn't cross the socket. The kit writes it to a modo_tools folder
in the temp folder and answers with the path; the server, running as the
same user on the same PC, reads it, deletes it and hands the agent the
image. A subfolder, not the temp folder's root: Modo 17.1 silently skips
writing a scene there, and gl.snapshot is not trusted to differ.

gl.snapshot on 17.1 takes a file name and no size; the image is the
viewport's size. A size scales a copy down after the capture,
through Modo's image service; the server reads the size from the file it
gets, and deletes both.
"""

import math
import os
import tempfile
import uuid

from . import errors, protocol
from .jobs import op

FOLDER = "modo_tools"


def folder():
    return os.path.join(tempfile.gettempdir(), FOLDER)


MIN_SIZE = 64

# The review of 7.1 (17.1, 2026-11-14): after an edit Modo draws a large mesh
# late, in step with its point count: 101,402 SubD points 0.25 s after the
# job, 400,000 about 1.2 s (2.5 to 2.9 microseconds a point; plain faces 2).
# A hold of 0.3 s gave the new frame 4 of 6 times on the first, 0.5 s 6 of 6.
# The kit can't tell which mesh a job changed, so the largest one counts.
DRAW_S_PER_POINT = 5e-6
DRAW_GAP_MAX_S = 5.0


def draw_gap():
    """Seconds Modo may need to draw after a job that can change the scene.
    The pump holds a screenshot that long (and never under its own 0.1 s)."""
    from . import ops_mesh
    return min(DRAW_GAP_MAX_S, DRAW_S_PER_POINT * ops_mesh.largest_mesh_points())


def _late_draw_note():
    """What a shot says when a mesh is past what the hold waits for."""
    from . import ops_mesh
    try:
        points = ops_mesh.largest_mesh_points()
    except Exception:
        return ""
    if DRAW_S_PER_POINT * points <= DRAW_GAP_MAX_S:
        return ""
    return ("A mesh here has {:,} points: Modo can take longer to draw an "
            "edit of it than the {:g} s this call waits. If the picture shows "
            "the scene before your last edit, call again."
            .format(points, DRAW_GAP_MAX_S))


def _size(payload):
    """The longest side asked for, in pixels, or None for the full picture."""
    size = payload.get("size")
    if size is None:
        return None
    if (isinstance(size, bool) or not isinstance(size, (int, float))
            or not math.isfinite(size) or size != int(size) or size < MIN_SIZE):
        raise _bad("size must be a whole number of pixels, %d or more, not %r"
                   % (MIN_SIZE, size),
                   hint="Pass size as the picture's longest side, e.g. 800; "
                        "leave it out for the viewport's full size.")
    return int(size)


def _scaled(path, size):
    """A copy of the shot at path, its longest side size pixels, through
    Modo's image service (as the backdrops do): no Pillow. Returns (its path,
    its size, the viewport's size, a note). Over the viewport's size it is
    the shot itself; a scaling that fails is the shot itself, and says so."""
    import lx
    service = lx.service.Image()
    image = lx.object.Image(service.LoadNoCache(path))
    full = [int(v) for v in image.Size()]
    if size is None or size >= max(full):
        return path, full, full, ""
    scale = float(size) / max(full)
    shape = [max(1, int(round(v * scale))) for v in full]
    out = path[:-len(".png")] + "_%d.png" % size
    try:
        copy = lx.object.Image(service.Create(shape[0], shape[1], image.Format(), 0))
        service.Resample(copy, image, lx.symbol.iPROCESS_ACCURATE)
        service.Save(copy, out, "PNG", None)
        if not os.path.isfile(out) or not os.path.getsize(out):
            raise RuntimeError("nothing was written")
    except errors.OpError:
        raise
    except Exception as exc:
        if os.path.isfile(out):
            os.remove(out)
        return path, full, full, ("The picture couldn't be scaled to %d px "
                                  "(%s), so this is the full picture." % (size, exc))
    return out, shape, full, ""


@op("screenshot", read=True, drawn=True)
def screenshot(payload, kit):
    """The viewport as drawn. The pump runs this job only once Modo has had a
    moment with no job to draw in (a probe: an edit's frame shows
    then, and no call inside the job draws it)."""
    import lx
    size = _size(payload)
    os.makedirs(folder(), exist_ok=True)
    # Forward slashes, as for scene.saveAs.
    path = os.path.join(folder(), "shot_%s.png" % uuid.uuid4().hex).replace("\\", "/")
    failure = ""
    try:
        lx.command("gl.snapshot", filename=path)
    except errors.OpError:
        raise
    except Exception as exc:
        failure = ": %s" % exc
    if not os.path.isfile(path) or not os.path.getsize(path):
        # run_job attaches the event log lines; Modo says why only there.
        raise errors.OpError(
            protocol.NO_VIEWPORT, "gl.snapshot wrote no image%s" % failure,
            hint="Modo needs a 3D viewport in the current layout. Switch to "
                 "a layout with one (Model, say) and call again.")
    try:
        shown, shape, full, note = _scaled(path, size)
    except errors.OpError:
        raise
    except Exception as exc:
        # Modo can't read its own shot: the server reads the size from it.
        shown, shape, full, note = path, None, None, (
            "The picture couldn't be read to scale it (%s), so this is the "
            "full picture." % exc if size else "")
    result = {"path": shown, "size": shape, "viewport": full}
    if shown != path:
        result["original"] = path
    note = " ".join(text for text in (note, _late_draw_note()) if text)
    if note:
        result["note"] = note
    return result


# -- backdrop ---------------------------------------------------------
#
# What the probe found on 17.1 (2026-11-10): clip.addStill makes a videoStill
# item "<stem>:videoStill001"; item.create backdrop makes the item with its
# transform items and selects it. Neither backdrop.add image:<clip> nor
# item.link (named, positional or on the selection) links anything; the
# backdropClip graph's AddLink(backdrop, clip) does, inside the job's undo
# step. pixelSize is metres per pixel on both axes; aspect is a boolean ("Keep
# Aspect"), so an image can't be drawn at another aspect without a stretched
# copy. The image is centred on the item's position. Modo's image service
# reads the size of any image Modo loads, and Resample and Save make the copy:
# no Pillow.

# The view's word (the projection channel's), and the world axes the image's
# width and height run along.
VIEWS = {"left": (2, 1), "right": (2, 1), "front": (0, 1), "back": (0, 1),
         "top": (0, 2), "bottom": (0, 2)}

GRAPH = "backdropClip"
COPIES = "backdrops"
MAX_PIXELS = 16384


def copies():
    """The stretched copies: a subfolder, apart from the screenshots."""
    return os.path.join(folder(), COPIES)


def _bad(message, hint=""):
    return errors.OpError(protocol.BAD_REQUEST, message, hint=hint)


def _metres(payload, key, required):
    value = payload.get(key)
    if value is None and not required:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not value > 0 or math.isinf(value)):
        raise _bad("%s must be a number of metres above 0, not %r" % (key, value),
                   hint="Pass width_m (and height_m, if the image must fit a "
                        "height too) in metres, e.g. width_m=0.142.")
    return float(value)


def _request(payload):
    """The payload, checked whole before anything is made."""
    path = str(payload.get("path") or "").strip().replace("\\", "/")
    if not path:
        raise _bad("no path given", hint="Pass path: the image file, e.g. "
                   "C:/refs/side.png.")
    if not os.path.isfile(path):
        raise _bad("no file at %s" % path,
                   hint="Check the path; Modo reads it on this PC.")
    view = str(payload.get("view") or "").strip().lower()
    if view not in VIEWS:
        raise _bad("no view %r" % payload.get("view"),
                   hint="Pass view as one of %s." % ", ".join(VIEWS))
    width = _metres(payload, "width_m", True)
    height = _metres(payload, "height_m", False)
    center = payload.get("center", [0, 0, 0])
    if (not isinstance(center, (list, tuple)) or len(center) != 3 or any(
            isinstance(c, bool) or not isinstance(c, (int, float))
            or not math.isfinite(c) for c in center)):
        raise _bad("center must be [x, y, z] in metres, not %r" % (center,))
    opacity = payload.get("opacity", OPACITY)
    if (isinstance(opacity, bool) or not isinstance(opacity, (int, float))
            or not 0 <= opacity <= 1):
        raise _bad("opacity must be from 0 to 1, not %r" % (opacity,),
                   hint="1 is the image as it is, 0 can't be seen.")
    name = str(payload.get("name") or "").strip() or os.path.splitext(
        os.path.basename(path))[0]
    return path, view, width, height, [float(c) for c in center], float(opacity), name


OPACITY = 0.65


def _load(path):
    """Modo's own image of path, and its size, or bad_request."""
    import lx
    service = lx.service.Image()
    try:
        image = lx.object.Image(service.LoadNoCache(path))
        width, height = image.Size()
    except errors.OpError:
        raise
    except Exception as exc:
        raise _bad("Modo can't read %s as an image: %s" % (path, exc),
                   hint="Use a PNG, JPEG, TGA or another format Modo opens.")
    return service, image, int(width), int(height)


def _stretch(service, image, path, width, height):
    """A copy of image at width x height px, written as a PNG to copies():
    named after the image, its whole path and the size, so one image at one
    size is one file, and side.png and side.jpg don't share one. Returns its path."""
    import hashlib
    import lx
    stem = os.path.splitext(os.path.basename(path))[0]
    tag = hashlib.sha1(path.lower().encode("utf-8")).hexdigest()[:6]
    out = os.path.join(copies(), "%s-%s-%dx%d.png" % (stem, tag, width, height))
    out = out.replace("\\", "/")
    try:
        os.makedirs(copies(), exist_ok=True)
        copy = lx.object.Image(service.Create(width, height, image.Format(), 0))
        service.Resample(copy, image, lx.symbol.iPROCESS_ACCURATE)
        service.Save(copy, out, "PNG", None)
    except errors.OpError:
        raise
    except Exception as exc:
        raise _bad("the stretched copy %s couldn't be written: %s" % (out, exc),
                   hint="Check the temp folder is writable, or stretch the "
                        "image yourself and call with height_m left out.")
    if not os.path.isfile(out) or not os.path.getsize(out):
        raise _bad("the stretched copy %s wasn't written" % out,
                   hint="Check the temp folder is writable.")
    return out


def _new(before, kind):
    import modo
    found = [i for i in modo.Scene().items(kind) if i.id not in before]
    if len(found) != 1:
        raise errors.OpError(protocol.COMMAND_FAILED,
                             "expected one new %s item, found %d" % (kind, len(found)))
    return found[0]


def _same(path):
    return str(path or "").replace("\\", "/").lower()


def _clip(before, path):
    """The still clip showing path: the one clip.addStill just made, or the
    one the scene had. On 17.1 clip.addStill makes no second clip for a file
    a clip already holds; the backdrops share it. Returns
    (clip, whether it is new)."""
    import modo
    clips = modo.Scene().items("videoStill")
    new = [c for c in clips if c.id not in before]
    if len(new) == 1:
        return new[0], True
    if not new:
        had = [c for c in clips if _same(c.channel("filename").get()) == _same(path)]
        if had:
            return had[-1], False
    raise errors.OpError(protocol.COMMAND_FAILED,
                         "expected one new videoStill item, found %d" % len(new))


CAMERA_NOTE = (
    "It still shows in the Camera view: Modo 17.1 has no backdrop channel "
    "for that view. For a camera shot, hide it with mt_set_channels item=%s "
    "{\"visible\": \"off\"} (every view) and set \"default\" back after, or "
    "turn off view3d.showBackdrop (every backdrop, in that viewport).")


@op("backdrop")
def backdrop(payload, kit):
    """A reference image behind the model in one job: a still clip, a
    Backdrop item linked to it, the view's projection, sized in metres and
    placed at center, hidden in the perspective view. One undo step; a
    failure is rolled back whole."""
    import lx
    import modo
    from . import ops_mesh
    path, view, width_m, height_m, center, opacity, name = _request(payload)
    service, image, w0, h0 = _load(path)
    width, height = w0, h0
    shown = path
    if height_m is not None:
        height = int(round(w0 * height_m / width_m))
        if not 1 <= height <= MAX_PIXELS:
            raise _bad("height_m %g at width_m %g would make the image %d px "
                       "tall" % (height_m, width_m, height),
                       hint="Check the two sizes are in metres.")
    stretched = None
    if height != h0:
        stretched = shown = _stretch(service, image, path, width, height)
    pixel = width_m / width

    scene = modo.Scene()
    selected = [i.id for i in scene.selected]
    mode = ops_mesh.current_mode()
    before = set(i.id for i in scene.items())
    lx.command("clip.addStill", filename=shown)
    clip, clip_new = _clip(before, shown)
    lx.command("item.create", type="backdrop", name=name)
    item = _new(before, "backdrop")
    # The channels are set on the selection item.create leaves, the new
    # backdrop alone, so no line names its id: the journal's replay runs
    # them in a new scene, where Modo may give it another.
    if [i.id for i in scene.selected] != [item.id]:
        lx.command("select.item", item=item.id, mode="set")
    # The link no command makes (the probe): undone with the job's step.
    graph = lx.object.ItemGraph(lx.object.Scene(item.Context()).GraphLookup(GRAPH))
    graph.AddLink(item, clip)
    for channel, value in (("projection", view), ("pixelSize", pixel),
                           ("transparency", round(1.0 - opacity, 6)),
                           ("hidePersp", 1)):
        lx.command("item.channel", name=channel, value=value)
    for axis, value in zip("XYZ", center):
        lx.command("transform.channel", name="pos." + axis, value=value)
    lx.command("select.drop", type="item")
    for ident in selected:
        lx.command("select.item", item=ident, mode="add")
    if mode:
        # item.create and select.drop item leave Modo in item mode: back to the mode it was in, its elements still selected.
        lx.command("select.typeFrom", typelist=mode)

    size = [width_m, height * pixel]
    low, high = list(center), list(center)
    for axis, extent in zip(VIEWS[view], size):
        low[axis] -= extent / 2.0
        high[axis] += extent / 2.0
    result = {
        "item": item.id, "name": item.name, "clip": clip.id, "view": view,
        "size_m": [round(v, 6) for v in size],
        "rect": {"min": [round(v, 6) for v in low],
                 "max": [round(v, 6) for v in high]},
        "pixels": [width, height], "image": [w0, h0],
        "pixel_size": pixel, "opacity": opacity,
        "hidden_in_perspective": True, "hidden_in_camera": False,
        "stretched": stretched,
        "file": shown, "link": [item.id, clip.id],
    }
    # HidePersp is the Perspective view's alone; no
    # backdrop channel keeps it out of the Camera view on 17.1.
    notes = [CAMERA_NOTE % item.id]
    if stretched:
        notes.append(
            "The image is %d x %d px, so a copy stretched to %d x %d px is "
            "shown: %s. It lives in the temp folder; copy it beside the scene "
            "before saving a scene you'll keep." % (w0, h0, width, height, stretched))
    if not clip_new:
        notes.append(
            "The scene already had a clip for this file (%s); this backdrop "
            "shares it, and undoing this call leaves the clip." % clip.id)
    result["note"] = " ".join(notes)
    return result
