"""Tests for the passive raw-traffic observer."""

from __future__ import annotations

import threading
import time

from ..traffic_observer import TrafficObserver


class MemoryLog:
    current_path = "memory.log"

    def __init__(self, fail: bool = False):
        self.lines: list[str] = []
        self.fail = fail
        self.closed = False

    def write_line(self, line: str):
        if self.fail:
            raise OSError("synthetic log failure")
        self.lines.append(line)

    def close(self):
        self.closed = True


def _wait_for(predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true"


def test_wait_matches_raw_bytes_across_chunks():
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    box = {}
    waiter = threading.Thread(
        target=lambda: box.setdefault("match", observer.wait_ascii("qualified=1", 1.0))
    )
    waiter.start()
    time.sleep(0.02)
    observer.publish("rx", b"STREAM quali")
    observer.publish("rx", b"fied=1\r\n")
    waiter.join(timeout=2.0)
    observer.stop()
    assert box["match"] is not None
    assert "raw match" in box["match"]


def test_arming_first_is_what_makes_a_fast_answer_matchable():
    """Arm, transmit, wait -- the order /command_wait uses."""
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    handle = observer.arm_ascii("OK")
    observer.publish("rx", b"OK\r\n")          # the board answers immediately
    matched = observer.wait_armed(handle, 1.0)
    observer.stop()
    assert matched is not None


def test_a_matcher_registered_after_the_reply_never_sees_it():
    """Not a bug to fix here: the sequence floor is deliberate.

    It is also exactly why arm_ascii() and POST /command_wait exist -- send-then-
    wait cannot work against a console that answers faster than an HTTP round
    trip, and a stale buffer must never satisfy a fresh wait.
    """
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("rx", b"OK\r\n")
    missed = observer.wait_ascii("OK", 0.3)
    observer.stop()
    assert missed is None


def test_binary_projection_is_compact_and_safe():
    log = MemoryLog()
    observer = TrafficObserver(log_writer=log)
    observer.start()
    observer.publish("tx", bytes(range(256)) * 8, source="test")
    _wait_for(lambda: bool(observer.get_log()))
    observer.stop()
    line = observer.get_log()[-1]
    assert "[raw len=2048 source=test]" in line
    assert len(line) < 500


def test_text_projection_remains_readable():
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("rx", b"Update ")
    observer.publish("rx", b"committed\r\n")
    _wait_for(lambda: any("Update committed" in line for line in observer.get_log()))
    observer.stop()
    assert any("<< Update committed" in line for line in observer.get_log())


def test_single_keystroke_with_no_newline_is_logged_after_idle():
    """The transport never needed Enter; the log must not need it either."""
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("tx", b"*", source="tcp:127.0.0.1:54185")
    _wait_for(lambda: any("[partial idle]" in line for line in observer.get_log()), 1.0)
    observer.stop()
    line = next(line for line in observer.get_log() if "[partial idle]" in line)
    assert line.endswith("*"), line
    assert "source=tcp:127.0.0.1:54185" in line, line


def test_completed_text_line_names_its_source():
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("tx", b"*tq0000\r\n", source="http-command")
    _wait_for(lambda: any("*tq0000" in line for line in observer.get_log()))
    observer.stop()
    line = next(line for line in observer.get_log() if "*tq0000" in line)
    assert ">> [source=http-command] *tq0000" in line, line
    assert "partial" not in line, line


def test_cr_only_terminal_line_is_complete_not_partial():
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("rx", b"first\rsecond\r")
    _wait_for(lambda: any("first" in line for line in observer.get_log()))
    observer.stop()
    lines = observer.get_log()
    assert any(line.endswith("<< first") for line in lines), lines
    # The trailing CR could still be half of a CRLF, so "second" waits for the
    # idle flush -- but it must not be lost.
    assert any("second" in line for line in lines), lines


def test_crlf_split_across_chunks_stays_one_line():
    observer = TrafficObserver(log_writer=MemoryLog())
    observer.start()
    observer.publish("rx", b"abc\r")
    observer.publish("rx", b"\ndef\n")
    _wait_for(lambda: any("def" in line for line in observer.get_log()))
    observer.stop()
    body = [line.split(" << ", 1)[1] for line in observer.get_log() if " << " in line]
    assert body == ["abc", "def"], body


def test_log_failure_is_fail_open():
    observer = TrafficObserver(log_writer=MemoryLog(fail=True))
    observer.start()
    observer.publish("rx", b"still observed\r\n")
    _wait_for(lambda: bool(observer.get_log()))
    observer.stop()
    assert observer.stats["errors"] >= 1
    assert any("still observed" in line for line in observer.get_log())
