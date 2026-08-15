"""Protocol-blind raw TCP <-> UART bridge.

The server does not decode, frame, or filter any byte value.  UART RX is fanned
out verbatim to every client, and **every client may type**: open three Tera
Terms on this port and all three can drive the board, exactly as three people
sharing one physical console would.  Nobody is promoted to "the writer".

That is a deliberate reversal of the older single-writer lease, where the first
client to press a key owned TX until it disconnected and every other terminal
went silently dead.  Colliding keystrokes are now the operator's business to
avoid, which is the tradeoff this project chose.

Telnet is not detected and not refused -- **every client is told the same thing
on connect** instead.  A Telnet client rewrites a bare CR as CR NUL and a data
0xFF as FF FF, which corrupts an XMODEM transfer partway through, so Tera Term
must connect with Service = Other.  Deciding *whether* a given client is Telnet
was tried and abandoned: the bridge sends no negotiation of its own, so a client
that only replies to a peer's IAC never identifies itself, and the fallback
signature (CR NUL) occurs naturally inside binary -- on 2026-08-09 it
disconnected a healthy XMODEM transfer 85 blocks in and the corruption was
blamed on the firmware.  An unconditional notice costs one paragraph on connect
and cannot be wrong.

One thing is still refused, and it is not about privilege: **while a transfer
holds the transmit gate** (XMODEM), client input is dropped for the seconds that
transfer lasts -- for every client equally, including the HTTP API.  See
``tx_gate``: it is a window around a transfer, not a lease held by a client.

Byte-transparency therefore has exactly three exceptions, all of them text this
tool writes about itself:

* the connect notice above -- to that client's own connection;
* a notice to the client whose input was discarded.  Without it the symptom is
  indistinguishable from dead firmware -- the terminal keeps printing UART output
  while every keystroke vanishes, which cost real debugging time on 2026-08-06.
  Rate-limited, and prefixed with the tool name so it cannot be mistaken for
  device output.
* monitor notices, fanned out to every client: the XMODEM arm command, the
  handshake wait and its deadline, transfer progress, and why a transfer failed.
  These went to the log file alone until 2026-08-13, so a transfer looked from a
  terminal like a board that had stopped talking for a minute.  The remedy for
  invisible activity is to show it, not to shorten it.

All three are prefixed ``[serial-monitor]`` or drawn as a banner; UART bytes are
never altered, delayed, or filtered in either direction.
"""

from __future__ import annotations

import queue
import socket
import threading
import time

from .serial_bridge import SerialManager
from .tx_gate import TxBlocked


_WARN_INTERVAL_S = 5.0  # per client, so a held-down key produces one notice

# -- the connect notice -------------------------------------------------------
# ASCII-only and ANSI-minimal on purpose: it has to be legible in a Tera Term
# whose encoding and colours are unknown, and it is printed before any UART byte.
_WIDTH = 72  # fits an 80-column terminal with room to spare
_REVERSE = "\x1b[1;33;41m"  # bold yellow on red
_RESET = "\x1b[0m"


def _loud(text: str = "") -> str:
    """One full-width highlighted line.  Padded, so the block has square edges."""
    body = f"##  {text}".ljust(_WIDTH - 2) + "##" if text else "#" * _WIDTH
    return f"{_REVERSE}{body}{_RESET}\r\n"


def connect_notice() -> str:
    """Shown to every client the moment it connects -- no test, no exceptions.

    Says what to set and what breaks if you don't, and states plainly that
    nothing enforces it, so a failed transfer is not mistaken for a refusal by
    this tool or for a fault in the firmware.
    """
    return (
        "\r\n"
        + _loud()
        + _loud("serial-monitor: connect with Service = Other, NOT Telnet")
        + _loud()
        + "  Tera Term: TCP/IP -> Service 'Other'  (or /T=0 on the command line).\r\n"
        "  Telnet rewrites a bare CR as CR NUL and a data 0xFF as FF FF, which\r\n"
        "  corrupts an XMODEM transfer partway through.\r\n"
        "  Nothing here detects or blocks that: the transfer just fails, and the\r\n"
        "  board is not at fault.\r\n"
        + _loud()
    )


def _addr_str(addr) -> str:
    return f"{addr[0]}:{addr[1]}"


class _Client:
    __slots__ = ("conn", "addr", "q", "alive", "warned_at")

    def __init__(self, conn: socket.socket, addr):
        self.conn = conn
        self.addr = addr
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=20000)
        self.alive = True
        self.warned_at = 0.0


class TcpStreamServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        mgr: SerialManager,
        allow_input: bool = False,
        replay: int = 50,
    ):
        self.host = host
        self.port = port
        self.mgr = mgr
        self.allow_input = allow_input
        self.replay = replay  # retained for CLI compatibility; live stream only
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()
        self._ignored_input_events = 0
        self._ignored_input_bytes = 0
        self._running = threading.Event()
        # Set when start() failed (a taken port, a privileged one, a bad host).
        # The HTTP server deliberately survives that -- the UART is the valuable
        # thing and it is already owned -- so the only way anyone learns the
        # terminal is missing is /status saying so.
        self.startup_error: str | None = None

    status_key = "tcp"  # how api.py labels this service's status in /status

    def status(self) -> dict:
        with self._clients_lock:
            return {
                "host": self.host,
                "port": self.port,
                "running": self._running.is_set(),
                "startup_error": self.startup_error,
                "allow_input": self.allow_input,
                "clients": sorted(_addr_str(c.addr) for c in self._clients),
                # Every client may write; there is no owner to report.
                "writers": "all-clients",
                "ignored_input_events": self._ignored_input_events,
                "ignored_input_bytes": self._ignored_input_bytes,
                # Telnet is not detected; every client is told on connect.
                "telnet_policy": "notice-on-connect",
            }

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(5)
            sock.settimeout(0.5)
        except Exception as exc:
            # Recorded here as well as by the caller: whoever starts this service
            # may choose to carry on without it, and then /status is the only
            # place the absence is visible.
            self.startup_error = f"{type(exc).__name__}: {exc}"
            raise
        self.startup_error = None
        self._sock = sock
        self._running.set()
        self.mgr.add_raw_listener(self._on_raw)
        self.mgr.add_notice_listener(self.notice)
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="tcp-accept",
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._running.clear()
        self.mgr.remove_raw_listener(self._on_raw)
        self.mgr.remove_notice_listener(self.notice)
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.alive = False
            try:
                client.conn.close()
            except Exception:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None

    # -- monitor notices -> every TCP client ----------------------------------
    def notice(self, text: str) -> None:
        """Show one monitor-generated line to every client.

        Own line, own prefix, and never mixed into the UART byte stream a client
        is otherwise reading verbatim.  Queued through the same per-client queue
        as UART RX, so it keeps its position relative to device output.
        """
        self._on_raw(f"\r\n[serial-monitor] {text}\r\n".encode())

    # -- UART RX -> every TCP client -----------------------------------------
    def _on_raw(self, data: bytes) -> None:
        payload = bytes(data)
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.q.put_nowait(payload)
            except queue.Full:
                # A slow viewer must never stall UART RX or other clients.
                pass

    # -- TCP clients ----------------------------------------------------------
    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            # Registered by _serve_client, not here: it queues the connect notice
            # first, and a client that is already receiving UART fan-out cannot be
            # guaranteed the notice comes first.
            client = _Client(conn, addr)
            threading.Thread(
                target=self._serve_client,
                args=(client,),
                daemon=True,
            ).start()

    def _serve_client(self, client: _Client) -> None:
        # No negotiation, ever -- this is raw TCP.  The one thing written before
        # UART bytes is the connect notice, unconditionally, to every client.
        #
        # It goes through the client's queue rather than straight to the socket:
        # exactly one thread (_writer) ever calls sendall on a given connection.
        # Writing it here in parallel with the writer thread let UART RX interleave
        # into the middle of the banner (review, 2026-08-15). Queue it first, then
        # join the fan-out, so it cannot even be preceded.
        try:
            client.q.put_nowait(connect_notice().encode())
        except queue.Full:  # pragma: no cover - fresh queue, maxsize 20000
            pass
        with self._clients_lock:
            self._clients.add(client)
        writer = threading.Thread(
            target=self._writer,
            args=(client,),
            daemon=True,
        )
        writer.start()
        try:
            client.conn.settimeout(0.5)
            while self._running.is_set() and client.alive:
                try:
                    data = client.conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break

                # Nothing is inspected: every byte a client sends is either
                # forwarded verbatim or dropped by the two policies below.
                if not self.allow_input:
                    self._warn_input_ignored(client, len(data), gate_owner=None)
                    continue

                # Only a transfer in progress can close the gate, and only for
                # its duration -- no client ever holds it.  Asking first and then
                # writing would leave room for a transfer to close the gate in
                # between and take this byte between two frames, so the permit
                # inside send_guarded does both at once.
                try:
                    self.mgr.send_guarded(
                        data, source=f"tcp:{_addr_str(client.addr)}"
                    )
                except TxBlocked as blocked:
                    self._warn_input_ignored(
                        client, len(data), gate_owner=blocked.blocker
                    )
                except Exception:
                    pass
        finally:
            client.alive = False
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.conn.close()
            except Exception:
                pass

    def _writer(self, client: _Client) -> None:
        while client.alive:
            try:
                chunk = client.q.get(timeout=0.5)
            except queue.Empty:
                if not self._running.is_set():
                    break
                continue
            try:
                client.conn.sendall(chunk)
            except OSError:
                client.alive = False
                break

    def _warn_input_ignored(
        self, client: _Client, nbytes: int, *, gate_owner: str | None
    ) -> None:
        """Tell the client that typed -- and only it -- that nothing was sent.

        Counters advance on every ignored chunk; the notice itself is
        rate-limited per client, so holding a key down does not turn one mistake
        into a screenful.

        Input is dropped, never queued for later: replaying a command the
        operator typed thirty seconds ago, after a transfer finished, would run it
        against a board that has since changed state.
        """
        now = time.monotonic()
        with self._clients_lock:
            self._ignored_input_events += 1
            self._ignored_input_bytes += nbytes
            if (now - client.warned_at) < _WARN_INTERVAL_S:
                return
            client.warned_at = now

        if gate_owner is None:
            reason = "this TCP terminal was started read-only (--no-tcp-allow-input)"
            hint = "Restart the monitor with --tcp-allow-input to type here."
        else:
            reason = f"a transfer ({gate_owner}) holds the transmit gate"
            hint = (
                "Every terminal is blocked for the seconds it lasts, then all of "
                "them can type again -- no client owns the port. Retype when it "
                "finishes."
            )

        notice = f"\r\n[serial-monitor] input ignored: {reason}. {hint}\r\n"
        try:
            client.q.put_nowait(notice.encode())
        except queue.Full:
            pass
        # Also put it in the log, so /log and the file show why input vanished --
        # log only.  note() fans out to every notice listener, i.e. straight back
        # here and on to all clients, which would show the typist the same line
        # twice and tell the others about input that was never theirs.
        try:
            self.mgr.log_note(
                f"TCP input ignored from {_addr_str(client.addr)}: {reason}"
            )
        except Exception:
            pass
