"""The MCP server: tools named mt_*, served over stdio.

Claude Code starts it as `python -m modo_tools`. It talks to the kit inside
Modo on 127.0.0.1:15680 (MODO_TOOLS_PORT to change it).

`python -m modo_tools call <op> [json payload] [timeout_ms]` sends one op to
the kit and prints the answer, for checks by hand.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from typing import Any

from . import __version__, budget, docs, journal, protocol, risky, watch
from .bridge import Bridge, BridgeError, format_error, format_late

HOST = os.environ.get("MODO_TOOLS_HOST", protocol.DEFAULT_HOST)
PORT = int(os.environ.get("MODO_TOOLS_PORT", str(protocol.DEFAULT_PORT)))

INSTRUCTIONS = (
    "Works in Foundry Modo through the modo-tools kit running inside it. "
    "Call mt_status first: it says whether Modo is reachable, and which "
    "Modo and Python versions you are working with. Modo's API is thin in "
    "training data: before using a command for the first time, check its "
    "real arguments with mt_command_info, and find names with "
    "mt_find_commands. Before guessing at Modo's Python API (dir(), a call "
    "in a try), search Modo's own docs with mt_search_docs. Bevel, extrude, primitives and transforms are tools, "
    "not commands: tool.set <tool> on, tool.attr, tool.doApply. Every "
    "command runs with !!, so a failure comes back as an error instead of a "
    "dialog in Modo; a dialog that opens anyway during a call (a command "
    "asking for an argument it couldn't parse) is cancelled, and the answer "
    "says so. Never run scene.open: ask the user to open scenes. Before an "
    "edit you may need to take back, open an action with mt_begin "
    "(mt_rollback undoes it all); mt_undo_last undoes the newest call. A "
    "long call "
    "says at the end whether Modo was busy or stuck behind a dialog while it "
    "waited; after a crash or a freeze, "
    "mt_op_log shows the last jobs, even with Modo closed."
)

RUN_COMMAND_DESCRIPTION = (
    "Run one Modo command line, e.g. 'item.create mesh' or a query "
    "'item.channel pos.X ?'. Returns its result. A failure comes back as "
    "command_failed with Modo's message, its event log lines and a hint; a "
    "name that isn't a command (or is a tool's) as unknown_command: nothing "
    "ran. "
    "purpose (optional) is one line shown in Modo's event log. Commands known "
    "to crash Modo are refused until acknowledge_risk=true, after saving. "
    "One undo step. Before an edit you may need to take back, open an "
    "action with mt_begin: mt_rollback then undoes it all. mt_undo_last "
    "undoes the newest call; app.undo can't run inside a call. A word that "
    "isn't one of its argument's values (mt_command_info lists them) is "
    "taken by Modo as some other value without an error, so the answer then "
    "has a note naming the value Modo set. Modo also drops a word with no "
    "name after a named one (in 'select.symmetry 0 uvActive:0 u' the u is "
    "never read): the note says so; name every argument after the first "
    "named one."
)

RUN_PYTHON_DESCRIPTION = (
    "Run Python inside Modo (Python 3.10 on 17.1) with lx, lxu and modo "
    "imported. Set RESULT to return a value; print() output comes back as "
    "stdout. purpose is required: one line on what the code does, shown in "
    "Modo's event log. Every lx.eval runs with !!, so a failing command "
    "raises instead of opening a dialog. Don't call sys.exit(). Code that "
    "runs past timeout_ms can't be stopped: the answer says it is still "
    "running and the next call reports how it ended. Geometry edits work: "
    "the code runs inside a Modo command, and the whole call is one undo "
    "step (undo: {\"step\": 1}), even when it changed nothing. A call that "
    "fails is undone before it answers (undo: {\"rolled_back\": true}), so "
    "a half edit never stays; if it couldn't be, the error says what to do. "
    "For code that "
    "only reads, pass read_only=true: it adds no undo step (undo: null), "
    "and any change it tries (a command that isn't a query, a channel set(), "
    "setMeshEdits) is refused as a python_error. Before an edit you may "
    "need to take back (a solve, a fit, anything you will judge after), "
    "open an action with mt_begin: mt_rollback undoes the whole action, "
    "mt_commit keeps it. A call that worked but was wrong is undone with "
    "mt_undo_last; app.undo is refused inside a call, so don't delete and "
    "rebuild by hand. To read or move vertices, use mt_points and "
    "mt_set_points: modo.MeshGeometry can read stale positions after a "
    "subdivide or an undo."
)

SET_CHANNELS_DESCRIPTION = (
    "Set channels on one item: item is an id (preferred: names aren't "
    "unique) or a name; channels is {channel: value}, e.g. {\"pos.X\": 0.5}. "
    "Values are raw, as Python reads them: metres, radians (not degrees), "
    "0-1 for percentages. pos.*, rot.* and scl.* on a mesh, locator, "
    "camera, light or backdrop go to its transform items. Every channel is checked before any is written. "
    "Returns old and new values. One undo step."
)

SAVE_SCENE_DESCRIPTION = (
    "Save the scene to path, or to its own file when path is left out. "
    "Overwrites what is at path. Checks the file was written: Modo 17.1 "
    "silently skips a save straight into the temp folder's root, so use a "
    "subfolder."
)

COMMAND_INFO_DESCRIPTION = (
    "Look up a command's real arguments in the running Modo: user name, "
    "description, and each argument's name, type and whether it is optional, "
    "with values: the words it takes, from Modo's own hints (with the "
    "integer each stands for, e.g. view3d.projection's front is fnt). "
    "For a tool such as poly.bevel, its attributes with types, defaults and "
    "values, and how to drive it."
)

FIND_COMMANDS_DESCRIPTION = (
    "Find commands whose name contains query (e.g. 'bevel'), with their user "
    "names, and matching tools. Up to limit results."
)

SEARCH_DOCS_DESCRIPTION = (
    "Search Modo's own docs on this PC before guessing at its API: the "
    "Python API source Modo installs (modo, lxu, lxserv, lxifc: one entry "
    "per function, class and method), the lx module's classes and methods "
    "with their signatures, and lx.symbol; command, tool and item/channel "
    "help; the example scripts Modo ships; and the facts this project found "
    "in Modo 17.1 (hazards included). Words or names, e.g. 'Image GetPixel' "
    "or 'layer scan point position'. kind narrows to one of api, help, "
    "example, facts. Each hit has an id for mt_read_doc, where it is (file "
    "and line), and the lines around the match. Modo needn't be running. "
    "Not every command has help on disk (item.create has none): "
    "mt_find_commands and mt_command_info read commands from Modo."
)

READ_DOC_DESCRIPTION = (
    "Read one entry from mt_search_docs whole, by its id: a function, "
    "method or class's source, a command's, tool's, item's or channel's "
    "help, an example script, or a fact. A long entry is cut to the size "
    "budget and says how to read on with offset (a line number)."
)

SCENE_SUMMARY_DESCRIPTION = (
    "What is open in Modo: the scene file, the frame range, the current "
    "frame and fps, how many items of each type, and the selected items. "
    "Cheap; call it before acting on a scene."
)

SCENE_GRAPH_DESCRIPTION = (
    "The item tree, each item as name, id and type with its children "
    "nested. Without types, what Modo's Items list shows (meshes, locators, "
    "cameras, lights); types=[\"mesh\"] for some types, [\"all\"] for every "
    "item including the shader tree. root (an id or name) starts below one "
    "item; depth=1 lists only the top level. An item whose children aren't "
    "listed has \"more\": how many. A big scene is cut and says so."
)

GET_ITEM_DESCRIPTION = (
    "One item by id (preferred: names aren't unique) or name: id, name, "
    "type, parent, and pos/rot/scl for a locator (metres, radians). "
    "channels: a list of channel names, or \"all\"; none unless asked, "
    "because one item carries a hundred. pos.X, rot.Y, scl.Z and the like "
    "read a locator's transform items, as mt_set_channels writes them."
)

GET_SELECTION_DESCRIPTION = (
    "What is selected. type=\"item\" (default): the selected items. "
    "\"vertex\", \"edge\" or \"polygon\": each selected mesh's selected "
    "elements by index (an edge as its two vertex indices)."
)

DESCRIBE_MESH_DESCRIPTION = (
    "Facts about meshes, to check an edit without a screenshot: each mesh's "
    "vertex, edge and polygon counts, its polygon types when any isn't a "
    "plain face (subd, psub, curve...), and its world-space bounding box "
    "(box: [min, max], null for an empty mesh). Shape facts: sides "
    "(polygons by side count: 1-2, 3, 4, 5+), poles (off-border vertices "
    "whose edge count isn't 4: 1-2, 3, 5+), boundary_loops and "
    "boundary_edges (holes; 0 on a closed mesh), parts (pieces sharing no "
    "vertex), area, and volume (signed: negative means the normals point "
    "in; 0 when a closed mesh encloses nothing, such as a two-sided card; "
    "null when the mesh is open). nonmanifold_edges, lone_vertices and "
    "zero_area appear only when not 0. Every number is in world space and "
    "metres. items: one id or name, or a list; left out, the "
    "selected meshes, or every mesh when none is selected. Reads the base "
    "mesh, before deformers. selection: what is selected in mode (vertex, "
    "edge or polygon; left out, Modo's current mode, and in item mode no "
    "selection): its count, and when not 0 the box and centroid of the "
    "vertices it touches. A mesh item that isn't selected keeps its elements' "
    "selection, and Modo's tools act on it only once the item is selected "
    "again. detail=\"selection\" adds elements, one per "
    "selected element in ascending order: a polygon's index, sides, normal, "
    "centroid and area; an edge's two vertex indices, length, polygons and "
    "angle (degrees between its two polygons' normals, 0 flat, null unless "
    "exactly two); a vertex's index, position, valence and border. More "
    "selected than fit in the answer is refused as too_large, never cut. "
    "The box, area and volume are the cage's: a SubD mesh's surface sits "
    "inside it. surface=true adds surface to each mesh: the surface Modo "
    "itself draws, read from Modo without a freeze, at the mesh's render "
    "level (Render Subdivision Level in its properties; Pixar SubD has its "
    "own, and a mesh with both kinds gets level null): level (null if it "
    "can't be told), triangles, box, area, volume; null for a mesh "
    "with no SubD or Pixar SubD polygon. Too many surface points is refused "
    "as too_large. "
    "Adds no undo entry. Many meshes are cut whole, in order, and say so."
)

SCREENSHOT_DESCRIPTION = (
    "A picture of Modo's 3D viewport as it is now: after an edit or a view "
    "change, the frame Modo drew after it (the call waits for Modo to draw: "
    "a moment, or up to 5 s after a change in a scene with a large mesh). "
    "size (optional, 64 or more) is the picture's longest side in "
    "pixels: a smaller picture costs fewer tokens. Modo 17.1 captures at the "
    "viewport's size, so a smaller one is scaled from it; the answer states "
    "both sizes. Use it to check a change looks right."
)

POINTS_DESCRIPTION = (
    "A mesh's vertex positions by index: points as [index, [x, y, z]], in "
    "the mesh's own space and metres (the item's transform is in "
    "mt_get_item; the answer says space: \"mesh\"), rounded to a "
    "micrometre. item is one mesh's id or name. start (default 0) and count "
    "(default all) pick a range; with polygons=true, also every polygon as "
    "its vertex indices. The base mesh, before deformers, read as Modo "
    "holds it now: use this, not modo.MeshGeometry, which can read stale "
    "positions after a subdivide or an undo. More points than fit in the "
    "answer: with no count, refused as too_large, which says how many fit; "
    "with a count, cut, and cut.next_start says where to go on. Polygons "
    "that don't fit are too_large. Adds no undo step."
)

SET_POINTS_DESCRIPTION = (
    "Move vertices of one mesh: item is its id or name; points is "
    "{\"12\": [x, y, z], ...} by vertex index (from mt_points), or a list "
    "of every position in index order (not the [index, [x, y, z]] pairs "
    "mt_points answers). In the mesh's own space and metres, "
    "as mt_points reads them. Every index and value is checked before any "
    "is written: an index out of range, a value that isn't three finite "
    "numbers or a list of the wrong length is refused, and Nothing was "
    "written. Then one write, through Modo's mesh API, not "
    "modo.MeshGeometry. One undo step: mt_undo_last takes it back; for a "
    "fit you will judge after, open an action with mt_begin first. The "
    "values go into the journal, so its replay is exact."
)

FIT_BACKDROPS_DESCRIPTION = (
    "Fit a SubD mesh to two backdrops' images: the surface Modo draws is "
    "moved to match the outlines of the shape seen in each. item: the mesh; "
    "backdrops: two backdrop ids or names in views at right angles (front or "
    "back with left, right, top or bottom; left or right with top or "
    "bottom), left out when the scene has just two backdrops (made with "
    "mt_backdrop, whose rect is where the image sits in metres). Each image's "
    "shape is what isn't background (clear, or near-white joined to the "
    "border), its largest piece; an image with no such background (a photo, "
    "a dark backdrop) is refused, and one over 512 px a side is read every "
    "nth pixel (the note says so). The two outlines make smooth slices along "
    "the axis the two views share; each cage point of the SubD polygons "
    "selected in polygon mode (all of them when none is selected, or in "
    "another mode: the note says which) is cast from inside the "
    "shape to its edge, keeping its height in the shape's middle, then a "
    "second pass casts each surface point along its normal (reaching an end "
    "above or below the cage); block out near the shape's height first (the "
    "answer's note says when the cage isn't). Parts are the caller's: one "
    "shape a call, so fit one part at a time by selecting its polygons. "
    "About 1.2 ms a point on Modo's main thread (700 points 1 s, 11,000 "
    "14 s). smooth (0 to 1, default 0.5) rounds "
    "the outlines and the targets; iterations (1 to 50, default 6) are the "
    "solve's rounds; floor: no point below this height (Y, metres). The "
    "cage is solved so Modo's own surface lands on the targets, measured "
    "after the write. Returns fitted (points), iou per view as [before, "
    "after] (the outline's overlap with the image's shape, 1 is exact), "
    "max_move_m and off_target_m (how far Modo's surface still is from the "
    "targets). Everything is checked before anything is written. One undo "
    "step: mt_undo_last takes it back; open an action with mt_begin first "
    "to try settings and roll back. A fit that doesn't settle fails and "
    "nothing stays. The points written go into the journal, so its replay "
    "sets them as values."
)

BACKDROP_DESCRIPTION = (
    "Put a reference image behind the model: a Backdrop item showing the "
    "image file at path in one view (left, right, front, back, top, bottom), "
    "width_m metres wide, centred at center ([x, y, z], metres), hidden in "
    "the perspective view. It still shows in the Camera view (Modo has no "
    "backdrop channel for that): for a camera shot set its visible channel "
    "to \"off\" with mt_set_channels, and \"default\" after. opacity: 1 is the image as it is (default 0.65). "
    "height_m: the height it must fit too; when that isn't the image's own "
    "aspect, a copy stretched to it is written to the temp folder and shown "
    "instead (Modo can't stretch a backdrop), and the note says so: copy it "
    "beside the scene before saving a scene you'll keep. Left and right "
    "views lay the width along Z, front and back along X, top and bottom "
    "along X with the height along Z. Returns the item, its clip, size_m and "
    "rect (min and max corners in metres). name: the item's name (the "
    "image's by default). The selection and its mode are left as they were. "
    "Any image Modo opens. An image the scene already shows shares its "
    "clip, and the note says so. One undo step, like any call: mt_undo_last "
    "takes it back, item and clip together (a shared clip stays)."
)

EVENT_LOG_DESCRIPTION = "The last count lines of Modo's event log, oldest first."

STATUS_DESCRIPTION = (
    "Is Modo reachable, and what is it running. Returns the kit, protocol, "
    "Modo and Python versions, the port, whether the pump is 'idle' (jobs run "
    "on their own) or 'manual' (they wait for modoTools.pump), the jobs "
    "queued, how the last Modo session ended, and undo: how a call is made "
    "one undo step (\"exec command\"), and rollback: how a failed call is "
    "undone (\"idle\": by the kit; \"ask\": the user presses Ctrl+Z once; "
    "\"off\": after an unsure rollback, until the kit restarts). When Modo "
    "is closed or the "
    "kit is stopped it says so and how to start it."
)

OP_LOG_DESCRIPTION = (
    "The last count lines of the op log, ~/.modo_tools/ops.log, oldest "
    "first: one JSON object per job the kit ran (op_start, then op_ok or "
    "op_error, then rollback and rollback_done when a failed job was undone) "
    "and per session. Read from the file, so it works while Modo is "
    "closed or frozen: after a crash, it shows which job was running."
)

JOURNAL_DESCRIPTION = (
    "The journal of what the agent changed in a scene: each changing call "
    "(mt_run_command, mt_run_python, mt_set_channels, mt_save_scene, "
    "mt_set_points, mt_backdrop, mt_fit_backdrops) is one "
    "numbered entry, written to ~/.modo_tools/journals/ before its answer, "
    "which says \"journal\": n. Lists the entries with their states: in the "
    "scene, rolled back, undone by the user (their Ctrl+Z; Ctrl+Shift+Z puts it "
    "back), undone by the agent (mt_undo_last), action open, action open at the crash (an action never "
    "committed), rollback unsure, or failed; an action's entries say which "
    "action, and the list ends with each action's outcome. untraced names writes the "
    "entry's commands don't hold (a channel set() or a selection through the "
    "modo module, SDK calls). name: empty for the current scene's journal (the "
    "newest with Modo closed), else one of the names the answer lists. Read "
    "from the files, so it works with Modo closed, after a crash. Builds the "
    "replay beside the journal, <name>.py: plain Python that rebuilds the "
    "scene with @ from the scene the journal began in, leaves out rolled-back "
    "and undone entries, actions not committed, and lines that save, open or "
    "export, and checks counts and "
    "bounding boxes where the journal took facts. With Modo up it first takes "
    "facts of the current scene, so the replay checks its end. exact: yes, "
    "or the entries the replay can't promise the same counts and boxes for "
    "(a query operator such as ?max that couldn't be read back, code run "
    "again, a selection by index), and the gaps: the user's edits between two "
    "calls, which the replay prints and can't redo. Run it with "
    "mt_run_command \"@<path>\" in a new scene: a failed check or line stops it "
    "and Modo undoes what it ran; the event log says why."
)

BEGIN_DESCRIPTION = (
    "Open an action: the calls that follow, until mt_commit or mt_rollback, "
    "are one action that can be rolled back together. label is one line on "
    "what it does, shown in Modo's event log. Each changing call inside it "
    "is still its own undo step, numbered: undo: {\"action\", \"step\"}; "
    "undone_by_user lists steps the user undid in Modo since the last call. "
    "Reads run as usual. While it is open, app.undo, app.redo, scene.new, "
    "scene.open, scene.close, scene.revert, cmds.undoTo, app.clearUndos, "
    "app.undoSuspend and app.undoLockout are refused (in_action), and "
    "so are changes from another agent. A call refused before it changed "
    "anything (bad_request, unknown_command, in_action, bad_channel, "
    "no_such_item, ambiguous_item, not_a_mesh) leaves the action open with its steps: "
    "undo: {\"action\", \"refused\": true, \"steps\"}, with \"kept\": true "
    "when its own step couldn't be undone and stays in the action. A call that fails "
    "after changing the scene rolls back the whole action and closes it. An action left idle for 5 minutes is "
    "committed as it stands, and the next call says so. Actions don't nest. "
    "Adds no undo step."
)

COMMIT_DESCRIPTION = (
    "Close the open action, keeping its changes. Returns steps (the calls "
    "that changed the scene) and presses: how many Ctrl+Z in Modo undo the "
    "whole action, one per step still there. Tell the user that number. Adds no "
    "undo step."
)

ROLLBACK_DESCRIPTION = (
    "Undo the open action back to where mt_begin was, and close it. Returns "
    "rolled_back (steps undone) and left (steps it couldn't reach). If the user "
    "edited in Modo during the action, their edit stays and the rollback stops "
    "beneath it: note says what the user presses to finish. With the manual pump "
    "nothing is undone, and left is the presses the user needs. Check the scene "
    "with mt_describe_mesh afterwards."
)

UNDO_LAST_DESCRIPTION = (
    "Undo this session's newest call that is still in the scene: its one undo "
    "step, as one Ctrl+Z in Modo would. Returns which call it was (journal "
    "n, op, purpose); its journal entry is marked undone by the agent, and "
    "the user's Ctrl+Shift+Z in Modo puts it back. One call per mt_undo_last: "
    "two calls back is two. Reads leave no step, so they are stepped over. "
    "Refused, with nothing undone, when the newest step is the user's (their edit "
    "or selection in Modo: ask them) or another agent session's call (also "
    "a call made before this server restarted), inside an open action "
    "(mt_rollback undoes the action), when no agent call is left to undo, "
    "and with the manual pump. Check the scene with mt_describe_mesh afterwards."
)

_bridge = Bridge(HOST, PORT)


def python_allowed() -> bool:
    """MODO_TOOLS_ALLOW_PYTHON=0 drops mt_run_python. Modo commands can still
    run scripts, so this narrows what an agent can do; it isn't a sandbox."""
    return os.environ.get("MODO_TOOLS_ALLOW_PYTHON", "1").strip() != "0"


