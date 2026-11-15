# python
"""Modo's event log, read back.

Checked on Modo 17.1 (2026-11-08): the event log is the log service's
primary subsystem; each entry is wrapped as lx.object.LogEntry and its text
is Message(). lx.out lines show up there at once, in order.

run_job marks the log before a job and, when the job fails, attaches the
lines written since: Modo often says why a command failed only there.

An entry can have child entries: a script run with @ writes its lx.out
lines and its exception under the line naming the script (17.1,
2026-11-08), and only there says why it failed. They come back indented
two spaces a level, after their entry.
"""

from .jobs import op

MAX_LINES = 500


def _primary():
    import lx
    return lx.service.Log().PrimarySubSystem()


def _text(sub, index):
    import lx
    return str(lx.object.LogEntry(sub.EntryByIndex(index)).Message())


def _lines(sub, index):
    """An entry's line, then its children's, indented."""
    import lx
    out = []

    def walk(raw, depth):
        entry = lx.object.LogEntry(raw)
        out.append("  " * depth + str(entry.Message()))
        try:
            count = entry.ChildCount()
        except Exception:
            return
        for i in range(count):
            walk(entry.ChildByIndex(i), depth + 1)
    walk(sub.EntryByIndex(index), 0)
    return out


def _span(sub, start, end):
    return [line for i in range(start, end) for line in _lines(sub, i)]


def last(count):
    """The last `count` entries' lines, oldest first. [] when the log can't be read."""
    try:
        sub = _primary()
        total = sub.EntryCount()
        return _span(sub, max(0, total - count), total)
    except Exception:
        return []


def mark():
    """Where the log stands: (entry count, last line)."""
    try:
        sub = _primary()
        total = sub.EntryCount()
        return (total, _text(sub, total - 1) if total else None)
    except Exception:
        return None


def since(marked, limit=20):
    """Lines written after mark(), newest last, of at most `limit` entries.

    A full log rolls: the count stays put while old lines drop off the front.
    Then the marked last line is looked for and what follows it is new.
    """
    if marked is None:
        return []
    count, last_line = marked
    try:
        sub = _primary()
        total = sub.EntryCount()
        if total > count:
            start = count
        else:
            start = total
            for index in range(total - 1, max(-1, total - 200), -1):
                if _text(sub, index) == last_line:
                    start = index + 1
                    break
        start = max(start, total - limit)
        return _span(sub, start, total)
    except Exception:
        return []


@op("event_log", read=True)
def event_log(payload, kit):
    try:
        count = int(payload.get("count", 30))
    except (TypeError, ValueError):
        count = 30
    count = max(1, min(count, MAX_LINES))
    return {"lines": last(count)}
