"""The journal, written by the kit.

Every changing call is an entry in a JSONL file for its scene, forced to
disk before the answer goes out: the call as asked, all of its code, the
command lines the guard saw it send, and the modo writes it couldn't see as
commands. Probes (2026-11-08) found what the trace holds: every lx.eval a
job sends, and none of the modo module's writes, not even a selection.
"""

import json
import os
import sys
import unittest

import support


def journals():
    return os.path.join(os.environ["MODO_TOOLS_HOME"], "journals")


def files():
    folder = journals()
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".jsonl"))


def records(name):
    with open(os.path.join(journals(), name), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class JournalBase(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.client = support.Client(self.kit.port)
        self.addCleanup(self.client.close)
        self.client.hello()
        self.n = 0
        mesh = self.fake.scene.add("mesh001", "Cube")
        mesh.transforms["translation"] = self.fake.scene.add(
            "translation005", "Translation", "translation",
            channels={"pos.X": 0.0, "pos.Y": 0.0, "pos.Z": 0.0})

    def ask(self, op, payload=None):
        self.n += 1
        asked = support.Asked(self.client, {"id": "j-%d" % self.n, "op": op,
                                            "payload": payload or {}})
        self.assertTrue(support.pump_until(self.fake, asked.done))
        return asked.answer

    def py(self, code, **extra):
        payload = {"code": code, "purpose": "test"}
        payload.update(extra)
        return self.ask("run_python", payload)

    def entries(self, name=None):
        name = name or files()[-1]
        return [r for r in records(name) if r["event"] == "entry"]


class JournalTest(JournalBase):
    # -- an entry per changing call ------------------------------------------

    def test_a_command_is_one_entry_with_its_line(self):
        answer = self.ask("run_command", {"command": "test.cube",
                                          "purpose": "a cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["journal"], 1)
        self.assertEqual(len(files()), 1)
        header, entry = records(files()[0])
        self.assertEqual(header["event"], "header")
        self.assertEqual(header["journal"], 1)
        self.assertEqual(header["scene"], {"name": "Untitled", "file": None,
                                           "unsaved": False})
        self.assertEqual(entry["event"], "entry")
        self.assertEqual(entry["n"], 1)
        self.assertEqual(entry["id"], "j-1")
        self.assertEqual(entry["tool"], "run_command")
        self.assertEqual(entry["purpose"], "a cube")
        self.assertEqual(entry["args"], {"command": "test.cube",
                                         "purpose": "a cube"})
        self.assertEqual(entry["commands"], ["!!test.cube"])
        self.assertEqual(entry["untraced"], [])
        self.assertIs(entry["ok"], True)
        self.assertEqual(entry["undo"], {"step": 1})

    def test_a_script_keeps_its_commands_not_its_queries(self):
        code = ("lx.eval('test.cube')\n"
                "lx.eval('query commandservice commands ?')\n"
                "lx.eval1('test.cube')\n"
                "lx.command('item.channel', name='pos.X', value=1.0,"
                " item='translation005')\n")
        answer = self.py(code)
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["args"]["code"], code)
        self.assertEqual(entry["commands"], [
            "!!test.cube", "!!test.cube",
            "!!item.channel name:{pos.X} value:{1.0} item:{translation005}"])
        self.assertEqual(entry["untraced"], [])

    def test_a_channel_set_in_code_is_untraced(self):
        # Channel.set sends no command, so the trace can't hold it.
        answer = self.py("modo.Scene().item('translation005')"
                         ".channel('pos.X').set(2.0)")
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["commands"], [])
        self.assertEqual(entry["untraced"], ["Channel.set"])
        self.assertEqual(
            self.fake.scene.find("translation005").channel("pos.X").get(), 2.0)

    def test_sdk_calls_are_untraced(self):
        answer = self.py("svc = lx.service.Undo()\nRESULT = 1")
        entry, = self.entries()
        self.assertEqual(entry["untraced"], ["lx.service"])

    def test_the_modo_writes_are_put_back_after_the_job(self):
        channel = sys.modules["fakelx"].FakeChannel
        real = channel.set
        self.py("RESULT = 1")
        self.assertIs(channel.set, real)

    def test_reads_read_only_and_refused_calls_write_nothing(self):
        for _ in range(3):
            self.assertTrue(self.ask("scene_summary")["ok"])
        answer = self.py("RESULT = 1", read_only=True)
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("journal", answer)
        refused = self.py(
            "lx.eval('query commandservice command.username ? {no.such}')")
        self.assertEqual(refused["error"]["type"], "unknown_command")
        self.assertNotIn("journal", refused)
        no_purpose = self.ask("run_python", {"code": "RESULT = 1"})
        self.assertEqual(no_purpose["error"]["type"], "bad_request")
        self.assertEqual(files(), [])

    def test_a_failed_call_is_written_then_rolled_back(self):
        answer = self.py("lx.eval('test.cube')\nraise ValueError('half')")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["journal"], 1)
        self.assertEqual(answer["undo"], {"rolled_back": True})
        recs = records(files()[0])
        self.assertEqual([r["event"] for r in recs],
                         ["header", "entry", "rolled_back"])
        self.assertIs(recs[1]["ok"], False)
        self.assertEqual(recs[1]["commands"], ["!!test.cube"])
        self.assertEqual(recs[1]["error"]["type"], "python_error")
        self.assertEqual(recs[2]["n"], 1)

    def test_a_refused_call_with_an_old_calls_id_marks_nothing(self):
        # every server counts its ids from r-1, so a second
        # session's call has the id of one of the first's. Refused and
        # rolled back, it wrote rolled_back against the first one's entry.
        def ask(payload):
            asked = support.Asked(self.client, {
                "id": "r-1", "op": "run_python", "payload": payload})
            self.assertTrue(support.pump_until(self.fake, asked.done))
            return asked.answer
        kept = ask({"code": "lx.eval('test.cube')", "purpose": "test"})
        self.assertEqual(kept["journal"], 1)
        refused = ask({"code": "lx.eval('test.cube')"})
        self.assertEqual(refused["error"]["type"], "bad_request")
        self.assertEqual(refused["undo"], {"rolled_back": True})
        self.assertEqual([r["event"] for r in records(files()[0])],
                         ["header", "entry"])

    def test_an_unsure_rollback_is_written(self):
        self.fake.silent_markers = True
        answer = self.py("lx.eval('test.cube')\nraise ValueError('half')")
        self.assertEqual(answer["undo"], {"step": 1, "rolled_back": "unsure"})
        recs = records(files()[0])
        self.assertEqual([r["event"] for r in recs][-1], "unsure")
        self.assertEqual(recs[-1]["n"], 1)

    def test_entries_are_numbered_in_one_file(self):
        self.ask("run_command", {"command": "test.cube"})
        second = self.py("lx.eval('test.cube')")
        self.assertEqual(second["journal"], 2)
        self.assertEqual([e["n"] for e in self.entries()], [1, 2])
        self.assertEqual(len(files()), 1)

    # -- a write that fails --------------------------------------------------

    def test_a_write_error_leaves_the_call_ok_and_says_so(self):
        # A file where the folder should be: every open fails.
        with open(journals(), "w") as handle:
            handle.write("not a folder")
        answer = self.ask("run_command", {"command": "test.cube"})
        self.assertTrue(answer["ok"], answer)
        self.assertIn("error", answer["journal"])
        status = self.ask("status")["result"]["journal"]
        self.assertIn("error", status)
        # The next call tries again.
        os.remove(journals())
        again = self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(again["journal"], 1)
        self.assertEqual([r["event"] for r in records(files()[0])],
                         ["header", "entry"])
        self.assertIsNone(self.ask("status")["result"]["journal"]["error"])

    def test_every_line_is_forced_to_disk(self):
        journal = sys.modules["modotools.journal"]
        before = journal.Journal.synced
        synced = []
        real = os.fsync
        os.fsync = lambda fd: (synced.append(fd), real(fd))[1]
        self.addCleanup(setattr, os, "fsync", real)
        self.ask("run_command", {"command": "test.cube"})
        # The op log's op_start and op_ok, and the journal's header and entry.
        self.assertGreaterEqual(len(synced), 4)
        self.assertEqual(journal.Journal.synced - before, 2)

    # -- one file per scene --------------------------------------------------

    def test_another_scene_ends_the_file_and_starts_one(self):
        self.ask("run_command", {"command": "test.cube"})
        first = files()[0]
        self.fake.scene.name = "Untitled (2)"
        answer = self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(answer["journal"], 1)
        self.assertEqual(len(files()), 2)
        old = records(first)
        self.assertEqual(old[-1]["event"], "end")
        self.assertEqual(old[-1]["why"], "another scene")
        new = [f for f in files() if f != first][0]
        self.assertIn("untitled-2", new)
        self.assertEqual([r["event"] for r in records(new)], ["header", "entry"])

    def test_a_save_by_the_agent_keeps_the_file(self):
        self.ask("run_command", {"command": "test.cube"})
        folder = os.path.join(os.environ["MODO_TOOLS_HOME"], "saved")
        os.makedirs(folder)
        path = os.path.join(folder, "cube.lxo").replace("\\", "/")
        answer = self.ask("save_scene", {"path": path})
        self.assertTrue(answer["ok"], answer)
        self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(len(files()), 1)
        events = [r["event"] for r in records(files()[0])]
        self.assertEqual(events, ["header", "entry", "entry", "save", "entry"])

    def test_the_same_file_with_other_slashes_is_the_same_scene(self):
        # A probe (2026-11-08): after scene.saveAs Modo gives the file with /,
        # after File > Open of the same file with \.
        self.fake.scene.name = "cube.lxo"
        self.fake.scene.filename = "C:/models/cube.lxo"
        self.ask("run_command", {"command": "test.cube"})
        self.fake.scene.filename = "C:\\Models\\cube.lxo"
        self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(len(files()), 1)

    def test_a_scene_the_kit_cant_read_starts_no_file(self):
        self.ask("run_command", {"command": "test.cube"})
        self.fake.scene.unreadable = True
        answer = self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(answer["journal"], 2)
        self.assertEqual(len(files()), 1)
        status = self.ask("status")["result"]["journal"]
        self.assertIn("scene", status["note"])

    def test_stop_ends_the_file(self):
        self.ask("run_command", {"command": "test.cube"})
        sys.modules["modotools"].stop()
        self.assertEqual(records(files()[0])[-1],
                         dict(records(files()[0])[-1], event="end", why="stop"))

    def test_status_names_the_journal(self):
        self.assertIsNone(self.ask("status")["result"]["journal"]["name"])
        self.ask("run_command", {"command": "test.cube"})
        status = self.ask("status")["result"]["journal"]
        self.assertEqual(status["name"], files()[0][:-len(".jsonl")])
        self.assertEqual(status["entries"], 1)
        self.assertIsNone(status["error"])


    # -- later checks ------------------------------------------

    def test_a_command_written_in_python_is_one_line(self):
        # Its own lx.eval lines are covered by the outer line, as its modo
        # writes are: replaying both would run them twice.
        fake = self.fake
        base = fake.builtin["test.cube"].__bases__[0]

        class Twice(base):
            def basic_Execute(self, msg, flags):
                import lx
                lx.eval("test.cube")
                lx.eval("test.cube")

        fake.builtin["test.twice"] = Twice
        answer = self.ask("run_command", {"command": "test.twice"})
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["commands"], ["!!test.twice"])

    def test_a_line_that_failed_is_kept_apart(self):
        answer = self.py("try:\n    lx.eval('test.fails 1')\n"
                         "except Exception:\n    pass\n"
                         "lx.eval('test.cube')")
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["commands"], ["!!test.cube"])
        self.assertEqual(entry["failed"], ["!!test.fails 1"])

    def test_set_channels_holds_its_values_and_is_not_untraced(self):
        # An mt_set_channels entry is the values it set, read
        # back. Its Channel.set is the op's own, not a write the entry misses.
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"pos.Y": 0.5}})
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["untraced"], [])
        self.assertEqual(entry["set"], {"pos.Y": {"old": 0.0, "new": 0.5,
                                                  "item": "translation005"}})

    def test_a_failed_save_writes_no_save_record(self):
        self.ask("run_command", {"command": "test.cube"})
        answer = self.ask("save_scene", {"path": "Z:/nowhere/cube.lxo"})
        self.assertFalse(answer["ok"], answer)
        events = [r["event"] for r in records(files()[0])]
        self.assertNotIn("save", events)

    def test_a_save_in_code_that_then_failed_keeps_the_file(self):
        # The save happened before the error: the scene has its new name,
        # and the journal goes on in the same file.
        folder = os.path.join(os.environ["MODO_TOOLS_HOME"], "saved")
        os.makedirs(folder)
        path = os.path.join(folder, "cube.lxo").replace("\\", "/")
        self.ask("run_command", {"command": "test.cube"})
        answer = self.py("lx.eval('scene.saveAs filename:{%s}')\n"
                         "raise ValueError('after the save')" % path)
        self.assertFalse(answer["ok"], answer)
        self.ask("run_command", {"command": "test.cube"})
        self.assertEqual(len(files()), 1)
        events = [r["event"] for r in records(files()[0])]
        self.assertIn("save", events)

    def test_a_refused_set_channels_writes_nothing(self):
        # bad_channel, no_such_item: "Nothing was written", as bad_request.
        answer = self.ask("set_channels", {"item": "mesh001",
                                           "channels": {"nope": 1}})
        self.assertFalse(answer["ok"], answer)
        self.assertNotIn("journal", answer)
        answer = self.ask("set_channels", {"item": "nothing999",
                                           "channels": {"pos.X": 1}})
        self.assertFalse(answer["ok"], answer)
        self.assertNotIn("journal", answer)
        self.assertEqual(files(), [])


