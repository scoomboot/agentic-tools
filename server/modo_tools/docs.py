"""Search Modo's own docs, and read one entry whole.

Sources, all on the user's PC and none copied into the repo (Foundry's files carry
no licence grant):

  api      the Python API source Modo installs (lxu, modo, lxserv, lxifc.py),
           one entry per function, class and method; and, once the kit has
           dumped it, the compiled lx module (every lx.object and lx.service
           method with its signature, every lx.symbol)
  help     resrc\\cmdhelp*.cfg and itemhelp.cfg: one entry per command, tool,
           argument type, item type and channel
  example  the scripts Modo ships beside the API, the bundled kits under
           extra\\Scripts, and the user's scripts folder; one entry per file
  facts    modo_facts.md beside this module: what was found in Modo 17.1
           does, one entry per section

The index is built once into ~/.modo_tools/docs/index.json and rebuilt when a
source's files or times change. The search is BM25 over words, with
camelCase and dotted names split into their parts.

The idea of a standard-library BM25 index over Modo's Python, split per
function and method, is modo-mcp's (MIT, Leonard Billeke, 2026: its
knowledge/index.py); the code is ours.
"""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import math
import os
import re
import threading
import time
import xml.parsers.expat
from collections import Counter
from typing import Any

from . import protocol

INDEX_VERSION = 1
KINDS = ("api", "help", "example", "facts")
PROGRAM_FILES = [os.environ.get("ProgramFiles", r"C:\Program Files")]
SCRIPTS = os.path.join("resrc", "python3kit", "extra64", "Python", "Scripts")
API_PACKAGES = ("lxu", "modo", "lxserv", "lxifc")
# Vendored code: indexing it buries Modo's own.
SKIP_DIRS = {"__pycache__", "PySide6", "shiboken6", "exifread", "fnpxr",
             "sitecustomize", ".git", "__MACOSX"}
FACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs",
                     "modo_facts.md")
HIT_CHARS = 400


class DocsError(Exception):
    def __init__(self, err_type: str, message: str, hint: str = ""):
        super().__init__(message)
        self.type = err_type
        self.message = message
        self.hint = hint


def docs_dir() -> str:
    return os.path.join(protocol.data_dir(), "docs")


def index_path() -> str:
    return os.path.join(docs_dir(), "index.json")


# -- finding the sources ----------------------------------------------------

def _version_key(path: str) -> list:
    name = os.path.basename(os.path.dirname(path))
    return [int(n) for n in re.findall(r"\d+", name)]


def find_install(bases: list[str] | None = None) -> str | None:
    """MODO_TOOLS_MODO_DIR, else the newest <base>\\Modo*\\modo."""
    named = os.environ.get("MODO_TOOLS_MODO_DIR", "").strip()
    if named:
        return named if os.path.isdir(named) else None
    found = []
    for base in PROGRAM_FILES if bases is None else bases:
        found += [p for p in glob.glob(os.path.join(base, "Modo*", "modo"))
                  if os.path.isdir(p)]
    return max(found, key=_version_key) if found else None


def user_scripts() -> str:
    named = os.environ.get("MODO_TOOLS_USER_SCRIPTS")
    if named is not None:
        return named
    return os.path.join(os.environ.get("APPDATA", ""), "Luxology", "Scripts")


def _walk(root: str, suffix: str) -> list[str]:
    out = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        out += [os.path.join(folder, f) for f in sorted(files)
                if f.endswith(suffix)]
    return out


def source_files(install: str | None) -> dict[str, list[tuple[str, str]]]:
    """{group: [(path, shown path)]}, the files each part of the index reads."""
    groups: dict[str, list[tuple[str, str]]] = {
        "api": [], "help": [], "example": []}
    if install:
        scripts = os.path.join(install, SCRIPTS)
        for path in _walk(scripts, ".py"):
            rel = os.path.relpath(path, scripts).replace("\\", "/")
            top = rel.split("/")[0]
            if top.endswith(".py"):
                top = top[:-3]
            if top in API_PACKAGES:
                groups["api"].append((path, rel))
            else:
                groups["example"].append((path, "Scripts/" + rel))
        resrc = os.path.join(install, "resrc")
        for path in sorted(glob.glob(os.path.join(resrc, "cmdhelp*.cfg"))
                           + glob.glob(os.path.join(resrc, "itemhelp.cfg"))):
            groups["help"].append((path, os.path.basename(path)))
        extra = os.path.join(install, "extra", "Scripts")
        for path in _walk(extra, ".py"):
            rel = os.path.relpath(path, extra).replace("\\", "/")
            groups["example"].append((path, "extra/" + rel))
    user = user_scripts()
    if user and os.path.isdir(user):
        for path in _walk(user, ".py"):
            rel = os.path.relpath(path, user).replace("\\", "/")
            groups["example"].append((path, "user/" + rel))
    return groups


