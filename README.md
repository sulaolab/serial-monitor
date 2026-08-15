# serial-monitor

One UART bridge for the whole fleet: it owns a COM port and serves it to an AI
agent over HTTP **and** to any number of humans over raw TCP (Tera Term), at the
same time, from one process.

This repo replaces the copies that had accumulated inside individual firmware
projects — 14 of them across 3 divergent lineages, and neither of the two live
ones was a superset of the other.

```
   You (AI agent)  ──HTTP 8080──▶                        ──UART──▶  the board
   Tera Term  x N  ──raw TCP 23─▶   serial-monitor
                                   (sole owner of the COM port)
```

- **AI agents: read [`AI_UART_ACCESS.md`](AI_UART_ACCESS.md) first.** It is the
  canonical rule for this fleet — never open the COM port directly.

## Getting it

Clone `https://github.com/sulaolab/serial-monitor.git` as a **sibling directory
named `serial-monitor/` next to each firmware repo** — not inside one:

```
git clone https://github.com/sulaolab/serial-monitor.git   # run from the dev root
```

Sibling placement is not cosmetic: every documented path is `../serial-monitor/...`
relative to the firmware repo, and profile resolution reads `.serial-monitor.json`
from the **current working directory** (the firmware repo you are standing in), so
the tool must sit one level up from that cwd for both to work.

A consuming repo carries **only its `.serial-monitor.json`** — never a copy of the
tool itself.

## Behaviour worth knowing before you use it

Two decisions here go deliberately against what the older copies did.

### Every client may type

Any number of TCP clients can be connected and **all of them can transmit**,
alongside the HTTP API. There is no single-writer lease.

The older tools handed TX to the first client that pressed a key and kept it
until that connection dropped, which made a second Tera Term silently dead —
printing output, swallowing every keystroke, indistinguishable from broken
firmware. That is gone. The cost is that two people typing at once produce
interleaved bytes on the wire; avoiding that is the operator's business.

### Telnet is not detected — every client is told on connect

The TCP terminal is byte-transparent and negotiates nothing, so Tera Term must
connect with **Service = Other** (`/T=0`). Telnet rewrites a bare CR as `CR NUL`
and a data `FF` as `FF FF`; those bytes are indistinguishable from console input
and corrupt an XMODEM transfer partway through.

So every connection opens with a banner that says exactly that, unconditionally,
and nothing else happens: no test, no disconnect, no muting. `/status` reports
`tcp.telnet_policy: notice-on-connect`.

Deciding *whether* a client is Telnet was tried twice and removed both times, and
the reason is worth keeping:

- This bridge sends no negotiation of its own, so a client that only **replies**
  to a peer's `IAC` never speaks `IAC` here. "Every Telnet client negotiates on
  connect" is not an assumption the code may make, which leaves...
- ...`CR NUL` as the only signature for a silent client — and `0D 00` occurs
  naturally inside binary. On 2026-08-09 that test disconnected a **healthy**
  XMODEM transfer 85 blocks in; the failure looked like corrupt firmware and was
  debugged as such for hours.

A detector good enough to tell those apart is possible (an XMODEM frame carries
its own CRC, so a frame that only verifies after un-escaping is proof the
transport rewrote it) but it buys nothing a sentence cannot: the operator either
sets Service = Other or their transfer fails. A notice cannot be wrong about
which client it is talking to, and the failure is no longer mistaken for the
board's fault. That trade — a paragraph on connect instead of a judgement — is
the whole policy.

### No port is ever recommended

`list-serial-monitor.ps1` labels every COM port with its USB vendor ID
(`VID_03EB` = Curiosity Nano nEDBG, `VID_04D8` = Microchip PKOB4) and stops
there. It marks nothing and `start-serial-monitor.ps1` auto-picks nothing:
interactive runs have no default, and a non-interactive run needs `-Port` (or a
`-Vid` **you** passed that leaves exactly one candidate).

Two guesses were removed to get here. Matching "Microchip" in the manufacturer
string picked the other board's PKOB4 and cost a session on a dead port. Ranking
by VID (03EB before 04D8) was worse in a subtler way: these scripts live in one
shared repo and know nothing about the board you mean, so run from one board's
repo it confidently starred the **other** board's port (measured 2026-08-09). A mark
that happens to be right is indistinguishable from one that is right for a
reason, so there is no mark. A profile's `vid` is not inherited either — a shared
config silently *hiding* candidates is the same misdirection pointing the other
way.

