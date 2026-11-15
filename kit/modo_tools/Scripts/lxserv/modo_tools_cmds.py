# python
# Parts adapted from modo-mcp 0.3.0, Copyright (c) 2026 Leonard Billeke,
# under the MIT License; see THIRD_PARTY_NOTICES.md.
"""The modoTools.* commands.

    modoTools.start [port:N]   start listening (15680 unless port is given)
    modoTools.stop             stop listening
    modoTools.status           write the kit's state to the event log
    modoTools.exec             run one queued job; fired by the idle pump
    modoTools.execRead         the same, for a read: adds no undo entry
    modoTools.pump             run the queue by hand, when the idle pump is off
    modoTools.running ?        the menu's Running checkbox: query, or start/stop
    modoTools.autostart ?      the menu's Start with Modo checkbox (off by default)
    modoTools.boot             run by Modo at start-up; starts the kit if
                               Start with Modo is on
    modoTools.opLog [count:N]  the op log's last lines, in the event log
    modoTools.folder           open ~/.modo_tools

Modo imports this file while it scans the kit at start-up. Large imports at
that moment have crashed Modo on Windows, so it only locates the modotools
package here; the import waits for the first command.

The exec / execRead flags and the package lookup follow modo-mcp's
mcp_bridge_cmds.py (see the note at the top).
"""

import importlib.util
import os
import sys

import lx
import lxu.command

_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON_MODULES = os.path.normpath(os.path.join(_HERE, "..", "python_modules"))
if os.path.isdir(_PYTHON_MODULES) and _PYTHON_MODULES not in sys.path:
    sys.path.append(_PYTHON_MODULES)

# Located, not imported: find_spec doesn't run the package.
_PACKAGE = None
for _candidate in ("modotools", "python_modules.modotools"):
    try:
        if importlib.util.find_spec(_candidate) is not None:
            _PACKAGE = _candidate
            break
    except (ImportError, ValueError):
        continue


def kit():
    """The modotools package, imported on first use."""
    if _PACKAGE is None:
        raise RuntimeError(
            "modo-tools: the modotools package isn't installed next to this "
            "file (expected %s). Reinstall the kit." % _PYTHON_MODULES)
    return importlib.import_module(_PACKAGE)


def log(message):
    try:
        lx.out(message)
    except Exception:
        print(message)


def settings():
    kit()
    return importlib.import_module(_PACKAGE + ".settings")


def start_kit(port=None):
    """Start (or restart) the kit and say so. Raises on failure."""
    running = kit().start(port=port, log=log)
    log("modo-tools ready on %d (pump: %s, last session: %s)"
        % (running.port, running.pump.strategy_name, running.last_session))
    log("modo-tools: connected AI clients can run code in Modo as you. "
        "Save your work, and run modoTools.stop when done.")
    return running


class CmdStart(lxu.command.BasicCommand):
    def __init__(self):
        lxu.command.BasicCommand.__init__(self)
        self.dyna_Add("port", lx.symbol.sTYPE_INTEGER)
        self.basic_SetFlags(0, lx.symbol.fCMDARG_OPTIONAL)

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        port = self.dyna_Int(0) if self.dyna_IsSet(0) else None
        try:
            start_kit(port)
        except Exception as exc:
            log("modo-tools: could not start: %s" % exc)


class CmdStop(lxu.command.BasicCommand):
    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        log("modo-tools: stopped" if kit().stop()
            else "modo-tools: was not running")


class CmdStatus(lxu.command.BasicCommand):
    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        state = kit().status()
        log("modo-tools status: " + ", ".join(
            "%s=%s" % (k, v) for k, v in sorted(state.items())))


class CmdExec(lxu.command.BasicCommand):
    """One job that may change the scene. Model and undo flags, because Modo
    only allows geometry edits inside a command that declares them."""

    def cmd_Flags(self):
        return lx.symbol.fCMD_MODEL | lx.symbol.fCMD_UNDO

    def basic_Execute(self, msg, flags):
        kit().pump_once(max_jobs=1)


class CmdExecRead(lxu.command.BasicCommand):
    """One read. UI only, so reading adds no entry to the undo stack."""

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        kit().pump_once(max_jobs=1)


class CmdPump(lxu.command.BasicCommand):
    """The manual pump: bind to a key when the idle callback can't be armed."""

    def cmd_Flags(self):
        return lx.symbol.fCMD_MODEL | lx.symbol.fCMD_UNDO

    def basic_Execute(self, msg, flags):
        count = kit().pump_once(max_jobs=None)
        if count:
            log("modo-tools: ran %d queued job(s)" % count)


