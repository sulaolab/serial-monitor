"""Passive observation of the raw UART traffic.

The transport owns the bytes.  This module only receives immutable copies and
may format, persist, or search them.  Observer failure and back-pressure are
deliberately fail-open: logging may drop events, but UART/TCP forwarding must
never wait for it.

Recording model.  Text is accumulated in memory per direction and written out
when a line ends (LF, or CR from a terminal that sends CR only), when the buffer
grows past `_MAX_TEXT_PARTIAL`, when a non-text chunk interrupts it, or when it
has simply sat still for `_PARTIAL_IDLE_S`.  That last rule is what makes a
single keystroke observable: the transport never waited for Enter -- it is raw
and byte-transparent -- but the *log* used to, so a typed `*` with no newline
left no trace and "the console ignored my key" could not be told apart from "the
byte was never sent".  A stale buffer is written with a `[partial ...]` marker so
a truncated projection is never mistaken for a completed line.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as _dt
import queue
import threading
import time
from typing import Literal

from .log_writer import LogWriter


Direction = Literal["rx", "tx"]
_MAX_TEXT_PARTIAL = 8192
_RAW_HEX_EDGE = 24
# How long a text buffer may sit unchanged before it is written out anyway.
_PARTIAL_IDLE_S = 0.2
# Drain-loop wakeup.  Also the resolution of the idle check above.
_IDLE_TICK_S = 0.1


@dataclasses.dataclass(frozen=True, slots=True)
class TrafficEvent:
    sequence: int
    timestamp: float
    direction: Direction
    data: bytes
    source: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _NoteEvent:
    timestamp: float
    text: str


class _Waiter:
    __slots__ = ("needle", "min_sequence", "event", "matched", "tail")

    def __init__(self, needle: bytes, min_sequence: int):
        self.needle = needle
        self.min_sequence = min_sequence
        self.event = threading.Event()
        self.matched: str | None = None
        self.tail = b""


class TrafficObserver:
    """Asynchronous log projection and raw-byte waiter service."""

    def __init__(
        self,
        *,
        log_writer: LogWriter,
        ring_size: int = 5000,
        queue_size: int = 20000,
    ):
        self._log = log_writer
        self._ring: collections.deque[str] = collections.deque(maxlen=ring_size)
        self._ring_lock = threading.Lock()
        self._queue: "queue.Queue[TrafficEvent | _NoteEvent]" = queue.Queue(
            maxsize=queue_size
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._waiters_lock = threading.Lock()
        # Text accumulators, plus when each last grew and who wrote into it.
        # Owned by the observer thread alone (stop() only touches them after the
        # thread has been joined), so they need no lock.
        self._partials: dict[Direction, bytearray] = {
            "rx": bytearray(),
            "tx": bytearray(),
        }
        self._partial_last: dict[Direction, float] = {"rx": 0.0, "tx": 0.0}
        self._partial_source: dict[Direction, str | None] = {"rx": None, "tx": None}
        self._dropped = 0
        self._errors = 0

    @property
    def current_path(self) -> str | None:
        return self._log.current_path

    @property
    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "dropped": self._dropped,
            "errors": self._errors,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="traffic-observer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        for direction in ("rx", "tx"):
            self._flush_partial(direction, time.time())
        with self._waiters_lock:
            for waiter in self._waiters:
                waiter.event.set()
        try:
            self._log.close()
        except Exception:
            self._errors += 1

    def publish(
        self,
        direction: Direction,
        data: bytes,
        *,
        source: str | None = None,
    ) -> None:
        if not data:
            return
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        event = TrafficEvent(
            sequence=sequence,
            timestamp=time.time(),
            direction=direction,
            data=bytes(data),
            source=source,
        )
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def note(self, text: str) -> None:
        try:
            self._queue.put_nowait(_NoteEvent(time.time(), str(text)))
        except queue.Full:
            self._dropped += 1

    def get_log(self, tail: int = 100) -> list[str]:
        if tail <= 0:
            tail = 1
        with self._ring_lock:
            if tail >= len(self._ring):
                return list(self._ring)
            return list(self._ring)[-tail:]

    def arm_ascii(self, contains: str) -> _Waiter:
        """Register a matcher for RX observed from *now* on, and return it.

        Split out from :meth:`wait_ascii` because arming and waiting have to be
        separable: a board that answers a command in ~10 ms replies long before a
        second HTTP request can be served, and nothing here searches backwards --
        the sequence floor below is what makes a match mean "after this point".
        So a caller that provokes the reply arms first, transmits, and only then
        waits.

        Every armed waiter must be handed to :meth:`wait_armed` (``timeout=0`` is
        a plain release), or it stays on the list until it matches once.
        """
        needle = contains.encode("utf-8")
        with self._sequence_lock:
            min_sequence = self._sequence + 1
        waiter = _Waiter(needle, min_sequence)
        with self._waiters_lock:
            self._waiters.append(waiter)
        return waiter

    def wait_armed(self, waiter: _Waiter, timeout: float) -> str | None:
        """Block on an armed waiter until it matches or ``timeout``, then release it."""
        try:
            waiter.event.wait(timeout=timeout)
            return waiter.matched
        finally:
            with self._waiters_lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass

    def wait_ascii(self, contains: str, timeout: float) -> str | None:
        """Wait for an ASCII/UTF-8 byte substring on UART RX, across chunks.

        Matching is against the observed raw byte stream, not decoded log lines.
        It is passive and cannot change or pause the transport.

        Arms and waits in one call, so it can only see what arrives after it --
        fine for output the board produces on its own, wrong for a reply to
        something you are about to send (:meth:`arm_ascii`).
        """
        return self.wait_armed(self.arm_ascii(contains), timeout)

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            # Reuse this loop's existing wakeup as the periodic flush tick: no
            # extra thread, and no per-byte file write.
            try:
                self._flush_stale_partials()
            except Exception:
                self._errors += 1
            try:
                event = self._queue.get(timeout=_IDLE_TICK_S)
            except queue.Empty:
                continue
            try:
                if isinstance(event, _NoteEvent):
                    self._emit(event.timestamp, "--", event.text)
                else:
                    if event.direction == "rx":
                        self._match_waiters(event)
                    self._format_traffic(event)
            except Exception:
                # Observation is fail-open by design. Never signal transport code.
                self._errors += 1

    def _match_waiters(self, event: TrafficEvent) -> None:
        with self._waiters_lock:
            waiters = list(self._waiters)
        for waiter in waiters:
            if waiter.event.is_set() or event.sequence < waiter.min_sequence:
                continue
            combined = waiter.tail + event.data
            if waiter.needle in combined:
                waiter.matched = (
                    f"{_ts(event.timestamp)} << [raw match] "
                    f"{_render_bytes(combined)}"
                )
                waiter.event.set()
                continue
            keep = max(0, len(waiter.needle) - 1)
            waiter.tail = combined[-keep:] if keep else b""

    def _format_traffic(self, event: TrafficEvent) -> None:
        direction = event.direction
        marker = "<<" if direction == "rx" else ">>"
        data = event.data
        if _is_text_like(data):
            partial = self._partials[direction]
            partial.extend(data)
            self._partial_last[direction] = event.timestamp
            if event.source:
                self._partial_source[direction] = event.source
            while True:
                idx = _find_eol(partial)
                if idx < 0:
                    break
                if partial[idx] == 0x0D and idx == len(partial) - 1:
                    # A trailing CR may still be followed by LF in the next
                    # chunk; don't split one CRLF into two lines.  The idle
                    # flush bounds how long this can wait.
                    break
                end = idx + 1
                if partial[idx] == 0x0D and partial[end] == 0x0A:
                    end += 1
                raw = bytes(partial[:idx])
                del partial[:end]
                self._emit(event.timestamp, marker, self._text_line(direction, raw))
            if not partial:
                self._partial_source[direction] = None
            elif len(partial) > _MAX_TEXT_PARTIAL:
                self._flush_partial(direction, event.timestamp)
            return

        # A non-text chunk ends any pending text projection but never changes the
        # bytes delivered by the transport.
        self._flush_partial(direction, event.timestamp)
        source = f" source={event.source}" if event.source else ""
        self._emit(
            event.timestamp,
            marker,
            f"[raw len={len(data)}{source}] {_render_bytes(data)}",
        )

    def _flush_stale_partials(self) -> None:
        """Write out any text buffer that has stopped growing.

        Applied to both directions: a TX buffer going stale is a keystroke that
        was sent without Enter, and an RX buffer going stale is the board having
        stopped mid-line -- both are exactly the evidence a hang investigation
        needs, and both were previously invisible.
        """
        now = time.time()
        for direction in ("rx", "tx"):
            if not self._partials[direction]:
                continue
            last = self._partial_last[direction]
            if now - last >= _PARTIAL_IDLE_S:
                # Stamped with when the bytes arrived, not when we noticed.
                self._flush_partial(direction, last, reason="partial idle")

    def _flush_partial(
        self,
        direction: Direction,
        timestamp: float,
        *,
        reason: str = "partial",
    ) -> None:
        partial = self._partials[direction]
        if not partial:
            return
        marker = "<<" if direction == "rx" else ">>"
        data = bytes(partial)
        partial.clear()
        text = self._text_line(direction, data, prefix=f"[{reason}] ")
        self._partial_source[direction] = None
        self._emit(timestamp, marker, text)

    def _text_line(self, direction: Direction, raw: bytes, prefix: str = "") -> str:
        """Render a text projection, naming its writer as raw lines already do."""
        source = self._partial_source[direction]
        tag = f"[source={source}] " if source else ""
        return f"{prefix}{tag}{_render_text(raw)}"

    def _emit(self, timestamp: float, marker: str, text: str) -> None:
        formatted = f"{_ts(timestamp)} {marker} {text}"
        with self._ring_lock:
            self._ring.append(formatted)
        try:
            self._log.write_line(formatted)
        except Exception:
            self._errors += 1


def _find_eol(buf: bytearray) -> int:
    """Index of the first LF or CR, or -1.  CR counts: some terminals send only it."""
    lf = buf.find(b"\n")
    cr = buf.find(b"\r")
    if lf < 0:
        return cr
    if cr < 0:
        return lf
    return min(lf, cr)


def _is_text_like(data: bytes) -> bool:
    return all(b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E for b in data)


def _render_text(data: bytes) -> str:
    return data.decode("utf-8", errors="backslashreplace")


def _render_bytes(data: bytes) -> str:
    if len(data) <= (_RAW_HEX_EDGE * 2):
        shown = data
        hex_text = " ".join(f"{b:02X}" for b in shown)
    else:
        head = " ".join(f"{b:02X}" for b in data[:_RAW_HEX_EDGE])
        tail = " ".join(f"{b:02X}" for b in data[-_RAW_HEX_EDGE:])
        hex_text = f"{head} ... {tail}"
    ascii_text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in data[:64])
    if len(data) > 64:
        ascii_text += "..."
    return f"{hex_text} |{ascii_text}|"


def _ts(timestamp: float) -> str:
    now = _dt.datetime.fromtimestamp(timestamp)
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
