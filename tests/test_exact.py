"""Exact replay.

A query operator (?+0.5, ?max, ?(a|b)) sets a value relative to the one there,
so its line replays somewhere else from another start. A probe (Modo 17.1,
2026-11-08): every operator tried could be read back by sending the same line
with ? in its place, so the kit writes the value read back. The user's edits
between calls are counted by a scene item listener and written as a gap
before the next entry.
"""

import math
import sys
import unittest

import support  # noqa: F401  (puts the kit on sys.path)
from test_journal import JournalBase, files, records

from modotools import guard


class OperatorTest(unittest.TestCase):
    """guard.operator: where in a line its query operator is."""

    def word(self, line):
        found = guard.operator(line)
        return None if found is None else line[found[0]:found[1]]

    def test_math_step_minmax_and_hint_operators_are_found(self):
        self.assertEqual(self.word("item.channel pos.X ?+2"), "?+2")
        self.assertEqual(self.word("!!transform.channel pos.X ?*2"), "?*2")
        self.assertEqual(self.word("tool.attr prim.cylinder sides ?-"), "?-")
        self.assertEqual(self.word("transform.channel pos.X ?+> item:loc1"),
                         "?+>")
        self.assertEqual(self.word("mesh.smoothMode ?max"), "?max")
        self.assertEqual(self.word("view3d.shadingStyle ?(advgl|vmap)"),
                         "?(advgl|vmap)")

    def test_a_named_argument_keeps_its_name(self):
        line = "item.channel value:?*2 name:pos.X"
        self.assertEqual(self.word(line), "?*2")

    def test_a_query_is_not_an_operator(self):
        self.assertIsNone(guard.operator("item.channel pos.X ?"))
        self.assertIsNone(guard.operator("item.channel name:pos.X value:?"))
        self.assertIsNone(guard.operator(
            "query sceneservice scene.name ? current"))
        self.assertIsNone(guard.operator("test.cube"))

    def test_a_question_mark_in_a_quoted_value_is_not_one(self):
        # Item.name "a ?+2 b" names the mesh a ?+2 b.
        self.assertIsNone(guard.operator('item.name "a ?+2 b" mesh'))
        self.assertIsNone(guard.operator("item.name {a ?+2 b} mesh"))
        self.assertIsNone(guard.operator('item.name name:"?+2"'))
        self.assertEqual(self.word('item.name "a b" ?+'), "?+")

    def test_the_read_back_asks_with_a_question_mark(self):
        line = "!!transform.channel pos.X ?+0.5 item:loc1"
        span = guard.operator(line)
        self.assertEqual(guard.with_word(line, span, "?"),
                         "!!transform.channel pos.X ? item:loc1")
        self.assertEqual(guard.with_word(line, span, "1.5"),
                         "!!transform.channel pos.X 1.5 item:loc1")


class ValueTest(unittest.TestCase):
    """guard.as_argument: a value read back, as it goes back in the line."""

    def test_numbers_and_plain_words(self):
        self.assertEqual(guard.as_argument(1.5), "1.5")
        self.assertEqual(guard.as_argument(0.30000000000000004),
                         "0.30000000000000004")
        self.assertEqual(guard.as_argument(32), "32")
        self.assertEqual(guard.as_argument("alwaysDisabled"), "alwaysDisabled")

    def test_text_with_spaces_is_braced(self):
        self.assertEqual(guard.as_argument("a b"), "{a b}")

    def test_one_value_for_every_item_is_that_value(self):
        # With two locators selected, ?+0.5 set both to the first's
        # value plus 0.5, and the read back was [1.5, 1.5].
        self.assertEqual(guard.as_argument([1.5, 1.5]), "1.5")

    def test_what_cant_be_written_back_is_none(self):
        self.assertIsNone(guard.as_argument([1.5, 2.0]))
        self.assertIsNone(guard.as_argument([]))
        self.assertIsNone(guard.as_argument(None))
        self.assertIsNone(guard.as_argument("a {b}"))


