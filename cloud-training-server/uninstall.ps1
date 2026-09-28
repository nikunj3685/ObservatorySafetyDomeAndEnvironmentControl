#
# Cloud Training Server - Windows uninstaller
# ---------------------------------------------
# Reverses everything install.ps1 set up: stops and removes the
# scheduled task, removes the firewall rule, and (only if you pass
# -RemoveData) deletes the virtual environment, downloaded training
# jobs, and server_config.json (which holds the API key - removing it
# means a future install.ps1 run generates a brand new key, and you'd
# need to update the Pi's Settings with it).
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData

param(
    [switch]$RemoveData
)

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host "Re-launching with administrator privileges..."
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($RemoveData) { $args += "-RemoveData" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $args
    exit
}

$here = $PSScriptRoot
$taskName = "CloudTrainingServer"
$ruleName = "Cloud Training Server"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'."
} else {
    Write-Host "Scheduled task '$taskName' was not present."
}

if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $ruleName
    Write-Host "Removed firewall rule '$ruleName'."
} else {
    Write-Host "Firewall rule '$ruleName' was not present."
}

if ($RemoveData) {
    foreach ($item in @("venv", "jobs", "server_config.json", "train_server.log")) {
        $path = Join-Path $here $item
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force
            Write-Host "Deleted $item"
        }
    }
    Write-Host ""
    Write-Host "All local data removed, including the API key. Re-running install.ps1"
    Write-Host "later will generate a NEW key - you'll need to update the Pi's Settings."
} else {
    Write-Host ""
    Write-Host "The scheduled task and firewall rule are gone; the virtual environment,"
    Write-Host "server_config.json (your API key), and any trained models in jobs/ were"
    Write-Host "left in place. Re-run install.ps1 any time to bring it back exactly as"
    Write-Host "it was. Pass -RemoveData to this script instead if you want those gone too."
}
