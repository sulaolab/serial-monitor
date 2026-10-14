# profiles/

One file per board family, holding only what that family fixes: its canonical
name, baud, and — as documentation — the USB vendor ID its debugger uses.

Profiles do **not** allocate loopback aliases. The selected COM number does:
`COM41` uses `127.0.0.41:8080` for HTTP and `127.0.0.41:23` for raw TCP. This
makes the address derivable from Tera Term or Device Manager without a table.

## A profile may also live outside this checkout

Profiles are looked up in this order, first match winning:

1. every directory in `$SERIAL_MONITOR_PROFILES` (`;`-separated on Windows);
2. `.serial-monitor-profiles/` **next to** this checkout — no setup at all;
3. `profiles/` in this checkout — the files below.

Use (1) or (2) for a bench you would rather not keep in the checkout: a
same-named file there wins, and nothing a `git pull` brings can overwrite it.
`-Help` and `--show-config` list the union of all three, so what you are shown is
what resolution will actually use.

A profile never names a COM port. Port numbers change when a board is re-plugged,
so freezing one here is a way to bridge to the wrong board — and no port is ever
recommended or auto-picked either. Once the caller names the current port, that
number safely supplies the loopback suffix for this run. `vid` is **not** inherited
as a filter by the launcher: narrowing the list is done by a `-Vid` you type.

Usage:

```powershell
.\start-serial-monitor.ps1 -Profile ck-ev97 -Port COM41
```

Better, from a firmware repo that declares itself once in `.serial-monitor.json`:

```json
{ "profile": "ck-ev97" }
```

Then `start-serial-monitor.ps1 -Port COM41` run anywhere inside that repo picks
the board name up automatically and derives `127.0.0.41` from the named port.
See `../docs/binding-and-profiles.md`.