def _fail(err_type: str, message: str, hint: str = "") -> str:
    return BridgeError(err_type, message, hint).text()


def rollback_line(undo: Any) -> str:
    """What a failed call's "undo" means, for the agent; "" when there was
    nothing to roll back (a read_only job's null)."""
    if not isinstance(undo, dict):
        return ""
    rolled = undo.get("rolled_back")
    if "action" in undo:
        # A failed call inside an action rolls back all of it.
        label = undo["action"]
        if undo.get("refused"):
            # Refused before it changed anything; the action
            # stays open and whole.
            n = undo.get("steps", 0)
            still = (f"The action '{label}' is still open "
                     f"({n} step{'' if n == 1 else 's'}).")
            if undo.get("kept"):
                # The call's own step couldn't be undone
                # (the manual pump, rollback off), and run_python may have
                # changed something before it was refused.
                return (still + " This call's own step couldn't be undone "
                        "and is one of them: anything the call changed "
                        "before it was refused is still there, until "
                        "mt_rollback or Ctrl+Z in Modo.")
            return "Nothing was written. " + still
        if rolled is False:
            presses = undo.get("presses", 0)
            return (f"Not rolled back: '{label}' is closed, and {presses} "
                    "Ctrl+Z in Modo undo it, this call's step included.")
        steps = f"{rolled} step{'' if rolled == 1 else 's'}"
        if not undo.get("left"):
            return (f"Rolled back: '{label}' ({steps}) is undone and closed. "
                    "The scene is as it was before mt_begin.")
        return (f"Rolled back {steps} of '{label}', and closed it. "
                + str(undo.get("note", "")))
    if rolled is True:
        return "Rolled back: the scene is as it was before this call."
    if rolled == "unsure":
        return ("Rollback unsure: check the scene. The undo didn't reverse "
                "this call's step, so it was redone, and rollback is off "
                "until the kit restarts.")
    return "Not rolled back: one Ctrl+Z in Modo undoes this call's changes."


