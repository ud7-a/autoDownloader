from time import sleep
import sys
import os
import time
import threading
from PyQt6.QtWidgets import QApplication, QSplashScreen
from PyQt6.QtCore import Qt, qInstallMessageHandler, QtMsgType
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont
from utils.config import APP_VERSION

def suppress_qt_warnings(msg_type, context, message):
    # Filter out common annoying style sheet warnings from QFluentWidgets / Qt
    ignored_phrases = [
        "does not have a property named",
        "Unknown property",
        "Unable to set geometry",
        "QApplication: QSS contains",
        "📢 Tips: QFluentWidgets Pro is now released. Click https://qfluentwidgets.com/pages/pro to learn more about it."
    ]
    if any(phrase in message for phrase in ignored_phrases):
        return
        
    # Write other messages to standard stderr/stdout
    if msg_type == QtMsgType.QtDebugMsg:
        sys.stdout.write(f"Debug: {message}\n")
    elif msg_type == QtMsgType.QtInfoMsg:
        sys.stdout.write(f"Info: {message}\n")
    elif msg_type == QtMsgType.QtWarningMsg:
        sys.stderr.write(f"Warning: {message}\n")
    elif msg_type == QtMsgType.QtCriticalMsg:
        sys.stderr.write(f"Critical: {message}\n")
    elif msg_type == QtMsgType.QtFatalMsg:
        sys.stderr.write(f"Fatal: {message}\n")
        sys.exit(-1)

# Silently suppress visual parsing warnings to keep the console clean
qInstallMessageHandler(suppress_qt_warnings)

def create_splash_pixmap():
    # Make a clean dark mode splash screen matching the Fluent Dark theme
    pixmap = QPixmap(580, 320)
    pixmap.fill(Qt.GlobalColor.transparent) # Transparent background for rounded corners
    
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    
    # Clip path for rounded corners
    from PyQt6.QtGui import QPainterPath
    path = QPainterPath()
    path.addRoundedRect(0, 0, 580, 320, 12, 12)
    painter.setClipPath(path)
    
    # Fill main background
    painter.fillPath(path, QColor("#1e1e1e"))
    
    # Draw custom premium accent line at the top
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4cc2ff")) # Custom blue accent color
    painter.drawRect(0, 0, 580, 6) # Thin accent line
    
    # Draw app title
    font_title = QFont("Segoe UI Variable Display", 26, QFont.Weight.Bold)
    painter.setFont(font_title)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(40, 100, "Auto Episodes Downloader")
    
    # Draw subtitle/version info
    font_sub = QFont("Segoe UI Variable Text", 12, QFont.Weight.Medium)
    painter.setFont(font_sub)
    painter.setPen(QColor("#aaaaaa"))
    painter.drawText(40, 135, f"Version {APP_VERSION}  •  High-Speed Downloader")
    
    # Draw loading message
    font_loading = QFont("Segoe UI Variable Small", 10)
    painter.setFont(font_loading)
    painter.setPen(QColor("#4cc2ff"))
    painter.drawText(40, 260, "Loading system modules...")
    
    painter.end()
    return pixmap

def cleanup_old_exe():
    if getattr(sys, 'frozen', False):
        old_exe_path = sys.executable + ".old"
        for attempt in range(30):
            try:
                os.chmod(old_exe_path, 0o777) # Strip Read-Only flag
                os.remove(old_exe_path)
                break
            except Exception:
                time.sleep(2)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # "Fusion" instantly removes ugly grey borders from dropdowns
    app.setStyle("Fusion") 
    
    # Run exe cleanup in the background
    threading.Thread(target=cleanup_old_exe, daemon=True).start()
    
    # Load system configurations and database
    from utils.config import load_config, PROFILE_DIR
    from utils.database import init_db
    
    init_db()
    load_config()
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)
        
    # Force Native Fluent Dark Mode
    from qfluentwidgets import setTheme, Theme, setThemeColor
    setTheme(Theme.DARK)
    setThemeColor('#4cc2ff') 
    
    # Load and apply the modern WinUI 3 custom stylesheet to standard controls
    from ui.styles import WIN11_QSS, generate_ui_icons
    check_icon, arrow_icon = generate_ui_icons()
    qss = WIN11_QSS.replace("ICON_CHECK", check_icon).replace("ICON_ARROW", arrow_icon)
    app.setStyleSheet(qss)
    
    # Globally set standard Segoe UI Variable font for typography layout matching WinUI 3
    font = QFont("Segoe UI Variable Text", 10)
    app.setFont(font)
    
    # Now import and instantiate the main app window
    from ui.app_window import AppWindow
    window = AppWindow()
    window.show()
    
    # Check for updates in the background ONLY after the window is fully initialized and listening!
    from core.updater import check_for_updates_silently
    threading.Thread(target=check_for_updates_silently, daemon=True).start()
    
    sys.exit(app.exec())