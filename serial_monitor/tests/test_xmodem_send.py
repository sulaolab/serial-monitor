"""Focused tests for the monitor-owned XMODEM sender."""

import threading

import pytest

from ..tx_gate import TxBlocked, TxGate, TxGateBusy
from ..xmodem_send import CAN, _RawQueue, _wait_for_crc_handshake, xmodem_send


def test_handshake_ignores_c_in_reset_text() -> None:
    rq = _RawQueue()
    rq(b"RCON raw: 00740040\r\nXMODEM-CRC now.\r\n")
    timer = threading.Timer(0.01, lambda: rq(b"C"))
    timer.start()
    try:
        assert _wait_for_crc_handshake(rq, 0.5) == (True, False)
    finally:
        timer.cancel()


def test_handshake_reports_receiver_cancel() -> None:
    rq = _RawQueue()
    rq(bytes([0x18]))
    assert _wait_for_crc_handshake(rq, 0.1) == (False, True)


class GateOnlyMgr:
    """Enough manager to reach the gate, and nothing that would start a transfer."""

    def __init__(self):
        self.tx_gate = TxGate()
        self.notes: list[str] = []

    def note(self, text: str):
        self.notes.append(text)


def test_bad_arguments_are_rejected_without_touching_the_gate() -> None:
    """A caller error must not block the terminals for even an instant."""
    mgr = GateOnlyMgr()
    with pytest.raises(ValueError):
        xmodem_send(mgr, b"data", block=999)
    with pytest.raises(ValueError):
        xmodem_send(mgr, b"")
    assert mgr.tx_gate.blocked_by() is None
    assert mgr.tx_gate.status()["holds"] == 0


def test_a_second_transfer_is_refused_while_one_is_running() -> None:
    mgr = GateOnlyMgr()
    with mgr.tx_gate.hold("xmodem (in flight)"):
        with pytest.raises(TxGateBusy):
            xmodem_send(mgr, b"x" * 128)


# -- the handshake window: candidate 'C' -> writers out, before it is confirmed --
def _ordinary_write_refused(gate: TxGate) -> bool:
    """Try to write as a TCP terminal would.  True if the gate refused."""
    try:
        with gate.write_permit("tcp:probe"):
            return False
    except TxBlocked:
        return True


def test_no_keystroke_can_land_during_the_handshake_isolation_interval() -> None:
    """A receiver that has sent 'C' is already waiting for block 1.

    Confirming the handshake takes HANDSHAKE_ISOLATION_S of silence, so closing
    the gate on the *confirmation* leaves the receiver listening for that whole
    interval while ordinary writers are still permitted -- and a keystroke then is
    read as the start of a frame.  The candidate closes the gate; the isolation
    interval only decides whether it stays closed.

    on_candidate runs synchronously inside that interval, which is why the probe
    below is the moment in question rather than a race the test hopes to hit.
    """
    gate = TxGate()
    rq = _RawQueue()
    rq(b"C")  # then silence -> a genuine handshake
    refused: list[bool] = []

    with gate.transfer("xmodem") as window:
        assert not _ordinary_write_refused(gate), "arm phase must stay open"

        def on_candidate() -> None:
            window.close(provisional=True)
            refused.append(_ordinary_write_refused(gate))

        got = _wait_for_crc_handshake(
            rq, 0.5, on_candidate=on_candidate, on_reject=window.reopen
        )
        assert got == (True, False)
        assert refused == [True]
        # Still closed after confirmation, and counted as exactly one hold.
        window.close()
        assert _ordinary_write_refused(gate)
        st = gate.status()
        assert st["holds"] == 1 and st["provisional"] is False

    assert not _ordinary_write_refused(gate), "the window must reopen for everyone"


