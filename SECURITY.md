# Security

## What this tool is, in security terms

`serial-monitor` opens a serial port and exposes it, **unauthenticated**, over
HTTP and raw TCP. That is the feature, not an oversight: it is a bench tool for
driving development hardware from an agent and a terminal at the same time, and
an auth handshake between a script and a COM port on the same machine would buy
nothing.

Two endpoints deserve to be named plainly:

- `POST /command` and the raw TCP terminal **drive the connected board**.
- `POST /xmodem_send` reads an **arbitrary path on the host** and streams it out
  the UART. There is no root directory, no allowlist.

Both are safe only because the sockets are bound to loopback. Every default is
`127.0.0.1`, and `127.0.0.0/8` is entirely local on the platforms this runs on.
A non-loopback `--http-host`/`--tcp-host` is **refused** — the process exits
without opening the COM port — unless `--allow-non-loopback` is also given. It
used to print a warning and start anyway, which is not the same thing as a
contract.

## Supported configuration

| Configuration | Status |
|---|---|
| HTTP and TCP bound to a loopback address (the default) | Supported |
| Bound to a LAN, VPN, container bridge, or `0.0.0.0` (needs `--allow-non-loopback`) | **Out of scope** — you have published an unauthenticated host-file-read and board-control surface |
| Reverse-proxied or port-forwarded to anything non-local | **Out of scope**, same reason |

Running it on a shared or corporate network on a non-loopback bind is not a
vulnerability in this tool; it is a deployment this tool tells you not to make.

## Not vulnerabilities

- **No authentication on the API.** By design; see above.
- **`/xmodem_send` reads any path.** By design, on a loopback-bound API.
- **Telnet is not detected or blocked.** A deliberate reversal: detection fired on
  ordinary binary (`CR NUL` inside an image) and killed a healthy transfer. Every
  client is told on connect instead. See `README.md`.
- **Every TCP client may type.** There is no writer lease; colliding keystrokes
  are the operator's business. Also deliberate — the lease it replaced made a
  second terminal silently dead.

## Reporting a vulnerability

Please **do not open a public issue** for anything exploitable.

Use GitHub's private vulnerability reporting on this repository: **Security →
Report a vulnerability**. That opens a private advisory thread with the
maintainer.

If private reporting is unavailable to you, open an issue containing only a
request for a private channel — no details, no reproducer — and it will be
answered.

What helps: the version or commit, the bind addresses in use, the exact request or
byte sequence, and what you expected instead. Reports about a non-loopback bind
will be closed with a pointer to the table above.
