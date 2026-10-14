#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Resolve one running serial-monitor profile to its actual endpoint.

.DESCRIPTION
    Read-only automation helper. It consumes list-serial-monitor.ps1 -Json,
    matches the profile reported by GET /status, and succeeds only when exactly
    one running monitor has that profile. No COM port is opened.

    This deliberately does not derive an endpoint from a profile: profiles name
    boards, while the selected COM number determines the loopback suffix.

.EXAMPLE
    .\resolve-serial-monitor.ps1 -Profile ck-ev97
    http://127.0.0.41:8080

.EXAMPLE
    .\resolve-serial-monitor.ps1 -Profile ak-nano -Json | ConvertFrom-Json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Profile,

    [switch]$Json
)

$ErrorActionPreference = 'Stop'

$inventoryText = @(& (Join-Path $PSScriptRoot 'list-serial-monitor.ps1') -Json)
try {
    $inventory = ($inventoryText -join "`n") | ConvertFrom-Json
} catch {
    throw "Could not parse the serial-monitor inventory JSON ($($_.Exception.Message))."
}
if ([int]$inventory.schema_version -ne 1) {
    throw "Unsupported serial-monitor inventory schema '$($inventory.schema_version)'."
}

$matches = @($inventory.monitors | Where-Object {
    [string]$_.profile -ieq $Profile
})
if ($matches.Count -eq 0) {
    $running = @($inventory.monitors | ForEach-Object {
        "{0} ({1}, {2})" -f $_.profile, $_.port, $_.http_url
    })
    $detail = if ($running.Count) { $running -join '; ' } else { 'none' }
    throw "No running serial-monitor reports profile '$Profile'. Running: $detail"
}
if ($matches.Count -gt 1) {
    $detail = ($matches | ForEach-Object {
        "{0} on {1}" -f $_.http_url, $_.port
    }) -join '; '
    throw "More than one running serial-monitor reports profile '$Profile': $detail. Pass an explicit URL to the consumer; no endpoint was guessed."
}

$match = $matches[0]
if ($Json) {
    [ordered]@{
        schema_version     = 1
        profile            = [string]$match.profile
        port               = [string]$match.port
        connected          = [bool]$match.connected
        http_host          = [string]$match.http_host
        http_port          = [int]$match.http_port
        http_url           = [string]$match.http_url
        tcp_host           = [string]$match.tcp_host
        tcp_port           = if ($null -ne $match.tcp_port) { [int]$match.tcp_port } else { $null }
        log_file           = [string]$match.log_file
        configured_profile = [string]$match.configured_profile
    } | ConvertTo-Json -Depth 3
} else {
    [string]$match.http_url
}
