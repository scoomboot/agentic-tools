"""Modo's windows, seen from outside: is a dialog open, is Modo hung?

A dialog that opens inside a job holds Python in Modo until it closes, so the
kit can't say so itself: its pings go silent, as in a freeze. Windows can
tell the two apart. A modal dialog disables Modo's main window and is a
visible window Modo owns; a freeze leaves the main window Not Responding.
Seen on 17.1 (2026-11-08): the dialog is a Qt631QWindowIcon window owned by
the main one, the main window is disabled and not hung, and WM_CLOSE on the
dialog cancels it and lets the job go on.

Only reads windows and posts WM_CLOSE; never touches Modo's memory.
Elsewhere than Windows, find() returns None: can't tell.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable

WM_CLOSE = 0x0010
GW_OWNER = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class Dialog:
    title: str
    close: Callable[[], bool]


@dataclass
class ModoWindows:
    """What Modo's windows show. alive False: no Modo process has a window.
    pids: the Modo processes with a main window; empty when not known."""
    alive: bool
    hung: bool = False
    dialogs: list[Dialog] = field(default_factory=list)
    pids: set[int] = field(default_factory=set)


def find() -> ModoWindows | None:
    if not sys.platform.startswith("win"):
        return None
    try:
        return _find()
    except (OSError, AttributeError, ValueError):
        return None


def _find() -> ModoWindows:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
    kernel32.OpenProcess.restype = wintypes.HANDLE

    names: dict[int, str] = {}

    def image(pid: int) -> str:
        if pid not in names:
            name = ""
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                          False, pid)
            if handle:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                       ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
                kernel32.CloseHandle(handle)
            names[pid] = name
        return names[pid]

    def title(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    windows: list[tuple[int, int, int]] = []  # hwnd, owner, pid

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _param):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if image(pid.value) == "modo.exe":
                windows.append((hwnd, user32.GetWindow(hwnd, GW_OWNER) or 0,
                                pid.value))
        return True

    user32.EnumWindows(each, 0)
    mains = [w for w in windows if not w[1]]
    if not mains:
        return ModoWindows(alive=False)
    hung = any(user32.IsHungAppWindow(w[0]) for w in mains)
    # A modal dialog disables the window it belongs to.
    held = [w for w in mains if not user32.IsWindowEnabled(w[0])]
    dialogs = []
    for hwnd, owner, pid in windows:
        if owner and user32.IsWindowEnabled(hwnd) and any(
                m[2] == pid for m in held):
            dialogs.append(Dialog(
                title(hwnd) or "(no title)",
                lambda h=hwnd: bool(user32.PostMessageW(h, WM_CLOSE, 0, 0))))
    return ModoWindows(alive=True, hung=hung, dialogs=dialogs,
                       pids={w[2] for w in mains})
