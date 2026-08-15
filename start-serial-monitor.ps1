#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Start the generic serial monitor UART bridge.

.DESCRIPTION
    Short launcher so you don't have to type the full
    `python -m serial_monitor ...` command. Runs in the FOREGROUND:
    press Ctrl-C to stop it gracefully (closes the COM port, flushes logs).

    Port selection (in priority order):
      * -List                 -> just show list-serial-monitor.ps1 and exit.
      * -Port COM12           -> use that port directly.
      * -Port 1               -> a bare number picks that Index from the list.
      * -Port omitted, interactive shell -> show the list and prompt you to pick.
        There is no default: Enter on its own is an error, not a choice.
      * -Port omitted, non-interactive    -> refuse to start, unless YOU passed
        -Vid and it leaves exactly one candidate.
      * -Vid 03EB / -Vid 04D8 -> show only that USB vendor's ports.

    Nothing is ever recommended or auto-picked. VID_03EB is a Curiosity Nano's
    own nEDBG CDC console and VID_04D8 a Microchip PKOB4, and both are printed
    for every port -- but this script is shared by every board in the workspace
    and cannot know which one you mean. It used to guess by VID preference order
    and would confidently star one board while run from another board's repo
    (measured 2026-08-09), so the guess is gone rather than tuned.

     Bind address and baud come from the board's profile: a .serial-monitor.json
     in the firmware repo you are standing in, or -Profile <name>. Without either,
     230400 baud on 127.0.0.1:8080 (HTTP) and 127.0.0.1:23 (raw TCP). Two boards
     at once want two aliases -- see docs/binding-and-profiles.md. Logs go to
     serial_monitor/monitor_logs/.

     With no arguments, after selecting a COM port interactively, choose either
     the normal raw TCP address or 127.0.0.<COM-number>. The HTTP API follows
     the same selected address, so the whole monitor stays on one alias. Any
     argument preserves explicit/profile address behaviour and skips that second
     menu.

    The COM port is never taken from a profile: port numbers move when a board is
    re-plugged, so naming it stays your job every time -- interactively, with
    -Port, or with a -Vid that YOU passed and that leaves exactly one candidate.

