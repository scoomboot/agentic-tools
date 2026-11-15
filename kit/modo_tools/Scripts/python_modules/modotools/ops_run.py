# python
"""run_command, run_python, set_channels, save_scene, undo_last.

Every lx call here runs inside run_job's guard, so it gets !! (a failure is
an error, never a dialog that freezes Modo) and can't send a query that
crashes Modo. The kit's own commands go through lx.command with named
arguments, never a joined string, so a name with a space or a quote can't
break one.
"""

import traceback

from . import errors, guard, protocol
from .jobs import op
from .ops_lookup import is_tool, tool_hint

CODE_NAME = "<mt_run_python>"


def say(purpose):
    """The purpose line in Modo's event log, so the artist sees what ran."""
    purpose = " ".join(str(purpose or "").split())
    if purpose:
        import lx
        lx.out("mt > " + purpose[:200])


def plain(value, depth=0):
    """Something JSON can carry. Built-in containers are walked; anything
    else becomes a short string, without touching Modo's objects further."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 6:
        return _short(value)
    if type(value) in (list, tuple, set, frozenset):
        return [plain(v, depth + 1) for v in list(value)[:1000]]
    if type(value) is dict:
        return {str(k): plain(v, depth + 1) for k, v in list(value.items())[:1000]}
    return _short(value)


def _short(value):
    try:
        return "<%s: %s>" % (type(value).__name__, str(value)[:200])
    except Exception:
        return "<%s>" % type(value).__name__


def command_hint(name):
    if name.startswith("@"):
        # A probe (17.1, 2026-11-08): a script that raises fails with no message.
        return ("The script raised: Modo has undone what it ran, and its own "
                "lines in the event log say why. A journal replay names the "
                "check or the line that stopped it.")
    if guard.exists(name):
        return ("%r is a command, so its arguments or the selection are likely "
                "wrong. Check them with mt_command_info(%r)." % (name, name))
    if is_tool(name):
        return tool_hint(name)
    near = guard.similar(name)
    return ("%r isn't a command.%s Search with mt_find_commands, then check "
            "the arguments with mt_command_info."
            % (name, (" Close: %s." % ", ".join(near)) if near else ""))


def failed(exc, name):
    """A command line that raised. A name that isn't a command (a tool's
    name too) ran nothing: on 17.1 Modo says "command could not be found".
    That is unknown_command, a refusal, so an open action stays open (before,
    as command_failed it rolled the whole action back). A
    script (@file) and a command that exists are command_failed."""
    if not name.startswith("@") and not guard.exists(name):
        return errors.OpError(protocol.UNKNOWN_COMMAND, str(exc) or repr(exc),
                              hint="Nothing ran. " + command_hint(name))
    return errors.OpError(protocol.COMMAND_FAILED, str(exc) or repr(exc),
                          hint=command_hint(name))


def _need(payload, key, example):
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise errors.OpError(protocol.BAD_REQUEST, "no %s given" % key,
                             hint="Pass %s, e.g. %s." % (key, example))
    return value


@op("run_command")
def run_command(payload, kit):
    import lx
    command = str(_need(payload, "command", "item.create mesh")).strip()
    guard.check(command)
    say(payload.get("purpose"))
    try:
        result = lx.eval(command)
    except errors.OpError:
        raise
    except Exception as exc:
        raise failed(exc, guard.command_name(command))
    answer = {"command": command, "result": plain(result)}
    note = stray_note(command)
    if note:
        answer["note"] = note
    return answer


def stray_note(command):
    """A note for each word of the line that isn't one of its argument's
    values, with what Modo set, read back with ? as the journal does.
    Modo ran the command, so this is a note, not an error. When the
    argument can't be asked (view3d.shadingStyle's bgmesh), the note says
    what Modo's parser took the word as (guard.taken); when
    the line can't be spawned either, it lists the values. None when every
    word is known."""
    import lx
    line = str(command).lstrip("!+ ")
    notes = []
    for arg, word, span, known in guard.strays(line):
        try:
            read = guard.as_argument(lx.eval(guard.with_word(line, span, "?")))
        except Exception:
            read = None
        took = guard.taken(line, arg, known) if read is None else None
        if read is not None:
            notes.append("%s is not one of %s's values; Modo set %s."
                         % (word, arg, read))
        elif took is not None:
            notes.append("%s is not one of %s's values; Modo took it as %s."
                         % (word, arg, took))
        else:
            notes.append("%s is not one of %s's values (%s); Modo may have "
                         "taken it as another." % (word, arg, ", ".join(known)))
    for word in guard.unread(line):
        # `select.symmetry 0 uvActive:0 u` left uvAxis as
        # it was, with no error.
        notes.append("Modo didn't read %s: a word with no name after a named "
                     "one is dropped. Give it its argument's name." % word)
    return " ".join(notes) or None


@op("run_python")
def run_python(payload, kit):
    """Run agent Python. RESULT comes back as the result; print() output is
    collected by run_job as stdout."""
    code = _need(payload, "code", "RESULT = len(modo.Scene().items)")
    read_only = payload.get("read_only", False)
    if not isinstance(read_only, bool):
        raise errors.OpError(protocol.BAD_REQUEST, "read_only must be true or false",
                             hint="Pass read_only=true for code that only reads.")
    purpose = payload.get("purpose")
    if not str(purpose or "").strip():
        raise errors.OpError(
            protocol.BAD_REQUEST, "no purpose given",
            hint="Pass purpose: one line on what the code does. It is shown "
                 "in Modo's event log as 'mt > <purpose>'.")
    try:
        compiled = compile(code, CODE_NAME, "exec")
    except SyntaxError as exc:
        import sys
        raise errors.OpError(
            protocol.PYTHON_ERROR,
            "SyntaxError: %s (line %s)" % (exc.msg, exc.lineno),
            hint="Modo runs Python %d.%d." % tuple(sys.version_info[:2]))

    import lx
    namespace = {"__name__": "__mt_run_python__", "lx": lx, "RESULT": None}
    for module in ("modo", "lxu"):
        try:
            namespace[module] = __import__(module)
        except Exception:
            pass

    say(purpose)
    try:
        exec(compiled, namespace)
    except (guard.Refused, guard.InAction):
        raise
    except SystemExit as exc:
        raise errors.OpError(
            protocol.PYTHON_ERROR, "the code called sys.exit(%s)" % (exc.code,),
            hint="Don't call sys.exit(): in Modo it ends the script host, not "
                 "the job. Raise an exception to stop, or let the code end; "
                 "set RESULT for a value.")
    except Exception as exc:
        raise errors.OpError(
            protocol.PYTHON_ERROR, "%s: %s" % (type(exc).__name__, exc),
            traceback_text=_agent_traceback(exc),
            hint=_python_hint(exc, read_only))
    out = {"result": plain(namespace.get("RESULT"))}
    # A refusal the code caught and went past: nothing was sent, but the
    # code may have counted on an answer.
    refused = guard.refused_so_far()
    if refused:
        out["refused"] = [r.message for r in refused]
    return out


def _agent_traceback(exc):
    """The traceback from the agent's code down, without the kit's frames."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != CODE_NAME:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))