async def call_text(op: str, payload: dict[str, Any],
                    timeout_ms: int = protocol.DEFAULT_TIMEOUT_MS,
                    bridge: Bridge | None = None,
                    shape=None, notes: list[str] | None = None,
                    fit=None) -> str:
    """Send one op and turn the answer into the tool's text, within the size
    budget: fit(shown, limit) cuts a shape it knows, cut_text anything else.
    Never raises."""
    bridge = bridge or _bridge
    try:
        response = await bridge.call(op, payload, timeout_ms=timeout_ms)
    except BridgeError as exc:
        return exc.text()
    track_action(bridge, op, response)
    notes = list(notes or [])
    closed = response.get("action_closed")
    if closed:
        notes.append(
            f"Action '{closed.get('action')}' was committed as it stood after "
            f"it sat {closed.get('reason')}: {closed.get('presses')} Ctrl+Z in "
            "Modo undo it. This call was not part of it.")
    tail = "".join("\n\n" + n for n in notes) + format_late(response)
    has_undo = "undo" in response
    if not response.get("ok"):
        # A failed call is rolled back, or says how to.
        if has_undo:
            line = rollback_line(response["undo"])
            tail = ("\n" + line if line else "") + "\nundo: " \
                + json.dumps(response["undo"]) + tail
        if "journal" in response:
            tail = "\njournal: " + json.dumps(response["journal"]) + tail
        limit = max(500, budget.max_chars() - len(tail))
        out = format_error(response)
        if response.get("stdout"):
            out += "\nstdout:\n" + response["stdout"]
        return budget.cut_text(out, limit) + tail
    limit = max(500, budget.max_chars() - len(tail))
    result = response.get("result")
    shown = shape(response) if shape else result
    if has_undo and isinstance(shown, dict):
        shown = {**shown, "undo": response["undo"]}
    if "journal" in response and isinstance(shown, dict):
        shown = {**shown, "journal": response["journal"]}
    if isinstance(shown, str):
        text = shown or "(empty)"
    elif fit is not None:
        text = fit(shown, limit)
    else:
        text = budget.render(shown)
    return budget.cut_text(text, limit) + tail


