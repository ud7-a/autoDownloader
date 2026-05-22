import sys
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon as FIF

from ui.downloader_tab import DownloaderWidget
from ui.manager_tab import SiteManagerWidget
from ui.history_tab import HistoryWidget
from ui.progress_tab import ProgressTab
from core.signals import signals
from utils.config import APP_VERSION, app_settings

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
        self.progress_interface = ProgressTab()
        self.manager_interface = SiteManagerWidget()
        self.history_interface = HistoryWidget()

        self.downloader_interface.setObjectName("downloader_interface")
        self.progress_interface.setObjectName("progress_interface")
        self.manager_interface.setObjectName("manager_interface")
        self.history_interface.setObjectName("history_interface")

        # Track if the progress tab has been added yet
        self.progress_added = False

        self.init_navigation()
        
        # Enable visual Acrylic Material blend on side navigation to match premium WinUI 3 design
        transparency_enabled = app_settings.get("transparency", True)
        if transparency_enabled:
            from utils.config import force_windows_transparency
            force_windows_transparency()
            
        self.setMicaEffectEnabled(transparency_enabled)
        self.navigationInterface.setAcrylicEnabled(transparency_enabled)
        
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

    def maximize_window(self):
        if hasattr(self, 'titleBar') and hasattr(self.titleBar, 'maxBtn'):
            self.titleBar.maxBtn.click()
        else:
            self.showMaximized()

    def init_navigation(self):
        # We only add Downloader, Manager, and History at startup!
        self.addSubInterface(self.downloader_interface, FIF.DOWNLOAD, "Downloader")
        self.addSubInterface(self.manager_interface, FIF.SETTING, "Profile Manager")
        self.addSubInterface(self.history_interface, FIF.HISTORY, "History")

    def hide_active_tasks(self):
        """Hides the Active Tasks tab completely"""
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
            
        # Automatically jump to the progress screen!
        self.switchTo(self.progress_interface)

    def delayed_hide_active_tasks(self, results=None):
        """Waits exactly 2 seconds, then smoothly hides the tab"""
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
        save_config()
        super().closeEvent(event)