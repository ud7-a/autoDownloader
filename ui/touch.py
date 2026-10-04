"""Touch-screen support.

Windows hands finger input to Qt as touch, and Qt turns it into mouse events for
the widgets (none of them take touch directly). Taps therefore already work, but
a finger dragged across a page arrives as a mouse *drag*: it presses whatever is
under it instead of scrolling, so no tab could be scrolled by finger.

TouchScroller watches those finger-made mouse events only (a real mouse is never
affected). A drag that goes past a small threshold in a direction the page can
scroll becomes scrolling -- with a fling when the finger lifts -- and whatever
the finger first landed on is released without being clicked. Shorter touches
stay ordinary taps.

Qt's own QScroller was tried first: on real Windows touch input it scrolled, but
swallowed the lift of a tap on buttons inside the page (no click) and let a drag
toggle the episode it started on.
"""
import sys

from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QVariantAnimation, Qt
from PyQt6.QtGui import QInputDevice, QMouseEvent
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QWidget

DRAG_THRESHOLD = 12          # px of finger travel before a touch becomes a scroll
FLING_SECONDS = 0.35         # how far a fling coasts: velocity x this
FLING_MIN_SPEED = 300        # px/s below which lifting the finger just stops


def has_touch_screen():
    """Windows reports touch digitizers through GetSystemMetrics(SM_DIGITIZER)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        SM_DIGITIZER = 94
        NID_INTEGRATED_TOUCH, NID_EXTERNAL_TOUCH, NID_READY = 0x01, 0x02, 0x80
        value = ctypes.windll.user32.GetSystemMetrics(SM_DIGITIZER)
        return bool(value & NID_READY) and bool(value & (NID_INTEGRATED_TOUCH | NID_EXTERNAL_TOUCH))
    except Exception:
        return False


def is_touch(event):
    device = event.device() if hasattr(event, "device") else None
    return device is not None and device.type() == QInputDevice.DeviceType.TouchScreen


def _scroll_target(widget, vertical):
    """Innermost scroll area around `widget` that can scroll on that axis."""
    w = widget
    while w is not None:
        if isinstance(w, QAbstractScrollArea):
            bar = w.verticalScrollBar() if vertical else w.horizontalScrollBar()
            if bar is not None and bar.maximum() > bar.minimum():
                return bar
        w = w.parentWidget()
    return None


class TouchScroller(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._reset()
        self._fling = None

    def _reset(self):
        self._pressed = None       # widget the finger landed on
        self._start = None         # global position of the touch
        self._last = None
        self._bar = None           # scrollbar being driven, once scrolling
        self._vertical = True
        self._samples = []         # (timestamp ms, position on the axis) for the fling
        self._seen = None          # (type, timestamp) already handled (events propagate)

    def _stop_fling(self):
        if self._fling is not None:
            self._fling.stop()
            self._fling = None

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                         QEvent.Type.MouseButtonRelease):
            return False
        if not is_touch(event) or not isinstance(obj, QWidget):
            # The top-level QWindow sees each event before any widget does; only
            # widgets matter here (they are what gets pressed and scrolled).
            return False
        # An ignored mouse event is re-delivered to each parent; act on it once.
        key = (etype, event.timestamp())
        if self._seen == key:
            return self._bar is not None
        self._seen = key
        pos = event.globalPosition()

        if etype == QEvent.Type.MouseButtonPress:
            self._stop_fling()
            self._reset()
            self._seen = key
            self._pressed, self._start, self._last = obj, pos, pos
            return False

        if self._start is None:
            return False

        if etype == QEvent.Type.MouseMove:
            if self._bar is None:
                dx, dy = pos.x() - self._start.x(), pos.y() - self._start.y()
                if max(abs(dx), abs(dy)) < DRAG_THRESHOLD:
                    return False
                vertical = abs(dy) >= abs(dx)
                bar = _scroll_target(self._pressed, vertical)
                if bar is None:
                    return False          # nothing scrolls this way: let the widget have it
                self._bar, self._vertical = bar, vertical
                self._cancel_press()
            delta = (pos.y() - self._last.y()) if self._vertical else (pos.x() - self._last.x())
            self._bar.setValue(int(round(self._bar.value() - delta)))
            self._last = pos
            self._samples.append((event.timestamp(), pos.y() if self._vertical else pos.x()))
            self._samples = self._samples[-6:]
            return True

        # release: a scroll ends in a fling and eats the release; a tap goes through
        scrolling = self._bar is not None
        if scrolling:
            self._start_fling()
        self._start = None
        self._bar = None
        return scrolling

    def _cancel_press(self):
        """Release the pressed widget somewhere far outside it, so it lets go
        without counting a click."""
        w = self._pressed
        if w is None:
            return
        outside = QPointF(-10000, -10000)
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, outside, w.mapToGlobal(outside),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(w, release)
        self._pressed = None

    def _start_fling(self):
        if len(self._samples) < 2:
            return
        (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
        if t1 <= t0:
            return
        speed = (p1 - p0) / ((t1 - t0) / 1000.0)        # px/s, finger direction
        if abs(speed) < FLING_MIN_SPEED:
            return
        bar = self._bar
        start = bar.value()
        end = max(bar.minimum(), min(bar.maximum(), int(start - speed * FLING_SECONDS)))
        if end == start:
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(int(min(900, 250 + abs(end - start) * 0.6)))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v, b=bar: b.setValue(int(v)))
        anim.start()
        self._fling = anim


def install(app=None, force=False):
    """Turn on finger scrolling where a touch screen is present (or `force`).
    The filter sees every event, so PCs without touch don't get it."""
    app = app or QApplication.instance()
    if not (force or has_touch_screen()):
        return None
    if getattr(app, "_aed_touch", None) is None:
        app._aed_touch = TouchScroller(app)
        app.installEventFilter(app._aed_touch)
    return app._aed_touch