def track_action(bridge: Bridge, op: str, response: dict[str, Any]) -> None:
    """Keep bridge.action in step with what the kit's answers say."""
    err = (response.get("error") or {}).get("type")
    undo = response.get("undo")
    if response.get("ok") and op == "begin":
        bridge.action = (response.get("result") or {}).get("action")
    elif response.get("ok") and op in ("commit", "rollback"):
        bridge.action = None
    elif err == protocol.ACTION_UNDONE or response.get("action_closed"):
        bridge.action = None
    elif op in ("commit", "rollback") and err == protocol.BAD_REQUEST:
        bridge.action = None                # the kit has none open
    elif isinstance(undo, dict) and "action" in undo:
        bridge.action = None if "rolled_back" in undo else undo["action"]
    elif isinstance(undo, dict) and err != protocol.IN_ACTION:
        # A changing call outside any action: the kit closed ours without
        # saying, as modoTools.stop or a restart does.
        bridge.action = None
    elif response.get("ok") and op == "status"             and not (response.get("result") or {}).get("action"):
        bridge.action = None


def action_refusal(bridge: Bridge | None, text: str) -> str:
    """in_action for the six names, when this server has an action open."""
    bridge = bridge or _bridge
    label = getattr(bridge, "action", None)
    names = risky.in_action(text) if label else []
    if not names:
        return ""
    return _fail(protocol.IN_ACTION,
                 f"refused {', '.join(names)}: action '{label}' is "
                 "open, and this would undo, redo or replace its steps",
                 "Nothing was sent to Modo. Commit or roll back first "
                 "(mt_commit or mt_rollback), then call again.")


async def mt_begin_text(label: str, bridge: Bridge | None = None) -> str:
    return await call_text("begin", {"label": label}, bridge=bridge)


async def mt_commit_text(bridge: Bridge | None = None) -> str:
    return await call_text("commit", {}, bridge=bridge)


