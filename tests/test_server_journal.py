"""On the server: mt_journal reads the journal files itself,
as mt_op_log reads the op log, so it works with Modo closed; changing
answers and mt_status name the journal."""

import asyncio
import json
import os
import re
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import journal, server
from test_server import InThread, free_port

FIRST = "2026-11-08_1845-untitled"


def write(name, records, tail=""):
    folder = os.path.join(os.environ["MODO_TOOLS_HOME"], "journals")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + ".jsonl"), "w", encoding="utf-8") as h:
        for record in records:
            h.write(json.dumps(record) + "\n")
        h.write(tail)


THREE = [
    {"event": "header", "journal": 1, "kit": "0.2.0", "modo": "17.1v1",
     "scene": {"name": "Untitled", "file": None, "unsaved": False}},
    {"event": "entry", "n": 1, "id": "a", "tool": "run_command",
     "purpose": "a cube", "args": {"command": "prim.cube sizeX:1"},
     "commands": ["!!prim.cube sizeX:1"], "untraced": [], "ok": True,
     "undo": {"step": 1}},
    {"event": "entry", "n": 2, "id": "b", "tool": "run_python",
     "purpose": "Bevel and raise", "args": {"code": "..."},
     "commands": ["!!tool.set poly.bevel on"], "untraced": ["Channel.set"],
     "ok": True, "undo": {"step": 1}},
    {"event": "entry", "n": 3, "id": "c", "tool": "run_command",
     "purpose": "", "args": {"command": "poly.bevel shift:abc"},
     "commands": ["!!poly.bevel shift:abc"], "untraced": [], "ok": False,
     "undo": {"step": 1}},
    {"event": "rolled_back", "n": 3},
]


class ReaderTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)

    def test_entries_with_their_states(self):
        write(FIRST, THREE)
        text = server.mt_journal_text(FIRST)
        self.assertIn("journal %s, started in Untitled (not saved), "
                      "3 entries:" % FIRST, text)
        lines = text.splitlines()
        one = next(l for l in lines if l.strip().startswith("1 "))
        two = next(l for l in lines if l.strip().startswith("2 "))
        three = next(l for l in lines if l.strip().startswith("3 "))
        self.assertIn("prim.cube", one)
        self.assertTrue(one.endswith("in the scene"), one)
        self.assertIn('"Bevel and raise"', two)
        self.assertTrue(two.endswith("in the scene, untraced: Channel.set"), two)
        self.assertTrue(three.endswith("rolled back"), three)
        self.assertIn("replay: %s" % journal.replay_path(FIRST), text)
        self.assertIn("checks: none;  untraced entries: 1 (2)", text)
        self.assertTrue(os.path.exists(journal.replay_path(FIRST)))

    def test_a_half_written_line_is_skipped(self):
        write(FIRST, THREE[:2], tail='{"event": "entry", "n": 2, "id"')
        records = journal.read(FIRST)
        self.assertEqual([r["event"] for r in records], ["header", "entry"])
        self.assertIn("1 entry:", server.mt_journal_text(FIRST))

    def test_unsure_and_failed_states(self):
        write(FIRST, THREE[:3] + [
            dict(THREE[3], n=3, id="c"), {"event": "unsure", "n": 3},
            dict(THREE[3], n=4, id="d")])
        states = [e["state"] for e in journal.entries(journal.read(FIRST))]
        self.assertEqual(states, ["in the scene",
                                  "in the scene, untraced: Channel.set",
                                  "rollback unsure", "failed, not rolled back"])

    def test_unknown_name_is_bad_request_with_the_newest(self):
        for i in range(7):
            write("2026-11-0%d_1200-untitled" % (i + 1), THREE[:2])
        text = server.mt_journal_text("nope")
        self.assertTrue(text.startswith("bad_request:"), text)
        newest = ["2026-11-0%d_1200-untitled" % i for i in (7, 6, 5, 4, 3)]
        self.assertIn(", ".join(newest), text)
        self.assertNotIn("2026-11-02_1200", text)

    def test_no_journal_yet_is_not_an_error(self):
        text = server.mt_journal_text("")
        self.assertTrue(text.startswith("No journal yet"), text)

    def test_empty_name_with_modo_closed_is_the_newest(self):
        write(FIRST, THREE)
        write("2026-11-09_0900-cube", THREE[:2])
        text = asyncio.run(server.mt_journal_async(
            "", bridge=bridge_mod.Bridge(port=free_port())))
        self.assertIn("journal 2026-11-09_0900-cube,", text)
        self.assertIn("Modo isn't reachable: no new facts were taken", text)
        self.assertTrue(os.path.exists(
            journal.replay_path("2026-11-09_0900-cube")))
        self.assertIn("newest: 2026-11-09_0900-cube, %s" % FIRST, text)

    def test_an_ended_journal_says_why(self):
        write(FIRST, THREE + [{"event": "end", "why": "another scene"}])
        self.assertIn("ended: another scene",
                      server.mt_journal_text(FIRST))


HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals")
HAND = ["2026-11-08_1845-untitled", "2026-11-08_1200-cube"]


def hand(name, suffix):
    with open(os.path.join(HERE, name + suffix), "rb") as handle:
        return handle.read()


def entry(n, tool="run_command", ok=True, **fields):
    out = {"event": "entry", "n": n, "id": "x%d" % n, "tool": tool,
           "purpose": "p", "args": {"command": "item.create mesh"},
           "commands": ["!!item.create mesh"], "untraced": [], "ok": ok,
           "undo": {"step": 1}}
    out.update(fields)
    return out