class ReadBackTest(JournalBase):
    """The guard reads back what an operator set, and the entry holds it."""

    LINE = "item.channel pos.X ?+0.5 item:translation005"

    def setUp(self):
        JournalBase.setUp(self)
        self.pos = self.fake.scene.find("translation005").channel("pos.X")
        self.pos.value = 1.0

    def command(self, line):
        answer = self.ask("run_command", {"command": line})
        self.assertTrue(answer["ok"], answer)
        return answer

    def test_the_value_read_back_is_in_the_line(self):
        self.command(self.LINE)
        self.command(self.LINE)
        self.assertEqual(self.pos.get(), 2.0)
        first, second = self.entries()
        self.assertEqual(first["commands"],
                         ["!!item.channel pos.X 1.5 item:translation005"])
        self.assertEqual(second["commands"],
                         ["!!item.channel pos.X 2.0 item:translation005"])
        self.assertEqual(first["operators"], [
            {"i": 0, "sent": "!!" + self.LINE, "read": True}])
        self.assertIs(first["exact"], True)

    def test_a_hint_list_is_read_back_as_the_choice(self):
        fakelx = sys.modules["fakelx"]
        item = self.fake.scene.find("translation005")
        item.channels["drawShape"] = fakelx.FakeChannel(item, "drawShape", "box")
        self.command("item.channel drawShape ?(box|sphere) item:translation005")
        entry, = self.entries()
        self.assertEqual(entry["commands"], [
            "!!item.channel drawShape sphere item:translation005"])

    def test_lx_command_is_read_back_too(self):
        answer = self.py("lx.command('item.channel', name='pos.X', "
                         "value='?*2', item='translation005')")
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["commands"], [
            "!!item.channel name:{pos.X} value:{2.0} item:{translation005}"])
        self.assertEqual(entry["operators"][0]["read"], True)

    def test_a_command_that_cant_be_asked_keeps_its_line_and_marks_it(self):
        answer = self.command("test.blind ?+2")
        self.assertNotIn("error", answer)
        entry, = self.entries()
        self.assertEqual(entry["commands"], ["!!test.blind ?+2"])
        self.assertEqual(entry["operators"], [
            {"i": 0, "sent": "!!test.blind ?+2", "read": False}])
        self.assertIs(entry["exact"], False)
        self.assertIs(entry["ok"], True)

    def test_the_read_back_adds_no_step_and_no_line(self):
        steps = len(self.fake.undo_steps)
        answer = self.command(self.LINE)
        self.assertEqual(answer["undo"], {"step": 1})
        self.assertEqual(len(self.fake.undo_steps), steps + 1)
        self.assertIn("!!item.channel pos.X ? item:translation005",
                      self.fake.evals)
        entry, = self.entries()
        self.assertEqual(len(entry["commands"]), 1)

    def test_a_line_with_no_operator_is_exact(self):
        self.command("test.cube")
        entry, = self.entries()
        self.assertIs(entry["exact"], True)
        self.assertNotIn("operators", entry)

    def test_an_angle_is_written_back_in_degrees(self):
        # On Modo 17.1 (): rot.Y ?+30 read back 30.0, in radians, and
        # "transform.channel rot.Y 30.0" set 30 degrees.
        fakelx = sys.modules["fakelx"]
        item = self.fake.scene.find("translation005")
        item.channels["rot.Y"] = fakelx.FakeChannel(item, "rot.Y", 0.0)
        self.command("item.channel rot.Y ?+0.5 item:translation005")
        self.py("lx.command('item.channel', name='rot.Y', value='?+0.5', "
                "item='translation005')")
        first, second = self.entries()
        self.assertEqual(first["commands"], [
            "!!item.channel rot.Y %r item:translation005" % math.degrees(0.5)])
        self.assertEqual(second["commands"], [
            "!!item.channel name:{rot.Y} value:{%r} item:{translation005}"
            % math.degrees(1.0)])

    def test_a_distance_and_an_unknown_type_are_written_as_read(self):
        self.command(self.LINE)
        entry, = self.entries()
        self.assertEqual(entry["commands"],
                         ["!!item.channel pos.X 1.5 item:translation005"])
        # A command that can't be spawned to ask its type.
        self.assertEqual(guard.typed(0.5, "tool.attr x y ?+1", (13, 16)), 0.5)

    def test_a_value_after_a_named_argument_has_its_own_type(self):
        # A query after a named word asks the argument
        # after it (select.symmetry 0 uvActive:0 ? answers uvAxis), so the
        # word after name:rot.Y is the angle, not name.
        line = "item.channel name:rot.Y 0.5 item:translation005"
        self.assertEqual(guard.arg_type(line, (24, 27)), "angle")

    def test_a_line_with_two_operators_keeps_its_line_and_marks_it(self):
        line = "item.channel pos.X ?+0.5 item:translation005 mode:?max"
        self.ask("run_command", {"command": line})
        entry, = self.entries()
        self.assertEqual(entry["commands"], ["!!" + line])
        self.assertEqual(entry["operators"],
                         [{"i": 0, "sent": "!!" + line, "read": False}])
        self.assertIs(entry["exact"], False)

    def test_a_failed_operator_line_is_not_read_back(self):
        answer = self.py("try:\n    lx.eval('test.fails ?+1')\n"
                         "except Exception:\n    pass\n")
        self.assertTrue(answer["ok"], answer)
        entry, = self.entries()
        self.assertEqual(entry["failed"], ["!!test.fails ?+1"])
        self.assertNotIn("operators", entry)