async def mt_rollback_text(bridge: Bridge | None = None) -> str:
    return await call_text("rollback", {}, bridge=bridge)


async def mt_undo_last_text(bridge: Bridge | None = None) -> str:
    return await call_text("undo_last", {}, bridge=bridge, shape=undo_last_shape)


def undo_last_shape(response: dict[str, Any]) -> str:
    """Undone: journal 47 "fit the body" (run_python). The scene is as it
    was before it."""
    result = response.get("result") or {}
    words = []
    if result.get("journal") is not None:
        words.append(f"journal {result['journal']}")
    else:
        words.append("the call (no journal entry)")
    purpose = " ".join(str(result.get("purpose") or "").split())
    if purpose:
        words.append(f'"{purpose}"')
    if result.get("op"):
        words.append(f"({result['op']})")
    return ("Undone: " + " ".join(words) + ". The scene is as it was before "
            "it; the user's Ctrl+Shift+Z in Modo puts it back.")


async def mt_run_command_text(command: str, purpose: str = "",
                              acknowledge_risk: bool = False,
                              bridge: Bridge | None = None) -> str:
    refused = action_refusal(bridge, command)
    if refused:
        return refused
    found = risky.scan(command)
    if found.refused and not acknowledge_risk:
        return risky.refusal(found)
    notes = [risky.lock_note(found, in_python=False)] if found.locks else []
    return await call_text("run_command", {"command": command, "purpose": purpose},
                           bridge=bridge, notes=notes)


async def mt_run_python_text(code: str, purpose: str,
                             timeout_ms: int = protocol.DEFAULT_TIMEOUT_MS,
                             acknowledge_risk: bool = False,
                             read_only: bool = False,
                             bridge: Bridge | None = None) -> str:
    if not python_allowed():
        return _fail(protocol.PYTHON_DISABLED,
                     "Python is switched off (MODO_TOOLS_ALLOW_PYTHON=0)",
                     "Use mt_run_command, or ask the artist to switch it on.")
    if not (purpose or "").strip():
        return _fail(protocol.BAD_REQUEST, "no purpose given",
                     "Pass purpose: one line on what the code does.")
    refused = action_refusal(bridge, code)
    if refused:
        return refused
    found = risky.scan(code)
    if found.refused and not acknowledge_risk:
        return risky.refusal(found)
    notes = [risky.lock_note(found, in_python=True)] if found.locks else []
    timeout_ms = max(1000, min(int(timeout_ms), 600000))
    payload: dict[str, Any] = {"code": code, "purpose": purpose}
    if read_only:
        payload["read_only"] = True
    return await call_text(
        "run_python", payload, timeout_ms=timeout_ms,
        bridge=bridge, notes=notes,
        shape=python_shape)


def python_shape(response: dict[str, Any]) -> dict[str, Any]:
    """RESULT and stdout; and any query the guard refused that the code
    caught and went past."""
    result = response.get("result") or {}
    shown = {"result": result.get("result"), "stdout": response.get("stdout", "")}
    if result.get("refused"):
        shown["refused"] = result["refused"]
    return shown


async def mt_scene_summary_text(bridge: Bridge | None = None) -> str:
    return await call_text(
        "scene_summary", {}, bridge=bridge,
        fit=lambda r, n: budget.fit_entries(r, "selected", n, "selected items",
                                            "mt_get_selection lists them"))


async def mt_scene_graph_text(root: str = "", depth: int = 99,
                              types: list[str] | None = None,
                              bridge: Bridge | None = None) -> str:
    payload: dict[str, Any] = {"depth": depth}
    if root:
        payload["root"] = root
    if types:
        payload["types"] = types
    return await call_text("scene_graph", payload, bridge=bridge,
                           fit=budget.fit_graph)


async def mt_get_item_text(item: str, channels: list[str] | str | None = None,
                           bridge: Bridge | None = None) -> str:
    payload: dict[str, Any] = {"item": item}
    if channels:
        payload["channels"] = channels
    return await call_text(
        "get_item", payload, bridge=bridge,
        fit=lambda r, n: budget.fit_entries(r, "channels", n, "channels",
                                            "ask for channels by name"))


async def mt_get_selection_text(type: str = "item",
                                bridge: Bridge | None = None) -> str:
    return await call_text("get_selection", {"type": type}, bridge=bridge,
                           fit=budget.fit_selection)


# The most characters one rounded element entry takes on its line, with
# coordinates to 10 km and indices to ten million (142 at worst): the budget
# over this is how many entries detail="selection" may answer, with room
# left for the meshes' summaries.
ENTRY_CHARS = 150


def max_elements() -> int:
    return budget.max_chars() // ENTRY_CHARS


def render_meshes(result: dict) -> str:
    """As budget.render, but each selected element's entry on one line of
    its own, so an entry costs what ENTRY_CHARS allows."""
    lines: dict[str, str] = {}
    mark = "@entry-%s-" % uuid.uuid4().hex

    def stand_in(entry):
        key = "%s%d" % (mark, len(lines))
        lines[key] = json.dumps(entry, separators=(",", ":"))
        return key

    meshes = []
    for mesh in result.get("meshes") or []:
        chosen = mesh.get("selection") or {}
        if "elements" in chosen:
            chosen = dict(chosen, elements=[stand_in(e) for e in chosen["elements"]])
            mesh = dict(mesh, selection=chosen)
        meshes.append(mesh)
    text = budget.render(dict(result, meshes=meshes)) if lines else budget.render(result)
    return re.sub(r'"(%s\d+)"' % re.escape(mark), lambda m: lines[m.group(1)], text)


def fit_meshes(result: dict, limit: int) -> str:
    """Whole meshes, in order: a mesh's facts are never cut in half, and a
    list of selected elements is never cut at all. When not even the first
    mesh's elements fit, the answer is too_large, as the kit's limit is."""
    text = render_meshes(result)
    meshes = result.get("meshes") or []
    if len(text) > limit and meshes:
        first = render_meshes(dict(result, meshes=meshes[:1]))
        chosen = meshes[0].get("selection") or {}
        if len(first) > limit and "elements" in chosen:
            count, mode = len(chosen["elements"]), chosen.get("mode", "polygon")
            unit = {"vertex": "vertices", "edge": "edges"}.get(mode, "polygons")
            return _fail(protocol.TOO_LARGE, "{:,} {} selected in {} take {:,} "
                         "characters; {:,} fit".format(
                             count, unit, meshes[0].get("name"), len(first), limit),
                         "Ask detail=\"summary\", whose selection still gives "
                         "the count, box and centroid, or select fewer.")
    return budget.fit_entries(result, "meshes", limit, "meshes",
                              "pass items= to ask for the rest by id",
                              render=render_meshes)


async def mt_describe_mesh_text(items: list[str] | str | None = None,
                                detail: str = "summary",
                                mode: str | None = None,
                                surface: bool = False,
                                bridge: Bridge | None = None) -> str:
    payload = {"items": items or None, "detail": detail, "mode": mode,
               "max_elements": max_elements()}
    if surface:
        payload["surface"] = True
    return await call_text(
        "describe_mesh", payload,
        bridge=bridge, fit=fit_meshes)