def lx_dumps() -> list[str]:
    return sorted(glob.glob(os.path.join(docs_dir(), "lx-*.json")),
                  key=os.path.getmtime)


def stamp(install: str | None, groups: dict) -> str:
    """Changes when any source file, its size or its time changes."""
    sha = hashlib.sha1()
    sha.update(str((INDEX_VERSION, install)).encode())
    paths = [p for files in groups.values() for p, _ in files]
    paths += [FACTS] + lx_dumps()[-1:]
    for path in paths:
        try:
            st = os.stat(path)
            sha.update(f"{path}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            sha.update(f"{path}|gone\n".encode())
    return sha.hexdigest()


# -- words ------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for",
         "on", "with", "as", "by", "this", "that", "from", "at", "be", "are",
         "how", "do", "i", "we", "can", "if", "not", "self", "def", "class",
         "import", "return", "none", "true", "false", "lx"}


_IRREGULAR = {"vertices": "vertex", "indices": "index", "matrices": "matrix"}
_ES = ("xes", "ches", "shes", "sses", "zes")
_NOT_PLURAL = ("ss", "us", "is")


def _singular(word: str) -> str:
    """pixels -> pixel, meshes -> mesh, copies -> copy, vertices -> vertex:
    a query's "pixel" finds a text's "pixels". The index
    and the query go through the same rule, so a wrong cut still matches
    itself."""
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 3:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith(_ES):
            return word[:-2]
        if word.endswith("s") and not word.endswith(_NOT_PLURAL):
            return word[:-1]
    return word


def words(text: str) -> list[str]:
    """Each name lowered, then its _ parts and camelCase parts, each in the
    singular: img_GetPixel -> img_getpixel, img, getpixel, get, pixel."""
    out: list[str] = []
    for name in _WORD.findall(text):
        seen = set()
        parts = [name] + [p for p in name.split("_") if p]
        for part in parts:
            for word in [part] + _CAMEL.findall(part):
                word = word.lower()
                if len(word) < 2 or word in _STOP:
                    continue
                word = _singular(word)
                if word not in seen:
                    seen.add(word)
                    out.append(word)
    return out


# -- entries ----------------------------------------------------------------

def _entry(id_: str, kind: str, title: str, where: str, text: str,
           search: str | None = None, line: int = 1) -> dict[str, Any]:
    out = {"id": id_, "kind": kind, "title": title, "where": where,
           "text": text, "line": line}
    if search is not None:
        out["search"] = search
    return out


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def python_entries(path: str, shown: str) -> list[dict]:
    """A module's functions, classes and public methods (and __init__).
    Raises SyntaxError/OSError for a file that can't be read or parsed."""
    source = _read_text(path)
    tree = ast.parse(source)
    lines = source.splitlines()
    module = shown[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-len(".__init__")]

    def span(node):
        first = min([node.lineno] + [d.lineno for d in
                                     getattr(node, "decorator_list", [])])
        return "\n".join(lines[first - 1:node.end_lineno or node.lineno])

    out = []
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, defs):
            name = f"{module}.{node.name}"
            out.append(_entry("api:" + name, "api", name + "()",
                              f"{shown}:{node.lineno}", span(node),
                              line=node.lineno))
        elif isinstance(node, ast.ClassDef):
            name = f"{module}.{node.name}"
            methods = [c for c in node.body if isinstance(c, defs)]
            # A class is searched by its header, docstring and method
            # names, not its whole body, which its methods already are.
            head = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            search = "\n".join([head, ast.get_docstring(node) or "",
                                " ".join(m.name for m in methods)])
            out.append(_entry("api:" + name, "api", name,
                              f"{shown}:{node.lineno}", span(node), search,
                              line=node.lineno))
            for child in methods:
                if child.name.startswith("_") and child.name != "__init__":
                    continue
                cname = f"{name}.{child.name}"
                out.append(_entry("api:" + cname, "api", cname + "()",
                                  f"{shown}:{child.lineno}", span(child),
                                  line=child.lineno))
    return out


