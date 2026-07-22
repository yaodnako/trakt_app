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

$configPath = Join-Path $env:LOCALAPPDATA "TraktTracker\config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Existing Trakt Tracker config was not found."
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$legacyPath = if ($config.database_path) { "$($config.database_path)" } else { Join-Path $env:LOCALAPPDATA "TraktTracker\tracker.sqlite3" }
$legacyHash = if (Test-Path -LiteralPath $legacyPath) { (Get-FileHash -LiteralPath $legacyPath -Algorithm SHA256).Hash } else { "" }
$runtimeLog = Join-Path $env:LOCALAPPDATA "TraktTracker\logs\runtime.log"
$initialLogLines = if (Test-Path -LiteralPath $runtimeLog) { @(Get-Content -LiteralPath $runtimeLog).Count } else { 0 }
$primary = $null
$portalUrl = ""

try {
    $primary = Start-Process -FilePath $executable -ArgumentList "--autostart" -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($primary.HasExited) {
            throw "Packaged application exited before the existing profile was verified (code $($primary.ExitCode))."
        }
        if (Test-Path -LiteralPath $runtimeLog) {
            $newLines = @(Get-Content -LiteralPath $runtimeLog | Select-Object -Skip $initialLogLines)
            $match = $newLines | Select-String -Pattern 'web portal ready at (http://127\.0\.0\.1:\d+)' | Select-Object -Last 1
            if ($match) {
                $portalUrl = $match.Matches[0].Groups[1].Value
                break
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if ([string]::IsNullOrWhiteSpace($portalUrl)) {
        throw "Packaged application did not publish a ready URL."
    }

    $health = Invoke-RestMethod -Uri "$portalUrl/healthz" -TimeoutSec 5
    $status = Invoke-RestMethod -Uri "$portalUrl/setup/status" -TimeoutSec 5
    $progress = Invoke-WebRequest -Uri "$portalUrl/progress" -TimeoutSec 15 -UseBasicParsing
    if ($health.status -ne "ok" -or $health.version -ne "0.2.0b1") {
        throw "Packaged health response is invalid."
    }
    if (-not $status.authorized -or $status.state -ne "complete" -or [string]::IsNullOrWhiteSpace($status.profile_slug)) {
        throw "Existing profile was not recognized as authorized and complete."
    }
    if ($progress.StatusCode -ne 200) {
        throw "Existing profile did not open Progress successfully."
    }
    if ($legacyHash) {
        $currentLegacyHash = (Get-FileHash -LiteralPath $legacyPath -Algorithm SHA256).Hash
        if ($currentLegacyHash -ne $legacyHash) {
            throw "Legacy database changed during packaged startup."
        }
    }
    Write-Output "Existing profile verified: $($status.profile_slug), setup=$($status.state), url=$portalUrl"
    Write-Output "Legacy database preserved: $legacyPath"
} finally {
    if ($primary -and -not $primary.HasExited) {
        $quit = Start-Process -FilePath $executable -ArgumentList "--quit" -PassThru -WindowStyle Hidden -Wait
        if (-not $primary.WaitForExit(15000)) {
            Stop-Process -Id $primary.Id -Force
            $primary.WaitForExit(5000) | Out-Null
        }
    }
}
