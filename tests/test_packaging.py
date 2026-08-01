from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portable_build_inputs_and_resources_are_declared() -> None:
    spec = (ROOT / "TraktTracker.spec").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'version = "0.2.0b1"' in pyproject
    assert '"pyinstaller==6.21.0"' in pyproject
    assert '"trakt_tracker.web" = ["templates/*.html", "static/*"]' in pyproject
    assert 'collect_data_files("trakt_tracker.web"' in spec
    assert 'collect_submodules("keyring.backends")' in spec
    assert 'contents_directory="_internal"' in spec


def test_build_script_never_contains_provider_values() -> None:
    build_script = (ROOT / "tools" / "build_portable.ps1").read_text(encoding="utf-8")
    assert "TRAKT_TRACKER_RELEASE_HOOK" in build_script
    assert "AllowMissingDefaults" in build_script
    assert 'version\\s*=\\s*"0\\.2\\.0b1"' in build_script
    assert "Get-FileHash" in build_script
    assert "client_id=" not in build_script.casefold()
    assert "client_secret=" not in build_script.casefold()


def test_verify_script_propagates_tool_failures() -> None:
    verify_script = (ROOT / "tools" / "verify.ps1").read_text(encoding="utf-8")

    assert (
        "node --test tests/test_ui_core_title_matrix_state.cjs tests/test_show_watch_panel_state.cjs"
        in verify_script
    )
    assert verify_script.count("if ($LASTEXITCODE -ne 0)") == 3
    assert verify_script.count("exit $LASTEXITCODE") == 4


def test_portable_readme_describes_local_data_and_manual_updates() -> None:
    readme = (ROOT / "tools" / "PORTABLE_README.txt").read_text(encoding="utf-8")
    assert "%LOCALAPPDATA%\\TraktTracker" in readme
    assert "To update" in readme
    assert "does not expose" in readme
