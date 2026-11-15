# python
"""The menu's remembered options, as flag files in ~/.modo_tools.

A file, not a Modo user value: creating a user value opens a dialog on some
Modo builds, and a dialog blocks the main thread the kit runs on. The idea is
modo-mcp's settings.py; the code is ours.

Start with Modo is on only when ~/.modo_tools/autostart.on is a file that
says "on". Anything else (no file, other text, a folder of that name, a file
that can't be read) is off: something that runs code shouldn't come up
uninvited because a file got mangled.
"""

import os
import subprocess
import sys

from . import protocol

AUTOSTART_TEXT = "on"


def autostart_path():
    return os.path.join(protocol.data_dir(), "autostart.on")


def autostart_state():
    """(on, problem): problem is None for a missing or good flag, else why a
    flag that is there doesn't count."""
    path = autostart_path()
    if not os.path.exists(path):
        return False, None
    if not os.path.isfile(path):
        return False, "%s is not a file" % path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read(64).strip()
    except (OSError, IOError, ValueError) as exc:
        return False, "%s can't be read: %s" % (path, exc)
    if text != AUTOSTART_TEXT:
        return False, "%s should say %r" % (path, AUTOSTART_TEXT)
    return True, None


def autostart_enabled():
    return autostart_state()[0]


def set_autostart(enabled):
    """Write or remove the flag. Raises OSError if it can't."""
    path = autostart_path()
    if enabled:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        if os.path.isdir(path):
            os.rmdir(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(AUTOSTART_TEXT + "\n")
    elif os.path.isdir(path):
        os.rmdir(path)
    elif os.path.exists(path):
        os.remove(path)


def open_with_system(path):
    """Show a folder in Explorer (Finder, a file manager)."""
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_data_folder():
    folder = protocol.data_dir()
    if not os.path.isdir(folder):
        os.makedirs(folder)
    open_with_system(folder)
    return folder


def oplog_tail(count=15):
    """The op log's last lines, or None when there is no op log yet."""
    try:
        with open(protocol.oplog_path(), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, IOError):
        return None
    return lines[-count:]
