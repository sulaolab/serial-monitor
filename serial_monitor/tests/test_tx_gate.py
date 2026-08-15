"""Transmit-gate tests: exclusivity lasts a transfer, never a client session."""

from __future__ import annotations

import threading

import pytest

from ..tx_gate import TxBlocked, TxGate, TxGateBusy


class FakeClock:
    """Monotonic time we control, so expiry is tested without sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_open_by_default() -> None:
    assert TxGate().blocked_by() is None


def test_hold_blocks_then_reopens() -> None:
    gate = TxGate()
    with gate.hold("xmodem"):
        assert gate.blocked_by() == "xmodem"
    # The window closes with the transfer -- nobody keeps a privilege afterwards.
    assert gate.blocked_by() is None


def test_reopens_even_if_the_transfer_raised() -> None:
    gate = TxGate()
    with pytest.raises(RuntimeError):
        with gate.hold("xmodem"):
            raise RuntimeError("transfer blew up")
    assert gate.blocked_by() is None


def test_second_transfer_is_refused_not_queued() -> None:
    gate = TxGate()
    with gate.hold("first"):
        with pytest.raises(TxGateBusy):
            with gate.hold("second"):
                pass
        assert gate.blocked_by() == "first"


def test_hold_expires_so_a_dead_transfer_cannot_wedge_the_uart() -> None:
    clock = FakeClock()
    gate = TxGate(max_hold_s=10.0, clock=clock)
    ctx = gate.hold("zombie")
    ctx.__enter__()
    assert gate.blocked_by() == "zombie"
    clock.t += 10.0
    assert gate.blocked_by() is None
    assert gate.status()["expiries"] == 1


def test_a_stale_release_cannot_reopen_a_newer_window() -> None:
    """The zombie's late __exit__ must not free the gate a live transfer holds."""
    clock = FakeClock()
    gate = TxGate(max_hold_s=10.0, clock=clock)
    zombie = gate.hold("zombie")
    zombie.__enter__()
    clock.t += 10.0
    assert gate.blocked_by() is None          # expired

    live = gate.hold("live")
    live.__enter__()
    try:
        zombie.__exit__(None, None, None)     # the stale release lands late
        assert gate.blocked_by() == "live"    # and is ignored
    finally:
        live.__exit__(None, None, None)
    assert gate.blocked_by() is None


def test_concurrent_transfers_only_one_wins() -> None:
    gate = TxGate()
    started = threading.Event()
    winners: list[str] = []
    busy: list[str] = []

    def run(name: str, wait: bool) -> None:
        try:
            with gate.hold(name):
                winners.append(name)
                started.set()
                if wait:
                    # Hold long enough that the other thread definitely collides.
                    threading.Event().wait(0.2)
        except TxGateBusy:
            busy.append(name)

    first = threading.Thread(target=run, args=("A", True))
    first.start()
    started.wait(1.0)
    second = threading.Thread(target=run, args=("B", False))
    second.start()
    first.join()
    second.join()

    assert winners == ["A"]
    assert busy == ["B"]
    assert gate.blocked_by() is None


# -- two-phase transfer (arm, then stream) ------------------------------------
def test_transfer_leaves_writers_alone_until_the_handshake() -> None:
    """The arm phase must not blind the terminals.

    An operator watching a board being armed has to be able to press reset or arm by
    hand; blocking them there protects nothing, because no frames are in flight
    yet (2026-08-13).
    """
    gate = TxGate()
    with gate.transfer("xmodem (160128 bytes)") as close:
        assert gate.blocked_by() is None                  # arming: gate open
        assert gate.status()["reserved_by"] == "xmodem (160128 bytes)"
        close()                                           # handshake landed
        assert gate.blocked_by() == "xmodem (160128 bytes)"
    assert gate.blocked_by() is None
    assert gate.status()["reserved_by"] is None


def test_transfer_that_never_handshakes_leaves_nothing_behind() -> None:
    gate = TxGate()
    with gate.transfer("xmodem") as _close:
        pass                                              # no 'C' ever arrived
    assert gate.blocked_by() is None
    st = gate.status()
    assert st["reserved_by"] is None and st["holds"] == 0


