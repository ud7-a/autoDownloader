"""Smooth opening animation for every dropdown and menu in the app.

qfluentwidgets opens a menu by sliding its top-level window down (or up) over
250 ms. Each frame it moves the translucent window, sets a new window mask, and
the menu's 30 px blur shadow is rendered again -- three expensive operations on
Windows, so ComboBox dropdowns visibly stutter, worst on a high-refresh monitor.

Here every menu animation type keeps its original placement logic but opens in
place and only fades in. Changing a window's opacity is composited by Windows
itself (no move, no mask, no shadow redraw), so it stays smooth.
"""
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
from qfluentwidgets.components.widgets.menu import (
    MenuAnimationManager, MenuAnimationType, DropDownMenuAnimationManager,
    PullUpMenuAnimationManager, FadeInDropDownMenuAnimationManager,
    FadeInPullUpMenuAnimationManager)

FADE_MS = 120


class _FadeInPlace:
    """Mixin: open at the final position and fade in. Placement (_endPosition)
    and view sizing (availableViewSize) come from the original manager."""

    def exec(self, pos):
        self.menu.move(self._endPosition(pos))
        self.menu.setWindowOpacity(0.0)
        fade = QPropertyAnimation(self.menu, b"windowOpacity", self)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setDuration(FADE_MS)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade = fade
        fade.start()

    def _onValueChanged(self):
        pass        # the slide's per-frame mask is what made it stutter


_REPLACED = (
    (MenuAnimationType.DROP_DOWN, DropDownMenuAnimationManager),
    (MenuAnimationType.PULL_UP, PullUpMenuAnimationManager),
    (MenuAnimationType.FADE_IN_DROP_DOWN, FadeInDropDownMenuAnimationManager),
    (MenuAnimationType.FADE_IN_PULL_UP, FadeInPullUpMenuAnimationManager),
)


def install_smooth_menus():
    """Swap the sliding menu animations for the fade. Safe to call more than once."""
    for kind, original in _REPLACED:
        current = MenuAnimationManager.managers.get(kind)
        if current is not None and issubclass(current, _FadeInPlace):
            continue
        MenuAnimationManager.managers[kind] = type(
            "Smooth" + original.__name__, (_FadeInPlace, original), {})
