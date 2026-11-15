# What Modo 17.1 actually does

Facts and hazards found in Modo 17.1v1 (Python 3.10) while building
modo-tools, each checked in Modo. mt_search_docs searches this file (kind
"facts").

## The lx module: every method's __doc__ is its signature

`lx` is compiled, with no source on disk, but every `lx.object` and
`lx.service` method carries its signature as a docstring:
`lx.object.Image.GetPixel.__doc__` is `GetPixel(integer x,integer y,integer
type,data[] pixel {})`, and `(integer w,integer h) = Size()` shows what a
method returns. A `data[]` argument is a buffer you pass
(`lx.object.storage`). The classes themselves have no docstring. 354 object
classes, 51 services, 4,240 methods, 5,144 `lx.symbol` names on 17.1v1;
mt_search_docs holds them all (kind "api"), so search before `dir()`.

## scene.open from a script froze Modo

On 2026-11-08 `scene.open` sent from a script hung Modo 17.1 (not responding,
no crash dump; force-quit). With a bad path it opens a dialog asking for the
file, which `!!` doesn't stop. The kit refuses it as risky. Ask the user to
open scenes from File > Open.

## A command name that isn't one fails as "command could not be found"

A misspelt command fails with "command could not be found", from
mt_run_command and from lx.eval in mt_run_python alike. Check names with
mt_find_commands and arguments with mt_command_info first.

## Reading tool.attr's hints froze Modo; scene.tagAdd's popup crashes it

Never read `Attributes.Hints(i)` on a variable argument (ArgFlags & 8) of a
bare spawned command: tool.attr's value opened a dialog and froze Modo for
good. Never read the popup (`UIValueHints(...).PopCount()`) of
`scene.tagAdd` (crashes Modo) or `octane.selectOcioColorspaceWhenSaving`. No
try catches either. The kit's mt_command_info skips both.

## Argument words: a stray word is silently another value

A word an argument doesn't know is taken silently, usually as its first
value: `view3d.projection frt` and `view3d.projection front` both set Top
(the word for the Front view is `fnt`; found 2026-11-11). Words match
without case; prefixes match too. Words with no name fill arguments only up
to the first named word; a word with no name after a named one is dropped
silently. mt_command_info lists each argument's real words.

## Angles: queries answer radians, commands take degrees

A query answers an angle in radians, but a bare number in a command line is
degrees (`transform.channel rot.Y 30` is 30 degrees; a "rad" suffix is
ignored). An operator works on the raw value (`rot.Y ?+30` adds 30 radians).
Distances and percents are raw both ways (`scl.X 1.5` is 150%).

## item.channel pos.X doesn't parse; use transform.channel

`item.channel pos.X` does not parse for a locator or mesh on 17.1. Use
`transform.channel pos.X <value>` on the selection, or with `item:<id>`.

## poly.bevel on a whole closed mesh needs group false

`poly.bevel` on a whole closed mesh changes nothing unless
`tool.attr poly.bevel group false` is set first. A polygon count read inside
the same job after a bevel is stale.

## modo.MeshGeometry can read stale or zero positions

`modo.Mesh().geometry` keeps the MeshGeometry it made on its first read
(modo/item.py). After `poly.subdivide` or an undo it has read zeros or old
positions, and a primitive made in the same call can't be read back through
it. Read and write points with mt_points and mt_set_points, or with a layer
scan (`lx.service.Layer().ScanAllocateItem`, MeshEdit, PointAccessor).

## Layer scan writes: mark POSITION and POINTS

Writing points through a layer scan: `ScanAllocateItem(item,
lx.symbol.f_LAYERSCAN_EDIT_VERTS)` (273) scans that one mesh whether or not
it is selected; MeshEdit(0), PointAccessor `SelectByIndex` and `SetPos`, then
`SetMeshChange(0, f_MESHEDIT_POSITION | f_MESHEDIT_POINTS)` and `Apply()`.
Marked POSITION alone, a later surface sample and two undos left the
edit-action mesh read showing only part of the points put back.

## Reading pixels: GetPixel fills a buffer you pass

