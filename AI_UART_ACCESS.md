# Talking to the board — for AI agents (READ THIS FIRST)

> **This file is the canonical source of truth**, and it lives in the
> `serial-monitor` repo. Thin pointer copies live at the workspace root
> (`CLAUDE.md`, `AGENTS.md`) and in each consuming project root. Those only link
> here — edit the rule here, not in the pointers. See "Where the copies live" at
> the bottom.
>
> Commands below use `serial-monitor`'s own names and defaults. Every *board-side*
> console verb here (`?status`, `enter-update-mode`, `READY`, …) is a
> **placeholder**: this tool knows nothing about any board's command set, and
> deliberately documents none. Substitute your own board's verbs.


## Where to get the tool (if it isn't on disk)

Clone `https://github.com/sulaolab/serial-monitor.git` as a **sibling directory
named `serial-monitor/` next to the firmware repo** (`git clone …` from the dev
root). A consuming repo holds **only its `.serial-monitor.json`**, never a copy of
the tool — so "config present, tool missing" means clone it, not hunt for a copy.

Sibling placement is what the rest of this file assumes: paths are written
`../serial-monitor/...` relative to the firmware repo, and profile resolution reads
`.serial-monitor.json` from the **cwd** (the firmware repo you are standing in), so
the tool must be exactly one level up from it.


**Do NOT open the serial / COM port directly.** The board's UART console
is owned **exclusively** by the `serial-monitor` bridge. A COM port can only be
held by one process at a time, so if you (or a script, or `pyserial`, or a
terminal) try to open it while the monitor is running, you will either fail with
"access denied / port busy" or you will steal the port and break the monitor and
any human watching in Tera Term.

Instead, **talk to the board over the monitor's HTTP API on `localhost`.** This
is the only supported way for an agent to send commands and read output.

```
   You (AI agent)  ──HTTP──▶  serial-monitor  ──UART──▶  the board
   Humans          ──TCP───▶       (owns the COM port; every client may type)
```

## The one rule

> Never `open()` a COM port, never run a serial terminal, never `pyserial` the
> board directly. Talk to the board only through the monitor's HTTP API, on the
> bind **you resolved for the board you mean** — see "Base URL — NOT a constant"
> below. Examples in this file use `127.0.0.1:8080` as a stand-in for it.
>
> **Starting the monitor is not "grabbing the port" — it is the sanctioned way
> to access the board, and you may do it yourself.** The only forbidden thing is
> *bypassing* the monitor with a direct COM open. If the monitor is down and your
> task needs the board, start it yourself (see "Starting it yourself"), don't
> make the user type the command.

## Self-service lifecycle (when you debug via the monitor)

If you are going to use the board through the monitor:

1. **Check first** with `GET /status`.
2. **If it is already running, just use it as-is** — do NOT start a second one
   (the port/HTTP bind will fail), and do NOT restart it (you would interrupt
   whoever is already using or watching it).
