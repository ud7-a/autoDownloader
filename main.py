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

if "--watcher" in sys.argv:
    from core.watcher import run_watcher
    run_watcher()
    sys.exit(0)

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

    # Acquire Windows Named Mutex so only ONE GUI instance ever runs
    import ctypes
    from ctypes import wintypes
    _main_app_mutex = None
    if sys.platform == "win32":
        ERROR_ALREADY_EXISTS = 183
        # Every signature below is declared explicitly. The callback used to be
        # WINFUNCTYPE(c_bool, ...) while EnumWindowsProc returns BOOL -- a 4-byte int,
        # not a 1-byte bool. Returning the wrong width corrupted the stack, so opening
        # the app while it was already running killed the second instance with
        # STATUS_STACK_BUFFER_OVERRUN (0xC0000409) instead of focusing the first.
        # Handles are pointer-sized too: without a restype, ctypes truncates them to
        # 32 bits on a 64-bit build.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        _main_app_mutex = kernel32.CreateMutexW(None, False, "Local\\AED_Main_App_Running_Mutex")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # Another instance is already running -> bring it to front and exit
            try:
                ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                user32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
                user32.EnumWindows.restype = wintypes.BOOL
                user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
                user32.GetWindowTextLengthW.restype = ctypes.c_int
                user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                user32.GetWindowTextW.restype = ctypes.c_int
                for _fn in ("ShowWindow", "BringWindowToTop", "SetForegroundWindow", "SwitchToThisWindow"):
                    getattr(user32, _fn).restype = wintypes.BOOL
                user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.BringWindowToTop.argtypes = [wintypes.HWND]
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]

                def _enum_win_cb(hwnd, _lparam):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        # The title carries the version ("... | Version 4.4.0"), so match
                        # on the prefix rather than the whole string.
                        if buf.value.startswith("Auto Episodes Downloader"):
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            user32.BringWindowToTop(hwnd)
                            user32.SetForegroundWindow(hwnd)
                            user32.SwitchToThisWindow(hwnd, True)
                            return False   # found it; stop enumerating
                    return True

                # Held in a local so the trampoline cannot be collected mid-enumeration.
                _cb = ENUMPROC(_enum_win_cb)
                user32.EnumWindows(_cb, 0)
            except Exception:
                pass
            sys.exit(0)

    # Fetch any pending remote download commands from cloud
    from utils.config import cloud_fetch_commands, app_settings
    initial_commands = []
    if app_settings.get("cloud_notify_enabled"):
        initial_commands = cloud_fetch_commands()

    # If launched on Windows boot with --autostart and no commands exist -> exit immediately
    if "--autostart" in sys.argv and not initial_commands:
        sys.exit(0)

    # Now import and instantiate the main app window
    from ui.app_window import AppWindow
    _startup_mark("ui.app_window imported")
    window = AppWindow(autostart_commands=initial_commands)
    _startup_mark("AppWindow built")
    window.showNormal()
    window.raise_()
    window.activateWindow()
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = int(window.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    _startup_mark("window shown")

    # Check for updates in the background ONLY after the window is fully initialized and listening!
    from core.updater import check_for_updates_silently
    threading.Thread(target=check_for_updates_silently, daemon=True).start()
    
    exit_code = app.exec()

    # Release mutex on exit
    if _main_app_mutex:
        ctypes.windll.kernel32.CloseHandle(_main_app_mutex)

    # Launch lightweight background watcher (<25MB) if cloud notifications are enabled
    if app_settings.get("cloud_notify_enabled"):
        try:
            import subprocess
            watcher_pyw = os.path.abspath(os.path.join(os.path.dirname(__file__), "aed_watcher.pyw"))
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable
            if os.path.exists(watcher_pyw):
                subprocess.Popen([pythonw, watcher_pyw], cwd=os.path.dirname(__file__))
        except Exception:
            pass

    sys.exit(exit_code)