def _python_hint(exc, read_only=False):
    text = str(exc)
    hint = ""
    if isinstance(exc, RuntimeError) and text and not isinstance(exc, guard.ReadOnly):
        hint = ("A Modo command or call failed. The event log lines say why; "
                "check a command's arguments with mt_command_info.")
    if read_only:
        # The guard refuses edits in a read-only job (guard.ReadOnly).
        hint = (hint + " " if hint else "") + (
            "This ran with read_only=true, which can't change the scene: if "
            "it was meant to, call again without read_only.")
    return hint


def _resolve(ref):
    # Imported here: ops_scene imports plain() from this module.
    from .ops_scene import resolve
    return resolve(ref)


# pos.X on a mesh: the mesh has no such channel; its translation item does.
_TRANSFORMS = (("pos.", "position"), ("rot.", "rotation"), ("scl.", "scale"))


def _transform_for(item, channel):
    """The transform item that holds `channel` for a locator, else None.

    The scene's own item (a modo.Mesh, say) has .position, .rotation and
    .scale. Wrapping it in modo.Locator raises 'Locator item is required' on
    17.1 for a mesh (seen 2026-11-08), so it isn't used. A backdrop is a
    locator to Modo, but the TD SDK gives it back as a plain modo.Item with
    no .position (a test model, 2026-11-10): it is found as mt_get_item finds
    it, through lx.object.Locator, which only looks.
    """
    for prefix, attribute in _TRANSFORMS:
        if channel.startswith(prefix):
            try:
                found = getattr(item, attribute)
            except AttributeError:
                found = _locator_transform(item, attribute)
            except Exception:
                return None
            try:
                return found if found is not None and found.channel(channel) is not None else None
            except Exception:
                return None
    return None