SENT_ARG = "item.channel pos.X ?+0.5 item:translation005"

HEADER = {"event": "header", "journal": 1, "kit": "0.2.0", "modo": "17.1v1",
          "scene": {"name": "Untitled", "file": None, "unsaved": False}}


class BuildTest(unittest.TestCase):
    """The replay, built from the journal file alone."""

    def build(self, records):
        return journal.build("2026-11-08_1845-untitled", records)

    def test_each_hand_journal_builds_to_its_replay(self):
        support.temp_home(self)
        folder = os.path.join(os.environ["MODO_TOOLS_HOME"], "journals")
        os.makedirs(folder)
        for name in HAND:
            with self.subTest(name=name):
                with open(os.path.join(folder, name + ".jsonl"), "wb") as h:
                    h.write(hand(name, ".jsonl"))
                text = journal.build(name, journal.read(name))
                self.assertEqual(text.encode("utf-8"), hand(name, ".py"))

    def test_the_built_files_compile(self):
        for name in HAND:
            compile(hand(name, ".py").decode("utf-8"), name, "exec")

    def test_a_rolled_back_entry_is_a_comment(self):
        text = self.build([HEADER, entry(1), {"event": "rolled_back", "n": 1}])
        self.assertIn("# 1  run_command  item.create  rolled back\n", text)
        self.assertNotIn("lx.eval('!!item.create mesh')", text)
        self.assertIn("_done(0)", text)

    def test_lines_that_save_open_or_export_are_comments(self):
        for line in ("!!scene.save", "!!scene.saveAs filename:C:/a.lxo",
                     "!!scene.open C:/a.lxo", "!!scene.new", "scene.close",
                     "!!scene.revert", "!!app.quit",
                     "!!scene.saveAs filename:C:/a.fbx format:fbx",
                     "!!export.selected C:/a.obj"):
            with self.subTest(line=line):
                text = self.build([HEADER, entry(1, commands=[line])])
                self.assertIn("# not replayed: %s\n" % line, text)
                self.assertNotIn("lx.eval(%r)" % line, text)
        text = self.build([HEADER, entry(1, commands=["!!scene.saveThing"])])
        self.assertIn("lx.eval('!!scene.saveThing')", text)

    def test_a_line_without_its_bangs_gets_them(self):
        text = self.build([HEADER, entry(1, commands=["item.create mesh"])])
        self.assertIn("lx.eval('!!item.create mesh')", text)

    def test_a_save_scene_entry_is_a_comment(self):
        text = self.build([HEADER, entry(1, tool="save_scene",
                                         args={"path": "C:/a.lxo"},
                                         commands=[])])
        self.assertIn("# 1  save_scene  C:/a.lxo  not replayed\n", text)

    def test_an_untraced_entry_is_its_code(self):
        code = "import modo\nmodo.Scene().item('Cube').position.x.set(1)\n"
        text = self.build([HEADER, entry(
            1, tool="run_python", args={"code": code, "purpose": "p"},
            commands=["!!select.drop item"], untraced=["Channel.set"])])
        self.assertIn("_code(r'''%s''', 1)" % code, text)
        self.assertNotIn("lx.eval('!!select.drop item')", text)
        self.assertIn("# Untraced, run as their code: 1 (1).", text)

    BACKDROP = ["!!clip.addStill filename:{C:/refs/side.png}",
                "!!item.create type:{backdrop} name:{side}",
                "!!item.channel name:{projection} value:{left}"]

    def test_a_backdrop_entry_replays_its_commands_and_its_link(self):
        # No command links a backdrop to its clip on 17.1, so
        # the kit's op links it through the graph, and the entry holds it.
        text = self.build([HEADER, entry(
            1, tool="backdrop", args={"path": "C:/refs/side.png",
                                      "view": "left", "width_m": 0.1},
            commands=self.BACKDROP, link=["backdrop003", "side:videoStill001"],
            clip_file="C:/refs/side.png")])
        self.assertIn("# 1  backdrop  C:/refs/side.png\n", text)
        for line in self.BACKDROP:
            self.assertIn("lx.eval(%r)" % line, text)
        link = text.index("AddLink")
        self.assertGreater(link, text.index("item.channel"))
        self.assertIn("'backdropClip'", text)
        # The newest of each, as the lines just made them, not by id: Modo
        # may give them other ids in the replay's scene.
        self.assertIn("items('backdrop')[-1]", text[link - 500:link])
        # The clip by its file, since the scene may have had it already
        #.
        self.assertIn("FILE = 'C:/refs/side.png'", text[link - 500:link])
        self.assertNotIn("backdrop003", text[link - 500:link + 300])
        self.assertNotIn("Untraced, run as their code", text)
        compile(text, "replay", "exec")

    def test_code_a_raw_string_cant_hold_is_quoted(self):
        for code in ("x = '''a'''\n", "x = 1  # ends in \\"):
            with self.subTest(code=code):
                text = self.build([HEADER, entry(
                    1, tool="run_python", args={"code": code, "purpose": "p"},
                    commands=[], untraced=["lx.object"])])
                self.assertIn("_code(%r, 1)" % code, text)
                compile(text, "replay", "exec")

    def test_a_failed_entry_not_rolled_back_replays_its_trace(self):
        text = self.build([HEADER, entry(
            1, tool="run_python", ok=False,
            args={"code": "lx.eval('item.create mesh')\nmodo.x()",
                  "purpose": "p"},
            untraced=["Channel.set"], failed=["!!poly.bevel shift:abc"])])
        self.assertIn("lx.eval('!!item.create mesh')", text)
        self.assertNotIn("_code(", text.split("_START =")[1])
        self.assertIn("# failed: !!poly.bevel shift:abc", text)
        self.assertIn("# Failed and not rolled back, their trace only: 1 (1).",
                      text)

    def test_a_half_last_line_is_skipped(self):
        support.temp_home(self)
        write(FIRST, [HEADER, entry(1)], tail='{"event": "entry", "n": 2, "i')
        text = journal.build(FIRST, journal.read(FIRST))
        self.assertIn("# 1 entry: 1 in this file.", text)
        self.assertNotIn("# 2 ", text)

    def test_facts_records_are_checks_where_they_fall(self):
        facts = [{"name": "Cube", "id": "mesh001", "points": 8,
                  "polygons": 6, "box": [[0, 0, 0], [1, 1, 1]]}]
        text = self.build([dict(HEADER, facts=facts), entry(1),
                           {"event": "facts", "after": 1, "meshes": facts},
                           entry(2)])
        body = text.split("_check(START, after=0)")[1]
        self.assertLess(body.index("# 1 "), body.index("_check(FACTS_1, after=1)"))
        self.assertLess(body.index("_check(FACTS_1, after=1)"), body.index("# 2 "))
        self.assertIn("# Checks: start, after 1.", text)


