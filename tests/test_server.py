"""The server half: the bridge client and mt_status, against the real kit
running in fake Modo. The asyncio side runs on a helper thread, because this
thread is Modo's main thread and has to tick."""

import asyncio
import json
import socket
import sys
import threading
import unittest

import support
from modo_tools import bridge as bridge_mod
from modo_tools import server


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class InThread(object):
    def __init__(self, coro):
        self.value = None
        self.thread = threading.Thread(target=self._run, args=(coro,))
        self.thread.daemon = True
        self.thread.start()

    def _run(self, coro):
        self.value = asyncio.run(coro)

    def done(self):
        return not self.thread.is_alive()


class UnreachableTest(unittest.TestCase):
    def test_mt_status_with_nothing_listening(self):
        support.temp_home(self)
        port = free_port()
        text = asyncio.run(server.mt_status_text(bridge_mod.Bridge(port=port)))
        self.assertTrue(text.startswith("unreachable: Modo isn't reachable on %d" % port),
                        text)
        self.assertIn("modoTools.start", text)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.kit = support.start_kit(self)

    def status(self, bridge):
        job = InThread(server.mt_status_text(bridge))
        self.assertTrue(support.pump_until(self.fake, job.done))
        return job.value

    def test_mt_status_answers_from_modo(self):
        text = self.status(bridge_mod.Bridge(port=self.kit.port))
        shown = json.loads(text)
        self.assertEqual(shown["modo"], "17.1v1")
        self.assertEqual(shown["kit"], "0.4.0")
        self.assertEqual(shown["protocol"], 1)
        self.assertEqual(shown["pump"], "idle")
        self.assertEqual(shown["port"], self.kit.port)
        self.assertEqual(shown["last_session"], "first session")

    def test_reconnects_after_stop_and_start(self):
        port = self.kit.port
        b = bridge_mod.Bridge(port=port)

        async def three():
            # One event loop for all three calls, as in the real server.
            results = []
            for step in range(3):
                results.append(await server.mt_status_text(b))
                stepped[step].set()
                await asyncio.get_running_loop().run_in_executor(
                    None, resume[step].wait)
            return results

        stepped = [threading.Event() for _ in range(3)]
        resume = [threading.Event() for _ in range(3)]
        job = InThread(three())

        support.pump_until(self.fake, stepped[0].is_set)
        self.fake.run_command("modoTools.stop")
        resume[0].set()
        support.pump_until(self.fake, stepped[1].is_set)
        self.fake.run_command("modoTools.start port:%d" % port)
        resume[1].set()
        support.pump_until(self.fake, stepped[2].is_set)
        resume[2].set()
        support.pump_until(self.fake, job.done)

        first, second, third = job.value
        self.assertEqual(json.loads(first)["pump"], "idle")
        self.assertTrue(second.startswith("unreachable"), second)
        self.assertEqual(json.loads(third)["last_session"], "ended cleanly")

    def test_a_wrong_token_is_reported(self):
        import os
        with open(os.path.join(os.environ["MODO_TOOLS_HOME"], "token"), "w") as handle:
            handle.write("wrong")
        text = self.status(bridge_mod.Bridge(port=self.kit.port))
        self.assertTrue(text.startswith("bad_token"), text)

    def test_a_kit_on_another_protocol_is_named(self):
        self.kit.listener.hello_info = dict(self.kit.listener.hello_info)
        listener_mod = sys.modules["modotools.listener"]
        proto = sys.modules["modotools.protocol"]
        old = proto.PROTOCOL_VERSION
        proto.PROTOCOL_VERSION = 2
        self.addCleanup(setattr, proto, "PROTOCOL_VERSION", old)
        text = self.status(bridge_mod.Bridge(port=self.kit.port))
        self.assertTrue(text.startswith("protocol_mismatch"), text)
        self.assertIn("Update the server", text)


class McpTest(unittest.TestCase):
    def test_mt_status_is_registered(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("the mcp package isn't installed")
        app = server.build_server()

        async def names():
            tools = await app.list_tools()
            return sorted(t.name for t in tools)

        self.assertIn("mt_status", asyncio.run(names()))


if __name__ == "__main__":
    unittest.main()
