"""XMODEM-CRC sender — drives a firmware image out the monitor's UART.

A board-side receiver of the usual kind is *blocking*: it flushes RX, then drives
the transfer by emitting 'C' (0x43) until the sender starts sending blocks. This
module is the counterpart sender, so a firmware update can be driven end-to-end
over the monitor's HTTP API instead of by a human working a terminal's "send file"
dialog. Which verb (if any) puts a given board into that receiver is a property of
its firmware; this module only writes the verb it is handed.

Only ``SerialManager`` owns the COM port, so the transfer runs *through* it:
  * ``add_raw_listener`` — every raw RX byte (incl. protocol control bytes
    ACK/NAK/'C'/CAN) is fanned out unconditionally, so we queue them here;
  * ``write_binary`` — send block frames + control bytes verbatim with no
    terminator. The monitor's passive observer may log them, but cannot alter
    or delay the raw transport.

Protocol (XMODEM-CRC, a.k.a. XMODEM/CRC + optional 1K blocks):
  SOH=0x01 (128-byte data)  STX=0x02 (1024-byte data)  EOT=0x04
  ACK=0x06  NAK=0x15  CAN=0x18  'C'=0x43 (receiver's CRC-mode poll)
  Frame: <SOH|STX><seq><255-seq><data...><crc_hi><crc_lo>
  seq starts at 1 and wraps 255->0->1... ; CRC-16-CCITT (poly 0x1021, init 0x0000,
  no reflection, no final xor, MSB-first) over the data field only.

Padding: the final short block is padded with 0xFF (NOT the classic 0x1A/CPMEOF).
A receiver programs whatever bytes it is handed straight into flash, so 0xFF — the
erased-flash value — is the natural pad; it keeps the programmed tail identical to
unprogrammed flash, which is what makes a whole-image CRC check on the board
exact. Sizes are therefore rounded up to a block boundary (we downgrade the
last block to 128 bytes when it helps, so padding waste is < 128 bytes).
"""

from __future__ import annotations

import queue
import time

from .serial_bridge import SerialManager

# -- protocol bytes -----------------------------------------------------------
SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRCCHR = 0x43  # 'C'

# A receiver handshake is a standalone control byte.  Boot/status text can
# legitimately contain an ASCII 'C' (for example "RCON" and "XMODEM-CRC").
# Treating the first such byte as the handshake sends block 1 while the target
# is still booting and then mistakes the remaining text for ACK/NAK replies.
HANDSHAKE_ISOLATION_S = 0.05

PAD = 0xFF  # see module docstring: pad with erased-flash value, not 0x1A

# How often to announce progress. A 160 kB image is ~157 blocks at 1 kB, so this
# is a handful of lines -- enough that a watching terminal can see movement,
# few enough that it never competes with the device's own output.
PROGRESS_EVERY_BLOCKS = 32


def crc16_ccitt(data: bytes) -> int:
    """CRC-16-CCITT (XMODEM): poly 0x1021, init 0x0000, MSB-first, no final xor."""
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


class XmodemError(RuntimeError):
    pass


class _RawQueue:
    """Collect raw RX bytes off the reader thread into a byte queue."""

    def __init__(self):
        self.q: "queue.Queue[int]" = queue.Queue()

    def __call__(self, data: bytes) -> None:
        # Runs on the UART reader thread: enqueue only, never block.
        for b in data:
            self.q.put(b)

    def drain(self) -> None:
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def get(self, timeout: float) -> int | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


