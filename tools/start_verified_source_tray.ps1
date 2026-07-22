param(
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "pythonw.exe was not found next to the active Python interpreter."
}

$runtime = Start-Process -FilePath $pythonw -ArgumentList @("-m", "trakt_tracker.web_tray", "--autostart") -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden
try {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($runtime.HasExited) {
            throw "Source tray exited early with code $($runtime.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 2
            if ($health.status -eq "ok") {
                $ready = $true
                break
            }
        } catch {
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) {
        throw "Source tray did not become ready."
    }

    $status = Invoke-RestMethod -Uri "http://127.0.0.1:8000/setup/status" -TimeoutSec 5
    $listener = Get-NetTCPConnection -State Listen -LocalPort 8000 | Select-Object -First 1
    $runtimeProcesses = @(
        Get-CimInstance Win32_Process |
            Where-Object { $_.Name -in @("python.exe", "pythonw.exe") -and $_.CommandLine -like "*trakt_tracker*" }
    )
    [pscustomobject]@{
        tray_pid = $runtime.Id
        health_version = $health.version
        profile = $status.profile_slug
        setup = $status.state
        listener_address = $listener.LocalAddress
        listener_pid = $listener.OwningProcess
        trakt_python_processes = $runtimeProcesses.Count
    }
} catch {
    if (-not $runtime.HasExited) {
        Stop-Process -Id $runtime.Id -Force
        $runtime.WaitForExit(5000) | Out-Null
    }
    throw
}