def example_entry(path: str, shown: str) -> dict:
    source = _read_text(path)
    if "\x00" in source:
        raise ValueError("not text")
    return _entry("example:" + shown, "example", shown, f"{shown}:1", source)


class _Node(object):
    __slots__ = ("tag", "attrs", "children", "text", "line")

    def __init__(self, tag, attrs, line):
        self.tag, self.attrs, self.line = tag, attrs, line
        self.children: list[_Node] = []
        self.text = ""


def _parse_xml(path: str) -> _Node:
    """A small tree with line numbers (ElementTree keeps none)."""
    parser = xml.parsers.expat.ParserCreate()
    root = _Node("", {}, 0)
    stack = [root]

    def start(tag, attrs):
        node = _Node(tag, attrs, parser.CurrentLineNumber)
        stack[-1].children.append(node)
        stack.append(node)

    def end(tag):
        stack.pop()

    def data(text):
        stack[-1].text += text

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    with open(path, "rb") as handle:
        parser.ParseFile(handle)
    return root


def _render_hash(node: _Node, depth: int = 0) -> list[str]:
    """A help hash as indented lines: its atoms, then its child hashes."""
    pad = "  " * depth
    out = []
    for child in node.children:
        if child.tag == "atom":
            text = " ".join(child.text.split())
            if text:
                out.append(f"{pad}{child.attrs.get('type', '')}: {text}")
    for child in node.children:
        if child.tag == "hash":
            out.append(f"{pad}{child.attrs.get('type', '')} "
                       f"{child.attrs.get('key', '')}")
            out += _render_hash(child, depth + 1)
    return out


def _atom(node: _Node, kind: str) -> str:
    for child in node.children:
        if child.tag == "atom" and child.attrs.get("type") == kind:
            return " ".join(child.text.split())
    return ""


HELP_TYPES = {"Command": "command", "Tool": "tool",
              "ArgumentType": "argtype", "Item": "item"}


def help_entries(path: str, shown: str) -> list[dict]:
    root = _parse_xml(path)
    out = []

    def visit(node):
        for child in node.children:
            if child.tag != "hash":
                visit(child)
                continue
            kind = HELP_TYPES.get(child.attrs.get("type", ""))
            key = child.attrs.get("key", "")
            if not kind:
                visit(child)
                continue
            if "@" in key:
                key, lang = key.split("@", 1)
                if lang != "en_US":
                    continue
            name = _atom(child, "UserName")
            title = f"{key} ({kind})" + (f": {name}" if name else "")
            text = "\n".join([title] + _render_hash(child, 1))
            search = None
            if kind == "item":
                # An item is searched by its own words and its channels'
                # names; each channel's help is an entry of its own.
                search = "\n".join([title] + [
                    c.attrs.get("key", "") for c in child.children
                    if c.tag == "hash"])
            out.append(_entry(f"help:{kind}:{key}", "help", title,
                              f"{shown}:{child.line}", text, search,
                              line=child.line))
            if kind == "item":
                for chan in child.children:
                    if (chan.tag == "hash"
                            and chan.attrs.get("type", "").lower() == "channel"):
                        ckey = chan.attrs.get("key", "")
                        cname = _atom(chan, "UserName")
                        ctitle = f"{key}.{ckey} (channel)" + (
                            f": {cname}" if cname else "")
                        ctext = "\n".join([ctitle, f"  of item type {key}"]
                                          + _render_hash(chan, 1))
                        out.append(_entry(f"help:channel:{key}.{ckey}", "help",
                                          ctitle, f"{shown}:{chan.line}",
                                          ctext, line=chan.line))

    visit(root)
    return out


def read_facts(path: str = FACTS) -> list[dict]:
    """modo_facts.md: one entry per ## section."""
    try:
        source = _read_text(path)
    except OSError:
        return []
    out = []
    sections = re.split(r"(?m)^## ", source)
    line = sections[0].count("\n") + 1
    for section in sections[1:]:
        title = section.splitlines()[0].strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        out.append(_entry("facts:" + slug, "facts", title,
                          f"modo_facts.md:{line}", "## " + section.strip(),
                          line=line))
        line += section.count("\n")
    return out


