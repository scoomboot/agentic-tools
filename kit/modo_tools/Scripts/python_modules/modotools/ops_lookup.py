# python
"""command_info and find_commands: a command's real arguments, from the live
command service, or a tool's attributes, from the host service.

Modo's API is thin in any model's training data, so an agent that can look a
command up before running it guesses less. Every per-command query here is
for a name already in Modo's command list (the guard refuses the rest; on
17.1 an unknown name crashes Modo).

Query forms and argument flag bits as modo-mcp verified them on 17.1; the
code is ours.
"""

from . import errors, guard, protocol
from .jobs import op

# command.argFlags bits (lx.symbol.fCMDARG_*).
OPTIONAL, QUERY, READONLY, VARIABLE, HIDDEN = 1, 2, 4, 8, 64

TOOL_USAGE = ("tool.set {0} on", "tool.reset {0}",
              "tool.attr {0} <attribute> <value>", "tool.doApply",
              "tool.set {0} off")

_tools = []


def tool_names():
    """Every modelling tool (poly.bevel, prim.cube ...). Tools aren't
    commands and aren't in the command list; the host service lists them."""
    global _tools
    if _tools:
        return _tools
    import lx
    found = []
    try:
        host = lx.service.Host()
        for index in range(host.NumServers(lx.symbol.u_TOOL)):
            try:
                found.append(str(host.ServerByIndex(lx.symbol.u_TOOL, index).Name()))
            except Exception:
                continue
    except Exception:
        return []
    _tools = sorted(found)
    return _tools


def is_tool(name):
    return name in tool_names()


def tool_hint(name):
    return ("%r is a tool, not a command. Use: %s. mt_command_info(%r) lists "
            "its attributes." % (name, ", then ".join(
                u.format(name) for u in TOOL_USAGE), name))


def _query(attribute, name):
    import lx
    try:
        values = lx.evalN("query commandservice %s ? {%s}" % (attribute, name))
    except errors.OpError:
        raise
    except Exception:
        return []
    if values is None:
        return []
    return list(values) if isinstance(values, (list, tuple)) else [values]


def _first(values):
    return values[0] if values else None


# An argument's words. A popup can be long (item.create's type
# lists ~1000 item types on 17.1), so a list stops here and says its length.
VALUES_MAX = 100


def values(attrs, ui, index):
    """The words argument `index` takes, from Modo's own hints (probed on
    17.1, 2026-11-10): its text hints as {word: integer}, else its popup's
    words as a list; with their full count when the list was cut. None when
    it has neither. Modo's hint list ends with "=set-name", which names the
    set and isn't a word; "&min", "&max", "%min", "%maxDegrees" and the like
    are an argument's bounds, not words either (45 arguments
    and 39 tool attributes on 17.1, clip.newSequence's width among them). Never call this for a
    variable argument or a command in POPUP_UNSAFE: see spawned_attributes."""
    hints = [(int(n), str(w)) for n, w in (attrs.Hints(index) or [])
             if str(w)[:1].isalnum()]
    if hints:
        return {w: n for n, w in hints}, None
    if ui is None:
        return None, None
    import lx
    popup = lx.object.UIValueHints(ui.UIValueHints(index))
    count = popup.PopCount()
    words = [str(popup.PopInternalName(k)) for k in range(min(count, VALUES_MAX))]
    if not words:
        return None, None
    return words, (count if count > VALUES_MAX else None)


def _add_values(entry, attrs, ui, index):
    # A hint read that raises leaves values out for that argument only: the
    # rest of the answer is still right (the gate's row 4).
    try:
        found, total = values(attrs, ui, index)
    except Exception:
        return
    if found:
        entry["values"] = found
        if total:
            entry["values_total"] = total


# On Modo 17.1 (2026-11-10) read what values() reads for all 3365
# commands. Three reads Modo doesn't survive, and no try can catch them:
# * Hints on a variable argument (one with no type until other arguments
#   are given): tool.attr's value opens a dialog and Modo never comes back.
#   No variable argument is ever read.
# * A popup is the command's own code, run with no arguments set.
#   scene.tagAdd's taglist crashed Modo in PopCount, twice;
#   octane.selectOcioColorspaceWhenSaving's opened its own error boxes and
#   Modo's "Item Channel Value" dialog. These commands' popups are never
#   read. A kit installed later may add another: sweep it first
#   (tools/lookup_probe/sweep.py).
POPUP_UNSAFE = ("scene.tagAdd", "octane.selectOcioColorspaceWhenSaving")


def spawned_attributes(name):
    """(Attributes, AttributesUI, variable) of the command `name`, spawned
    with no arguments, or (None, None, ()) when it can't be. variable: the
    indices of the arguments values() must not be called for; an argument
    whose flags can't be read counts as one. AttributesUI is None for a
    command in POPUP_UNSAFE, so its popups aren't read."""
    import lx
    try:
        spawned = lx.service.Command().SpawnFromString(name)
        obj = spawned[-1] if isinstance(spawned, tuple) else spawned
        attrs = lx.object.Attributes(obj)
        command = lx.object.Command(obj)
        variable = set()
        for index in range(attrs.Count()):
            try:
                if command.ArgFlags(index) & VARIABLE:
                    variable.add(index)
            except Exception:
                variable.add(index)
        ui = None if name in POPUP_UNSAFE else lx.object.AttributesUI(obj)
        return attrs, ui, variable
    except Exception:
        return None, None, ()


