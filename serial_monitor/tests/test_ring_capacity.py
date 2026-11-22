"""The /log ring must hold a large board-side dump AND the telemetry after it (2026-11-15).

A sonora p26 MEAS capture dump is ~5,730 lines; with the old 5,000-line ring a client that read the telemetry after
the dump found only the dump.  These tests push a dump larger than that through the real observer and the real
HTTP app, the way the UART reader delivers it (small raw chunks, lines split across chunks), and check that nothing
is dropped, truncated or reordered, and that the response stays reasonable in size and time.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ..api import create_app
from ..cli import build_parser
from ..traffic_observer import DEFAULT_RING_SIZE, TrafficObserver
from .test_api import InstantBoard, MemoryLog

DUMP_LINES = 7000          # > the 5,730-line dump that overflowed the old ring
CHUNK = 61                 # odd size so lines straddle chunk boundaries, as UART reads do


def _feed(observer: TrafficObserver, n_dump: int) -> None:
    observer.publish("rx", b"DSPload:A=18.9% B=22.8% bad=0/0/0/0 seq=before\r\n")
    blob = b"".join(b"MEAS dump %06d 0x%08X\r\n" % (i, (i * 2654435761) & 0xFFFFFFFF) for i in range(n_dump))
    for i in range(0, len(blob), CHUNK):
        observer.publish("rx", blob[i:i + CHUNK])
    for k in range(3):
        observer.publish("rx", b"DSPload:A=18.9%% B=22.8%% bad=0/0/0/0 seq=after%d\r\n" % k)


def _drain(observer: TrafficObserver, want: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if observer.stats["queued"] == 0 and len(observer.get_log(DEFAULT_RING_SIZE)) >= want:
            return
        time.sleep(0.02)
    raise AssertionError(f"observer did not drain: {observer.stats}, {len(observer.get_log(DEFAULT_RING_SIZE))} lines")


def test_default_ring_is_16384_everywhere():
    assert DEFAULT_RING_SIZE == 16384
    assert build_parser().parse_args([]).ring_size == DEFAULT_RING_SIZE
    assert TrafficObserver(log_writer=MemoryLog())._ring.maxlen == DEFAULT_RING_SIZE
    assert TrafficObserver(log_writer=MemoryLog()).stats["ring_size"] == DEFAULT_RING_SIZE
    assert TrafficObserver(log_writer=MemoryLog(), ring_size=123).stats["ring_size"] == 123


def test_a_7000_line_dump_and_the_telemetry_after_it_survive_end_to_end():
    mgr = InstantBoard()
    with TestClient(create_app(mgr)) as client:
        _feed(mgr.observer, DUMP_LINES)
        _drain(mgr.observer, DUMP_LINES + 4)
        t0 = time.perf_counter()
        res = client.get("/log", params={"tail": DEFAULT_RING_SIZE})
        dt = time.perf_counter() - t0
    assert res.status_code == 200, res.text
    body = res.json()
    lines = body["lines"]
    assert body["count"] == len(lines) == DUMP_LINES + 4
    assert mgr.observer.stats["dropped"] == 0
    # nothing truncated or reordered: every dump line present once, in order, intact
    dump = [ln for ln in lines if "MEAS dump " in ln]
    assert len(dump) == DUMP_LINES
    for i, ln in enumerate(dump):
        assert ln.endswith("MEAS dump %06d 0x%08X" % (i, (i * 2654435761) & 0xFFFFFFFF)), (i, ln)
    # the telemetry before and after the dump is in the same slice -- what aq's RT window needs
    assert "seq=before" in lines[0]
    assert [ln.rsplit("seq=", 1)[1] for ln in lines[-3:]] == ["after0", "after1", "after2"]
    # a full-ring response is a few MB at most and fast on the host
    assert len(res.content) < 4 * 1024 * 1024, len(res.content)
    assert dt < 2.0, dt


def test_the_ring_still_rolls_over_at_its_size():
    obs = TrafficObserver(log_writer=MemoryLog(), ring_size=100)
    obs.start()
    try:
        for i in range(250):
            obs.publish("rx", b"line %d\r\n" % i)
        deadline = time.time() + 5
        while obs.stats["queued"] and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        got = obs.get_log(1000)
    finally:
        obs.stop()
    assert len(got) == 100 and got[0].endswith("line 150") and got[-1].endswith("line 249")
