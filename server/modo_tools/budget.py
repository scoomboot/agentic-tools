"""The size budget: every answer fits in MODO_TOOLS_MAX_CHARS characters.

An answer over the budget is not an error. It is cut at the end, keeps its
shape (still JSON, the start of the tree or list), and says what was cut and
how to ask for less. Each fit_* finds the most that fits by halving: render,
measure, keep more or less.

The idea is modo-mcp's (formatting.py); the code is ours.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

DEFAULT_MAX_CHARS = 40000


def max_chars() -> int:
    try:
        return max(1000, int(os.environ.get("MODO_TOOLS_MAX_CHARS", "")))
    except ValueError:
        return DEFAULT_MAX_CHARS


def render(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def cut_text(text: str, limit: int) -> str:
    """The last resort for an answer no fit_* knows the shape of."""
    if len(text) <= limit:
        return text
    note = "\n... cut at {:,} characters of {:,}; ask for less.".format(
        limit, len(text))
    return text[:max(0, limit - len(note))] + note


def _fit(size: int, keep: Callable[[int], Any], limit: int,
         render: Callable[[Any], str] = render) -> str | None:
    """The rendering of keep(n) for the largest n that fits, or None."""
    low, high, best = 0, size, None
    while low <= high:
        n = (low + high) // 2
        text = render(keep(n))
        if len(text) <= limit:
            best, low = text, n + 1
        else:
            high = n - 1
    return best


def _labelled(value: dict, note: str) -> dict:
    out = {"truncated": note}
    out.update(value)
    return out


def _count(nodes: list) -> int:
    return sum(1 + _count(n.get("children", [])) for n in nodes)


def _prune(nodes: list, room: int) -> tuple[list, int]:
    """The first `room` nodes of the tree in order. A node that loses
    children gets "more": how many of its children aren't listed."""
    out = []
    for node in nodes:
        if room <= 0:
            break
        copy = {k: v for k, v in node.items() if k != "children"}
        room -= 1
        children = node.get("children") or []
        if children:
            kept, room = _prune(children, room)
            if kept:
                copy["children"] = kept
            lost = len(children) - len(kept) + node.get("more", 0)
            if lost:
                copy["more"] = lost
        out.append(copy)
    return out, room


def fit_graph(result: dict, limit: int) -> str:
    text = render(result)
    if len(text) <= limit:
        return text
    nodes = result.get("items") or []
    total = result.get("count", _count(nodes))
    size = _count(nodes)

    def keep(n):
        kept, _ = _prune(nodes, n)
        note = ("{:,} of {:,} items shown; ask with root= or types=, or a "
                "smaller depth".format(_count(kept), total))
        return _labelled(dict(result, items=kept), note)

    return _fit(size, keep, limit) or cut_text(text, limit)


def fit_entries(result: dict, key: str, limit: int, unit: str, ask: str,
                render: Callable[[Any], str] = render) -> str:
    """Cut result[key], a list or a dict, keeping its first entries."""
    text = render(result)
    entries = result.get(key)
    if len(text) <= limit or not entries:
        return cut_text(text, limit)
    pairs = list(entries.items()) if isinstance(entries, dict) else list(entries)

    def keep(n):
        part = dict(pairs[:n]) if isinstance(entries, dict) else pairs[:n]
        note = "{:,} of {:,} {} shown; {}".format(n, len(pairs), unit, ask)
        return _labelled(dict(result, **{key: part}), note)

    return _fit(len(pairs), keep, limit, render) or cut_text(text, limit)


def fit_selection(result: dict, limit: int) -> str:
    """Cut the selected elements' indices, mesh by mesh, in order."""
    text = render(result)
    meshes = result.get("meshes")
    if len(text) <= limit or not meshes:
        return fit_entries(result, "items", limit, "items",
                           "the selection is too large to list whole")
    total = sum(len(m["indices"]) for m in meshes)
    unit = {"vertex": "vertices", "edge": "edges"}.get(result.get("type"), "polygons")

    def keep(n):
        out, room = [], n
        for mesh in meshes:
            if room <= 0:
                break
            out.append(dict(mesh, indices=mesh["indices"][:room]))
            room -= len(mesh["indices"])
        note = ("{:,} of {:,} {} shown; select fewer, or read them with "
                "mt_run_python".format(min(n, total), total, unit))
        return _labelled(dict(result, meshes=out), note)

    return _fit(total, keep, limit) or cut_text(text, limit)
