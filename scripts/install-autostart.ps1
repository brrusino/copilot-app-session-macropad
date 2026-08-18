<#
.SYNOPSIS
    Starts the macropad daemon automatically when you log in.

.DESCRIPTION
    Installs a shortcut into your per-user Startup folder, so the daemon comes
    up whenever you sign in -- including each time you connect to a Cloud PC or
    VM over RDP, since that is a fresh logon.

    Deliberately uses the Startup folder rather than a scheduled task: creating
    a scheduled task requires administrator rights, which you may not have on a
    managed machine. Nothing here needs elevation.

    The daemon runs via pythonw.exe so there is no console window, and logs to
    a rotating file instead (there is no console to print to at startup).

    Running it twice is safe: the daemon refuses to start if another instance
    already holds its hook port, and says so in the log.

.PARAMETER Uninstall
    Remove the startup entry.

.PARAMETER Status
    Report whether autostart is installed and whether the daemon is running.

.EXAMPLE
    .\install-autostart.ps1
    .\install-autostart.ps1 -Status
    .\install-autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent $PSScriptRoot
$daemonDir  = Join-Path $repoRoot 'daemon'
$pythonw    = Join-Path $daemonDir '.venv\Scripts\pythonw.exe'
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcut   = Join-Path $startupDir 'Copilot Macropad.lnk'
$logFile    = Join-Path $HOME '.copilot\macropad.log'

function Get-DaemonProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*macropad_daemon*' }
}

if ($Status) {
    Write-Host "Autostart shortcut : $(if (Test-Path $shortcut) { 'installed' } else { 'not installed' })"
    Write-Host "Log file           : $logFile"
    $running = Get-DaemonProcess
    if ($running) {
        Write-Host "Daemon             : running (pid $($running.ProcessId -join ', '))" -ForegroundColor Green
    }
    else {
        Write-Host "Daemon             : not running" -ForegroundColor Yellow
    }
    if (Test-Path $logFile) {
        Write-Host ''
        Write-Host 'Last few log lines:' -ForegroundColor Cyan
        Get-Content $logFile -Tail 8 | ForEach-Object { "  $_" }
    }
    return
}

if ($Uninstall) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Force
        Write-Host "Removed $shortcut" -ForegroundColor Yellow
    }
    else {
        Write-Host 'Autostart was not installed.' -ForegroundColor Yellow
    }
    Write-Host 'Any daemon already running is left alone; stop it yourself if you want it gone.'
    return
}

if (-not (Test-Path $pythonw)) {
    throw @"
Could not find $pythonw

Create the virtualenv first:
    cd daemon
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
"@
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath       = $pythonw
$link.Arguments        = "-m macropad_daemon --log-file `"$logFile`""
$link.WorkingDirectory = $daemonDir
$link.Description      = 'Keybow 2040 macropad daemon for the GitHub Copilot app'
$link.Save()

Write-Host "Installed $shortcut" -ForegroundColor Green
Write-Host ''
Write-Host 'The daemon will start automatically at every logon, including each' -ForegroundColor Cyan
Write-Host 'RDP reconnect. It logs to:' -ForegroundColor Cyan
Write-Host "    $logFile"
Write-Host ''
Write-Host 'To start it now without logging out:' -ForegroundColor Cyan
Write-Host "    Start-Process '$pythonw' -ArgumentList '-m','macropad_daemon','--log-file','$logFile' -WorkingDirectory '$daemonDir' -WindowStyle Hidden"