3. **If it is not running, start it yourself.** Announce it first ("starting
   serial-monitor…") so the user knows a process is now holding the COM port,
   then start it (see "Starting it yourself").
4. **Work** over the HTTP API.
5. **Shutdown depends on who started it:**
   - If **you started it**, stop it yourself when you're done, and say so (see
     "Stopping it"). Exception: leave it running if the user is watching in Tera
     Term or asked you to.
   - If it was **already running when you arrived** (you didn't start it), **do
     NOT stop it** — leave it exactly as you found it.

## What this bridge refuses (and what it does not)

Two refusals exist. Neither is about privilege, and knowing which one you hit
saves you from debugging the firmware for nothing.

1. **Telnet is not detected, and not refused — every client is told on connect.**
   The raw TCP terminal is byte-transparent and negotiates nothing. A Telnet
   client rewrites a bare CR as `CR NUL` and a data `FF` as `FF FF`, which
   corrupts an XMODEM transfer partway through, so Tera Term must connect with
   **Service = Other** (`/T=0`). Every connection now opens with a banner saying
   exactly that; `/status` reports `tcp.telnet_policy: notice-on-connect`.

   Nothing enforces it, on purpose. Detection was tried and removed: this bridge
   sends no negotiation of its own, so a client that only replies to a peer's
   `IAC` never identifies itself, and the fallback signature (`CR NUL`) occurs
   naturally inside binary — on 2026-08-09 it disconnected a **healthy** XMODEM
   transfer 85 blocks in, and the corruption was blamed on the firmware for
   hours. If a transfer over Telnet fails now, it simply fails: the tool did not
   stop it and the board is not at fault.

   The connect banner is one of exactly three things this tool ever writes to a
   TCP client that did not come from the UART: the banner, the input-ignored
   notice to the client that typed, and monitor notices during a transfer (arm
   command, handshake wait, progress, failure) which go to every client. All
   three are a whole line and carry `[serial-monitor]` or the banner's frame. If
   you parse TCP 23 programmatically, skip those lines; the HTTP API is
   unaffected.

2. **While a transfer holds the transmit gate, every writer is held off** — all
   TCP terminals *and* this HTTP API. `POST /command` then returns **409** and the
   command is NOT sent; retry when the transfer finishes. `/status` shows
   `tx_gate.held_by` and `held_ms`. The window lasts the seconds of the transfer,
   expires after `--max-tx-hold` seconds if a transfer dies without releasing, and
   opens again for everybody at once.

What is **not** refused: concurrent typing. Any number of Tera Terms can be
connected and **all of them can type**, together with this API. There is no
single-writer lease and no "first terminal wins" — so if two of you send a
command at the same time, the board sees both interleaved. Avoiding that is the
operator's business, not the bridge's.

If your keystrokes vanish, `GET /status` distinguishes the three possible causes
without guessing: `tcp.allow_input` false (read-only start), `tx_gate.held_by`
non-null (a transfer is running), or `connected` false (no UART).

## Base URL — NOT a constant. Resolve it, do not assume it.

Several monitors run on this PC at once, one per board, separated by loopback
alias but all on port 8080. `127.0.0.1:8080` will answer even when it is the
**wrong board**, so assuming it is how you end up driving someone else's hardware
while every reply looks healthy.

Resolve it for the repo you are working in:

```bash
# from inside the firmware repo (it declares its board in .serial-monitor.json)
serial-monitor --show-config     # prints http_host/http_port + where they came from
```

That console script exists only after `pip install -e <path>/serial-monitor`.
Without the install, `python -m serial_monitor --show-config` works **only from
the serial-monitor repo itself** (a sibling checkout is not on `sys.path`) unless
you set `PYTHONPATH=<path>/serial-monitor`. The no-setup equivalent, which
resolves the *caller's* directory either way, is:

```powershell
pwsh ../serial-monitor/start-serial-monitor.ps1 -List
# Profile 'ck'  ->  bind 127.0.0.5  (from: profile:ck)   -- then it exits
```

Or run `list-serial-monitor.ps1`, which reports whether a monitor is up on the
bind resolved for your directory. **Do not carry an alias table in your head** —
which board owns which alias is a property of this PC's profiles, so ask for it:
`start-serial-monitor.ps1 -Help` prints every profile with its bind, and
`--show-config` prints the one your directory resolves to. The profiles that exist
today are `sonora` (`127.0.0.1`), `ck` (`127.0.0.5`), `nano` (`127.0.0.6`),
`samha` (`127.0.0.7`), `eval92m` (`127.0.0.31`) and `eval92s` (`127.0.0.32`) — see
`docs/binding-and-profiles.md`.

Then **confirm with `GET /status` that `profile` and `port` are the board you
mean** before sending anything. One call, and the wrong-monitor mistake stops
being invisible:

```json
{"profile": "ck", "port": "COM60", "connected": true, ...}
```

Examples below use `127.0.0.1:8080` as a stand-in; substitute the bind you
resolved.

## Step 0 — check the monitor is up and connected

```bash
curl -s http://127.0.0.1:8080/status
```
```json
{ "connected": true, "port": "COM12", "baud": 230400, "log_file": "..." }
```
- `connected: true`  → good, proceed.
- `connected: false` → the monitor is running but the board/port isn't attached
  (unplugged, powered off, or the console is wedged). Tell the human; do not open
  the port.
- Connection refused / no response → the monitor isn't running. Announce it, then
  **start it yourself** (see "Starting it yourself" below). Do **not** open the
  COM port directly as a workaround.

## Step 1 — send a command

`POST /command` with `{"cmd": "..."}`. The monitor appends the line terminator
(`\r\n` by default) and writes it to the UART.

```bash
curl -s -X POST http://127.0.0.1:8080/command \
  -H "Content-Type: application/json" -d '{"cmd":"?status"}'
```
```json
{ "ok": true, "sent": "?status" }
```
`503` means the UART isn't connected right now. `409` means a transfer holds the
transmit gate and **nothing was sent**.

## Step 2 — the reply: use `/command_wait`, not `/command` then `/wait`

**`POST /command_wait`** sends one command and waits for a marker in the answer,
with the matcher armed *before* the byte leaves. This is the endpoint to use for
anything whose reply you provoked.

```bash
curl -s -X POST http://127.0.0.1:8080/command_wait \
  -H "Content-Type: application/json" \
  -d '{"cmd":"?status","contains":"READY","timeout":10}'
```
- `200` `{ "matched": true, "sent": "?status", "line": "…<< … READY …" }`
- `408` → the command **was** sent; the marker never appeared.
- `409` → a transfer holds the gate; the command was **not** sent.

**Why not `/command` then `/wait`:** `POST /wait` matches only what is observed
**after the call that started it**, and never searches buffered traffic. A console
that answers in milliseconds has already answered before a second HTTP request can
be served, so send-then-wait times out on exactly the commands that reply fastest.
The ordering can only be fixed inside the monitor, which is what `/command_wait`
is.

`POST /wait` on its own is still right for output the board produces
unprompted — a boot banner, telemetry, the result of a reset someone else
triggered — or when *another* client sends the command:

```bash
curl -s -X POST http://127.0.0.1:8080/wait \
  -H "Content-Type: application/json" -d '{"contains":"boot","timeout":10}'
```

If you must use the two separate calls (an older monitor with no
`/command_wait`), **arm `/wait` first in the background, then send**:

```bash
( curl -s -X POST http://127.0.0.1:8080/wait \
    -H 'Content-Type: application/json' -d '{"contains":"READY","timeout":10}' ) &
sleep 1                                    # let the waiter register
curl -s -X POST http://127.0.0.1:8080/command \
  -H 'Content-Type: application/json' -d '{"cmd":"?status"}'
wait
```

Matching in both endpoints is against the raw RX byte stream, works across UART
read chunk boundaries, and is case-sensitive.

## Step 3 — read recent log lines

`GET /log?tail=N` returns the last `N` lines (RX, TX, and monitor notes).

```bash
curl -s "http://127.0.0.1:8080/log?tail=50"
```
Line prefixes: `>>` sent to the board, `<<` received from the board,
`--` monitor status (connect / disconnect / reconnect).

Text is buffered in memory and written out per line, but **the log never waits for
Enter** — the transport does not, and neither does the record. A buffer that has
sat unchanged for 200 ms is written anyway, marked `[partial idle]`:

```
16:26:19.318 >> [partial idle] [source=tcp:127.0.0.1:54185] *
```

So a single keystroke with no newline is visible within 200 ms, and an `<<`
`[partial idle]` line means the board stopped mid-line. `[partial]` (no `idle`)
means the buffer was cut off by a non-text chunk or by the 8 KB cap. Anything
without a `[partial…]` marker is a line that really ended. Text lines carry
`[source=…]` for who wrote them, the same way `[raw …]` lines do.

The full, timestamped history is also on disk at the `log_file` path reported by
`/status` — read that file if you need more than the in-memory tail.

## Firmware update over XMODEM (`POST /xmodem_send`) — only into a receiver that exists

`POST /xmodem_send` drives a firmware image out the UART with **XMODEM-CRC** into
a board-side receiver. Whether your firmware *has* such a receiver, and which verb
arms it, is a property of that firmware and not of this tool — which documents
none of them. **Do not call this endpoint unless the human has confirmed the
running image accepts a transfer.** When it does, the transfer has to run
*through* the monitor: it owns the COM port, so you cannot XMODEM the board
yourself.

`POST /xmodem_send` body:

| field               | default | meaning                                                        |
|---------------------|---------|----------------------------------------------------------------|
| `path`              | —       | image file **on the monitor host** (required)                  |
| `block`             | `1024`  | `1024` (STX/1K) or `128` (SOH); last short block auto-uses 128 |
| `arm_cmd`           | `null`  | your board's verb for arming its receiver, e.g. `"enter-update-mode"` |
| `handshake_timeout` | `30`    | seconds to wait for the receiver's `C` handshake               |
| `ack_timeout`       | `10`    | seconds per-block ACK wait                                     |
| `result_marker`     | `null`  | substring of the board's result line to await, e.g. `"crc="`   |
| `result_timeout`    | `30`    | seconds to wait for `result_marker` after EOT                  |

**`arm_cmd` must land the board in its receiver by itself.** Nothing here resets
anything: the verb is written, and then the `C` poll is awaited directly. A verb
that merely *prepares* an update and returns with the application still running is
not enough — no receiver starts, and the call fails with `no 'C' handshake` having
sent no image at all. If arming needs a manual step (a reset, a jumper, a button),
do that yourself, confirm the receiver's `C` poll is running, then call this
endpoint with no `arm_cmd`.

**While the board is being armed the terminals stay usable**, deliberately: no
frames are in flight yet, and a keystroke during that wait is often exactly what
is needed. The transmit gate closes on the **first `C` byte** — not when that byte
is confirmed to be the handshake: confirming takes a short silence, and a receiver
that has sent `C` is already waiting for block 1, so anything typed in between
would be read as the start of a frame. A `C` that turns out to be ordinary boot
text hands the terminals straight back (`tx_gate.provisional` in `/status` is true
for that interval; `provisional_reverts` counts the ones that were text). If a
terminal write was **already in flight** when the `C` arrived, the transfer is
abandoned instead: that write began before the receiver started listening, so its
tail may already be sitting in front of block 1. You get `ok:false` with
`error` = *"a terminal write was still in flight when the 'C' handshake
arrived…"*, the receiver is cancelled, **no image was sent**, and the fix is to
retry with nobody typing (`tx_gate.contended_closes` in `/status` counts them). Every
step — the arm verb, the handshake deadline, progress every 32 blocks, the reason
a transfer failed — is announced on each connected terminal as its own
`[serial-monitor]` line, so a transfer never looks like a board that went silent.

The UART transport never switches modes: every byte remains raw. A passive,
asynchronous observer produces `/log` as readable text or bounded hex summaries
and implements `/wait` as a raw RX byte-substring match. Observer processing and
failures cannot alter or delay the transfer. Most receivers print a result line
when they are done, which is what `result_marker` catches; pick a marker specific
enough that it cannot plausibly occur inside the image payload.

One-shot end-to-end (arm + send + collect result):

```bash
curl -s -X POST http://127.0.0.1:8080/xmodem_send \
  -H 'Content-Type: application/json' \
  -d '{"path":"C:/tmp/test_image.bin","block":1024,"arm_cmd":"enter-update-mode","result_marker":"crc="}'
```
```json
{ "ok": true, "blocks": 42, "retries": 0, "bytes": 43008, "elapsed": 2.1,
  "result_line": "…<< … bytes=43008 … crc=1A2B accepted" }
```

`ok:false` + `error` = protocol failure (no handshake, too many retries, EOT not
ACKed, receiver CAN). **`ok:true` means the bytes went out, not that the board
accepted them** — only the board's own result line says that, so read
`result_line` rather than inferring a pass. Padding of the final short block is
`0xFF` (the erased-flash value) and not the classic `0x1A`, so the programmed tail
matches unprogrammed flash and a whole-image CRC check on the board is exact.

> **Note:** this endpoint only exists after the monitor is (re)started with the
> updated code. If it's a `404`, the running monitor predates this feature — see
> the monitor-restart caveat in the one rule (only restart one you started; else
> announce + get the go-ahead, or let the owner restart it).

## Interactive reference

While the monitor runs, `http://127.0.0.1:8080/docs` is the live OpenAPI (Swagger)
page listing every endpoint and schema.

## PowerShell equivalents (Windows)

```powershell
Invoke-RestMethod http://127.0.0.1:8080/status
Invoke-RestMethod http://127.0.0.1:8080/command_wait -Method Post -ContentType application/json -Body '{"cmd":"?status","contains":"READY","timeout":10}'
Invoke-RestMethod http://127.0.0.1:8080/command      -Method Post -ContentType application/json -Body '{"cmd":"?status"}'
Invoke-RestMethod "http://127.0.0.1:8080/log?tail=50"
```

## A complete example

```bash
# 1. confirm the bridge is live -- and that profile/port are the board you mean
curl -s http://127.0.0.1:8080/status | grep -q '"connected": true' || { echo "monitor down — announce, then start it (see 'Starting it yourself')"; exit 1; }
# 2. ask the board something and catch the answer in the SAME call
curl -s -X POST http://127.0.0.1:8080/command_wait -H 'Content-Type: application/json' \
  -d '{"cmd":"?status","contains":"READY","timeout":10}'
# 3. dump the surrounding context
curl -s "http://127.0.0.1:8080/log?tail=20"
```

## Troubleshooting

- **`/status` says `connected: false`** — board off/unplugged, or the console is
  wedged. Ask the human to power-cycle / reset the board. The monitor
  auto-reconnects once the port reappears.
- **Commands send OK but no reply / no telemetry** — the console may be wedged, or
  you may be waiting the wrong way: `/command` then `/wait` loses a fast reply
  (Step 2). Many CDC consoles also need **DTR asserted**, which the monitor does by
  default; a terminal that grabbed the port with DTR off can leave the console
  quiet until the board is reset.
- **Connection refused** — the monitor process isn't running. Announce it, then
  start it yourself (below).

## Starting it yourself

First announce it ("starting serial-monitor…"). Then, from the `serial-monitor`
repo root (COM port / baud may vary per board):

```powershell
cd serial-monitor

# see the COM ports + whether a monitor is already running (read-only; safe)
.\list-serial-monitor.ps1

# easiest: the launcher (230400/8080/23). No port is ever auto-picked: name one,
# or be prompted for one in an interactive shell.
.\start-serial-monitor.ps1 -Help           # which bind address is which board + examples
.\start-serial-monitor.ps1 -Port COM12     # name a port explicitly
.\start-serial-monitor.ps1 -Port 1         # or pick by list-serial-monitor.ps1 index
.\start-serial-monitor.ps1 -Profile ck -Port COM60   # another board (binds 127.0.0.5)
.\start-serial-monitor.ps1 -List           # just show ports + monitor status, then exit

# or the explicit command (name the port yourself). `python -m serial_monitor`
# needs this repo as the cwd; `serial-monitor` works anywhere once installed.
python -m serial_monitor --port COM12 --baud 230400 --http-port 8080 --tcp-port 23
```

**Some boards present two CDC ports, and they are not interchangeable.** A
debugger or programmer (PKOB4, nEDBG, CMSIS-DAP, an FTDI bridge) often exposes its
own console port beside the board's real UART, and firmware sometimes *mirrors*
application output onto it. Whatever a bootloader prints — a boot banner, an
XMODEM `C` poll — then exists only on the port the bootloader itself writes to,
while the mirror keeps looking perfectly healthy.

**A matching `profile` and `port` in `GET /status` does not prove the port is
right: the wrong port of the right board answers just as convincingly.** When two
ports share a vendor ID the PID is what separates them, and which PID plays which
role is a fact about your board — read its documentation, record the answer in your
firmware repo's own notes, and name the port explicitly here.

```powershell
Get-PnpDevice -Class Ports | Where-Object Status -eq OK |
    Select-Object FriendlyName, InstanceId    # VID_xxxx & PID_xxxx, per port
```

Run it in the background (or a separate terminal) so you can keep issuing HTTP
calls, then poll `GET /status` until `connected: true`. If a monitor is **already
running**, don't start another — the second one fails to bind the port/HTTP and
exits; just use the running one.

Logs default to `monitor_logs/` inside the `serial-monitor` folder (git-ignored,
no `--log-dir` needed). Humans can watch the same stream live in Tera Term via
TCP `127.0.0.1:23`, which is **read/write by default** (they can type commands
too); `--no-tcp-allow-input` makes it view-only. See `README.md` next to this
file for all options.

## Stopping it (when the work is done)

**Only stop the monitor if you started it.** If it was already running when you
arrived (you just used it as-is), **leave it running** — don't stop it.

If you started it, you own its shutdown: **stop it when your task is done** (and
say so) to free the board for a new monitor, Tera Term, or MPLAB. **Exception:**
if the user is watching in Tera Term or asked you to leave it running, leave it.

To stop: press **Ctrl-C** in the terminal running it (graceful — closes the port,
flushes logs). If you started it in the background, stop that process. Avoid
hard-killing it mid-stream; that can wedge the board's console (fix with a board
reset). Details: `README.md` → "Stopping the monitor".

## Where the copies live

This file is the **canonical** source of truth. Thin pointer copies only link
back here — if you change the rule, change it here.

- Canonical (full, this file): `serial-monitor/AI_UART_ACCESS.md`
  (the `serial-monitor` repo — the single home of this tool)
- Pointer, workspace root: `CLAUDE.md`, `AGENTS.md`
- Pointer, each consuming project root: `CLAUDE.md`, `AGENTS.md`

Older copies of the tool itself, duplicated into individual firmware projects
under `tools/`, are superseded by this repo. Point at this one rather than editing
a copy.
