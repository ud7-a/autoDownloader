from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import FluentWindow, FluentIcon as FIF

from ui.downloader_tab import DownloaderWidget
from ui.search_tab import AnimeSearchWidget
from ui.manager_tab import SiteManagerWidget
from ui.history_tab import HistoryWidget
from ui.progress_tab import ProgressTab
from ui.watchlist_tab import WatchlistWidget
from core.signals import signals
from utils.config import APP_VERSION, app_settings, get_watchlist

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
        signals.task_started.connect(self.show_active_tasks)
        signals.task_cancelled.connect(self.hide_active_tasks)
        
        # When a download successfully finishes, trigger the delayed auto-hide!
        signals.task_finished.connect(self.delayed_hide_active_tasks)
        
        # Wire up the profile manager modifications to automatically update the downloader tab's dropdown list!
        self.manager_interface.profile_saved_signal.connect(self.downloader_interface.refresh_dropdown)
        self.search_interface.profile_created_signal.connect(self.on_search_profile_created)
        self.downloader_interface.goto_profiles_signal.connect(lambda: self.switchTo(self.manager_interface))
        self.history_interface.redownload_signal.connect(self.on_redownload_from_history)

        # Watchlist wiring: follow from Search, and download-new -> Downloader.
        self.search_interface.follow_signal.connect(self.on_follow_anime)
        self.watchlist_interface.download_new_signal.connect(self.on_watch_download_new)

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
        # Search created a profile (and set it as last_profile); refresh the Downloader and jump there.
        self.downloader_interface.refresh_dropdown()
        self.switchTo(self.downloader_interface)

    def on_follow_anime(self, title, url, domain, cover):
        # A Search result was followed -> add to the Watchlist and jump there.
        self.watchlist_interface.follow(title, url, domain, cover)
        self.switchTo(self.watchlist_interface)

    def on_watch_download_new(self, title, template, domain, max_ep, episodes_str):
        # Create/reuse a profile for the followed anime and start its new episodes.
        name = self.search_interface._create_profile(title, template, max_ep, domain=domain)
        self.downloader_interface.refresh_dropdown()
        self.switchTo(self.downloader_interface)
        self.downloader_interface.start_redownload(name, episodes_str)

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

    def hide_active_tasks(self):
        """Hides the Active Tasks tab completely"""
        self.navigationInterface.setEnabled(True)
        if self.progress_added:
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

    def delayed_hide_active_tasks(self, _results=None):
        """Waits exactly 2 seconds, then smoothly hides the tab"""
        self.navigationInterface.setEnabled(True)
        if hasattr(self, 'hide_timer') and self.hide_timer:
            self.hide_timer.stop()
            
        self.hide_timer = QTimer()
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_active_tasks)
        self.hide_timer.start(2000)

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
        # Tear the shared search browser down OFF the UI thread. A synchronous
        # driver.quit() (or waiting on an in-flight search's lock) could otherwise
        # freeze the window while it closes; as a daemon thread it can't block exit.
        try:
            import threading
            from ui.search_tab import shutdown_shared_driver
            threading.Thread(target=shutdown_shared_driver, daemon=True).start()
        except Exception:
            pass
        super().closeEvent(event)