### The one exclusive window: transfers

`POST /xmodem_send` is hundreds of consecutive writes, and one byte from anyone
else between two frames corrupts a firmware update. So a transfer takes the
**transmit gate** ([`tx_gate.py`](serial_monitor/tx_gate.py)): for the seconds it
runs, every other writer is held off — TCP clients get a notice on their own
connection, `POST /command` gets **409** — and when it finishes the gate opens for
everyone at once. It closes on the receiver's first `C` byte rather than on the
short silence that proves it *was* the handshake, because by then the receiver has
been waiting for block 1 for that whole interval; a `C` that turns out to be boot
text reopens it immediately. A write that was *already* in flight when that `C`
arrived is a different matter: closing waits it out, but its tail could still have
reached a receiver that is already listening, so the transfer is **abandoned**
(receiver cancelled, no image sent, retry with nobody typing) rather than started
on a guess — `tx_gate.contended_closes` counts those. Nobody accumulates a
privilege, and `--max-tx-hold` expires a
window whose transfer died so the UART can never be wedged mute.

`GET /status` tells you which of these you are in (`tcp.allow_input`,
`tx_gate.held_by`) instead of leaving you to guess why typing does nothing.

## Which address does it bind to (two boards at once)

Two monitors run on one PC simultaneously and both want 8080, so they are kept
apart by loopback alias: `sonora` on `127.0.0.1`, `ck` on `127.0.0.5`. You should
not have to remember which — the firmware repo declares it once:

```jsonc
// dspic33ck-hal-lab/.serial-monitor.json
{ "profile": "ck" }
```

```powershell
cd dspic33ck-hal-lab
..\serial-monitor\start-serial-monitor.ps1
# Profile 'ck'  ->  bind 127.0.0.5  (from: profile:ck)
```

`profiles/` holds one file per board. A bench you would rather keep out of the
checkout can live in `.serial-monitor-profiles/` next to it, or anywhere in
`$SERIAL_MONITOR_PROFILES`; both win over a same-named file here.

Precedence: explicit flag > `.serial-monitor.json` (found by walking up) >
`-Profile <name>` > defaults. The COM port is deliberately *not* part of it and
stays interactive — port numbers move when a board is re-plugged. `GET /status`
reports the `profile`, so "which board am I on" is one call.

One install serves every board and every session at once: processes are
independent, the OS arbitrates the COM port, bind clashes are refused before the
port is opened, and logs are namespaced per profile
(`monitor_logs/<profile>/`). Stopping never guesses — with more than one monitor
running, `stop-serial-monitor.ps1` lists them and stops nothing until you name one
with `-Profile` / `-HttpHost` / `-Port` (or `-All`).

Full rules: [`docs/binding-and-profiles.md`](docs/binding-and-profiles.md).

## Quick start (Windows / PowerShell)

**Requirements:** Windows with PowerShell (the launchers use CIM to enumerate COM
ports) and **Python 3.10 or newer** — every module uses `str | None` annotations at
runtime. No board is needed to run the tests.

**Which half is Windows-only:** the three `*.ps1` launchers, and only those. They
enumerate COM ports, resolve the bind, and start the process. The `serial_monitor`
package underneath is plain Python — pyserial, sockets, threads — so
`python -m serial_monitor --port /dev/ttyUSB0` is expected to work elsewhere, but
nobody runs it there, so [CI](.github/workflows/tests.yml) tests Windows only and
this repo claims nothing more. Note the raw TCP terminal defaults to port **23**,
which is privileged on Unix; pass `--tcp-port 2300` (or any high port) there.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1   # optional but tidy
pip install -e .                                     # or: pip install -e ".[dev]" to run the tests

.\list-serial-monitor.ps1                  # show COM ports + any running monitor
.\start-serial-monitor.ps1 -Port COM12     # name the port -- nothing is auto-picked
.\start-serial-monitor.ps1                 # no -Port: the list is shown and you are asked
.\stop-serial-monitor.ps1