def lx_entries(dump: dict) -> list[dict]:
    """The kit's api_dump: lx.object and lx.service classes and methods with
    their signature docstrings, and lx.symbol names with values."""
    version = dump.get("version") or "?"
    where = f"lx module (Modo {version})"
    out = []
    for group in ("object", "service"):
        for cls, info in sorted((dump.get(group + "s") or {}).items()):
            methods = info.get("methods") or {}
            name = f"lx.{group}.{cls}"
            text = "\n".join([name] + [f"  {doc or m}" for m, doc in
                                       sorted(methods.items())])
            out.append(_entry("api:" + name, "api", name, where, text))
            for method, doc in sorted(methods.items()):
                out.append(_entry(f"api:{name}.{method}", "api",
                                  f"{name}.{method}()", where,
                                  f"{name}.{method}: {doc or method}"))
    for symbol, value in sorted((dump.get("symbols") or {}).items()):
        out.append(_entry("api:lx.symbol." + symbol, "api",
                          "lx.symbol." + symbol, where,
                          f"lx.symbol.{symbol} = {value!r}"))
    return out


def _write_json(path: str, data: dict) -> None:
    """Write whole or not at all. The .tmp name is this process's and this
    thread's: several servers (one per Claude Code session) share the
    folder, and on Windows a second writer of one .tmp name is refused."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_lx_dump(dump: dict) -> str:
    os.makedirs(docs_dir(), exist_ok=True)
    version = re.sub(r"[^\w.]+", "_", str(dump.get("version") or "unknown"))
    path = os.path.join(docs_dir(), f"lx-{version}.json")
    _write_json(path, dump)
    return path


def has_lx_dump(version: str) -> bool:
    version = re.sub(r"[^\w.]+", "_", str(version))
    return os.path.isfile(os.path.join(docs_dir(), f"lx-{version}.json"))


# -- the index --------------------------------------------------------------

class Index(object):
    K1, B = 1.4, 0.72

    def __init__(self, data: dict):
        self.data = data
        self.entries: list[dict] = data["entries"]
        self.built = data.get("built")
        self.by_id = {e["id"]: n for n, e in enumerate(self.entries)}
        self.postings: dict[str, dict[int, int]] = {}
        self.lengths: list[int] = []
        for n, entry in enumerate(self.entries):
            tokens = words(entry["title"] + "\n" + entry.get("search",
                                                             entry["text"]))
            # The title's words count twice: a name is the best signal.
            tokens += words(entry["title"])
            self.lengths.append(len(tokens) or 1)
            for word, count in Counter(tokens).items():
                self.postings.setdefault(word, {})[n] = count
        self.average = (sum(self.lengths) / len(self.lengths)
                        if self.lengths else 1.0)

    def get(self, id_: str) -> dict | None:
        n = self.by_id.get(id_)
        return None if n is None else self.entries[n]

    def search(self, query: str, kind: str | None, limit: int) -> list[dict]:
        tokens = list(dict.fromkeys(words(query)))
        total = len(self.entries)
        scores: dict[int, float] = {}
        for word in tokens:
            postings = self.postings.get(word)
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5)
                           / (len(postings) + 0.5))
            for n, freq in postings.items():
                norm = 1 - self.B + self.B * self.lengths[n] / self.average
                scores[n] = scores.get(n, 0.0) + idf * (
                    freq * (self.K1 + 1) / (freq + self.K1 * norm))
        # A query that names an entry. Most for its whole name
        # (lx.service.Layer, select.symmetry), then for the name's end
        # (Mesh.geometry), then for its last part, or a method named
        # without its prefix (GetPixel for img_GetPixel), with any parts
        # before it in the name too (modo.Mesh for modo.item.Mesh).
        # Only words that look like names count (a lone word, or one with
        # an inner capital, _ or .): "backdrop" in a sentence is a word.
        found = re.findall(r"[\w.]+", query)
        names = {q.strip(".").lower() for q in found
                 if len(found) == 1 or re.search(r"[a-z][A-Z]|[_.]", q)}
        names.discard("")
        for n in list(scores) if names else ():
            name = self.entries[n]["id"].split(":")[-1].lower()
            parts = name.split(".")
            last = parts[-1]
            short = last.split("_", 1)[-1]
            bonus = 0.0
            for q in names:
                if q == name:
                    bonus = max(bonus, 24.0)
                elif name.endswith("." + q):
                    bonus = max(bonus, 16.0)
                elif q in (last, short):
                    bonus = max(bonus, 8.0)
                elif "." in q:
                    asked = q.split(".")
                    if asked[-1] in (last, short) and all(
                            p in parts for p in asked[:-1]):
                        bonus = max(bonus, 12.0)
            scores[n] += bonus
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for n, score in ranked:
            entry = self.entries[n]
            if kind and entry["kind"] != kind:
                continue
            out.append(dict(entry, score=round(score, 2)))
            if len(out) >= limit:
                break
        return out


_loaded: Index | None = None
# An agent sends its searches together, and each runs on its own thread:
# one loads or builds the index, the others wait for it.
_lock = threading.Lock()


def forget() -> None:
    """Drop the index this process holds (the tests use this)."""
    global _loaded
    _loaded = None


def build(install: str | None, groups: dict, key: str) -> dict:
    started = time.time()
    entries: list[dict] = []
    skipped: list[str] = []
    for path, shown in groups["api"]:
        try:
            entries += python_entries(path, shown)
        except (OSError, SyntaxError, ValueError, RecursionError):
            skipped.append(shown)
    for path, shown in groups["help"]:
        try:
            entries += help_entries(path, shown)
        except (OSError, xml.parsers.expat.ExpatError):
            skipped.append(shown)
    for path, shown in groups["example"]:
        try:
            entries.append(example_entry(path, shown))
        except (OSError, ValueError):
            skipped.append(shown)
    facts = read_facts()
    entries += facts
    lx_dump = None
    dumps = lx_dumps()
    if dumps:
        try:
            with open(dumps[-1], encoding="utf-8") as handle:
                lx_dump = json.load(handle)
            entries += lx_entries(lx_dump)
        except (OSError, ValueError):
            skipped.append(os.path.basename(dumps[-1]))
            lx_dump = None
    # The same id twice (a module and a package of one name): keep the first.
    seen, unique = set(), []
    for entry in entries:
        if entry["id"] not in seen:
            seen.add(entry["id"])
            unique.append(entry)
    return {"version": INDEX_VERSION, "stamp": key, "install": install,
            "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "build_s": round(time.time() - started, 2),
            "skipped": skipped,
            "lx": (lx_dump or {}).get("version") if lx_dump else None,
            "facts": len(facts),
            "counts": dict(Counter(e["kind"] for e in unique)),
            "entries": unique}


def load() -> Index:
    """The index, rebuilt when a source changed or the file is broken."""
    with _lock:
        return _load()


def _load() -> Index:
    global _loaded
    install = find_install()
    groups = source_files(install)
    key = stamp(install, groups)
    if _loaded is not None and _loaded.data.get("stamp") == key:
        return _loaded
    data = None
    try:
        with open(index_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if (not isinstance(data, dict) or data.get("stamp") != key
                or data.get("version") != INDEX_VERSION):
            data = None
    except (OSError, ValueError):
        data = None
    if data is None:
        data = build(install, groups, key)
        # The file is a cache. Another server process reading it (Windows
        # then refuses the replace) or a full disk doesn't fail the search:
        # this process keeps the index it built.
        try:
            os.makedirs(docs_dir(), exist_ok=True)
            _write_json(index_path(), data)
        except OSError:
            pass
    _loaded = Index(data)
    return _loaded


# -- answers ----------------------------------------------------------------

def _around(entry: dict, query: str) -> str:
    """The few lines of an entry where the query's words are thickest."""
    lines = entry["text"].splitlines() or [""]
    wanted = set(words(query))
    best, best_n = 0, -1
    for n, line in enumerate(lines):
        hits = len(wanted & set(words(line)))
        if hits > best_n:
            best, best_n = n, hits
    start = max(0, best - 1)
    text = "\n".join(lines[start:start + 4])
    if len(text) > HIT_CHARS:
        text = text[:HIT_CHARS - 3] + "..."
    return text