def render_points(result: Any) -> str:
    """As budget.render, but each point and each polygon on one line of its
    own, so a point costs what the kit's POINT_CHARS allows."""
    if not isinstance(result, dict):
        return budget.render(result)
    lines: dict[str, str] = {}
    mark = "@line-%s-" % uuid.uuid4().hex

    def stand_in(entry):
        key = "%s%d" % (mark, len(lines))
        lines[key] = json.dumps(entry)
        return key

    shown = dict(result)
    for key in ("points", "polygons"):
        if isinstance(shown.get(key), list):
            shown[key] = [stand_in(e) for e in shown[key]]
    text = budget.render(shown)
    return re.sub(r'"(%s\d+)"' % re.escape(mark), lambda m: lines[m.group(1)], text)


async def mt_points_text(item: str, start: int = 0, count: int | None = None,
                         polygons: bool = False,
                         bridge: Bridge | None = None) -> str:
    """mt_points: the kit fits the answer to the budget."""
    payload: dict[str, Any] = {"item": item, "start": start, "count": count,
                               "polygons": polygons,
                               "max_chars": budget.max_chars()}
    if count is None:
        del payload["count"]
    return await call_text("points", payload, bridge=bridge,
                           fit=lambda shown, limit: render_points(shown))


async def mt_set_points_text(item: str, points: dict[str, Any] | list[Any],
                             bridge: Bridge | None = None) -> str:
    return await call_text("set_points", {"item": item, "points": points},
                           bridge=bridge)


async def mt_backdrop_text(path: str, view: str, width_m: float,
                           height_m: float | None = None,
                           center: list[float] | None = None,
                           opacity: float | None = None, name: str = "",
                           bridge: Bridge | None = None) -> str:
    """mt_backdrop: the arguments as given; the kit checks
    them all before it makes anything."""
    payload: dict[str, Any] = {"path": path, "view": view, "width_m": width_m}
    for key, value in (("height_m", height_m), ("center", center),
                       ("opacity", opacity)):
        if value is not None:
            payload[key] = value
    if name:
        payload["name"] = name
    return await call_text("backdrop", payload, bridge=bridge)


async def mt_fit_backdrops_text(item: str, backdrops: Any = None,
                                smooth: float = 0.5, iterations: int = 6,
                                floor: float | None = None,
                                bridge: Bridge | None = None) -> str:
    """mt_fit_backdrops: the arguments as given; the kit
    checks them all, and both images, before it writes anything."""
    payload: dict[str, Any] = {"item": item, "smooth": smooth,
                               "iterations": iterations}
    if backdrops is not None:
        payload["backdrops"] = backdrops
    if floor is not None:
        payload["floor"] = floor
    return await call_text("fit_backdrops", payload, bridge=bridge)


def is_shot(path: str) -> bool:
    """A file the kit's screenshot op wrote, and so ours to delete: a
    shot_*.png in a modo_tools folder. The path comes over the socket; the
    server deletes nothing else."""
    folder, name = os.path.split(os.path.normpath(path))
    return (os.path.basename(folder) == "modo_tools"
            and name.startswith("shot_") and name.endswith(".png"))


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's IHDR, which follows the 8-byte
    signature and the chunk's length and type."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


