from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import (FluentWindow, FluentIcon as FIF, MessageBoxBase,
                            SubtitleLabel, BodyLabel, PushButton)

from ui.downloader_tab import DownloaderWidget
from ui.search_tab import AnimeSearchWidget
from ui.manager_tab import SiteManagerWidget
from ui.history_tab import HistoryWidget
from ui.progress_tab import ProgressTab
from ui.watchlist_tab import WatchlistWidget
from core.signals import signals
from utils.config import APP_VERSION, app_settings, get_watchlist

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts')


class MissingEpisodesDialog(MessageBoxBase):
    """Asks what to do when "Start Watching" is pressed but part of the download is
    missing. `choice` is one of "start", "retry" or "cancel"."""

    def __init__(self, missing, total, parent=None):
        super().__init__(parent)
        self.choice = "cancel"

        from ui.downloader_tab import compact_episode_spec
        n = len(missing)
        heading = SubtitleLabel(f"{n} episode{'s' if n != 1 else ''} didn't download")
        detail = BodyLabel(
            f"Missing: {compact_episode_spec(missing)}\n"
            f"{total - n} of {total} episodes are ready to watch."
        )
        detail.setWordWrap(True)
        self.viewLayout.addWidget(heading)
        self.viewLayout.addWidget(detail)

        self.yesButton.setText("Start anyway")
        self.cancelButton.setText("Cancel")
        self.retryButton = PushButton("Retry missing episodes")
        self.retryButton.setCursor(Qt.CursorShape.PointingHandCursor)
        # Sits between "Start anyway" and "Cancel".
        self.buttonLayout.insertWidget(1, self.retryButton)

        self.yesButton.clicked.connect(lambda: setattr(self, "choice", "start"))
        self.retryButton.clicked.connect(self._on_retry)
        self.cancelButton.clicked.connect(lambda: setattr(self, "choice", "cancel"))

    def _on_retry(self):
        self.choice = "retry"
        self.accept()


def _episode_files(folder, episodes):
    """Map each episode number to its downloaded file, or None if it is missing.

    Downloads are saved as "<profile> Ep<n>.<ext>"; the digit boundary stops "Ep5"
    from matching "Ep50", and a duplicate lands as "Ep5 (2).mp4".
    """
    import os
    import re
    found = {}
    try:
        videos = [os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(VIDEO_EXTENSIONS)]
    except Exception:
        videos = []
    for ep in sorted(set(episodes or [])):
        pattern = re.compile(rf"Ep{ep}(?!\d)", re.IGNORECASE)
        matches = sorted(v for v in videos if pattern.search(os.path.basename(v)))
        found[ep] = matches[0] if matches else None
    return found


def _play_first_video(folder, parent=None, episodes=None):
    """Open the first episode of the session that just finished.

    `episodes` is that session's episode numbers. Downloads are saved as
    "<profile> Ep<n>.<ext>", so we look for those specific numbers in order and play
    the earliest one present -- otherwise a folder already holding episodes 1-4 would
    start at 1 when the user just downloaded 5-10. Falls back to the naturally first
    video (then the folder itself) when nothing matches.
    """
    import os
    import re
    from qfluentwidgets import InfoBar, InfoBarPosition

    def warn(title, content):
        InfoBar.warning(title=title, content=content, orient=Qt.Orientation.Horizontal,
                        isClosable=True, position=InfoBarPosition.TOP,
                        duration=4000, parent=parent)

    if not folder or not os.path.isdir(folder):
        warn("Folder Not Found", "The download folder no longer exists.")
        return
    try:
        videos = [os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(VIDEO_EXTENSIONS)]
    except Exception:
        videos = []

    target = next((p for p in _episode_files(folder, episodes).values() if p), None)

    if target is None:
        def natural_key(path):
            name = os.path.basename(path)
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]
        target = sorted(videos, key=natural_key)[0] if videos else folder

    try:
        os.startfile(target)
    except Exception as e:
        warn("Playback Error", f"Could not open it: {e}")


