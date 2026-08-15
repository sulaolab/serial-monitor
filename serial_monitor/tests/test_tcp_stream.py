"""Tests for the protocol-blind raw TCP terminal.

Three properties are pinned here, and they are the ones that were wrong in the
tools this repo replaces:

1. Byte transparency in both directions -- no framing, no filtering, and nothing
   inspected.  Binary containing CR NUL or IAC pairs goes through untouched: the
   guard that used to test for those killed a healthy XMODEM transfer 85 blocks
   in (2026-08-09), and the replacement is a notice, not a test.
2. **Every** connected client may type.  There is no single-writer lease, so a
   second Tera Term is never silently dead.
3. Every client gets the same connect notice -- no detection, no exceptions.

Plus the one refusal that is not about privilege: while a transfer holds the
transmit gate, every client is held off equally and told so.
"""

from __future__ import annotations

import socket
import time

import pytest

from ..tcp_stream import TcpStreamServer, connect_notice
from ..tx_gate import TxGate


class FakeMgr:
    """Just enough SerialManager for the TCP server: listeners, TX, notes, gate."""

    port = "TEST"
    baud = 230400

    def __init__(self):
        self.raw_writes: list[bytes] = []
        self.sources: list[str | None] = []
        self.listeners: list = []
        self.notice_listeners: list = []
        self.notes: list[str] = []
        self.tx_gate = TxGate()

    def send_raw(self, data: bytes, *, source: str | None = None):
        self.raw_writes.append(bytes(data))
        self.sources.append(source)

    def send_guarded(self, data: bytes, *, source: str | None = None):
        """Ordinary-writer path: permit first, exactly like SerialManager."""
        with self.tx_gate.write_permit(source or "writer"):
            self.send_raw(data, source=source)

    def tx_blocked_by(self) -> str | None:
        return self.tx_gate.blocked_by()

    def note(self, text: str):
        self.notes.append(text)
        for fn in list(self.notice_listeners):
            fn(text)

    def log_note(self, text: str):
        """Log/ring only -- no fan-out, exactly like SerialManager.log_note."""
        self.notes.append(text)

    def add_notice_listener(self, fn):
        self.notice_listeners.append(fn)

    def remove_notice_listener(self, fn):
        try:
            self.notice_listeners.remove(fn)
        except ValueError:
            pass

    def add_raw_listener(self, fn):
        self.listeners.append(fn)

    def remove_raw_listener(self, fn):
        try:
            self.listeners.remove(fn)
        except ValueError:
            pass

    def emit_uart(self, data: bytes):
        for fn in list(self.listeners):
            fn(bytes(data))

    def written(self) -> bytes:
        return b"".join(self.raw_writes)


def _start(*, allow_input: bool = True):
    mgr = FakeMgr()
    server = TcpStreamServer(host="127.0.0.1", port=0, mgr=mgr, allow_input=allow_input)
    server.start()
    port = server._sock.getsockname()[1]
    return mgr, server, port


def _connect(port: int, *, keep_notice: bool = False) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    client.settimeout(1.0)
    if not keep_notice:
        _drain_notice(client)
    return client


def _drain_notice(sock: socket.socket) -> bytes:
    """Read the connect notice, so a test can assert on UART bytes alone."""
    expected = len(connect_notice().encode())
    got = bytearray()
    sock.settimeout(1.0)
    while len(got) < expected:
        chunk = sock.recv(expected - len(got))
        if not chunk:
            break
        got.extend(chunk)
    return bytes(got)


def _recv_all(sock: socket.socket, timeout: float = 1.0) -> bytes:
    """Read whatever arrives within ``timeout`` (the peer may or may not close)."""
    sock.settimeout(timeout)
    out = bytearray()
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            out.extend(chunk)
    except (socket.timeout, OSError):
        pass
    return bytes(out)


