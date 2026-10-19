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

      2. Monitor status -- queried over the localhost HTTP API (GET /status).
         When run directly, endpoints are read from running monitor process
         command lines. This finds COM-derived and old fixed-bind monitors, and
         keeps dead/re-enumerated COM handles visible during migration without
         waiting on one HTTP timeout per unused COM port. An explicit -HttpHost
         or -HttpPort keeps a focused single-bind view.

    Run directly to see both. When called from another script it returns the
    port PSCustomObjects (Index, Port, Description, Manufacturer, Vid,
    HeldByMonitor); pass -Quiet to suppress the human-readable output.

.EXAMPLE
    .\list-serial-monitor.ps1
    Show the COM port list and every running monitor found on the derived binds.

.EXAMPLE
    $ports = .\list-serial-monitor.ps1 -Quiet
    Get the port objects without printing (used by start-serial-monitor.ps1).

    .EXAMPLE
    $inventory = .\list-serial-monitor.ps1 -Json | ConvertFrom-Json
    Get a stable machine-readable inventory. Consumers must select exactly one
    monitor whose profile matches the board they intend to drive.
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
    [switch]$Quiet,

    # Emit one JSON document containing the responding monitors and current COM
    # inventory. This is the supported automation interface; unlike the human
    # table, its property names are stable. Implies no human-readable output.
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

# --- probe running monitors (localhost HTTP API; never opens a COM port) ------
function Get-MonitorStatus {
    param(
        [string]$Address,
        [int]$Port,
        [string]$ConfiguredProfile
    )
    $uri = "http://$Address`:$Port/status"
    try {
        $status = Invoke-RestMethod -Uri $uri -TimeoutSec 2 -ErrorAction Stop
        return [pscustomobject]@{
            HttpHost = $Address
            HttpPort = $Port
            ConfiguredProfile = $ConfiguredProfile
            Status = $status
        }
    }
    catch {
        return $null
    }
}

function Get-RunningMonitorEndpoints {
    # The stop helper already treats the command line as the authority for a
    # running monitor. Reuse the same narrow process signature here: it finds
    # legacy fixed-bind processes as well as COM-derived ones, without probing
    # every address in 127/8.
    $moduleRun = '(?:^|\s)-m\s+serial_monitor(?:\s|$)'
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $moduleRun -and $_.Name -match 'python' })
    foreach ($process in $processes) {
        $cmd = [string]$process.CommandLine
        $httpAddress = $null
        if ($cmd -match '--http-host\s+(\S+)') {
            $httpAddress = $Matches[1].Trim('"')
        } elseif ($cmd -match '--port\s+"?COM(\d+)"?(?:\s|$)') {
            # A direct `python -m serial_monitor --port COM<n>` launch has no
            # explicit host in its process command line; the Python CLI derives
            # the same address after parsing. Mirror only that deterministic rule.
            $comNumber = [int]$Matches[1]
            if ($comNumber -ge 1 -and $comNumber -le 254) {
                $httpAddress = "127.0.0.$comNumber"
            }
        }
        if ([string]::IsNullOrWhiteSpace($httpAddress)) { continue }
        $port = 8080
        if ($cmd -match '--http-port\s+(\d+)') { $port = [int]$Matches[1] }
        $name = $null
        if ($cmd -match '--name\s+(\S+)') { $name = $Matches[1].Trim('"') }
        [pscustomobject]@{ Profile = $name; HttpHost = $httpAddress; HttpPort = $port }
    }
}

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

# --- probe running monitors (localhost HTTP API; never opens a COM port) ------
$scanAllMonitors = (-not $PSBoundParameters.ContainsKey('HttpHost')) -and
                   (-not $PSBoundParameters.ContainsKey('HttpPort'))
if ($scanAllMonitors) {
    $monitorEndpoints = @(Get-RunningMonitorEndpoints | Sort-Object HttpHost, HttpPort -Unique)
} else {
    $monitorEndpoints = @([pscustomobject]@{ Profile = $null; HttpHost = $HttpHost; HttpPort = $HttpPort })
}

$monitors = @(
    foreach ($endpoint in $monitorEndpoints) {
        $result = Get-MonitorStatus -Address $endpoint.HttpHost -Port $endpoint.HttpPort -ConfiguredProfile $endpoint.Profile
        if ($null -ne $result) { $result }
    }
)

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
  selected COM port decides the bind address. The profile names the board but
  deliberately does not decide the COM port, because numbers move on re-plug.