def test_a_c_inside_text_gives_the_terminals_straight_back() -> None:
    """Rejecting a candidate must not cost the operator the rest of the wait.

    Typing during the arm phase is the point of the two-phase gate -- a reset, or
    arming by hand.  A 'C' in boot text closes the gate for the isolation interval
    and no longer.
    """
    gate = TxGate()
    rq = _RawQueue()
    rq(b"XMODEM-CRC now.\r\n")  # 'C' twice, both followed by more text
    with gate.transfer("xmodem") as window:
        got = _wait_for_crc_handshake(
            rq, 0.2,
            on_candidate=lambda: window.close(provisional=True),
            on_reject=window.reopen,
        )
        assert got == (False, False)
        assert not _ordinary_write_refused(gate)
        st = gate.status()
        assert st["provisional_reverts"] >= 1
        # A reverted candidate is not a transfer: it must not count as a hold.
        assert st["holds"] == 0


class ArmEchoMgr:
    """Fake manager whose 'board' answers the arm command with boot text.

    The text contains a 'C' followed by more text -- a handshake candidate that is
    not one -- which is what makes the counters below evidence about the sender's
    wiring rather than about the gate in isolation.
    """

    def __init__(self, reply: bytes):
        self.tx_gate = TxGate()
        self.reply = reply
        self.notes: list[str] = []
        self.writes: list[bytes] = []
        self._raw: list = []

    def add_raw_listener(self, fn):
        self._raw.append(fn)

    def remove_raw_listener(self, fn):
        if fn in self._raw:
            self._raw.remove(fn)

    def note(self, text: str):
        self.notes.append(text)

    def write_binary(self, data: bytes):
        self.writes.append(bytes(data))
        if self.reply:
            reply, self.reply = self.reply, b""
            for fn in list(self._raw):
                fn(reply)


def test_the_sender_hands_the_gate_a_revertible_window() -> None:
    """End-to-end: a candidate 'C' really does close the gate, and really reverts.

    Only ``_wait_for_crc_handshake``'s candidate/reject callbacks can move
    ``provisional_reverts``, so a non-zero count here proves the sender wires them
    to the window -- and the open gate afterwards proves the operator keeps their
    keyboard.
    """
    mgr = ArmEchoMgr(b"XMODEM-CRC ready?\r\n")
    res = xmodem_send(mgr, b"x" * 128, block=128, handshake_timeout=0.2,
                      arm_cmd="enter-update-mode")
    assert res["ok"] is False and "no 'C' handshake" in res["error"]
    st = mgr.tx_gate.status()
    assert st["provisional_reverts"] >= 1
    assert st["held_by"] is None and st["holds"] == 0


def test_a_write_in_flight_at_the_handshake_abandons_the_transfer() -> None:
    """A genuine 'C' is not enough if somebody was mid-write when it arrived.

    That write started before the receiver began listening, so its tail can be
    read as the first bytes of block 1.  Waiting for it (which the gate does) does
    not undo that, and one guarded TCP write is a whole recv buffer.  So: cancel
    the receiver, send no image, and say to retry -- rather than stream 100 kB at a
    receiver that may already be out of frame.
    """
    mgr = ArmEchoMgr(b"C")  # a real, isolated handshake
    gate = mgr.tx_gate
    in_write = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with gate.write_permit("tcp"):
            in_write.set()
            release.wait(3.0)

    w = threading.Thread(target=writer)
    w.start()
    assert in_write.wait(2.0)
    threading.Timer(0.1, release.set).start()

    res = xmodem_send(mgr, b"x" * 128, block=128, handshake_timeout=1.0,
                      arm_cmd="enter-update-mode")
    w.join(3.0)

    assert res["ok"] is False and "still in flight" in res["error"]
    assert res["blocks"] == 0, "no frame may go out after a contended handshake"
    assert mgr.writes[-1] == bytes([CAN, CAN, CAN]), "the receiver must be cancelled"
    st = gate.status()
    assert st["contended_closes"] == 1
    assert st["held_by"] is None, "the gate reopens for everyone either way"
