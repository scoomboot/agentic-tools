#!/usr/bin/env python3
"""Package the kit as modo_tools.lpk.

An .lpk is a zip with an index.xml that lists each file and where it goes.
Drag it into a Modo viewport (or double-click it) and Modo installs the kit
into its kits folder; restart="YES" because a kit only loads at start-up.
The manifest's shape is modo-mcp's build_lpk.py; the code is ours.

    python kit/modo_tools/build_lpk.py [--out DIR] [--version 1710]

Writes dist/modo_tools.lpk in the repo unless --out says otherwise.
"""

import argparse
import os
import sys
import zipfile
from xml.sax.saxutils import escape, quoteattr

KIT_NAME = "modo_tools"
KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_DIST = os.path.normpath(os.path.join(KIT_ROOT, "..", "..", "dist"))

# Stale bytecode in a kit folder outlives the source it came from.
SKIP_DIRS = {"__pycache__", "dist"}
SKIP_FILES = {"build_lpk.py", "index.xml"}

MESSAGE = ("modo-tools is installed. Restart Modo, then choose "
           "Modo Tools > Start, or type modoTools.start in the command line.")


def collect_files(root=KIT_ROOT):
    """Kit-relative paths, with forward slashes, of every file to ship."""
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            if name in SKIP_FILES or name.endswith(".pyc"):
                continue
            path = os.path.join(folder, name)
            found.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return sorted(found)


def index_xml(files, version="1710"):
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<package version=%s>' % quoteattr(version),
             '\t<kit name="%s" restart="YES">' % KIT_NAME]
    for relative in files:
        lines.append('\t\t<source target=%s>%s</source>'
                     % (quoteattr(KIT_NAME + "/" + relative), escape(relative)))
    lines.append('\t</kit>')
    lines.append('\t<message button="OK">%s</message>' % escape(MESSAGE))
    lines.append('</package>')
    return "\n".join(lines) + "\n"


def build(out=REPO_DIST, version="1710", root=KIT_ROOT):
    """Write <out>/modo_tools.lpk and return its path."""
    files = collect_files(root)
    if not files:
        raise SystemExit("no kit files found under %s" % root)
    if not os.path.isdir(out):
        os.makedirs(out)
    path = os.path.join(out, KIT_NAME + ".lpk")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.xml", index_xml(files, version))
        for relative in files:
            archive.write(os.path.join(root, relative), relative)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=REPO_DIST,
                        help="folder to write the .lpk to (default: dist/)")
    parser.add_argument("--version", default="1710",
                        help="lowest Modo version, e.g. 1710 for 17.1")
    args = parser.parse_args(argv)
    path = build(args.out, args.version)
    print("Built %s (%d files, %.1f kB)" % (
        path, len(collect_files()), os.path.getsize(path) / 1024.0))
    print("Install: drag it into a Modo viewport, then restart Modo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
