"""The "What's new" window: shown once, on the first launch after an update.

One page per feature of the new version (or of each version since the one last
shown), straight from the bundled CHANGELOG.md (utils/changelog.py): its emoji,
title and points. Next / the pips / the arrow keys move between pages; "Show me"
closes the window on the tab the feature is about. Closing it any way counts as
seen. It can be opened again from "Check for updates" when the app is up to date.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (BodyLabel, FluentIcon as FIF, MessageBoxBase, PipsPager,
                            PipsScrollButtonDisplayMode, PushButton, SmoothScrollArea,
                            SubtitleLabel)

from utils import changelog
from utils.config import APP_VERSION, app_settings, config_lock, save_config

# Which tab a feature is about, by words in its title: (words, tab, button text).
# First match wins, so the specific come before the general.
TARGETS = (
    ("anime news", "news_interface", "Open Anime news"),
    ("skip opening", "player_interface", "Open Video Player"),
    ("video player", "player_interface", "Open Video Player"),
    ("free up space", "watch_later_interface", "Open the Library"),
    ("library", "watch_later_interface", "Open the Library"),
    ("watchlist", "watchlist_interface", "Open the Watchlist"),
    ("search", "search_interface", "Open Search"),
    ("profile", "manager_interface", "Open Profile Manager"),
    ("witanime", "manager_interface", "Open Profile Manager"),
    ("download", "downloader_interface", "Open the Downloader"),
)


def target_for(title):
    """(tab key, button text) for a feature title, or None."""
    low = (title or "").lower()
    for words, key, label in TARGETS:
        if words in low:
            return key, label
    return None


def pages_from(versions):
    """[(version, date, feature)] in the order shown: newest version first."""
    return [(v, d, f) for v, d, feats in versions for f in feats]


def _long_date(iso):
    """"3 November 2026" from "2026-11-03"."""
    import datetime
    try:
        d = datetime.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d.strftime('%B %Y')}"


class FeaturePage(QWidget):
    show_me = pyqtSignal(str)            # tab key

    def __init__(self, version, date, feature, several_versions, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(14)
        tile = QLabel(feature.get("emoji") or "✨")
        tile.setFixedSize(52, 52)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setStyleSheet("background: rgba(255, 255, 255, 0.06); border-radius: 10px; "
                           "font-size: 24px;")
        head.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(4)
        title = feature.get("title") or "Other changes"
        is_new = title.lower().startswith("new:")
        top = QHBoxLayout()
        top.setSpacing(8)
        if is_new:
            title = title[4:].strip()
            # The QLabel.BadgeSuccess look, inline: a dialog's own stylesheet keeps
            # the app-wide class rule from reaching labels inside it.
            badge = QLabel("New")
            badge.setStyleSheet("background: rgba(81, 207, 102, 0.15); color: #51cf66; "
                                "border: 1px solid rgba(81, 207, 102, 0.3); border-radius: 10px; "
                                "padding: 2px 8px; font-size: 11px; font-weight: bold;")
            top.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = QLabel(title[:1].upper() + title[1:])
        self.title.setWordWrap(True)
        self.title.setStyleSheet("color: #ffffff; background: transparent; font-size: 17px; "
                                 "font-weight: 600;")
        top.addWidget(self.title, 1)
        names.addLayout(top)
        when = [f"Version {version}"] if several_versions else []
        if date and several_versions:
            when.append(_long_date(date))
        if when:
            sub = QLabel(" · ".join(p for p in when if p))
            sub.setStyleSheet("color: #999999; background: transparent; font-size: 12px;")
            names.addWidget(sub)
        head.addLayout(names, 1)
        root.addLayout(head)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 10, 0)
        col.setSpacing(10)
        col.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.points = []
        for point in feature.get("points") or []:
            row = QHBoxLayout()
            row.setSpacing(10)
            dot = QLabel("•")
            dot.setStyleSheet("color: #4cc2ff; background: transparent; font-size: 14px;")
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
            text = QLabel(point)
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setWordWrap(True)
            text.setOpenExternalLinks(True)
            text.setStyleSheet("color: #dddddd; background: transparent; font-size: 14px;")
            row.addWidget(text, 1)
            col.addLayout(row)
            self.points.append(text)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        target = target_for(feature.get("title"))
        self.button = None
        if target:
            key, label = target
            self.button = PushButton(FIF.RIGHT_ARROW, label)
            self.button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.button.clicked.connect(lambda: self.show_me.emit(key))
            root.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)


class WhatsNewDialog(MessageBoxBase):
    """Pages of features. `opened` (a tab key) is set when "Show me" closed it."""

    def __init__(self, versions, parent=None):
        super().__init__(parent)
        self.opened = None
        self._closing_to = False
        self._index = -1
        pages = pages_from(versions)
        several = len(versions) > 1

        head = QHBoxLayout()
        if several:
            heading = f"What's new since version {self._previous(versions)}"
        else:
            heading = f"What's new in version {versions[0][0]}"
        head.addWidget(SubtitleLabel(heading), 1)
        self.counter = BodyLabel("")
        self.counter.setStyleSheet("color: #999999;")
        head.addWidget(self.counter, 0, Qt.AlignmentFlag.AlignVCenter)
        self.viewLayout.addLayout(head)
        if not several and versions[0][1]:
            released = QLabel(f"Released {_long_date(versions[0][1])}")
            released.setStyleSheet("color: #999999; background: transparent; font-size: 13px;")
            self.viewLayout.addWidget(released)

        self.stack = QStackedWidget()
        self.stack.setObjectName("whatsNewPages")
        self.stack.setStyleSheet("#whatsNewPages, #whatsNewPages > QWidget { background: transparent; }")
        self.stack.setMinimumHeight(260)
        self.pages = []
        for version, date, feature in pages:
            page = FeaturePage(version, date, feature, several)
            page.show_me.connect(self._show_me)
            self.stack.addWidget(page)
            self.pages.append(page)
        self.viewLayout.addWidget(self.stack, 1)

        self.pager = PipsPager(Qt.Orientation.Horizontal)
        self.pager.setPageNumber(len(pages))
        self.pager.setVisibleNumber(min(len(pages), 9))
        self.pager.setNextButtonDisplayMode(PipsScrollButtonDisplayMode.ALWAYS)
        self.pager.setPreviousButtonDisplayMode(PipsScrollButtonDisplayMode.ALWAYS)
        self.pager.currentIndexChanged.connect(self.go)
        self.pager.setVisible(len(pages) > 1)
        self.viewLayout.addWidget(self.pager, 0, Qt.AlignmentFlag.AlignHCenter)

        self.cancelButton.setText("Close")
        self.widget.setMinimumWidth(640)
        self.go(0)

    @staticmethod
    def _previous(versions):
        """The version before the oldest one shown, as the user knew it."""
        seen = (app_settings.get("whats_new_seen") or "").strip()
        return seen or versions[-1][0]

    def go(self, index):
        index = max(0, min(index, len(self.pages) - 1))
        if index == self._index:
            return
        # The index is kept here: PipsPager.currentIndex() fails under PyQt6 in
        # this qfluentwidgets version. setCurrentIndex() calls go() back, which
        # then returns above.
        self._index = index
        self.stack.setCurrentIndex(index)
        self.pager.setCurrentIndex(index)
        last = index == len(self.pages) - 1
        self.yesButton.setText("Got it" if last else "Next")
        self.counter.setText(f"{index + 1} of {len(self.pages)}" if len(self.pages) > 1 else "")

    def validate(self):
        """Next turns the page; only the last page's button closes."""
        if self._closing_to:
            return True
        index = self._index
        if index < len(self.pages) - 1:
            self.go(index + 1)
            return False
        return True

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.go(self._index + 1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.go(self._index - 1)
        else:
            super().keyPressEvent(event)

    def _show_me(self, key):
        self.opened = key
        self._closing_to = True
        self.accept()


def mark_seen(version=APP_VERSION):
    with config_lock:
        app_settings["whats_new_seen"] = version
    save_config()


def pending():
    """The versions to show on this launch, from the bundled changelog."""
    return changelog.whats_new(changelog.read_bundled(),
                               app_settings.get("whats_new_seen") or "", APP_VERSION)


def show(window, versions, open_tab):
    """Show the window for `versions` over `window`; afterwards it counts as seen,
    and "Show me" opens its tab through open_tab(key)."""
    if not versions:
        return None
    dlg = WhatsNewDialog(versions, window)
    dlg.exec()
    mark_seen()
    if dlg.opened:
        open_tab(dlg.opened)
    return dlg
