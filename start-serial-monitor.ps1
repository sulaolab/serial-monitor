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

     The selected COM number decides the bind address: COM60 uses
     127.0.0.60:8080 (HTTP) and 127.0.0.60:23 (raw TCP). A profile names the
     board and supplies baud/default metadata; it no longer assigns a fixed
     loopback alias. Explicit -HttpHost/-TcpHost values remain escape hatches.

    The COM port is never taken from a profile: port numbers move when a board is
    re-plugged, so naming it stays your job every time -- interactively, with
    -Port, or with a -Vid that YOU passed and that leaves exactly one candidate.

.EXAMPLE
    .\start-serial-monitor.ps1 -Help
    Show the canonical board profiles, COM-to-address rule, and examples.

.EXAMPLE
    .\start-serial-monitor.ps1 -List
    Show the COM ports and the current monitor status, then exit.

.EXAMPLE
    .\start-serial-monitor.ps1
    Pick a port interactively. COM31 uses 127.0.0.31 for both HTTP and raw TCP.

.EXAMPLE
    .\start-serial-monitor.ps1 -Port COM12 -Baud 115200
    Use an explicit port and baud.

.EXAMPLE
    .\start-serial-monitor.ps1 -Port 2
    Use the port shown as index 2 in list-serial-monitor.ps1.

.EXAMPLE
    .\start-serial-monitor.ps1 -Profile ak-curio -Port COM17 -Detach
    Open the monitor in its own window and return when it answers (for agents).

.EXAMPLE
    .\start-serial-monitor.ps1 -ReadOnly
    Make the Tera Term TCP tail view-only (default is read/write).
