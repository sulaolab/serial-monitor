"""Which debugger sits next to this UART -- so /status can name the board.

A Sonora/ak-curio board exposes two USB functions behind one hub: the UART
bridge this monitor opens (VID_04D8&PID_00DD, an MCP2221-class part whose USB
serial is a Windows-made instance string) and the PKOB4 debugger
(VID_04D8&PID_810B, whose USB serial is the PKOB4 serial that ``reset_pkob4
--list`` prints). Two boards of one family share a profile name, so the profile
alone cannot tell them apart; the PKOB4 serial can.

Lookup: the UART's USB device -> its parent hub -> the hub's other children,
keeping the ones whose USB serial looks like a debugger serial. Windows-only
(cfgmgr32); elsewhere, or when nothing is found, the answer is ``None`` and
/status says so instead of guessing.
"""

from __future__ import annotations

import ctypes
import re
import sys

# Debugger USB IDs whose instance segment is the tool's own serial number.
DEBUGGER_IDS = {
    ("04D8", "810B"): "pkob4",
}

_CR_SUCCESS = 0
_ID_RE = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})\\([^\\]+)$", re.I)


def _cfgmgr():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("cfgmgr32")
    except OSError:
        return None


def _device_id(cm, devinst: int) -> str | None:
    buf = ctypes.create_unicode_buffer(512)
    if cm.CM_Get_Device_IDW(devinst, buf, len(buf), 0) != _CR_SUCCESS:
        return None
    return buf.value


def _children(cm, devinst: int):
    child = ctypes.c_uint32()
    if cm.CM_Get_Child(ctypes.byref(child), devinst, 0) != _CR_SUCCESS:
        return
    while True:
        yield child.value
        nxt = ctypes.c_uint32()
        if cm.CM_Get_Sibling(ctypes.byref(nxt), child.value, 0) != _CR_SUCCESS:
            return
        child = nxt


def _uart_usb_instance(port: str) -> str | None:
    """``USB\\VID_xxxx&PID_xxxx\\<instance>`` of the device that owns ``port``."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        if p.device.upper() == port.upper() and p.vid is not None and p.serial_number:
            return f"USB\\VID_{p.vid:04X}&PID_{p.pid:04X}\\{p.serial_number}"
    return None


def debugger_next_to(port: str | None) -> dict | None:
    """``{"tool": "pkob4", "serial": "...", "usb_id": "..."}`` or ``None``.

    If the UART itself is the debugger's CDC (a PKOB4 also exposes one), the
    serial comes straight from it. More than one debugger on the same hub is
    reported as ``None``: that hub does not identify a single board.
    """
    if not port:
        return None
    inst = _uart_usb_instance(port)
    if inst is None:
        return None
    m = _ID_RE.match(inst)
    if m and (m.group(1).upper(), m.group(2).upper()) in DEBUGGER_IDS:
        return {"tool": DEBUGGER_IDS[(m.group(1).upper(), m.group(2).upper())],
                "serial": m.group(3), "usb_id": inst, "via": "self"}
    cm = _cfgmgr()
    if cm is None:
        return None
    node = ctypes.c_uint32()
    if cm.CM_Locate_DevNodeW(ctypes.byref(node), ctypes.c_wchar_p(inst), 0) != _CR_SUCCESS:
        return None
    parent = ctypes.c_uint32()
    if cm.CM_Get_Parent(ctypes.byref(parent), node.value, 0) != _CR_SUCCESS:
        return None
    found = []
    for child in _children(cm, parent.value):
        cid = _device_id(cm, child)
        cm_ = _ID_RE.match(cid or "")
        if not cm_:
            continue
        key = (cm_.group(1).upper(), cm_.group(2).upper())
        if key in DEBUGGER_IDS:
            found.append({"tool": DEBUGGER_IDS[key], "serial": cm_.group(3),
                          "usb_id": cid, "via": "same-hub"})
    return found[0] if len(found) == 1 else None