class CheckTest(unittest.TestCase):
    """The replay's own _check and _code, run on the fake Modo."""

    def setUp(self):
        import fakelx
        from test_meshmath import CUBE
        self.fake, self.kit = support.start_kit(self)
        cube = self.fake.scene.add("mesh001", "Cube")
        cube.mesh = fakelx.FakeMesh(CUBE["positions"], CUBE["polygons"],
                                    CUBE["types"], CUBE["edges"])
        self.facts = [{"name": "Cube", "id": "mesh001", "points": 8,
                       "polygons": 6,
                       "box": [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]]}]

    def run_replay(self, records):
        text = journal.build("2026-11-08_1845-untitled", records)
        space = {"__name__": "__replay__"}
        exec(compile(text, "replay", "exec"), space)
        return space

    def test_the_same_facts_pass(self):
        self.run_replay([dict(HEADER, facts=self.facts),
                         entry(1, commands=["!!deformer.freeze"]),
                         {"event": "facts", "after": 1, "meshes": self.facts}])
        self.assertIn("replay: 1 entries run; checks start, after 1: same "
                      "counts and bounding boxes", self.fake.event_log)

    def test_a_differing_mesh_is_named_with_its_entry(self):
        later = [dict(self.facts[0], points=12,
                      box=[[-0.5, -0.5, -0.5], [0.5, 0.75, 0.5]])]
        with self.assertRaises(RuntimeError) as caught:
            self.run_replay([dict(HEADER, facts=self.facts),
                             entry(1, commands=["!!deformer.freeze"]),
                             {"event": "facts", "after": 1, "meshes": later}])
        text = str(caught.exception)
        self.assertTrue(text.startswith("replay stopped: check after 1: "
                                        "Cube (mesh001) has 8 points, the "
                                        "journal says 12"), text)
        self.assertIn("Cube (mesh001)'s box is (-0.5, -0.5, -0.5) to "
                      "(0.5, 0.5, 0.5), the journal says (-0.5, -0.5, -0.5) "
                      "to (0.5, 0.75, 0.5)", text)
        self.assertIn(text, self.fake.event_log)

    def test_a_mesh_with_another_id_and_its_name_is_the_same_mesh(self):
        # A Modo check: a rolled-back action's meshes used up ids, so
        # the replay, which leaves them out, gives the next mesh another id
        # (mesh031 for mesh039) with the same name, counts and box.
        moved = [dict(self.facts[0], id="mesh039")]
        self.run_replay([dict(HEADER, facts=self.facts),
                         entry(1, commands=["!!deformer.freeze"]),
                         {"event": "facts", "after": 1, "meshes": moved}])
        self.assertIn("replay: 1 entries run; checks start, after 1: same "
                      "counts and bounding boxes", self.fake.event_log)

    def test_an_id_the_journal_gave_another_mesh_pairs_by_name(self):
        # ids drift by any amount when steps are left out, so
        # the replay's Cube can hold the id the journal gave the Lid. Paired
        # by id first, the check said "Lid (mesh001) is called Cube".
        import fakelx
        from test_meshmath import CUBE
        lid = self.fake.scene.add("mesh002", "Lid")
        lid.mesh = fakelx.FakeMesh(CUBE["positions"], CUBE["polygons"],
                                   CUBE["types"], CUBE["edges"])
        swapped = [dict(self.facts[0], name="Lid"),
                   dict(self.facts[0], id="mesh002")]
        self.run_replay([HEADER, entry(1, commands=["!!deformer.freeze"]),
                         {"event": "facts", "after": 1, "meshes": swapped}])
        self.assertIn("replay: 1 entries run; check after 1: same counts "
                      "and bounding boxes", self.fake.event_log)

    def test_stale_facts_dont_stop_the_replay(self):
        # Facts after entry 2 held its edit; mt_undo_last then undid it.
        later = [dict(self.facts[0], points=12)]
        space = self.run_replay([
            dict(HEADER, facts=self.facts),
            entry(1, commands=["!!deformer.freeze"]),
            entry(2, commands=["!!deformer.freeze"]),
            {"event": "facts", "after": 2, "meshes": later},
            {"event": "undone", "n": 2, "by": "agent"}])
        self.assertEqual(space["_checked"], [0])

    def test_another_id_with_other_facts_still_differs(self):
        moved = [dict(self.facts[0], id="mesh039", points=12)]
        with self.assertRaises(RuntimeError) as caught:
            self.run_replay([dict(HEADER, facts=self.facts),
                             entry(1, commands=["!!deformer.freeze"]),
                             {"event": "facts", "after": 1, "meshes": moved}])
        self.assertIn("Cube (mesh039) has 8 points, the journal says 12",
                      str(caught.exception))

    def test_the_wrong_start_stops_before_any_line(self):
        start = [{"name": "Mesh", "id": "mesh002", "points": 0,
                  "polygons": 0, "box": None}]
        with self.assertRaises(RuntimeError) as caught:
            self.run_replay([dict(HEADER, facts=start),
                             entry(1, commands=["!!test.cube"])])
        self.assertEqual(str(caught.exception),
                         "replay stopped: this isn't the scene the journal "
                         "began from (File > New: 1 mesh, Mesh, 0 points; "
                         "here: 1 mesh, Cube, 8 points)")
        self.assertNotIn("test.cube", self.fake.ran)

    def test_code_skips_the_lines_never_replayed(self):
        code = ("import lx\nlx.eval('scene.saveAs filename:C:/a.lxo')\n"
                "lx.command('scene.save')\nlx.eval('test.cube')\n")
        before = len(self.fake.ran)
        self.run_replay([HEADER, entry(
            1, tool="run_python", args={"code": code, "purpose": "p"},
            commands=[], untraced=["lx.object"])])
        self.assertEqual(self.fake.ran[before:], ["test.cube"])
        self.assertIn("replay: entry 1: not replayed: scene.saveAs "
                      "filename:C:/a.lxo", self.fake.event_log)