#>
[CmdletBinding()]
param(
    # COM name (COM12), a bare list index (2), or empty to select interactively.
    [string]$Port = '',

    # Board family / baud metadata, e.g. 'ck-ev97'.
    # Any name -Help lists (profiles/ here, or a directory outside the checkout).
    # Usually unnecessary: a .serial-monitor.json in the firmware repo you are
    # standing in already declares it. See docs/binding-and-profiles.md.
    [string]$Profile = '',

    # Ports default from config. Hosts derive from COM unless explicitly passed.
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
    # Print canonical profiles, the COM-derived bind rule, and examples.
    [switch]$Help,
    # By default the Tera Term TCP tail is read/write (you can type commands).
    # Pass -ReadOnly to make it view-only.
    [switch]$ReadOnly,
    # Run the monitor in its own new PowerShell window and return once /status
    # answers. For agents and scripts: their shell call is not held, and the
    # monitor outlives the caller. Closing that window stops it.
    [switch]$Detach,
    # Any extra flags are passed straight through to `python -m serial_monitor`
    # (e.g. --log-dir <path>, --dtr off).
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

$ErrorActionPreference = 'Stop'

# NOTE: we deliberately do NOT `Set-Location $PSScriptRoot` here. That would
# leave the *caller's* shell sitting in the repo root after the monitor exits (the
# current location is a runspace-wide property, so it persists past the script).
# Everything below uses absolute paths ($PSScriptRoot); only the final
# `python -m serial_monitor` needs the repo root as its working directory, and that is
# scoped to the python process alone (Push/Pop) at the bottom of the script.

$listScript = Join-Path $PSScriptRoot 'list-serial-monitor.ps1'

<#
  -Help keeps the board name and COM-selection requirements visible. The address
  no longer needs a memorized allocation table: it follows the selected COM.
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
    Write-Host "    1. the PROFILE      -- which board family this monitor serves"
    Write-Host "    2. the COM PORT     -- its current Windows number; also the bind suffix"
    Write-Host ""
    Write-Host "  The selected COM port determines both endpoints:" -ForegroundColor Cyan
    Write-Host "    COM<n> -> HTTP 127.0.0.<n>:8080 and raw TCP 127.0.0.<n>:23" -ForegroundColor Cyan
    Write-Host ""

    Write-Host "  Profiles (board identity; bind comes from the selected COM):" -ForegroundColor Cyan
    $seen = @{}
    foreach ($dir in $profileDirs) {
        if (-not (Test-Path $dir)) { continue }
        foreach ($f in (Get-ChildItem -Path $dir -Filter *.json | Sort-Object Name)) {
            if ($seen.ContainsKey($f.Name)) { continue }   # a nearer dir already won
            $seen[$f.Name] = $true
            try { $p = Get-Content -Raw -LiteralPath $f.FullName | ConvertFrom-Json } catch { continue }
            $vid = if ($p.vid) { "VID_$($p.vid)" } else { 'any VID' }
            Write-Host ("    -Profile {0,-16} ({1} baud, {2})" -f $p.name, $p.baud, $vid)
        }
    }
    Write-Host "    (searched: $($profileDirs -join ' ; '))" -ForegroundColor DarkGray
    Write-Host "    (no profile)      -> generic label/settings; address still follows COM" -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "  How the profile is chosen, in order:" -ForegroundColor Cyan
    Write-Host "    1. -Profile <name> on the command line"
    Write-Host "    2. a .serial-monitor.json in the directory you are standing in (or above it)"
    Write-Host "    3. nothing -> generic label/settings (still requires an explicit COM choice)" -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "  Examples:" -ForegroundColor Cyan
    Write-Host "    # interactive: selecting COM31 binds both services to 127.0.0.31:"
    Write-Host "    .\start-serial-monitor.ps1" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # EV97 board on COM60 -> 127.0.0.60, from anywhere:"
    Write-Host "    .\start-serial-monitor.ps1 -Profile ck-ev97 -Port COM60" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # AK Curiosity on COM12 -> 127.0.0.12:"
    Write-Host "    .\start-serial-monitor.ps1 -Profile ak-curio -Port COM12" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # from inside a firmware repo, the profile is already declared -- name the port:"
    Write-Host "    pwsh ../serial-monitor/start-serial-monitor.ps1 -Port COM12" -ForegroundColor Green
    Write-Host ""
    Write-Host "    # an address no profile covers (overrides the profile; both halves needed):"
    Write-Host "    .\start-serial-monitor.ps1 -HttpHost 127.0.0.5 -TcpHost 127.0.0.5 -Port COM60" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Check before you drive it:" -ForegroundColor Cyan
    Write-Host "    .\start-serial-monitor.ps1 -List        # resolved bind + ports + is one running"
    Write-Host "    curl http://127.0.0.60:8080/status          # confirm profile+port ARE the board you mean"
    Write-Host "    .\stop-serial-monitor.ps1 -Profile ck-ev97 # never -All: that kills other boards too" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Full details: docs/binding-and-profiles.md   |   Get-Help for every parameter." -ForegroundColor DarkGray
    Write-Host ""
}

if ($Help) {
    Show-BoardHelp
    exit 0
}

# --- resolve the bind/baud/VID -----------------------------------------------
# Board metadata resolution is delegated to `python -m serial_monitor.config`.
# The bind itself is derived below, after the caller has named the COM port.
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
# A profile's `vid` is NOT inherited. Letting a shared config file silently narrow the port list
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
    $listArgs = @{}
    if ($PSBoundParameters.ContainsKey('HttpHost')) { $listArgs.HttpHost = $HttpHost }
    if ($PSBoundParameters.ContainsKey('HttpPort')) { $listArgs.HttpPort = $HttpPort }
    if (-not [string]::IsNullOrWhiteSpace($Vid)) { $listArgs.Vid = $Vid }
    & $listScript @listArgs
    exit 0
}

