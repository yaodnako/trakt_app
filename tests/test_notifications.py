import subprocess
from unittest.mock import patch

from trakt_tracker.infrastructure.notifications import NotificationMessage, NotificationSender


def test_notification_sender_hides_powershell_window_on_windows() -> None:
    with patch("trakt_tracker.infrastructure.notifications.subprocess.run") as run:
        NotificationSender().send(NotificationMessage(title="Show", body="S01E01 Pilot"))

    kwargs = run.call_args.kwargs
    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
