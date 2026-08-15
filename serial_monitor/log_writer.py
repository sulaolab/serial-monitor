"""Timestamped, date-rotating UART log writer.

Design goals from the spec:
* Log directory is chosen via a CLI option, never a fixed path.
* The directory is auto-created if missing.
* File names are rotation-friendly and can embed the port name, so a future
  multi-COM version drops in without collisions.
* We must never litter the git repo / VS Code workspace root with log files;
  callers are expected to pass an explicit, out-of-tree --log-dir.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading


class LogWriter:
    """Append UART lines to ``<log-dir>/<port>-<YYYYMMDD>.log``.

    A new file is opened automatically when the local date rolls over, so a
    long-running monitor produces one file per day per port.
    """

    def __init__(self, log_dir: str, port: str):
        self.log_dir = os.path.abspath(log_dir)
        # Sanitise the port token so it is always a legal filename component
        # (e.g. POSIX paths like /dev/ttyUSB0 -> dev_ttyUSB0).
        self._port_token = _sanitise(port)
        self._lock = threading.Lock()
        self._fh = None
        self._cur_date: _dt.date | None = None
        self._current_path: str | None = None
        os.makedirs(self.log_dir, exist_ok=True)

    # -- public ---------------------------------------------------------------
    def write_line(self, formatted: str) -> None:
        """Persist one already-formatted log line (no trailing newline needed)."""
        with self._lock:
            now = _dt.datetime.now()
            self._ensure_file(now)
            self._fh.write(formatted + "\n")
            self._fh.flush()

    @property
    def current_path(self) -> str | None:
        return self._current_path

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()
                self._fh = None

    # -- internals ------------------------------------------------------------
    def _ensure_file(self, now: _dt.datetime) -> None:
        d = now.date()
        if self._fh is not None and d == self._cur_date:
            return
        if self._fh is not None:
            self._fh.close()
        self._cur_date = d
        fname = f"{self._port_token}-{now:%Y%m%d}.log"
        self._current_path = os.path.join(self.log_dir, fname)
        # Line-buffered text append; newline="" so we control line endings.
        self._fh = open(self._current_path, "a", encoding="utf-8", newline="")


def _sanitise(token: str) -> str:
    out = []
    for ch in token:
        out.append(ch if (ch.isalnum() or ch in "._-") else "_")
    return "".join(out) or "port"