# --- resolve the port ---
function Resolve-Port {
    param([string]$Requested)

    $listArgs = @{ Quiet = $true }
    if (-not [string]::IsNullOrWhiteSpace($Vid)) { $listArgs['Vid'] = $Vid }
    $ports = @(& $listScript @listArgs)
    if ($ports.Count -eq 0) {
        Write-Error "No serial (COM) ports found. Plug in the board and retry."
        exit 1
    }

    # Nothing here picks a port on its own -- not $ports[0], and no VID
    # preference order either. The COM port is the one thing this tool cannot
    # infer: a mark that guesses "probably this board" reads exactly like one that knows (it starred one
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

function Get-ComDerivedBind {
    param(
        [Parameter(Mandatory)]
        [string]$SelectedPort
    )

    # COM names selected by Resolve-Port are normalized to COM<n>. Restrict the
    # derived address to one ordinary host octet; explicit hosts remain available
    # for unusual names and COM numbers outside this range.
    $match = [regex]::Match($SelectedPort, '^COM(\d+)$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) {
        throw "Port '$SelectedPort' cannot derive a loopback address. Pass -HttpHost and -TcpHost explicitly."
    }
    $comNumber = [int]$match.Groups[1].Value
    if ($comNumber -lt 1 -or $comNumber -gt 254) {
        throw "COM$comNumber cannot be represented safely as 127.0.0.<n>. Pass -HttpHost and -TcpHost explicitly."
    }
    return "127.0.0.$comNumber"
}

$Port = Resolve-Port -Requested $Port
$needsDerivedHttp = -not $PSBoundParameters.ContainsKey('HttpHost')
$needsDerivedTcp = -not $PSBoundParameters.ContainsKey('TcpHost')
if ($needsDerivedHttp -or $needsDerivedTcp) {
    $derivedHost = Get-ComDerivedBind -SelectedPort $Port
    if ($needsDerivedHttp) { $HttpHost = $derivedHost }
    if ($needsDerivedTcp) { $TcpHost = $derivedHost }
    if ($needsDerivedHttp) { $bindOrigin = "derived from $Port" }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found on PATH. Install Python 3.10+ and try again."
    exit 1
}

if ($Detach) {
    # Hand the monitor to its own PowerShell window -- the same window a human
    # would open by hand -- and return once it answers. Everything is already
    # resolved, so the child gets explicit values and prompts for nothing.
    $base = "http://{0}:{1}" -f $HttpHost, $HttpPort
    try {
        $st = Invoke-RestMethod -Uri "$base/status" -TimeoutSec 2 -ErrorAction Stop
        Write-Host ("Already running: {0}  profile={1} port={2} connected={3}" -f $base, $st.profile, $st.port, $st.connected) -ForegroundColor Yellow
        exit 0
    } catch { }
    $childArgs = @("-Port '$Port'", "-Profile '$profileName'", "-Baud $Baud",
        "-HttpHost '$HttpHost'", "-HttpPort $HttpPort", "-TcpHost '$TcpHost'", "-TcpPort $TcpPort")
    if ($ReadOnly) { $childArgs += '-ReadOnly' }
    foreach ($x in @($Extra)) { if ($x) { $childArgs += "'$($x -replace "'", "''")'" } }
    $title = "serial-monitor $profileName $Port"
    # The window closes with the monitor (stop-serial-monitor.ps1 included),
    # except when it dies within 15 s of starting: then it waits so the startup
    # error can be read instead of vanishing.
    $cmd = "`$Host.UI.RawUI.WindowTitle = '$title'; `$t0 = Get-Date; & '$PSCommandPath' $($childArgs -join ' '); " +
        "if (`$LASTEXITCODE -and ((Get-Date) - `$t0).TotalSeconds -lt 15) { Read-Host ('monitor failed to start (exit ' + `$LASTEXITCODE + ') -- Enter closes this window') }"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    $proc = Start-Process -FilePath 'pwsh' -ArgumentList @('-NoLogo', '-EncodedCommand', $enc) -PassThru
    Write-Host ("Started in its own window '{0}' (pid {1}). Close it or Ctrl-C there to stop." -f $title, $proc.Id) -ForegroundColor Cyan
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $st = Invoke-RestMethod -Uri "$base/status" -TimeoutSec 2 -ErrorAction Stop
            Write-Host ("Ready: {0}  profile={1} port={2} connected={3}" -f $base, $st.profile, $st.port, $st.connected) -ForegroundColor Green
            exit 0
        } catch { }
    }
    Write-Error "No answer from $base/status after 15 s -- read the error in window '$title'."
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
