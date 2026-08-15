"""HTTP-surface tests for the one thing a caller cannot fix from outside: order.

`/command` then `/wait` is a losing sequence against real hardware. A dsPIC
console answers in single-digit milliseconds; an HTTP round trip is slower than
that, and `/wait` deliberately never searches backwards (a stale buffer must not
satisfy a fresh wait). So the reply is already gone before the second request is
served, and the caller sees a timeout for exactly the commands that work best.

`/command_wait` arms the matcher before the byte leaves, which is the only place
that ordering can be guaranteed -- inside the monitor. These tests pin both
halves: the atomic endpoint matches, and the old sequence still does not, so
nobody removes the endpoint as redundant.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .. import __version__
from ..api import create_app
from ..traffic_observer import TrafficObserver
from ..tx_gate import TxGate


class MemoryLog:
    current_path = "memory.log"

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str) -> None:
        self.lines.append(line)

    def close(self) -> None:
        pass


class InstantBoard:
    """A manager whose board has already replied by the time ``send`` returns.

    Not a contrived worst case -- it is the normal case for a console command,
    and it is the case the old documented sequence could not handle.
    """

    port = "TEST"
    baud = 230400

    def __init__(self, reply: bytes = b"\r\nOK version=1.2.3\r\n") -> None:
        self.observer = TrafficObserver(log_writer=MemoryLog())
        self.tx_gate = TxGate()
        self.reply = reply
        self.sent: list[str] = []

    # -- lifecycle (driven by the app's lifespan) --
    def start(self) -> None:
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()

    def status(self) -> dict:
        return {
            "connected": True,
            "port": self.port,
            "baud": self.baud,
            "tx_gate": self.tx_gate.status(),
        }

    # -- writers --
    def send(self, cmd: str) -> None:
        with self.tx_gate.write_permit("http-command"):
            self.sent.append(cmd)
            self.observer.publish("tx", (cmd + "\r\n").encode())
            self.observer.publish("rx", self.reply)   # the board answers at once

    # -- observation --
    def get_log(self, tail: int = 100) -> list[str]:
        return self.observer.get_log(tail)

    def wait(self, contains: str, timeout: float) -> str | None:
        return self.observer.wait_ascii(contains, timeout)

    def arm_wait(self, contains: str):
        return self.observer.arm_ascii(contains)

    def wait_armed(self, handle, timeout: float) -> str | None:
        return self.observer.wait_armed(handle, timeout)

    def tx_blocked_by(self) -> str | None:
        return self.tx_gate.blocked_by()


def test_command_wait_catches_a_reply_that_beats_the_http_round_trip():
    mgr = InstantBoard()
    with TestClient(create_app(mgr)) as client:
        res = client.post(
            "/command_wait",
            json={"cmd": "?version", "contains": "OK", "timeout": 3},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["matched"] is True and body["sent"] == "?version"
    assert mgr.sent == ["?version"]


def test_command_then_wait_is_the_trap_command_wait_exists_for():
    """Pins the losing order, so the atomic endpoint is never dropped as sugar."""
    mgr = InstantBoard()
    with TestClient(create_app(mgr)) as client:
        assert client.post("/command", json={"cmd": "?version"}).status_code == 200
        late = client.post("/wait", json={"contains": "OK", "timeout": 0.3})
    assert late.status_code == 408


def test_a_transfer_refuses_both_command_endpoints_and_sends_nothing():
    mgr = InstantBoard()
    with TestClient(create_app(mgr)) as client:
        with mgr.tx_gate.hold("xmodem (test)"):
            plain = client.post("/command", json={"cmd": "?version"})
            atomic = client.post(
                "/command_wait",
                json={"cmd": "?version", "contains": "OK", "timeout": 1},
            )
    assert plain.status_code == 409
    assert atomic.status_code == 409
    assert mgr.sent == []          # 409 means NOT sent, for both


def test_status_reports_the_gate_and_the_profile():
    mgr = InstantBoard()
    with TestClient(create_app(mgr, profile="testboard")) as client:
        st = client.get("/status").json()
    assert st["profile"] == "testboard"
    assert st["tx_gate"]["held_by"] is None


def test_root_lists_every_endpoint_it_actually_serves() -> None:
    """The endpoint list is read off the router, so it cannot forget a new one.

    It was typed out by hand and had already lost ``/command_wait`` -- the one an
    agent most needs to be pointed at (review, 2026-08-15).
    """
    mgr = InstantBoard()
    with TestClient(create_app(mgr)) as client:
        body = client.get("/").json()
    assert body["version"] == __version__
    for path in ("/status", "/command", "/command_wait", "/wait", "/log",
                 "/xmodem_send"):
        assert path in body["endpoints"], path
    # FastAPI's own docs routes are not part of the contract.
    assert "/openapi.json" not in body["endpoints"]
