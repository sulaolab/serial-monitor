"""Command-line entry point: wire up SerialManager + HTTP API and run.

Example:
    python -m serial_monitor \\
        --port COM33 --baud 2000000 \\
        --log-dir C:\\Temp\\SerialMonitorLogs --http-port 8080
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys

import uvicorn

from . import __version__
from .api import create_app
from .config import ConfigError, available_profiles, resolve
from .log_writer import LogWriter
from .serial_bridge import TERMINATORS, SerialManager
from .tcp_stream import TcpStreamServer


def default_log_dir(profile: str | None = None) -> str:
    """`monitor_logs/<profile>/` inside the package folder, next to the source.

    Namespaced by profile because this tool is installed once and serves several
    boards at the same time.  File names alone would not actually collide
    (`<port>-<date>.log`, and a COM port can only be held by one process), but a
    single flat directory mixing every board's console is a poor place to go
    looking for evidence six commits later.
    """
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    base = os.path.join(pkg_dir, "monitor_logs")
    return os.path.join(base, profile) if profile else base


def _is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, and localhost -- anything else publishes the API."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serial-monitor",
        description="AI-oriented bridge for a firmware UART.",
    )
    # --- UART ---
    # Not argparse-required: --show-config must work without a board attached
    # (the PowerShell launchers ask for the resolved bind before choosing a port).
    p.add_argument("--port", default=None, help="Serial port, e.g. COM33 or /dev/ttyUSB0")
    p.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Baud rate. Default comes from the profile / .serial-monitor.json "
        "(230400 if neither applies).",
    )
    p.add_argument(
        "--parity",
        choices=["none", "even", "odd", "mark", "space"],
        default="none",
    )
    p.add_argument("--data-bits", type=int, choices=[5, 6, 7, 8], default=8)
    p.add_argument("--stop-bits", type=float, choices=[1, 1.5, 2], default=1)
    p.add_argument(
        "--terminator",
        choices=list(TERMINATORS),
        default=None,
        help="Line terminator appended to sent commands (default crlf)",
    )
    p.add_argument(
        "--reconnect-interval",
        type=float,
        default=1.0,
        help="Seconds between UART reconnect attempts (default 1.0)",
    )
    p.add_argument(
        "--dtr",
        choices=["on", "off", "leave"],
        default="on",
        help="DTR line state on open (default on; many CDC consoles need it)",
    )
    p.add_argument(
        "--rts",
        choices=["on", "off", "leave"],
        default="on",
        help="RTS line state on open (default on)",
    )
    # --- logging ---
    p.add_argument(
        "--log-dir",
        default=None,
        help="Directory for UART logs (auto-created). "
        "Default: 'monitor_logs/<profile>/' inside the serial_monitor folder, so "
        "several boards served by one install do not share one flat directory.",
    )
    p.add_argument(
        "--ring-size",
        type=int,
        default=5000,
        help="In-memory line buffer size for GET /log (default 5000)",
    )
    # --- HTTP ---
    p.add_argument(
        "--http-host",
        default=None,
        help="HTTP bind host (default 127.0.0.1, or the profile's). Loopback only: the API is "
        "unauthenticated and can read any file on this host (see api.py).",
    )
    p.add_argument("--http-port", type=int, default=None, help="HTTP port (default 8080)")
    p.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Permit a non-loopback --http-host/--tcp-host. Refused without this: "
        "the API has no authentication, drives the board, and reads any file on "
        "this host, so exposing it has to be deliberate rather than warned about.",
    )
    # --- Protocol-blind raw TCP terminal (Tera Term Service=Other) ---
    p.add_argument(
        "--tcp-port",
        type=int,
        default=None,
        help="Protocol-blind raw TCP terminal port for Tera Term Service=Other "
        "(default 23; nothing is decoded and Telnet is neither detected nor "
        "blocked -- every client is told on connect; 0 disables)",
    )
    p.add_argument("--tcp-host", default=None, help="TCP terminal bind host (default 127.0.0.1, or the profile's)")
    p.add_argument(
        "--tcp-allow-input",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Forward what you type in the TCP terminal (Tera Term) to the UART "
        "byte-for-byte, like a COM terminal. Default ON (read/write), for EVERY "
        "connected client -- avoiding collisions is the operator's business. Use "
        "--no-tcp-allow-input for a view-only terminal.",
    )
    p.add_argument(
        "--tcp-replay",
        type=int,
        default=50,
        help="Reserved (no longer used: the raw terminal streams live only)",
    )
    p.add_argument(
        "--max-tx-hold",
        type=float,
        default=180.0,
        help="Safety valve: seconds after which a transfer's exclusive transmit "
        "window expires even if it never released (default 180). A UART that "
        "accepts input again beats one that is silently mute forever.",
    )
    # --- which board's settings to use -------------------------------------
    p.add_argument(
        "--profile",
        default=None,
        help="Named profile from profiles/ (available: "
        f"{', '.join(available_profiles()) or 'none'}). Sets the loopback alias "
        "and baud for one board family; never the COM port.",
    )
    p.add_argument(
        "--project-config",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Search upward from the current directory for .serial-monitor.json "
        "(default on). --no-project-config ignores it.",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Label reported by GET /status as 'profile' (the board's name). The "
        "PowerShell launcher passes the profile it resolved; without it a child "
        "started with --no-project-config would report 'default' and the "
        "which-board-am-I-on check would silently always say the same thing.",
    )
    p.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved settings (and where each came from) and exit, "
        "without opening the port.",
    )
    p.add_argument("--version", action="version", version=f"serial-monitor {__version__}")
    return p


def _bind_is_taken(host: str, port: int) -> bool:
    """True if something already holds (host, port).

    Checked before the COM port is opened: the alternative is uvicorn failing with
    a bare WinError deep in a traceback after this process already grabbed the
    UART away from whoever had it.

    The family comes from ``getaddrinfo``, not from a hard-coded ``AF_INET``: an
    IPv6 bind such as ``--http-host ::1`` passes the loopback check above, and a
    v4 probe socket cannot bind it at all -- so a fixed family reported "already
    in use" for a port nobody held (review, 2026-08-15).
    """
    if port <= 0:
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        # Unresolvable host: let uvicorn produce the real error, do not invent a
        # bind conflict.
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            probe = socket.socket(family, socktype, proto)
        except OSError:
            continue
        try:
            probe.bind(sockaddr)
        except OSError:
            return True
        finally:
            probe.close()
    return False


def _who_is_on(host: str, port: int) -> str | None:
    """Ask a suspected serial-monitor on (host, port) which board it is on."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=1.5) as fh:
            st = json.load(fh)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return (
        f"profile={st.get('profile')!r} uart={st.get('port')} "
        f"connected={st.get('connected')}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg, sources = resolve(
            profile=args.profile,
            use_project_config=args.project_config,
            overrides={
                "name": args.name,
                "baud": args.baud,
                "terminator": args.terminator,
                "http_host": args.http_host,
                "http_port": args.http_port,
                "tcp_host": args.tcp_host,
                "tcp_port": args.tcp_port,
            },
        )
    except ConfigError as exc:
        print(f"[serial-monitor] {exc}", file=sys.stderr)
        return 2

    if args.show_config:
        print(json.dumps({**cfg, "_sources": sources}, indent=2))
        return 0

    if not args.port:
        print(
            "[serial-monitor] --port is required (e.g. --port COM12). "
            "Use --show-config to inspect the resolved settings without a port.",
            file=sys.stderr,
        )
        return 2

    args.baud = cfg["baud"]
    args.terminator = cfg["terminator"]
    args.http_host = cfg["http_host"]
    args.http_port = cfg["http_port"]
    args.tcp_host = cfg["tcp_host"]
    args.tcp_port = cfg["tcp_port"]
    profile_name = cfg["name"]

    # Two boards are separated only by their loopback alias, so a bind clash is
    # never harmless: either this monitor fails late, or -- far worse -- you go on
    # talking to the OTHER board's monitor believing it is this one.
    for label, host, port in (
        ("HTTP", args.http_host, args.http_port),
        ("TCP", args.tcp_host, args.tcp_port),
    ):
        if not _bind_is_taken(host, port):
            continue
        who = _who_is_on(host, args.http_port) if label == "HTTP" else None
        detail = f" It answers /status as: {who}." if who else ""
        print(
            f"[serial-monitor] {label} bind {host}:{port} is already in use.{detail}"
            "\n  A monitor is probably already running there -- use it instead of "
            "starting a second one."
            "\n  For a different board, give it its own loopback alias: "
            "--profile <name>, or --http-host/--tcp-host.",
            file=sys.stderr,
        )
        return 3

    tri = {"on": True, "off": False, "leave": None}

    log_dir = args.log_dir or default_log_dir(profile_name)
    log_writer = LogWriter(log_dir=log_dir, port=args.port)
    mgr = SerialManager(
        port=args.port,
        baud=args.baud,
        parity=args.parity,
        data_bits=args.data_bits,
        stop_bits=args.stop_bits,
        terminator=args.terminator,
        dtr=tri[args.dtr],
        rts=tri[args.rts],
        log_writer=log_writer,
        ring_size=args.ring_size,
        reconnect_interval=args.reconnect_interval,
        max_tx_hold_s=args.max_tx_hold,
    )

    # Non-loopback is refused, not warned about. Every document here states the
    # loopback contract, and a warning on stderr is not a contract: the launcher
    # scrolls it past, and the process that ignored it is serving an
    # unauthenticated board-control API to the network anyway. Exposing it stays
    # possible -- it just costs one explicit bit of intent (review, 2026-08-15).
    for label, bind in (("--http-host", args.http_host), ("--tcp-host", args.tcp_host)):
        if _is_loopback(bind):
            continue
        if not args.allow_non_loopback:
            print(
                f"[serial-monitor] ERROR: {label}={bind} is not loopback, so it is "
                "refused. This API has no authentication, drives the board, and "
                "can read any file on this host. Pass --allow-non-loopback if you "
                "deliberately accept that on this network.",
                file=sys.stderr,
            )
            return 2
        print(
            f"[serial-monitor] WARNING: {label}={bind} is not loopback and "
            "--allow-non-loopback was given. The API is unauthenticated: anyone "
            "who can reach it drives the board and can read any file on this host.",
            file=sys.stderr,
        )

    aux = []
    if args.tcp_port and args.tcp_port > 0:
        aux.append(
            TcpStreamServer(
                host=args.tcp_host,
                port=args.tcp_port,
                mgr=mgr,
                allow_input=args.tcp_allow_input,
                replay=args.tcp_replay,
            )
        )

    # create_app installs a lifespan handler that start()s / stop()s the
    # SerialManager and any aux services together with the HTTP server.
    app = create_app(mgr, aux_services=aux, profile=profile_name)

    tcp_note = (
        f"  TCP tail: {args.tcp_host}:{args.tcp_port} "
        f"({'raw read/write' if args.tcp_allow_input else 'raw read-only'})"
        if args.tcp_port and args.tcp_port > 0
        else ""
    )
    print(
        f"[serial-monitor {__version__}] profile {profile_name!r} "
        f"(bind from: {sources.get('http_host', 'default')})"
        f"\n  UART {args.port} @ {args.baud} "
        f"-> http://{args.http_host}:{args.http_port}{tcp_note}  "
        f"(logs: {log_writer.log_dir})",
        file=sys.stderr,
    )

    uvicorn.run(app, host=args.http_host, port=args.http_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