class ReviewTest(unittest.TestCase):
    """What the replay builder got wrong."""

    def build(self, records):
        return journal.build("2026-11-08_1845-untitled", records)

    def test_an_unsure_untraced_entry_replays_its_trace_not_its_code(self):
        # unsure follows a failed call only: its code raised once and would
        # raise again, and Modo would undo the whole replay.
        code = "lx.eval('item.create mesh')\nraise ValueError('half')\n"
        records = [HEADER, entry(1, tool="run_python", ok=False,
                                 args={"code": code, "purpose": "p"},
                                 untraced=["Channel.set"]),
                   {"event": "unsure", "n": 1}]
        text = self.build(records)
        self.assertNotIn("_code(", text.split("_START =")[1])
        self.assertIn("lx.eval('!!item.create mesh')", text)
        self.assertNotIn("# Untraced, run as their code", text)
        self.assertEqual(journal.summary(records)[1], [])

    def test_text_with_a_line_break_stays_in_its_comment(self):
        evil = "a\nlx.eval('!!scene.close')\n"
        records = [dict(HEADER, scene={"name": "U", "file": "C:/a\nb.lxo",
                                       "unsaved": False}),
                   entry(1, tool="run_python", purpose=evil,
                         args={"code": "pass", "purpose": evil},
                         commands=["!!item.create mesh"]),
                   entry(2, tool="set_channels", args={"item": evil,
                         "channels": {"pos.X": 1}}, commands=[],
                         set={"pos.X": {"old": 0, "new": 1}}),
                   entry(3, tool="save_scene", args={"path": evil},
                         commands=[]),
                   {"event": "save", "path": evil},
                   {"event": "end", "why": evil}]
        text = self.build(records)
        compile(text, "replay", "exec")
        self.assertNotIn("\nlx.eval('!!scene.close')", text)

    def test_code_with_a_nul_is_quoted(self):
        text = self.build([HEADER, entry(
            1, tool="run_python", args={"code": "x = 1\0", "purpose": "p"},
            commands=[], untraced=["lx.object"])])
        compile(text, "replay", "exec")


def in_action(n, label="Raise the top", step=1, **fields):
    return entry(n, undo={"action": label, "step": step}, action=label,
                 commands=["!!item.create locator%d" % n], **fields)


BEGIN = {"event": "action_begin", "label": "Raise the top"}


def commit(steps=2, reason="commit"):
    return {"event": "action_commit", "label": "Raise the top",
            "steps": steps, "reason": reason}


def rollback(steps=2):
    return {"event": "action_rollback", "label": "Raise the top",
            "steps": steps}