_XFRM_SYMBOLS = {"position": "iXFRM_POSITION", "rotation": "iXFRM_ROTATION",
                 "scale": "iXFRM_SCALE"}


def _locator_transform(item, attribute):
    """A locator-type item's existing transform item, or None. Nothing is
    made: a missing one stays bad_channel, as nothing may change before
    every channel is checked."""
    import lx
    import modo
    try:
        if not item.isLocatorSuperType():
            return None
        return modo.Item(lx.object.Locator(item).GetTransformItem(
            getattr(lx.symbol, _XFRM_SYMBOLS[attribute])))
    except (LookupError, TypeError, RuntimeError, AttributeError):
        return None


@op("set_channels")
def set_channels(payload, kit):
    """Set channels on one item: {"item": id or name, "channels": {name: value}}.

    Values are raw, as Python reads them: metres, radians, 0-1 for percent.
    (item.channel would take UI units, degrees for angles, while reading back
    gives radians; checked on 17.1, 2026-11-08.) pos/rot/scl on a locator go
    to its transform items. Every channel is checked before any is written.
    """
    ref = _need(payload, "item", "mesh001")
    channels = payload.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise errors.OpError(protocol.BAD_REQUEST, "no channels given",
                             hint='Pass channels as {"pos.X": 1.0}.')
    item = _resolve(ref)
    targets, missing = {}, []
    for name in channels:
        if item.channel(name) is not None:
            targets[name] = item
        else:
            holder = _transform_for(item, name)
            if holder is None:
                missing.append(name)
            else:
                targets[name] = holder
    if missing:
        hint = "Nothing was written. Vector channels are separate (pos.X, pos.Y, pos.Z)."
        if any(m.startswith(p) for m in missing for p, _a in _TRANSFORMS):
            hint += (" Position, rotation and scale live on the transform "
                     "items of locator-type items (meshes, locators, cameras, "
                     "lights); check this item's type with mt_get_item.")
        raise errors.OpError(
            protocol.BAD_CHANNEL,
            "%s has no channel %s" % (item.id, ", ".join(repr(m) for m in missing)),
            hint=hint)
    done = {}
    for name, value in channels.items():
        target = targets[name]
        channel = target.channel(name)
        old = plain(channel.get())
        try:
            channel.set(value)
        except Exception as exc:
            error = errors.OpError(
                protocol.COMMAND_FAILED, "%s.%s: %s" % (target.id, name, exc),
                hint="Check the value's type for this channel.")
            if done:
                error.hint += " Already written: %s." % ", ".join(done)
            raise error
        entry = {"old": old, "new": plain(target.channel(name).get())}
        if target is not item:
            entry["item"] = target.id
        done[name] = entry
    return {"item": item.id, "channels": done}


