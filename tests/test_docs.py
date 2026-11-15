"""Mt_search_docs and mt_read_doc, on made-up files.

Foundry's files carry no licence grant, so no test reads or copies them: each
test builds a small fake install in a temp folder, shaped like Modo 17.1's
(resrc\\python3kit\\extra64\\Python\\Scripts, resrc\\cmdhelp*.cfg,
resrc\\itemhelp.cfg, extra\\Scripts).
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest

import support
from modo_tools import docs, protocol, server
from test_server import free_port
from modo_tools import bridge as bridge_mod

SCRIPTS = os.path.join("resrc", "python3kit", "extra64", "Python", "Scripts")

LXIFC = '''\
# Copyright 0000 nobody
class Image(object):
    def img_Size(self):
        lx.notimpl()
    def img_GetPixel(self, x, y, type, pixel):
        lx.notimpl()


class Visitor(object):
    def vis_Evaluate(self):
        lx.notimpl()
'''

ITEM = '''\
"""Made-up modo.item."""


def helper(item):
    """Answer the item's name."""
    return item


class Mesh(object):
    """A mesh item."""

    def __init__(self, item):
        self._geo = None

    @property
    def geometry(self):
        """The MeshGeometry, kept from the first read."""
        if self._geo is None:
            self._geo = object()
        return self._geo

    def _private(self):
        return 1
'''

COMMAND = '''\
def storage_buffer():
    item_types = lx.object.storage()
    item_types.setType('i')
    return item_types
'''

CMDHELP = '''\
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <atom type="CommandHelp">
    <hash type="ArgumentType" key="edge-bridge-mode@en_US">
      <atom type="UserName">Bridge Mode</atom>
      <hash type="Option" key="linear">
        <atom type="UserName">Linear</atom>
      </hash>
    </hash>
    <hash type="Command" key="select.symmetry@en_US">
      <atom type="UserName">Symmetry</atom>
      <atom type="Desc">Turn symmetry on or off.</atom>
      <hash type="Argument" key="axis">
        <atom type="UserName">Axis</atom>
        <atom type="Desc">The symmetry axis.</atom>
      </hash>
    </hash>
    <hash type="Tool" key="edge.bridge@en_US">
      <atom type="UserName">Bridge</atom>
      <atom type="Desc">Bridge two edge loops with polygons.</atom>
      <hash type="Attribute" key="segments">
        <atom type="UserName">Segments</atom>
        <atom type="Desc">Number of segments.</atom>
      </hash>
    </hash>
    <hash type="Command" key="select.symmetry@ja_JP">
      <atom type="UserName">X</atom>
    </hash>
  </atom>
</configuration>
'''

ITEMHELP = '''\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE configuration [
<!ENTITY deg "&#176;" >
]>
<configuration>
  <atom type="ItemHelp">
    <hash type="Item" key="backdrop@en_US">
      <atom type="UserName">Backdrop Item</atom>
      <hash type="Channel" key="hidePersp">
        <atom type="UserName">Hide in perspective</atom>
      </hash>
      <hash type="Channel" key="angle">
        <atom type="UserName">Angle (&deg;)</atom>
      </hash>
    </hash>
    <hash type="Item" key="camera@en_US">
      <atom type="UserName">Camera</atom>
      <hash type="Channel" key="focalLen">
        <atom type="UserName">Focal Length</atom>
      </hash>
    </hash>
  </atom>