class UndoStatesTest(unittest.TestCase):
    """Each kind of journal entry, one row each, from journal to replay."""

    def build(self, records, **kw):
        return journal.build("2026-11-08_1845-untitled", records, **kw)

    def states(self, records, **kw):
        return [e["state"] for e in journal.entries(records, **kw)]

    def body(self, text):
        return text.split("_START =")[1]

    def test_a_rolled_back_call_is_a_comment(self):
        records = [HEADER, entry(1, ok=False), {"event": "rolled_back", "n": 1}]
        self.assertEqual(self.states(records), ["rolled back"])
        text = self.build(records)
        self.assertIn("# 1  run_command  item.create  rolled back\n", text)
        self.assertNotIn("lx.eval(", self.body(text))

    def test_rollback_unsure_is_its_trace_with_a_warning(self):
        records = [HEADER, entry(1, ok=False), {"event": "unsure", "n": 1}]
        self.assertEqual(self.states(records), ["rollback unsure"])
        text = self.build(records)
        self.assertIn("# Warning: entry 1's rollback was unsure", text)
        self.assertIn("lx.eval('!!item.create mesh')", self.body(text))

    def test_a_committed_action_is_under_its_label(self):
        records = [HEADER, entry(1), BEGIN, in_action(2), in_action(3, step=2),
                   commit()]
        self.assertEqual(self.states(records), ["in the scene"] * 3)
        text = self.body(self.build(records))
        self.assertIn('# action "Raise the top": 2 steps, committed\n'
                      "# 2  run_command  item.create\n"
                      "lx.eval('!!item.create locator2')\n", text)
        self.assertIn("lx.eval('!!item.create locator3')", text)
        self.assertIn("_done(3)", text)

    def test_a_rolled_back_action_is_out_but_a_step_it_left_stays(self):
        # The user's edit sat on step 1: the loop undid 3 and 2, then stopped.
        records = [HEADER, BEGIN, in_action(1), in_action(2, step=2),
                   in_action(3, step=3), rollback(3),
                   {"event": "rolled_back", "n": 3},
                   {"event": "rolled_back", "n": 2}]
        self.assertEqual(self.states(records),
                         ["in the scene", "rolled back", "rolled back"])
        text = self.build(records)
        body = self.body(text)
        self.assertIn('# action "Raise the top": rolled back; 1 step it '
                      "couldn't reach stays in\n", body)
        self.assertIn("lx.eval('!!item.create locator1')", body)
        self.assertNotIn("locator2'", body)
        self.assertNotIn("locator3'", body)
        self.assertIn('# 2  run_command  item.create  (action "Raise the top", '
                      "rolled back)\n", body)
        self.assertIn("_done(1)", body)

    def test_erics_undo_is_left_out(self):
        records = [HEADER, entry(1), entry(2), {"event": "undone", "n": 2}]
        self.assertEqual(self.states(records), ["in the scene", "undone by the user"])
        body = self.body(self.build(records))
        self.assertIn("# 2  run_command  item.create  undone by the user\n", body)
        self.assertEqual(body.count("lx.eval('!!item.create mesh')"), 1)
        self.assertIn("_done(1)", body)
        self.assertIn("1 undone by the user", self.build(records))

    def test_the_agents_undo_is_left_out_and_said_apart(self):
        # Mt_undo_last's record carries by: agent.
        records = [HEADER, entry(1), entry(2),
                   {"event": "undone", "n": 2, "by": "agent"}]
        self.assertEqual(self.states(records),
                         ["in the scene", "undone by the agent"])
        text = self.build(records)
        body = self.body(text)
        self.assertIn("# 2  run_command  item.create  undone by the agent\n", body)
        self.assertEqual(body.count("lx.eval('!!item.create mesh')"), 1)
        self.assertIn("1 undone by the agent", text)
        self.assertNotIn("undone by the user", text)

    def test_the_agents_undo_then_erics_redo_is_in_again(self):
        records = [HEADER, entry(1), {"event": "undone", "n": 1, "by": "agent"},
                   {"event": "redone", "n": 1}]
        self.assertEqual(self.states(records), ["in the scene"])

    def test_facts_taken_before_the_undo_are_not_checked(self):
        # The review: mt_journal took facts after entry 2, mt_undo_last
        # then undid it. The replay leaves 2 out, so those facts aren't its.
        cube = [{"name": "Cube", "id": "mesh001", "points": 8, "polygons": 6,
                 "box": [[0, 0, 0], [1, 1, 1]]}]
        records = [HEADER, entry(1),
                   {"event": "facts", "after": 1, "meshes": cube},
                   entry(2), {"event": "facts", "after": 2, "meshes": cube},
                   {"event": "undone", "n": 2, "by": "agent"}, entry(3)]
        text = self.build(records)
        self.assertIn("_check(FACTS_1, after=1)", text)
        self.assertNotIn("FACTS_2", text)
        self.assertIn("# check after 2 left out: entry 2 was undone", text)
        self.assertIn("# Checks: after 1.", text)
        self.assertIn("# Not checked: after 2 (entry 2).", text)
        self.assertEqual(journal.summary(records)[0], ["after 1"])

    def test_facts_taken_again_after_the_undo_are_checked(self):
        cube = [{"name": "Cube", "id": "mesh001", "points": 8, "polygons": 6,
                 "box": [[0, 0, 0], [1, 1, 1]]}]
        records = [HEADER, entry(1), entry(2),
                   {"event": "facts", "after": 2, "meshes": cube},
                   {"event": "undone", "n": 2, "by": "agent"},
                   {"event": "facts", "after": 2, "meshes": cube}]
        text = self.build(records)
        self.assertIn("_check(FACTS_2, after=2)", text)
        self.assertNotIn("Not checked", text)

    def test_facts_taken_while_undone_are_not_checked_after_a_redo(self):
        cube = [{"name": "Cube", "id": "mesh001", "points": 8, "polygons": 6,
                 "box": [[0, 0, 0], [1, 1, 1]]}]
        records = [HEADER, entry(1), {"event": "undone", "n": 1},
                   {"event": "facts", "after": 1, "meshes": cube},
                   {"event": "redone", "n": 1}]
        text = self.build(records)
        self.assertNotIn("_check(FACTS_1", text)
        self.assertIn("# Checks: none.\n", text)
        self.assertIn("# Not checked: after 1 (entry 1).", text)

    def test_facts_taken_inside_an_action_rolled_back_are_not_checked(self):
        cube = [{"name": "Cube", "id": "mesh001", "points": 8, "polygons": 6,
                 "box": [[0, 0, 0], [1, 1, 1]]}]
        records = [HEADER, BEGIN, in_action(1),
                   {"event": "facts", "after": 1, "meshes": cube},
                   rollback(1), {"event": "rolled_back", "n": 1}]
        self.assertNotIn("_check(FACTS_1", self.build(records))

    def test_undo_then_redo_is_in_again(self):
        records = [HEADER, entry(1), {"event": "undone", "n": 1},
                   {"event": "redone", "n": 1}]
        self.assertEqual(self.states(records), ["in the scene"])
        self.assertIn("lx.eval('!!item.create mesh')",
                      self.body(self.build(records)))

    def test_an_action_open_at_a_crash_is_commented(self):
        records = [HEADER, entry(1), BEGIN, in_action(2), in_action(3, step=2)]
        self.assertEqual(self.states(records), [
            "in the scene", "action open at the crash",
            "action open at the crash"])
        body = self.body(self.build(records))
        self.assertIn('# action "Raise the top": 2 steps, open at the crash: '
                      "not replayed\n", body)
        self.assertIn('# 2  run_command  item.create  (action "Raise the top", '
                      "open at the crash)\n", body)
        self.assertNotIn("locator2'", body)
        self.assertIn("_done(1)", body)

    def test_an_action_still_open_is_commented_as_open(self):
        records = [HEADER, BEGIN, in_action(1)]
        self.assertEqual(self.states(records, open_action="Raise the top"),
                         ["action open"])
        body = self.body(self.build(records, open_action="Raise the top"))
        self.assertIn('# action "Raise the top": 1 step, open: not replayed\n',
                      body)
        self.assertIn("_done(0)", body)
        # Another label open in the kit: this one never committed.
        self.assertEqual(self.states(records, open_action="Other"),
                         ["action open at the crash"])

    def test_a_save_is_a_comment(self):
        records = [HEADER, entry(1), entry(2, tool="save_scene",
                                            args={"path": "C:/a.lxo"},
                                            commands=[])]
        self.assertEqual(self.states(records)[1], "in the scene")
        self.assertIn("# 2  save_scene  C:/a.lxo  not replayed",
                      self.build(records))

    def test_an_action_that_began_in_an_earlier_file_is_grouped(self):
        # No action_begin in this file: the entries' own action field.
        records = [HEADER, in_action(1), in_action(2, step=2), commit()]
        body = self.body(self.build(records))
        self.assertIn('# action "Raise the top": 2 steps, committed\n', body)
        self.assertIn("_done(2)", body)

    def test_two_actions_with_one_label_are_two(self):
        records = [HEADER, BEGIN, in_action(1), rollback(1),
                   {"event": "rolled_back", "n": 1},
                   BEGIN, in_action(2), in_action(3, step=2), commit()]
        self.assertEqual(self.states(records),
                         ["rolled back", "in the scene", "in the scene"])
        body = self.body(self.build(records))
        self.assertIn('# action "Raise the top": rolled back\n', body)
        self.assertIn('# action "Raise the top": 2 steps, committed\n', body)

    def test_a_rolled_back_entry_redone_by_eric_is_in(self):
        records = [HEADER, entry(1), {"event": "rolled_back", "n": 1},
                   {"event": "redone", "n": 1}]
        self.assertEqual(self.states(records), ["in the scene"])

    def test_mt_journal_lists_the_states_with_their_action(self):
        support.temp_home(self)
        write(FIRST, [HEADER, entry(1), BEGIN, in_action(2), rollback(1),
                      {"event": "rolled_back", "n": 2},
                      BEGIN, in_action(3), in_action(4, step=2), commit(),
                      entry(5), {"event": "undone", "n": 5},
                      BEGIN, in_action(6)])
        text = server.mt_journal_text(FIRST)
        lines = {l.split()[0]: l for l in text.splitlines()
                 if l[:5].strip().isdigit()}
        self.assertTrue(lines["1"].endswith("in the scene"), lines["1"])
        self.assertIn("(action 1)", lines["2"])
        self.assertTrue(lines["2"].endswith("rolled back"), lines["2"])
        self.assertIn("(action 2)", lines["4"])
        self.assertTrue(lines["4"].endswith("in the scene"), lines["4"])
        self.assertTrue(lines["5"].endswith("undone by the user"), lines["5"])
        self.assertTrue(lines["6"].endswith("action open at the crash"),
                        lines["6"])
        self.assertIn('actions: 1 "Raise the top" rolled back; 2 "Raise the '
                      'top" committed; 3 "Raise the top" open at the crash',
                      text)
        open_text = server.mt_journal_text(FIRST, open_action="Raise the top")
        self.assertIn("action open\n", open_text + "\n")


class ExactTest(unittest.TestCase):
    """The replay and mt_journal say which entries can't promise
    the same counts and boxes, and where the user edited between calls."""

    SENT = "!!item.channel pos.X ?+0.5 item:translation005"
    READ = "!!item.channel pos.X 1.5 item:translation005"

    def records(self):
        return [
            HEADER,
            entry(1, args={"command": SENT_ARG}, commands=[self.READ],
                  operators=[{"i": 0, "sent": self.SENT, "read": True}],
                  exact=True),
            {"event": "gap", "before": 2, "events": 3, "added": 1,
             "removed": 0, "items": [
                 {"id": "translation009", "name": "Position (Camera)"},
                 {"id": "locator040", "name": None}]},
            entry(2, args={"command": "tool.attr poly.bevel shift ?max"},
                  commands=["!!tool.attr poly.bevel shift ?max"],
                  operators=[{"i": 0, "read": False,
                              "sent": "!!tool.attr poly.bevel shift ?max"}],
                  exact=False),
            entry(3, tool="run_python", args={"code": "x", "purpose": "p"},
                  commands=[], untraced=["Channel.set"]),
            entry(4, commands=["!!select.element mesh001 polygon set 5"]),
            entry(5, commands=["!!test.blind ?+2"], exact=False,
                  operators=[{"i": 0, "sent": "!!test.blind ?+2",
                              "read": False}]),
            {"event": "rolled_back", "n": 5},
        ]

    def test_inexact_entries_with_why(self):
        self.assertEqual(journal.inexact(self.records()), [
            (2, "?max couldn't be read back"), (3, "run as its code"),
            (4, "selects by index")])

    def test_an_id_named_after_a_step_left_out_may_miss(self):
        # A step left out of the replay used up ids, so a later line
        # naming an item by id can miss it or hit another.
        records = [HEADER, entry(1), entry(2), {"event": "undone", "n": 2},
                   entry(3, commands=["!!select.item mesh039 set"]),
                   entry(4)]
        self.assertEqual(journal.inexact(records), [
            (3, "names an item by id after a step left out")])

    def test_a_backdrop_after_a_step_left_out_is_still_exact(self):
        # Its lines act on the selection and its link on the newest
        # items, so ids moved by a step left out don't matter.
        records = [HEADER, entry(1), {"event": "undone", "n": 1},
                   entry(2, tool="backdrop", commands=[
                       "!!item.create type:{backdrop} name:{side}",
                       "!!item.channel name:{projection} value:{left}"],
                         link=["backdrop003", "side:videoStill001"])]
        self.assertEqual(journal.inexact(records), [])

    def test_a_backdrop_that_put_a_selection_back_names_it_by_id(self):
        # exact only with nothing selected. The selection is
        # put back by id, and that line is no more exact than any other.
        records = [HEADER, entry(1), {"event": "undone", "n": 1},
                   entry(2, tool="backdrop", commands=[
                       "!!item.create type:{backdrop} name:{side}",
                       "!!select.drop type:{item}",
                       "!!select.item item:{mesh021} mode:{add}"],
                         link=["backdrop003", "side:videoStill001"])]
        self.assertEqual(journal.inexact(records), [
            (2, "names an item by id after a step left out")])

    def test_an_operator_after_a_nested_brace_value_is_found(self):
        # The kit reads {a {b} c} as one word; so does this.
        self.assertEqual(journal.operator_word(
            "!!cmd.x {a {b} ?+1} ?+2"), "?+2")

    def test_an_exact_journal_has_none(self):
        records = [HEADER, entry(1)]
        self.assertEqual(journal.inexact(records), [])
        self.assertEqual(journal.gaps(records), [])
        self.assertIn("# Exact: yes.", journal.build(FIRST, records))

    def test_the_header_lists_inexact_entries_and_gaps(self):
        text = journal.build(FIRST, self.records())
        head = text.split("import re")[0].replace("\n# ", " ")
        self.assertIn(" Not exact: 2 (?max couldn't be read back), 3 (run "
                      "as its code), 4 (selects by index).", head)
        self.assertIn(" Gaps, edits outside the agent it can't redo: before "
                      "2.", head)

    def test_the_line_read_back_replays_with_the_line_as_sent_above(self):
        body = journal.build(FIRST, self.records()).split("_START =")[1]
        self.assertIn("# as sent: %s\nlx.eval(%r)" % (self.SENT, self.READ),
                      body)
        self.assertIn("# couldn't be read back, replays relative to the "
                      "scene: !!tool.attr poly.bevel shift ?max\n"
                      "lx.eval('!!tool.attr poly.bevel shift ?max')", body)

    def test_a_gap_is_printed_where_it_falls(self):
        body = journal.build(FIRST, self.records()).split("_START =")[1]
        gap = ("_gap(2, '3 events, 1 added: Position (Camera), locator040')")
        self.assertIn(gap, body)
        self.assertLess(body.index("# 1  run_command"), body.index(gap))
        self.assertLess(body.index(gap), body.index("# 2  run_command"))

    def test_mt_journal_says_exact_or_which_entries_arent(self):
        support.temp_home(self)
        write(FIRST, self.records())
        text = server.mt_journal_text(FIRST)
        self.assertIn("exact: no: 2 (?max couldn't be read back), 3 (run as "
                      "its code), 4 (selects by index); gaps before 2", text)
        lines = text.splitlines()
        one = next(l for l in lines if l.strip().startswith("1 "))
        self.assertIn("?+0.5 read back", one)
        gap = lines[lines.index(one) + 1]
        self.assertEqual(gap.strip(), "gap: outside the agent, 3 events, 1 "
                         "added: Position (Camera), locator040")
        write(FIRST, [HEADER, entry(1)])
        self.assertIn("exact: yes", server.mt_journal_text(FIRST))


