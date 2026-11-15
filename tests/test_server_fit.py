"""On the server: mt_fit_backdrops, the arguments it sends and
what its description promises."""

import json
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import server
from test_fit_backdrops import Base


class FitToolTest(Base):
    def ask(self, coro):
        from test_server import InThread
        job = InThread(coro)
        self.assertTrue(support.pump_until(self.fake, job.done, timeout=30))
        return job.value

    def setUp(self):
        Base.setUp(self)
        self.bridge = bridge_mod.Bridge(port=self.kit.port)

    def calls(self):
        sent = []
        real = self.bridge.call

        async def call(op, payload, **kw):
            sent.append((op, payload))
            return await real(op, payload, **kw)
        self.bridge.call = call
        return sent

    def test_the_defaults_are_sent_and_the_rest_left_out(self):
        sent = self.calls()
        text = self.ask(server.mt_fit_backdrops_text("Ball", bridge=self.bridge))
        self.assertEqual(sent, [("fit_backdrops", {
            "item": "Ball", "smooth": 0.5, "iterations": 6})])
        shown = json.loads(text)
        self.assertEqual(shown["fitted"], len(self.before))
        self.assertNotIn("_written", shown)
        self.assertEqual(shown["undo"], {"step": 1})

    def test_backdrops_and_floor_are_sent_as_given(self):
        sent = self.calls()
        self.ask(server.mt_fit_backdrops_text(
            "Ball", backdrops=["front", "side"], smooth=0.25, iterations=4,
            floor=-0.5, bridge=self.bridge))
        self.assertEqual(sent, [("fit_backdrops", {
            "item": "Ball", "smooth": 0.25, "iterations": 4,
            "backdrops": ["front", "side"], "floor": -0.5})])

    def test_a_refusal_reads_as_one(self):
        text = self.ask(server.mt_fit_backdrops_text(
            "Ball", backdrops=["front"], bridge=self.bridge))
        self.assertTrue(text.startswith("bad_request:"), text)


class DescriptionTest(unittest.TestCase):
    def test_fit_backdrops(self):
        for words in ("SubD", "mt_backdrop", "two backdrops", "selected",
                      "smooth", "iterations", "floor", "iou", "max_move_m",
                      "off_target_m", "One undo step", "mt_begin",
                      "Modo's own surface", "height", "journal",
                      "one part at a time",
                      "in polygon mode", "512 px", "is refused",
                      "ms a point"):
            self.assertIn(words, server.FIT_BACKDROPS_DESCRIPTION)

    def test_the_journal_lists_it(self):
        self.assertIn("mt_fit_backdrops", server.JOURNAL_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
