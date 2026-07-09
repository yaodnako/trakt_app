from __future__ import annotations

import sys
from pathlib import Path


AUTOSTART_NAME = "TraktTrackerWebPortal"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _pythonw_candidate(executable: str | None = None) -> Path:
    path = Path(executable or sys.executable)
    if path.name.casefold() == "python.exe":
        candidate = path.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return path


def build_web_tray_autostart_command(executable: str | None = None) -> str:
    return f'"{_pythonw_candidate(executable)}" -m trakt_tracker.web_tray'


def set_web_tray_autostart(enabled: bool, *, command: str | None = None) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command or build_web_tray_autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def is_web_tray_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, AUTOSTART_NAME)
    except FileNotFoundError:
        return False
    return bool(str(value or "").strip())