def test_close_is_idempotent() -> None:
    gate = TxGate()
    with gate.transfer("xmodem") as close:
        close()
        close()
        assert gate.status()["holds"] == 1
    assert gate.blocked_by() is None


def test_a_reservation_still_keeps_a_second_transfer_out() -> None:
    """Open for keystrokes is not open for another transfer."""
    gate = TxGate()
    with gate.transfer("first") as _close:
        with pytest.raises(TxGateBusy):
            with gate.transfer("second"):
                pass
        with pytest.raises(TxGateBusy):
            with gate.hold("second"):
                pass



# -- ordinary writers: checking and writing are one step ----------------------
def test_permit_is_refused_once_the_gate_is_closed() -> None:
    gate = TxGate()
    with gate.transfer("xmodem") as close:
        with gate.write_permit("tcp"):          # arm phase: writers still pass
            pass
        close()
        with pytest.raises(TxBlocked) as excinfo:
            with gate.write_permit("tcp"):
                pass
        assert excinfo.value.blocker == "xmodem"
    with gate.write_permit("tcp"):              # and open again afterwards
        pass


def test_closing_the_gate_waits_for_a_write_already_in_flight() -> None:
    """The race a permit exists to remove.

    Ask the gate a question and then write, and a transfer can close the gate in
    the gap -- that byte then lands between two frames, which is the corruption
    this module was written to prevent.  So a write that has been let through must
    complete before the transfer's frames start: close() drains it.
    """
    gate = TxGate()
    in_write = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def writer() -> None:
        with gate.write_permit("tcp"):
            in_write.set()
            release.wait(3.0)                   # a slow, still-legitimate write

    def transfer() -> None:
        with gate.transfer("xmodem") as close:
            close()
            closed.set()

    w = threading.Thread(target=writer)
    w.start()
    assert in_write.wait(2.0)
    t = threading.Thread(target=transfer)
    t.start()
    # close() must not report a closed gate while our byte is still going out.
    assert not closed.wait(0.25)
    release.set()
    assert closed.wait(3.0)
    w.join(3.0)
    t.join(3.0)
    assert gate.status()["drain_timeouts"] == 0
    assert gate.status()["writers_in_flight"] == 0


def test_a_close_over_a_write_in_flight_is_marked_contended() -> None:
    """Draining is not being clean -- the window has to say so.

    The bytes of a write that started before the close cannot land between two
    frames (close() drains it), but on a handshake the receiver is already waiting
    for block 1, so they can land in FRONT of it.  The gate cannot know whether
    they did; it records the doubt so the sender can refuse to start.
    """
    gate = TxGate()
    in_write = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with gate.write_permit("tcp"):
            in_write.set()
            release.wait(3.0)

    w = threading.Thread(target=writer)
    w.start()
    assert in_write.wait(2.0)
    threading.Timer(0.05, release.set).start()

    with gate.transfer("xmodem") as window:
        window.close(provisional=True)
        assert window.contended is True
        assert gate.status()["contended_closes"] == 1
        # A candidate that was only text means the writer was legitimately
        # permitted and no receiver is listening: the doubt goes away with it.
        window.reopen()
        assert window.contended is False
    w.join(3.0)


def test_an_uncontended_close_is_not_marked() -> None:
    gate = TxGate()
    with gate.transfer("xmodem") as window:
        window.close()
        assert window.contended is False
    assert gate.status()["contended_closes"] == 0


def test_status_counts_writers_in_flight() -> None:
    gate = TxGate()
    with gate.write_permit("tcp"):
        assert gate.status()["writers_in_flight"] == 1
    assert gate.status()["writers_in_flight"] == 0


def test_status_reports_the_holder_and_counters() -> None:
    clock = FakeClock()
    gate = TxGate(max_hold_s=60.0, clock=clock)
    with gate.hold("xmodem (4096 bytes)"):
        clock.t += 1.5
        st = gate.status()
        assert st["held_by"] == "xmodem (4096 bytes)"
        assert st["held_ms"] == 1500
        assert st["holds"] == 1
    st = gate.status()
    assert st["held_by"] is None and st["held_ms"] is None and st["expiries"] == 0
