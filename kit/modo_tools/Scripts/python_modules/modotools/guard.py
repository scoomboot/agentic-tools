# python
"""The guard run_job puts around every job.

Two things Modo 17.1 does that no try/except can fix after the fact:

* A command that fails opens a modal dialog. The dialog blocks the main
  thread, and with it the kit, until someone clicks OK. With a !! prefix
  the same failure is an error instead. modo-mcp found that queries need it
  too: an unknown command service attribute is also a dialog.
* A per-command command service query for a name that isn't a command,
    query commandservice command.username ? {no.such}
  crashes Modo outright. The only defence is not to send it.

So while a job runs, lx.eval, lx.eval1, lx.evalN and lx.command are replaced
by versions that add !! and refuse such a query. In an exec job the same
versions keep the trace for the journal: each line that isn't a
query, as sent, and each modo write that sends no command. Patching the lx module, not
handing the job a wrapper, is what reaches agent Python too, which does its
own `import lx`. Jobs never overlap and the originals are put back when the
job ends, so the artist's own commands are never touched.

The idea is modo-mcp's (ops_command.py); the code is ours.
"""

import difflib
import math
import re
import time

from . import errors, protocol

_PER_COMMAND = re.compile(
    r"^[\s!+]*query\s+commandservice\s+command\.\w+\s*\?\s*(.*?)\s*$",
    re.IGNORECASE | re.DOTALL)

# Modo's command names, read once per session. A name missing from the list
# re-reads it, at most every few seconds, in case a kit registered late.
_names = []
_names_at = 0.0
_REFRESH_S = 5.0

_active = None


class Refused(errors.OpError):
    """A query that would crash Modo, refused before it was sent."""

    def __init__(self, message, hint):
        errors.OpError.__init__(self, protocol.UNKNOWN_COMMAND, message, hint)


def suppress(command):
    """The command with !! in front, unless it already has a prefix."""
    text = str(command).lstrip()
    if text.startswith("!"):
        return text
    return "!!" + text


def command_name(command):
    """The command's name: its first word, without ! or + prefixes."""
    text = str(command).strip().lstrip("!+").strip()
    return text.split()[0] if text else ""


def query_target(command):
    """The name a per-command command service query asks about, else None."""
    match = _PER_COMMAND.match(str(command))
    if not match:
        return None
    target = match.group(1).strip()
    if target.startswith("{") and target.endswith("}"):
        target = target[1:-1]
    return target.strip().strip("\"'").strip()


def _evalN():
    if _active is not None:
        return _active.real_evalN
    import lx
    return lambda text: lx.evalN(suppress(text))


def names(refresh=False):
    """Every command name Modo knows. The list query is safe."""
    global _names, _names_at
    if _names and not refresh:
        return _names
    try:
        found = _evalN()(suppress("query commandservice commands ?")) or []
    except Exception:
        found = []
    if isinstance(found, str):
        found = [found]
    found = sorted(set(str(n) for n in found))
    if found:
        _names, _names_at = found, time.time()
    return _names


def exists(name):
    if not name:
        return False
    if name in names():
        return True
    if time.time() - _names_at > _REFRESH_S:
        return name in names(refresh=True)
    return False


def similar(name, limit=5):
    """Command names close to `name`: same start, close spelling, same word."""
    known = names()
    low = name.lower()
    prefix = [n for n in known if n.lower().startswith(low)]
    close = difflib.get_close_matches(name, known, n=limit, cutoff=0.6)
    last = low.split(".")[-1]
    word = [n for n in known if last and last in n.lower()]
    out = []
    for n in close + prefix + word:
        if n not in out and n.lower() != low:
            out.append(n)
    return out[:limit]


def refusal(command):
    """Refused, if `command` is a per-command query for an unknown name."""
    target = query_target(command)
    if target is None or exists(target):
        return None
    near = similar(target) if target else []
    if not names():
        message = ("refused %r: Modo's command list couldn't be read, so %r "
                   "can't be checked, and on Modo 17.1 a command service "
                   "query for an unknown name crashes Modo" % (command, target))
    else:
        message = ("refused %r: %r is not a command, and on Modo 17.1 a "
                   "command service query for an unknown name crashes Modo"
                   % (command, target))
    hint = "Nothing was sent to Modo. Find the name with mt_find_commands"
    if near:
        hint += " (close: %s)" % ", ".join(near)
    hint += ", then look it up with mt_command_info."
    return Refused(message, hint)


def check(command):
    refused = refusal(command)
    if refused is not None:
        if _active is not None:
            _active.refused.append(refused)
        raise refused