def _wait_for(predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true"


# -- 1. byte transparency -----------------------------------------------------
def test_connect_notice_is_the_only_thing_written_and_negotiates_nothing():
    """Every client is told to use Service = Other, and nothing else is sent.

    The notice is text, not negotiation: no IAC byte appears in it, so a client
    cannot mistake it for a Telnet option request.
    """
    _mgr, server, port = _start()
    client = _connect(port, keep_notice=True)
    try:
        notice = _drain_notice(client)
        assert b"Service = Other" in notice and b"NOT Telnet" in notice
        assert b"\xff" not in notice
        # ...and then silence until the UART says something.
        client.settimeout(0.15)
        try:
            received = client.recv(1)
        except socket.timeout:
            received = None
        assert received is None
    finally:
        client.close()
        server.stop()


def test_tcp_to_uart_preserves_every_byte():
    """All 256 values, IAC pairs included -- nothing is special-cased."""
    mgr, server, port = _start()
    client = _connect(port)
    payload = bytes(b for b in range(256) if b != 0xFF) + b"\xff\x41\xff\x00"
    try:
        client.sendall(payload)
        _wait_for(lambda: len(mgr.written()) == len(payload))
        assert mgr.written() == payload
    finally:
        client.close()
        server.stop()


def test_uart_to_tcp_preserves_every_byte():
    mgr, server, port = _start()
    client = _connect(port)
    payload = bytes(range(255, -1, -1)) * 4
    try:
        _wait_for(lambda: len(server._clients) == 1)
        mgr.emit_uart(payload)
        received = bytearray()
        while len(received) < len(payload):
            received.extend(client.recv(len(payload) - len(received)))
        assert bytes(received) == payload
    finally:
        client.close()
        server.stop()


def test_xmodem_frame_from_a_client_is_forwarded_verbatim():
    """A client pushing binary must not be reframed, delayed, or judged.

    The 1024-byte pad run is 0xFF, XMODEM's padding byte.
    """
    mgr, server, port = _start()
    client = _connect(port)
    frame = b"\x02\x01\xfe" + b"\xff" * 1024 + b"\x12\x34"
    try:
        _wait_for(lambda: len(server._clients) == 1)
        mgr.emit_uart(b"C")
        assert client.recv(1) == b"C"
        client.sendall(frame)
        _wait_for(lambda: len(mgr.written()) == len(frame))
        assert mgr.written() == frame
    finally:
        client.close()
        server.stop()


# -- 2. every client may type -------------------------------------------------
def test_all_clients_can_write():
    """The property the old single-writer lease broke."""
    mgr, server, port = _start()
    first, second, third = _connect(port), _connect(port), _connect(port)
    try:
        _wait_for(lambda: len(server.status()["clients"]) == 3)
        for sock, line in ((first, b"from-first\n"),
                           (second, b"from-second\n"),
                           (third, b"from-third\n")):
            sock.sendall(line)
            _wait_for(lambda line=line: line in mgr.written())

        # Nobody was refused, so nothing was reported as ignored.
        st = server.status()
        assert st["ignored_input_events"] == 0
        assert st["writers"] == "all-clients"
        # And each write is attributed to the client that sent it.
        assert len({s for s in mgr.sources if s}) == 3
    finally:
        for sock in (first, second, third):
            sock.close()
        server.stop()


def test_a_viewer_that_never_types_sees_only_uart_bytes():
    mgr, server, port = _start()
    typist, viewer = _connect(port), _connect(port)
    try:
        _wait_for(lambda: len(server.status()["clients"]) == 2)
        typist.sendall(b"?status\n")
        _wait_for(lambda: b"?status\n" in mgr.written())
        mgr.emit_uart(b"OK\r\n")
        assert _recv_all(viewer, 0.5) == b"OK\r\n"
    finally:
        typist.close()
        viewer.close()
        server.stop()


# -- 3. nothing is inspected ---------------------------------------------------
def test_telnet_signatures_from_a_client_are_just_bytes():
    """IAC pairs and CR NUL reach the UART verbatim -- there is no detector.

    This is the 2026-08-09 regression stated as a property: a firmware image
    containing ``0D 00`` or ``FF FB`` used to disconnect the client mid-transfer.
    """
    mgr, server, port = _start()
    client = _connect(port)
    payload = bytes([0x0D, 0x00, 0xFF, 0xFB, 0x18, 0xFF, 0xFD, 0x03]) * 64
    try:
        client.sendall(payload)
        _wait_for(lambda: len(mgr.written()) == len(payload))
        assert mgr.written() == payload
        # Still connected, still one client, nothing reported.
        assert len(server.status()["clients"]) == 1
        assert server.status()["telnet_policy"] == "notice-on-connect"
    finally:
        client.close()
        server.stop()


def test_every_client_gets_the_notice_including_a_second_one():
    _mgr, server, port = _start()
    first = _connect(port, keep_notice=True)
    second = _connect(port, keep_notice=True)
    try:
        for sock in (first, second):
            assert b"NOT Telnet" in _drain_notice(sock)
    finally:
        first.close()
        second.close()
        server.stop()

# -- monitor notices reach the terminals, not just the log --------------------
def test_monitor_notices_are_shown_to_every_client():
    """The 2026-08-13 report: the arm line never appeared, so it looked frozen.

    The arm command, the handshake deadline and the failure reason were written
    to the log alone.  They must reach the live terminals, on their own prefixed
    line, without touching the UART byte stream.
    """
    mgr, server, port = _start()
    first, second = _connect(port), _connect(port)
    try:
        _wait_for(lambda: len(server.status()["clients"]) == 2)
        mgr.note("[xmodem] arm: enter-update-mode")
        for sock in (first, second):
            seen = _recv_all(sock, 0.5)
            assert b"[serial-monitor] [xmodem] arm: enter-update-mode" in seen
            assert seen.startswith(b"\r\n") and seen.endswith(b"\r\n")
    finally:
        first.close()
        second.close()
        server.stop()


def test_notices_stop_when_the_server_stops():
    """A stopped server must not keep a listener on the manager."""
    mgr, server, port = _start()
    client = _connect(port)
    try:
        _wait_for(lambda: len(server._clients) == 1)
    finally:
        client.close()
    server.stop()
    mgr.note("after stop")          # must not raise, must reach nobody
    assert mgr.notice_listeners == []


def test_uart_bytes_are_not_touched_by_the_notice_path():
    """A notice is an extra line, never a rewrite of device output."""
    mgr, server, port = _start()
    client = _connect(port)
    try:
        _wait_for(lambda: len(server._clients) == 1)
        mgr.emit_uart(b"\xff\x00\x0d")
        mgr.note("progress")
        mgr.emit_uart(b"\xff\x00\x0d")
        seen = _recv_all(client, 0.5)
        assert seen.startswith(b"\xff\x00\x0d")
        assert seen.endswith(b"\xff\x00\x0d")
        assert b"progress" in seen
    finally:
        client.close()
        server.stop()


# -- the transmit gate --------------------------------------------------------
def test_gate_holds_every_client_off_then_frees_all_of_them():
    mgr, server, port = _start()
    first, second = _connect(port), _connect(port)
    try:
        _wait_for(lambda: len(server.status()["clients"]) == 2)
        with mgr.tx_gate.hold("xmodem (4096 bytes)"):
            first.sendall(b"during\n")
            second.sendall(b"during\n")
            _wait_for(lambda: server.status()["ignored_input_events"] == 2)
            assert mgr.written() == b""
            # Both were told, on their own connection, why nothing happened.
            for sock in (first, second):
                notice = _recv_all(sock, 0.5)
                assert b"input ignored" in notice
                assert b"transmit gate" in notice

        # Gate open again -- for everyone, with nobody holding a leftover claim.
        first.sendall(b"after-first\n")
        second.sendall(b"after-second\n")
        _wait_for(lambda: b"after-first\n" in mgr.written())
        _wait_for(lambda: b"after-second\n" in mgr.written())
    finally:
        first.close()
        second.close()
        server.stop()


def test_gate_blocked_input_is_dropped_not_replayed():
    """A command typed during a transfer must never surface after it ends."""
    mgr, server, port = _start()
    client = _connect(port)
    try:
        _wait_for(lambda: len(server._clients) == 1)
        with mgr.tx_gate.hold("xmodem"):
            client.sendall(b"?status\n")
            _wait_for(lambda: server.status()["ignored_input_events"] == 1)
        time.sleep(0.15)
        assert b"?status" not in mgr.written()
    finally:
        client.close()
        server.stop()


# -- a terminal that never started must not look healthy ----------------------
def test_startup_failure_is_reported_by_status():
    """The HTTP server survives a TCP bind failure; /status is where it shows.

    Otherwise the monitor holds the UART, answers every HTTP call, and looks
    entirely well while the humans' terminal simply does not exist.
    """
    server = TcpStreamServer(
        host="serial-monitor.invalid", port=23, mgr=FakeMgr(), allow_input=True
    )
    with pytest.raises(OSError):
        server.start()
    st = server.status()
    assert st["running"] is False
    assert st["startup_error"]


# -- read-only mode -----------------------------------------------------------
def test_read_only_discards_input_and_says_so():
    mgr, server, port = _start(allow_input=False)
    client = _connect(port)
    try:
        client.sendall(b"must-not-reach-uart")
        _wait_for(lambda: server.status()["ignored_input_events"] == 1)
        assert mgr.written() == b""
        notice = _recv_all(client, 0.5)
        assert b"read-only" in notice
    finally:
        client.close()
        server.stop()


def test_read_only_still_shows_the_connect_notice():
    """The notice does not depend on the input policy."""
    _mgr, server, port = _start(allow_input=False)
    client = _connect(port, keep_notice=True)
    try:
        assert b"NOT Telnet" in _drain_notice(client)
    finally:
        client.close()
        server.stop()


# -- notices go to the right sockets, one writer per socket -------------------
def test_input_ignored_notice_reaches_only_the_client_that_typed():
    """The typist is told; the others are not told about input that was not theirs.

    The notice used to be logged with ``note()``, which fans out to every notice
    listener -- i.e. straight back into this server and on to all clients, so the
    typist saw it twice and a bystander saw it once (review, 2026-08-15).  It is
    logged with ``log_note()`` now: log and ring buffer, no fan-out.
    """
    mgr, server, port = _start(allow_input=False)
    typist = _connect(port)
    bystander = _connect(port)
    try:
        typist.sendall(b"x")
        _wait_for(lambda: server._ignored_input_events >= 1)

        assert b"input ignored" in _recv_all(typist, timeout=0.6)
        # The bystander gets UART bytes and monitor notices, never a report of
        # somebody else's dropped keystroke.
        assert _recv_all(bystander, timeout=0.6) == b""
        # ...and the reason is still on the record for /log and the file.
        assert any("input ignored" in note for note in mgr.notes)
    finally:
        typist.close()
        bystander.close()
        server.stop()


def test_connect_notice_is_never_cut_in_half_by_uart_output():
    """One thread owns each socket, so the banner cannot be interleaved.

    The banner used to be written straight to the socket by the accepting thread
    while the per-client writer thread was already fanning out UART RX to the same
    socket; both called sendall, so device output could land inside the banner.
    """
    mgr, server, port = _start()
    client = _connect(port, keep_notice=True)
    try:
        # Wait only for the registration (which happens *after* the banner is
        # queued), then emit -- i.e. exactly while the banner is being delivered.
        _wait_for(lambda: len(server._clients) == 1)
        for _ in range(20):
            mgr.emit_uart(b"UART-RX-MARKER\r\n")
        stream = _recv_all(client, timeout=1.0)
        notice = connect_notice().encode()
        # Contiguous, and ahead of every UART byte.
        assert notice in stream
        assert stream.index(notice) == 0
        assert stream.count(b"UART-RX-MARKER") == 20
    finally:
        client.close()
        server.stop()