LX_MISSING = ("The lx module (lx.object, lx.service, lx.symbol) is "
              "missing: it is read from Modo once, the first time a "
              "search runs while Modo is open.")


def _note(index: Index, lx_why: str = "") -> str:
    data = index.data
    notes = []
    if not data.get("install"):
        notes.append("No Modo install found under C:\\Program Files\\Modo*"
                     "\\modo (set MODO_TOOLS_MODO_DIR): only the bundled "
                     "facts and any lx dump were searched.")
    if not data.get("lx"):
        notes.append(lx_why or LX_MISSING)
    return " ".join(notes)


def check_query(query: str, kind: str | None) -> str | None:
    if not (query or "").strip():
        raise DocsError(protocol.BAD_REQUEST, "the query is empty",
                        "Ask for words or a name, e.g. 'Image GetPixel'.")
    kind = (kind or "").strip() or None
    if kind is not None and kind not in KINDS:
        raise DocsError(protocol.BAD_REQUEST, f"no kind {kind!r}",
                        "kind is one of " + ", ".join(KINDS) + ", or empty.")
    return kind


def search(query: str, kind: str | None = None, limit: int = 8,
           lx_why: str = "") -> dict:
    """lx_why: the server's reason the lx dump couldn't be made, if any."""
    kind = check_query(query, kind)
    limit = max(1, min(int(limit), 50))
    index = load()
    hits = [{"id": e["id"], "kind": e["kind"], "title": e["title"],
             "where": e["where"], "text": _around(e, query)}
            for e in index.search(query, kind, limit)]
    answer: dict[str, Any] = {"hits": hits}
    notes = []
    if not hits and not words(query):
        notes.append("The query has no words to search: a word is a name or "
                     "letters, two or more, and 'lx' and words like 'the' "
                     "don't count. Ask for a class, a method or what it does.")
    elif not hits:
        notes.append("No entry has these words" + (f" in kind {kind}" if kind
                                                    else "")
                     + ". Try other words or a name. Not every command has "
                     "help on disk: mt_find_commands and mt_command_info "
                     "read commands from Modo.")
    notes.append(_note(index, lx_why))
    note = " ".join(n for n in notes if n)
    if note:
        answer["note"] = note
    if index.data.get("skipped"):
        answer["skipped"] = len(index.data["skipped"])
    return answer


