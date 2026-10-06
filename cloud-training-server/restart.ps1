#
# Cloud Training Server - restart helper
# --------------------------------------
# If the server is running, stops it and starts it again. If it isn't
# running, just starts it. Works whether install.ps1 registered it as a
# scheduled task (default) or a Windows Service (-Service) - both are
# named "CloudTrainingServer". Use this after replacing train_server.py.
#
#   powershell -ExecutionPolicy Bypass -File restart.ps1

$ErrorActionPreference = "Stop"
$name = "CloudTrainingServer"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Needs Administrator to control the service/task.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host "Re-launching with administrator privileges..."
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    exit
}

$service = Get-Service -Name $name -ErrorAction SilentlyContinue
$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue

if ($service) {
    Write-Host "Found Windows Service '$name' (status: $($service.Status))."
    if ($service.Status -eq "Running") {
        Write-Host "Stopping..."
        Stop-Service -Name $name -Force
        $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
    }
    Write-Host "Starting..."
    Start-Service -Name $name
} elseif ($task) {
    $state = $task.State
    Write-Host "Found scheduled task '$name' (state: $state)."
    if ($state -eq "Running") {
        Write-Host "Stopping..."
        Stop-ScheduledTask -TaskName $name
        Start-Sleep -Seconds 3
    }
    Write-Host "Starting..."
    Start-ScheduledTask -TaskName $name
} else {
    Write-Host "Neither a service nor a scheduled task named '$name' exists." -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

# Self-test: wait for /health to answer, using the port from server_config.json.
$port = 8787
$cfg = Join-Path $here "server_config.json"
if (Test-Path $cfg) {
    try { $port = (Get-Content $cfg -Raw | ConvertFrom-Json).port } catch { }
}
Write-Host "Waiting for http://localhost:$port/health ..."
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod "http://localhost:$port/health" -TimeoutSec 3
        if ($r.ok) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host "Server is up. (Note: any training job still in progress was lost with the restart.)" -ForegroundColor Green
} else {
    Write-Host "Server did not answer on port $port within 60 seconds - check train_server.log in this folder." -ForegroundColor Red
}
Read-Host "Press Enter to close"
