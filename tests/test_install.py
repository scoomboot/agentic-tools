"""The .lpk, the menu and the Start with Modo flag."""

import importlib.util
import os
import sys
import tempfile
import shutil
import unittest
import xml.etree.ElementTree as ET
import zipfile

import support

BUILD_LPK = os.path.join(support.KIT, "build_lpk.py")
CONFIGS = os.path.join(support.KIT, "Configs")

# Modo's own commands the menu may use. Anything else has to be ours.
MODO_COMMANDS = {"attr.formPopover"}


def load_build_lpk():
    spec = importlib.util.spec_from_file_location("build_lpk", BUILD_LPK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def any_free_port(testcase):
    """The menu and boot start on the default port, which a real Modo on this
    machine may hold. Point the default at a free port for one test."""
    import modotools.protocol as protocol
    testcase.addCleanup(setattr, protocol, "DEFAULT_PORT", protocol.DEFAULT_PORT)
    protocol.DEFAULT_PORT = 0


def kit_files():
    """Every file a kit install needs: what is under kit/modo_tools, less
    bytecode and the build script itself."""
    found = []
    for folder, dirs, files in os.walk(support.KIT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            path = os.path.join(folder, name)
            if os.path.samefile(path, BUILD_LPK):
                continue
            found.append(os.path.relpath(path, support.KIT).replace(os.sep, "/"))
    return sorted(found)


def config_commands(paths):
    """(file, command) for every command a config runs: each control's
    `cmd ...` and each startup command."""
    found = []
    for path in paths:
        root = ET.parse(path).getroot()
        for control in root.iter("list"):
            kind = control.get("type")
            if kind == "Control":
                value = (control.get("val") or "").strip()
                if value.startswith("cmd "):
                    found.append((path, value.split()[1]))
            elif kind == "Command":
                found.append((path, (control.text or "").split()[0]))
    return found


def unknown_commands(paths, registered):
    return [(os.path.basename(path), name)
            for path, name in config_commands(paths)
            if name not in registered and name not in MODO_COMMANDS]


class LpkTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="modo_tools_lpk_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_index_lists_every_kit_file_and_nothing_else(self):
        lpk = load_build_lpk().build(self.out)
        with zipfile.ZipFile(lpk) as archive:
            names = sorted(archive.namelist())
            index = ET.fromstring(archive.read("index.xml"))
        kit = index.find("kit")
        self.assertEqual(kit.get("name"), "modo_tools")
        self.assertEqual(kit.get("restart"), "YES")
        sources = sorted(node.text for node in kit.findall("source"))
        self.assertEqual(sources, kit_files())
        for node in kit.findall("source"):
            self.assertEqual(node.get("target"), "modo_tools/" + node.text)
        self.assertEqual(names, sorted(["index.xml"] + kit_files()))

    def test_bytecode_and_the_build_script_stay_out(self):
        stray = os.path.join(support.PYMODS, "modotools", "__pycache__")
        made = not os.path.isdir(stray)
        os.makedirs(stray, exist_ok=True)
        junk = os.path.join(stray, "lpk_test_junk.pyc")
        with open(junk, "wb") as handle:
            handle.write(b"junk")
        self.addCleanup(os.remove, junk)
        if made:
            self.addCleanup(os.rmdir, stray)
        lpk = load_build_lpk().build(self.out)
        with zipfile.ZipFile(lpk) as archive:
            names = archive.namelist()
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        self.assertNotIn("build_lpk.py", names)

    def test_the_lpk_is_named_for_the_kit(self):
        lpk = load_build_lpk().build(self.out)
        self.assertEqual(os.path.basename(lpk), "modo_tools.lpk")


class MenuTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        self.addCleanup(support.stop_running_kit)
        self.fake, _module = support.load_plugin()
        any_free_port(self)

    def configs(self):
        return sorted(os.path.join(CONFIGS, name) for name in os.listdir(CONFIGS)
                      if name.endswith(".cfg"))

    def test_index_cfg_imports_the_configs(self):
        self.assertIn("Configs", support.index_cfg_imports())

    def test_configs_name_only_registered_commands(self):
        self.assertTrue(self.configs())
        self.assertEqual(unknown_commands(self.configs(), self.fake.commands), [])

    def test_the_check_catches_a_command_that_does_not_exist(self):
        folder = tempfile.mkdtemp(prefix="modo_tools_cfg_")
        self.addCleanup(shutil.rmtree, folder, True)
        bad = os.path.join(folder, "bad.cfg")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0"?><configuration>'
                         '<atom type="Attributes"><hash type="Sheet" key="x">'
                         '<list type="Control" val="cmd modoTools.nope">'
                         '</list></hash></atom>'
                         '<atom type="StartupCommands">'
                         '<list type="Command">modoTools.alsoNope</list>'
                         '</atom></configuration>')
        self.assertEqual(unknown_commands([bad], self.fake.commands),
                         [("bad.cfg", "modoTools.nope"),
                          ("bad.cfg", "modoTools.alsoNope")])

    def test_the_menu_has_every_entry(self):
        commands = [name for _path, name in config_commands(self.configs())]
        for name in ("modoTools.start", "modoTools.stop", "modoTools.status",
                     "modoTools.running", "modoTools.autostart",
                     "modoTools.opLog", "modoTools.folder"):
            self.assertIn(name, commands)

    def test_the_menu_is_on_the_menu_bar_and_boot_runs_at_startup(self):
        menubar, startup = [], []
        for path in self.configs():
            root = ET.parse(path).getroot()
            for node in root.iter("hash"):
                if node.get("type") == "InCategory" and node.get("key", "").startswith("menubar#"):
                    menubar.append(path)
            for atom in root.iter("atom"):
                if atom.get("type") == "StartupCommands":
                    startup += [(c.text or "").strip() for c in atom.iter("list")]
        self.assertTrue(menubar)
        self.assertEqual(startup, ["modoTools.boot"])

    def test_running_toggles_and_answers_a_query(self):
        self.assertEqual(self.fake.run_command("modoTools.running ?"), 0)
        self.fake.run_command("modoTools.running true")
        self.assertIsNotNone(sys.modules["modotools"].running())
        self.assertEqual(self.fake.run_command("modoTools.running ?"), 1)
        self.fake.run_command("modoTools.running false")
        self.assertIsNone(sys.modules["modotools"].running())

    def test_op_log_shows_the_last_lines(self):
        self.fake.run_command("modoTools.opLog")
        self.assertIn("no op log yet", self.fake.event_log[-1])
        self.fake.run_command("modoTools.start port:0")
        self.fake.run_command("modoTools.opLog")
        self.assertIn('"event": "session"', self.fake.event_log[-1])

    def test_folder_opens_the_data_folder(self):
        opened = []
        import modotools.settings as settings
        self.addCleanup(setattr, settings, "open_with_system",
                        settings.open_with_system)
        settings.open_with_system = opened.append
        self.fake.run_command("modoTools.folder")
        self.assertEqual(opened, [os.environ["MODO_TOOLS_HOME"]])


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_home(self)
        self.addCleanup(support.stop_running_kit)
        self.fake, _module = support.load_plugin()
        any_free_port(self)
        self.flag = os.path.join(self.home, "autostart.on")

    def boot(self):
        self.fake.run_command("modoTools.boot")
        package = sys.modules.get("modotools")
        return package is not None and package.running() is not None

    def test_off_by_default(self):
        self.assertFalse(os.path.exists(self.flag))
        self.assertEqual(self.fake.run_command("modoTools.autostart ?"), 0)
        self.assertFalse(self.boot())

    def test_on_starts_the_kit_at_boot(self):
        self.fake.run_command("modoTools.autostart true")
        self.assertTrue(os.path.isfile(self.flag))
        self.assertEqual(self.fake.run_command("modoTools.autostart ?"), 1)
        self.assertTrue(self.boot())
        self.assertIn("started with Modo", " ".join(self.fake.event_log))

    def test_off_removes_the_flag(self):
        self.fake.run_command("modoTools.autostart true")
        self.fake.run_command("modoTools.autostart false")
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(self.boot())

    def test_a_broken_flag_file_counts_as_off(self):
        for broken in ("garbage", "", None):
            with self.subTest(broken=broken):
                if os.path.isdir(self.flag):
                    os.rmdir(self.flag)
                elif os.path.exists(self.flag):
                    os.remove(self.flag)
                if broken is None:
                    os.makedirs(self.flag)
                else:
                    with open(self.flag, "w", encoding="utf-8") as handle:
                        handle.write(broken)
                self.assertEqual(self.fake.run_command("modoTools.autostart ?"), 0)
                self.assertFalse(self.boot())
                self.assertIn("autostart.on", self.fake.event_log[-1])

    def test_boot_never_raises(self):
        self.fake.run_command("modoTools.autostart true")
        import modotools
        original = modotools.start

        def fails(**_kwargs):
            raise RuntimeError("no socket today")

        modotools.start = fails
        self.addCleanup(setattr, modotools, "start", original)
        self.assertFalse(self.boot())
        self.assertIn("could not start with Modo", self.fake.event_log[-1])
        self.assertEqual(self.fake.dialogs, [])


if __name__ == "__main__":
    unittest.main()
