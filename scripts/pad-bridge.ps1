<#
.SYNOPSIS
    Relays a locally-attached Keybow 2040 to a macropad daemon on another machine.

.DESCRIPTION
    PowerShell counterpart of pad_bridge.py, for machines where you cannot
    install anything -- a locked-down SSO device, for instance.

    It needs NO installation. Windows PowerShell ships with Windows, and both
    halves of this script use built-in .NET types:

        System.IO.Ports.SerialPort      talks to the pad
        System.Net.Sockets.TcpClient    talks to the daemon

    Run it on the machine the pad is physically plugged into. It reads the pad's
    USB serial port and forwards the line-delimited JSON protocol, in both
    directions, to the daemon's bridge listener on your devbox.

    The connection is outbound, in the same direction your RDP session already
    travels, so it needs no inbound firewall change on this machine.

    If script execution is blocked by policy, you do not need a .ps1 at all --
    see "RUNNING WITHOUT A SCRIPT FILE" below.

.PARAMETER DaemonHost
    Host running the macropad daemon (the machine with the Copilot app).

.PARAMETER Port
    Daemon bridge port. Defaults to 7831.

.PARAMETER Token
    Shared bridge token. The daemon writes this to ~/.copilot/macropad.token
    on its own machine the first time it runs in network mode.

.PARAMETER TokenFile
    Read the token from a file instead of passing it on the command line.

.PARAMETER SerialPort
    Pad COM port, e.g. "COM7". Auto-detected when omitted.

.PARAMETER TestConnection
    Verify the daemon is reachable and the token is accepted, then exit.
    Does not need the pad attached -- useful when setting the two machines up.

.EXAMPLE
    .\pad-bridge.ps1 -DaemonHost devbox -Token abc123

.EXAMPLE
    .\pad-bridge.ps1 -DaemonHost devbox -Token abc123 -TestConnection

.NOTES
    RUNNING WITHOUT A SCRIPT FILE

    PowerShell's execution policy applies to script FILES, not to -Command. If
    .ps1 files are blocked on this machine, paste the script body into an
    interactive PowerShell window instead, or invoke it as:

        powershell -Command "<script body>"

    Neither route requires admin rights or a policy change.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DaemonHost,

    [int]$Port = 7831,

    [string]$Token,

    [string]$TokenFile,

    [string]$SerialPort,

    [switch]$TestConnection
)

$ErrorActionPreference = 'Stop'

# USB vendor ids CircuitPython boards present with.
# 2E8A = Raspberry Pi (RP2040), 239A = Adafruit, 16D0 = MCS/Pimoroni.
$script:KnownVids = @('2E8A', '239A', '16D0')
$script:ReconnectDelay = 2
$script:ProbeTimeoutMs = 3500

# PowerShell 7 does not always have System.IO.Ports loaded; Windows PowerShell
# always does. Loading it twice is harmless.
try { Add-Type -AssemblyName System.IO.Ports -ErrorAction SilentlyContinue } catch { }

function Resolve-Token {
    if ($Token) { return $Token }
    if ($TokenFile) {
        if (-not (Test-Path $TokenFile)) { throw "Token file not found: $TokenFile" }
        return (Get-Content $TokenFile -Raw).Trim()
    }
    throw 'One of -Token or -TokenFile is required.'
}

function Get-CandidatePort {
    <#
        Ports whose USB vendor id looks like a CircuitPython board, best guess
        first, falling back to every port when none match. Each probe costs a
        few seconds, so walking every COM port on the machine is a last resort.
    #>
    $all = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object

    $known = @()
    try {
        $pnp = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\((COM\d+)\)' }
        foreach ($device in $pnp) {
            if ($device.Name -match '\((COM\d+)\)') {
                $name = $Matches[1]
                foreach ($vid in $script:KnownVids) {
                    if ($device.DeviceID -match "VID_$vid") {
                        $known += $name
                        break
                    }
                }
            }
        }
    }
    catch { }

    if ($known.Count -gt 0) {
        return ($known | Sort-Object -Unique)
    }
    return $all
}

