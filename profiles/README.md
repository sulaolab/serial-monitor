# profiles/

One file per board family, holding only what that family fixes: which loopback
alias its monitor binds to, its baud, and — as documentation — the USB vendor ID
that board's debugger uses.

Every monitor on one PC wants port 8080, so they are separated by address:
`sonora` on `127.0.0.1`, `ck` on `127.0.0.5`, `nano` on `127.0.0.6`, `eval92m` on
`127.0.0.31`, `eval92s` on `127.0.0.32`. All are loopback (`127.0.0.0/8` is
entirely local, no OS setup needed).

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
recommended or auto-picked either. `vid` is **not** inherited as a filter by the
launcher: it is printed by `-Help` so you know which VID to expect, and narrowing
the list is done by a `-Vid` you type yourself.

Usage:

```powershell
.\start-serial-monitor.ps1 -Profile ck
```

Better, from a firmware repo that declares itself once in `.serial-monitor.json`:

```json
{ "profile": "ck" }
```

Then plain `start-serial-monitor.ps1` run anywhere inside that repo picks the ck
bind up automatically -- see `../docs/binding-and-profiles.md`.
