from __future__ import annotations

from trakt_tracker.infrastructure.windows_autostart import build_web_tray_autostart_command


def test_build_web_tray_autostart_command_uses_module_entrypoint() -> None:
    command = build_web_tray_autostart_command("C:/Python/pythonw.exe")
    assert command.endswith('" -m trakt_tracker.web_tray')
    assert "pythonw.exe" in command
