import sys
import os
import time
import threading
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
from PyQt6.QtGui import QFont

def suppress_qt_warnings(msg_type, _context, message):
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

def cleanup_old_exe():
    if getattr(sys, 'frozen', False):
        old_exe_path = sys.executable + ".old"
        for _ in range(30):
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

    # Start background bootstrap of downloader and extractor engines
    from utils.tools_manager import ensure_aria2c, ensure_unrar
    threading.Thread(target=ensure_aria2c, daemon=True).start()
    threading.Thread(target=ensure_unrar, daemon=True).start()
        
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