def refused_so_far():
    """The refusals in this job so far, including ones agent Python caught."""
    return list(_active.refused) if _active is not None else []


class ReadOnly(RuntimeError):
    """A change tried in a read-only job, refused before it was made."""


# Refused while an action is open, from any session : each
# would undo, redo or replace steps the action counts on, or (the last four,
# a review) undo several, clear the undo list or stop recording.
# Saving is allowed. The server refuses the same names before sending
# (risky.py).
ACTION_REFUSED = ("app.undo", "app.redo", "scene.new", "scene.open",
                  "scene.close", "scene.revert", "cmds.undoTo",
                  "app.clearUndos", "app.undoSuspend", "app.undoLockout")


class InAction(errors.OpError):
    """A command an open action can't survive (action.REFUSED), refused
    before it was sent."""

    def __init__(self, name, label):
        errors.OpError.__init__(
            self, protocol.IN_ACTION,
            "refused %s: action '%s' is open, and %s would undo, redo or "
            "replace its steps" % (name, label, name),
            hint="Nothing was sent to Modo. Commit or roll back first "
                 "(mt_commit or mt_rollback), then call again.")


def is_query(command):
    """Whether a command line only asks: `query ...`, or an argument of ?."""
    text = str(command).strip().lstrip("!+").strip()
    if text.lower().startswith("query "):
        return True
    return any(word == "?" or word.endswith(":?") for word in text.split()[1:])


def _words(line):
    """(start, end) of each argument of a command line, after its name. A
    value in "..." or {...} is one word, and so is name:"..." (Modo's
    quoting)."""
    n, i = len(line), 0
    while i < n and (line[i].isspace() or line[i] in "!+"):
        i += 1
    while i < n and not line[i].isspace():
        i += 1
    while True:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            return
        start = i
        while i < n and not line[i].isspace():
            if line[i] == '"':
                close = line.find('"', i + 1)
                i = n if close < 0 else close + 1
            elif line[i] == "{":
                depth = 0
                while i < n:
                    depth += {"{": 1, "}": -1}.get(line[i], 0)
                    i += 1
                    if depth == 0:
                        break
            else:
                i += 1
        yield start, i


_ARG_NAME = re.compile(r"^[A-Za-z_][\w.]*:")


def operator(command):
    """(start, end) of the line's query operator, or None. An
    operator is an argument that starts with ? and goes on: ?+2, ?*3, ?+,
    ?+>, ?min, ?max, ?(a|b) (a Modo scripting guide). A bare ? is a
    query, and a ? inside a quoted value is text."""
    found = operators(command)
    return found[0] if found else None


def operators(command):
    """(start, end) of each query operator in the line. Only one can be read
    back, so a line with two keeps its line as sent."""
    line, found = str(command), []
    for start, end in _words(line):
        name = _ARG_NAME.match(line[start:end])
        if name:
            start += name.end()
        if line.startswith("?", start) and end - start > 1:
            found.append((start, end))
    return found


def placed(line, names):
    """[(argument index, (start, end) of its value, read)] for each word of
    a command line, as Modo 17.1 places them (2026-11-10).
    A named word goes to its argument. Words with no name fill the
    arguments in order, up to the first named word. After a named word, a
    word with no name is dropped: `select.symmetry 0 uvActive:0 u` left
    uvAxis as it was. Such a word has read False; its index is the argument
    after the word before it, which is the one a query there asks
    (`select.symmetry 0 uvActive:0 ?` answers uvAxis). A word past the last
    argument isn't listed. A word whose name isn't an argument is a value
    with a colon in it (C:/scene.lxo)."""
    names = list(names)
    out, at, named = [], 0, False
    for start, end in _words(line):
        match = _ARG_NAME.match(line[start:end])
        if match and match.group(0)[:-1] in names:
            at = names.index(match.group(0)[:-1])
            out.append((at, (start + match.end(), end), True))
            named = True
        elif at < len(names):
            out.append((at, (start, end),
                        not named or line.startswith("?", start)))
        at += 1
    return out


def unread(line):
    """The words of a command line Modo 17.1 drops: each word with no name
    after a named one (see placed). [] when the command can't be spawned
    (tool.attr), and for a line with no such word, without spawning. Never
    raises."""
    line = str(line).lstrip("!+ ")
    named = False
    for start, end in _words(line):
        if _ARG_NAME.match(line[start:end]):
            named = True
        elif named:
            break
    else:
        return []
    try:
        from . import ops_lookup
        attrs, _ui, _variable = ops_lookup.spawned_attributes(command_name(line))
        if attrs is None:
            return []
        names = [attrs.Name(i) for i in range(attrs.Count())]
        return [line[start:end] for _at, (start, end), read
                in placed(line, names) if not read]
    except Exception:
        return []


