#
# Cloud Training Server - Windows installer
# ------------------------------------------
# Sets up this folder's train_server.py to run automatically on this
# Windows machine: installs Python (if needed) and its dependencies into
# a private virtual environment, generates an API key, opens a firewall
# rule so the Pi can reach it on the LAN, and registers something to
# start it automatically and restart it if it ever crashes - either a
# scheduled task (default) or a true Windows Service via NSSM (-Service).
#
# Run it by right-clicking this file -> "Run with PowerShell", or from
# an existing PowerShell window:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Service
#
# Safe to re-run any time (e.g. after moving the folder, or to recover
# from a problem) - it reuses the existing API key instead of generating
# a new one each time, and cleanly replaces whichever autostart mechanism
# it previously set up (even if you switch from one mode to the other,
# the old one is removed first, so only ONE ever runs on this machine).

param(
    [switch]$Service
)

# ---------------------------------------------------------------------
# 0. Self-elevate to Administrator - the firewall rule and scheduled
#    task/service below both require it.
# ---------------------------------------------------------------------

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host "Re-launching with administrator privileges..."
    $relaunchArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Service) { $relaunchArgs += "-Service" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Write-Host "Installing into: $here"

# Any error path below calls this instead of a bare "exit 1", so a double-
# clicked window still shows the message instead of vanishing instantly.
function Exit-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message
    Write-Host ""
    Read-Host "Something went wrong - press Enter to close this window"
    exit 1
}

# ---------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------

function Find-Python {
    Get-Command python -ErrorAction SilentlyContinue
}

$python = Find-Python
if (-not $python) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Python not found - installing via winget..."
        winget install --id Python.Python.3.12 -e --silent `
            --accept-package-agreements --accept-source-agreements
        # winget just changed PATH on disk, but this process's own copy
        # of $env:Path won't know that yet - reload it from the registry
        # instead of asking the user to reopen PowerShell and re-run.
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $python = Find-Python
    }
    if (-not $python) {
        Exit-WithError (
            "ERROR: Python was not found and could not be installed automatically.`n" +
            "Install it yourself from https://python.org (check 'Add python.exe to PATH'`n" +
            "during setup), then run this script again."
        )
    }
}
Write-Host "Using Python: $($python.Source)"

# ---------------------------------------------------------------------
# 2. Virtual environment + dependencies
# ---------------------------------------------------------------------

$venvDir = Join-Path $here "venv"
if (-not (Test-Path $venvDir)) {
    Write-Host "Creating virtual environment..."
    & python -m venv $venvDir
}
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Exit-WithError "ERROR: virtual environment creation failed - $venvPython not found."
}

Write-Host "Installing dependencies (TensorFlow is a big download - this can take"
Write-Host "several minutes on the first run)..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Exit-WithError "ERROR: pip install failed - see the output above for details."
}

# ---------------------------------------------------------------------
# 3. API key + port (server_config.json) - generated once, reused on
#    every re-run so you don't have to update the Pi's Settings again
#    each time you re-install.
# ---------------------------------------------------------------------

$configPath = Join-Path $here "server_config.json"
if (Test-Path $configPath) {
    $existing = Get-Content $configPath -Raw | ConvertFrom-Json
    $apiKey = $existing.api_key
    $port = $existing.port
    Write-Host "Reusing existing API key and port ($port) from server_config.json."
} else {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $apiKey = ([Convert]::ToBase64String($bytes) -replace '[^a-zA-Z0-9]', '').Substring(0, 32)
    $port = 8787
    # Written via .NET directly (not Set-Content -Encoding UTF8) because
    # Windows PowerShell's UTF8 encoding always prepends a byte-order-mark,
    # which breaks Python's json.load with "Expecting value: line 1 column 1".
    $json = @{ api_key = $apiKey; port = $port } | ConvertTo-Json
    [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Generated a new API key and saved it to server_config.json."
}

# ---------------------------------------------------------------------
# 4. Firewall rule - LAN-reachable only; nothing here opens this port
#    to the public internet unless your router is separately configured
#    to forward it, which this script does not do and you should not
#    do either. Use Tailscale (see the summary at the end) instead of
#    port-forwarding if you need to reach this from outside your LAN.
# ---------------------------------------------------------------------

$ruleName = "Cloud Training Server"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
        -Protocol TCP -LocalPort $port -Action Allow | Out-Null
    Write-Host "Firewall rule added for inbound TCP port $port."
} else {
    Write-Host "Firewall rule '$ruleName' already exists."
}

