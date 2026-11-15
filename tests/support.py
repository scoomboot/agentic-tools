"""Shared test setup: paths, a temp ~/.modo_tools, fake lx, the kit loaded the
way Modo loads it, and a plain socket client.

Run all tests from the repo root:  python -m unittest discover -s tests
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import types
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "kit", "modo_tools")
PYMODS = os.path.join(KIT, "Scripts", "python_modules")
PLUGIN = os.path.join(KIT, "Scripts", "lxserv", "modo_tools_cmds.py")
SERVER = os.path.join(ROOT, "server")

for path in (PYMODS, SERVER, os.path.dirname(os.path.abspath(__file__))):
    if path not in sys.path:
        sys.path.insert(0, path)

import fakelx  # noqa: E402


def temp_home(testcase):
    """Point MODO_TOOLS_HOME at a fresh folder for one test."""
    folder = tempfile.mkdtemp(prefix="modo_tools_test_")
    old = os.environ.get("MODO_TOOLS_HOME")
    os.environ["MODO_TOOLS_HOME"] = folder

    def restore():
        if old is None:
            os.environ.pop("MODO_TOOLS_HOME", None)
        else:
            os.environ["MODO_TOOLS_HOME"] = old
        shutil.rmtree(folder, ignore_errors=True)

    testcase.addCleanup(restore)
    # Modo may be running on the machine the tests run on: don't let its
    # windows into the watch. Tests that want windows pass their own.
    from modo_tools import window
    real_find = window.find
    window.find = lambda: None
    testcase.addCleanup(setattr, window, "find", real_find)
    return folder


def forget_kit():
    """Drop the imported modotools package, as a fresh Modo would have it."""
    for name in list(sys.modules):
        if name == "modotools" or name.startswith("modotools."):
            del sys.modules[name]


def index_cfg_imports():
    root = ET.parse(os.path.join(KIT, "index.cfg")).getroot()
    return [node.text.strip() for node in root.findall("import")]


def load_plugin():
    """Install fake lx and exec the lxserv file as Modo does at start-up.
    Returns (fake modo, plugin module)."""
    stop_running_kit()
    forget_kit()
    fake = fakelx.install()
    module = types.ModuleType("modo_tools_cmds")
    module.__file__ = PLUGIN
    with open(PLUGIN, "r", encoding="utf-8") as handle:
        source = handle.read()
    exec(compile(source, PLUGIN, "exec"), module.__dict__)
    return fake, module


def stop_running_kit():
    package = sys.modules.get("modotools")
    if package is not None:
        try:
            package.stop()
        except Exception:
            pass
    state = getattr(sys, "_modo_tools_state", None)
    if state and state.get("kit") is not None:
        try:
            state["kit"].listener.stop()
        except Exception:
            pass
        state["kit"] = None


def start_kit(testcase, port=0):
    """Fake Modo with the kit started on a free port. Returns (fake, kit)."""
    temp_home(testcase)
    fake, _module = load_plugin()
    fake.run_command("modoTools.start port:%d" % port)
    import modotools
    kit = modotools.running()
    testcase.assertIsNotNone(kit, "\n".join(fake.event_log))
    testcase.addCleanup(stop_running_kit)
    return fake, kit


def pump_until(fake, done, timeout=5.0):
    """Tick Modo's idle callback from this (the main) thread until done()."""
    end = time.time() + timeout
    while time.time() < end:
        fake.tick()
        if done():
            return True
        time.sleep(0.005)
    return False


class Client(object):
    """A plain, blocking client for the kit's socket."""

    def __init__(self, port, timeout=5.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buf = b""

    def send(self, message):
        self.sock.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def receive(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def ask(self, message):
        self.send(message)
        return self.receive()

    def hello(self, token=None, protocol=1, session=None):
        if token is None:
            with open(os.path.join(os.environ["MODO_TOOLS_HOME"], "token")) as handle:
                token = handle.read().strip()
        payload = {"token": token, "protocol": protocol}
        if session is not None:
            payload["session"] = session
        return self.ask({"id": "hello", "op": "hello", "payload": payload})

    def is_closed(self):
        try:
            self.sock.settimeout(2.0)
            return self.sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        except socket.timeout:
            return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Asked(object):
    """A request sent from another thread, so this thread can tick Modo."""

    def __init__(self, client, message):
        import threading
        self.answer = None
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(client, message))
        self._thread.daemon = True
        self._thread.start()

    def _run(self, client, message):
        try:
            self.answer = client.ask(message)
        except Exception as exc:  # surfaced by the test
            self.error = exc

    def done(self):
        return not self._thread.is_alive()