#>
<#
  This table is knowledge, not a filter: every COM port is listed whether or not
  its VID appears here. A VID that is missing from the table is called out as
  UNKNOWN in the list rather than printed bare, because a bare "VID_1234" line
  reads like "nothing to see here" and hides the one port the caller is hunting
  for. Add the row when you identify a device; do not add a recommendation.
#>
$VidLabels = @{
    '03EB' = 'Atmel/Microchip nEDBG - Curiosity Nano CDC'
    '04D8' = 'Microchip PKOB4 / MCP serial'
    '0403' = 'FTDI USB-serial bridge'
    '10C4' = 'Silicon Labs CP210x USB-serial bridge'
}

if (-not [string]::IsNullOrWhiteSpace($Vid)) {
    $wantVid = $Vid.ToUpper()
    $ports = @($ports | Where-Object { $_.Vid -eq $wantVid })
}

$heldByPort = @{}
foreach ($runningMonitor in $monitors) {
    $heldPort = [string]$runningMonitor.Status.port
    if (-not [string]::IsNullOrWhiteSpace($heldPort)) {
        $heldByPort[$heldPort] = $runningMonitor
    }
}

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
$missingPortMonitors = @(
    $monitors | Where-Object {
        $heldPort = [string]$_.Status.port
        (-not [string]::IsNullOrWhiteSpace($heldPort)) -and
        (-not ($allPorts | Where-Object { $_.Port -eq $heldPort }))
    }
)

# Present, but hidden from this view by -Vid. Worth one line: otherwise the list
# has no "<== monitor" mark and the reader is left wondering.
$filteredPortMonitors = @(
    $monitors | Where-Object {
        $heldPort = [string]$_.Status.port
        (-not [string]::IsNullOrWhiteSpace($heldPort)) -and
        (-not ($missingPortMonitors -contains $_)) -and
        (-not ($ports | Where-Object { $_.Port -eq $heldPort }))
    }
)

$i = 0
foreach ($p in $ports) {
    $i++
    Add-Member -InputObject $p -NotePropertyName Index -NotePropertyValue $i -Force
    Add-Member -InputObject $p -NotePropertyName HeldByMonitor -NotePropertyValue $heldByPort.ContainsKey($p.Port) -Force
}