# ---------------------------------------------------------------------
# 5. Autostart - a scheduled task (default) or a true Windows Service
#    via NSSM (-Service). Only one of these should ever be registered on
#    this machine at a time (running both is exactly how you can end up
#    with two competing copies fighting over the same port) - so
#    whichever mode you asked for, the OTHER one is removed first here,
#    every time this script runs.
# ---------------------------------------------------------------------

$taskName = "CloudTrainingServer"

function Remove-ExistingTask {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing scheduled task '$taskName'..."
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}

function Remove-ExistingService {
    if (Get-Service -Name $taskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing service '$taskName'..."
        Stop-Service -Name $taskName -ErrorAction SilentlyContinue
        $nssmPath = Join-Path $here "tools\nssm.exe"
        if (Test-Path $nssmPath) {
            & $nssmPath remove $taskName confirm | Out-Null
        } else {
            # NSSM isn't here (e.g. installed on a different machine, or
            # tools\ got deleted) - sc.exe can still remove the service
            # registration itself.
            & sc.exe delete $taskName | Out-Null
        }
        Start-Sleep -Seconds 1
    }
}

function Get-Nssm {
    # Returns the full path to a working nssm.exe, downloading it once
    # into tools\ if it isn't already there.
    $toolsDir = Join-Path $here "tools"
    $nssmPath = Join-Path $toolsDir "nssm.exe"
    if (Test-Path $nssmPath) {
        return $nssmPath
    }

    Write-Host "NSSM (used to install this as a Windows Service) not found - downloading it..."
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    $zipPath = Join-Path $toolsDir "nssm.zip"
    $extractDir = Join-Path $toolsDir "nssm_extract"
    try {
        Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
        $found = Get-ChildItem -Path $extractDir -Filter "nssm.exe" -Recurse |
            Where-Object { $_.FullName -match $arch } | Select-Object -First 1
        if (-not $found) {
            # Fall back to whichever architecture copy exists, rather
            # than failing outright on an unusual layout.
            $found = Get-ChildItem -Path $extractDir -Filter "nssm.exe" -Recurse | Select-Object -First 1
        }
        if (-not $found) {
            throw "nssm.exe not found inside the downloaded zip."
        }
        Copy-Item $found.FullName $nssmPath -Force
    } catch {
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Exit-WithError (
            "ERROR: could not download NSSM automatically ($($_.Exception.Message)).`n" +
            "This machine's network may be blocking the download. To fix it by hand:`n" +
            "  1. On any machine with internet access, download https://nssm.cc/release/nssm-2.24.zip`n" +
            "  2. Copy the win64\nssm.exe (or win32\nssm.exe on a 32-bit Windows) from inside it to:`n" +
            "     $nssmPath`n" +
            "  3. Run this installer again with the same -Service flag."
        )
    }
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Write-Host "NSSM downloaded to $nssmPath."
    return $nssmPath
}