def arg_type(line, span):
    """The type Modo gives the argument at span of line, such as "angle",
    or None when the command can't be spawned to ask (tool.attr can't)."""
    import lx
    spawned = lx.service.Command().SpawnFromString(str(line).lstrip("!+ "))
    attrs = lx.object.Attributes(
        spawned[-1] if isinstance(spawned, tuple) else spawned)
    names = [attrs.Name(i) for i in range(attrs.Count())]
    for index, (start, end), _read in placed(line, names):
        if start <= span[0] < end:
            return attrs.TypeName(index)
    return None


def typed(value, line, span):
    """The value read back as the argument's text. On Modo 17.1
    (2026-11-08): an angle is read back in radians but a number in a command
    line is degrees, so transform.channel rot.Y 0.52 is 0.52 degrees. When
    the type can't be asked, the value as it is."""
    if isinstance(value, (list, tuple)) and value and all(
            v == value[0] for v in value):
        value = value[0]
    if isinstance(value, float):
        try:
            if arg_type(with_word(line, span, repr(value)), span) == "angle":
                return math.degrees(value)
        except Exception:
            pass
    return value


def _hinted(line):
    """(Attributes, [(argument, span)]) for the arguments of a command line,
    or None when the command can't be spawned. tool.attr's value is the
    tool's attribute, read from the tool."""
    from . import ops_lookup
    import lx
    name = command_name(line)
    words = list(_words(line))
    named, positional = {}, []
    for start, end in words:
        match = _ARG_NAME.match(line[start:end])
        if match:
            named[match.group(0)[:-1]] = (start + match.end(), end)
        else:
            positional.append((start, end))
    if name == "tool.attr":
        spans = [named.get(k) or (positional[i] if i < len(positional) else None)
                 for i, k in enumerate(("tool", "attr", "value"))]
        if None in spans:
            return None
        tool, attr = (_bare(line[s:e]) for s, e in spans[:2])
        if not ops_lookup.is_tool(tool):
            return None
        attrs = lx.object.Attributes(lx.service.Host().LookupServer(
            lx.symbol.u_TOOL, tool, 0).Spawn())
        return attrs, [(attr, spans[2])]
    attrs, _ui, variable = ops_lookup.spawned_attributes(name)
    if attrs is None:
        return None
    # A variable argument's hints are never read: on 17.1 that read hangs
    # Modo for tool.attr's value.
    names = [attrs.Name(i) for i in range(attrs.Count())]
    return attrs, [(names[index], span)
                   for index, span, read in placed(line, names)
                   if read and index not in variable]


def _bare(word):
    """A word without Modo's {...} or "..." around it."""
    if len(word) > 1 and word[0] + word[-1] in ("{}", '""'):
        return word[1:-1]
    return word


def strays(line):
    """[(argument, word, span, {word: integer})] for each argument of the
    line given a word that isn't one of its text hints. On 17.1
    Modo matches a hint word without case and takes an integer as it is;
    any other word is taken, with no word, as the first value (frt became
    top). Only text hints count: a popup's words may be cut, or open-ended.
    A query or an operator (?...) is left alone. Never raises."""
    from . import ops_lookup
    try:
        found = _hinted(str(line).lstrip("!+ "))
    except Exception:
        return []
    if not found:
        return []
    attrs, args = found
    text = str(line).lstrip("!+ ")
    out = []
    for arg, span in args:
        word = _bare(text[span[0]:span[1]])
        if not word or word.startswith("?"):
            continue
        try:
            known, _total = ops_lookup.values(attrs, None, attrs.Lookup(arg))
        except Exception:
            continue
        if not isinstance(known, dict):
            continue
        if word.lower() in (w.lower() for w in known):
            continue
        try:
            if int(word) in known.values():
                continue
        except ValueError:
            pass
        out.append((arg, word, span, known))
    return out


def taken(line, arg, known):
    """The value Modo's parser gives the line's argument `arg`, as its hint
    word (known: {word: integer}), or None when the line can't be spawned.
    On Modo 17.1 (2026-11-10): a line spawned, not run, holds each
    word as the integer Modo took it as, so an argument that can't be asked
    with ? can still be read: view3d.shadingStyle shade inactve holds
    bgmesh 0, active. Never raises."""
    import lx
    try:
        spawned = lx.service.Command().SpawnFromString(str(line).lstrip("!+ "))
        attrs = lx.object.Attributes(
            spawned[-1] if isinstance(spawned, tuple) else spawned)
        number = int(attrs.GetInt(attrs.Lookup(arg)))
    except Exception:
        return None
    for word, value in known.items():
        if value == number:
            return word
    return str(number)