</configuration>
'''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class FakeInstall(object):
    """A made-up Modo install, found through MODO_TOOLS_MODO_DIR."""

    def __init__(self, testcase):
        self.root = tempfile.mkdtemp(prefix="modo_install_test_")
        testcase.addCleanup(shutil.rmtree, self.root, True)
        scripts = os.path.join(self.root, SCRIPTS)
        self.scripts = scripts
        write(os.path.join(scripts, "lxifc.py"), LXIFC)
        write(os.path.join(scripts, "modo", "__init__.py"), "")
        write(os.path.join(scripts, "modo", "item.py"), ITEM)
        write(os.path.join(scripts, "lxu", "__init__.py"), "")
        write(os.path.join(scripts, "lxu", "command.py"), COMMAND)
        write(os.path.join(scripts, "PySide6", "QtCore.py"),
              "def vendoredPixelThing():\n    pass\n")
        write(os.path.join(scripts, "ppi_flip.py"),
              "# Flip the selected polygons.\nlx.eval('poly.flip')\n")
        write(os.path.join(scripts, "modo", "broken.py"), "def oops(:\n")
        write(os.path.join(self.root, "resrc", "cmdhelp.cfg"), CMDHELP)
        write(os.path.join(self.root, "resrc", "itemhelp.cfg"), ITEMHELP)
        write(os.path.join(self.root, "extra", "Scripts", "SomeKit",
                           "kit_tool.py"),
              "# A bundled kit's script.\nimport lx\nlx.eval('item.create locator')\n")
        old = os.environ.get("MODO_TOOLS_MODO_DIR")
        os.environ["MODO_TOOLS_MODO_DIR"] = self.root
        testcase.addCleanup(self._restore, old)
        # No user scripts folder from the PC running the tests.
        old_user = os.environ.get("MODO_TOOLS_USER_SCRIPTS")
        os.environ["MODO_TOOLS_USER_SCRIPTS"] = os.path.join(self.root, "none")
        testcase.addCleanup(self._restore_user, old_user)

    @staticmethod
    def _restore(old):
        if old is None:
            os.environ.pop("MODO_TOOLS_MODO_DIR", None)
        else:
            os.environ["MODO_TOOLS_MODO_DIR"] = old

    @staticmethod
    def _restore_user(old):
        if old is None:
            os.environ.pop("MODO_TOOLS_USER_SCRIPTS", None)
        else:
            os.environ["MODO_TOOLS_USER_SCRIPTS"] = old


def ids(answer):
    return [hit["id"] for hit in answer["hits"]]


class SourcesTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        self.install = FakeInstall(self)

    def test_the_install_is_found_from_the_variable(self):
        self.assertEqual(docs.find_install(), self.install.root)

    def test_the_newest_install_is_found_under_program_files(self):
        base = tempfile.mkdtemp(prefix="pf_test_")
        self.addCleanup(shutil.rmtree, base, True)
        for name in ("Modo16.0v1", "Modo17.1v1", "Modo9.0v2"):
            os.makedirs(os.path.join(base, name, "modo"))
        os.environ.pop("MODO_TOOLS_MODO_DIR", None)
        self.assertEqual(docs.find_install([base]),
                         os.path.join(base, "Modo17.1v1", "modo"))

    def test_no_install_is_none(self):
        os.environ["MODO_TOOLS_MODO_DIR"] = os.path.join(self.install.root, "x")
        self.assertIsNone(docs.find_install([]))

    def test_vendored_folders_are_skipped(self):
        found = docs.search("vendoredPixelThing")
        self.assertFalse([i for i in ids(found) if "QtCore" in i], found)


class PythonEntriesTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        FakeInstall(self)

    def test_a_module_splits_into_function_class_and_method(self):
        index = docs.load()
        entry = index.get("api:modo.item.Mesh.geometry")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["kind"], "api")
        self.assertEqual(entry["where"], "modo/item.py:16")
        self.assertIn("kept from the first read", entry["text"])
        self.assertIsNotNone(index.get("api:modo.item.Mesh"))
        self.assertIsNotNone(index.get("api:modo.item.helper"))
        self.assertIsNotNone(index.get("api:modo.item.Mesh.__init__"))
        self.assertIsNone(index.get("api:modo.item.Mesh._private"))

    def test_a_camelcase_query_finds_its_method(self):
        found = docs.search("get pixel")
        self.assertEqual(ids(found)[0], "api:lxifc.Image.img_GetPixel", found)
        found = docs.search("GetPixel")
        self.assertEqual(ids(found)[0], "api:lxifc.Image.img_GetPixel", found)

    def test_a_hit_shows_where_and_the_lines_around_the_match(self):
        hit = docs.search("storage setType")["hits"][0]
        self.assertEqual(hit["id"], "api:lxu.command.storage_buffer")
        self.assertEqual(hit["where"], "lxu/command.py:1")
        self.assertIn("lx.object.storage()", hit["text"])

    def test_an_example_script_is_one_entry(self):
        found = docs.search("flip polygons", kind="example")
        self.assertEqual(ids(found)[0], "example:Scripts/ppi_flip.py", found)
        found = docs.search("locator", kind="example")
        self.assertIn("example:extra/SomeKit/kit_tool.py", ids(found))

    def test_a_file_that_cant_be_parsed_is_skipped_and_counted(self):
        found = docs.search("oops")
        self.assertEqual(found["skipped"], 1, found)
        self.assertNotIn("api:modo.broken.oops", ids(found))


class HelpEntriesTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        FakeInstall(self)

    def test_commands_tools_and_channels_are_entries(self):
        index = docs.load()
        command = index.get("help:command:select.symmetry")
        self.assertIn("The symmetry axis.", command["text"])
        self.assertTrue(command["where"].startswith("cmdhelp.cfg:"))
        tool = index.get("help:tool:edge.bridge")
        self.assertIn("Number of segments.", tool["text"])
        channel = index.get("help:channel:backdrop.hidePersp")
        self.assertIn("Hide in perspective", channel["text"])
        self.assertIn("hidePersp", index.get("help:item:backdrop")["text"])
        self.assertIsNotNone(index.get("help:argtype:edge-bridge-mode"))

    def test_only_english_help(self):
        self.assertNotIn("X", docs.load().get(
            "help:command:select.symmetry")["title"].split())

    def test_an_entity_in_itemhelp_reads(self):
        self.assertIn("Angle (°)", docs.load().get(
            "help:channel:backdrop.angle")["text"])

    def test_channel_help_ranks_for_its_words(self):
        found = docs.search("backdrop hide in perspective")
        self.assertEqual(ids(found)[0], "help:channel:backdrop.hidePersp", found)

    def test_tool_help_ranks_for_its_words(self):
        found = docs.search("bridge two edge loops")
        self.assertEqual(ids(found)[0], "help:tool:edge.bridge", found)


class FactsTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        # Not the scripts folder of the PC running the tests (the review).
        old = os.environ.get("MODO_TOOLS_USER_SCRIPTS")
        os.environ["MODO_TOOLS_USER_SCRIPTS"] = os.path.join(
            tempfile.gettempdir(), "no_user_scripts_7_4")
        self.addCleanup(FakeInstall._restore_user, old)

    def test_the_facts_load_with_no_install(self):
        os.environ["MODO_TOOLS_MODO_DIR"] = os.path.join(
            tempfile.gettempdir(), "no_modo_here_7_4")
        self.addCleanup(os.environ.pop, "MODO_TOOLS_MODO_DIR", None)
        old = docs.PROGRAM_FILES
        docs.PROGRAM_FILES = []
        self.addCleanup(setattr, docs, "PROGRAM_FILES", old)
        found = docs.search("scene.open froze")
        self.assertTrue(found["hits"], found)
        self.assertEqual(found["hits"][0]["kind"], "facts")
        self.assertIn("No Modo install", found["note"])

    def test_every_fact_has_a_body(self):
        for entry in docs.read_facts():
            self.assertGreater(len(entry["text"].split()), 10, entry["id"])


class SearchAndReadTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        self.install = FakeInstall(self)

    def test_an_empty_query_is_refused(self):
        with self.assertRaises(docs.DocsError) as caught:
            docs.search("  ")
        self.assertEqual(caught.exception.type, protocol.BAD_REQUEST)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(docs.DocsError) as caught:
            docs.search("pixel", kind="manual")
        self.assertEqual(caught.exception.type, protocol.BAD_REQUEST)
        self.assertIn("api", caught.exception.hint)

    def test_kind_narrows_the_search(self):
        found = docs.search("bridge", kind="facts")
        self.assertTrue(all(h["kind"] == "facts" for h in found["hits"]))

    def test_limit(self):
        self.assertEqual(len(docs.search("item", limit=2)["hits"]), 2)

    def test_an_unknown_id_is_not_found(self):
        with self.assertRaises(docs.DocsError) as caught:
            docs.read("api:modo.nothing")
        self.assertEqual(caught.exception.type, protocol.NOT_FOUND)

    def test_read_answers_the_entry_whole(self):
        entry = docs.read("api:modo.item.Mesh")
        self.assertIn("def geometry(self):", entry["text"])
        self.assertIn("def _private(self):", entry["text"])
        self.assertEqual(entry["where"], "modo/item.py:9")

    def test_a_long_entry_is_cut_and_reads_on(self):
        body = "".join("    x%d = %d  # line\n" % (i, i) for i in range(400))
        write(os.path.join(self.install.scripts, "modo", "long.py"),
              "def big():\n" + body)
        docs.forget()
        first = docs.read("api:modo.long.big", limit_chars=2000)
        self.assertLessEqual(len(json.dumps(first)), 2000)
        self.assertIn("offset", first["more"])
        second = docs.read("api:modo.long.big", offset=first["next"],
                           limit_chars=2000)
        self.assertEqual(second["lines"][0], first["next"] + 1)
        self.assertTrue(second["text"].startswith("    x%d" % (first["next"] - 1)),
                        second["text"][:40])

    def test_the_index_is_written_under_modo_tools_home(self):
        docs.search("pixel")
        path = os.path.join(protocol.data_dir(), "docs", "index.json")
        self.assertTrue(os.path.isfile(path))


class IndexOnDiskTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        self.install = FakeInstall(self)

    def test_a_changed_file_rebuilds_the_index(self):
        self.assertNotIn("api:modo.item.fresh", ids(docs.search("fresh")))
        path = os.path.join(self.install.scripts, "modo", "item.py")
        write(path, ITEM + "\n\ndef fresh():\n    \"\"\"Brand new.\"\"\"\n")
        later = time.time() + 5
        os.utime(path, (later, later))
        docs.forget()
        self.assertIn("api:modo.item.fresh", ids(docs.search("fresh")))

    def test_the_index_is_reused_when_nothing_changed(self):
        docs.search("pixel")
        built = docs.load().built
        docs.forget()
        self.assertEqual(docs.load().built, built)

    def test_a_broken_index_is_rebuilt(self):
        docs.search("pixel")
        path = os.path.join(protocol.data_dir(), "docs", "index.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        docs.forget()
        self.assertEqual(ids(docs.search("GetPixel"))[0],
                         "api:lxifc.Image.img_GetPixel")


LX_DUMP = {
    "version": "17.1v1",
    "objects": {"Image": {"methods": {
        "GetPixel": "GetPixel(integer x,integer y,integer type,data[] pixel {})",
        "Size": "(integer w, integer h) = Size()"}}},
    "services": {"Image": {"methods": {
        "LoadNoCache": "Image object {} = LoadNoCache(string filename)"}}},
    "symbols": {"iIMV_RGBA": 4, "sITYPE_MESH": "mesh"},
}


class LxDumpEntriesTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        FakeInstall(self)

    def test_without_a_dump_the_search_says_lx_is_missing(self):
        self.assertIn("lx", docs.search("GetPixel")["note"])

    def test_a_saved_dump_becomes_entries(self):
        docs.save_lx_dump(LX_DUMP)
        docs.forget()
        index = docs.load()
        method = index.get("api:lx.object.Image.GetPixel")
        self.assertIn("data[] pixel", method["text"])
        self.assertIn("GetPixel", index.get("api:lx.object.Image")["text"])
        self.assertIsNotNone(index.get("api:lx.service.Image.LoadNoCache"))
        self.assertIn("4", index.get("api:lx.symbol.iIMV_RGBA")["text"])
        found = docs.search("Image GetPixel")
        self.assertIn("api:lx.object.Image.GetPixel", ids(found)[:3], found)
        self.assertEqual(found.get("note", ""), "")


class FakeBridge(object):
    """Only what the server's lx dump step uses."""

    def __init__(self, version="17.1v1", reachable=True):
        self.server_info = {}
        self.version = version
        self.reachable = reachable
        self.calls = []

    async def call(self, op, payload=None, timeout_ms=None):
        self.calls.append(op)
        if not self.reachable:
            raise bridge_mod.BridgeError(protocol.UNREACHABLE, "closed")
        self.server_info = {"modo": self.version}
        if op == "status":
            return {"ok": True, "result": {}}
        if op == "api_dump":
            return {"ok": True, "result": dict(LX_DUMP, version=None)}
        return {"ok": False, "error": {"type": "unknown_op"}}


class ServerToolsTest(unittest.TestCase):
    def setUp(self):
        support.temp_home(self)
        docs.forget()
        FakeInstall(self)
        server._lx_asked = -server.LX_RETRY_S

    def test_modo_closed_is_asked_once_a_minute(self):
        # A refused connection costs about 2 s on Windows.
        bridge = FakeBridge(reachable=False)
        asyncio.run(server.mt_search_docs_text("GetPixel", bridge=bridge))
        asyncio.run(server.mt_search_docs_text("GetPixel", bridge=bridge))
        self.assertEqual(bridge.calls, ["status"])

    def test_the_server_asks_for_the_dump_once_per_version(self):
        bridge = FakeBridge()
        text = asyncio.run(server.mt_search_docs_text("Image GetPixel",
                                                      bridge=bridge))
        self.assertIn("api:lx.object.Image.GetPixel", text)
        self.assertEqual(bridge.calls.count("api_dump"), 1)
        self.assertTrue(os.path.isfile(os.path.join(
            protocol.data_dir(), "docs", "lx-17.1v1.json")))
        asyncio.run(server.mt_search_docs_text("Image GetPixel", bridge=bridge))
        self.assertEqual(bridge.calls.count("api_dump"), 1)

    def test_modo_closed_is_not_an_error(self):
        bridge = FakeBridge(reachable=False)
        text = asyncio.run(server.mt_search_docs_text("GetPixel", bridge=bridge))
        answer = json.loads(text)
        self.assertEqual(answer["hits"][0]["id"], "api:lxifc.Image.img_GetPixel")
        self.assertIn("lx", answer["note"])

    def test_refusals_come_back_as_text(self):
        bridge = FakeBridge(reachable=False)
        text = asyncio.run(server.mt_search_docs_text("", bridge=bridge))
        self.assertTrue(text.startswith("bad_request:"), text)
        text = server.mt_read_doc_text("api:nothing")
        self.assertTrue(text.startswith("not_found:"), text)

    def test_read_doc_text(self):
        text = server.mt_read_doc_text("api:modo.item.Mesh.geometry")
        answer = json.loads(text)
        self.assertEqual(answer["where"], "modo/item.py:16")


class ApiDumpOpTest(unittest.TestCase):
    """The kit's api_dump op, in fake Modo."""

    def setUp(self):
        self.fake, self.kit = support.start_kit(self)
        import sys
        import types
        lx = sys.modules["lx"]

        class Image(object):
            def GetPixel(self):
                "GetPixel(integer x,integer y,integer type,data[] pixel {})"

            def _hidden(self):
                "no"

        class Platform(object):
            def AppVersion(self):
                "integer = AppVersion()"

        old_object, old_service = lx.object, lx.service
        lx.object = types.SimpleNamespace(Image=Image, storage=lambda: None)
        lx.service = types.SimpleNamespace(Platform=Platform)
        self.addCleanup(setattr, lx, "object", old_object)
        self.addCleanup(setattr, lx, "service", old_service)

    def test_the_dump_lists_classes_methods_and_symbols(self):
        import sys
        jobs = sys.modules["modotools.jobs"]
        answer = jobs.run_job({"id": "t-1", "op": "api_dump", "payload": {}},
                              self.kit)
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]
        self.assertEqual(result["objects"]["Image"]["methods"],
                         {"GetPixel": "GetPixel(integer x,integer y,integer "
                                      "type,data[] pixel {})"})
        self.assertNotIn("storage", result["objects"])
        self.assertEqual(result["services"]["Platform"]["methods"]["AppVersion"],
                         "integer = AppVersion()")
        self.assertEqual(result["symbols"]["iIMV_RGBA"], 4)

    def test_the_dump_is_a_read(self):
        import sys
        jobs = sys.modules["modotools.jobs"]
        self.assertTrue(jobs.is_read("api_dump"))


class ReviewTest(unittest.TestCase):
    """Found 2026-11-12: what the tools did in an agent's hands."""

    def setUp(self):
        support.temp_home(self)
        docs.forget()
        self.install = FakeInstall(self)
        server._lx_asked = -server.LX_RETRY_S

    def test_searches_at_the_same_time_build_the_index_once(self):
        # An agent sends its searches together; each ran on its own thread,
        # each built the index and wrote the same .tmp file: on Windows all
        # but one failed with PermissionError.
        import threading
        built, errors = [], []
        real = docs.build

        def slow(*args):
            built.append(1)
            time.sleep(0.3)
            return real(*args)

        docs.build = slow
        self.addCleanup(setattr, docs, "build", real)

        def one(query):
            try:
                docs.search(query)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one, args=(q,))
                   for q in ("GetPixel", "bridge", "backdrop", "symmetry")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(built), 1)

    def test_an_index_that_cant_be_saved_still_answers(self):
        # The file on disk is only a cache: another server process holding
        # it, or a full disk, is not the search's failure.
        real = os.replace

        def refuse(src, dst):
            raise PermissionError(13, "in use", dst)

        os.replace = refuse
        self.addCleanup(setattr, os, "replace", real)
        found = docs.search("GetPixel")
        self.assertEqual(ids(found)[0], "api:lxifc.Image.img_GetPixel")
        left = os.listdir(os.path.join(protocol.data_dir(), "docs"))
        self.assertEqual([n for n in left if ".tmp" in n], [])

    def test_a_full_dotted_name_finds_its_entry_first(self):
        docs.save_lx_dump(LX_DUMP)
        docs.forget()
        for query, want in (("lx.service.Image", "api:lx.service.Image"),
                            ("lx.object.Image", "api:lx.object.Image"),
                            ("modo.item.Mesh", "api:modo.item.Mesh"),
                            ("Mesh.geometry", "api:modo.item.Mesh.geometry"),
                            ("select.symmetry", "help:command:select.symmetry"),
                            ("backdrop.hidePersp",
                             "help:channel:backdrop.hidePersp")):
            found = docs.search(query)
            self.assertEqual(ids(found)[:1], [want], (query, ids(found)))

    def test_a_plural_finds_the_singular_and_back(self):
        self.assertEqual(docs.words("pixels"), docs.words("pixel"))
        self.assertEqual(docs.words("vertices"), docs.words("vertex"))
        self.assertEqual(docs.words("meshes"), docs.words("mesh"))
        self.assertEqual(docs.words("copies"), docs.words("copy"))
        # Not plurals.
        self.assertEqual(docs.words("axis"), ["axis"])
        self.assertEqual(docs.words("class status pass"), ["status", "pass"])
        found = docs.search("flip polygon", kind="example")
        self.assertEqual(ids(found)[:1], ["example:Scripts/ppi_flip.py"])
        found = docs.search("reading pixel buffers", kind="facts")
        self.assertEqual(ids(found)[:1],
                         ["facts:reading-pixels-getpixel-fills-a-buffer-you-pass"])

    def test_no_hits_says_why(self):
        found = docs.search("lx")
        self.assertEqual(found["hits"], [])
        self.assertIn("no words to search", found["note"])
        found = docs.search("zzyzx")
        self.assertEqual(found["hits"], [])
        self.assertIn("mt_find_commands", found["note"])

    def test_an_id_without_its_kind_names_the_id(self):
        with self.assertRaises(docs.DocsError) as caught:
            docs.read("lxifc.Image.img_GetPixel")
        self.assertEqual(caught.exception.type, protocol.NOT_FOUND)
        self.assertIn("api:lxifc.Image.img_GetPixel", caught.exception.hint)
        with self.assertRaises(docs.DocsError) as caught:
            docs.read("API:LXIFC.image.img_getpixel")
        self.assertIn("api:lxifc.Image.img_GetPixel", caught.exception.hint)

    def test_an_offset_past_the_end_says_so(self):
        entry = docs.read("api:lxifc.Image.img_GetPixel", offset=50)
        self.assertEqual(entry["text"], "")
        self.assertEqual(entry["lines"], [0, 0, 2])
        self.assertIn("2 lines", entry["note"])

    def test_a_kit_with_no_api_dump_is_named(self):
        # The kit installed in Modo can be older than the server: the note
        # said the lx part is read "the first time a search runs while Modo
        # is open", which never came true.
        class OldKit(FakeBridge):
            async def call(self, op, payload=None, timeout_ms=None):
                if op == "api_dump":
                    self.calls.append(op)
                    return {"ok": False, "error": {
                        "type": protocol.UNKNOWN_OP,
                        "message": "no op named 'api_dump'"}}
                return await FakeBridge.call(self, op, payload, timeout_ms)

        bridge = OldKit()
        answer = json.loads(asyncio.run(
            server.mt_search_docs_text("GetPixel", bridge=bridge)))
        self.assertIn("older", answer["note"])
        self.assertNotIn("first time a search runs", answer["note"])
        # And a second search within the minute still says it.
        answer = json.loads(asyncio.run(
            server.mt_search_docs_text("GetPixel", bridge=bridge)))
        self.assertIn("older", answer["note"])
        self.assertEqual(bridge.calls.count("api_dump"), 1)

    def test_the_facts_hold_this_slices_probe(self):
        found = docs.search("lx method signature docstring", kind="facts")
        self.assertTrue(found["hits"], found)
        self.assertIn("__doc__", docs.read(found["hits"][0]["id"])["text"])


if __name__ == "__main__":
    unittest.main()