class AppWindow(FluentWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(f"Auto Episodes Downloader | Version {APP_VERSION}")

        self.setMinimumSize(1100, 700)

        # Load saved window position/geometry
        desktop = QApplication.primaryScreen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        
        target_w = app_settings.get("window_width", 1100)
        target_h = app_settings.get("window_height", 800)
        target_x = app_settings.get("window_x", -1)
        target_y = app_settings.get("window_y", -1)
        
        if target_x == -1 or target_y == -1:
            target_x = w // 2 - target_w // 2
            target_y = h // 2 - target_h // 2
            
        if target_x < 0 or target_y < 0 or target_x > w or target_y > h:
            target_x, target_y = w // 2 - target_w // 2, h // 2 - target_h // 2
            
        self.setGeometry(target_x, target_y, target_w, target_h)

        # Defer maximization slightly to prevent any Windows 7 title bar flash
        if app_settings.get("window_maximized", False):
            QTimer.singleShot(50, self.maximize_window)

        self.downloader_interface = DownloaderWidget()
        self.search_interface = AnimeSearchWidget(self)
        self.progress_interface = ProgressTab()
        self.manager_interface = SiteManagerWidget()
        self.history_interface = HistoryWidget()
        self.watchlist_interface = WatchlistWidget()

        self.downloader_interface.setObjectName("downloader_interface")
        self.search_interface.setObjectName("search_interface")
        self.progress_interface.setObjectName("progress_interface")
        self.manager_interface.setObjectName("manager_interface")
        self.history_interface.setObjectName("history_interface")
        self.watchlist_interface.setObjectName("watchlist_interface")

        # Track if the progress tab has been added yet
        self.progress_added = False

        self.init_navigation()

        # Transparency (Mica/Acrylic) is forced OFF, regardless of system settings.
        self.setMicaEffectEnabled(False)
        self.navigationInterface.setAcrylicEnabled(False)
        
        # Disable the interface switching animation
        if hasattr(self, 'stackedWidget'):
            self.stackedWidget.setAnimationEnabled(False)
        
        # --- THE FIX: Wiring up our signals ---
        self._await_finish_dismiss = False   # finished tab is waiting to be dismissed
        signals.task_started.connect(self.show_active_tasks)
        signals.task_cancelled.connect(lambda: self.hide_active_tasks())

        # When downloads finish the tab stays put; it closes on "Start Watching" or
        # as soon as the user navigates elsewhere.
        signals.task_finished.connect(self.on_task_finished)
        self.progress_interface.watch_requested.connect(self.on_start_watching)
        self.stackedWidget.currentChanged.connect(self._dismiss_finished_tab_on_navigate)
        self.stackedWidget.currentChanged.connect(self._sync_manager_to_downloader)
        
        # Wire up the profile manager modifications to automatically update the downloader tab's dropdown list!
        self.manager_interface.profile_saved_signal.connect(self.downloader_interface.refresh_dropdown)
        self.search_interface.profile_created_signal.connect(self.on_search_profile_created)
        self.downloader_interface.goto_profiles_signal.connect(lambda: self.switchTo(self.manager_interface))
        self.history_interface.redownload_signal.connect(self.on_redownload_from_history)

        # Watchlist wiring: follow from Search, and download-new -> Downloader.
        self.search_interface.follow_signal.connect(self.on_follow_anime)
        self.watchlist_interface.download_new_signal.connect(self.on_watch_download_new)
        self.watchlist_interface.download_all_signal.connect(self.on_watch_download_all)
        self.watchlist_interface.new_episodes_found.connect(self.on_new_episodes_found)

        # Batch downloads run back-to-back: the engine handles one task at a time.
        self._download_queue = []
        signals.task_finished.connect(self._start_next_queued)
        signals.task_cancelled.connect(self._clear_download_queue)

        # Wire up the update signal
        signals.update_available.connect(self.prompt_update)

        # Auto-check the Watchlist shortly after launch (only if anything is followed).
        if get_watchlist():
            QTimer.singleShot(8000, self.watchlist_interface.check_all)

    def prompt_update(self, latest_version, download_url):
        print(f"[UI] prompt_update TRIGGERED for version {latest_version}")
        # Prevent spamming the prompt
        if getattr(self, '_update_prompted', False):
            print("[UI] Update already prompted this session. Skipping.")
            return
        self._update_prompted = True
        
        from qfluentwidgets import MessageBox, InfoBar, InfoBarPosition
        from PyQt6.QtWidgets import QProgressDialog
        from PyQt6.QtCore import Qt
        
        title = "Update Available"
        content = f"🚀 Version {latest_version} is available!\n\nWould you like to download and install it now?"
        
        print("[UI] Attempting to show MessageBox...")
        w = MessageBox(title, content, self)
        if w.exec():
            print("[UI] User clicked YES to update!")
            self.setEnabled(False)
            
            self.progress_dialog = QProgressDialog("Fetching new setup file from GitHub... Please wait.", "Cancel", 0, 100, self)
            self.progress_dialog.setWindowTitle("Downloading Update")
            self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self.progress_dialog.setCancelButton(None) # Disable cancel button
            self.progress_dialog.show()
            
            from core.updater import UpdateDownloaderThread, launch_setup_and_exit
            self.update_thread = UpdateDownloaderThread(download_url)
            self.update_thread.progress.connect(self.progress_dialog.setValue)
            
            def on_download_finished(setup_path):
                self.progress_dialog.close()
                launch_setup_and_exit(setup_path)
                
            def on_download_error(err):
                self.setEnabled(True)
                self.progress_dialog.close()
                InfoBar.error("Update Failed", f"Could not download the update: {err}", parent=self, position=InfoBarPosition.TOP)
                
            self.update_thread.finished.connect(on_download_finished)
            self.update_thread.error.connect(on_download_error)
            self.update_thread.start()

    def on_redownload_from_history(self, profile, episodes_str):
        self.switchTo(self.downloader_interface)
        self.downloader_interface.start_redownload(profile, episodes_str)

    def on_search_profile_created(self, _name):
        # Search created a profile (and set it as last_profile); refresh both the
        # Downloader dropdown and the Profile Manager list, then jump to the Downloader.
        self.downloader_interface.refresh_dropdown()
        self.manager_interface.refresh_combo()
        self.switchTo(self.downloader_interface)

    def on_watch_download_all(self, items):
        """Start a batch of Watchlist downloads; the rest are queued behind it."""
        self._download_queue = list(items)
        self._start_next_queued()

    def _start_next_queued(self, _results=None):
        if not self._download_queue:
            return
        title, template, domain, _max_ep, episodes_str = self._download_queue.pop(0)
        self.switchTo(self.downloader_interface)
        self.downloader_interface.start_watch_download(title, template, domain, episodes_str)

    def _clear_download_queue(self):
        """Cancelling one download abandons the whole batch."""
        self._download_queue = []

    def on_new_episodes_found(self, _count):
        """A Watchlist check turned up new episodes -> take the user straight there.

        Skipped while a download is running, so an auto-check can't yank the user off
        the Active Tasks screen mid-download.
        """
        if self.progress_added:
            return
        self.switchTo(self.watchlist_interface)

    def on_follow_anime(self, title, url, domain, cover):
        # A Search result was followed -> add to the Watchlist and jump there.
        self.watchlist_interface.follow(title, url, domain, cover)
        self.switchTo(self.watchlist_interface)

    def on_watch_download_new(self, title, template, domain, max_ep, episodes_str):
        # Download the new episodes directly -- no saved/duplicate profile is created.
        self.switchTo(self.downloader_interface)
        self.downloader_interface.start_watch_download(title, template, domain, episodes_str)

    def maximize_window(self):
        if hasattr(self, 'titleBar') and hasattr(self.titleBar, 'maxBtn'):
            self.titleBar.maxBtn.click()
        else:
            self.showMaximized()

    def init_navigation(self):
        # We only add Downloader, Manager, and History at startup!
        self.addSubInterface(self.downloader_interface, FIF.DOWNLOAD, "Downloader")
        self.addSubInterface(self.search_interface, FIF.SEARCH, "Search Anime")
        self.addSubInterface(self.watchlist_interface, FIF.HEART, "Watchlist")
        self.addSubInterface(self.manager_interface, FIF.SETTING, "Profile Manager")
        self.addSubInterface(self.history_interface, FIF.HISTORY, "History")

    def hide_active_tasks(self, switch_away=True):
        """Hides the Active Tasks tab completely"""
        self._await_finish_dismiss = False
        self.navigationInterface.setEnabled(True)
        if self.progress_added:
            # Only pull the user back to the Downloader if they are still standing on
            # the tab being removed -- otherwise leave them where they navigated to.
            if switch_away and self.stackedWidget.currentWidget() is self.progress_interface:
                self.switchTo(self.downloader_interface)
            self.navigationInterface.removeWidget(self.progress_interface.objectName())
            self.progress_added = False

    def show_active_tasks(self):
        """Dynamically injects and switches to the Active Tasks tab"""
        if hasattr(self, 'hide_timer') and self.hide_timer:
            self.hide_timer.stop()
            
        if not self.progress_added:
            self.addSubInterface(self.progress_interface, FIF.SYNC, "Active Tasks")
            self.progress_added = True
            
        self.navigationInterface.setEnabled(False)
        # Automatically jump to the progress screen!
        self.switchTo(self.progress_interface)

    def on_task_finished(self, _results=None):
        """Downloads finished: unlock navigation and KEEP the Active Tasks tab open.

        It used to vanish on a 2s timer, which threw the result away before it could
        be acted on. Now it stays until the user either presses "Start Watching" or
        navigates to another tab.
        """
        self.navigationInterface.setEnabled(True)
        self._await_finish_dismiss = True

    def _dismiss_finished_tab_on_navigate(self, _index=None):
        """Drop the finished Active Tasks tab once the user moves somewhere else."""
        if not getattr(self, "_await_finish_dismiss", False):
            return
        if self.stackedWidget.currentWidget() is self.progress_interface:
            return   # still looking at the result
        self._await_finish_dismiss = False
        self.hide_active_tasks(switch_away=False)

    def _sync_manager_to_downloader(self, _index=None):
        """Opening the profile manager shows the profile the downloader is set to.

        Without this the manager loads whichever profile is first in the config, so a
        user who picked a profile on the Downloader tab and came here to edit it was
        looking at a different one -- easy to edit the wrong profile by mistake.
        """
        if self.stackedWidget.currentWidget() is not self.manager_interface:
            return
        try:
            self.manager_interface.show_profile(
                self.downloader_interface.combo_site.currentText())
        except Exception:
            pass   # never let a dropdown sync block tab navigation

    def on_start_watching(self):
        """Play the episodes that were just downloaded, then close the finished tab.

        If part of the download never landed, ask first -- watching from a gap-ridden
        folder is rarely what the user wants, and retrying is usually one click.
        """
        folder = getattr(self.downloader_interface, "last_download_folder", "")
        episodes = getattr(self.downloader_interface, "last_download_episodes", [])
        missing = [ep for ep, path in _episode_files(folder, episodes).items() if not path]

        if missing:
            dlg = MissingEpisodesDialog(missing, len(episodes), self)
            dlg.exec()
            if dlg.choice == "retry":
                self._await_finish_dismiss = False
                self.downloader_interface.retry_episodes(missing)
                return
            if dlg.choice != "start":
                # Cancel: leave the finished tab and go back to the Downloader.
                self._await_finish_dismiss = False
                self.hide_active_tasks(switch_away=False)
                self.switchTo(self.downloader_interface)
                return

        self._await_finish_dismiss = False
        self.hide_active_tasks(switch_away=False)
        self.switchTo(self.downloader_interface)
        _play_first_video(folder, self, episodes)

    def closeEvent(self, event):
        app_settings["window_maximized"] = self.isMaximized()
        
        # Only save normal geometry if it's not minimized
        rect = self.normalGeometry()
        app_settings["window_width"] = rect.width()
        app_settings["window_height"] = rect.height()
        app_settings["window_x"] = rect.x()
        app_settings["window_y"] = rect.y()
            
        from utils.config import save_config
        try:
            save_config()
        except Exception:
            pass

        # Cancel any in-flight background detection threads so they return before
        # their C++ objects are destroyed (avoids "QThread destroyed while running").
        # Cancellation is cooperative (poll flags at loop boundaries), so a bounded
        # wait keeps close prompt even if one is mid request.
        threads = []
        try:
            threads += [t for t in getattr(self.search_interface, "_threads", []) if t and t.isRunning()]
        except Exception:
            pass
        for obj, attr in ((self.watchlist_interface, "_check_thread"),
                          (self.downloader_interface, "_conn_thread")):
            try:
                t = getattr(obj, attr, None)
                if t and t.isRunning():
                    threads.append(t)
            except Exception:
                pass
        for t in threads:
            try:
                t.requestInterruption()   # cooperative: threads bail at loop boundaries
            except Exception:
                pass

        # Tear the shared search browser down OFF the UI thread. This also unblocks
        # any in-flight search/detail get() (it raises once the driver quits), so
        # those threads return promptly. As a daemon it can't block exit.
        try:
            import threading
            from ui.search_tab import shutdown_shared_driver
            threading.Thread(target=shutdown_shared_driver, daemon=True).start()
        except Exception:
            pass

        # Bounded wait so cancellation lands (threads return) before their C++
        # objects are destroyed -- but never freeze close for long.
        import time as _t
        deadline = _t.time() + 3.0   # total budget across all threads
        for t in threads:
            try:
                t.wait(max(1, int((deadline - _t.time()) * 1000)))
            except Exception:
                pass

        super().closeEvent(event)