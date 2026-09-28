#
# Cloud Training Server - Windows installer
# ------------------------------------------
# Sets up this folder's train_server.py to run automatically on this
# Windows machine: installs Python (if needed) and its dependencies into
# a private virtual environment, generates an API key, opens a firewall
# rule so the Pi can reach it on the LAN, and registers a scheduled task
# so it starts when you log in and restarts itself if it ever crashes.
#
# Run it by right-clicking this file -> "Run with PowerShell", or from
# an existing PowerShell window:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Safe to re-run any time (e.g. after moving the folder, or to recover
# from a problem) - it reuses the existing API key instead of generating
# a new one each time, and cleanly replaces the scheduled task.

# ---------------------------------------------------------------------
# 0. Self-elevate to Administrator - the firewall rule and scheduled
#    task below both require it.
# ---------------------------------------------------------------------

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Host "Re-launching with administrator privileges..."
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    )
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
# 5. Scheduled task - starts the server when you log in, restarts it if
#    it crashes. (Runs "at logon" rather than a true unattended service,
#    so it won't start before anyone signs in to Windows - the simplest
#    option that avoids storing an account password. If you need it
#    running with nobody logged in, look at NSSM instead, which wraps
#    this same command as a real Windows Service.)
# ---------------------------------------------------------------------

$taskName = "CloudTrainingServer"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing scheduled task '$taskName'..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $venvPython -Argument "train_server.py" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)  # 0 = no time limit (default kills long-running tasks after 72h)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Pi Safety Aggregator cloud training server" | Out-Null
Write-Host "Scheduled task '$taskName' registered (starts at logon, auto-restarts on crash)."

# ---------------------------------------------------------------------
# 6. Keep the machine reachable - disable sleep while plugged in. Left
#    untouched on battery power, in case this is a laptop.
# ---------------------------------------------------------------------

powercfg /change standby-timeout-ac 0
Write-Host "Sleep disabled while plugged in (battery behavior left unchanged)."

# ---------------------------------------------------------------------
# 7. Start it now (don't make you log out and back in to see it work),
#    then self-test against /health.
# ---------------------------------------------------------------------

Start-ScheduledTask -TaskName $taskName
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
Write-Host "(The server keeps running in the background after this window closes -"
Write-Host "it's a scheduled task, not tied to this console.)"
Read-Host "Press Enter to close this window"