def describe_command(name):
    info = {
        "kind": "command",
        "name": name,
        "username": _first(_query("command.username", name)),
        "description": _first(_query("command.desc", name)) or "",
        "arguments": [],
    }
    arg_names = [str(n) for n in _query("command.argNames", name)]
    usernames = _query("command.argUsernames", name)
    types = _query("command.argTypeNames", name)
    flags = _query("command.argFlags", name)
    attrs, ui, variable = spawned_attributes(name)
    for index, arg in enumerate(arg_names):
        entry = {"name": arg}
        if index < len(usernames) and usernames[index] not in (None, "", arg):
            entry["username"] = usernames[index]
        if index < len(types) and types[index]:
            entry["type"] = types[index]
        try:
            bits = int(flags[index])
        except (IndexError, TypeError, ValueError):
            bits = None
        if bits is not None:
            entry["optional"] = bool(bits & OPTIONAL)
            for key, bit in (("query", QUERY), ("readonly", READONLY),
                             ("variable", VARIABLE), ("hidden", HIDDEN)):
                if bits & bit:
                    entry[key] = True
        if attrs is not None:
            try:
                at = attrs.Lookup(arg)
            except Exception:
                at = None
            if at is not None and at not in variable:
                _add_values(entry, attrs, ui, at)
        info["arguments"].append(entry)
    return info


def _default(attrs, index):
    import lx
    try:
        kind = attrs.Type(index)
        if kind == lx.symbol.i_TYPE_INTEGER:
            return attrs.GetInt(index)
        if kind == lx.symbol.i_TYPE_FLOAT:
            return attrs.GetFlt(index)
        if kind == lx.symbol.i_TYPE_STRING:
            return attrs.GetString(index)
    except Exception:
        pass
    return None


def describe_tool(name):
    import lx
    info = {"kind": "tool", "name": name, "attributes": [],
            "usage": [u.format(name) for u in TOOL_USAGE],
            "note": "A modelling tool, not a command. Distances are in "
                    "metres. Most tools act on the current selection."}
    factory = lx.service.Host().LookupServer(lx.symbol.u_TOOL, name, 0)
    try:
        username = factory.UserName()
        if username and username != name:
            info["username"] = username
    except Exception:
        pass
    tool = factory.Spawn()
    attrs = lx.object.Attributes(tool)
    try:
        ui = lx.object.AttributesUI(tool)
    except Exception:
        ui = None
    for index in range(attrs.Count()):
        entry = {"name": attrs.Name(index)}
        try:
            entry["type"] = attrs.TypeName(index)
        except Exception:
            pass
        default = _default(attrs, index)
        if default is not None:
            entry["default"] = default
        _add_values(entry, attrs, ui, index)
        info["attributes"].append(entry)
    return info


@op("command_info", read=True)
def command_info(payload, kit):
    name = guard.command_name(payload.get("name") or "")
    if not name:
        raise errors.OpError(protocol.BAD_REQUEST, "no name given",
                             hint="Pass a command or tool name, e.g. item.create.")
    if guard.exists(name):
        return describe_command(name)
    if is_tool(name):
        return describe_tool(name)
    near = guard.similar(name)
    tools = [t for t in tool_names() if name.split(".")[-1] in t][:5]
    hint = ""
    if near:
        hint += "Close commands: %s. " % ", ".join(near)
    if tools:
        hint += "Close tools: %s. " % ", ".join(tools)
    hint += "Search with mt_find_commands."
    raise errors.OpError(protocol.UNKNOWN_COMMAND,
                         "%r is neither a command nor a tool" % name, hint=hint)


@op("find_commands", read=True)
def find_commands(payload, kit):
    query = str(payload.get("query") or "").strip().lower()
    if not query:
        raise errors.OpError(protocol.BAD_REQUEST, "no query given",
                             hint="Pass part of a name, e.g. bevel.")
    try:
        limit = max(1, min(int(payload.get("limit", 25)), 200))
    except (TypeError, ValueError):
        limit = 25
    known = guard.names()
    exact = [n for n in known if n.lower() == query]
    prefix = [n for n in known if n.lower().startswith(query) and n not in exact]
    rest = [n for n in known if query in n.lower()
            and n not in exact and n not in prefix]
    found = exact + prefix + rest
    commands = []
    for name in found[:limit]:
        entry = {"name": name}
        username = _first(_query("command.username", name))
        if username:
            entry["username"] = username
        commands.append(entry)
    out = {"query": query, "total": len(found), "commands": commands}
    tools = [t for t in tool_names() if query in t.lower()]
    if tools:
        out["tools"] = tools[:limit]
        out["tools_note"] = ("Tools, not commands: tool.set <tool> on, "
                             "tool.attr, tool.doApply. mt_command_info lists "
                             "a tool's attributes.")
    if not found and not tools:
        out["similar"] = guard.similar(query, limit=10)
    return out


# The lx module is compiled (lxPython3.lx), so its classes can only
# be read here. On 17.1 every lx.object and lx.service method carries its
# signature as a docstring ("GetPixel(integer x,integer y,integer type,data[]
# pixel {})"): 354 object classes, 51 services, 4,240 methods, 5,144 symbols.
# The server saves the dump in ~/.modo_tools/docs/ for mt_search_docs.
def _classes(module):
    import inspect
    out = {}
    for name in dir(module):
        if name.startswith("_"):
            continue
        cls = getattr(module, name, None)
        if not inspect.isclass(cls):
            continue
        methods = {}
        for method in dir(cls):
            if method.startswith("_"):
                continue
            doc = getattr(getattr(cls, method, None), "__doc__", None)
            methods[method] = (doc or "").strip()
        out[name] = {"methods": methods}
    return out


@op("api_dump", read=True)
def api_dump(payload, kit):
    import lx
    symbols = {}
    for name in dir(lx.symbol):
        if name.startswith("_"):
            continue
        value = getattr(lx.symbol, name, None)
        if isinstance(value, (int, float, str)):
            symbols[name] = value
    return {"objects": _classes(lx.object), "services": _classes(lx.service),
            "symbols": symbols}
