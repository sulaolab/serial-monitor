"""serial-monitor — an HTTP/TCP bridge that owns one firmware UART.

This is NOT a human serial terminal. It owns the COM port exclusively and
exposes the UART two ways at once:

* a localhost HTTP API, so an AI agent (Codex / Claude / ChatGPT) can send
  commands, read logs, wait for markers, and push firmware over XMODEM without
  ever touching the serial port directly;
* a protocol-blind raw TCP terminal, so any number of humans can watch and type
  in Tera Term at the same time.

Two rules define its behaviour, and both were chosen against the grain of the
tools this package replaces:

* **Every client may type.** No single-writer lease, no "first terminal wins".
  Colliding keystrokes are the operator's business to avoid.
* **Telnet is not detected and not blocked** -- every client is told on connect to
  use Service = Other instead.  Detection was tried and removed: its only usable
  signature (``CR NUL``) occurs naturally inside binary, and it killed a healthy
  XMODEM transfer 85 blocks in, after which the corruption was blamed on the
  firmware.  A notice cannot be wrong about which client it is talking to.

The only exclusivity is a *transfer* window (see ``tx_gate``): while XMODEM runs,
every writer including the HTTP API is held off for those seconds, then all of
them are released together.
"""

__version__ = "1.0.0"
