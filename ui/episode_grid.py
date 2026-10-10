"""Episode picker for the paused screen.

One painted widget instead of a checkbox per episode: a 1000-episode run used to
build 1000 CheckBox widgets on every pause, which froze the UI and grew the panel
past the window. Painting costs the same at any size and only redraws what's visible.
"""
import math

from PyQt6.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QInputDevice, QPainter, QPen
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy
from qfluentwidgets import (SmoothScrollArea, LineEdit, PushButton, FluentIcon as FIF,
                            themeColor, getFont)

CELL_W = 54     # minimum; cells stretch to fill the row
CELL_H = 30
GAP = 6

# Kept episodes use the app's accent exactly like a PrimaryPushButton: solid
# themeColor() fill with dark text. Read at paint time so it follows setThemeColor.
ON_TEXT = QColor(0, 0, 0)
OFF_BG, OFF_BORDER, OFF_TEXT = QColor(255, 255, 255, 8), QColor(255, 255, 255, 22), QColor(255, 255, 255, 95)
HOVER_BORDER = QColor(255, 255, 255, 110)


class EpisodeGrid(QWidget):
    """Grid of episode cells. Click toggles; drag paints the same state across
    cells; Shift+click sets everything between the last click and here."""

    changed = pyqtSignal()
    # Unselected cells are struck through: on the paused screen they are skipped.
    # Watch later's grid marks watched episodes instead, where off means "not yet".
    STRIKE_OFF = True
    CELL_H = CELL_H                     # taller in grids that show a caption line

    def __init__(self, parent=None):
        super().__init__(parent)
        self._captions = {}             # episode -> small second line, e.g. its site
        self._eps = []
        self._sel = []
        self._hover = None
        self._anchor = None
        self._drag_state = None
        self._drag_last = None
        self._touch_pending = None       # episode a finger is on, toggled on lift
        # Same font as the Fluent widgets around it (CheckBox, buttons).
        self.setFont(getFont(13))
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    # ---- data
    def set_episodes(self, episodes, selected=None):
        self._eps = sorted(set(episodes))
        keep = set(self._eps if selected is None else selected)
        self._sel = [e in keep for e in self._eps]
        self._hover = self._anchor = None
        self.updateGeometry()
        self.update()

    def set_captions(self, captions):
        """A small line under each episode number ({episode: text})."""
        self._captions = dict(captions or {})
        self.update()

    def episodes(self):
        return list(self._eps)

    def selected(self):
        return [e for e, on in zip(self._eps, self._sel) if on]

    def skipped(self):
        return [e for e, on in zip(self._eps, self._sel) if not on]

    def set_selected(self, wanted):
        wanted = set(wanted)
        new = [e in wanted for e in self._eps]
        if new != self._sel:
            self._sel = new
            self.update()
            self.changed.emit()

    # ---- geometry
    @staticmethod
    def columns_for(width):
        return max(1, (int(width) + GAP) // (CELL_W + GAP))

    def heightForWidth(self, width):
        rows = math.ceil(len(self._eps) / self.columns_for(width)) if self._eps else 0
        return max(0, rows * (self.CELL_H + GAP) - GAP)

    def hasHeightForWidth(self):
        return True

    def sizeHint(self):
        w = 10 * (CELL_W + GAP) - GAP
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self):
        return QSize(CELL_W, self.CELL_H)

    def _layout(self):
        cols = self.columns_for(self.width())
        cell_w = (self.width() - (cols - 1) * GAP) / cols
        return cols, cell_w

    def _rect(self, i, cols, cell_w):
        row, col = divmod(i, cols)
        return QRectF(col * (cell_w + GAP), row * (self.CELL_H + GAP), cell_w, self.CELL_H)

    def index_at(self, pos):
        cols, cell_w = self._layout()
        col = int(pos.x() // (cell_w + GAP))
        row = int(pos.y() // (self.CELL_H + GAP))
        if pos.x() < 0 or pos.y() < 0 or col >= cols:
            return None
        i = row * cols + col
        return i if 0 <= i < len(self._eps) else None

    # ---- painting
    def paintEvent(self, event):
        if not self._eps:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = themeColor()
        accent_hover = accent.lighter(112)
        on_font = QFont(self.font())
        off_font = QFont(self.font())
        off_font.setStrikeOut(self.STRIKE_OFF)
        cols, cell_w = self._layout()
        # Only the rows inside the exposed area -- the rest is scrolled away.
        first = max(0, int(event.rect().top() // (self.CELL_H + GAP)) * cols)
        last = min(len(self._eps), (int(event.rect().bottom() // (self.CELL_H + GAP)) + 1) * cols)
        for i in range(first, last):
            r = self._rect(i, cols, cell_w).adjusted(0.5, 0.5, -0.5, -0.5)
            on = self._sel[i]
            hover = i == self._hover
            if on:
                fill = accent_hover if hover else accent
                p.setBrush(fill)
                p.setPen(QPen(fill, 1))
            else:
                p.setBrush(OFF_BG)
                p.setPen(QPen(HOVER_BORDER if hover else OFF_BORDER, 1))
            p.drawRoundedRect(r, 6, 6)
            p.setFont(on_font if on else off_font)
            p.setPen(ON_TEXT if on else OFF_TEXT)
            caption = self._captions.get(self._eps[i])
            if caption:
                number = QRectF(r.left(), r.top() + 2, r.width(), r.height() * 0.55)
                p.drawText(number, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                           str(self._eps[i]))
                small = QFont(self.font())
                small.setPixelSize(10)
                small.setStrikeOut(False)
                p.setFont(small)
                line = QRectF(r.left() + 3, number.bottom(), r.width() - 6,
                              r.bottom() - number.bottom() - 2)
                text = p.fontMetrics().elidedText(caption, Qt.TextElideMode.ElideRight,
                                                  int(line.width()))
                p.drawText(line, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)
            else:
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(self._eps[i]))
        p.end()

    # ---- mouse
    def _set_range(self, a, b, state):
        lo, hi = min(a, b), max(a, b)
        touched = False
        for i in range(lo, hi + 1):
            if self._sel[i] != state:
                self._sel[i] = state
                touched = True
        return touched

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        i = self.index_at(event.position())
        if i is None:
            return
        if self._from_touch(event):
            # A finger toggles on lift, not on contact: the touch may still turn
            # into a scroll (ui.touch), and then nothing should change.
            self._touch_pending = i
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None:
            changed = self._set_range(self._anchor, i, self._sel[self._anchor])
        else:
            self._drag_state = not self._sel[i]
            self._drag_last = i
            self._anchor = i
            changed = self._set_range(i, i, self._drag_state)
        self.update()
        if changed:
            self.changed.emit()

    @staticmethod
    def _from_touch(event):
        """Clicks Qt made from a finger tap: there is no hover to show for those."""
        device = event.device() if hasattr(event, "device") else None
        return device is not None and device.type() == QInputDevice.DeviceType.TouchScreen

    def mouseMoveEvent(self, event):
        i = self.index_at(event.position())
        hover = None if self._from_touch(event) else i
        if hover != self._hover:
            self._hover = hover
            self.update()
        if self._drag_state is None or i is None or i == self._drag_last:
            return
        # A fast drag skips cells; fill everything between, in episode order.
        changed = self._set_range(self._drag_last, i, self._drag_state)
        self._drag_last = i
        if changed:
            self.update()
            self.changed.emit()

    def mouseReleaseEvent(self, event):
        self._drag_state = None
        self._drag_last = None
        pending, self._touch_pending = self._touch_pending, None
        if pending is not None and self.index_at(event.position()) == pending:
            # A tap: lifted on the episode it started on (a scroll releases elsewhere).
            self._anchor = pending
            self._set_range(pending, pending, not self._sel[pending])
            self.update()
            self.changed.emit()
        if self._from_touch(event) and self._hover is not None:
            self._hover = None          # a finger leaves no pointer behind
            self.update()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)


class EpisodePicker(QWidget):
    """Summary + All/None, the scrollable grid (capped height), and a range box
    ("13-40, 45") that stays in sync with the grid."""

    changed = pyqtSignal()
    MIN_GRID_HEIGHT = CELL_H + GAP + 18         # at least ~1.5 rows on a short window

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        top = QHBoxLayout()
        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("color: #dddddd;")
        top.addWidget(self.lbl_summary, 1)
        top.setSpacing(8)
        self.btn_all = PushButton(FIF.ACCEPT, "All")
        self.btn_all.setToolTip("Keep every episode")
        self.btn_none = PushButton(FIF.REMOVE, "None")
        self.btn_none.setToolTip("Skip every episode")
        for btn in (self.btn_all, self.btn_none):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumWidth(96)
            top.addWidget(btn)
        v.addLayout(top)

        self.grid = EpisodeGrid()
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.setWidget(self.grid)
        self.grid.setStyleSheet("background: transparent;")
        # Grows into whatever height the paused panel has, then scrolls.
        v.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()
        lbl = QLabel("Download")
        lbl.setStyleSheet("color: #aaaaaa;")
        bottom.addWidget(lbl)
        self.edit_spec = LineEdit()
        self.edit_spec.setPlaceholderText("e.g. 13-40, 45")
        self.edit_spec.setClearButtonEnabled(True)
        bottom.addWidget(self.edit_spec, 1)
        v.addLayout(bottom)

        hint = QLabel("Click to skip or keep · drag across episodes · Shift+click for a range")
        hint.setStyleSheet("color: #888888; font-size: 11px;")
        v.addWidget(hint)
        # Few episodes: the grid stops at its content and the spare room goes here,
        # keeping the range box right under the last row. Stretch 0: it only gets
        # space once the grid (stretch 1) has reached its full content height.
        v.addStretch(0)

        self.btn_all.clicked.connect(lambda: self.grid.set_selected(self.grid.episodes()))
        self.btn_none.clicked.connect(lambda: self.grid.set_selected(()))
        self.grid.changed.connect(self._on_grid_changed)
        self.edit_spec.editingFinished.connect(self._on_spec_edited)

    def set_episodes(self, episodes, selected=None):
        self.grid.set_episodes(episodes, selected)
        self._on_grid_changed()
        self._fit_height()
        self.scroll.verticalScrollBar().setValue(0)

    def skipped(self):
        return self.grid.skipped()

    def _fit_height(self):
        width = self.scroll.viewport().width() or self.width()
        needed = self.grid.heightForWidth(width)
        self.grid.setMinimumHeight(needed)
        self.scroll.setMinimumHeight(min(needed + 2, self.MIN_GRID_HEIGHT))
        self.scroll.setMaximumHeight(needed + 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def _on_grid_changed(self):
        from ui.downloader_tab import compact_episode_spec
        sel = self.grid.selected()
        total = len(self.grid.episodes())
        skipped = total - len(sel)
        text = f"Downloading {len(sel)} of {total}"
        if skipped:
            text += f"  ·  <span style='color:#f39c12'>skipping {skipped}</span>"
        self.lbl_summary.setText(text)
        if not self.edit_spec.hasFocus():
            self.edit_spec.setText(compact_episode_spec(sel))
        self.changed.emit()

    def _on_spec_edited(self):
        from ui.downloader_tab import compact_episode_spec, spec_to_ranges
        text = self.edit_spec.text().strip()
        ranges = spec_to_ranges(text)
        if text and not ranges:
            # Nothing parseable -- put back what the grid really holds.
            self.edit_spec.setText(compact_episode_spec(self.grid.selected()))
            return
        wanted = {e for e in self.grid.episodes() if any(a <= e <= b for a, b in ranges)}
        self.grid.set_selected(wanted)
        self.edit_spec.setText(compact_episode_spec(self.grid.selected()))
