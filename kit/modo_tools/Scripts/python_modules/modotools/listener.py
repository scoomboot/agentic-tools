# python
# Parts adapted from modo-mcp 0.3.0, Copyright (c) 2026 Leonard Billeke,
# under the MIT License; see THIRD_PARTY_NOTICES.md.
"""The socket thread: 127.0.0.1 only, one JSON message per line.

It accepts, reads lines, checks the token, queues each job for the main
thread, waits, and answers. It never imports or calls lx: Modo's API is not
thread-safe. hello is answered here, from values read on the main thread when
the kit started, and so is ping, from values the main thread keeps on the pump:
ping has to answer while the main thread is held by a job or a dialog.

Parts follow modo-mcp's netserver.py (see the note at the top).
"""

import hmac
import socket
import sys
import threading
import time

from . import jobs, protocol


def configure_listener(sock, platform=None):
    """On Windows SO_REUSEADDR would let a second socket bind our port and
    read the token the server sends, so Windows gets SO_EXCLUSIVEADDRUSE."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        return "exclusive"
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return "reuse"


class Listener(object):
    def __init__(self, pump, token, hello_info, host=protocol.DEFAULT_HOST,
                 port=protocol.DEFAULT_PORT):
        self.pump = pump
        self.token = token
        self.hello_info = hello_info
        self.host = host
        self.port = port
        self._sock = None
        self._thread = None
        self._running = threading.Event()
        # Jobs that were still running when their caller stopped waiting.
        # Each finished one is handed to the next answer, under "late".
        self._late = []
        self._late_lock = threading.Lock()
        self._conns = set()
        self._conns_lock = threading.Lock()
        self._busy = 0  # connections between reading a request and answering
        self._conn_ids = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("modo-tools only listens on 127.0.0.1")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_listener(sock)
        try:
            sock.bind((self.host, self.port))
            sock.listen(4)
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="modo-tools-accept")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        # Close open connections too, so a stopped kit can't take a request;
        # first give answers already on their way (the pump has failed every
        # queued job by now) a moment to go out.
        end = time.time() + 1.0
        while self._busy and time.time() < end:
            time.sleep(0.01)
        with self._conns_lock:
            conns, self._conns = list(self._conns), set()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def is_running(self):
        return self._running.is_set()

    # -- connections -------------------------------------------------------------

    def _accept_loop(self):
        while self._running.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._client_loop, args=(conn,),
                                      name="modo-tools-client")
            thread.daemon = True
            thread.start()

    def _client_loop(self, conn):
        reader = protocol.LineReader()
        conn.settimeout(1.0)
        authed = False
        with self._conns_lock:
            self._conns.add(conn)
            self._conn_ids += 1
            # Until hello names the server's session, this
            # connection is a session of its own.
            state = {"session": "conn-%d" % self._conn_ids}
        try:
            while self._running.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    lines = reader.feed(chunk)
                except ValueError as exc:
                    self._send(conn, protocol.make_error(
                        None, protocol.BAD_REQUEST, str(exc)))
                    break
                for line in lines:
                    with self._conns_lock:
                        self._busy += 1
                    try:
                        response, authed, close = self.handle_line(line, authed, state)
                        if response is not None:
                            self._send(conn, response)
                    finally:
                        with self._conns_lock:
                            self._busy -= 1
                    if close:
                        return
        finally:
            with self._conns_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def handle_line(self, line, authed, state=None):
        """One request line -> (answer, authed, close the connection). state
        is the connection's: its session, which every job carries."""
        state = state if state is not None else {}
        try:
            request = protocol.decode(line)
            if not isinstance(request, dict):
                raise ValueError("not a JSON object")
        except Exception as exc:
            return protocol.make_error(
                None, protocol.BAD_REQUEST, "malformed request: %s" % exc,
                hint="Send one JSON object per line."), authed, False

        req_id = request.get("id")
        op = request.get("op")

        if op == "hello":
            payload = request.get("payload") or {}
            answer = self._hello(req_id, payload)
            if answer[1] and payload.get("session"):
                state["session"] = str(payload["session"])
            return answer

        if not authed:
            return protocol.make_error(
                req_id, protocol.BAD_TOKEN, "hello with the token comes first",
                hint="The server sends hello with the token from "
                     "~/.modo_tools/token before anything else."), False, True

        if op == "ping":
            return protocol.make_response(req_id, self.pump.ping_info()), True, False

        if not jobs.known(op):
            return self._with_late(protocol.make_error(
                req_id, protocol.UNKNOWN_OP, "no op named %r" % (op,),
                hint="The server and the kit may be different versions; "
                     "update the older one.")), True, False

        # The session tells an open action's own calls from another server's
        #. Set here, never taken from the request.
        request["session"] = state.get("session")
        timeout_ms = request.get("timeout_ms") or protocol.DEFAULT_TIMEOUT_MS
        result, info = self.pump.submit(request, timeout_ms / 1000.0)
        if info is None:
            if result is None:
                result = protocol.make_error(
                    req_id, protocol.STOPPED, "the kit stopped before the job ran",
                    hint="Run modoTools.start in Modo, then call again.")
            elif result.get("id") is None:
                result = dict(result, id=req_id)
            return self._with_late(result), True, False
        if not info["started"]:
            message = "the job didn't start within %d ms" % timeout_ms
            busy = info.get("busy_with")
            if busy:
                message += "; Modo was busy with %s for %s s" % (
                    busy.get("op"), busy.get("running_s"))
            return self._with_late(protocol.make_error(
                req_id, protocol.TIMEOUT, message,
                hint="The job never started and won't, so nothing changed. "
                     "If Modo shows a dialog, close it, then call again.")), True, False
        with self._late_lock:
            self._late.append(info["job"])
        return self._with_late(protocol.make_error(
            req_id, protocol.STILL_RUNNING,
            "the job is still running in Modo after %d ms (%s s so far)"
            % (timeout_ms, info.get("running_s")),
            hint="Don't send it again: it would run twice. The next call "
                 "reports how it ended, under 'late'.")), True, False

    def _hello(self, req_id, payload):
        given = str(payload.get("token") or "")
        # Constant time: the token is all that stands between a local process
        # and running code inside Modo.
        if not hmac.compare_digest(given.encode("utf-8"),
                                   (self.token or "").encode("utf-8")) or not self.token:
            return protocol.make_error(
                req_id, protocol.BAD_TOKEN, "wrong or missing token",
                hint="The server reads the token from ~/.modo_tools/token, "
                     "which the kit writes. Check both use the same user "
                     "profile and MODO_TOOLS_HOME."), False, True
        theirs = payload.get("protocol")
        if theirs != protocol.PROTOCOL_VERSION:
            older = "server" if (isinstance(theirs, int)
                                 and theirs < protocol.PROTOCOL_VERSION) else "kit"
            return protocol.make_error(
                req_id, protocol.PROTOCOL_MISMATCH,
                "the server speaks protocol %r, the kit speaks %d"
                % (theirs, protocol.PROTOCOL_VERSION),
                hint="Update the %s, so both halves are the same version."
                % older), False, True
        result = dict(self.hello_info)
        result["protocol"] = protocol.PROTOCOL_VERSION
        return protocol.make_response(req_id, result), True, False

    def _with_late(self, response):
        """Attach the results of earlier still-running jobs that have ended."""
        with self._late_lock:
            finished = [job for job in self._late if job.done.is_set()]
            self._late = [job for job in self._late if not job.done.is_set()]
        if finished:
            response["late"] = [job.result for job in finished]
        return response

    def _send(self, conn, message):
        try:
            conn.sendall(protocol.encode(message))
        except OSError:
            pass