@op("save_scene")
def save_scene(payload, kit):
    """Save to `path`, or to the scene's own file."""
    import lx
    import modo
    path = str(payload.get("path") or "").strip()
    if path:
        # Forward slashes: Modo takes them everywhere, and a backslash in a
        # command argument is not something to rely on.
        path = path.replace("\\", "/")
        name = "scene.saveAs"
        before = _stamp(path)
        try:
            lx.command(name, filename=path)
        except errors.OpError:
            raise
        except Exception as exc:
            raise errors.OpError(protocol.COMMAND_FAILED,
                                 "saving to %s failed: %s" % (path, exc),
                                 hint="Check the folder exists and is writable.")
        return {"saved": path, "bytes": _written(path, before)}
    current = modo.Scene().filename
    if not current:
        raise errors.OpError(protocol.BAD_REQUEST,
                             "the scene has never been saved",
                             hint="Pass a path for its first save.")
    # scene.saveAs to its own file, not scene.save: on 17.1 scene.save fails
    # when nothing changed since the last save (seen 2026-11-08).
    before = _stamp(current)
    try:
        lx.command("scene.saveAs", filename=current)
    except errors.OpError:
        raise
    except Exception as exc:
        raise errors.OpError(protocol.COMMAND_FAILED,
                             "saving %s failed: %s" % (current, exc))
    return {"saved": current, "bytes": _written(current, before)}


def _stamp(path):
    """The file's modification time and size, or None if it isn't there."""
    import os
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _written(path, before=None):
    """The saved file's size. Modo can report a save as done without writing
    anything (seen on 17.1, 2026-11-08), so the file is checked: it has to be
    there, not empty, and, if it was there before the save, touched by it."""
    after = _stamp(path)
    size = after[1] if after else 0
    if not size or after == before:
        raise errors.OpError(
            protocol.COMMAND_FAILED,
            "Modo reported the save as done, but %s wasn't written" % path,
            hint="The scene's file name may now point at that path. Check "
                 "Modo's event log (mt_event_log), and save from Modo's File "
                 "menu if it keeps failing.")
    return size


# -- undo the last call -------------------------------------------------

NOTHING_HINT = (
    "Reads and run_python with read_only leave no step; a call that failed "
    "was rolled back already; a call the user undid stays undone (their "
    "Ctrl+Shift+Z puts it back); steps from before the kit started aren't "
    "known.")


