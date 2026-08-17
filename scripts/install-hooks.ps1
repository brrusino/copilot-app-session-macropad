<#
.SYNOPSIS
    Installs the Copilot macropad hook configuration.

.DESCRIPTION
    Writes ~/.copilot/hooks/macropad.json, which is what feeds live session
    state to the macropad LEDs.

    This is purely additive. The Copilot CLI loads every *.json in its hooks
    directory, so this script writes only its own file and never reads, merges
    or modifies agency.json, constellation.json or anything else already there.

.PARAMETER Uninstall
    Remove the hook file instead of writing it.

.EXAMPLE
    .\install-hooks.ps1
    .\install-hooks.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$daemonDir = Join-Path $repoRoot 'daemon'

# Prefer the project venv if one exists, otherwise fall back to python on PATH.
$venvPython = Join-Path $daemonDir '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

Push-Location $daemonDir
try {
    if ($Uninstall) {
        $hookFile = Join-Path $HOME '.copilot\hooks\macropad.json'
        if (Test-Path $hookFile) {
            Remove-Item $hookFile
            Write-Host "Removed $hookFile" -ForegroundColor Yellow
        }
        else {
            Write-Host 'Nothing to remove.' -ForegroundColor Yellow
        }
        return
    }

    & $python -m macropad_daemon --install-hooks
    if ($LASTEXITCODE -ne 0) {
        throw "Hook installation failed with exit code $LASTEXITCODE"
    }

    Write-Host ''
    Write-Host 'Hooks installed. Existing hook files were left untouched:' -ForegroundColor Green
    Get-ChildItem (Join-Path $HOME '.copilot\hooks') -Filter *.json |
        ForEach-Object { Write-Host "  $($_.Name)" }
    Write-Host ''
    Write-Host 'New Copilot sessions will pick these up. Start the daemon with:' -ForegroundColor Cyan
    Write-Host '  python -m macropad_daemon'
}
finally {
    Pop-Location
}
