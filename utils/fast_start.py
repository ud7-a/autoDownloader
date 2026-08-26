"""Startup cost trimming.

qfluentwidgets does `from scipy.ndimage.filters import gaussian_filter` at import
time (common/image_utils.py). Importing scipy.ndimage costs ~270ms -- roughly a
sixth of the app's entire launch -- and the only consumer is AcrylicLabel's blur,
which this app never uses (Mica and Acrylic are forced off).

`defer_scipy()` installs a placeholder so that import is free, then steps back out
of the way. It is a deferral, not a removal: if anything ever really calls
gaussian_filter, the placeholder imports the genuine scipy at that moment and hands
off to it, so behaviour is unchanged -- only the cost moves off the startup path.
"""

import sys
import types

_STUBBED = ("scipy", "scipy.ndimage", "scipy.ndimage.filters")


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


def defer_scipy():
    """Make qfluentwidgets' scipy import a no-op. Call BEFORE importing it.

    Returns True if the placeholders were installed. Safe to call more than once,
    and a no-op if scipy is already loaded for real.
    """
    if any(m in sys.modules for m in _STUBBED):
        return False

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
    """Remove the placeholders so any later real `import scipy` gets the real one.

    Call once qfluentwidgets has been imported: it has already bound the lazy
    function, which keeps working regardless.
    """
    for name in reversed(_STUBBED):
        mod = sys.modules.get(name)
        if getattr(mod, "__aed_stub__", False):
            del sys.modules[name]
