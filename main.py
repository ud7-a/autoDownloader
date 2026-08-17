import sys
import os
import time
import threading

_T0 = time.time()   # first line of our own code; everything before is interpreter start


def _startup_mark(label):
    """Record how long startup has taken so far.

    Only active when AED_STARTUP_LOG names a file, so it costs nothing normally.
    Diagnosing a slow launch of the packaged .exe is otherwise guesswork: the
    frozen app cannot simply be profiled from a terminal.
    """
    path = os.environ.get("AED_STARTUP_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{(time.time() - _T0) * 1000:8.0f} ms  {label}\n")
    except Exception:
        pass


_startup_mark("python reached main.py")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
from PyQt6.QtGui import QFont

_startup_mark("PyQt6 imported")

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
    _startup_mark("QApplication created")

    # "Fusion" instantly removes ugly grey borders from dropdowns
    app.setStyle("Fusion")

    # App icon (title bar, taskbar, Alt-Tab). Resolve from the PyInstaller bundle
    # when frozen, else from the repo.
    from PyQt6.QtGui import QIcon
    _base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    _icon_path = os.path.join(_base, "assets", "app_icon.ico")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))

    # Run exe cleanup in the background
    threading.Thread(target=cleanup_old_exe, daemon=True).start()
    
    # Load system configurations and database
    from utils.config import load_config, PROFILE_DIR
    from utils.database import init_db
    
    init_db()
    load_config()
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)
    _startup_mark("config + database ready")

    # Start background bootstrap of downloader and extractor engines
    from utils.tools_manager import ensure_aria2c, ensure_unrar
    threading.Thread(target=ensure_aria2c, daemon=True).start()
    threading.Thread(target=ensure_unrar, daemon=True).start()
        
    # Force Native Fluent Dark Mode.
    # qfluentwidgets pulls in scipy.ndimage (~270ms) purely for an acrylic blur this
    # app never uses, so keep that off the launch path -- see utils.fast_start.
    from utils.fast_start import defer_scipy, undefer_scipy
    defer_scipy()
    from qfluentwidgets import setTheme, Theme, setThemeColor
    undefer_scipy()
    setTheme(Theme.DARK)
    setThemeColor('#4cc2ff')
    _startup_mark("qfluentwidgets imported + theme set")

    # Load and apply the modern WinUI 3 custom stylesheet to standard controls
    from ui.styles import WIN11_QSS, generate_ui_icons
    check_icon, arrow_icon = generate_ui_icons()
    qss = WIN11_QSS.replace("ICON_CHECK", check_icon).replace("ICON_ARROW", arrow_icon)
    app.setStyleSheet(qss)

    # Globally set standard Segoe UI Variable font for typography layout matching WinUI 3
    font = QFont("Segoe UI Variable Text", 10)
    app.setFont(font)
    _startup_mark("stylesheet + font applied")

    # Now import and instantiate the main app window
    from ui.app_window import AppWindow
    _startup_mark("ui.app_window imported")
    window = AppWindow()
    _startup_mark("AppWindow built")
    window.show()
    _startup_mark("window shown")

    # Check for updates in the background ONLY after the window is fully initialized and listening!
    from core.updater import check_for_updates_silently
    threading.Thread(target=check_for_updates_silently, daemon=True).start()
    
    sys.exit(app.exec())