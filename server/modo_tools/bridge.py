# Parts adapted from modo-mcp 0.3.0, Copyright (c) 2026 Leonard Billeke,
# under the MIT License; see THIRD_PARTY_NOTICES.md.
"""The server's side of the connection to the kit inside Modo.

Opens a connection on the first call and a new one after any error, so a
Modo restart or a modoTools.stop/start needs nothing from the agent. The
first message on each connection is hello, with the token from
~/.modo_tools/token, read at connect time because Modo may start after us.

A call that waits long gets a watch beside it (watch.py), which tells the agent
whether Modo is busy, stuck behind a dialog, or not running Python at all, and
cancels a dialog that opens in the call's own job.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from typing import Any

from . import budget, protocol, watch as watch_mod


class BridgeError(Exception):
    """The kit couldn't be asked, or refused to be. `type` is one of the
    protocol's error types."""

    def __init__(self, err_type: str, message: str, hint: str = ""):
        super().__init__(message)
        self.type = err_type
        self.message = message
        self.hint = hint

    def text(self) -> str:
        out = f"{self.type}: {self.message}"
        if self.hint:
            out += f"\nWhat to do: {self.hint}"
        return out


def read_token() -> str | None:
    try:
        with open(protocol.token_path(), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


class Bridge:
    def __init__(self, host: str = protocol.DEFAULT_HOST,
                 port: int = protocol.DEFAULT_PORT,
                 connect_timeout: float = 5.0,
                 grace_s: float = 5.0,
                 watch: watch_mod.Watch | None | bool = True):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        # Wait this much longer than the kit does, so the kit's own timeout
        # answer (which says whether the job ran) arrives first.
        self.grace_s = grace_s
        if watch is True:
            watch = watch_mod.Watch(host, port)
        self.watch: watch_mod.Watch | None = watch or None
        self.server_info: dict[str, Any] = {}
        # One per server process, sent in every hello, so the kit tells this
        # server's calls from another's across reconnects.
        self.session = uuid.uuid4().hex
        # The label of the action this server has open, as far as its answers
        # say; the kit holds the real one. For refusing before sending.
        self.action: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    # -- the connection ------------------------------------------------------

    def _unreachable(self, detail: str = "") -> BridgeError:
        message = f"Modo isn't reachable on {self.port}"
        if detail:
            message += f" ({detail})"
        stopped = watch_mod.last_stop()
        if stopped:
            message += f". {stopped}"
        return BridgeError(
            protocol.UNREACHABLE, message,
            "Modo is closed or the kit is stopped. Start Modo, then run "
            "modoTools.start in its command line, and call again.")

    async def _connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                # asyncio's default line limit is 64 KiB; a scene graph of
                # 2,000 items is more. The kit refuses lines over this one.
                asyncio.open_connection(self.host, self.port,
                                        limit=protocol.MAX_LINE_BYTES),
                timeout=self.connect_timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            await self.close()
            # Refused can also be a kit whose listen queue filled while a
            # dialog held Python in Modo: Modo's windows tell.
            if self.watch is not None:
                seen = self.watch.without_kit(refused=True)
                if seen.kind != "unreachable":
                    raise seen_error(seen) from exc
            raise self._unreachable(type(exc).__name__) from exc

        try:
            hello = await self._roundtrip(
                {"id": "hello", "op": "hello",
                 "payload": {"token": read_token() or "",
                             "protocol": protocol.PROTOCOL_VERSION,
                             "session": self.session}},
                self.connect_timeout)
        except asyncio.TimeoutError as exc:
            # The connection opened (the OS does that) but nothing answered.
            await self.close()
            if self.watch is not None:
                seen = await self.watch.look(None)
                if seen.kind in ("stuck_hard", "stuck", "dialog", "answered"):
                    raise seen_error(seen) from exc
            raise BridgeError(
                protocol.TIMEOUT,
                f"Modo didn't answer hello in {self.connect_timeout:.0f} s",
                "Call mt_status in a moment.") from exc
        except (OSError, ValueError) as exc:
            await self.close()
            raise self._unreachable(str(exc) or type(exc).__name__) from exc
        if not hello.get("ok"):
            await self.close()
            err = hello.get("error") or {}
            raise BridgeError(err.get("type", "error"),
                              err.get("message", "hello refused"),
                              err.get("hint", ""))
        info = hello.get("result") or {}
        if info.get("protocol") != protocol.PROTOCOL_VERSION:
            await self.close()
            theirs = info.get("protocol")
            older = "kit" if (isinstance(theirs, int)
                              and theirs < protocol.PROTOCOL_VERSION) else "server"
            raise BridgeError(
                protocol.PROTOCOL_MISMATCH,
                f"the kit speaks protocol {theirs!r}, the server speaks "
                f"{protocol.PROTOCOL_VERSION}",
                f"Update the {older}, so both halves are the same version.")
        self.server_info = info

    async def close(self) -> None:
        writer = self._writer
        self._reader = self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _roundtrip(self, request: dict[str, Any],
                         timeout_s: float) -> dict[str, Any]:
        assert self._reader is not None and self._writer is not None
        self._writer.write(protocol.encode(request))
        await self._writer.drain()
        raw = await asyncio.wait_for(self._reader.readline(), timeout=timeout_s)
        if not raw:
            raise ConnectionResetError("the kit closed the connection")
        return protocol.decode(raw)

    # -- calls -----------------------------------------------------------------

    async def call(self, op: str, payload: dict[str, Any] | None = None,
                   timeout_ms: int = protocol.DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        """Send one op; return the kit's answer (ok or not). Raises
        BridgeError when the kit can't be asked at all."""
        request = protocol.make_request(f"r-{next(self._ids)}", op,
                                        payload or {}, timeout_ms)
        wait_s = timeout_ms / 1000.0 + self.grace_s
        async with self._lock:
            reused = self._writer is not None and not self._writer.is_closing()
            for attempt in (1, 2):
                if self._writer is None or self._writer.is_closing():
                    await self._connect()
                seen: list[watch_mod.Seen] = []
                try:
                    response = await self._watched(request, wait_s, seen)
                    return judge(response, seen)
                except asyncio.TimeoutError as exc:
                    await self.close()
                    raise BridgeError(
                        protocol.TIMEOUT,
                        f"no answer from Modo in {wait_s:.0f} s",
                        "Modo may be showing a dialog or running a long job. "
                        "Check Modo, then call mt_status.") from exc
                except (OSError, ConnectionError, ValueError) as exc:
                    await self.close()
                    # A connection kept from an earlier call can be dead
                    # (Modo restarted, kit stopped) without our knowing until
                    # now. Such a request never reached a live kit, so one
                    # retry on a fresh connection is safe.
                    if attempt == 1 and reused:
                        continue
                    raise self._unreachable(str(exc) or type(exc).__name__) from exc
        raise self._unreachable()

    async def _watched(self, request: dict[str, Any], wait_s: float,
                       seen: list[watch_mod.Seen]) -> dict[str, Any]:
        """The roundtrip, with the watch beside it when the call can wait
        long enough for the watch to matter."""
        if self.watch is None or wait_s <= self.watch.first_s:
            return await self._roundtrip(request, wait_s)
        answer = asyncio.ensure_future(self._roundtrip(request, wait_s))
        watcher = asyncio.ensure_future(self.watch.run(request["id"], seen))
        try:
            await asyncio.wait({answer, watcher},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (answer, watcher):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
        if answer.done() and not answer.cancelled():
            return answer.result()
        # stuck_hard. Close this connection: an answer that turns up later
        # would otherwise be read as the next call's. The job's ending, if it
        # has one, is in the op log.
        await self.close()
        raise stuck_hard(watcher.result())


def seen_error(seen: watch_mod.Seen) -> BridgeError:
    """The error for a call that couldn't reach the kit, from what the watch
    saw instead."""
    if seen.kind == "stuck_hard":
        return stuck_hard(seen)
    if seen.kind == "answered":
        return BridgeError(protocol.STUCK, seen.text,
                           "Call again: Modo should answer now.")
    return BridgeError(protocol.STUCK, seen.text, DIALOG_HINT)


DIALOG_HINT = (
    "Ask the user to answer the dialog in Modo (Enter or Esc), then call again. "
    "modo-tools cancels only the dialogs that open in its own jobs.")


def stuck_hard(seen: watch_mod.Seen) -> BridgeError:
    return BridgeError(
        protocol.STUCK_HARD, seen.text,
        "This is how the Modo 17.1 Python-lock freeze looks. If Modo's window "
        "says Not Responding, end Modo in Task Manager; after a restart and "
        "modoTools.start, mt_status names the job. If Modo is only working "
        "through a heavy command, wait and call mt_status. If Modo's window "
        "responds, a dialog is open instead: press Enter or Esc in it. Either "
        "way, mt_op_log shows how the job ended, if it did.")


def judge(response: dict[str, Any], seen: list[watch_mod.Seen]) -> dict[str, Any]:
    """Put what the watch saw on the kit's answer. A job that never started
    becomes stuck or busy when the watch saw why."""
    if not seen:
        return response
    response = dict(response)
    response["watch"] = seen
    if response.get("ok"):
        return response
    err = dict(response.get("error") or {})
    kinds = [s.kind for s in seen]
    if err.get("type") == protocol.TIMEOUT:
        if kinds[-1] == "dialog":
            err.update(type=protocol.STUCK, message=err.get("message", "")
                       + "; " + seen[-1].text, hint=(
                "Ask the user to answer the dialog in Modo, then call again. The "
                "job never started, so nothing changed."))
        elif kinds[-1] == "stuck":
            err.update(type=protocol.STUCK, hint=(
                "A dialog is probably open in Modo: close it, then call again. "
                "The job never started, so nothing changed."))
        elif "busy" in kinds:
            err["type"] = protocol.BUSY
    elif err.get("type") == protocol.STILL_RUNNING and "busy" in kinds:
        err["message"] = err.get("message", "") + "; Modo is busy, not frozen"
    response["error"] = err
    return response


def format_error(response: dict[str, Any]) -> str:
    err = response.get("error") or {}
    out = f"{err.get('type', 'error')}: {err.get('message', '')}"
    if err.get("hint"):
        out += f"\nWhat to do: {err['hint']}"
    if err.get("event_log"):
        out += "\nModo event log:\n" + "\n".join(
            f"  {line}" for line in err["event_log"][-8:])
    if err.get("traceback"):
        out += "\nTraceback:\n" + err["traceback"]
    return out


def format_late(response: dict[str, Any]) -> str:
    """What the watch saw, then earlier jobs that were still running when
    their call gave up. At most half the size budget, so the answer it ends
    still fits."""
    return budget.cut_text(_late_text(response), budget.max_chars() // 2)


def _late_text(response: dict[str, Any]) -> str:
    out = watch_mod.format_seen(response.get("watch") or [],
                                bool(response.get("ok")))
    late = response.get("late") or []
    if not late:
        return out
    parts = []
    for item in late:
        if item.get("ok"):
            parts.append(f"{item.get('id')}: finished: "
                         + json.dumps(item.get("result"), default=str))
        else:
            parts.append(f"{item.get('id')}: failed: " + format_error(item))
    return out + ("\n\nEarlier calls that were still running have ended:\n"
                  + "\n".join(parts))
