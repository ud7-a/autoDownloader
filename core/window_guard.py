"""
Window Guard for Hidden Background Browsers.

Ensures that background Chrome instances running on Windows:
1. Never steal keyboard/mouse/window focus from the user.
2. Never pop up visibly on screen (including popups opened via window.open / ads).
3. Do not appear in the Windows Taskbar or Alt+Tab switcher (WS_EX_TOOLWINDOW).
4. Do not activate when tabs are switched or opened (WS_EX_NOACTIVATE).
5. Immediately restore the user's active application window if focus is ever shifted.
"""

import sys
import time
import threading
import ctypes
from ctypes import wintypes

# Win32 Constants
SW_HIDE = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

if sys.platform == "win32":
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        GetWindowLongPtrW = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        GetWindowLongPtrW.restype = ctypes.c_ssize_t
        GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]

        SetWindowLongPtrW = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        SetWindowLongPtrW.restype = ctypes.c_ssize_t
        SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    except Exception:
        user32 = None
        kernel32 = None
        GetWindowLongPtrW = None
        SetWindowLongPtrW = None
        WNDENUMPROC = None
else:
    user32 = None
    kernel32 = None
    GetWindowLongPtrW = None
    SetWindowLongPtrW = None
    WNDENUMPROC = None


def restore_foreground(target_hwnd):
    """Restores focus to target_hwnd using AttachThreadInput to bypass Windows focus-locking."""
    if not target_hwnd or sys.platform != "win32" or not user32 or not kernel32:
        return False
    try:
        if not user32.IsWindow(target_hwnd):
            return False
        cur_fg = user32.GetForegroundWindow()
        if cur_fg == target_hwnd:
            return True
        cur_th = user32.GetWindowThreadProcessId(cur_fg, None)
        target_th = user32.GetWindowThreadProcessId(target_hwnd, None)
        my_th = kernel32.GetCurrentThreadId()
        att_c = att_t = False
        try:
            if cur_th and cur_th != my_th:
                att_c = bool(user32.AttachThreadInput(my_th, cur_th, True))
            if target_th and target_th != my_th:
                att_t = bool(user32.AttachThreadInput(my_th, target_th, True))
            user32.BringWindowToTop(target_hwnd)
            user32.SetForegroundWindow(target_hwnd)
        finally:
            if att_t:
                user32.AttachThreadInput(my_th, target_th, False)
            if att_c:
                user32.AttachThreadInput(my_th, cur_th, False)
        return user32.GetForegroundWindow() == target_hwnd
    except Exception:
        return False


def get_chrome_pids():
    """Returns set of all currently running chrome/chromedriver PIDs."""
    pids = set()
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info.get("name") or "").lower()
                if "chrome" in name:
                    pids.add(p.info["pid"])
            except Exception:
                pass
    except Exception:
        pass
    return pids


def get_descendant_pids(parent_pid):
    """Returns set containing parent_pid and all its child/descendant PIDs."""
    if not isinstance(parent_pid, int) or parent_pid <= 0:
        return set()
    pids = {parent_pid}
    try:
        import psutil
        proc = psutil.Process(parent_pid)
        for child in proc.children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    return pids


def get_session_chrome_pids(chromedriver_pid=None, profile_dir=None):
    """Discovers all PIDs belonging to this Selenium browser session."""
    pids = set()
    if isinstance(chromedriver_pid, int) and chromedriver_pid > 0:
        pids |= get_descendant_pids(chromedriver_pid)
    if profile_dir:
        try:
            import psutil
            pdir_str = str(profile_dir).lower()
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if "chrome" in name:
                        cmdline = " ".join(p.info.get("cmdline") or []).lower()
                        if pdir_str in cmdline:
                            pids.add(p.info["pid"])
                except Exception:
                    pass
        except Exception:
            pass
    return pids


