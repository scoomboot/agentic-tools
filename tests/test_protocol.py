import filecmp
import os
import unittest
import warnings

import support
from modotools import protocol


class LineReaderTest(unittest.TestCase):
    def test_splits_lines_across_chunks(self):
        reader = protocol.LineReader()
        self.assertEqual(reader.feed(b'{"a": 1}\n{"b"'), [b'{"a": 1}'])
        self.assertEqual(reader.feed(b': 2}\n\n'), [b'{"b": 2}'])

    def test_caps_a_long_line(self):
        reader = protocol.LineReader(max_line_bytes=10)
        with self.assertRaises(ValueError):
            reader.feed(b"x" * 11)
        reader = protocol.LineReader(max_line_bytes=10)
        with self.assertRaises(ValueError):
            reader.feed(b"x" * 20 + b"\n")

    def test_round_trip(self):
        message = protocol.make_request("r-1", "status", {}, 1000)
        self.assertEqual(protocol.decode(protocol.encode(message)), message)

    def test_the_server_copy_matches_the_kit_byte_for_byte(self):
        kit = os.path.join(support.PYMODS, "modotools", "protocol.py")
        server = os.path.join(support.SERVER, "modo_tools", "protocol.py")
        self.assertTrue(filecmp.cmp(kit, server, shallow=False),
                        "server/modo_tools/protocol.py differs from the kit's; "
                        "copy the kit's over it")

    def test_port_and_folder_differ_from_modo_mcp(self):
        self.assertEqual(protocol.DEFAULT_PORT, 15680)
        self.assertNotIn(".modo_mcp", protocol.token_path())
        self.assertTrue(protocol.oplog_path().endswith("ops.log"))


class SourcesTest(unittest.TestCase):
    def test_every_source_compiles_without_a_warning(self):
        # An invalid escape such as "\d" in a plain string is a
        # SyntaxWarning on Python 3.12 and slated to become an error.
        for top in (support.PYMODS, support.SERVER):
            for folder, _, names in os.walk(top):
                for name in names:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(folder, name)
                    with open(path, encoding="utf-8") as f:
                        source = f.read()
                    with warnings.catch_warnings():
                        warnings.simplefilter("error")
                        try:
                            compile(source, path, "exec")
                        except SyntaxError as e:
                            self.fail("%s: %s" % (path, e))


if __name__ == "__main__":
    unittest.main()
