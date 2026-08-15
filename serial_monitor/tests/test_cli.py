"""Startup checks in cli.py that decide whether we ever open the COM port."""

from __future__ import annotations

import socket

import pytest

from ..cli import _bind_is_taken, _is_loopback


def _free_port(host: str, family: int) -> int:
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_a_free_ipv4_loopback_port_is_not_taken() -> None:
    port = _free_port("127.0.0.1", socket.AF_INET)
    assert _bind_is_taken("127.0.0.1", port) is False


def test_a_held_port_is_reported_taken() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert _bind_is_taken("127.0.0.1", s.getsockname()[1]) is True
    finally:
        s.close()


def test_disabled_port_is_never_probed() -> None:
    assert _bind_is_taken("127.0.0.1", 0) is False


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 on this host")
def test_an_ipv6_loopback_bind_is_probed_with_an_ipv6_socket() -> None:
    """``--http-host ::1`` passes the security check, so it must be startable.

    A hard-coded AF_INET probe cannot bind an IPv6 address at all, so every ::1
    start was refused as "already in use" for a port nobody held.
    """
    assert _is_loopback("::1")
    port = _free_port("::1", socket.AF_INET6)
    assert _bind_is_taken("::1", port) is False


def test_an_unresolvable_host_is_left_to_uvicorn() -> None:
    """Not a bind conflict: do not blame a running monitor for a typo'd host."""
    assert _bind_is_taken("no-such-host.invalid", 8080) is False