async def mt_screenshot_parts(size: Any = None, bridge: Bridge | None = None
                              ) -> tuple[bytes | None, str]:
    """(PNG bytes, the text that goes with them), or (None, an error's
    text). The kit's files, the picture and the full shot it was scaled
    from, are read and deleted here; the kit checks size."""
    bridge = bridge or _bridge
    payload: dict[str, Any] = {} if size is None else {"size": size}
    try:
        response = await bridge.call("screenshot", payload)
    except BridgeError as exc:
        return None, exc.text()
    if not response.get("ok"):
        return None, format_error(response) + format_late(response)
    result: dict[str, Any] = response.get("result") or {}
    path = str(result.get("path") or "")
    original = str(result.get("original") or "")
    if original and is_shot(original):
        try:
            os.remove(original)
        except OSError:
            pass
    if not is_shot(path):
        return None, _fail(protocol.NO_VIEWPORT,
                           "the kit named %r as the screenshot" % path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return None, _fail(protocol.NO_VIEWPORT,
                           "the screenshot %s can't be read: %s" % (path, exc),
                           "Check Modo's event log with mt_event_log.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    shape = png_size(data)
    viewport = result.get("viewport")
    if not shape:
        text = "the viewport's size (unreadable from the file)"
    elif (isinstance(viewport, list) and len(viewport) == 2
          and list(shape) != viewport):
        text = "%dx%d, scaled from the viewport's %dx%d" % (shape + tuple(viewport))
    else:
        text = "%dx%d, the viewport's size" % shape
    if result.get("note"):
        text += ". " + str(result["note"])
    if (shape and "viewport" not in result and isinstance(size, (int, float))
            and not isinstance(size, bool) and size < max(shape)):
        # An older kit takes size and ignores it.
        text += (". The kit in Modo is older than the server and can't scale "
                 "the picture: this is the full picture. Ask the user to install "
                 "the current kit")
    return data, text + format_late(response)


def screenshot_content(data: bytes, text: str) -> list:
    """What mt_screenshot returns: the image, then its text. Works with the
    mcp SDK 2.x and 1.x."""
    try:
        from mcp.server.mcpserver.utilities.types import Image
    except ImportError:
        from mcp.server.fastmcp import Image
    return [Image(data=data, format="png"), text]


async def mt_status_text(bridge: Bridge | None = None) -> str:
    """The text mt_status answers with. Never raises."""
    bridge = bridge or _bridge
    try:
        response = await bridge.call("status", timeout_ms=10000)
    except BridgeError as exc:
        return exc.text()
    if not response.get("ok"):
        return format_error(response) + format_late(response)
    track_action(bridge, "status", response)
    info = bridge.server_info
    result: dict[str, Any] = response.get("result") or {}
    shown = {
        "kit": info.get("kit"),
        "protocol": info.get("protocol"),
        "modo": info.get("modo"),
        "python": info.get("python"),
        "port": result.get("port"),
        "pump": result.get("pump"),
        "queue": result.get("queue"),
        "last_session": result.get("last_session"),
        "undo": result.get("undo"),
        "rollback": result.get("rollback"),
        "action": result.get("action"),
        "journal": result.get("journal"),
        "server": __version__,
    }
    return json.dumps(shown, indent=2) + format_late(response)


def mt_op_log_text(count: int = 20) -> str:
    """The op log's last lines, read here, not through the kit."""
    count = max(1, min(int(count), 500))
    lines = watch.read_oplog(count)
    if lines is None:
        return (f"There is no op log at {protocol.oplog_path()} yet: the kit "
                "writes it from its first job on.")
    return budget.cut_text("\n".join(lines) or "(empty)", budget.max_chars())


def mt_journal_text(name: str = "", current: str | None = None,
                    facts: str | None = None,
                    open_action: str | None = None) -> str:
    """The text mt_journal answers with, from the files alone, after it
    builds the replay beside the journal. An empty name is `current`
    (the kit's journal), else the newest. facts: what mt_journal_async
    asked the kit for, one line. open_action: the label of the action the
    kit holds open, which counts only for the current scene's journal; any other action with no commit was open at a crash."""
    newest = journal.names()
    name = _journal_name(name)
    if not newest:
        return ("No journal yet: the kit starts one with the first call that "
                f"changes a scene, in {protocol.journals_dir()}.")
    if not name:
        name = current if current in newest else newest[0]
    records = journal.read(name) if name in newest else None
    if records is None:
        return _fail(protocol.BAD_REQUEST, f"no journal called {name!r}",
                     "The newest: " + ", ".join(newest[:journal.NEWEST]) + ".")
    if current is not None and name != current:
        open_action = None
    checks, as_code = journal.summary(records)
    try:
        replay = ["replay: " + journal.write_replay(name, records, open_action),
                  "  checks: %s;  untraced entries: %s" % (
                      ", ".join(checks) or "none",
                      "%d (%s)" % (len(as_code), ", ".join(map(str, as_code)))
                      if as_code else "0")]
    except OSError as exc:
        replay = [f"replay: not written: {type(exc).__name__}: {exc}"]
    if facts:
        replay.append(facts)
    limit = budget.max_chars()
    return budget.cut_text(
        journal.render(name, records, newest, limit, replay=replay,
                       open_action=open_action), limit)


def _journal_name(name: str) -> str:
    name = (name or "").strip()
    return name[:-len(".jsonl")] if name.endswith(".jsonl") else name


async def mt_journal_async(name: str = "", bridge: Bridge | None = None) -> str:
    """mt_journal: ask the kit which journal is the current scene's, and for
    a facts record in it, then build. With Modo closed, from the
    files: an empty name is the newest."""
    bridge = bridge or _bridge
    asked = _journal_name(name)
    current = None
    open_action = None
    try:
        response = await bridge.call("status", timeout_ms=3000)
        current = (((response.get("result") or {}).get("journal") or {})
                   .get("name"))
        open_action = (((response.get("result") or {}).get("action") or {})
                       .get("label"))
        if asked and asked != current:
            facts = f"facts: none taken; {asked} isn't the current scene's journal"
        else:
            answer = await bridge.call("journal_facts", timeout_ms=30000)
            result = answer.get("result") or {}
            if not answer.get("ok"):
                facts = "facts: none taken; " + str(
                    (answer.get("error") or {}).get("message"))
            elif result.get("facts") is not None:
                facts = "facts: taken after entry %s" % result["facts"]
            else:
                facts = "facts: none taken; " + str(
                    result.get("note") or result.get("error"))
    except BridgeError:
        facts = "Modo isn't reachable: no new facts were taken"
    return mt_journal_text(name, current, facts, open_action)


LX_RETRY_S = 60.0
_lx_asked = -LX_RETRY_S
# Why the last try for the lx dump made none, for the search's note.
_lx_why = ""
LX_OLD_KIT = (
    "The lx module (lx.object, lx.service, lx.symbol) is missing: the "
    "modo_tools kit running in Modo is older than this server and can't "
    "list it (it has no api_dump). Install the kit again from "
    "dist\\modo_tools.lpk and restart Modo."
)


async def ensure_lx_dump(bridge: Bridge) -> None:
    """The lx module is compiled, so its classes are read in
    Modo, once per Modo version (a read job, no undo step). Asks only when
    no dump is saved for the version Modo says it is, or, before the bridge
    has talked to Modo, when there is no dump at all. Never raises: Modo
    closed or busy leaves the search without the lx part; a kit too old to
    have the op leaves _lx_why saying so (Modo's installed
    kit was older)."""
    global _lx_asked, _lx_why
    version = (bridge.server_info or {}).get("modo")
    if version and docs.has_lx_dump(version):
        _lx_why = ""
        return
    if not version and docs.lx_dumps():
        return
    # A refused connection costs about 2 s on Windows: with Modo closed,
    # ask again only after LX_RETRY_S.
    now = time.monotonic()
    if now - _lx_asked < LX_RETRY_S:
        return
    _lx_asked = now
    try:
        if not version:
            await bridge.call("status", timeout_ms=3000)
            version = (bridge.server_info or {}).get("modo")
            if not version or docs.has_lx_dump(version):
                return
        response = await bridge.call("api_dump", timeout_ms=60000)
    except BridgeError:
        return
    result = response.get("result")
    if response.get("ok") and isinstance(result, dict):
        try:
            await asyncio.to_thread(docs.save_lx_dump,
                                    dict(result, version=version))
        except OSError:
            return
        _lx_why = ""
        docs.forget()
    elif (response.get("error") or {}).get("type") == protocol.UNKNOWN_OP:
        _lx_why = LX_OLD_KIT


async def mt_search_docs_text(query: str, kind: str = "", limit: int = 8,
                              bridge: Bridge | None = None) -> str:
    """The text mt_search_docs answers with. Never raises."""
    try:
        docs.check_query(query, kind)
    except docs.DocsError as exc:
        return _fail(exc.type, exc.message, exc.hint)
    await ensure_lx_dump(bridge or _bridge)
    try:
        answer = await asyncio.to_thread(docs.search, query, kind or None,
                                         limit, _lx_why)
    except docs.DocsError as exc:
        return _fail(exc.type, exc.message, exc.hint)
    return budget.fit_entries(answer, "hits", budget.max_chars(), "hits",
                              "ask with a smaller limit, or a kind")


def mt_read_doc_text(id: str, offset: int = 0) -> str:
    """The text mt_read_doc answers with. Never raises a DocsError."""
    try:
        entry = docs.read(id, offset, budget.max_chars())
    except docs.DocsError as exc:
        return _fail(exc.type, exc.message, exc.hint)
    return json.dumps(entry)


def build_server():
    """The MCP server with its tools. Works with the mcp SDK 2.x (MCPServer)
    and 1.x (FastMCP)."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    from mcp.types import ToolAnnotations

    try:
        mcp = Server(name="modo-tools", instructions=INSTRUCTIONS,
                     version=__version__)
    except TypeError:
        mcp = Server(name="modo-tools", instructions=INSTRUCTIONS)

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    changes = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                              open_world_hint=False)

    @mcp.tool(description=STATUS_DESCRIPTION, annotations=read_only)
    async def mt_status() -> str:
        return await mt_status_text()

    @mcp.tool(description=RUN_COMMAND_DESCRIPTION, annotations=changes)
    async def mt_run_command(command: str, purpose: str = "",
                             acknowledge_risk: bool = False) -> str:
        return await mt_run_command_text(command, purpose, acknowledge_risk)

    if python_allowed():
        @mcp.tool(description=RUN_PYTHON_DESCRIPTION, annotations=changes)
        async def mt_run_python(code: str, purpose: str,
                                timeout_ms: int = protocol.DEFAULT_TIMEOUT_MS,
                                acknowledge_risk: bool = False,
                                read_only: bool = False) -> str:
            return await mt_run_python_text(code, purpose, timeout_ms,
                                            acknowledge_risk, read_only)

    @mcp.tool(description=BEGIN_DESCRIPTION, annotations=read_only)
    async def mt_begin(label: str) -> str:
        return await mt_begin_text(label)

    @mcp.tool(description=COMMIT_DESCRIPTION, annotations=read_only)
    async def mt_commit() -> str:
        return await mt_commit_text()

    @mcp.tool(description=ROLLBACK_DESCRIPTION, annotations=changes)
    async def mt_rollback() -> str:
        return await mt_rollback_text()

    @mcp.tool(description=UNDO_LAST_DESCRIPTION, annotations=changes)
    async def mt_undo_last() -> str:
        return await mt_undo_last_text()

    @mcp.tool(description=SET_CHANNELS_DESCRIPTION, annotations=changes)
    async def mt_set_channels(item: str, channels: dict[str, Any]) -> str:
        return await call_text("set_channels", {"item": item, "channels": channels})

    @mcp.tool(description=SAVE_SCENE_DESCRIPTION, annotations=changes)
    async def mt_save_scene(path: str = "") -> str:
        return await call_text("save_scene", {"path": path} if path else {})

    @mcp.tool(description=COMMAND_INFO_DESCRIPTION, annotations=read_only)
    async def mt_command_info(name: str) -> str:
        return await call_text("command_info", {"name": name})

    @mcp.tool(description=FIND_COMMANDS_DESCRIPTION, annotations=read_only)
    async def mt_find_commands(query: str, limit: int = 25) -> str:
        return await call_text("find_commands", {"query": query, "limit": limit})

    @mcp.tool(description=SEARCH_DOCS_DESCRIPTION, annotations=read_only)
    async def mt_search_docs(query: str, kind: str = "", limit: int = 8) -> str:
        return await mt_search_docs_text(query, kind, limit)

    @mcp.tool(description=READ_DOC_DESCRIPTION, annotations=read_only)
    async def mt_read_doc(id: str, offset: int = 0) -> str:
        return await asyncio.to_thread(mt_read_doc_text, id, offset)

    @mcp.tool(description=SCENE_SUMMARY_DESCRIPTION, annotations=read_only)
    async def mt_scene_summary() -> str:
        return await mt_scene_summary_text()

    @mcp.tool(description=SCENE_GRAPH_DESCRIPTION, annotations=read_only)
    async def mt_scene_graph(root: str = "", depth: int = 99,
                             types: list[str] | None = None) -> str:
        return await mt_scene_graph_text(root, depth, types)

    @mcp.tool(description=GET_ITEM_DESCRIPTION, annotations=read_only)
    async def mt_get_item(item: str, channels: list[str] | str | None = None) -> str:
        return await mt_get_item_text(item, channels)

    @mcp.tool(description=GET_SELECTION_DESCRIPTION, annotations=read_only)
    async def mt_get_selection(type: str = "item") -> str:
        return await mt_get_selection_text(type)

    @mcp.tool(description=DESCRIBE_MESH_DESCRIPTION, annotations=read_only)
    async def mt_describe_mesh(items: list[str] | str | None = None,
                               detail: str = "summary",
                               mode: str | None = None,
                               surface: bool = False) -> str:
        return await mt_describe_mesh_text(items, detail, mode, surface)

    @mcp.tool(description=POINTS_DESCRIPTION, annotations=read_only)
    async def mt_points(item: str, start: int = 0, count: int | None = None,
                        polygons: bool = False) -> str:
        return await mt_points_text(item, start, count, polygons)

    @mcp.tool(description=SET_POINTS_DESCRIPTION, annotations=changes)
    async def mt_set_points(item: str,
                            points: dict[str, Any] | list[Any]) -> str:
        # No finer types: the kit checks every index and value and says
        # what to send, where a typed argument answered a wrong shape with
        # the mcp package's validation text.
        return await mt_set_points_text(item, points)

    @mcp.tool(description=SCREENSHOT_DESCRIPTION, annotations=read_only)
    async def mt_screenshot(size: int | float | None = None):
        # Not int alone: the kit refuses 1.5 with its own text and hint.
        data, text = await mt_screenshot_parts(size)
        return text if data is None else screenshot_content(data, text)

    @mcp.tool(description=BACKDROP_DESCRIPTION, annotations=changes)
    async def mt_backdrop(path: str, view: str, width_m: float,
                          height_m: float | None = None,
                          center: list[float] | None = None,
                          opacity: float | None = None, name: str = "") -> str:
        return await mt_backdrop_text(path, view, width_m, height_m, center,
                                      opacity, name)

    @mcp.tool(description=FIT_BACKDROPS_DESCRIPTION, annotations=changes)
    async def mt_fit_backdrops(item: str, backdrops: Any = None,
                               smooth: Any = 0.5, iterations: Any = 6,
                               floor: Any = None) -> str:
        # Typed Any: the kit refuses a wrong value and says what to send
        #.
        return await mt_fit_backdrops_text(item, backdrops, smooth,
                                           iterations, floor)

    @mcp.tool(description=OP_LOG_DESCRIPTION, annotations=read_only)
    async def mt_op_log(count: int = 20) -> str:
        return mt_op_log_text(count)

    @mcp.tool(description=JOURNAL_DESCRIPTION, annotations=read_only)
    async def mt_journal(name: str = "") -> str:
        return await mt_journal_async(name)

    @mcp.tool(description=EVENT_LOG_DESCRIPTION, annotations=read_only)
    async def mt_event_log(count: int = 30) -> str:
        return await call_text("event_log", {"count": count},
                               shape=lambda r: "\n".join(
                                   (r.get("result") or {}).get("lines", [])))

    return mcp


async def _call_once(op: str, payload: dict[str, Any], timeout_ms: int) -> int:
    bridge = Bridge(HOST, PORT)
    try:
        response = await bridge.call(op, payload, timeout_ms)
    except BridgeError as exc:
        print(exc.text())
        return 1
    finally:
        await bridge.close()
    print(json.dumps({"hello": bridge.server_info, "answer": response},
                     indent=2, default=str))
    return 0 if response.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "call":
        if len(argv) < 2:
            print("usage: python -m modo_tools call <op> [json payload] [timeout_ms]")
            return 2
        payload = json.loads(argv[2]) if len(argv) > 2 else {}
        timeout_ms = int(argv[3]) if len(argv) > 3 else protocol.DEFAULT_TIMEOUT_MS
        return asyncio.run(_call_once(argv[1], payload, timeout_ms))
    build_server().run(transport="stdio")
    return 0
