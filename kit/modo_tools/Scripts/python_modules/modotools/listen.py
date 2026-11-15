# python
"""The user's edits between calls, counted for the journal.

A probe (Modo 17.1, 2026-11-08): a Python lxifc.SceneItemListener is told of a
hand move (ChannelValue on the locator's transform item, "Position (Camera)"),
an item added by hand (ItemAdd for it and its transforms) and a mesh edit by
hand (ChannelValue on the mesh), always on the main thread. A job's own edits
come through it too, while the guard is active; Ctrl+Z and Ctrl+Shift+Z come
with the undo service's State() at 0 instead of 1 (iUNDO_ACTIVE). Neither is
the user's edit: the job is its entry, and Ctrl+Z is written by the markers. What is left is counted, and the journal writes it as a gap record
before the next entry. A 200,934-polygon bevel gave 6 events and 0.1 ms in
the callbacks.

It only counts: no file is written from a callback. A callback that raises
turns the listener off for the session, and status says so; it is removed at
the next job, never from inside Modo's call to it.
"""

from . import guard

# Items named in one gap, at most; the count goes on.
KEEP_ITEMS = 20


def _ident(item):
    import lx
    return lx.object.Item(item).Ident()


def _undo_state():
    import lx
    return lx.service.Undo().State()


class Listen(object):
    """Kit.listen: the scene item listener and what it counted since the
    last take()."""

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        self._com = None
        self.error = None
        self.ident = _ident
        self.undo_state = _undo_state
        self._clear()

    def _clear(self):
        self.events = 0
        self.added = 0
        self.removed = 0
        self.items = []

    # -- on and off ------------------------------------------------------------

    def start(self):
        """Main thread, at kit start. A probe: RemoveListener only finds the
        wrapper AddListener was given, so it is kept."""
        try:
            import lx
            self._com = lx.object.Unknown(_make(self))
            lx.service.Listener().AddListener(self._com)
        except Exception as exc:
            self._com = None
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def stop(self):
        com, self._com = self._com, None
        if com is None:
            return
        try:
            import lx
            lx.service.Listener().RemoveListener(com)
        except Exception as exc:
            self._log("modo-tools: the scene listener couldn't be removed: "
                      "%s" % exc)

    def status(self):
        return {"on": self._com is not None and self.error is None,
                "error": self.error}

    # -- what Modo calls -------------------------------------------------------

    def heard(self, kind, item=None):
        """One event. Never raises into Modo: a failure turns listening off."""
        if self._com is None or self.error is not None:
            return
        try:
            if guard._active is not None or self.undo_state() != 1:
                return
            self.events += 1
            if kind == "add":
                self.added += 1
            elif kind == "remove":
                self.removed += 1
            if item is not None:
                ident = self.ident(item)
                if ident not in self.items and len(self.items) < KEEP_ITEMS:
                    self.items.append(ident)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            try:
                self._log("modo-tools: the scene listener failed and is off "
                          "until the kit restarts: %s" % self.error)
            except Exception:
                pass
            # Taken off at the next take() or stop(), not from inside Modo's
            # call to it.

    # -- what the journal takes ------------------------------------------------

    def peek(self):
        """What was heard since the last take, without forgetting it: the
        item ids, or None when nothing was (7.8 names the user's step)."""
        if not self.events:
            return None
        return list(self.items)

    def take(self):
        """What was heard since the last take, as {"events", "added",
        "removed", "items": [ids]}, or None; then forget it. A listener that
        failed is taken off here, in a job, outside Modo's call to it."""
        if self.error is not None:
            self.stop()
        if not self.events:
            return None
        heard = {"events": self.events, "added": self.added,
                 "removed": self.removed, "items": list(self.items)}
        self._clear()
        return heard


def _make(listen):
    import lxifc

    class SceneItems(lxifc.SceneItemListener):
        def sil_ItemAdd(self, item):
            listen.heard("add", item)

        def sil_ItemRemove(self, item):
            listen.heard("remove", item)

        def sil_ItemName(self, item):
            listen.heard("change", item)

        def sil_ItemParent(self, item):
            listen.heard("change", item)

        def sil_ChannelValue(self, action, item, index):
            listen.heard("change", item)

    return SceneItems()
