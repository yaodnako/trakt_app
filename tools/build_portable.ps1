param(
    [switch]$AllowMissingDefaults,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pyprojectText = Get-Content -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Raw
if ($pyprojectText -notmatch '(?m)^version\s*=\s*"0\.2\.0b1"\s*$') {
    throw "Portable artifact/version metadata must be updated together."
}
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "dist"))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "build"))
$projectPrefix = $projectRoot.TrimEnd('\') + '\'
if (-not $distRoot.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a dist path outside the project."
}
if (-not $buildRoot.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a build path outside the project."
}

$providerNames = @(
    "TRAKT_TRACKER_TRAKT_CLIENT_ID",
    "TRAKT_TRACKER_TRAKT_CLIENT_SECRET",
    "TRAKT_TRACKER_TMDB_API_KEY",
    "TRAKT_TRACKER_TMDB_READ_ACCESS_TOKEN"
)
$providerDefaults = @{}
foreach ($name in $providerNames) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        if ($value.Contains("`0") -or $value.Contains("`r") -or $value.Contains("`n")) {
            throw "$name contains an unsupported control character."
        }
        $providerDefaults[$name] = $value
    }
}

if (-not $AllowMissingDefaults) {
    if (-not $providerDefaults.ContainsKey("TRAKT_TRACKER_TRAKT_CLIENT_ID") -or
        -not $providerDefaults.ContainsKey("TRAKT_TRACKER_TRAKT_CLIENT_SECRET")) {
        throw "Release build requires Trakt Client ID and Client Secret environment variables."
    }
    if (-not $providerDefaults.ContainsKey("TRAKT_TRACKER_TMDB_API_KEY") -and
        -not $providerDefaults.ContainsKey("TRAKT_TRACKER_TMDB_READ_ACCESS_TOKEN")) {
        throw "Release build requires a TMDb API key or read access token environment variable."
    }
}

$pythonBits = & python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or "$pythonBits".Trim() -ne "64") {
    throw "Portable build requires 64-bit Python."
}
& python -c "import PyInstaller; print(PyInstaller.__version__)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is missing. Install release dependencies with: python -m pip install -e ".[release]"'
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("TraktTrackerBuild-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$runtimeHook = Join-Path $temporaryRoot "release_defaults_hook.py"
$versionFile = Join-Path $temporaryRoot "version_info.txt"
$previousRuntimeHook = $env:TRAKT_TRACKER_RELEASE_HOOK
$previousVersionFile = $env:TRAKT_TRACKER_VERSION_FILE

try {
    $defaultsJson = $providerDefaults | ConvertTo-Json -Compress
    $defaultsBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($defaultsJson))
    $hookLines = @(
        "import base64",
        "import json",
        "import os",
        "_DEFAULTS = json.loads(base64.b64decode('$defaultsBase64').decode('utf-8'))",
        "for _name, _value in _DEFAULTS.items():",
        "    os.environ.setdefault(_name, _value)",
        "del _DEFAULTS"
    )
    [System.IO.File]::WriteAllLines($runtimeHook, $hookLines, [Text.UTF8Encoding]::new($false))

    $versionText = @'
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(0, 2, 0, 1),
    prodvers=(0, 2, 0, 1),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Trakt Tracker'),
        StringStruct('FileDescription', 'Trakt Tracker Web Portal'),
        StringStruct('FileVersion', '0.2.0 beta 1'),
        StringStruct('InternalName', 'TraktTracker'),
        StringStruct('OriginalFilename', 'TraktTracker.exe'),
        StringStruct('ProductName', 'Trakt Tracker'),
        StringStruct('ProductVersion', '0.2.0 beta 1')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'@
    [System.IO.File]::WriteAllText($versionFile, $versionText, [Text.UTF8Encoding]::new($false))
    $env:TRAKT_TRACKER_RELEASE_HOOK = $runtimeHook
    $env:TRAKT_TRACKER_VERSION_FILE = $versionFile

    if (-not $SkipTests) {
        Push-Location $projectRoot
        try {
            & python -m pytest -q
            if ($LASTEXITCODE -ne 0) { throw "Tests failed; portable build aborted." }
        } finally {
            Pop-Location
        }
    }

    if (Test-Path -LiteralPath $distRoot) { Remove-Item -LiteralPath $distRoot -Recurse -Force }
    if (Test-Path -LiteralPath $buildRoot) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }

    Push-Location $projectRoot
    try {
        & python -m PyInstaller --noconfirm --clean TraktTracker.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    } finally {
        Pop-Location
    }

    $bundleRoot = Join-Path $distRoot "TraktTracker"
    $executable = Join-Path $bundleRoot "TraktTracker.exe"
    if (-not (Test-Path -LiteralPath $executable)) {
        throw "Packaged executable was not created."
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_README.txt") -Destination (Join-Path $bundleRoot "README.txt")

    $archive = Join-Path $distRoot "TraktTracker-0.2.0b1-win64-portable.zip"
    Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $archive -CompressionLevel Optimal
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText("$archive.sha256", "$hash  $([System.IO.Path]::GetFileName($archive))`n", [Text.UTF8Encoding]::new($false))
    Write-Output "Portable archive: $archive"
    Write-Output "SHA256: $hash"
} finally {
    $env:TRAKT_TRACKER_RELEASE_HOOK = $previousRuntimeHook
    $env:TRAKT_TRACKER_VERSION_FILE = $previousVersionFile
    if (Test-Path -LiteralPath $buildRoot) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }
    $resolvedTemp = [System.IO.Path]::GetFullPath($temporaryRoot)
    $tempPrefix = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedTemp.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedTemp)) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