class FactsTest(JournalBase):
    """Facts records, and the header's start facts."""

    def setUp(self):
        JournalBase.setUp(self)
        import fakelx
        from test_meshmath import CUBE
        cube = self.fake.scene.find("mesh001")
        cube.mesh = fakelx.FakeMesh(CUBE["positions"], CUBE["polygons"],
                                    CUBE["types"], CUBE["edges"])
        cube.world = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                      (2.0, 0.0, 0.0))

    CUBE_FACTS = {"name": "Cube", "id": "mesh001", "points": 8,
                  "polygons": 6, "box": [[1.5, -0.5, -0.5], [2.5, 0.5, 0.5]]}

    def test_the_header_holds_the_start_facts(self):
        self.ask("run_command", {"command": "test.cube"})
        header = records(files()[0])[0]
        # The scene as it was before the first entry: test.cube's mesh isn't
        # in it.
        self.assertEqual(header["facts"], [self.CUBE_FACTS])

    def test_journal_facts_writes_a_record_and_adds_no_step(self):
        self.ask("run_command", {"command": "test.cube"})
        steps = len(self.fake.undo_steps)
        answer = self.ask("journal_facts")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["facts"], 1)
        self.assertEqual(answer["result"]["meshes"], 2)
        self.assertNotIn("journal", answer)
        self.assertEqual(len(self.fake.undo_steps), steps)
        facts = records(files()[0])[-1]
        self.assertEqual(facts["event"], "facts")
        self.assertEqual(facts["after"], 1)
        self.assertEqual(facts["meshes"][0], self.CUBE_FACTS)
        self.assertEqual(facts["meshes"][1]["points"], 0)
        self.assertIsNone(facts["meshes"][1]["box"])
        self.assertEqual(len(self.entries()), 1)
        # The next call is entry 2: facts are no entry.
        self.assertEqual(self.ask("run_command", {"command": "test.cube"})
                         ["journal"], 2)

    def test_no_journal_no_facts_record(self):
        answer = self.ask("journal_facts")
        self.assertTrue(answer["ok"], answer)
        self.assertIsNone(answer["result"]["facts"])
        self.assertIn("no journal", answer["result"]["note"])
        self.assertEqual(files(), [])

    def test_another_scene_gets_no_facts_record(self):
        self.ask("run_command", {"command": "test.cube"})
        self.fake.scene.name = "Untitled (2)"
        answer = self.ask("journal_facts")
        self.assertIsNone(answer["result"]["facts"])
        self.assertIn("another scene", answer["result"]["note"])
        self.assertEqual([r["event"] for r in records(files()[0])],
                         ["header", "entry"])

    def test_a_scene_too_big_gives_no_facts(self):
        ops_mesh = sys.modules["modotools.ops_mesh"]
        self.addCleanup(setattr, ops_mesh, "FACTS_MAX_POINTS",
                        ops_mesh.FACTS_MAX_POINTS)
        ops_mesh.FACTS_MAX_POINTS = 4
        self.ask("run_command", {"command": "test.cube"})
        header = records(files()[0])[0]
        self.assertIsNone(header["facts"])
        self.assertIn("8 points", header["facts_note"])
        answer = self.ask("journal_facts")
        self.assertIsNone(answer["result"]["facts"])
        self.assertIn("8 points", answer["result"]["note"])
        self.assertEqual([r["event"] for r in records(files()[0])],
                         ["header", "entry"])