if (-not $Quiet -and -not $Json) {
    # --- monitor status block ---
    Write-Host ""
    if ($monitors.Count -gt 0) {
        $heading = if ($scanAllMonitors) { 'Serial Monitors: RUNNING' } else { 'Serial Monitor: RUNNING' }
        Write-Host $heading -ForegroundColor Green
        foreach ($runningMonitor in $monitors) {
            $status = $runningMonitor.Status
            $profileLabel = if (-not [string]::IsNullOrWhiteSpace([string]$status.profile)) {
                [string]$status.profile
            } elseif (-not [string]::IsNullOrWhiteSpace([string]$runningMonitor.ConfiguredProfile)) {
                [string]$runningMonitor.ConfiguredProfile
            } else {
                'unknown profile'
            }
            $heldPort = [string]$status.port
            $heldPortMissing = $missingPortMonitors -contains $runningMonitor
            if ($heldPortMissing) {
                $conn = 'STALE / PORT MISSING'
                $connColor = 'Red'
            } elseif ($status.connected) {
                $conn = 'CONNECTED'
                $connColor = 'Green'
            } else {
                $conn = 'not connected'
                $connColor = 'Yellow'
            }
            Write-Host ("  {0}  (http://{1}:{2})" -f $profileLabel, $runningMonitor.HttpHost, $runningMonitor.HttpPort) -ForegroundColor DarkGray
            Write-Host ("    UART {0} @ {1}  [{2}]" -f $heldPort, $status.baud, $conn) -ForegroundColor $connColor
            if ($status.board -and $status.board.serial) {
                Write-Host ("    board: {0} {1}" -f $status.board.tool, $status.board.serial) -ForegroundColor DarkGray
            }
            if ($heldPortMissing) {
                Write-Host ("    {0} is NOT in the current PnP port list -- the monitor is holding a dead handle." -f $heldPort) -ForegroundColor Red
                Write-Host "    The board most likely re-enumerated to a different COM number (USB reconnect)." -ForegroundColor Red
                Write-Host "    /status will keep saying connected while delivering garbage or nothing at all." -ForegroundColor Red
                Write-Host "    Restart it on the right port: .\stop-serial-monitor.ps1 then .\start-serial-monitor.ps1 -Port <COMn>" -ForegroundColor Red
            }
            if ($filteredPortMonitors -contains $runningMonitor) {
                Write-Host ("    {0} exists but is hidden from the list below by -Vid {1} -- your filter, not a fault." -f $heldPort, $Vid.ToUpper()) -ForegroundColor DarkGray
            }
            if ($status.log_file) {
                Write-Host ("    log: {0}" -f $status.log_file) -ForegroundColor DarkGray
            }
        }
    }
    else {
        if ($scanAllMonitors) {
            Write-Host 'Serial Monitors: not running (no running monitor process endpoint responded)' -ForegroundColor DarkGray
        } else {
            Write-Host ("Serial Monitor: not running (no response on http://{0}:{1})" -f $HttpHost, $HttpPort) -ForegroundColor DarkGray
        }
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
                $owner = $heldByPort[$p.Port]
                $ownerProfile = if ($owner.Status.profile) { [string]$owner.Status.profile } elseif ($owner.ConfiguredProfile) { [string]$owner.ConfiguredProfile } else { 'unknown profile' }
                if ($owner.Status.tcp -and $owner.Status.tcp.host -and $owner.Status.tcp.port) {
                    "   <== monitor {0} {1}:{2}" -f $ownerProfile, $owner.Status.tcp.host, $owner.Status.tcp.port
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
            $vidText = if ($p.Vid) { "VID_$($p.Vid)" } else { 'VID unreadable (not a USB device ID)' }
            # An unrecognised device is still a device. Say so out loud: silence on
            # this line is indistinguishable from "this port does not matter", and
            # that is exactly the port a bring-up is looking for.
            $vidNote = if (-not $p.Vid) {
                ' = [UNKNOWN - no VID in the PnP device ID; on-board / non-USB port?]'
            } elseif ($VidLabels.ContainsKey($p.Vid)) {
                " = $($VidLabels[$p.Vid])"
            } else {
                ' = [UNKNOWN VID - not in this script''s device table; add it]'
            }
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
        Write-Host "This list is what Windows currently enumerates. A board that is unplugged," -ForegroundColor DarkGray
        Write-Host "unpowered, or whose debugger has dropped off the USB bus is ABSENT, not UNKNOWN." -ForegroundColor DarkGray
    }
    Write-Host ""
}

# Automation consumes the status that each endpoint actually returned, not a
# profile guessed from its process command line. ConfiguredProfile is retained
# as diagnostic evidence for old/custom launchers whose /status omitted it.
if ($Json) {
    $monitorData = @(
        foreach ($runningMonitor in $monitors) {
            $status = $runningMonitor.Status
            $tcpHost = $null
            $tcpPort = $null
            if ($status.tcp) {
                $tcpHost = [string]$status.tcp.host
                if ($null -ne $status.tcp.port) { $tcpPort = [int]$status.tcp.port }
            }
            [ordered]@{
                profile            = [string]$status.profile
                board_serial       = if ($status.board) { [string]$status.board.serial } else { $null }
                configured_profile = [string]$runningMonitor.ConfiguredProfile
                port               = [string]$status.port
                baud               = if ($null -ne $status.baud) { [int]$status.baud } else { $null }
                connected          = [bool]$status.connected
                http_host          = [string]$runningMonitor.HttpHost
                http_port          = [int]$runningMonitor.HttpPort
                http_url           = "http://$($runningMonitor.HttpHost):$($runningMonitor.HttpPort)"
                tcp_host           = $tcpHost
                tcp_port           = $tcpPort
                log_file           = [string]$status.log_file
            }
        }
    )
    $portData = @(
        foreach ($p in $ports) {
            [ordered]@{
                index           = [int]$p.Index
                port            = [string]$p.Port
                description     = [string]$p.Description
                manufacturer    = [string]$p.Manufacturer
                device_id       = [string]$p.DeviceId
                vid             = [string]$p.Vid
                pid             = [string]$p.Pid
                held_by_monitor = [bool]$p.HeldByMonitor
            }
        }
    )
    [ordered]@{
        schema_version = 1
        monitors       = $monitorData
        ports          = $portData
    } | ConvertTo-Json -Depth 5
    return
}

# In -Quiet (data) mode, emit the objects for pipeline / caller consumption
# (start-serial-monitor.ps1). In human mode we already printed the table above and
# suppress the raw objects to keep the console clean.
if ($Quiet) {
    $ports
}
