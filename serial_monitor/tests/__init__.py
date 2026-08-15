"""Tests for the serial-monitor package.

Run them with the package importable from the repo root:

    python -m pytest serial_monitor/tests -q

Every test here runs without hardware: the UART is replaced by a fake manager,
and the TCP server binds an ephemeral loopback port.
"""