.EXAMPLE
    .\start-serial-monitor.ps1 -Help
    Show which bind address belongs to which board (read from profiles/*.json),
    how the profile is chosen, and the command for each board -- read from the
    profile search path, not typed out here. Exits without starting anything.

.EXAMPLE
    .\start-serial-monitor.ps1 -List
    Show the COM ports and the current monitor status, then exit.

.EXAMPLE
    .\start-serial-monitor.ps1
    Pick a port interactively, then choose the default raw TCP address or the
    address derived from that COM number (for example COM31 -> 127.0.0.31:23).
    The HTTP API follows the selected address.

.EXAMPLE
    .\start-serial-monitor.ps1 -Port COM12 -Baud 115200
    Use an explicit port and baud.

.EXAMPLE
    .\start-serial-monitor.ps1 -Port 2
    Use the port shown as index 2 in list-serial-monitor.ps1.

.EXAMPLE
    .\start-serial-monitor.ps1 -ReadOnly
    Make the Tera Term TCP tail view-only (default is read/write).
#>
[CmdletBinding()]
param(
    # COM name (COM12), a bare list index (2), or empty to select interactively.
    [string]$Port = '',

    # Board family whose loopback alias / baud / VID hint to use, e.g. 'ck'.
    # Any name -Help lists (profiles/ here, or a directory outside the checkout).
    # Usually unnecessary: a .serial-monitor.json in the firmware repo you are
    # standing in already declares it. See docs/binding-and-profiles.md.
    [string]$Profile = '',

    # These four default to whatever the profile / .serial-monitor.json resolves
    # to. Passing one explicitly wins over both.
    [int]$Baud = 0,
    [string]$HttpHost = '',
    [int]$HttpPort = 0,
    [string]$TcpHost = '',
    [int]$TcpPort = -1,
    # Restrict port selection to one USB vendor ID, e.g. '03EB' for a Curiosity
    # Nano's own CDC console, '04D8' for a Microchip PKOB4. Passed through to
    # list-serial-monitor.ps1. Useful when several debugger boards are attached.
    [string]$Vid,
    # Just list the ports + monitor status (via list-serial-monitor.ps1) and exit.
    [switch]$List,
    # Print how to aim this at a specific board (which bind address, how to pick
    # it) and exit. Reads the actual profiles, so it cannot drift from them.
    [switch]$Help,
    # By default the Tera Term TCP tail is read/write (you can type commands).
    # Pass -ReadOnly to make it view-only.
    [switch]$ReadOnly,
    # Any extra flags are passed straight through to `python -m serial_monitor`
    # (e.g. --log-dir <path>, --dtr off).
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

# With no arguments this script is a two-step interactive launcher: choose the
# UART first, then choose whether to keep the normal loopback address or give
# this UART its own 127.0.0.<COM-number> address.  Any supplied argument keeps
# the existing non-interactive/explicit behaviour intact.
$bareInteractiveStart = $PSBoundParameters.Count -eq 0

$ErrorActionPreference = 'Stop'

# NOTE: we deliberately do NOT `Set-Location $PSScriptRoot` here. That would
# leave the *caller's* shell sitting in the repo root after the monitor exits (the
# current location is a runspace-wide property, so it persists past the script).
# Everything below uses absolute paths ($PSScriptRoot); only the final
# `python -m serial_monitor` needs the repo root as its working directory, and that is
# scoped to the python process alone (Push/Pop) at the bottom of the script.

$listScript = Join-Path $PSScriptRoot 'list-serial-monitor.ps1'

<#
  -Help exists because the two things you have to get right -- which board's bind
  address, and which COM port -- are the two things this script will not guess for
  you, and `Get-Help` buries that under the parameter list. The profile table is
  read from profiles/*.json rather than typed out here: a help text that lists
  addresses from memory is exactly how someone ends up driving 127.0.0.1 while
  reading a document that says 127.0.0.5.
#>
function Show-BoardHelp {
    # Every directory config.py searches, in the same precedence: a profile kept
    # outside the checkout wins over a same-named one in profiles/. Listing only
    # profiles/ would hide a board that resolution would actually pick.
    $profileDirs = @()
    if ($env:SERIAL_MONITOR_PROFILES) {
        $profileDirs += ($env:SERIAL_MONITOR_PROFILES -split ';' | Where-Object { $_.Trim() })
    }
    $profileDirs += (Join-Path (Split-Path -Parent $PSScriptRoot) '.serial-monitor-profiles')
    $profileDirs += (Join-Path $PSScriptRoot 'profiles')
    Write-Host ""
    Write-Host "start-serial-monitor.ps1 -- one monitor per board, each on its own loopback alias." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Two things must be right, and neither is guessed for you:" -ForegroundColor Cyan
    Write-Host "    1. the BIND ADDRESS -- which board's monitor this is (all on port 8080)"
    Write-Host "    2. the COM PORT     -- never taken from a profile; port numbers move on re-plug"
    Write-Host ""
    Write-Host "  With no arguments: choose a COM port, then choose the default raw TCP address" -ForegroundColor Cyan
    Write-Host "  or 127.0.0.<COM-number>. The HTTP API follows the selected address." -ForegroundColor Cyan
    Write-Host "  Passing any argument keeps explicit/profile address selection and skips this second menu."
    Write-Host ""

    Write-Host "  Profiles (bind address comes from here):" -ForegroundColor Cyan
    $seen = @{}
    foreach ($dir in $profileDirs) {
        if (-not (Test-Path $dir)) { continue }
        foreach ($f in (Get-ChildItem -Path $dir -Filter *.json | Sort-Object Name)) {
            if ($seen.ContainsKey($f.Name)) { continue }   # a nearer dir already won
            $seen[$f.Name] = $true
            try { $p = Get-Content -Raw -LiteralPath $f.FullName | ConvertFrom-Json } catch { continue }
            $vid = if ($p.vid) { "VID_$($p.vid)" } else { 'any VID' }
            Write-Host ("    -Profile {0,-16} ->  http://{1}:{2}   (raw TCP {1}:{3}, {4} baud, {5})" -f `
                $p.name, $p.http_host, $(if ($p.http_port) { $p.http_port } else { 8080 }), `
                $(if ($p.tcp_port) { $p.tcp_port } else { 23 }), $p.baud, $vid)
        }
    }
    Write-Host "    (searched: $($profileDirs -join ' ; '))" -ForegroundColor DarkGray
    Write-Host "    (no profile)      ->  http://127.0.0.1:8080   -- the DEFAULT" -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "  How the profile is chosen, in order:" -ForegroundColor Cyan
    Write-Host "    1. -Profile <name> on the command line"
    Write-Host "    2. a .serial-monitor.json in the directory you are standing in (or above it)"
    Write-Host "    3. nothing -> the default above. Standing in serial-monitor/ itself gives you"
    Write-Host "       this case, which binds the default address whatever board you meant." -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "  Examples:" -ForegroundColor Cyan
    Write-Host "    # interactive: select COM31, then choose 127.0.0.1:23 or 127.0.0.31:23:"
    Write-Host "    .\start-serial-monitor.ps1" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # CK board (Curiosity Nano) on 127.0.0.5, from anywhere:"
    Write-Host "    .\start-serial-monitor.ps1 -Profile ck -Vid 03EB" -ForegroundColor Green
    Write-Host "    .\start-serial-monitor.ps1 -Profile ck -Port COM60" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # sonora (dsPIC33AK, PKOB4 CDC) on 127.0.0.1:"
    Write-Host "    .\start-serial-monitor.ps1 -Profile sonora -Port COM12" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # from inside a firmware repo, the profile is already declared -- name the port:"
    Write-Host "    pwsh ../serial-monitor/start-serial-monitor.ps1 -Port COM12" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # an address no profile covers (overrides the profile; both halves needed):"
    Write-Host "    .\start-serial-monitor.ps1 -HttpHost 127.0.0.5 -TcpHost 127.0.0.5 -Port COM60" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Check before you drive it:" -ForegroundColor Cyan
    Write-Host "    .\start-serial-monitor.ps1 -List        # resolved bind + ports + is one running"
    Write-Host "    curl http://127.0.0.5:8080/status       # confirm profile+port ARE the board you mean"
    Write-Host "    .\stop-serial-monitor.ps1 -Profile ck   # never -All: that kills other boards too" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Full details: docs/binding-and-profiles.md   |   Get-Help for every parameter." -ForegroundColor DarkGray
    Write-Host ""
}

if ($Help) {
    Show-BoardHelp
    exit 0
}

# --- resolve the bind/baud/VID -----------------------------------------------
# Delegated to `python -m serial_monitor.config`, deliberately: two boards are
# separated only by their loopback alias, and two implementations of "where does
# that come from" is how the answer starts depending on which one you asked. The
# resolver walks up from the CALLER's directory ($startDir, captured before any
# Push-Location), so standing in a firmware repo that declares
# .serial-monitor.json is enough.
$startDir = (Get-Location).Path
$cfgArgs = @('-m', 'serial_monitor.config', '--start-dir', $startDir, '--with-sources')
if (-not [string]::IsNullOrWhiteSpace($Profile)) { $cfgArgs += @('--profile', $Profile) }

Push-Location $PSScriptRoot
try { $cfgJson = & python @cfgArgs 2>&1 | Out-String }
finally { Pop-Location }

try { $cfg = $cfgJson | ConvertFrom-Json }
catch {
    Write-Error "Could not resolve settings. python said:`n$cfgJson"
    exit 1
}
if ($cfg.PSObject.Properties.Name -contains 'error') {
    Write-Error $cfg.error
    exit 1
}

# An explicitly passed parameter always wins over the resolved value.
if (-not $PSBoundParameters.ContainsKey('Baud'))     { $Baud     = [int]$cfg.baud }
if (-not $PSBoundParameters.ContainsKey('HttpHost')) { $HttpHost = [string]$cfg.http_host }
if (-not $PSBoundParameters.ContainsKey('HttpPort')) { $HttpPort = [int]$cfg.http_port }
if (-not $PSBoundParameters.ContainsKey('TcpHost'))  { $TcpHost  = [string]$cfg.tcp_host }
if (-not $PSBoundParameters.ContainsKey('TcpPort'))  { $TcpPort  = [int]$cfg.tcp_port }
# A profile's `vid` is NOT inherited. It decides the bind address and nothing
# about the COM port: letting a shared config file silently narrow the port list
# is the same misdirection as starring a port -- it just hides candidates instead
# of pointing at one. Pass -Vid yourself when you want that filter.

$profileName = [string]$cfg.name
# Report where the bind ACTUALLY came from: an explicit -HttpHost has already
# overwritten the resolved value above, so echoing the resolver's source there
# would name a file that lost.
$bindOrigin = if ($PSBoundParameters.ContainsKey('HttpHost')) { 'command line' }
              else { [string]$cfg._sources.http_host }

# -List: show the ports + monitor status and stop here. -Vid must reach the list
# script here too, or `-List -Vid 04D8` would show the default 03EB-first view.
if ($List) {
    Write-Host ("Profile '{0}'  ->  bind {1}  (from: {2})" -f $profileName, $HttpHost, $bindOrigin) -ForegroundColor DarkCyan
    if (-not [string]::IsNullOrWhiteSpace($Vid)) {
        & $listScript -HttpHost $HttpHost -HttpPort $HttpPort -Vid $Vid
    } else {
        & $listScript -HttpHost $HttpHost -HttpPort $HttpPort
    }
    exit 0
}

# --- resolve the port ---
function Resolve-Port {
    param([string]$Requested)

    $listArgs = @{ HttpHost = $HttpHost; HttpPort = $HttpPort; Quiet = $true }
    if (-not [string]::IsNullOrWhiteSpace($Vid)) { $listArgs['Vid'] = $Vid }
    $ports = @(& $listScript @listArgs)
    if ($ports.Count -eq 0) {
        Write-Error "No serial (COM) ports found. Plug in the board and retry."
        exit 1
    }

    # Nothing here picks a port on its own -- not $ports[0], and no VID
    # preference order either. The COM port is the one thing this tool cannot
    # infer: the profile only knows the bind address, and a mark that guesses
    # "probably this board" reads exactly like one that knows (it starred one
    # board while run from another's repo, 2026-08-09). Say which port you mean.
    # The one non-interactive shortcut is -Vid: if YOU narrow the list to a vendor
    # and exactly one port is left, that is your statement, not our guess.

    # bare number -> pick that Index from the list
    if ($Requested -match '^\d+$') {
        $sel = $ports | Where-Object { $_.Index -eq [int]$Requested } | Select-Object -First 1
        if (-not $sel) {
            Write-Error "No port at index $Requested. Run: .\start-serial-monitor.ps1 -List"
            exit 1
        }
        return $sel.Port
    }

    # explicit COM name
    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        return $Requested.ToUpper()
    }

    # omitted: ask. There is no default to fall back on, deliberately.
    if (-not [Console]::IsInputRedirected) {
        if (-not [string]::IsNullOrWhiteSpace($Vid)) {
            & $listScript -HttpHost $HttpHost -HttpPort $HttpPort -Vid $Vid
        } else {
            & $listScript -HttpHost $HttpHost -HttpPort $HttpPort
        }
        $ans = Read-Host "Select port by number or name (no default -- name the board you mean)"
        if ([string]::IsNullOrWhiteSpace($ans)) {
            Write-Error "No port selected. Re-run with -Port COM<n>."
            exit 1
        }
        if ($ans -match '^\d+$') {
            $sel = $ports | Where-Object { $_.Index -eq [int]$ans } | Select-Object -First 1
            if (-not $sel) {
                Write-Error "No port at index $ans."
                exit 1
            }
            return $sel.Port
        }
        return $ans.ToUpper()
    }

    # Non-interactive (agent / CI): the only accepted shortcut is a -Vid the
    # caller passed that leaves exactly one candidate.
    if (-not [string]::IsNullOrWhiteSpace($Vid) -and $ports.Count -eq 1) {
        Write-Host ("No -Port given; -Vid $Vid leaves exactly one: {0} ({1})." -f $ports[0].Port, $ports[0].Description) -ForegroundColor Yellow
        return $ports[0].Port
    }

    Write-Host "Cannot select a port without being told which. Candidates:" -ForegroundColor Yellow
    foreach ($p in $ports) {
        Write-Host ("  {0,-6} {1}  VID_{2}" -f $p.Port, $p.Description, $p.Vid) -ForegroundColor Yellow
    }
    Write-Error "Re-run with -Port COM<n> (or -Vid <4 hex digits> if that leaves one port)."
    exit 1
}

function Select-InteractiveBind {
    param(
        [Parameter(Mandatory)]
        [string]$SelectedPort
    )

    # COM names selected by Resolve-Port are normalized to COM<n>.  Restrict the
    # derived address to one IPv4 octet: COM256 and above cannot map to a valid
    # 127.0.0.<n> host and therefore retain the normal default only.
    $match = [regex]::Match($SelectedPort, '^COM(\d+)$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) {
        return $null
    }
    $comNumber = [int]$match.Groups[1].Value
    if ($comNumber -lt 1 -or $comNumber -gt 255) {
        Write-Host ("COM{0} cannot be represented as 127.0.0.<n>; using the default bind." -f $comNumber) -ForegroundColor Yellow
        return $null
    }

    $derivedHost = "127.0.0.$comNumber"
    Write-Host ""
    Write-Host "Select raw TCP address:" -ForegroundColor Cyan
    Write-Host ("  1. {0}:{1}  (default; HTTP API: http://{0}:{2})" -f $TcpHost, $TcpPort, $HttpPort)
    Write-Host ("  2. {0}:{1}  (COM{2}; HTTP API: http://{0}:{3})" -f $derivedHost, $TcpPort, $comNumber, $HttpPort)

    while ($true) {
        $answer = Read-Host "Select 1 or 2"
        switch ($answer.Trim()) {
            '1' { return $null }
            '2' { return $derivedHost }
            default { Write-Host "Enter 1 or 2." -ForegroundColor Yellow }
        }
    }
}

$Port = Resolve-Port -Requested $Port

if ($bareInteractiveStart) {
    $interactiveHost = Select-InteractiveBind -SelectedPort $Port
    if ($null -ne $interactiveHost) {
        # Keep both monitor endpoints together.  This makes the HTTP API's
        # /status endpoint unambiguously refer to the selected UART.
        $HttpHost = $interactiveHost
        $TcpHost = $interactiveHost
        $bindOrigin = 'interactive COM-address selection'
    }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found on PATH. Install Python 3.10+ and try again."
    exit 1
}

$tcpMode = if ($ReadOnly) { 'read-only' } else { 'read/write' }
Write-Host ("Profile '{0}'  ->  HTTP bind {1}  (from: {2})" -f $profileName, $HttpHost, $bindOrigin) -ForegroundColor DarkCyan
Write-Host ("Serial Monitor  ->  http://{0}:{1}   (raw TCP: {2}:{3}, {4})" -f $HttpHost, $HttpPort, $TcpHost, $TcpPort, $tcpMode) -ForegroundColor Cyan
Write-Host ("UART {0} @ {1}   logs: serial_monitor\monitor_logs\" -f $Port, $Baud) -ForegroundColor DarkGray
Write-Host "Press Ctrl-C to stop (graceful)." -ForegroundColor DarkGray

# TCP tail is read/write by default; -ReadOnly opts out.
$inputArg = if ($ReadOnly) { '--no-tcp-allow-input' } else { '--tcp-allow-input' }

# `python -m serial_monitor` must run with the repo root (this folder, the parent of
# the serial_monitor package) as its working directory so the module resolves.
# Scope that to the python process only via Push/Pop: the finally block restores
# the caller's location whether python exits normally or on Ctrl-C (the
# foreground child takes the Ctrl-C, exits, and control returns here), so the
# shell is never left sitting in the repo root.
Push-Location $PSScriptRoot
try {
    # Every setting is passed explicitly, so the python side re-resolves nothing
    # and cannot disagree with the banner printed above.
    python -m serial_monitor --port $Port --baud $Baud --http-host $HttpHost --http-port $HttpPort --tcp-host $TcpHost --tcp-port $TcpPort --name $profileName --no-project-config $inputArg @Extra
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
