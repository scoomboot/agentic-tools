import os
import stat
import sys
import unittest

import support
from modotools import protocol, token


class TokenTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)

    def test_created_once_and_reused(self):
        path = protocol.token_path()
        first = token.load_or_create(path)
        self.assertTrue(len(first) >= 32)
        self.assertEqual(token.load_or_create(path), first)
        self.assertEqual(token.read(path), first)

    @unittest.skipIf(sys.platform.startswith("win"), "Windows uses profile rights")
    def test_owner_only(self):
        path = protocol.token_path()
        token.load_or_create(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_lives_in_modo_tools_home(self):
        self.assertEqual(os.path.dirname(protocol.token_path()),
                         os.environ["MODO_TOOLS_HOME"])


if __name__ == "__main__":
    unittest.main()
