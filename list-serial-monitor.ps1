#!/usr/bin/env pwsh
<#
.SYNOPSIS
    List serial (COM) ports and show the running serial monitor's status.

.DESCRIPTION
    Two read-only views in one shot:

      1. COM ports  -- enumerated via CIM/PnP (device metadata only; this does
         NOT open any port, so it is safe to run while the monitor holds one).
         Each port's USB vendor ID is shown so you can tell the boards apart.
         Nothing is marked as recommended: see the note by $VidLabels.

      2. Monitor status -- queried over the localhost HTTP API (GET /status on
         -HttpPort). Shows whether the serial monitor bridge is running, which
         port/baud it currently holds, and whether the UART is connected. If it
         is running, the COM port it owns is flagged "<== monitor" in the list.

    Run directly to see both. When called from another script it returns the
    port PSCustomObjects (Index, Port, Description, Manufacturer, Vid,
    HeldByMonitor); pass -Quiet to suppress the human-readable output.

.EXAMPLE
    .\list-serial-monitor.ps1
    Show the COM port list and the current monitor status.

.EXAMPLE
    $ports = .\list-serial-monitor.ps1 -Quiet
    Get the port objects without printing (used by start-serial-monitor.ps1).
#>
[CmdletBinding()]
param(
    # Localhost HTTP host/port the serial monitor API listens on.
    [string]$HttpHost = '127.0.0.1',
    [int]$HttpPort = 8080,

    # Show only ports with this USB vendor ID, e.g. '03EB' for a Curiosity Nano
    # or '04D8' for a Microchip PKOB4. A filter you asked for, not a guess this
    # script makes: without it, every port is listed.
    [string]$Vid,

    # Return the port objects only; do not print the human-readable output.
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# --- probe the running monitor (localhost HTTP API; never opens a COM port) ---
function Get-MonitorStatus {
    param([int]$Port)
    $uri = "http://$HttpHost`:$Port/status"
    try {
        return Invoke-RestMethod -Uri $uri -TimeoutSec 2 -ErrorAction Stop
    }
    catch {
        return $null
    }
}

$monitor = Get-MonitorStatus -Port $HttpPort

# --- enumerate COM ports from PnP (metadata only; does not open the port) ---
$entities = @(
    Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\(COM\d+\)' }
)

$ports = foreach ($e in $entities) {
    if ($e.Name -notmatch '\((COM\d+)\)') { continue }
    $portName = $Matches[1]
    $devId = [string]$e.PNPDeviceID
    # Names matter here. PowerShell variable names are CASE-INSENSITIVE, so a
    # loop-local $vid would be the very same variable as the -Vid parameter and
    # would silently overwrite it on every iteration (leaving -Vid holding the
    # last port's VID, or $null). And $pid is a readonly automatic variable.
    $usbVid = if ($devId -match 'VID_([0-9A-Fa-f]{4})') { $Matches[1].ToUpper() } else { $null }
    $usbPid = if ($devId -match 'PID_([0-9A-Fa-f]{4})') { $Matches[1].ToUpper() } else { $null }

    [pscustomobject]@{
        Port         = $portName
        Num          = [int]($portName -replace 'COM', '')
        Description  = ($e.Name -replace '\s*\(COM\d+\)\s*', '').Trim()
        Manufacturer = ($e.Manufacturer)
        DeviceId     = $devId
        Vid          = $usbVid
        Pid          = $usbPid
    }
}

$ports = @($ports | Sort-Object Num)

# Keep the unfiltered list: -Vid narrows what is DISPLAYED, and must not change
# what is true about the machine. Mixing the two made the monitor's own port look
# gone whenever the filter excluded it (see $heldPortMissing below).
$allPorts = $ports

<#
  This script does NOT recommend a port. It labels each one by USB vendor ID and
  stops there.

    VID_03EB -- Atmel/Microchip nEDBG: the Curiosity Nano's own CDC console
                (its drag-and-drop mass-storage drive is the same VID)
    VID_04D8 -- Microchip PKOB4 / MCP USB serial (a different debugger board)

  Two earlier attempts at recommending one are why. The first ("Microchip in the
  Manufacturer string") picked the *other* board's PKOB4 and cost a session
  chasing a dead port. The second (VID preference order, 03EB before 04D8) is
  worse in a subtler way: this script lives in a shared repo and knows nothing
  about the board the caller means, so standing in one board's repo it happily
  starred another board's port -- a confident mark pointing at the wrong hardware
  (measured 2026-08-09). A mark that is right by coincidence is indistinguishable
  from one that is right by reason, so there is no mark.

  Which board you want is stated by -Port (or -Vid to narrow the list). The
  profile decides the bind address; it deliberately does not decide the COM port,
  because port numbers move when a board is re-plugged.
#>
$VidLabels = @{ '03EB' = 'Curiosity Nano (nEDBG CDC)'; '04D8' = 'Microchip PKOB4 / MCP serial' }

if (-not [string]::IsNullOrWhiteSpace($Vid)) {
    $wantVid = $Vid.ToUpper()
    $ports = @($ports | Where-Object { $_.Vid -eq $wantVid })
}

$heldPort = if ($null -ne $monitor) { [string]$monitor.port } else { $null }

<#
  A monitor holding a port that no longer exists in the PnP list is the failure
  mode that cost a whole session: the board re-enumerated to a new COM number
  after a USB reconnect, the bridge kept its old handle, and /status still said
  connected while delivering bit-shifted garbage and then silence -- which reads
  exactly like hung firmware. Surface it loudly instead.

  Judged against every port on the machine ($allPorts), never against the
  -Vid-filtered view. Asking the filtered list turned `-Vid 03EB` while a monitor
  held a 04D8 port into a confident "dead handle, restart it" -- a wrong diagnosis
  produced by the caller's own display filter, which is the one thing this repo
  refuses to do (a mark that is right by coincidence is indistinguishable from one
  that is right by reason).
#>
$heldPortMissing = ($null -ne $heldPort) -and
                   ($heldPort -ne '') -and
                   (-not ($allPorts | Where-Object { $_.Port -eq $heldPort }))

# Present, but hidden from this view by -Vid. Worth one line: otherwise the list
# has no "<== monitor" mark and the reader is left wondering.
$heldPortFiltered = ($null -ne $heldPort) -and
                    ($heldPort -ne '') -and
                    (-not $heldPortMissing) -and
                    (-not ($ports | Where-Object { $_.Port -eq $heldPort }))

$i = 0
foreach ($p in $ports) {
    $i++
    Add-Member -InputObject $p -NotePropertyName Index -NotePropertyValue $i -Force
    Add-Member -InputObject $p -NotePropertyName HeldByMonitor -NotePropertyValue ($p.Port -eq $heldPort) -Force
}

if (-not $Quiet) {
    # --- monitor status block ---
    Write-Host ""
    if ($null -ne $monitor) {
        if ($heldPortMissing) {
            $conn = 'STALE / PORT MISSING'
            $connColor = 'Red'
        } elseif ($monitor.connected) {
            $conn = 'CONNECTED'
            $connColor = 'Green'
        } else {
            $conn = 'not connected'
            $connColor = 'Yellow'
        }
        Write-Host "Serial Monitor: RUNNING" -ForegroundColor Green -NoNewline
        Write-Host ("  (http://{0}:{1})" -f $HttpHost, $HttpPort) -ForegroundColor DarkGray
        Write-Host ("  UART {0} @ {1}  [{2}]" -f $monitor.port, $monitor.baud, $conn) -ForegroundColor $connColor
        if ($heldPortMissing) {
            Write-Host ("  {0} is NOT in the current PnP port list -- the monitor is holding a dead handle." -f $monitor.port) -ForegroundColor Red
            Write-Host "  The board most likely re-enumerated to a different COM number (USB reconnect)." -ForegroundColor Red
            Write-Host "  /status will keep saying connected while delivering garbage or nothing at all." -ForegroundColor Red
            Write-Host "  Restart it on the right port: .\stop-serial-monitor.ps1 then .\start-serial-monitor.ps1 -Port <COMn>" -ForegroundColor Red
        }
        if ($heldPortFiltered) {
            Write-Host ("  {0} exists but is hidden from the list below by -Vid {1} -- your filter, not a fault." -f $monitor.port, $Vid.ToUpper()) -ForegroundColor DarkGray
        }
        if ($monitor.log_file) {
            Write-Host ("  log: {0}" -f $monitor.log_file) -ForegroundColor DarkGray
        }
    }
    else {
        Write-Host ("Serial Monitor: not running (no response on http://{0}:{1})" -f $HttpHost, $HttpPort) -ForegroundColor DarkGray
    }

    # --- COM port list ---
    Write-Host ""
    if ($ports.Count -eq 0) {
        Write-Host "No serial (COM) ports found." -ForegroundColor Yellow
    }
    else {
        Write-Host "Available serial (COM) ports:" -ForegroundColor Cyan
        foreach ($p in $ports) {
            $mfg = if ([string]::IsNullOrWhiteSpace($p.Manufacturer)) { '' } else { "  [$($p.Manufacturer)]" }
            $held = if ($p.HeldByMonitor) {
                if ($monitor.tcp -and $monitor.tcp.host -and $monitor.tcp.port) {
                    "   <== monitor {0}:{1}" -f $monitor.tcp.host, $monitor.tcp.port
                } else {
                    # HeldByMonitor is true here, so the monitor genuinely holds this
                    # port -- the missing piece is the TCP tail address, not the
                    # monitor itself (tcp_port=0 disables that service deliberately).
                    '   <== monitor (no tcp)'
                }
            } else { '' }
            Write-Host ("  {0}) {1,-6} {2}{3}{4}" -f $p.Index, $p.Port, $p.Description, $mfg, $held) -ForegroundColor Gray
            # The VID is the only reliable way to tell these boards apart -- show it
            # on its own line rather than making the reader trust the display name.
            $vidText = if ($p.Vid) { "VID_$($p.Vid)" } else { 'VID unknown' }
            $vidNote = if ($p.Vid -and $VidLabels.ContainsKey($p.Vid)) { " = $($VidLabels[$p.Vid])" } else { '' }
            # The PID is printed as well as the VID, and for a reason: a board can
            # expose two CDC ports that share one VID, in which case only the PID
            # separates them -- and one of the two may carry a mirror of the other's
            # output rather than the same UART. Which PID means what is a fact about
            # your board; this script prints the pair and interprets neither.
            $pidText = if ($p.Pid) { "  PID_$($p.Pid)" } else { '' }
            Write-Host ("      {0}{1}{2}" -f $vidText, $pidText, $vidNote) -ForegroundColor DarkGray
        }
        Write-Host "No port is recommended: this script cannot know which board you mean." -ForegroundColor DarkGray
        Write-Host "Choose by number, or pass -Port COM<n>. VID_03EB = Curiosity Nano, VID_04D8 = PKOB4." -ForegroundColor DarkGray
        Write-Host "If one VID appears twice, the two ports are not interchangeable: check the PID." -ForegroundColor DarkGray
    }
    Write-Host ""
}

# In -Quiet (data) mode, emit the objects for pipeline / caller consumption
# (start-serial-monitor.ps1). In human mode we already printed the table above and
# suppress the raw objects to keep the console clean.
if ($Quiet) {
    $ports
}
