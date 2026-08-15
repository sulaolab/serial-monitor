"""FastAPI application exposing the UART over a localhost-only REST API.

Endpoints (all JSON):
    GET  /status                   -> connection state + every aux service's state
    POST /command  {cmd}           -> send "<cmd>\\r\\n" to the UART
    GET  /log?tail=100             -> last N log lines
    POST /wait {contains,timeout}  -> passively match raw UART RX bytes
    POST /command_wait {cmd,contains,timeout}
                                   -> send and match, matcher armed first
    POST /xmodem_send {path,...}   -> send a host file to the board over XMODEM

/command followed by /wait is NOT reliable and never was: /wait only matches bytes
observed after it is called, and a console that answers in milliseconds has
already answered by the time the second HTTP request is served.  Use
``/command_wait`` for anything whose reply you provoked; plain ``/wait`` is for
output the board emits on its own (or arm it before you send from another client).

SECURITY: there is NO authentication, by design -- the API is meant to be bound
to loopback only (the ``--http-host`` default is 127.0.0.1). Two endpoints are
worth being explicit about: ``/command`` drives the connected board, and
``/xmodem_send`` reads an ARBITRARY path on the monitor host and streams it out.
Binding to anything but a loopback address therefore publishes an unauthenticated
board-control and host-file-read surface to whoever can reach that address; do
not do it on a shared corporate network. cli.py refuses such a bind outright
unless ``--allow-non-loopback`` says you meant it.

The HTTP API is a writer like any other, so it is subject to the same transmit
gate as the TCP terminals: while an XMODEM transfer runs, ``/command`` returns
409 rather than injecting a line into the middle of it. Being "the AI's channel"
earns it no privilege -- one stray command corrupts a firmware update exactly as
a human keystroke would.

Endpoints are declared as plain ``def`` so FastAPI runs them in its worker
threadpool; that lets POST /wait block on a threading.Event without stalling
the event loop.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .serial_bridge import SerialManager
from .tx_gate import TxBlocked, TxGateBusy
from .xmodem_send import xmodem_send


class CommandIn(BaseModel):
    cmd: str


class WaitIn(BaseModel):
    contains: str = Field(..., min_length=1)
    timeout: float = Field(15.0, gt=0, le=600)


class CommandWaitIn(BaseModel):
    cmd: str
    contains: str = Field(..., min_length=1)
    timeout: float = Field(15.0, gt=0, le=600)


class XmodemSendIn(BaseModel):
    path: str = Field(..., min_length=1)          # image file on the monitor host
    block: int = Field(1024)                       # 1024 (STX) or 128 (SOH)
    handshake_timeout: float = Field(30.0, gt=0, le=600)
    ack_timeout: float = Field(10.0, gt=0, le=120)
    arm_cmd: str | None = Field(None)              # your board's verb; must land IN the receiver
    result_marker: str | None = Field(None)        # substring of the board's result line, e.g. "crc="
    result_timeout: float = Field(30.0, gt=0, le=600)


def create_app(
    mgr: SerialManager,
    aux_services: list | None = None,
    profile: str | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``aux_services`` is an optional list of objects with ``start()``/``stop()``
    (e.g. the TCP live-tail server) started after the UART and stopped before
    it, all tied to the HTTP server lifespan.

    ``profile`` names the board family this monitor was started for and is
    reported by ``/status``.  Several monitors run on this PC at once, separated
    only by loopback alias but all on port 8080, so "which board am I talking to"
    must be answerable in one call -- otherwise the wrong-monitor mistake is
    invisible until something breaks on hardware.
    """
    services = list(aux_services or [])

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mgr.start()          # begin owning the UART on server startup
        for svc in services:
            try:
                svc.start()
            except Exception as exc:  # a failed aux service must not kill HTTP
                # ...but it must not be invisible either. A monitor whose TCP
                # terminal never bound still holds the UART, still answers /status
                # and still looks entirely healthy, so the failure is recorded on
                # the service itself and reported by /status (see full_status).
                print(f"[serial-monitor] auxiliary service {svc!r} failed to start: {exc}")
                try:
                    svc.startup_error = f"{type(exc).__name__}: {exc}"
                except Exception:
                    pass
        try:
            yield
        finally:
            for svc in reversed(services):
                try:
                    svc.stop()
                except Exception:
                    pass
            mgr.stop()       # release the port + close logs on shutdown

    # One version, from the package -- an app version typed here diverges from the
    # one pip installed the moment either is bumped.
    app = FastAPI(title="Serial Monitor", version=__version__, lifespan=lifespan)

    def full_status() -> dict:
        """UART status plus whatever each aux service reports about itself.

        The TCP tail contributes its connected clients, whether it is running at
        all (``running`` / ``startup_error``: the HTTP server survives a terminal
        that failed to bind), and how much input it has discarded; ``tx_gate`` says
        whether a transfer is currently holding every writer off.  Together that
        is the state which explains "the terminal prints but my keys do nothing"
        without anyone having to inspect sockets.
        """
        st = {"profile": profile, **mgr.status()}
        for svc in services:
            key = getattr(svc, "status_key", None)
            fn = getattr(svc, "status", None)
            if not key or not callable(fn):
                continue
            try:
                st[key] = fn()
            except Exception as exc:  # never let a broken aux status hide UART state
                st[key] = {"error": str(exc)}
        return st

    # FastAPI's own docs routes; everything else on this app is ours.
    _DOCS_PATHS = {"/", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    def _endpoint_paths() -> list[str]:
        """The endpoints this app actually serves.

        Read off the router rather than typed out: a hand-written list is one that
        omits the endpoint added last, and it omitted ``/command_wait`` -- the one
        an agent most needs to be told about (review, 2026-08-15).
        """
        paths = {getattr(r, "path", "") for r in app.routes}
        return sorted(paths - _DOCS_PATHS - {""})

    @app.get("/")
    def root():
        return {
            "name": "serial-monitor",
            "version": __version__,
            "role": "AI-oriented UART bridge",
            "endpoints": _endpoint_paths(),
            "status": full_status(),
        }

    @app.get("/status")
    def status():
        return full_status()

    def _gate_409(blocker: str) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail=(
                f"a transfer ({blocker}) holds the transmit gate; the command "
                "was NOT sent. Retry when it finishes -- see /status tx_gate."
            ),
        )

    @app.post("/command")
    def command(body: CommandIn):
        # The gate is checked inside the write, not before it: asking first leaves
        # a window in which a transfer closes the gate and this line still goes
        # out, between two frames (see tx_gate).
        try:
            mgr.send(body.cmd)
        except TxBlocked as blocked:
            raise _gate_409(blocked.blocker)
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return {"ok": True, "sent": body.cmd}

    @app.get("/log")
    def log(tail: int = 100):
        lines = mgr.get_log(tail)
        return {"count": len(lines), "lines": lines}

    @app.post("/wait")
    def wait(body: WaitIn):
        matched = mgr.wait(body.contains, body.timeout)
        if matched is None:
            raise HTTPException(
                status_code=408,
                detail=f"timeout after {body.timeout}s waiting for '{body.contains}'",
            )
        return {"matched": True, "contains": body.contains, "line": matched}

    @app.post("/command_wait")
    def command_wait(body: CommandWaitIn):
        """Send one command and wait for a marker in the reply, atomically.

        The matcher is armed BEFORE the byte goes out, which is the whole reason
        this endpoint exists: `/command` then `/wait` loses every reply that
        arrives before the second request is served, and a dsPIC console answers
        in milliseconds -- far inside one HTTP round trip. The old advice ("send,
        then wait") produced a permanent timeout for exactly the commands that
        work best.

        408 means the command WAS sent and the marker did not appear; 409 means it
        was not sent at all.
        """
        handle = mgr.arm_wait(body.contains)
        try:
            mgr.send(body.cmd)
        except TxBlocked as blocked:
            mgr.wait_armed(handle, 0)        # nothing was sent: release the matcher
            raise _gate_409(blocked.blocker)
        except ConnectionError as exc:
            mgr.wait_armed(handle, 0)
            raise HTTPException(status_code=503, detail=str(exc))

        matched = mgr.wait_armed(handle, body.timeout)
        if matched is None:
            raise HTTPException(
                status_code=408,
                detail=(
                    f"sent {body.cmd!r}, then timed out after {body.timeout}s "
                    f"waiting for '{body.contains}'"
                ),
            )
        return {
            "ok": True,
            "sent": body.cmd,
            "matched": True,
            "contains": body.contains,
            "line": matched,
        }

    @app.post("/xmodem_send")
    def xmodem_send_ep(body: XmodemSendIn):
        """Send a firmware image to the board over XMODEM-CRC.

        ``path`` is read from the monitor host's filesystem with no confinement to
        a root directory -- safe only because the API is loopback-bound and
        unauthenticated (see the module docstring).

        Reads ``path`` from the monitor host, optionally arms the board's receiver
        with ``arm_cmd`` (which must land the board *in* that receiver by itself;
        see ``xmodem_send``), runs the transfer, and — if ``result_marker`` is set —
        blocks for the board's post-transfer result marker in the passively observed
        raw UART RX stream.

        Holds the transmit gate for the duration, so the TCP terminals and
        ``/command`` are held off until it returns.
        """
        if not os.path.isfile(body.path):
            raise HTTPException(status_code=400, detail=f"no such file: {body.path}")
        try:
            data = open(body.path, "rb").read()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"read failed: {exc}")

        # Arm the passive raw-byte waiter BEFORE the transfer, so a result line
        # printed the instant the last block lands is still matched. Registration
        # is synchronous here; the thread this used to spawn might not have
        # registered until after the transfer had already started.
        handle = mgr.arm_wait(body.result_marker) if body.result_marker else None

        try:
            res = xmodem_send(
                mgr,
                data,
                block=body.block,
                handshake_timeout=body.handshake_timeout,
                ack_timeout=body.ack_timeout,
                arm_cmd=body.arm_cmd,
            )
        except TxGateBusy as exc:
            # Two transfers at once would interleave frames and fail both.
            if handle is not None:
                mgr.wait_armed(handle, 0)
            raise HTTPException(status_code=409, detail=str(exc))
        except (ConnectionError, ValueError) as exc:
            if handle is not None:
                mgr.wait_armed(handle, 0)
            raise HTTPException(status_code=503, detail=str(exc))

        if handle is not None:
            # Armed before the transfer, so this either returns immediately (the
            # marker already arrived) or waits out the rest of its window.
            res["result_line"] = mgr.wait_armed(handle, body.result_timeout)

        return res

    return app
