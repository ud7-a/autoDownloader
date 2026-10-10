"""Windows notifications for finished downloads and new episodes.

Qt posts a Windows notification through a system-tray icon, so one is shown
while the app runs -- only as the notifications' source (clicking it brings the
window back); staying resident after close is still the watcher's job.
Notifications are skipped while the app is the window being used: its own
in-app messages cover that case.
"""
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from utils.config import app_settings


def finished_message(profile, episodes, failed):
    """(title, text) for a finished download run."""
    total = len(episodes or [])
    failed = list(failed or [])
    ok = max(0, total - len(failed))
    name = profile or "Download"
    if total and not failed:
        return ("Download complete",
                f"{name}: {ok} episode{'s' if ok != 1 else ''} downloaded.")
    if total and ok == 0:
        return ("Download failed", f"{name}: no episode could be downloaded.")
    shown = ", ".join(str(e) for e in failed[:5]) + ("…" if len(failed) > 5 else "")
    return ("Download finished with errors",
            f"{name}: {ok} of {total} downloaded; failed: {shown}.")


def new_episodes_message(count):
    return ("New episodes",
            f"{count} new episode{'s' if count != 1 else ''} across your Watchlist.")


class Notifier:
    def __init__(self, window):
        self._window = window
        self._tray = None

    def _ensure_tray(self):
        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            icon = self._window.windowIcon()
            if icon.isNull():
                icon = QApplication.windowIcon() or QIcon()
            self._tray = QSystemTrayIcon(icon, self._window)
            self._tray.setToolTip(self._window.windowTitle())
            self._tray.activated.connect(self._bring_back)
            self._tray.messageClicked.connect(self._bring_back)
            self._tray.show()
        return self._tray

    def _bring_back(self, *_):
        w = self._window
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def notify(self, title, text):
        """Post a notification unless turned off or the app is in use. Returns
        True if one was shown."""
        if not app_settings.get("windows_notifications", True):
            return False
        if self._window.isActiveWindow() and not self._window.isMinimized():
            return False
        tray = self._ensure_tray()
        if tray is None:
            return False
        tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 8000)
        return True
