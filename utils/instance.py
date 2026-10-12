"""Talking to an already-running copy of the app. ctypes only -- this runs at the
very top of main.py, before Qt is imported, so a second launch that only has to
hand something over costs the interpreter's start and nothing else.

The running app's window listens for registered window messages (see
AppWindow.nativeEvent); a launch with --library, or a Play next launch whose
episode isn't downloaded, posts one and exits.
"""
import sys

MUTEX_NAME = "Local\\AED_Main_App_Running_Mutex"
# The main window's title is "Auto Episodes Downloader | Version X.Y.Z". The
# " | " matters: a File Explorer window open on C:\Auto Episodes Downloader is
# titled "Auto Episodes Downloader" too.
WINDOW_TITLE_PREFIX = "Auto Episodes Downloader | "
OPEN_LIBRARY_MESSAGE = "AED_OPEN_LIBRARY"
DOWNLOAD_NEXT_MESSAGE = "AED_DOWNLOAD_NEXT"

SYNCHRONIZE = 0x00100000
SW_RESTORE = 9


def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    u.RegisterWindowMessageW.restype = wintypes.UINT
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    return u


def message_id(name):
    """A registered window message (the same number in every process)."""
    if sys.platform != "win32":
        return 0
    return _user32().RegisterWindowMessageW(name)


def open_library_message_id():
    """The registered message the main window treats as "show the Library"."""
    return message_id(OPEN_LIBRARY_MESSAGE)


def app_is_running():
    """Another GUI copy holds the single-instance mutex (opened, never created)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if not handle:
        return False
    k.CloseHandle(handle)
    return True


def find_main_window():
    """The running app's main window handle, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    u = _user32()
    found = []
    # BOOL callback (4 bytes), not c_bool: the wrong width corrupts the stack
    # (see the instance check in main.py).
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumWindows.argtypes = [proc_type, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL

    def visit(hwnd, _lparam):
        length = u.GetWindowTextLengthW(hwnd)
        if length > 0 and u.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith(WINDOW_TITLE_PREFIX):
                found.append(hwnd)
                return False
        return True

    callback = proc_type(visit)          # held while enumerating
    u.EnumWindows(callback, 0)
    return found[0] if found else None


def forward_open_library():
    """If the app is already running, bring its window up on the Library and
    return True (this launch can exit). False when there is nothing to hand to."""
    return forward(OPEN_LIBRARY_MESSAGE)


def forward_download_next():
    """The Play next shortcut found the next episode isn't downloaded: the
    running app sets the Downloader up for it (AppWindow.download_next works out
    which anime from the Library, as this launch did)."""
    return forward(DOWNLOAD_NEXT_MESSAGE)


def forward(name):
    """Bring the running app's window up and post it the registered message
    `name`. True when handed over; False when there is nothing to hand to."""
    if not app_is_running():
        return False
    hwnd = find_main_window()
    if not hwnd:
        return False
    u = _user32()
    # This process was started by the hotkey, so Windows lets it hand the
    # foreground over -- the running app could not take it by itself.
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    u.SetForegroundWindow(hwnd)
    return bool(u.PostMessageW(hwnd, message_id(name), 0, 0))
