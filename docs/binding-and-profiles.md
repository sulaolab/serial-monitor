# Which address does this monitor bind to?

## The problem

Several boards get debugged on this PC at the same time, and each wants its own
monitor. They all want port 8080, so they are separated by **loopback alias**
instead:

| Profile | HTTP + raw TCP bind | Board it belongs to |
|---|---|---|
| `sonora` | `127.0.0.1:8080` / `:23` | dsPIC33AK board (PKOB4 CDC, VID_04D8) |
| `ck` | `127.0.0.5:8080` / `:23` | dsPIC33CK kit (Curiosity Nano nEDBG, VID_03EB) |
| `nano` | `127.0.0.6:8080` / `:23` | another Curiosity Nano |
| `eval92m` | `127.0.0.31:8080` / `:23` | EVAL92 pair, `m` unit (no VID filter) |
| `eval92s` | `127.0.0.32:8080` / `:23` | EVAL92 pair, `s` unit (no VID filter) |

The two `eval92*` profiles declare no `vid`, so the interactive prompt cannot
narrow the port list for them — name the port yourself (`-Port COM<n>`).

All of `127.0.0.0/8` is loopback on Windows with no setup needed, so an alias
costs nothing and keeps the API unreachable from the network.

Profiles for a bench you would rather not keep in this checkout can live outside
it: `.serial-monitor-profiles/<name>.json` **next to** the checkout, or any
directory in `$SERIAL_MONITOR_PROFILES`, both of which win over a file of the same
name in `profiles/`. `-Help` and `--show-config` list the union either way.

The dangerous failure here is not a port conflict — that one is loud. It is an
agent that reads `127.0.0.1:8080` from a document, gets a healthy `/status`, and
drives the **other** board while believing it is on the right one. Everything
below exists to make that mistake hard to make and cheap to notice.

## Where the setting comes from

Precedence, highest first:

1. an explicit flag — `-HttpHost 127.0.0.9`, `--http-host …`
2. **`.serial-monitor.json` found by walking up from your current directory**
3. `-Profile ck` / `--profile ck` → `ck.json`, found along the profile search
   path (an outside directory first, this repo's `profiles/` last)
4. built-in defaults (`127.0.0.1:8080`, 230400 baud)

The recommended shape is (2): the firmware repo declares its board once, and then
nobody — human or agent — has to remember an address.

```jsonc
// dspic33ck-hal-lab/.serial-monitor.json
{ "profile": "ck" }
```

```powershell
cd dspic33ck-hal-lab            # anywhere inside the repo, any depth
..\serial-monitor\start-serial-monitor.ps1
# Profile 'ck'  ->  bind 127.0.0.5  (from: profile:ck)
```

A project config can also override individual fields — a second CK board on the
same alias but a different port:

```jsonc
{ "profile": "ck", "http_port": 8090, "name": "ck-second-board" }
```

Allowed keys: `name`, `profile`, `baud`, `terminator`, `http_host`, `http_port`,
`tcp_host`, `tcp_port`, `vid`. Whole-line `//` comments are allowed (a `//` inside
a value is left alone — truncating a value silently would be worse than refusing
the file). An unknown key is an **error**, not a shrug — a
silently ignored `htp_host` is an afternoon of "why is it still on 8080".

Inspect the outcome without opening a port:

```powershell
serial-monitor --show-config --profile ck       # values + where each came from
```

That command needs the package installed (`pip install -e <this repo>`); without
it, `python -m serial_monitor --show-config` resolves only when the current
directory is this repo, or `PYTHONPATH` points at it. With no setup at all, the
launcher answers the same question about the directory you are standing in:

```powershell
pwsh ..\serial-monitor\start-serial-monitor.ps1 -List
```

## What a profile deliberately does NOT set: the COM port

Port numbers are reassigned when a board is re-plugged, so a pinned `COM12`
eventually points at something else — and being bridged to the wrong board is the
exact failure this whole mechanism is trying to prevent. `port` is not even an
accepted key.

So port selection stays as it was:

- `-Port COM12` — explicit
- `-Port 2` — by index from `-List`
- **omitted, interactive shell → the list is shown and you are prompted.** There
  is no default; Enter on its own is an error
- omitted, non-interactive (agent/CI) → it stops and asks for `-Port`, unless a
  `-Vid` **you** passed leaves exactly one candidate

Nothing is recommended and nothing is auto-picked. A profile's `vid` is **not**
inherited by the launcher either: these scripts are shared by every board in the
workspace, so any hint they offer about the port is a guess dressed as knowledge
— it starred the *other* board's port while being run from one board's repo
(2026-08-09).
`-Vid` remains as a filter you can type yourself — and because it is a *display*
filter, it never changes what the tool claims about the machine: the monitor's own
port is checked against every port present, not against the filtered view.

## Two implementations would be one too many

The precedence rules live in [`serial_monitor/config.py`](../serial_monitor/config.py)
only. The PowerShell launcher does not reimplement them — it calls
`python -m serial_monitor.config --start-dir <caller's cwd> --with-sources` and
uses the answer, then passes every resolved value explicitly to
`python -m serial_monitor` with `--no-project-config` so the child re-resolves
nothing. Otherwise "where does the bind come from" would start depending on which
of the two you asked.

The start directory is captured **before** any `Push-Location`, so discovery walks
up from where *you* stood, not from the serial-monitor repo.

## Three ways the wrong-board mistake gets caught

1. **`GET /status` reports `profile`.** One call answers "which board am I on":
   `{"profile": "ck", "port": "COM60", "connected": true, …}`. An agent that
   checks this cannot silently be on the wrong monitor.
2. **The launcher prints the bind and its origin** on every start:
   `Profile 'ck' -> bind 127.0.0.5 (from: profile:ck)`. If a stale
   `.serial-monitor.json` is winning, you see it there.
3. **A taken bind is refused before the COM port is opened**, and the running
   monitor is identified: `HTTP bind 127.0.0.5:8080 is already in use. It answers
   /status as: profile='ck' uart=COM60 connected=True`. Without this you get a
   bare `WinError` from deep inside uvicorn — after this process has already taken
   the UART away from whoever had it.

## One install, several boards, several sessions

The tool is installed **once** (this repo, in the workspace root) and every
firmware project uses that one copy. Each project declares its board in its own
`.serial-monitor.json`; nothing else is distributed.

Sharing one install across concurrent sessions is safe by construction:

| Shared thing | Why it is fine |
|---|---|
| Processes | Fully independent. One per board, any number at once. |
| The COM port | Windows gives it to one process. A second attempt fails, by design. |
| The bind address | Checked before the COM port is opened; a clash is refused and the holder named. |
| Log files | `<port>-<date>.log`, and a port is held by one process -- names cannot collide. |
| `__pycache__` | CPython writes a temp file and renames it. Safe under concurrency. |

Two things needed fixing for this to be true, and both are done:

**Logs are namespaced by profile** -- `monitor_logs/<profile>/COM60-20260809.log`.
Names would not have collided anyway, but one flat directory mixing every board's
console is a bad place to look for evidence later.

**Stopping refuses to guess.** `stop-serial-monitor.ps1` used to kill every
`serial_monitor` process it found, and `-HttpPort 8080` did not narrow anything
(instances differ by *alias*, not port). With several boards live that takes out
another session's work mid-command -- which is exactly what happened on
2026-08-09. It now lists the matches and stops nothing unless you name one:

```powershell
.\stop-serial-monitor.ps1                 # 2 running -> lists them, exits 1, kills nothing
.\stop-serial-monitor.ps1 -Profile ck      # exactly the CK board's monitor
.\stop-serial-monitor.ps1 -HttpHost 127.0.0.5
.\stop-serial-monitor.ps1 -Port COM60
.\stop-serial-monitor.ps1 -All             # every instance, when you are sure
```

Prefer Ctrl-C in the monitor's own window: that is a graceful close (port
released, logs flushed).

One caveat that is about *developing* this tool rather than using it: a single
shared working tree means two sessions editing it can swallow each other's
uncommitted changes. Change `serial-monitor` from one session at a time.

## For AI agents

**The base URL is not a constant.** Do not assume `127.0.0.1:8080`. Either

- run `list-serial-monitor.ps1` (it reports whether a monitor is up on the bind it
  resolved for your directory), or
- run `python -m serial_monitor --show-config` to see the bind for the repo you
  are working in,

then confirm with `GET /status` that `profile` and `port` are the board you mean.
See [`../AI_UART_ACCESS.md`](../AI_UART_ACCESS.md) for the rest of the protocol.
