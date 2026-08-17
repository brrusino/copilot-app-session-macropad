<#
.SYNOPSIS
    Copies the macropad firmware onto a Keybow 2040 running CircuitPython.

.DESCRIPTION
    Finds the CIRCUITPY drive and copies boot.py, code.py and config.py onto it.

    Two things worth knowing:

    * boot.py only takes effect on a hard reset. After the first install,
      unplug and replug the pad so the USB CDC data port appears -- a soft
      reload is not enough.
    * The PMK and adafruit_hid libraries are NOT copied by this script. Install
      them into CIRCUITPY\lib first; see the README.

.PARAMETER Drive
    CIRCUITPY drive letter, e.g. "E:". Auto-detected when omitted.

.PARAMETER Calibrate
    Install calibrate.py as code.py instead, to identify physical key numbers.

.EXAMPLE
    .\flash-firmware.ps1
    .\flash-firmware.ps1 -Calibrate
    .\flash-firmware.ps1 -Drive E:
#>
[CmdletBinding()]
param(
    [string]$Drive,
    [switch]$Calibrate
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$firmwareDir = Join-Path $repoRoot 'keybow'

function Find-CircuitPyDrive {
    $candidates = Get-CimInstance Win32_LogicalDisk |
        Where-Object { $_.VolumeName -eq 'CIRCUITPY' }
    if ($candidates.Count -eq 1) { return $candidates[0].DeviceID }
    if ($candidates.Count -gt 1) {
        throw "Multiple CIRCUITPY drives found. Pass -Drive to pick one."
    }
    return $null
}

if (-not $Drive) {
    $Drive = Find-CircuitPyDrive
}

if (-not $Drive) {
    Write-Host 'No CIRCUITPY drive found.' -ForegroundColor Red
    Write-Host ''
    Write-Host 'Check that:' -ForegroundColor Yellow
    Write-Host '  1. The Keybow 2040 is plugged in.'
    Write-Host '  2. It is running CircuitPython, not the stock firmware.'
    Write-Host '     Flash the CircuitPython .uf2 by holding BOOT while plugging in,'
    Write-Host '     then dropping the .uf2 onto the RPI-RP2 drive that appears.'
    exit 1
}

$Drive = $Drive.TrimEnd('\')
if (-not (Test-Path $Drive)) { throw "Drive $Drive is not accessible." }

Write-Host "Target: $Drive" -ForegroundColor Cyan

# Warn rather than fail: the libraries may be vendored some other way.
$libDir = Join-Path $Drive 'lib'
foreach ($lib in @('pmk', 'adafruit_hid')) {
    if (-not (Test-Path (Join-Path $libDir $lib))) {
        Write-Host "  WARNING: $lib not found in $libDir -- firmware will not run without it." -ForegroundColor Yellow
    }
}

$files = @('boot.py', 'config.py')
foreach ($file in $files) {
    $source = Join-Path $firmwareDir $file
    Copy-Item $source (Join-Path $Drive $file) -Force
    Write-Host "  copied $file" -ForegroundColor Green
}

$mainSource = if ($Calibrate) { 'calibrate.py' } else { 'code.py' }
Copy-Item (Join-Path $firmwareDir $mainSource) (Join-Path $Drive 'code.py') -Force
Write-Host "  copied $mainSource -> code.py" -ForegroundColor Green

Write-Host ''
if ($Calibrate) {
    Write-Host 'Calibration firmware installed.' -ForegroundColor Cyan
    Write-Host 'Open the CircuitPython REPL and press keys to read their numbers,'
    Write-Host 'then put the results into ROWS in keybow/config.py and re-run'
    Write-Host 'this script without -Calibrate.'
}
else {
    Write-Host 'Firmware installed.' -ForegroundColor Cyan
    Write-Host 'If this is the first install, UNPLUG AND REPLUG the pad now so'
    Write-Host 'boot.py can enable the USB serial data port.'
}
