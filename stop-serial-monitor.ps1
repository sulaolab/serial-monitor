#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Stop (force-kill) a running serial monitor. Companion to start-serial-monitor.ps1.

.DESCRIPTION
    Finds the python process(es) running `python -m serial_monitor` and terminates
    them, freeing the COM port and the HTTP / TCP ports. Only that module counts:
    a process that merely mentions serial_monitor (a `python -m pytest
    serial_monitor/tests` run, for instance) is not a monitor and is left alone.

    Prefer Ctrl-C in the monitor's own foreground terminal when you have one --
    that is a *graceful* shutdown (closes the COM port, flushes logs). Use this
    script for a monitor running in the background or another window, or when a
    graceful stop isn't possible.

    SCOPE: this tool is installed ONCE and serves several boards at the same time,
    each in its own process, often each driven by a different session. So a stop
    that matches more than one instance REFUSES to act and lists what it found --
    killing "all of them" would take out someone else's board mid-command. That is
    not hypothetical: it happened on 2026-08-09.

    Say which one you mean with -Profile / -HttpHost / -Port, or -All when you
    really do want every instance gone.

    Note that -HttpPort alone usually does NOT narrow anything: instances are
    separated by loopback alias (127.0.0.1 vs 127.0.0.5), and they all use 8080.

.PARAMETER Profile
    Stop the instance started for this board family (matches the
    --name the launcher passed).

.PARAMETER HttpHost
    Stop the instance bound to this HTTP host, e.g. '127.0.0.5'.

.PARAMETER Port
    Stop the instance that owns this COM port, e.g. 'COM60'.

.PARAMETER HttpPort
    Stop the instance whose command line uses this --http-port. Rarely useful on
    its own -- see the note above.

.PARAMETER All
    Stop every serial_monitor instance found, without the ambiguity check.

.PARAMETER Quiet
    Suppress the per-process details; just report how many were stopped.

.EXAMPLE
    .\stop-serial-monitor.ps1 -Profile ck
    Stop the monitor serving the CK board and nothing else.

.EXAMPLE
    .\stop-serial-monitor.ps1
    With one instance running, stops it. With several, lists them and stops
    nothing.

.EXAMPLE
    .\stop-serial-monitor.ps1 -All
    Stop every instance (only do this when no one else is on a board).
#>
[CmdletBinding()]
param(
    [string]$Profile = '',
    [string]$HttpHost = '',
    [string]$Port = '',
    [int]$HttpPort = 0,
    [switch]$All,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# Match python processes that are RUNNING THE MODULE -- `-m serial_monitor` -- and
# not merely mentioning it.
#
# Matching the bare string 'serial_monitor' is too wide by exactly the amount that
# hurts: `python -m pytest serial_monitor/tests` matches it, so a stop issued while
# only the test suite was running would Stop-Process -Force the tests. So would a
# `python -m serial_monitor.config` resolver call, which is a short-lived child of
# the launcher and never the monitor. The pattern below excludes both: '.config'
# follows without whitespace, and pytest's -m names pytest.
#
# (The Name guard keeps an editor or agent that has the path in its command line
# out; this script itself is 'pwsh' running stop-serial-monitor.ps1, so it is never
# matched.)
$moduleRun = '(?:^|\s)-m\s+serial_monitor(?:\s|$)'
$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $moduleRun -and $_.Name -match 'python' })

if ($procs.Count -eq 0) {
    Write-Host "No running serial_monitor found." -ForegroundColor Yellow
    exit 0
}

# --- narrow down --------------------------------------------------------------
$filters = @()
if (-not [string]::IsNullOrWhiteSpace($Profile)) {
    $procs = @($procs | Where-Object { $_.CommandLine -match "--name\s+$([regex]::Escape($Profile))(\s|$)" })
    $filters += "-Profile $Profile"
}
if (-not [string]::IsNullOrWhiteSpace($HttpHost)) {
    $procs = @($procs | Where-Object { $_.CommandLine -match "--http-host\s+$([regex]::Escape($HttpHost))(\s|$)" })
    $filters += "-HttpHost $HttpHost"
}
if (-not [string]::IsNullOrWhiteSpace($Port)) {
    $procs = @($procs | Where-Object { $_.CommandLine -match "--port\s+$([regex]::Escape($Port))(\s|$)" })
    $filters += "-Port $Port"
}
if ($HttpPort -gt 0) {
    $procs = @($procs | Where-Object { $_.CommandLine -match "--http-port\s+$HttpPort(\s|$)" })
    $filters += "-HttpPort $HttpPort"
}

if ($procs.Count -eq 0) {
    Write-Host ("No serial_monitor matches {0}." -f ($filters -join ' ')) -ForegroundColor Yellow
    Write-Host "Run with no filters to see what is running." -ForegroundColor DarkGray
    exit 0
}

# --- refuse to guess when several match --------------------------------------
if ($procs.Count -gt 1 -and -not $All) {
    Write-Host ("{0} monitors match{1} -- stopping nothing." -f $procs.Count,
        $(if ($filters.Count) { " " + ($filters -join ' ') } else { '' })) -ForegroundColor Yellow
    foreach ($p in $procs) {
        $name = if ($p.CommandLine -match '--name\s+(\S+)') { $Matches[1] } else { '?' }
        $com = if ($p.CommandLine -match '--port\s+(\S+)') { $Matches[1] } else { '?' }
        $bind = if ($p.CommandLine -match '--http-host\s+(\S+)') { $Matches[1] } else { '?' }
        $hp = if ($p.CommandLine -match '--http-port\s+(\S+)') { $Matches[1] } else { '?' }
        Write-Host ("  pid {0,-6} profile={1,-8} uart={2,-6} bind={3}:{4}" -f $p.ProcessId, $name, $com, $bind, $hp)
    }
    Write-Host ""
    Write-Host "Another session is probably using one of these. Name the one you mean:" -ForegroundColor DarkGray
    Write-Host "  .\stop-serial-monitor.ps1 -Profile <name>   (or -HttpHost / -Port)" -ForegroundColor DarkGray
    Write-Host "  .\stop-serial-monitor.ps1 -All              (every instance, if you are sure)" -ForegroundColor DarkGray
    exit 1
}

$stopped = 0
foreach ($p in $procs) {
    if (-not $Quiet) {
        Write-Host ("Stopping PID {0}: {1}" -f $p.ProcessId, $p.CommandLine) -ForegroundColor Cyan
    }
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        $stopped++
    }
    catch {
        Write-Host ("  failed to stop PID {0}: {1}" -f $p.ProcessId, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ("Stopped {0} serial_monitor process(es)." -f $stopped) -ForegroundColor Green
Write-Host "Note: a force-stop mid-stream can occasionally leave the board console wedged; if the console goes silent, power-cycle / reset the board." -ForegroundColor DarkGray
exit 0