`lx.service.Image().LoadNoCache(path)` loads an image. `Image.GetPixel(x, y,
lx.symbol.iIMV_RGBA, buf)` with `buf = lx.object.storage('b', 4)` fills the
buffer; it doesn't return the pixel (lxifc.py: `img_GetPixel(self, x, y,
type, pixel)`). `Image.GetLine(y, iIMV_RGBA, buf)` leaves buf alone and
returns an untyped storage: `setType('b'); setSize(4 * w); get()`. No need to
decode PNGs by hand.

## Selection: read packets, never TestMarks; select by geometry

Never read the selection from mesh `select` marks (TestMarks): they said a
whole cube was selected while one polygon was. Read the selection service's
packets. Scripted `select.element mesh polygon set 5` silently selects
nothing (it wants a layer index), and a following `delete` empties the whole
mesh; select with `geometry.polygons[i].select()`.

## Undo: one call is one step; app.undo is refused in a script

Each kit job is one undo step. Modo refuses `app.undo` from a script or
command ("cannot be used inside a script"). A command block
(BlockBegin/BlockEnd) loses channel set() to undo, so jobs are never wrapped
in one. Use mt_undo_last, or mt_begin and mt_rollback.

## Holding Ctrl+Z past the step that made a scene crashed Modo

Modo 17.1 crashed about a second after a held Ctrl+Z undid the step of an
`mt_run_command "scene.new"` (2026-11-08). Not reproduced,
and not known to be the kit's. A single `app.undo` of that step was safe
later. Don't hold Ctrl+Z past the steps of the scene you are in.

## clip.addStill makes no second clip for a file the scene has

`clip.addStill` for a path a clip already holds (same spelling) makes no new
item and raises nothing; it adds the existing clip to the selection.

## No command links a backdrop to its image

On 17.1 `backdrop.add image:` and `item.link backdropClip` silently link
nothing. `lx.object.ItemGraph(scene.GraphLookup('backdropClip')).AddLink(
backdrop, clip)` does, and is undone with the job's step. mt_backdrop does
this for you.

## No backdrop channel hides it in the Camera view

A Backdrop's `hidePersp` hides it in the Perspective view only. `visible`
off hides it in every view; `view3d.showBackdrop false` hides all backdrops
in that viewport.

## SubD levels: SubD cuts each side, Pixar SubD doubles

Modo's SubD (subpatch) level counts cuts per side: 2 * n * L^2 triangles per
n-gon. Pixar SubD doubles each level: 2 * n * 4^(L-1). The render level is
`mesh.renderSubdiv` for SubD and `mesh.psubRenderSubdiv` for Pixar SubD.
Modo's SubD is not plain Catmull-Clark; mt_describe_mesh surface=true reads
Modo's own surface.

## The glTF writer exports the cage: freeze first

Modo's glTF writer exports a SubD mesh's cage, not its smoothed surface.
Freeze the mesh before exporting (inside an action you roll back after).

## mt_screenshot can show the view a call late

After view3d commands or mesh edits, mt_screenshot has shown the view one to
three calls late. Run another call (e.g. `view3d.projection` with the same
view), then shoot again. Changed 2026-11-14: the kit now holds a
screenshot until Modo has drawn, so one call is enough.

## Modo draws the viewport only when no job runs for a moment

On 17.1 `gl.snapshot` grabs the last frame Modo drew. Modo draws only when
the kit's idle callback has run no job for a moment: a shot straight after
an edit was the old frame 10 of 10 times, after a 0.05 s gap the new one 10 of
10. Nothing inside the job draws it: `debug.redraw`, a first `gl.snapshot`,
`view3d.projection` with the same view, and a second call sent at once all
gave the old frame. The kit holds a screenshot job until Modo has been idle
0.1 s since the last job ended (longer after an edit when a mesh is large:
see the next fact). `size` scales the shot down after the capture, through
Modo's image service.

## Modo draws a large mesh late after an edit

On 17.1 the viewport shows an edit of a large mesh some time after the job,
in step with the mesh's point count: 25,000 points at once, 101,402 SubD
points 0.25 s after the job, 400,000 SubD points 1.1 to 1.2 s, 400,000 plain
faces 0.75 to 0.85 s (a test PC, 2026-11-14). An undo of such an edit is as
late. A view change, or an edit of a small mesh in the same scene, is drawn
at once. So after a call that can change the scene, `mt_screenshot` waits 5
microseconds a point of the scene's largest mesh (0.5 s at 100,000 points,
2 s at 400,000; 0.1 s at least, 5 s at most); past a million points the
answer says the picture can be old, and to call again. A `gl.snapshot` run
another way sooner than that after an edit is the old frame.

## A busy Modo runs no idle callback and draws nothing

While Modo 17.1 is busy with its own work (0.6 and 1.35 s were seen after a
delete of 400,000 points), `DoWhenUserIsIdle` callbacks do not fire, the
viewport is not drawn, and Python threads other than the main thread do not
run, so the kit's answer to the last call waits too. Time on the clock is
then not time in which Modo could draw: count time only while the idle
callback fires (it fires about 100,000 times a second while Modo is idle).

## QApplication.processEvents() inside a job crashed Modo

`PySide6.QtWidgets.QApplication.processEvents()` called inside a kit job (an
mt_run_python or op, inside a Modo command) crashed Modo 17.1 at once, with a
crash dump. Never pump Qt's events from a job.

## Polygons and SubD edge weights in one layer scan crashed Modo

In a test (2026-11-11) two `mt_run_python` jobs crashed Modo 17.1
with no answer: one made a mesh item (`item.create mesh`) and filled it through
`lx.service.Layer().ScanAllocateItem(item, f_LAYERSCAN_EDIT)` in the same job;
the other filled an existing item and, in the same scan, set SubD edge weights
(`MeshMapAccessor` `i_VMAP_SUBDIV` "Subdivision", `EdgeAccessor.SelectEndpoints`,
`SetMapValue`). Make the item, add the points and polygons, and set the weights
each in its own call: 15 times, no crash. Which pair is the cause is not
isolated yet.

## SubD edge weights: write them through the "Subdivision" edge map

An edge weight (crease) is a value in the mesh's `i_VMAP_SUBDIV` map named
"Subdivision": `MeshMapAccessor.SelectByName` (or `New`) gives its id, then
`EdgeAccessor.SelectEndpoints(p1, p2)` and `SetMapValue(id, storage)` with a
one-float storage; `SetMeshChange(0, f_MESHEDIT_MAP_OTHER |
f_MESHEDIT_MAP_CONTINUITY)`. A weight of 1.0 is fully sharp: a 0.02 m SubD
cube with its four top edges at 1.0 has its surface's top at 0.02, the
others at 0.0176. Do it in its own job (see the crash above).

## poly.bevel on a selected patch of a closed SubD mesh works

`poly.bevel` with `group true` on 10 selected wall polygons of a closed SubD
mesh, applied twice (inset 1.2 mm, then inset 2.5 mm with shift -4.5 mm), made
a recessed window: 28 new points and polygons appended after the old ones,
whose indices stay. Select the polygons with `modo.Mesh(item).geometry.polygons[i].select()`
after `select.typeFrom polygon` and `select.drop polygon`.

## prim.cube: radius before segmentsR; sharp adds holding loops

`prim.cube` refuses `tool.attr prim.cube segmentsR` and `tool.attr prim.cube
sharp` while `radius` is 0 ("Corner radius must be non-zero."), so set
`radius` first, and leave both alone for a box with no chamfer. With a radius
and `segmentsR 1`, `sharp` on (the default) makes a box of 114 polygons (90
quads, 24 triangles): it adds holding loops for SubD. `sharp false` makes 26
(18 quads, 8 corner triangles), the plain chamfered box a polygon model wants.
`cenX/Y/Z` and `sizeX/Y/Z` are the centre and the full size, in metres; the
box comes out to the micrometre (a test, 2026-11-14: 60
boxes, every one at its drawn size). Set the attributes after `tool.reset
prim.cube` and before `tool.doApply`; the box goes into the selected mesh.

## prim.cylinder: size is the radius and half the length

`prim.cylinder`'s `sizeX`, `sizeY` and `sizeZ` are half extents, unlike
`prim.cube`'s: with `axis 2` (Z), `sizeX` and `sizeY` are the radius and
`sizeZ` is half the length. A cylinder of diameter 0.81 in and length 0.23 in
along Z took `sizeX 0.010287`, `sizeY 0.010287`, `sizeZ 0.002921` and came out
0.020574 x 0.020574 x 0.005842 m. `polType 0` makes plain
faces; `sides` sets the count round it.

## viewport.fit frames the camera and lights too

`viewport.fit` fits every item, cameras and lights included, so with the
camera 13 m away the model was a small patch in the middle of the Front view.
Select the meshes (`select.itemType mesh`), run
`viewport.fitSelected`, then `select.drop item`: that frames the model. Both
are undo steps and journal entries for now, and both wait for
Modo's scene evaluation, so send each alone.