class GapReplayTest(unittest.TestCase):
    """The replay's _gap, and a check after one, run on the fake Modo."""

    def setUp(self):
        import fakelx
        from test_meshmath import CUBE
        self.fake, self.kit = support.start_kit(self)
        cube = self.fake.scene.add("mesh001", "Cube")
        cube.mesh = fakelx.FakeMesh(CUBE["positions"], CUBE["polygons"],
                                    CUBE["types"], CUBE["edges"])
        self.facts = [{"name": "Cube", "id": "mesh001", "points": 8,
                       "polygons": 6,
                       "box": [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]]}]

    def run_replay(self, records):
        text = journal.build(FIRST, records)
        exec(compile(text, "replay", "exec"), {"__name__": "__replay__"})

    def test_a_check_after_a_gap_warns_and_goes_on(self):
        later = [dict(self.facts[0], points=12)]
        self.run_replay([dict(HEADER, facts=self.facts),
                         entry(1, commands=["!!deformer.freeze"]),
                         {"event": "gap", "before": 2, "events": 1,
                          "added": 0, "removed": 0, "items": []},
                         {"event": "facts", "after": 1, "meshes": later}])
        log = self.fake.event_log
        self.assertIn("replay: gap before entry 2, outside the agent: 1 "
                      "event; not replayed", log)
        self.assertTrue(any(l.startswith("replay: check after 1 differs, "
                                         "after a gap: Cube (mesh001) has 8 "
                                         "points") for l in log), log)
        self.assertIn("replay: 1 entries run; check start: same counts and "
                      "bounding boxes; check after 1 differs after a gap; "
                      "1 gap not replayed", log)


