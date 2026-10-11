"""Seeing a key combination before any program's hotkey does (Windows, ctypes).

While the shortcut picker has focus, a combination that another program (or a
Start-menu shortcut) has registered would fire there instead of reaching the
picker: the picker never saw it, and the other program ran. A low-level keyboard
hook is called before hotkeys are matched, so for the short time it is
installed the picker gets every Ctrl / Alt combination and nothing else fires.

Only combinations with Ctrl, Alt or Shift held are taken; plain keys and the
modifiers themselves pass through untouched. Installed on focus, removed on
focus loss -- never left running.
"""
import ctypes
import sys
from ctypes import wintypes

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    u.SetWindowsHookExW.restype = wintypes.HHOOK
    u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT
    u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u.UnhookWindowsHookEx.restype = wintypes.BOOL
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    return u


class ComboHook:
    """on_combo(ctrl, alt, shift, vk) gets each Ctrl / Alt / Shift combination,
    which is swallowed; pass_through(ctrl, alt, shift, vk) -> True lets one go
    on (the picker's own live test needs its combination to reach Windows)."""

    def __init__(self, on_combo, pass_through=None):
        self.on_combo = on_combo
        self.pass_through = pass_through or (lambda *_a: False)
        self._hook = None
        self._proc = HOOKPROC(self._handle)      # kept: the hook calls it until removed
        self._down = set()                       # keys swallowed and still held

    @property
    def installed(self):
        return bool(self._hook)

    def install(self):
        if self._hook or sys.platform != "win32":
            return bool(self._hook)
        k32 = ctypes.WinDLL("kernel32")
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._u = _user32()
        self._hook = self._u.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                               k32.GetModuleHandleW(None), 0)
        return bool(self._hook)

    def remove(self):
        if self._hook:
            self._u.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._down.clear()

    def _held(self, vk):
        return bool(self._u.GetAsyncKeyState(vk) & 0x8000)

    def _handle(self, code, wparam, lparam):
        try:
            if code == 0:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                vk = kb.vkCode
                if wparam in (WM_KEYUP, WM_SYSKEYUP) and vk in self._down:
                    self._down.discard(vk)
                    return 1                     # its key-down never went on either
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN) and vk not in MODIFIER_VKS:
                    ctrl, alt, shift = self._held(0x11), self._held(0x12), self._held(0x10)
                    if (ctrl or alt or shift) and not self.pass_through(ctrl, alt, shift, vk):
                        if vk not in self._down:          # not an auto-repeat
                            self._down.add(vk)
                            self.on_combo(ctrl, alt, shift, vk)
                        return 1
        except Exception:
            pass                                 # never break the keyboard
        return self._u.CallNextHookEx(None, code, wparam, lparam)
