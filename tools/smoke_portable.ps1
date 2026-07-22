param(
    [string]$BundleDirectory = "",
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($BundleDirectory)) {
    $BundleDirectory = Join-Path $projectRoot "dist\TraktTracker"
}
$bundleRoot = (Resolve-Path $BundleDirectory).Path
$executable = Join-Path $bundleRoot "TraktTracker.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Packaged executable not found: $executable"
}

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("TraktTrackerSmoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$previousLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = $smokeRoot
$primary = $null
$portalUrl = ""

try {
    $primary = Start-Process -FilePath $executable -ArgumentList "--autostart" -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $logPath = Join-Path $smokeRoot "TraktTracker\logs\runtime.log"
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($primary.HasExited) {
            throw "Packaged application exited before becoming ready (code $($primary.ExitCode))."
        }
        if (Test-Path -LiteralPath $logPath) {
            $match = Select-String -LiteralPath $logPath -Pattern 'web portal ready at (http://127\.0\.0\.1:\d+)' | Select-Object -Last 1
            if ($match) {
                $portalUrl = $match.Matches[0].Groups[1].Value
                try {
                    $health = Invoke-RestMethod -Uri "$portalUrl/healthz" -TimeoutSec 2
                    if ($health.status -eq "ok" -and $health.version -eq "0.2.0b1") { break }
                } catch {
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if ([string]::IsNullOrWhiteSpace($portalUrl)) {
        throw "Packaged application did not publish a ready URL."
    }
    if ($health.version -ne "0.2.0b1") {
        throw "Packaged health endpoint reported an unexpected version: $($health.version)"
    }

    $secondary = Start-Process -FilePath $executable -ArgumentList "--autostart" -PassThru -WindowStyle Hidden -Wait
    if ($secondary.ExitCode -ne 0 -or $primary.HasExited) {
        throw "Single-instance check failed."
    }

    $quit = Start-Process -FilePath $executable -ArgumentList "--quit" -PassThru -WindowStyle Hidden -Wait
    if ($quit.ExitCode -ne 0) {
        throw "Packaged quit command failed."
    }
    if (-not $primary.WaitForExit(15000)) {
        throw "Packaged application did not stop after --quit."
    }
    try {
        Invoke-RestMethod -Uri "$portalUrl/healthz" -TimeoutSec 2 | Out-Null
        throw "Web listener remained reachable after packaged application stopped."
    } catch {
        if ($_.Exception.Message -eq "Web listener remained reachable after packaged application stopped.") { throw }
    }
    Write-Output "Portable smoke passed: $portalUrl"
} finally {
    if ($primary -and -not $primary.HasExited) {
        Stop-Process -Id $primary.Id -Force
        $primary.WaitForExit(5000) | Out-Null
    }
    $env:LOCALAPPDATA = $previousLocalAppData
    $resolvedSmoke = [System.IO.Path]::GetFullPath($smokeRoot)
    $tempPrefix = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedSmoke.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedSmoke)) {
        Remove-Item -LiteralPath $resolvedSmoke -Recurse -Force
    }
}