class LongTest(unittest.TestCase):
    """A long journal keeps its newest entries and its file
    line, and says how many earlier entries it left out."""

    def setUp(self):
        support.temp_home(self)
        os.environ["MODO_TOOLS_MAX_CHARS"] = "2000"
        self.addCleanup(os.environ.pop, "MODO_TOOLS_MAX_CHARS", None)

    def test_the_newest_entries_are_kept(self):
        many = [THREE[0]] + [
            {"event": "entry", "n": n, "id": str(n), "tool": "run_command",
             "args": {"command": "prim.cube"}, "commands": [], "untraced": [],
             "ok": True} for n in range(1, 301)]
        write(FIRST, many)
        text = server.mt_journal_text(FIRST)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("300 entries", text)
        self.assertTrue(any(l.strip().startswith("300 ")
                            for l in text.splitlines()), text)
        self.assertNotIn("\n    1  ", text)
        self.assertIn("earlier entries not shown", text)
        self.assertIn("file: ", text)

    def test_gap_lines_left_out_arent_counted_as_entries(self):
        # The count was of lines, and a gap is a line.
        many = [THREE[0]]
        for n in range(1, 301):
            many.append({"event": "gap", "before": n, "events": 1,
                         "added": 0, "removed": 0, "items": []})
            many.append({"event": "entry", "n": n, "id": str(n),
                         "tool": "run_command", "args": {"command": "prim.cube"},
                         "commands": [], "untraced": [], "ok": True})
        write(FIRST, many)
        lines = server.mt_journal_text(FIRST).splitlines()
        shown = sum(1 for l in lines if re.match(r"^\s+\d+\s+run_command", l))
        said = next(l for l in lines if "earlier entries not shown" in l)
        self.assertEqual(said.strip(),
                         "... %d earlier entries not shown" % (300 - shown))


class AnswersTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def run_calls(self, make):
        job = InThread(make(self.bridge))
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_changing_answers_and_status_name_the_journal(self):
        async def calls(bridge):
            one = await server.mt_run_command_text("test.cube", bridge=bridge)
            failed = await server.mt_run_python_text(
                "lx.eval('test.cube')\nraise ValueError('half')", "half",
                bridge=bridge)
            read = await server.mt_run_python_text("RESULT = 1", "read",
                                                   read_only=True, bridge=bridge)
            status = await server.mt_status_text(bridge=bridge)
            listed = await server.mt_journal_async("", bridge=bridge)
            return one, failed, read, status, listed
        one, failed, read, status, listed = self.run_calls(calls)
        self.assertEqual(json.loads(one)["journal"], 1)
        self.assertIn("\njournal: 2", failed)
        self.assertNotIn("journal", json.loads(read))
        shown = json.loads(status)["journal"]
        self.assertEqual(shown["entries"], 2)
        self.assertIsNone(shown["error"])
        self.assertIn("journal %s," % shown["name"], listed)
        self.assertIn("2 entries:", listed)

    def test_mt_journal_takes_facts_first_and_builds(self):
        async def calls(bridge):
            await server.mt_run_command_text("test.cube", bridge=bridge)
            return await server.mt_journal_async("", bridge=bridge)
        listed = self.run_calls(calls)
        name = journal.names()[0]
        records = journal.read(name)
        self.assertEqual(records[-1]["event"], "facts")
        self.assertEqual(records[-1]["after"], 1)
        self.assertIn("facts: taken after entry 1", listed)
        self.assertIn("replay: %s" % journal.replay_path(name), listed)
        self.assertIn("checks: start, after 1;  untraced entries: 0", listed)
        with open(journal.replay_path(name), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("lx.eval('!!test.cube')", text)
        self.assertIn("_check(FACTS_1, after=1)", text)

    def test_another_journal_by_name_takes_no_facts(self):
        write(FIRST, THREE)

        async def calls(bridge):
            await server.mt_run_command_text("test.cube", bridge=bridge)
            return await server.mt_journal_async(FIRST, bridge=bridge)
        listed = self.run_calls(calls)
        self.assertIn("facts: none taken; %s isn't the current scene's "
                      "journal" % FIRST, listed)
        self.assertNotIn("facts", [r["event"] for r in journal.read(FIRST)])


class SchemaTest(unittest.TestCase):
    def test_mt_journal(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        app = server.build_server()

        async def tools():
            return {t.name: t for t in await app.list_tools()}
        tool = asyncio.run(tools())["mt_journal"]
        self.assertEqual(tool.input_schema.get("required", []), [])
        self.assertIn("name", tool.input_schema["properties"])
        self.assertIn("Modo closed", tool.description)
        # The states an agent reads in the list.
        self.assertIn("undone by the user", tool.description)
        self.assertIn("open at the crash", tool.description)


if __name__ == "__main__":
    unittest.main()