def with_word(command, span, word):
    """The line with the word at span replaced."""
    return command[:span[0]] + word + command[span[1]:]


def as_argument(value):
    """A value read back, written as the argument Modo would read, or None
    when it can't be one. A probe: with two items selected the query answers one
    value per item, the same for each after an operator set them."""
    if isinstance(value, (list, tuple)):
        if not value or any(v != value[0] for v in value):
            return None
        value = value[0]
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if re.match(r'^[^\s"{}]+$', text):
        return text
    if not re.search(r'["{}]', text):
        return "{%s}" % text
    return None


def _refuse_change(what):
    raise ReadOnly(
        "read_only: %s would change the scene, so it wasn't run" % what)


# The modo module's writes a read-only job can reach without a command:
# (module path, class, method). Checked on 17.1, 2026-11-08.
_MODO_WRITES = (("channel", "Channel", "set"),
                (None, "MeshGeometry", "setMeshEdits"))

# The modo writes an exec job's trace records as untraced. A probe
# (2026-11-08): on 17.1 none of these sends a command, a selection included,
# so a replay of the trace alone would miss them. Those that exist are
# wrapped; the rest of the modo module, and lx.object, lx.service and lxu,
# can't be seen this way (journal.untraced reads the code for those).
_MODO_RECORDED = _MODO_WRITES + (
    (None, "Scene", "addMesh"), (None, "Scene", "addItem"),
    (None, "Scene", "removeItems"), (None, "Scene", "select"),
    (None, "Scene", "deselect"), (None, "Item", "select"),
    (None, "Item", "deselect"), (None, "Item", "setParent"),
    (None, "MeshPolygon", "select"), (None, "MeshVertex", "select"),
    (None, "MeshEdge", "select"))


def command_line(name, kwargs):
    """lx.command's call as the line Modo would read, for the trace."""
    return suppress(name) + "".join(" %s:{%s}" % (k, v)
                                    for k, v in kwargs.items())