if ($Service) {
    Remove-ExistingTask
    $nssmExe = Get-Nssm
    Remove-ExistingService

    & $nssmExe install $taskName $venvPython "train_server.py"
    & $nssmExe set $taskName AppDirectory $here
    & $nssmExe set $taskName DisplayName "Pi Safety Aggregator - Cloud Training Server"
    & $nssmExe set $taskName Description `
        "Trains the sky-image AI Model for the Pi Safety Aggregator project. Installed via install.ps1 -Service."
    & $nssmExe set $taskName Start SERVICE_AUTO_START
    & $nssmExe set $taskName AppStdout (Join-Path $here "train_server.log")
    & $nssmExe set $taskName AppStderr (Join-Path $here "train_server.log")
    & $nssmExe set $taskName AppRotateFiles 1
    & $nssmExe set $taskName AppRotateBytes 10485760
    & $nssmExe set $taskName AppExit Default Restart
    & $nssmExe set $taskName AppRestartDelay 3000
    Write-Host "Windows Service '$taskName' registered via NSSM (starts at boot, before anyone logs in;"
    Write-Host "restarts itself automatically if it ever exits)."
} else {
    Remove-ExistingService
    Remove-ExistingTask

    $action = New-ScheduledTaskAction -Execute $venvPython -Argument "train_server.py" -WorkingDirectory $here
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Days 0)  # 0 = no time limit (default kills long-running tasks after 72h)

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Pi Safety Aggregator cloud training server" | Out-Null
    Write-Host "Scheduled task '$taskName' registered (starts at logon, auto-restarts on crash)."
    Write-Host "Note: this only starts once someone logs in, and can be interrupted by the console"
    Write-Host "session ending (lock/logoff/disconnect). Re-run this installer with -Service for a"
    Write-Host "true Windows Service that starts at boot and isn't tied to any logon session."
}

# ---------------------------------------------------------------------
# 6. Keep the machine reachable - disable sleep while plugged in. Left
#    untouched on battery power, in case this is a laptop.
# ---------------------------------------------------------------------

powercfg /change standby-timeout-ac 0
Write-Host "Sleep disabled while plugged in (battery behavior left unchanged)."

# ---------------------------------------------------------------------
# 7. Start it now (don't make you log out and back in, or reboot, to
#    see it work), then self-test against /health.
# ---------------------------------------------------------------------

if ($Service) {
    Start-Service -Name $taskName
} else {
    Start-ScheduledTask -TaskName $taskName
}
Write-Host "Waiting for the server to come up..."
Start-Sleep -Seconds 5

$healthy = $false
try {
    $health = Invoke-RestMethod -Uri "http://localhost:$port/health" -TimeoutSec 5
    Write-Host "Self-test OK: $($health | ConvertTo-Json -Compress)"
    $healthy = $true
} catch {
    Write-Host "WARNING: http://localhost:$port/health did not respond yet."
    Write-Host "This can happen if TensorFlow's first import is still warming up -"
    Write-Host "check again in a minute, or look at train_server.log in this folder."
}

# ---------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------

$ipAddr = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -notmatch "Loopback" -and $_.IPAddress -notmatch "^169\.254\." } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "===================================================================="
Write-Host " Cloud Training Server $(if ($healthy) {'is running'} else {'installed'})."
Write-Host " Autostart mode: $(if ($Service) {'Windows Service (NSSM)'} else {'Scheduled Task (at logon)'})"
Write-Host ""
Write-Host " Paste these into the Pi's Settings -> AI Learning:"
Write-Host "   Server URL : http://${ipAddr}:${port}/"
Write-Host "   API key    : $apiKey"
Write-Host ""
Write-Host " (also saved in server_config.json, in this same folder, if you need"
Write-Host " it again later.)"
Write-Host ""
Write-Host " This is reachable on your LAN only, via the firewall rule just added -"
Write-Host " nothing here exposes it to the public internet. If you ever need to"
Write-Host " reach it from outside your LAN, install Tailscale on this machine and"
Write-Host " the Pi (https://tailscale.com/download) and use this machine's"
Write-Host " Tailscale hostname instead of $ipAddr - do not port-forward this."
Write-Host ""
Write-Host " To remove everything this script set up, run uninstall.ps1."
Write-Host "===================================================================="
Write-Host ""
if ($Service) {
    Write-Host "(The server keeps running in the background after this window closes - it's a"
    Write-Host "Windows Service, so it starts at boot and isn't tied to you being logged in.)"
} else {
    Write-Host "(The server keeps running in the background after this window closes -"
    Write-Host "it's a scheduled task, not tied to this console.)"
}
Read-Host "Press Enter to close this window"
