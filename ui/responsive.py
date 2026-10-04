"""Keep each tab readable at any window size.

On wide windows (2560 px and up) forms used to stretch edge to edge -- 1900 px
text fields with their buttons a screen away. WidthCap centres a tab's content in
a column no wider than `max_width` by growing the layout's side margins, and
passes mouse-wheel turns over those margins on to the tab's scroll area, so the
whole window still scrolls.
"""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractScrollArea


class WidthCap(QObject):
    def __init__(self, host, max_width, layout=None, scroll=None):
        super().__init__(host)
        self._host = host
        self._layout = layout or host.layout()
        self._max = max_width
        self._scroll = scroll
        m = self._layout.contentsMargins()
        self._base = (m.left(), m.top(), m.right(), m.bottom())
        host.installEventFilter(self)
        self._apply()

    def side_margin(self, width=None):
        """Extra margin on each side for a host of `width` (default: current)."""
        width = self._host.width() if width is None else width
        return max(0, (width - self._max) // 2)

    def _apply(self):
        extra = self.side_margin()
        left, top, right, bottom = self._base
        self._layout.setContentsMargins(left + extra, top, right + extra, bottom)
        # The extra margins must not count toward the tab's minimum size: Qt would
        # otherwise refuse to shrink a window that was once maximized on a wide
        # screen (min width ~2000 px). An explicit minimum stops the layout from
        # imposing its own (QLayout::SetDefaultConstraint).
        need = self._layout.minimumSize()
        self._host.setMinimumSize(max(0, need.width() - 2 * extra), need.height())

    def eventFilter(self, obj, event):
        if obj is self._host:
            if event.type() == QEvent.Type.Resize:
                self._apply()
            elif (event.type() == QEvent.Type.Wheel and self._scroll is not None
                    and self._scroll.isVisible()):
                self._scroll.wheelEvent(event)
                return True
        return False


def cap_width(interface, max_width):
    """Cap a tab's content width; its `scroll` attribute (if it has one) keeps
    receiving the wheel from the margins."""
    scroll = getattr(interface, "scroll", None)
    if not isinstance(scroll, QAbstractScrollArea):
        scroll = None
    if interface.layout() is None:
        return None
    return WidthCap(interface, max_width, scroll=scroll)
