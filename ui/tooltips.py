"""Every hint in the app uses the Fluent tooltip, not Qt's white system box.

qfluentwidgets only draws its own tooltip on widgets that have a ToolTipFilter
installed, and the app has dozens of setToolTip() calls. Instead of wiring each
one, install_fluent_tooltips() puts one filter on the application: the first time
the mouse enters a widget that has a tooltip, that widget gets a ToolTipFilter.
Widgets that already have one (the navigation bar, the ones set up by hand) are
left alone.
"""
from PyQt6.QtCore import QObject, QEvent
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import ToolTipFilter, ToolTipPosition

SHOW_DELAY_MS = 300
_MARK = "_aed_fluent_tooltip"


class _AnyStateToolTipFilter(ToolTipFilter):
    """ToolTipFilter that also explains disabled controls. The stock filter skips
    them, but a hint on a greyed-out button is often the only place that says why
    it is greyed out -- and the native one it replaces would have been blocked."""

    def _canShowToolTip(self):
        parent = self.parent()
        return parent.isWidgetType() and bool(parent.toolTip())


def has_fluent_tooltip(widget):
    return any(isinstance(c, ToolTipFilter) for c in widget.children())


class _FluentToolTips(QObject):
    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Enter and isinstance(obj, QWidget) and not obj.property(_MARK):
            if obj.toolTip():
                obj.setProperty(_MARK, True)
                if not has_fluent_tooltip(obj):
                    # Application filters run before the widget's own, so the new
                    # filter still receives this Enter and starts its timer.
                    obj.installEventFilter(
                        _AnyStateToolTipFilter(obj, SHOW_DELAY_MS, ToolTipPosition.TOP))
        elif kind == QEvent.Type.ToolTip and isinstance(obj, QWidget) and obj.toolTip():
            # Never the native box; a widget not entered yet gets the Fluent one on
            # its next hover.
            return True
        return False


_instance = None


def install_fluent_tooltips(app):
    """Call once, after the QApplication exists."""
    global _instance
    if _instance is None:
        _instance = _FluentToolTips(app)
        app.installEventFilter(_instance)
    return _instance
