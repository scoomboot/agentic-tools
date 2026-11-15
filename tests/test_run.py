"""run_command, run_python, set_channels, save_scene, event_log."""

import os
import sys
import unittest

import support


def job(kit, op, payload):
    jobs = sys.modules["modotools.jobs"]
    return jobs.run_job({"id": "t-1", "op": op, "payload": payload}, kit)


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    # A word that isn't one of the argument's values.

    def projection(self):
        self.fake.describe("view3d.projection", "Projection", args=[
            ("type", "integer", 2, "Type")], hints={"type": [
                (0, "top"), (1, "bot"), (2, "bck"), (3, "fnt"), (4, "rgt"),
                (5, "lft"), (6, "psp"), (0, "=view-projection")]})

    def test_a_stray_word_gets_a_note_with_what_modo_set(self):
        # Found 2026-11-09: frt gave the Top view; front is fnt.
        self.projection()
        answer = job(self.kit, "run_command", {"command": "view3d.projection frt"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["note"],
                         "frt is not one of type's values; Modo set top.")
        self.assertEqual(self.fake.scene.projection, "top")

    def test_a_stray_integer_gets_the_note_too(self):
        self.projection()
        answer = job(self.kit, "run_command",
                     {"command": "view3d.projection type:12"})
        self.assertEqual(answer["result"]["note"],
                         "12 is not one of type's values; Modo set 12.")

    def test_a_known_word_gets_no_note(self):
        self.projection()
        for word in ("fnt", "FNT", "3", "type:psp", "{lft}"):
            answer = job(self.kit, "run_command",
                         {"command": "view3d.projection " + word})
            self.assertTrue(answer["ok"], answer)
            self.assertNotIn("note", answer["result"], word)

    def test_a_query_gets_no_note(self):
        self.projection()
        answer = job(self.kit, "run_command", {"command": "view3d.projection ?"})
        self.assertNotIn("note", answer["result"])

    def test_a_stray_tool_attribute_word_gets_the_note(self):
        # prim.sphere method is an integer whose 1 is QuadBall.
        self.fake.tools["prim.sphere"] = [
            ("method", "integer", 0, [(0, "globe"), (1, "qball"), (2, "tess"),
                                      (0, "=prim-sphere-method")])]
        job(self.kit, "run_command", {"command": "tool.set prim.sphere on"})
        answer = job(self.kit, "run_command",
                     {"command": "tool.attr prim.sphere method qbal"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["note"],
                         "qbal is not one of method's values; Modo set globe.")
        known = job(self.kit, "run_command",
                    {"command": "tool.attr prim.sphere method qball"})
        self.assertNotIn("note", known["result"])

    def test_an_argument_that_cant_be_asked_says_what_modo_took_it_as(self):
        # On 17.1 view3d.shadingStyle's bgmesh can't be asked with ?
        # ("command argument parsing failed"), so what Modo set is unknown.
        self.fake.describe("view3d.shadingStyle", "Shading", args=[
            ("style", "integer", 2, "Style"), ("bgmesh", "integer", 1, "Mesh")],
            hints={"style": [(0, "wire"), (3, "shaded"), (5, "advgl"),
                             (7, "shd1")],
                   "bgmesh": [(0, "active"), (1, "inactive")]})
        answer = job(self.kit, "run_command",
                     {"command": "view3d.shadingStyle shade inactve"})
        self.assertTrue(answer["ok"], answer)
        # The line spawned, not run, holds what Modo's
        # parser took the word as.
        self.assertEqual(answer["result"]["note"],
                         "shade is not one of style's values; Modo set wire. "
                         "inactve is not one of bgmesh's values; Modo took "
                         "it as active.")

    # On 17.1 a word with no name after a named one is
    # dropped. `select.symmetry 0 uvActive:0 u` left uvAxis at v.

    def three(self):
        self.fake.describe("test.three", "Three", args=[
            ("a", "integer", 1, "A"), ("b", "integer", 1, "B"),
            ("c", "integer", 1, "C")], hints={
                "a": [(0, "a1"), (1, "a2")], "b": [(0, "b1"), (1, "b2")],
                "c": [(0, "c1"), (1, "c2")]})

    DROPPED = ("Modo didn't read %s: a word with no name after a named one "
               "is dropped. Give it its argument's name.")

    def test_a_word_after_a_named_one_is_dropped_and_said(self):
        self.three()
        for word in ("c2", "zzz"):
            answer = job(self.kit, "run_command",
                         {"command": "test.three b:b2 " + word})
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(self.fake.three, {"b": "b2"})
            # Not "zzz is not one of a's values": no argument was given it.
            self.assertEqual(answer["result"]["note"], self.DROPPED % word)

    def test_words_before_a_named_one_are_read_in_order(self):
        self.three()
        answer = job(self.kit, "run_command", {"command": "test.three a2 zzz c:c1"})
        self.assertEqual(self.fake.three, {"a": "a2", "b": "zzz", "c": "c1"})
        self.assertEqual(answer["result"]["note"],
                         "zzz is not one of b's values; Modo took it as b1.")

    def test_a_dropped_word_is_said_for_a_command_without_hints(self):
        self.fake.describe("test.three", "Three", args=[
            ("a", "string", 1, "A"), ("b", "string", 1, "B"),
            ("c", "string", 1, "C")])
        answer = job(self.kit, "run_command", {"command": "test.three a:x {y z}"})
        self.assertEqual(self.fake.three, {"a": "x"})
        self.assertEqual(answer["result"]["note"], self.DROPPED % "{y z}")
        # A value with a colon in it is not a named word.
        answer = job(self.kit, "run_command", {"command": "test.three C:/x.lxo y"})
        self.assertEqual(self.fake.three, {"a": "C:/x.lxo", "b": "y"})
        self.assertNotIn("note", answer["result"])

    def test_a_line_that_cant_be_spawned_gets_its_values_listed(self):
        self.three()
        self.fake.unspawnable = ("test.three a1 zzz",)
        answer = job(self.kit, "run_command", {"command": "test.three a1 zzz"})
        self.assertEqual(answer["result"]["note"],
                         "zzz is not one of b's values (b1, b2); Modo may "
                         "have taken it as another.")

    def test_a_number_in_a_range_gets_no_note(self):
        # "&min" and "&max" are bounds, not words, so 512
        # isn't a stray word for clip.newSequence's width.
        self.fake.describe("test.three", "Three", args=[
            ("a", "integer", 1, "A"), ("b", "float", 1, "B"),
            ("c", "integer", 1, "C")], hints={
                "a": [(1, "&min"), (65536, "&max")], "b": [(1, "%min")]})
        answer = job(self.kit, "run_command", {"command": "test.three 512 0.5"})
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("note", answer["result"])

    def test_a_variable_arguments_word_is_never_checked(self):
        # Reading a variable argument's hints from the bare command hangs
        # Modo (tool.attr's value), so its word is left alone.
        self.fake.describe("test.three", "Three", args=[
            ("a", "integer", 1, "A"), ("b", "", 8, "B"),
            ("c", "integer", 1, "C")], hints={"b": "hang"})
        answer = job(self.kit, "run_command", {"command": "test.three 1 zzz"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.crashes, [])
        self.assertNotIn("note", answer["result"])

    def test_a_command_without_hints_gets_no_note(self):
        answer = job(self.kit, "run_command", {"command": "select.time 2.0"})
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("note", answer["result"])

    def test_a_failing_command_is_an_error_not_a_dialog(self):
        answer = job(self.kit, "run_command", {"command": "test.fails value:3"})
        self.assertFalse(answer["ok"])
        err = answer["error"]
        self.assertEqual(err["type"], "command_failed")
        self.assertIn("Command failed", err["message"])
        self.assertIn("Fails: the value is out of range", err["event_log"])
        self.assertEqual(self.fake.dialogs, [])

    def test_a_failed_script_points_at_the_event_log(self):
        # A probe (17.1, 2026-11-08): a script run with @ that raises fails as
        # "command failed" with no message; Modo has undone what it ran, and
        # says why only in the event log's lines under the script's name.
        answer = job(self.kit, "run_command",
                     {"command": "@C:/models/replay.py"})
        err = answer["error"]
        self.assertEqual(err["type"], "command_failed")
        self.assertNotIn("isn't a command", err["hint"])
        self.assertIn("event log", err["hint"])
        self.assertIn("undone", err["hint"])

    def test_a_tool_name_gets_the_tool_hint(self):
        self.fake.tools["poly.bevel"] = [("inset", "distance", 0.0)]
        answer = job(self.kit, "run_command", {"command": "poly.bevel inset:0.01"})
        err = answer["error"]
        # Nothing ran, so it is a refusal.
        self.assertEqual(err["type"], "unknown_command")
        self.assertIn("is a tool, not a command", err["hint"])
        self.assertIn("tool.set", err["hint"])
        self.assertEqual(self.fake.dialogs, [])

    def test_an_unknown_name_hints_at_find_commands(self):
        # A name that isn't a command ran nothing, so it
        # is unknown_command, a refusal: an open action stays open.
        answer = job(self.kit, "run_command", {"command": "scene.sav"})
        self.assertEqual(answer["error"]["type"], "unknown_command")
        self.assertIn("isn't a command", answer["error"]["hint"])
        self.assertIn("Nothing ran", answer["error"]["hint"])
        self.assertIn("mt_find_commands", answer["error"]["hint"])
        self.assertIn("scene.save", answer["error"]["hint"])

    def test_a_command_that_exists_and_fails_stays_command_failed(self):
        answer = job(self.kit, "run_command", {"command": "test.fails value:3"})
        self.assertEqual(answer["error"]["type"], "command_failed")

    def test_a_command_runs_and_its_purpose_is_logged(self):
        answer = job(self.kit, "run_command", {"command": "test.cube",
                                               "purpose": "a cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(self.fake.scene.items), 1)
        self.assertIn("mt > a cube", self.fake.event_log)

    def test_a_query_returns_its_value(self):
        answer = job(self.kit, "run_command",
                     {"command": "query commandservice command.username ? scene.save"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["result"], ["scene.save"])


class RunPythonTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    def py(self, code, purpose="test"):
        return job(self.kit, "run_python", {"code": code, "purpose": purpose})

    def test_result_and_stdout(self):
        answer = self.py("lx.eval('test.cube')\nprint('made it')\n"
                         "RESULT = len(modo.Scene().meshes)", purpose="make a cube")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["result"], 1)
        self.assertEqual(answer["stdout"], "made it\n")
        self.assertIn("mt > make a cube", self.fake.event_log)

    def test_purpose_is_required(self):
        answer = self.py("RESULT = 1", purpose="")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["type"], "bad_request")

    def test_a_python_error_comes_back_with_its_traceback(self):
        answer = self.py("x = 1\nraise KeyError('nope')")
        err = answer["error"]
        self.assertEqual(err["type"], "python_error")
        self.assertIn("KeyError", err["message"])
        self.assertIn("line 2", err["traceback"])

    def test_a_syntax_error_is_a_python_error(self):
        answer = self.py("def (:")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertIn("SyntaxError", answer["error"]["message"])

    def test_a_failing_lx_eval_is_a_python_error_without_a_dialog(self):
        answer = self.py("lx.eval('no.such.command')")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertEqual(self.fake.dialogs, [])

    def test_a_cancelled_dialog_is_a_python_error(self):
        answer = self.py("lx.eval('test.fails value:1')")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertIn("Fails: the value is out of range",
                      answer["error"]["event_log"])
        self.assertEqual(self.fake.dialogs, [])

    def test_sys_exit_is_a_python_error_and_the_host_keeps_running(self):
        answer = self.py("import sys\nsys.exit(3)")
        self.assertEqual(answer["error"]["type"], "python_error")
        self.assertIn("sys.exit", answer["error"]["hint"])
        again = self.py("RESULT = 2")
        self.assertEqual(again["result"]["result"], 2)

    def test_an_odd_result_is_made_plain(self):
        answer = self.py("RESULT = {'a': (1, 2), 'b': object()}")
        result = answer["result"]["result"]
        self.assertEqual(result["a"], [1, 2])
        self.assertIsInstance(result["b"], str)

    def test_through_the_socket_and_the_pump(self):
        client = support.Client(self.kit.port)
        self.addCleanup(client.close)
        client.hello()
        asked = support.Asked(client, {"id": "r-1", "op": "run_python", "payload": {
            "code": "RESULT = 40 + 2", "purpose": "add"}})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        self.assertEqual(asked.answer["result"]["result"], 42)
        self.assertEqual(self.fake.off_thread, [])
        self.assertIn("modoTools.exec", self.fake.ran)


class ChannelsAndSaveTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.home = os.environ["MODO_TOOLS_HOME"]
        mesh = self.fake.scene.add("mesh001", "Cube", channels={"visible": "default"})
        # As in Modo: a mesh has no pos/rot channels; its transform items do.
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})
        mesh.transforms["rotation"] = self.fake.scene.add(
            "rotation004", "Rotation", "rotation",
            channels={"rot.X": 0.0, "rot.Y": 0.0, "rot.Z": 0.0})

    def test_pos_and_rot_on_a_mesh_go_to_its_transform_items(self):
        answer = job(self.kit, "set_channels", {"item": "mesh001",
                                                "channels": {"pos.X": 1.5, "rot.Y": 0.3}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.scene.find("translation005").channel("pos.X").get(), 1.5)
        self.assertEqual(answer["result"]["channels"]["pos.X"],
                         {"old": 0.0, "new": 1.5, "item": "translation005"})
        # Raw values, as Python reads them back: radians, not degrees.
        self.assertEqual(answer["result"]["channels"]["rot.Y"]["new"], 0.3)

    def test_an_own_channel_and_a_name(self):
        answer = job(self.kit, "set_channels", {"item": "Cube",
                                                "channels": {"visible": "off"}})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["channels"]["visible"],
                         {"old": "default", "new": "off"})

    def test_a_name_two_items_share_writes_nothing(self):
        self.fake.scene.add("mesh002", "Cube", channels={"visible": "default"})
        answer = job(self.kit, "set_channels", {"item": "Cube",
                                                "channels": {"visible": "off"}})
        self.assertEqual(answer["error"]["type"], "ambiguous_item")
        self.assertEqual(self.fake.scene.find("mesh001").channel("visible").get(),
                         "default")

    def test_no_such_item(self):
        answer = job(self.kit, "set_channels", {"item": "mesh999",
                                                "channels": {"pos.X": 1}})
        self.assertEqual(answer["error"]["type"], "no_such_item")

    def test_bad_channel_writes_nothing(self):
        answer = job(self.kit, "set_channels", {"item": "mesh001",
                                                "channels": {"pos.X": 3, "pos.Q": 1}})
        self.assertEqual(answer["error"]["type"], "bad_channel")
        self.assertIn("pos.Q", answer["error"]["message"])
        self.assertEqual(self.fake.scene.find("translation005").channel("pos.X").get(), 0.0)

    def test_pos_on_a_mesh_without_transform_items(self):
        self.fake.scene.add("mesh002", "Bare")
        answer = job(self.kit, "set_channels", {"item": "mesh002",
                                                "channels": {"pos.X": 1}})
        self.assertEqual(answer["error"]["type"], "bad_channel")
        self.assertIn("transform items", answer["error"]["hint"])

    def test_save_to_a_path_uses_lx_command(self):
        path = os.path.join(self.home, "a b.lxo")
        answer = job(self.kit, "save_scene", {"path": path})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.scene.saved_to, [path.replace("\\", "/")])
        self.assertEqual(answer["result"]["bytes"], 3)
        self.assertEqual([n for n, _a in self.fake.command_calls], ["!!scene.saveAs"])

    def test_backslashes_become_forward_slashes(self):
        self.fake.scene.write_files = False
        answer = job(self.kit, "save_scene", {"path": "C:\\work\\x.lxo"})
        self.assertEqual(self.fake.scene.saved_to, ["C:/work/x.lxo"])

    def test_a_save_that_writes_nothing_is_an_error(self):
        self.fake.scene.write_files = False
        answer = job(self.kit, "save_scene", {"path": os.path.join(self.home, "x.lxo")})
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertIn("wasn't written", answer["error"]["message"])

    def test_save_without_a_path_uses_the_scenes_file_even_unchanged(self):
        self.fake.scene.filename = os.path.join(self.home, "x.lxo").replace("\\", "/")
        answer = job(self.kit, "save_scene", {})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.fake.scene.saved_to, [self.fake.scene.filename])

    def test_a_save_over_an_old_file_that_writes_nothing_is_an_error(self):
        # The file is there from an earlier save, so its size alone says
        # nothing: the save has to have touched it.
        path = os.path.join(self.home, "x.lxo")
        with open(path, "w") as handle:
            handle.write("old lxo")
        os.utime(path, (1000000000, 1000000000))
        self.fake.scene.filename = path.replace("\\", "/")
        self.fake.scene.write_files = False
        answer = job(self.kit, "save_scene", {})
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertIn("wasn't written", answer["error"]["message"])

    def test_a_save_over_an_old_file_that_writes_is_ok(self):
        path = os.path.join(self.home, "x.lxo")
        with open(path, "w") as handle:
            handle.write("old lxo")
        os.utime(path, (1000000000, 1000000000))
        answer = job(self.kit, "save_scene", {"path": path})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["bytes"], 3)

    def test_save_a_never_saved_scene_without_a_path(self):
        answer = job(self.kit, "save_scene", {})
        self.assertEqual(answer["error"]["type"], "bad_request")
        self.assertIn("path", answer["error"]["hint"])

    def test_a_failed_save_is_command_failed(self):
        answer = job(self.kit, "save_scene", {"path": "Z:/nowhere.lxo"})
        self.assertEqual(answer["error"]["type"], "command_failed")
        self.assertEqual(self.fake.dialogs, [])


