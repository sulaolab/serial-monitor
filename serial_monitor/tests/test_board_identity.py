"""board_identity: the PKOB4 serial next to a UART, and nothing when unsure."""

from __future__ import annotations

from .. import board_identity as bi


class _Port:
    def __init__(self, device, vid, pid, serial_number):
        self.device, self.vid, self.pid, self.serial_number = device, vid, pid, serial_number


def test_debugger_cdc_names_itself(monkeypatch):
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_Port("COM22", 0x04D8, 0x810B, "020085204RYN000318")])
    got = bi.debugger_next_to("com22")
    assert got["serial"] == "020085204RYN000318" and got["via"] == "self"


def test_unknown_port_and_no_port_are_none(monkeypatch):
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    assert bi.debugger_next_to("COM99") is None
    assert bi.debugger_next_to(None) is None


def test_non_usb_port_is_none(monkeypatch):
    monkeypatch.setattr(
        "serial.tools.list_ports.comports", lambda: [_Port("COM230", None, None, None)])
    assert bi.debugger_next_to("COM230") is None


def test_hub_lookup_unavailable_is_none(monkeypatch):
    # The UART bridge is not a debugger; without cfgmgr32 there is no hub to walk.
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_Port("COM17", 0x04D8, 0x00DD, "6&2082AB8B&0&2")])
    monkeypatch.setattr(bi, "_cfgmgr", lambda: None)
    assert bi.debugger_next_to("COM17") is None
