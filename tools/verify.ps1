param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

if (-not $SkipTests) {
    python -m pytest
}

python -m ruff check trakt_tracker tests
python -m pyright