class UndoTest(JournalBase):
    """The markers and the action loop tell the journal. A probe
    (2026-11-08): Ctrl+Z four steps down reaches a committed action's
    markers, one by one, and Ctrl+Shift+Z gives one back."""

    def events(self):
        return [(r["event"], r.get("n")) for r in records(files()[-1])
                if r["event"] not in ("header", "entry")]

    def cube(self):
        return self.ask("run_command", {"command": "test.cube",
                                        "purpose": "a cube"})

    def test_erics_undo_and_redo_are_written_by_entry(self):
        self.cube()
        self.cube()
        self.cube()
        self.fake.undo()
        self.fake.undo()
        self.fake.redo()
        self.assertEqual(self.events(), [("undone", 3), ("undone", 2),
                                         ("redone", 2)])

    def test_a_marker_with_no_entry_writes_nothing_and_logs_once(self):
        self.cube()
        logged = []
        self.kit.journal._log = logged.append
        # Refused after its marker was recorded: a step, and no entry.
        refused = self.ask("run_python", {"code": "RESULT = 1"})
        self.assertEqual(refused["error"]["type"], "bad_request")
        # The kit rolled its step back at once; the user's Ctrl+Shift+Z brings
        # it back, and Ctrl+Z takes it again: its marker has no entry.
        self.assertTrue(self.fake.redo())
        self.assertTrue(self.fake.undo())
        self.assertEqual(self.events(), [])
        self.assertEqual(len(logged), 0)
        # A marker the journal never heard of at all: one line, once.
        self.kit.journal.marker(9999, "reversed")
        self.kit.journal.marker(9999, "forward")
        self.assertEqual(len(logged), 1)
        self.assertIn("9999", logged[0])
        self.assertEqual(self.events(), [])

    def test_closing_the_scene_writes_nothing(self):
        # File > Close of a scene with markers on its stack, and File >
        # New, called no Reverse; nor did a Ctrl+Z in the new scene.
        self.cube()
        self.cube()
        before = records(files()[-1])
        self.fake.close_scene()
        self.assertFalse(self.fake.undo())
        self.assertEqual(records(files()[-1]), before)

    def test_a_failed_marker_write_never_raises_into_undo(self):
        self.cube()
        self.kit.journal.where = os.path.join(journals(), "missing", "\0bad")
        self.assertTrue(self.fake.undo())
        self.assertIsNotNone(self.kit.journal.error)

    def test_an_action_writes_begin_its_entries_and_commit(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        self.cube()
        self.ask("commit")
        recs = records(files()[-1])
        self.assertEqual([r["event"] for r in recs],
                         ["header", "action_begin", "entry", "entry",
                          "action_commit"])
        self.assertEqual(recs[1]["label"], "Raise the top")
        self.assertEqual([r["action"] for r in recs[2:4]], ["Raise the top"] * 2)
        self.assertEqual((recs[4]["label"], recs[4]["steps"], recs[4]["reason"]),
                         ("Raise the top", 2, "commit"))

    def test_a_begin_waits_for_the_actions_first_entry(self):
        # written at once, a begin went to the open file even
        # when the user had moved to another scene, and that journal showed an
        # action "open at the crash" with no entries.
        self.cube()
        first = files()[0]
        self.ask("begin", {"label": "Raise the top"})
        self.assertEqual(records(first)[-1]["event"], "entry")
        self.fake.scene.name = "Untitled (2)"
        self.cube()
        self.ask("commit")
        self.assertEqual([r["event"] for r in records(first)],
                         ["header", "entry", "end"])
        new = [f for f in files() if f != first][0]
        self.assertEqual([r["event"] for r in records(new)],
                         ["header", "action_begin", "entry", "action_commit"])

    def test_an_action_with_no_entry_writes_nothing(self):
        self.cube()
        before = records(files()[0])
        self.ask("begin", {"label": "Look only"})
        self.ask("commit")
        self.ask("begin", {"label": "Look again"})
        self.ask("rollback")
        self.assertEqual(records(files()[0]), before)

    def test_an_action_across_two_scenes_ends_in_both_files(self):
        # the commit went only to the file open at the end,
        # so the first scene's entries stayed "open at the crash" for good.
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        first = files()[0]
        self.fake.scene.name = "Untitled (2)"
        self.cube()
        self.ask("commit")
        new = [f for f in files() if f != first][0]
        self.assertEqual([r["event"] for r in records(first)],
                         ["header", "action_begin", "entry", "end",
                          "action_commit"])
        self.assertEqual([r["event"] for r in records(new)],
                         ["header", "entry", "action_commit"])

    def test_a_rollback_writes_its_record_and_each_step_it_undid(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        self.cube()
        self.ask("rollback")
        self.assertEqual(self.events(), [
            ("action_begin", None), ("action_rollback", None),
            ("rolled_back", 2), ("rolled_back", 1)])

    def test_erics_step_left_on_top_is_not_rolled_back(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        self.cube()
        result = self.ask("rollback")["result"]
        self.assertEqual((result["rolled_back"], result["left"]), (1, 1))
        # The user's edit between the two calls is a gap before the second
        #.
        self.assertEqual(self.events(), [
            ("action_begin", None), ("gap", None), ("action_rollback", None),
            ("rolled_back", 2)])

    def test_a_failed_call_in_an_action_rolls_back_every_step_once(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        answer = self.py("lx.eval('test.cube')\nraise ValueError('stop')")
        self.assertEqual(answer["undo"]["rolled_back"], 1)
        self.assertEqual(self.events(), [
            ("action_begin", None), ("action_rollback", None),
            ("rolled_back", 2), ("rolled_back", 1)])

    def test_a_single_rollback_is_not_an_undo(self):
        answer = self.py("lx.eval('test.cube')\nraise ValueError('half')")
        self.assertEqual(answer["undo"], {"rolled_back": True})
        self.assertEqual(self.events(), [("rolled_back", 1)])

    def test_every_step_undone_closes_the_action_with_a_commit_record(self):
        self.ask("begin", {"label": "Raise the top"})
        self.cube()
        self.fake.undo()
        refused = self.cube()
        self.assertEqual(refused["error"]["type"], "action_undone")
        self.assertEqual(self.events(), [
            ("action_begin", None), ("undone", 1), ("action_commit", None)])
        self.assertEqual(records(files()[-1])[-1]["reason"], "undone")


class KeepTest(unittest.TestCase):
    def test_the_kit_keeps_the_100_newest_at_start(self):
        support.temp_home(self)
        folder = journals()
        os.makedirs(folder)
        names = ["2026-11-%02d_%04d-untitled.jsonl" % (1 + i // 60, i % 60)
                 for i in range(101)]
        for name in names:
            with open(os.path.join(folder, name), "w") as handle:
                handle.write("{}\n")
        # Not one of ours: never deleted.
        with open(os.path.join(folder, "notes.txt"), "w") as handle:
            handle.write("The user's\n")
        fake, _module = support.load_plugin()
        fake.run_command("modoTools.start port:0")
        self.addCleanup(support.stop_running_kit)
        left = sorted(os.listdir(folder))
        self.assertEqual(len(left), 101)
        self.assertNotIn(names[0], left)
        self.assertIn(names[1], left)
        self.assertIn("notes.txt", left)


if __name__ == "__main__":
    unittest.main()