class GapTest(JournalBase):
    """A probe (2026-11-08): a scene item listener hears the user's hand edits on the
    main thread; events in a job are the agent's, and events while undo or
    redo plays (the undo service's State() 0) are Ctrl+Z, which the markers
    already write."""

    def cube(self):
        answer = self.ask("run_command", {"command": "test.cube"})
        self.assertTrue(answer["ok"], answer)
        return answer

    def events(self):
        return [r for r in records(files()[-1])
                if r["event"] not in ("header", "facts")]

    def test_a_hand_edit_between_calls_is_a_gap_before_the_next_entry(self):
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 2.0)
        self.fake.hand_edit("translation005", "pos.Y", 1.0)
        self.fake.hand_add("locator009", "Camera Target")
        self.cube()
        kinds = [(r["event"], r.get("n"), r.get("before")) for r in self.events()]
        self.assertEqual(kinds, [("entry", 1, None), ("gap", None, 2),
                                 ("entry", 2, None)])
        gap = self.events()[1]
        self.assertEqual(gap["events"], 3)
        self.assertEqual(gap["added"], 1)
        self.assertEqual(gap["items"], [
            {"id": "translation005", "name": "Translation"},
            {"id": "locator009", "name": "Camera Target"}])

    def test_events_in_a_job_are_not_a_gap(self):
        # test.cube tells the listener of the item it adds, in the job.
        self.cube()
        self.cube()
        self.assertEqual([r["event"] for r in self.events()],
                         ["entry", "entry"])

    def test_erics_undo_and_redo_are_not_a_gap(self):
        self.cube()
        self.cube()
        self.fake.undo()
        self.fake.redo()
        self.cube()
        self.assertEqual([r["event"] for r in self.events()],
                         ["entry", "entry", "undone", "redone", "entry"])

    def test_a_gap_before_another_scenes_file_is_dropped(self):
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 2.0)
        self.fake.scene.name = "Other"
        self.cube()
        self.assertEqual(len(files()), 2)
        self.assertNotIn("gap", [r["event"] for r in records(files()[0])])
        self.assertNotIn("gap", [r["event"] for r in records(files()[1])])

    def test_a_facts_record_writes_the_gap_first(self):
        self.cube()
        self.fake.hand_edit("translation005", "pos.X", 2.0)
        answer = self.ask("journal_facts")
        self.assertTrue(answer["ok"], answer)
        events = [(r["event"], r.get("before"), r.get("after"))
                  for r in records(files()[-1])][1:]
        self.assertEqual(events, [("entry", None, None), ("gap", 2, None),
                                  ("facts", None, 1)])

    def test_a_listener_that_raises_is_turned_off_and_status_says_so(self):
        self.cube()
        listen = self.kit.listen

        def broken(item):
            raise RuntimeError("no ident")
        listen.ident = broken
        self.fake.hand_edit("translation005", "pos.X", 2.0)
        # Not removed from inside Modo's call to it: off now,
        # and taken off at the next job.
        self.assertEqual(len(self.fake.listeners), 1)
        self.assertIs(self.ask("status")["result"]["listen"]["on"], False)
        # What it heard before it failed is still a gap; after, nothing.
        self.cube()
        self.assertEqual(self.fake.listeners, [])
        self.fake.hand_edit("translation005", "pos.X", 3.0)
        self.cube()
        self.assertEqual([(r["event"], r.get("events")) for r in self.events()],
                         [("entry", None), ("gap", 1), ("entry", None),
                          ("entry", None)])
        status = self.ask("status")["result"]["listen"]
        self.assertIs(status["on"], False)
        self.assertIn("no ident", status["error"])

    def test_the_listener_is_on_at_start_and_off_at_stop(self):
        self.assertEqual(len(self.fake.listeners), 1)
        self.assertEqual(self.ask("status")["result"]["listen"],
                         {"on": True, "error": None})
        import modotools
        modotools.stop()
        self.assertEqual(self.fake.listeners, [])


if __name__ == "__main__":
    unittest.main()
