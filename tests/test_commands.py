"""The lxserv file, loaded the way Modo loads it."""

import socket
import sys
import unittest

import support


class CommandsTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        self.addCleanup(support.stop_running_kit)

    def test_index_cfg_imports_the_script_folders(self):
        self.assertEqual(support.index_cfg_imports(),
                         ["Configs", "Scripts", "Scripts/python_modules"])

    def test_commands_bless_and_nothing_heavy_loads_at_scan(self):
        fake, _module = support.load_plugin()
        self.assertEqual(sorted(fake.commands), sorted([
            "modoTools.start", "modoTools.stop", "modoTools.status",
            "modoTools.exec", "modoTools.execRead", "modoTools.pump",
            "modoTools.running", "modoTools.autostart", "modoTools.boot",
            "modoTools.opLog", "modoTools.folder"]))
        self.assertNotIn("modotools", sys.modules)
        for name in fake.commands:
            self.assertEqual(name.count("."), 1)
            self.assertFalse(name.startswith("mcpBridge"))

    def test_flags(self):
        fake, module = support.load_plugin()
        lx = sys.modules["lx"]
        self.assertEqual(fake.commands["modoTools.exec"]().cmd_Flags(),
                         lx.symbol.fCMD_MODEL | lx.symbol.fCMD_UNDO)
        self.assertEqual(fake.commands["modoTools.execRead"]().cmd_Flags(),
                         lx.symbol.fCMD_UI)

    def test_start_says_ready_and_status_logs(self):
        fake, _module = support.load_plugin()
        fake.run_command("modoTools.start port:0")
        port = sys.modules["modotools"].running().port
        self.assertTrue(any(line.startswith("modo-tools ready on %d" % port)
                            for line in fake.event_log), fake.event_log)
        fake.run_command("modoTools.status")
        self.assertIn("running=True", fake.event_log[-1])
        fake.run_command("modoTools.stop")
        self.assertEqual(fake.event_log[-1], "modo-tools: stopped")
        fake.run_command("modoTools.stop")
        self.assertEqual(fake.event_log[-1], "modo-tools: was not running")

    def test_a_port_in_use_is_named(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]
        fake, _module = support.load_plugin()
        fake.run_command("modoTools.start port:%d" % port)
        last = fake.event_log[-1]
        self.assertIn("could not start", last)
        self.assertIn("port %d is already in use" % port, last)
        self.assertIn("port:", last)
        self.assertIsNone(sys.modules["modotools"].running())

    def test_start_twice_restarts(self):
        fake, _module = support.load_plugin()
        fake.run_command("modoTools.start port:0")
        fake.run_command("modoTools.start port:0")
        self.assertTrue(sys.modules["modotools"].running().listener.is_running())


if __name__ == "__main__":
    unittest.main()
