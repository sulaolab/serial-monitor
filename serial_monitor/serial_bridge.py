"""Byte-transparent, single-owner UART transport.

SerialManager owns the COM port and copies bytes without decoding or protocol
detection.  Passive observers receive immutable copies for logging and /wait;
their failure or back-pressure never changes the UART/TCP data plane.
"""

from __future__ import annotations

import threading

import serial

from .log_writer import LogWriter
from .traffic_observer import TrafficObserver
from .tx_gate import TxBlocked, TxGate

__all__ = ["SerialManager", "TxBlocked", "PARITY_MAP", "STOPBITS_MAP",
           "BYTESIZE_MAP", "TERMINATORS"]


PARITY_MAP = {
    "none": serial.PARITY_NONE,
    "even": serial.PARITY_EVEN,
    "odd": serial.PARITY_ODD,
    "mark": serial.PARITY_MARK,
    "space": serial.PARITY_SPACE,
}

STOPBITS_MAP = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}

BYTESIZE_MAP = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}

TERMINATORS = {
    "crlf": "\r\n",
    "lf": "\n",
    "cr": "\r",
    "none": "",
}


class SerialManager:
    def __init__(
        self,
        *,
        port: str,
        baud: int,
        parity: str = "none",
        data_bits: int = 8,
        stop_bits: float = 1,
        terminator: str = "crlf",
        dtr: bool | None = True,
        rts: bool | None = True,
        log_writer: LogWriter,
        ring_size: int = 5000,
        reconnect_interval: float = 1.0,
        max_tx_hold_s: float = 180.0,
    ):
        self.port = port
        self.baud = baud
        self.parity = parity
        self.data_bits = data_bits
        self.stop_bits = stop_bits
        self._dtr = dtr
        self._rts = rts
        self._terminator = TERMINATORS[terminator]
        self._reconnect_interval = reconnect_interval
        self._observer = TrafficObserver(
            log_writer=log_writer,
            ring_size=ring_size,
        )

        self._ser: serial.Serial | None = None
        self._ser_lock = threading.Lock()
        self._connected = threading.Event()

        # Exclusivity for a whole multi-write transfer (XMODEM).  _ser_lock only
        # keeps one write from being split; it cannot keep a keystroke out from
        # between two XMODEM frames.  Every writer consults tx_blocked_by() --
        # see tx_gate.py for why this is a transfer window and not a per-client
        # lease.
        self.tx_gate = TxGate(max_hold_s=max_tx_hold_s)

        # Raw RX listeners get exactly the bytes read from UART. They are used by
        # the TCP fanout and optional active clients such as the XMODEM sender.
        # A listener must enqueue only and return immediately.
        self._raw_listeners: list = []
        self._raw_listeners_lock = threading.Lock()

        # Notices the monitor generates about itself (not UART bytes): the XMODEM
        # arm, its handshake wait, transfer progress, the reason a transfer failed.
        # These used to reach the log file only, so a human terminal saw a minute of
        # silence with no hint that a transfer was even being attempted -- read as a
        # frozen board (reported 2026-08-13). The TCP fanout subscribes here.
        # A listener must not block; it is called on the caller's thread.
        self._notice_listeners: list = []
        self._notice_listeners_lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._observer.start()
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="uart-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._ser_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
        self._connected.clear()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._observer.stop()

    # -- public thread-safe API ----------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "baud": self.baud,
            "parity": self.parity,
            "data_bits": self.data_bits,
            "stop_bits": self.stop_bits,
            "log_file": self._observer.current_path,
            "observer": self._observer.stats,
            "tx_gate": self.tx_gate.status(),
        }

    def tx_blocked_by(self) -> str | None:
        """Name of the transfer blocking writes right now, or None if open.

        For reporting only -- "why did my keystroke vanish".  Do NOT use it to
        decide whether to write: a transfer can close the gate between the answer
        and the write.  Ordinary writers use :meth:`send_guarded` (see tx_gate).
        """
        return self.tx_gate.blocked_by()

    def send(self, cmd: str) -> None:
        """Write a line-oriented command as an ordinary, gate-checked writer."""
        payload = (cmd + self._terminator).encode("utf-8", errors="replace")
        self.send_guarded(payload, source="http-command")

    def send_guarded(self, data: bytes, *, source: str | None = None) -> None:
        """Write as an ordinary writer: the gate check and the write are one step.

        Raises ``TxBlocked`` (nothing was written) if a transfer holds the gate,
        or ``ConnectionError`` if the UART is down.  Every writer uses this except
        a transfer writing its own frames -- that one owns the closed gate and
        calls :meth:`send_raw` directly.
        """
        with self.tx_gate.write_permit(source or "writer"):
            self.send_raw(data, source=source)

    def send_raw(self, data: bytes, *, source: str | None = None) -> None:
        """Write bytes verbatim, then publish a passive TX observation copy.

        Asks the transmit gate nothing, deliberately: this is the call a transfer
        holder uses.  Ordinary writers want :meth:`send_guarded`.
        """
        payload = bytes(data)
        with self._ser_lock:
            ser = self._ser
            if ser is None or not self._connected.is_set():
                raise ConnectionError(f"UART {self.port} is not connected")
            ser.write(payload)
            ser.flush()
        self._observer.publish("tx", payload, source=source)

    def write_binary(self, data: bytes) -> None:
        """Compatibility alias for an active raw client such as /xmodem_send."""
        self.send_raw(data, source="xmodem-api")

    def log_note(self, text: str) -> None:
        """Record a notice in the log and ring buffer ONLY -- no fan-out.

        For something a viewer has already been told directly.  ``note()`` reaches
        every live terminal, so using it to log a message already delivered to one
        client shows that client the same thing twice and tells everyone else about
        input that was not theirs (review, 2026-08-15).
        """
        self._observer.note(text)

    def note(self, text: str) -> None:
        """Record a monitor-generated notice, and show it to live viewers."""
        self._observer.note(text)
        with self._notice_listeners_lock:
            listeners = list(self._notice_listeners)
        for fn in listeners:
            try:
                fn(text)
            except Exception:
                # A broken viewer must never break the thing it is reporting on.
                pass

    def add_notice_listener(self, fn) -> None:
        with self._notice_listeners_lock:
            self._notice_listeners.append(fn)

    def remove_notice_listener(self, fn) -> None:
        with self._notice_listeners_lock:
            try:
                self._notice_listeners.remove(fn)
            except ValueError:
                pass

    def add_raw_listener(self, fn) -> None:
        with self._raw_listeners_lock:
            self._raw_listeners.append(fn)

    def remove_raw_listener(self, fn) -> None:
        with self._raw_listeners_lock:
            try:
                self._raw_listeners.remove(fn)
            except ValueError:
                pass

    def get_log(self, tail: int = 100) -> list[str]:
        return self._observer.get_log(tail)

    def wait(self, contains: str, timeout: float) -> str | None:
        """Passively wait for a UTF-8 byte substring on raw UART RX.

        Only matches bytes observed *after* this call.  If your own command is what
        provokes the reply, arm first with :meth:`arm_wait` -- see there.
        """
        return self._observer.wait_ascii(contains, timeout)

    def arm_wait(self, contains: str):
        """Register a matcher now, wait for it later.

        A console that answers in milliseconds beats any send-then-wait sequence,
        and a match is never searched for retroactively, so "send, then start
        waiting" loses fast replies outright.  Arm, transmit, then
        :meth:`wait_armed`.  Returns an opaque handle.
        """
        return self._observer.arm_ascii(contains)

    def wait_armed(self, handle, timeout: float) -> str | None:
        """Block on an armed handle and release it (``timeout=0`` just releases)."""
        return self._observer.wait_armed(handle, timeout)

    # -- reader thread --------------------------------------------------------
    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baud,
                    parity=PARITY_MAP[self.parity],
                    bytesize=BYTESIZE_MAP[self.data_bits],
                    stopbits=STOPBITS_MAP[self.stop_bits],
                    timeout=0.2,
                )
            except Exception as exc:
                self._connected.clear()
                self._note(f"[monitor] open {self.port} failed: {exc}")
                if self._stop.wait(self._reconnect_interval):
                    break
                continue

            try:
                if self._dtr is not None:
                    ser.dtr = self._dtr
                if self._rts is not None:
                    ser.rts = self._rts
            except Exception as exc:
                self._note(f"[monitor] DTR/RTS set failed: {exc}")

            with self._ser_lock:
                self._ser = ser
            self._connected.set()
            self._note(
                f"[monitor] connected {self.port} @ {self.baud} "
                f"(dtr={self._dtr}, rts={self._rts})"
            )

            try:
                while not self._stop.is_set():
                    n = ser.in_waiting
                    data = ser.read(n if n else 1)
                    if not data:
                        continue
                    # Data-plane delivery happens first. Observation is a
                    # best-effort immutable copy and can never gate forwarding.
                    self._fanout_raw(data)
                    self._observer.publish("rx", data, source="uart")
            except Exception as exc:
                if not self._stop.is_set():
                    self._note(f"[monitor] {self.port} read error: {exc}")
            finally:
                self._connected.clear()
                with self._ser_lock:
                    if self._ser is ser:
                        self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                self._note(f"[monitor] {self.port} disconnected; reconnecting...")
                if self._stop.wait(self._reconnect_interval):
                    break

    def _note(self, text: str) -> None:
        # Same path as note(): a port that just disconnected is exactly what a
        # viewer needs told, and it is the monitor's own voice either way.
        self.note(text)

    def _fanout_raw(self, data: bytes) -> None:
        with self._raw_listeners_lock:
            listeners = list(self._raw_listeners)
        for fn in listeners:
            try:
                fn(data)
            except Exception:
                # A broken observer/client must not kill the UART reader.
                pass