# equivalent explicit form
python -m serial_monitor --port COM12 --baud 230400 --http-port 8080 --tcp-port 23
```

`pip install -e .` is what makes the module resolvable from **outside** this
folder: a sibling checkout is not on `sys.path`, so `python -m serial_monitor` from
a firmware repo otherwise fails with `No module named serial_monitor` (the
PowerShell launcher works either way — it changes directory here first). Installed,
you also get a `serial-monitor` command that works from anywhere:

```powershell
cd ..\my-firmware-repo
serial-monitor --show-config               # which bind does THIS repo resolve to?
```

Defaults: 230400 baud, HTTP on `127.0.0.1:8080`, raw TCP on `127.0.0.1:23`, logs
in `serial_monitor/monitor_logs/<profile>/` (git-ignored). Both binds are loopback; the API
is unauthenticated, drives the board, and can read any file on the host, so a
non-loopback `--http-host`/`--tcp-host` is **refused**, not warned about. Exposing
it anyway takes `--allow-non-loopback`, which is there to make that a decision
somebody made rather than a warning that scrolled past.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/status` | UART state + TCP clients + `tx_gate` |
| `POST` | `/command` `{cmd}` | send one console line (409 while a transfer holds the gate) |
| `POST` | `/command_wait` `{cmd,contains,timeout}` | send **and** match, with the matcher armed first — use this for a reply you provoked |
| `POST` | `/wait` `{contains,timeout}` | block until a marker appears in RX; matches only what arrives **after** the call |
| `GET` | `/log?tail=N` | recent RX/TX lines |
| `POST` | `/xmodem_send` `{path,arm_cmd,…}` | push a host file to the board's XMODEM receiver |

`/command` then `/wait` is not a working sequence and never was: a console that
answers in milliseconds has replied before a second HTTP request can be served,
and `/wait` never searches backwards. `/command_wait` exists because that ordering
can only be fixed inside the monitor.

## Layout

| Path | What it is |
|---|---|
| [`serial_monitor/serial_bridge.py`](serial_monitor/serial_bridge.py) | owns the COM port; byte-transparent transport + the shared `TxGate` |
| [`serial_monitor/tcp_stream.py`](serial_monitor/tcp_stream.py) | raw TCP terminal: fan-out to all, input from all |
| [`serial_monitor/tx_gate.py`](serial_monitor/tx_gate.py) | transfer-scoped exclusivity (not a per-client lease) |
| [`serial_monitor/api.py`](serial_monitor/api.py) | FastAPI app |
| [`serial_monitor/xmodem_send.py`](serial_monitor/xmodem_send.py) | XMODEM-CRC sender (holds the gate) |
| [`serial_monitor/traffic_observer.py`](serial_monitor/traffic_observer.py) | passive logging / ring buffer / `/wait` matching |
| [`serial_monitor/config.py`](serial_monitor/config.py) | profile / `.serial-monitor.json` resolution (the only copy of these rules) |
| [`profiles/`](profiles/) | one file per board family: bind alias, baud, VID hint |
| [`serial_monitor/tests/`](serial_monitor/tests/) | the whole suite; no hardware needed |

## Tests

```powershell
pip install -e ".[dev]"
python -m pytest -q
```

They pin the properties above: byte transparency both ways, every client can
write, that every client gets the connect notice, and that **nothing is
inspected** — binary containing `CR NUL` or `IAC` pairs reaches the UART verbatim.
Plus that gate-blocked input is *dropped, never replayed later* against a board
whose state has since changed.

## Using it from a firmware project

Point at this repo rather than copying the folder in — the copies are what this
repo exists to end. A project needs one file of its own, and no code:

```jsonc
// <firmware-repo>/.serial-monitor.json
{ "profile": "ck" }        // or "sonora", "nano"
```

Then run `start-serial-monitor.ps1` from inside that repo and reach the API on the
bind it prints. Don't hardcode `127.0.0.1:8080` in the project's docs — with two
boards live, a base URL that is right today is how you end up driving the other
board tomorrow.

## Security

The API is **unauthenticated by design** and `/xmodem_send` reads any path on the
host, which is safe only on the loopback binds it defaults to. Supported
configurations, what is deliberately not a vulnerability, and how to report one
privately: [`SECURITY.md`](SECURITY.md).

## License

[MIT-0](LICENSE) (MIT No Attribution): use it, copy it into your own tooling,
change it, ship it — no attribution required.