class Guard(object):
    """Patch lx for the length of one job.

    read_only: refuse every command that isn't a query, and the modo
    module's channel and mesh writes. A read-only job runs through
    modoTools.execRead, and on 17.1 Modo lets an edit through there
    with no undo step (2026-11-08), so it is refused here instead.
    """

    def __init__(self, read_only=False, action=None, refuse=(), trace=False):
        self.read_only = read_only
        # Keep the trace (commands, untraced) for the journal.
        self.trace = trace and not read_only
        self.commands = []
        self.untraced = []
        # Lines that raised: kept apart, so a replay of commands doesn't run
        # a line that failed and that the code caught.
        self.failed = []
        # Each line with a query operator, as sent, and whether
        # the value it set was read back into commands.
        self.operators = []
        # Commands of ours running now: a modo write or an lx.eval inside
        # one (a command written in Python) is covered by its line.
        self._sending = 0
        # While action (its label) is open, the names in refuse.
        self.action = action
        self.refuse = frozenset(refuse) if action else frozenset()
        self._patched = []

    def __enter__(self):
        global _active
        try:
            import lx
        except ImportError:
            self._lx = None
            return self
        self._lx = lx
        self.refused = []
        self._saved = {n: getattr(lx, n) for n in ("eval", "eval1", "evalN",
                                                   "command") if hasattr(lx, n)}
        real_evalN = self._saved.get("evalN")
        if real_evalN is None:
            real_eval = self._saved["eval"]

            def real_evalN(text):
                value = real_eval(text)
                if value is None:
                    return []
                return list(value) if isinstance(value, (list, tuple)) else [value]
        self.real_evalN = real_evalN

        read_only, trace = self.read_only, self.trace
        refuse, label = self.refuse, self.action

        def refuse_in_action(command):
            name = command_name(command)
            if name in refuse:
                refused = InAction(name, label)
                self.refused.append(refused)
                raise refused

        def wrap(real):
            def guarded(command, *args, **kwargs):
                check(command)
                if refuse and not is_query(command):
                    refuse_in_action(command)
                if read_only and not is_query(command):
                    _refuse_change(command_name(command))
                line = (suppress(command)
                        if trace and not is_query(command) else None)
                spans = operators(line) if line is not None else []
                resolve = None
                if spans:
                    span = spans[0]

                    def resolve():
                        if len(spans) > 1:
                            return None
                        value = self._ask(with_word(line, span, "?"))
                        word = as_argument(typed(value, line, span))
                        return None if word is None else with_word(line, span, word)
                return self._send(line, resolve, real, suppress(command),
                                  *args, **kwargs)
            guarded.modo_tools_guard = True
            return guarded

        def wrap_command(real):
            # The command is positional only: item.channel has an argument
            # called name, and it has to reach Modo as a keyword.
            def guarded(*args, **kwargs):
                if refuse and "?" not in kwargs.values():
                    refuse_in_action(args[0])
                if read_only and "?" not in kwargs.values():
                    _refuse_change(command_name(args[0]))
                line = (command_line(args[0], kwargs)
                        if trace and "?" not in kwargs.values() else None)
                keys = [k for k, v in kwargs.items() if isinstance(v, str)
                        and v.startswith("?") and len(v) > 1]
                resolve = None
                if line is not None and keys:
                    key = keys[0]

                    def resolve():
                        if len(keys) > 1:
                            return None
                        asked = dict(kwargs)
                        asked[key] = "?"
                        query = command_line(args[0], asked).replace(
                            " %s:{?}" % key, " %s:?" % key)
                        value = self._ask(query)
                        at = query.index(" %s:?" % key) + len(key) + 2
                        word = as_argument(typed(value, query, (at, at + 1)))
                        if word is None:
                            return None
                        if word.startswith("{"):
                            word = word[1:-1]
                        asked[key] = word
                        return command_line(args[0], asked)
                return self._send(line, resolve, real, suppress(args[0]),
                                  *args[1:], **kwargs)
            guarded.modo_tools_guard = True
            return guarded

        for name, real in self._saved.items():
            setattr(lx, name, wrap_command(real) if name == "command" else wrap(real))
        if read_only:
            self._patch_modo_writes(_MODO_WRITES, None)
        elif self.trace:
            self._patch_modo_writes(_MODO_RECORDED, self.untraced)
        _active = self
        return self

    def _send(self, line, resolve, real, *args, **kwargs):
        """Send, and keep line (None for none) in the trace: in commands once
        it returned, in failed if it raised. Only the job's own lines: one
        sent from inside another command is covered by that command's.
        resolve, for a line with a query operator: () -> the line with the
        value read back in place of the operator, or None."""
        line = line if not self._sending else None
        self._sending += 1
        try:
            result = real(*args, **kwargs)
        except BaseException:
            if line is not None:
                self.failed.append(line)
            raise
        finally:
            self._sending -= 1
        if line is not None:
            self.commands.append(line)
            if resolve is not None:
                resolved = resolve()
                self.operators.append({"i": len(self.commands) - 1,
                                       "sent": line,
                                       "read": resolved is not None})
                if resolved is not None:
                    self.commands[-1] = resolved
        return result

    def _ask(self, query):
        """The value a query line answers, or None when it can't be asked
        (the read-back). A query that would crash Modo is refused as
        any is; a query adds no undo step. Never raises."""
        try:
            check(query)
            return self.real_evalN(suppress(query))
        except Exception:
            return None

    def _patch_modo_writes(self, writes, record):
        """Refuse each write (record None), or note it in record and let it
        through."""
        try:
            import modo
        except ImportError:
            return
        for module, cls_name, method in writes:
            owner = getattr(modo, module, None) if module else modo
            cls = getattr(owner, cls_name, None)
            real = getattr(cls, method, None) if isinstance(cls, type) else None
            if real is None:
                continue
            # An inherited method is wrapped on this class and deleted after,
            # so the base class is never touched.
            own = method in cls.__dict__
            what = "%s.%s" % (cls_name, method)
            if record is None:
                wrapped = (lambda *a, _what=what + "()", **k:
                           _refuse_change(_what))
            else:
                def wrapped(*a, _what=what, _real=real, **k):
                    if not self._sending and _what not in record:
                        record.append(_what)
                    return _real(*a, **k)
            setattr(cls, method, wrapped)
            self._patched.append((cls, method, real if own else None))

    def __exit__(self, exc_type, exc, tb):
        global _active
        _active = None
        if self._lx is not None:
            for name, real in self._saved.items():
                setattr(self._lx, name, real)
        for cls, method, real in reversed(self._patched):
            if real is None:
                delattr(cls, method)
            else:
                setattr(cls, method, real)
        self._patched = []
        return False
