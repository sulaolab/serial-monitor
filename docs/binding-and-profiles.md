# Which address does this monitor bind to?

## The rule: COM number equals the loopback suffix

Every monitor uses the selected Windows COM number as its loopback address:

| UART | HTTP API | Raw TCP / Tera Term |
|---|---|---|
| `COM4` | `http://127.0.0.4:8080` | `127.0.0.4:23` |
| `COM41` | `http://127.0.0.41:8080` | `127.0.0.41:23` |
| `COM230` | `http://127.0.0.230:8080` | `127.0.0.230:23` |

Tera Term already shows the COM number, so a human can derive both network
endpoints without consulting a separate allocation table. All of `127.0.0.0/8`
is loopback on Windows; no adapter configuration is required and the services
remain unreachable from the network.

`COM1` through `COM254` map directly. A different port name or a COM number
outside that range requires explicit `-HttpHost` and `-TcpHost` values. Explicit
host arguments always win over the derived address.

The dangerous failure is a healthy response from the wrong board. Always check
both fields returned by `GET /status`:

```json
{ "profile": "ck-ev97", "port": "COM41", "connected": true }
```

The URL tells you which UART number you aimed at; `profile` tells you which board
the monitor was started for. Neither one replaces the other.

Scripts that need an already-running endpoint must not parse the human table or
reconstruct a URL from a profile. Use the read-only machine interface:

```powershell
.\list-serial-monitor.ps1 -Json
.\resolve-serial-monitor.ps1 -Profile ck-ev97 -Json
```

The resolver selects the actual profile returned by `/status` and requires one
and only one match. If two monitors intentionally use the same board profile,
the caller must supply the intended URL explicitly.

## Profiles name boards; they do not allocate addresses

Shipped profiles are:

| Profile | Board |
|---|---|
| `ak-curio` | dsPIC33AK Curiosity Platform |
| `ak-nano` | dsPIC33AK Curiosity Nano |
| `ck-ev97` | EV97U97A Touch-CAN-LIN / dsPIC33CK1024MP710 (primary CK board) |
| `ck-dm33` | DM330030 / dsPIC33CK256MP508 (retiring) |
| `ck-nano` | dsPIC33CK Curiosity Nano |
| `samha` | SAM HA1G16A Xplained Pro |
| `AIS18031` | AIS18031 / ATSAMV71Q21B |
| `eval92m` / `eval92s` | EVAL92 pair |

The legacy name `sonora` is an unambiguous compatibility alias for `ak-curio`.
The old names `ck` and `nano` each referred to more than one physical board, so
they are refused with a message naming the explicit choices. Silently mapping
either name would put a plausible but false board label on a live UART.

Profiles for a private bench may live in either location below. First match wins:

1. each directory in `$SERIAL_MONITOR_PROFILES` (`;`-separated on Windows);
2. `.serial-monitor-profiles/` next to this checkout;
3. `profiles/` in this checkout.

An exact external profile is allowed to use a retired name; exact files win over
the shipped alias/ambiguity policy.

## Selecting a profile

Precedence, highest first:

1. `-Profile ck-ev97` / `--profile ck-ev97`;
2. `.serial-monitor.json` found by walking upward from the caller's directory;
3. no profile (the generic default name/settings).

A firmware repository normally declares its board once:

```jsonc
{ "profile": "ck-ev97" }
```

Then start it by naming the port visible in Windows or Tera Term:

```powershell
pwsh ..\serial-monitor\start-serial-monitor.ps1 -Port COM41
# Profile 'ck-ev97' -> HTTP 127.0.0.41:8080, raw TCP 127.0.0.41:23
```

Allowed configuration keys are `name`, `profile`, `baud`, `terminator`,
`http_host`, `http_port`, `tcp_host`, `tcp_port`, and `vid`. Whole-line `//`
comments are allowed. Unknown keys are errors. `port` is intentionally not an
accepted key because COM numbers can change when a board is re-enumerated.

Host fields remain valid for non-Windows port names and configuration inspection,
but a Windows `COM<n>` launch uses its derived host unless the corresponding
command-line host is explicit. Shipped profiles therefore contain no host fields.

## Port selection never guesses the board

- `-Port COM41`: explicit name;
- `-Port 2`: index shown by `-List`;
- omitted in an interactive shell: show the list and prompt, with no default;
- omitted non-interactively: stop unless a caller-supplied `-Vid` leaves exactly
  one candidate.

A profile's `vid` is descriptive and is not silently applied as a port filter.
Several boards share the same debugger VID. Use `-Vid` only when you intentionally
want to narrow the displayed candidates.

## Listing running monitors

Run:

```powershell
.\list-serial-monitor.ps1
```

The list helper never opens a COM port. It:

1. enumerates current COM devices through PnP;
2. reads running `python -m serial_monitor` process command lines;
3. probes the endpoint each process actually declared.

The process-derived probe preserves visibility of monitors started under the old fixed-address
scheme and of a monitor holding a dead handle after its board re-enumerated.
This is why an already-running `ck` monitor on `127.0.0.5` need not be stopped
during migration.

An explicit `-HttpHost` or `-HttpPort` requests a focused one-endpoint check.

## Concurrent use and stopping

Each monitor is a separate process. A bind conflict is checked before the COM
port is opened, and the existing `/status` owner is reported when possible.
Logs remain namespaced by profile under `serial_monitor/monitor_logs/<profile>/`.

Stopping is intentionally narrow:

```powershell
.\stop-serial-monitor.ps1 -Profile ck-ev97
.\stop-serial-monitor.ps1 -HttpHost 127.0.0.41
.\stop-serial-monitor.ps1 -Port COM41
```

With several matches, an interactive caller must choose one. A redirected caller
stops nothing. `-All` is explicit and should only be used when every board owner
has agreed. Prefer Ctrl-C in the monitor's own window for a graceful shutdown.

## For AI agents

Given `COM41`, try `http://127.0.0.41:8080/status`, but never treat a successful
HTTP response alone as proof. Confirm `profile`, `port`, and `connected` before
sending a command. Do not open the COM port directly. Full protocol and ownership
rules are in [`../AI_UART_ACCESS.md`](../AI_UART_ACCESS.md).