class _Toggle(lxu.command.BasicCommand):
    """A checkbox in the menu: `name ?` answers on or off, `name true|false`
    sets it, and a bare `name` flips it."""

    LABEL = "option"

    def __init__(self):
        lxu.command.BasicCommand.__init__(self)
        self.dyna_Add("state", lx.symbol.sTYPE_BOOLEAN)
        self.basic_SetFlags(0, lx.symbol.fCMDARG_OPTIONAL
                            | lx.symbol.fCMDARG_QUERY)

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def is_on(self):
        raise NotImplementedError

    def set_on(self, on):
        raise NotImplementedError

    def basic_Execute(self, msg, flags):
        target = self.dyna_Bool(0) if self.dyna_IsSet(0) else not self.is_on()
        try:
            self.set_on(bool(target))
        except Exception as exc:
            log("modo-tools: could not switch %s: %s" % (self.LABEL, exc))
            return
        log("modo-tools: %s %s" % (self.LABEL, "on" if self.is_on() else "off"))

    def cmd_Query(self, index, vaQuery):
        # A query must answer even when the kit can't load: Modo asks every
        # time it draws the menu.
        try:
            if index == 0:
                values = lx.object.ValueArray()
                values.set(vaQuery)
                values.AddInt(1 if self.is_on() else 0)
        except Exception:
            pass
        return lx.result.OK


class CmdRunning(_Toggle):
    """The Running checkbox: shows whether the kit listens, starts or stops it."""

    LABEL = "running"

    def is_on(self):
        return kit().running() is not None

    def set_on(self, on):
        if on and not self.is_on():
            start_kit()
        elif not on:
            kit().stop()


class CmdAutostart(_Toggle):
    """The Start with Modo checkbox. Off by default."""

    LABEL = "start with Modo"

    def is_on(self):
        return settings().autostart_enabled()

    def set_on(self, on):
        settings().set_autostart(on)


class CmdBoot(lxu.command.BasicCommand):
    """Run once by Modo at start-up (StartupCommands in the kit's config),
    after the kit scan, on the main thread. Never raises: a failure here must
    not disturb Modo's start-up."""

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        try:
            on, problem = settings().autostart_state()
            if problem:
                log("modo-tools: not started with Modo: %s. Tick Start with "
                    "Modo in the Modo Tools menu to fix it." % problem)
            if not on or kit().running() is not None:
                return
            running = start_kit()
            log("modo-tools: started with Modo on %d" % running.port)
        except Exception as exc:
            log("modo-tools: could not start with Modo: %s. Use Modo Tools > "
                "Start." % exc)


class CmdOpLog(lxu.command.BasicCommand):
    """The op log's last lines in the event log: after a crash, the last
    op_start without an op_ok is what Modo was doing."""

    def __init__(self):
        lxu.command.BasicCommand.__init__(self)
        self.dyna_Add("count", lx.symbol.sTYPE_INTEGER)
        self.basic_SetFlags(0, lx.symbol.fCMDARG_OPTIONAL)

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        count = self.dyna_Int(0) if self.dyna_IsSet(0) else 15
        lines = settings().oplog_tail(max(1, min(count, 200)))
        if lines is None:
            log("modo-tools: no op log yet; the kit writes it once started")
            return
        for line in lines:
            log("modo-tools op log: " + line)


class CmdFolder(lxu.command.BasicCommand):
    """Open ~/.modo_tools: the token, the op log and the flag files."""

    def cmd_Flags(self):
        return lx.symbol.fCMD_UI

    def basic_Execute(self, msg, flags):
        try:
            settings().open_data_folder()
        except Exception as exc:
            log("modo-tools: could not open the data folder: %s" % exc)


# One dot in each name, as Modo expects. Each name blessed exactly once.
lx.bless(CmdStart, "modoTools.start")
lx.bless(CmdStop, "modoTools.stop")
lx.bless(CmdStatus, "modoTools.status")
lx.bless(CmdExec, "modoTools.exec")
lx.bless(CmdExecRead, "modoTools.execRead")
lx.bless(CmdPump, "modoTools.pump")
lx.bless(CmdRunning, "modoTools.running")
lx.bless(CmdAutostart, "modoTools.autostart")
lx.bless(CmdBoot, "modoTools.boot")
lx.bless(CmdOpLog, "modoTools.opLog")
lx.bless(CmdFolder, "modoTools.folder")