class WindowGuard:
    """
    Guards hidden Chrome browser instances to guarantee zero focus stealing,
    zero visible popups on screen, and no taskbar presence.
    """
    def __init__(self, headless=True, profile_dir=None):
        self.headless = bool(headless) and (sys.platform == "win32") and (user32 is not None)
        self.profile_dir = profile_dir
        self.target_pids = set()
        self.existing_pids = set()
        self.chromedriver_pid = None
        self.saved_user_fg = 0
        self.last_user_fg = 0
        self.prelaunch = True
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start_prelaunch(self):
        """Starts guard thread before Chrome launches to capture and suppress initial window."""
        if not self.headless:
            return self
        try:
            self.saved_user_fg = user32.GetForegroundWindow()
            self.last_user_fg = self.saved_user_fg
            self.existing_pids = get_chrome_pids()
            self.prelaunch = True
            self._thread = threading.Thread(target=self._guard_loop, daemon=True, name="WindowGuardLoop")
            self._thread.start()
        except Exception:
            pass
        return self

    def attach_driver(self, driver):
        """
        Attaches to the initialized Selenium driver, maps its PIDs, wraps
        switch_to.window, and immediately suppresses all browser windows.
        """
        if not self.headless:
            return
        with self._lock:
            try:
                service = getattr(driver, "service", None)
                proc = getattr(service, "process", None)
                pid = getattr(proc, "pid", None)
                if isinstance(pid, int):
                    self.chromedriver_pid = pid
                self.target_pids = get_session_chrome_pids(self.chromedriver_pid, self.profile_dir)
                self.prelaunch = False
            except Exception:
                pass

        # Immediate synchronous sweep
        self.suppress_windows()

        # If user active window was changed to Chrome during startup, restore it
        if self.saved_user_fg:
            try:
                cur_fg = user32.GetForegroundWindow()
                if cur_fg != self.saved_user_fg:
                    restore_foreground(self.saved_user_fg)
            except Exception:
                pass

        # Wrap driver.switch_to.window so every tab switch is strictly guarded
        try:
            orig_switch = driver.switch_to.window
            def guarded_switch(handle):
                user_fg = self.get_safe_user_foreground()
                try:
                    res = orig_switch(handle)
                finally:
                    self.suppress_windows()
                    if user_fg:
                        restore_foreground(user_fg)
                return res
            driver.switch_to.window = guarded_switch
        except Exception:
            pass

        # Wrap driver.quit to stop guard thread automatically
        try:
            orig_quit = driver.quit
            def guarded_quit():
                self.stop()
                return orig_quit()
            driver.quit = guarded_quit
        except Exception:
            pass

        try:
            driver._window_guard = self
        except Exception:
            pass

    def get_safe_user_foreground(self):
        """Returns the current foreground window if it is NOT one of our Chrome windows."""
        if not user32:
            return 0
        try:
            fg = user32.GetForegroundWindow()
            if not fg or not user32.IsWindow(fg):
                return self.last_user_fg
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if pid.value in self.target_pids:
                return self.last_user_fg
            return fg
        except Exception:
            return self.last_user_fg

    def suppress_windows(self):
        """Enforces off-screen position, SW_HIDE, and WS_EX_NOACTIVATE on all our Chrome windows."""
        if not self.headless or not user32:
            return
        try:
            buf = ctypes.create_unicode_buffer(256)
            rect = RECT()
            pid = ctypes.c_ulong()
            fg = user32.GetForegroundWindow()

            with self._lock:
                target_pids = set(self.target_pids)
                existing_pids = set(self.existing_pids)
                prelaunch = self.prelaunch

            def _enum_cb(hwnd, _):
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                w_pid = pid.value

                is_ours = False
                if not prelaunch and target_pids:
                    is_ours = (w_pid in target_pids)
                else:
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    if rect.left <= -5000 and rect.top <= -5000:
                        is_ours = True
                    elif w_pid not in existing_pids:
                        user32.GetClassNameW(hwnd, buf, 256)
                        if "Chrome_WidgetWin" in buf.value:
                            is_ours = True

                if is_ours:
                    user32.GetClassNameW(hwnd, buf, 256)
                    cls = buf.value
                    if "Chrome_WidgetWin" in cls:
                        # 1. Non-activating and tool window styles (removes taskbar & Alt+Tab entry)
                        if GetWindowLongPtrW and SetWindowLongPtrW:
                            try:
                                ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                                if not (ex & WS_EX_NOACTIVATE):
                                    new_ex = (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
                                    SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex)
                                    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE)
                            except Exception:
                                pass

                        # 2. Position offscreen (-32000, -32000) if on or near screen
                        try:
                            user32.GetWindowRect(hwnd, ctypes.byref(rect))
                            if rect.left > -5000 or rect.top > -5000:
                                user32.SetWindowPos(hwnd, 0, -32000, -32000, 1920, 1080,
                                                    SWP_NOACTIVATE | SWP_NOZORDER)
                        except Exception:
                            pass

                        # 3. Hide window completely
                        try:
                            if user32.IsWindowVisible(hwnd):
                                user32.ShowWindow(hwnd, SW_HIDE)
                        except Exception:
                            pass

                        # 4. Restore user active window if Chrome stole foreground
                        if fg == hwnd and self.last_user_fg and self.last_user_fg != hwnd:
                            restore_foreground(self.last_user_fg)

                return True

            cb = WNDENUMPROC(_enum_cb)
            user32.EnumWindows(cb, 0)
        except Exception:
            pass

    def _guard_loop(self):
        last_pid_refresh = time.time()
        pid_c = ctypes.c_ulong()
        while not self._stop_event.is_set():
            try:
                # Track user's foreground window whenever user is active outside of Chrome
                cur_fg = user32.GetForegroundWindow()
                if cur_fg and user32.IsWindow(cur_fg):
                    user32.GetWindowThreadProcessId(cur_fg, ctypes.byref(pid_c))
                    if pid_c.value not in self.target_pids:
                        self.last_user_fg = cur_fg

                # Periodically refresh session PIDs to capture newly spawned renderers/popups
                now = time.time()
                if now - last_pid_refresh > 2.0:
                    last_pid_refresh = now
                    with self._lock:
                        if self.chromedriver_pid or self.profile_dir:
                            self.target_pids = get_session_chrome_pids(self.chromedriver_pid, self.profile_dir)

                self.suppress_windows()
            except Exception:
                pass
            time.sleep(0.035)

    def stop(self):
        """Stops the background guard loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass
