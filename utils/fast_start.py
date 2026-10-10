"""Startup cost trimming.

qfluentwidgets' common/image_utils.py imports, at import time:

    import numpy as np                      # + its OpenBLAS DLL (~35 MB to load)
    from colorthief import ColorThief       # which imports PIL
    from PIL import Image
    from scipy.ndimage.filters import gaussian_filter    # ~270 ms on its own

for exactly two things: AcrylicLabel's blur and DominantColor. This app uses
neither (Mica and Acrylic are forced off, no dominant-color lookups), yet those
imports were most of what importing qfluentwidgets cost on every launch.

`defer_scipy()` installs placeholders so those imports are free, then
`undefer_scipy()` steps back out of the way. It is a deferral, not a removal: the
first real use of any of them imports the genuine library at that moment and
hands off to it, so behaviour is unchanged -- only the cost moves off the startup
path. (The names are kept from when scipy was the only one deferred.)
"""

import importlib
import sys
import types

_STUBBED = ("scipy", "scipy.ndimage", "scipy.ndimage.filters")
# Deferred the same way. Parents before children.
_LAZY = ("numpy", "PIL", "PIL.Image", "colorthief")


def _drop_stubs(prefixes):
    for name in list(sys.modules):
        if name.split(".")[0] in prefixes and getattr(sys.modules.get(name), "__aed_stub__", False):
            del sys.modules[name]


class _LazyModule(types.ModuleType):
    """Stands in for a module until something actually uses it, then imports the
    real one and forwards to it."""
    __aed_stub__ = True

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        name = object.__getattribute__(self, "__name__")
        _drop_stubs({name.split(".")[0]})
        return getattr(importlib.import_module(name), attr)


def _lazy_colorthief(*args, **kwargs):
    """ColorThief, imported on first use (DominantColor -- never called here)."""
    _drop_stubs({"colorthief", "PIL"})
    # By name: colorthief comes with qfluentwidgets, it isn't one of ours.
    return importlib.import_module("colorthief").ColorThief(*args, **kwargs)


def _lazy_gaussian_filter(*args, **kwargs):
    """Load the real scipy on first actual use and delegate to it.

    scipy is excluded from the frozen build (48 MB of files that never execute -- the
    only caller is AcrylicLabel's blur, and Mica/Acrylic are forced off), so in the
    packaged app the import below simply isn't satisfiable. Blur is cosmetic, so the
    unblurred image is returned rather than raising: a missing decoration must never
    take the window down. Running from source, where scipy is installed, still gets
    the genuine filter.
    """
    for name in _STUBBED:
        mod = sys.modules.get(name)
        if getattr(mod, "__aed_stub__", False):
            del sys.modules[name]
    try:
        from scipy.ndimage import gaussian_filter
    except Exception:
        return args[0] if args else None
    return gaussian_filter(*args, **kwargs)


def _windows_theme():
    """darkdetect.theme() on Windows: "Dark", "Light", or None when unknown."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
    except OSError:
        return None
    return "Light" if light else "Dark"


def _fast_darkdetect():
    """darkdetect, without its import cost. Its __init__ calls platform.release()
    and platform.version(), which on Windows run a WMI query (~35 ms of every
    launch) just to confirm Windows 10+. theme/isDark/isLight read the same
    registry value directly; anything else (listener) loads the real darkdetect
    on first use."""
    if sys.platform != "win32" or "darkdetect" in sys.modules:
        return
    mod = _LazyModule("darkdetect")
    mod.theme = _windows_theme
    mod.isDark = lambda: None if _windows_theme() is None else _windows_theme() == "Dark"
    mod.isLight = lambda: None if _windows_theme() is None else _windows_theme() == "Light"
    sys.modules["darkdetect"] = mod


def _defer_image_libs():
    """Placeholders for numpy, PIL and colorthief -- each only if it isn't loaded
    for real already (then its import costs nothing anyway)."""
    installed = False
    for name in _LAZY:
        if name in sys.modules:
            continue
        mod = _LazyModule(name)
        if name == "PIL":
            mod.__path__ = []              # a package, so `from PIL import Image` works
        elif name == "colorthief":
            mod.ColorThief = _lazy_colorthief
        sys.modules[name] = mod
        if name == "PIL.Image" and isinstance(sys.modules.get("PIL"), _LazyModule):
            sys.modules["PIL"].Image = mod
        installed = True
    return installed


def defer_scipy():
    """Make qfluentwidgets' scipy, numpy, PIL and colorthief imports free. Call
    BEFORE importing it.

    Returns True if any placeholder was installed. Safe to call more than once,
    and a no-op for a library that is already loaded for real.
    """
    installed = _defer_image_libs()
    _fast_darkdetect()
    if any(m in sys.modules for m in _STUBBED):
        return installed

    for name in _STUBBED:
        mod = types.ModuleType(name)
        mod.__aed_stub__ = True
        if name == "scipy":
            mod.__path__ = []          # mark as a package so submodules resolve
        elif name == "scipy.ndimage":
            mod.__path__ = []
            mod.gaussian_filter = _lazy_gaussian_filter
        else:
            mod.gaussian_filter = _lazy_gaussian_filter
        sys.modules[name] = mod
    return True


def undefer_scipy():
    """Remove the placeholders so any later real import gets the real library.

    Call once qfluentwidgets has been imported: it has already bound the lazy
    objects, which keep working regardless.
    """
    for name in reversed(_STUBBED + _LAZY + ("darkdetect",)):
        mod = sys.modules.get(name)
        if getattr(mod, "__aed_stub__", False):
            del sys.modules[name]