function Test-PadPort {
    param([string]$Name)

    # The firmware emits a heartbeat unprompted, so we mostly have to listen.
    # We also nudge it in case we connected mid-cycle.
    $serial = New-Object System.IO.Ports.SerialPort $Name, 115200
    $serial.ReadTimeout = 200
    $serial.WriteTimeout = 200
    try {
        $serial.Open()
        try { $serial.WriteLine('{"t":"hb"}') } catch { }
        $deadline = (Get-Date).AddMilliseconds($script:ProbeTimeoutMs)
        while ((Get-Date) -lt $deadline) {
            try {
                $line = $serial.ReadLine()
            }
            catch [TimeoutException] { continue }
            catch { break }

            if ($line -and $line.Trim().StartsWith('{')) {
                try {
                    $parsed = $line.Trim() | ConvertFrom-Json
                    if ($parsed.PSObject.Properties.Name -contains 't') { return $true }
                }
                catch { }
            }
        }
    }
    catch { return $false }
    finally {
        if ($serial.IsOpen) { $serial.Close() }
        $serial.Dispose()
    }
    return $false
}

function Find-Pad {
    if ($SerialPort) {
        if (Test-PadPort -Name $SerialPort) { return $SerialPort }
        return $null
    }
    foreach ($name in Get-CandidatePort) {
        Write-Verbose "probing $name"
        if (Test-PadPort -Name $name) { return $name }
    }
    return $null
}

function Connect-Daemon {
    param([string]$AuthToken)

    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect($DaemonHost, $Port)
    $stream = $client.GetStream()
    $handshake = [System.Text.Encoding]::UTF8.GetBytes(
        ('{{"t":"auth","token":"{0}"}}' -f $AuthToken) + "`n")
    $stream.Write($handshake, 0, $handshake.Length)
    $stream.Flush()
    return $client
}

function Test-DaemonLink {
    <#
        Returns 'ok', 'rejected', or 'unreachable'.

        The daemon drops an unauthenticated socket without replying, so
        TcpClient.Connected is NOT a usable signal -- it reflects the last I/O
        operation, not the peer's close, and a write to a just-closed socket
        frequently succeeds. The reliable test is to READ: a clean remote close
        surfaces as a zero-byte read (EOF).
    #>
    param([string]$AuthToken)

    try {
        $client = Connect-Daemon -AuthToken $AuthToken
    }
    catch {
        $script:LastError = $_.Exception.Message
        return 'unreachable'
    }

    try {
        $stream = $client.GetStream()

        # Nudge the daemon so a dead peer surfaces promptly.
        try {
            $ping = [System.Text.Encoding]::UTF8.GetBytes("{`"t`":`"hb`"}`n")
            $stream.Write($ping, 0, $ping.Length)
            $stream.Flush()
        }
        catch {
            return 'rejected'
        }

        # Give the daemon time to drop us if the token was bad. Without this the
        # read races the server's close and a rejected socket looks like a
        # healthy idle one.
        Start-Sleep -Milliseconds 800

        $stream.ReadTimeout = 2000
        $buffer = New-Object byte[] 256
        try {
            $count = $stream.Read($buffer, 0, $buffer.Length)
            if ($count -le 0) { return 'rejected' }   # clean EOF: we were dropped
            return 'ok'                                # daemon sent us something
        }
        catch {
            # Read() can fail two very different ways and they must not be
            # conflated:
            #
            #   TimedOut        - connection alive, daemon simply has nothing to
            #                     say yet. That is a healthy accepted bridge.
            #   reset/aborted   - the daemon dropped us, i.e. bad token.
            #
            # PowerShell wraps both in a MethodInvocationException, so unwrap to
            # the SocketException and read its SocketErrorCode. Treating every
            # IOException as a timeout reports a rejected token as success.
            $inner = $_.Exception
            while ($inner.InnerException) { $inner = $inner.InnerException }

            if ($inner -is [System.Net.Sockets.SocketException]) {
                if ($inner.SocketErrorCode -eq [System.Net.Sockets.SocketError]::TimedOut) {
                    return 'ok'
                }
                return 'rejected'
            }
            return 'rejected'
        }
    }
    finally {
        $client.Close()
    }
}

function Invoke-Relay {
    <#
        Pump bytes both ways until either side goes away.
    #>
    param(
        [System.IO.Ports.SerialPort]$Pad,
        [System.Net.Sockets.TcpClient]$Client
    )

    $stream = $Client.GetStream()
    $buffer = New-Object byte[] 1024

    while ($true) {
        # Pad -> daemon
        try {
            $waiting = $Pad.BytesToRead
            if ($waiting -gt 0) {
                $count = $Pad.Read($buffer, 0, [Math]::Min($waiting, $buffer.Length))
                if ($count -gt 0) {
                    $stream.Write($buffer, 0, $count)
                    $stream.Flush()
                }
            }
        }
        catch {
            Write-Warning 'pad went away'
            return
        }

        # Daemon -> pad
        try {
            if ($stream.DataAvailable) {
                $count = $stream.Read($buffer, 0, $buffer.Length)
                if ($count -le 0) {
                    Write-Warning 'daemon closed the connection'
                    return
                }
                $Pad.Write($buffer, 0, $count)
            }
            elseif (-not $Client.Connected) {
                Write-Warning 'daemon connection lost'
                return
            }
        }
        catch {
            Write-Warning 'link lost'
            return
        }

        Start-Sleep -Milliseconds 5
    }
}

# --- main -----------------------------------------------------------------

$authToken = Resolve-Token

if ($TestConnection) {
    Write-Host "Testing $DaemonHost`:$Port ..." -ForegroundColor Cyan
    $result = Test-DaemonLink -AuthToken $authToken

    switch ($result) {
        'ok' {
            Write-Host '  OK: daemon reachable and token accepted.' -ForegroundColor Green
            Write-Host ''
            Write-Host 'Now run without -TestConnection to bridge the pad.' -ForegroundColor Cyan
            exit 0
        }
        'rejected' {
            Write-Host '  REJECTED: connected, but the daemon dropped us.' -ForegroundColor Red
            Write-Host '  The token is probably wrong -- check ~/.copilot/macropad.token' -ForegroundColor Yellow
            Write-Host '  on the daemon machine.' -ForegroundColor Yellow
            exit 1
        }
        default {
            Write-Host "  UNREACHABLE: $script:LastError" -ForegroundColor Red
            Write-Host ''
            Write-Host 'Check that the daemon is running with transport = "network",' -ForegroundColor Yellow
            Write-Host 'that bridge_host is not 127.0.0.1, and that the port is open.' -ForegroundColor Yellow
            exit 1
        }
    }
}

Write-Host "Bridging pad -> $DaemonHost`:$Port" -ForegroundColor Cyan
Write-Host 'Ctrl-C to stop.'

while ($true) {
    $portName = Find-Pad
    if (-not $portName) {
        Write-Host 'keybow not found; retrying' -ForegroundColor Yellow
        Start-Sleep -Seconds $script:ReconnectDelay
        continue
    }

    $pad = New-Object System.IO.Ports.SerialPort $portName, 115200
    $pad.ReadTimeout = 200
    $pad.WriteTimeout = 200
    try {
        $pad.Open()
    }
    catch {
        Write-Warning "could not open $portName`: $($_.Exception.Message)"
        $pad.Dispose()
        Start-Sleep -Seconds $script:ReconnectDelay
        continue
    }

    Write-Host "pad on $portName" -ForegroundColor Green

    $client = $null
    try {
        $client = Connect-Daemon -AuthToken $authToken
        Write-Host 'connected to daemon' -ForegroundColor Green
        Invoke-Relay -Pad $pad -Client $client
    }
    catch {
        Write-Warning "daemon unreachable: $($_.Exception.Message)"
    }
    finally {
        if ($client) { $client.Close() }
        if ($pad.IsOpen) { $pad.Close() }
        $pad.Dispose()
    }

    Start-Sleep -Seconds $script:ReconnectDelay
}