@op("undo_last", read=True, direct=True)
def undo_last(payload, kit):
    """Undo the newest undo step when it is an agent call's, and say which
    call it was. Runs in the idle visit, outside any command, like a
    rollback (Modo refuses app.undo inside one). The marker says whose the
    step is: when app.undo reverses no marker of ours, the step was someone
    else's, so it is redone at once, with nothing read in between (5.3's
    a probe), and refused. So is a step that is another session's call: two agents can share one Modo, and each undoes its own."""
    from .undo import quiet
    caller = kit.action.caller
    kit.action.refuse_open("undo_last", caller)
    markers = kit.markers
    newest = markers.newest_forward()
    if newest is None:
        raise errors.OpError(
            protocol.BAD_REQUEST,
            "nothing to undo: no agent call's step is on Modo's undo stack",
            hint=NOTHING_HINT)
    if not kit.pump.outside_command or kit.rollback.off:
        why = ("rollback is off after an unsure rollback" if kit.rollback.off
               else "the kit can't undo from inside a command (the manual pump)")
        raise errors.OpError(
            protocol.BAD_REQUEST, "nothing undone: %s" % why,
            hint="If the newest step in Modo is %s, one Ctrl+Z in Modo "
                 "undoes it; ask the user." % _call_words(_call(kit, newest)))
    before = markers.forward()
    redo_failed = None
    import lx
    with quiet(markers):
        try:
            lx.eval("!!app.undo")
        except Exception as exc:
            raise errors.OpError(
                protocol.COMMAND_FAILED, "app.undo failed: %s" % exc,
                hint="Nothing was undone, as far as the kit can tell. Check "
                     "the scene with mt_describe_mesh.")
        undone = sorted(s for s in before if markers.state(s) == "reversed")
        theirs = len(undone) == 1 and _other_session(markers, undone[0], caller)
        if len(undone) != 1 or theirs:
            # No marker of ours (or, never seen, more than one) reversed, or
            # the call was another session's: put the step back before
            # anything else runs.
            try:
                lx.eval("!!app.redo")
            except Exception as exc:
                redo_failed = str(exc) or type(exc).__name__
                kit.pump.log("modo-tools: app.redo failed after undo_last: "
                             "%s" % redo_failed)
    if redo_failed is not None:
        # The step is undone and it shouldn't be: say that, not "nothing
        # was undone".
        raise errors.OpError(
            protocol.COMMAND_FAILED,
            "the newest step on Modo's undo stack wasn't this session's "
            "call, so it was undone to find out and had to be put back, but "
            "app.redo failed: %s. That step is still undone" % redo_failed,
            hint="Ask the user to press Ctrl+Shift+Z in Modo once, then check "
                 "the scene with mt_describe_mesh / mt_scene_graph before "
                 "going on.")
    if len(undone) != 1:
        raise errors.OpError(
            protocol.BAD_REQUEST,
            "the newest step on Modo's undo stack isn't an agent call's: "
            "it is %s. Nothing was undone" % _whose(kit),
            hint="Don't undo past the user's work: ask them. Once their step is off "
                 "the stack (their Ctrl+Z), mt_undo_last reaches %s."
                 % _call_words(_call(kit, newest)))
    step = undone[0]
    call = _call(kit, step)
    if theirs:
        raise errors.OpError(
            protocol.BAD_REQUEST,
            "the newest step on Modo's undo stack is another agent "
            "session's call: %s. Nothing was undone" % _call_words(call),
            hint="mt_undo_last undoes only this session's own calls. The "
                 "agent that made that call can undo it, or one Ctrl+Z in "
                 "Modo does; ask the user.")
    if kit.journal is not None:
        kit.journal.agent_undone(step)
    say("undo: " + _call_words(call))
    call["scene"] = "as it was before that call"
    return call


def _other_session(markers, step, caller):
    """Whether a marker's step is a call from a session other than the
    caller's. A step with no session on record counts as the caller's."""
    session = markers.about(step).get("session")
    return session is not None and session != caller


def _call(kit, step):
    """{"journal", "op", "purpose"} for a marker's step."""
    about = kit.markers.about(step)
    n = kit.journal.entry_of(step) if kit.journal is not None else None
    return {"journal": n, "op": about.get("op"),
            "purpose": about.get("purpose")}


def _call_words(call):
    """journal 47 "fit the body" (run_python)"""
    words = []
    if call.get("journal") is not None:
        words.append("journal %s" % call["journal"])
    else:
        words.append("the call")
    purpose = " ".join(str(call.get("purpose") or "").split())
    if purpose:
        words.append('"%s"' % purpose[:120])
    if call.get("op"):
        words.append("(%s)" % call["op"])
    return " ".join(words)


def _whose(kit):
    """Whose the step on top is, as far as the kit heard."""
    listen = kit.listen
    items = listen.peek() if listen is not None else None
    if items is None:
        return ("the user's: a selection or an edit in Modo, or a step from "
                "before the kit started")
    if not items:
        return "the user's edit in Modo"
    names = []
    for ident in items[:5]:
        name = kit.journal.item_name(ident) if kit.journal is not None else None
        names.append(name or ident)
    more = len(items) - len(names)
    return "the user's edit in Modo, to %s%s" % (
        ", ".join(names), " and %d more" % more if more > 0 else "")
