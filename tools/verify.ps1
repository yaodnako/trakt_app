param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

if (-not $SkipTests) {
    python -m pytest
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

python -m ruff check trakt_tracker tests
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

python -m pyright
exit $LASTEXITCODE
