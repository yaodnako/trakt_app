from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(slots=True)
class NotificationMessage:
    title: str
    body: str


class NotificationSender:
    def send(self, message: NotificationMessage) -> None:
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
            "$template = @\"<toast><visual><binding template='ToastGeneric'>"
            f"<text>{self._escape(message.title)}</text>"
            f"<text>{self._escape(message.body)}</text>"
            "</binding></visual></toast>\"@; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            "$xml.LoadXml($template); "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Trakt Tracker'); "
            "$notifier.Show($toast)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _escape(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