def read(id_: str, offset: int = 0, limit_chars: int = 40000) -> dict:
    """One entry whole, from line offset on, cut to limit_chars of JSON."""
    index = load()
    entry = index.get((id_ or "").strip())
    if entry is None:
        # An id typed without its kind, or in another case: name the real one.
        wanted = (id_ or "").strip().lower()
        tail = wanted.split(":")[-1]
        near = [e["id"] for e in index.entries
                if tail and e["id"].lower().split(":")[-1] == tail][:5]
        hint = "Take an id from mt_search_docs."
        if near:
            hint = "Did you mean " + ", ".join(near) + "? " + hint
        raise DocsError(protocol.NOT_FOUND, f"no entry {id_!r}", hint)
    lines = entry["text"].splitlines()
    base = {"id": entry["id"], "kind": entry["kind"], "title": entry["title"],
            "where": entry["where"]}
    if lines and int(offset) >= len(lines):
        return dict(base, lines=[0, 0, len(lines)], text="",
                    note=f"offset {int(offset)} is past the end: the entry "
                         f"has {len(lines)} lines, and offset counts them "
                         "from 0.")
    offset = max(0, min(int(offset), len(lines)))

    def shape(n):
        out = dict(base, lines=[offset + 1, offset + n, len(lines)],
                   text="\n".join(lines[offset:offset + n]))
        if offset + n < len(lines):
            out["next"] = offset + n
            out["more"] = (f"{len(lines) - offset - n} more lines: read on "
                           f"with offset={offset + n}")
        return out

    low, high, best = 0, len(lines) - offset, None
    while low <= high:
        n = (low + high) // 2
        if len(json.dumps(shape(n))) <= limit_chars:
            best, low = n, n + 1
        else:
            high = n - 1
    if not best:
        # One line longer than the budget: cut that line.
        out = shape(1)
        room = max(0, limit_chars - len(json.dumps(dict(out, text=""))) - 10)
        out["text"] = out["text"][:room]
        return out
    return shape(best)