class EventLogTest(unittest.TestCase):
    def test_last_n_lines(self):
        fake, kit = support.start_kit(self)
        for n in range(5):
            fake.out("line %d" % n)
        answer = job(kit, "event_log", {"count": 3})
        self.assertEqual(answer["result"]["lines"], ["line 2", "line 3", "line 4"])

    def test_child_lines_are_indented_under_their_entry(self):
        # A probe (2026-11-08): a script run with @ writes its lx.out lines and
        # its exception as child entries of the line naming the script.
        fake, kit = support.start_kit(self)
        fake.event_log.append(("j4.py", ["replay: hello",
                                         ("Unhandled exception", [" .. <string>: 6"])]))
        fake.out("script.implicit failed")
        answer = job(kit, "event_log", {"count": 2})
        self.assertEqual(answer["result"]["lines"],
                         ["j4.py", "  replay: hello", "  Unhandled exception",
                          "     .. <string>: 6", "script.implicit failed"])

    def test_a_failed_job_carries_the_child_lines(self):
        fake, kit = support.start_kit(self)
        answer = job(kit, "run_python", {
            "code": "import fakelx\n"
                    "fakelx.STATE.event_log.append(('j4.py', "
                    "['check after 2: Cube differs']))\n"
                    "raise RuntimeError('command failed')",
            "purpose": "t"})
        self.assertFalse(answer["ok"])
        self.assertIn("  check after 2: Cube differs",
                      answer["error"]["event_log"])


if __name__ == "__main__":
    unittest.main()