def _wait_for_crc_handshake(
    rq: _RawQueue,
    timeout: float,
    *,
    on_candidate=None,
    on_reject=None,
) -> tuple[bool, bool]:
    """Return ``(got_c, cancelled)`` for an isolated XMODEM-CRC handshake.

    A genuine receiver ``C`` is followed by silence while it waits for SOH/STX.
    Requiring a short idle interval rejects matching characters embedded in the
    target's reset diagnostics without depending on any product-specific text.

    ``on_candidate`` fires the instant a ``C`` is seen -- *before* the isolation
    interval that decides whether it was real.  If it was, the receiver has been
    listening for that whole interval, so anything permitted to write during it
    lands in front of block 1; the caller uses this to shut writers out first and
    ``on_reject`` to let them back in when the candidate proves to be text.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        b = rq.get(timeout=min(0.5, max(0.0, remaining)))
        if b is None:
            continue
        if b == CAN:
            return False, True
        if b != CRCCHR:
            continue

        if on_candidate is not None:
            on_candidate()
        remaining = deadline - time.monotonic()
        following = rq.get(timeout=min(HANDSHAKE_ISOLATION_S,
                                       max(0.0, remaining)))
        if following is None:
            return True, False
        if on_reject is not None:
            on_reject()
        if following == CAN:
            return False, True
        # The candidate was part of a text/binary stream.  The following byte
        # has been consumed deliberately; continue scanning the queued tail.

    return False, False


def _make_block(seq: int, chunk: bytes, block_size: int) -> bytes:
    """Build one framed XMODEM block for ``chunk`` (padded to ``block_size``)."""
    if len(chunk) < block_size:
        chunk = chunk + bytes([PAD]) * (block_size - len(chunk))
    header = SOH if block_size == 128 else STX
    crc = crc16_ccitt(chunk)
    return bytes([header, seq & 0xFF, (255 - (seq & 0xFF)) & 0xFF]) + chunk + \
        bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _xmodem_send_locked(
    mgr: SerialManager,
    data: bytes,
    *,
    block: int = 1024,
    handshake_timeout: float = 30.0,
    ack_timeout: float = 10.0,
    max_retries: int = 10,
    arm_cmd: str | None = None,
    close_gate=None,
) -> dict:
    """Send ``data`` to the board over XMODEM-CRC through ``mgr``.

    If ``arm_cmd`` is given, it is written first through the same raw transport to
    arm the board's receiver.  The raw listener is already attached, so the ensuing
    'C' handshake cannot be missed.

    ``arm_cmd`` must be a verb that lands the board *in* its receiver by itself,
    because nothing here resets anything: the command is written and then the 'C'
    poll is awaited directly.  A verb that merely prepares an update and returns
    with the application still running does not qualify -- no receiver starts, and
    this call fails with ``no 'C' handshake`` having sent no image.  When arming
    needs a manual step, do it by hand, confirm the receiver's 'C' poll, and call
    this with no ``arm_cmd``.

    Returns ``{ok, blocks, retries, bytes, elapsed, error?}``. Never raises for a
    protocol failure (returns ``ok=False`` + ``error``); raises only on a
    connection error from the underlying manager.
    """
    if block not in (128, 1024):
        raise ValueError("block must be 128 or 1024")
    if not data:
        raise ValueError("empty image")

    rq = _RawQueue()
    mgr.add_raw_listener(rq)
    started = time.monotonic()
    blocks_sent = 0
    retries = 0

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        rq.drain()

        # Every step below is announced through mgr.note(), which reaches the live
        # terminals and not just the log. An arm command followed by a silent wait
        # is indistinguishable from a hung board, and the operator is the one who
        # can fix it -- if they can see what is being waited for.
        if arm_cmd:
            mgr.note(f"[xmodem] arm: {arm_cmd}")
            mgr.write_binary((arm_cmd + "\r\n").encode("ascii", errors="replace"))

        # --- handshake: wait for the receiver's 'C' (CRC mode) -------------
        # The transmit gate is still OPEN here: nothing is in flight to corrupt,
        # and typing may be exactly what is needed (press reset, arm by hand).
        mgr.note(f"[xmodem] waiting up to {handshake_timeout:.0f}s for the"
                 f" receiver's 'C' handshake; terminals can still type until it"
                 f" arrives")

        # A candidate 'C' shuts writers out immediately, before the isolation
        # interval rules on it: by the time it is confirmed the receiver has been
        # waiting for block 1 for that whole interval, and a keystroke arriving
        # then would be read as the start of a frame.  A candidate that turns out
        # to be ordinary text gives the terminals straight back -- the operator
        # may still need to type (reset, arm by hand) for the rest of the wait.
        # A caller that passed a plain callable (no reopen) keeps the old
        # close-on-confirmation behaviour rather than being locked out on text.
        revertible = close_gate is not None and hasattr(close_gate, "reopen")

        def _on_candidate() -> None:
            if revertible:
                close_gate.close(provisional=True)

        def _on_reject() -> None:
            if revertible:
                close_gate.reopen()

        got_c, cancelled = _wait_for_crc_handshake(
            rq, handshake_timeout,
            on_candidate=_on_candidate, on_reject=_on_reject,
        )
        if cancelled:
            return _fail(mgr, "receiver cancelled during handshake",
                         blocks_sent, retries, len(data), elapsed())
        if not got_c:
            mgr.write_binary(bytes([CAN, CAN, CAN]))
            return _fail(mgr, f"no 'C' handshake within {handshake_timeout}s"
                              f" -- nothing was sent, the board is untouched",
                         blocks_sent, retries, len(data), elapsed())

        # Handshake in hand: from here frames go out back to back.  This confirms
        # the close taken on the candidate above (or, for a plain callable, closes
        # the gate now) -- either way it does not reopen in between.
        if close_gate is not None:
            close_gate()

        # Closing waits out an ordinary write that was already in flight, but the
        # receiver started listening when it sent 'C' -- so the tail of a write
        # that began BEFORE that may already have reached it as block 1's first
        # bytes.  Whether it did is unknowable here, so do not stream an image at
        # a receiver that may be mid-garbage: cancel and let the operator retry
        # with nobody typing (review, 2026-08-15).
        if getattr(close_gate, "contended", False):
            mgr.write_binary(bytes([CAN, CAN, CAN]))
            return _fail(mgr, "a terminal write was still in flight when the 'C'"
                              " handshake arrived, so the receiver may have taken"
                              " a stray byte -- no image was sent; retry with no"
                              " one typing",
                         blocks_sent, retries, len(data), elapsed())

        mgr.note(f"[xmodem] handshake OK, sending {len(data)} bytes"
                 f" in {block}-byte blocks; terminal input is blocked until the"
                 f" transfer ends")

        # --- send data blocks ---------------------------------------------
        seq = 1
        off = 0
        total = len(data)
        while off < total:
            remaining = total - off
            bsz = 1024 if (block == 1024 and remaining >= 1024) else 128
            chunk = data[off:off + bsz]
            frame = _make_block(seq, chunk, bsz)

            attempt = 0
            while True:
                mgr.write_binary(frame)
                resp = rq.get(timeout=ack_timeout)
                if resp == ACK:
                    break
                if resp == CAN:
                    return _fail(mgr, f"receiver cancelled at block {seq}",
                                 blocks_sent, retries, total, elapsed())
                # NAK, timeout (None), or noise -> retransmit
                attempt += 1
                retries += 1
                if attempt > max_retries:
                    mgr.write_binary(bytes([CAN, CAN, CAN]))
                    return _fail(mgr, f"block {seq} failed after {max_retries} retries",
                                 blocks_sent, retries, total, elapsed())

            blocks_sent += 1
            off += bsz
            seq = (seq + 1) & 0xFF

            # Enough to show it is moving, rare enough not to flood a terminal.
            if (blocks_sent % PROGRESS_EVERY_BLOCKS) == 0:
                mgr.note(f"[xmodem] {blocks_sent} blocks,"
                         f" {off * 100 // total}% ({off}/{total} bytes),"
                         f" {retries} retries")

        # --- end of transmission ------------------------------------------
        attempt = 0
        while True:
            mgr.write_binary(bytes([EOT]))
            resp = rq.get(timeout=ack_timeout)
            if resp == ACK:
                break
            attempt += 1
            retries += 1
            if attempt > max_retries:
                return _fail(mgr, "no final ACK for EOT",
                             blocks_sent, retries, len(data), elapsed())

        mgr.note(f"[xmodem] done: {blocks_sent} blocks, {retries} retries, "
                 f"{elapsed():.1f}s")
        return {
            "ok": True,
            "blocks": blocks_sent,
            "retries": retries,
            "bytes": len(data),
            "elapsed": round(elapsed(), 3),
        }
    finally:
        mgr.remove_raw_listener(rq)


def xmodem_send(
    mgr: SerialManager,
    data: bytes,
    **kwargs,
) -> dict:
    """Reserve the transmit gate, then close it once the transfer really starts.

    A transfer is hundreds of consecutive writes, and one byte from any other
    writer between two frames corrupts it.  ``_ser_lock`` cannot help -- it only
    keeps a single write intact -- so the gate closes for the frames and reopens
    the moment this returns, for every writer at once (see ``tx_gate``).

    It does NOT close for the arm-and-wait phase before that.  Blocking the
    terminals there protects nothing (no frames are in flight) and takes away the
    one thing that helps when the handshake does not come -- the operator's
    keyboard.  So the arm command goes out with the gate still open, and
    ``_xmodem_send_locked`` closes it at the ``C`` handshake.

    Argument validation happens before the gate is reserved: a caller passing
    ``block=999`` must fail without ever affecting the other terminals.  Raises
    ``TxGateBusy`` if another transfer is already arming or running.
    """
    block = kwargs.get("block", 1024)
    if block not in (128, 1024):
        raise ValueError("block must be 128 or 1024")
    if not data:
        raise ValueError("empty image")

    with mgr.tx_gate.transfer(f"xmodem ({len(data)} bytes)") as close_gate:
        return _xmodem_send_locked(mgr, data, close_gate=close_gate, **kwargs)


def _fail(mgr: SerialManager, msg: str, blocks: int, retries: int,
          nbytes: int, elapsed: float) -> dict:
    mgr.note(f"[xmodem] FAIL: {msg}")
    return {
        "ok": False,
        "error": msg,
        "blocks": blocks,
        "retries": retries,
        "bytes": nbytes,
        "elapsed": round(elapsed, 3),
    }
