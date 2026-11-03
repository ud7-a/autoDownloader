"""The "What's new" window: shown once, on the first launch after an update.

One page per feature of the new version (or of each version since the one last
shown), from the bundled CHANGELOG.md (utils/changelog.py): a Fluent icon, the
title and the short "app:" line written for users; "More details" opens the full
notes. Next / the pips / the arrow keys move between pages; a page about a tab
has a button that closes the window there, where a notice offers to continue
with the next page. Closing it any way counts as seen. It
can be opened again from "Check for updates" when the app is up to date.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (FluentIcon as FIF, MessageBoxBase, PipsPager,
                            PipsScrollButtonDisplayMode, PushButton, SmoothScrollArea,
                            TransparentPushButton)

from utils import changelog
from utils.config import APP_VERSION, app_settings, config_lock, save_config

ACCENT = "#4cc2ff"

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

# The icon a feature gets, by words in its title (the changelog's emoji are not
# used: Windows draws them poorly). First match wins.
ICONS = (
    ("what's new", FIF.TAG),
    ("anime news", FIF.MEGAPHONE),
    ("skip opening", FIF.PAGE_RIGHT),         # SKIP_FORWARD draws "30 s"
    ("free up space", FIF.BROOM),
    ("video player", FIF.VIDEO),
    ("library", FIF.LIBRARY),
    ("witanime", FIF.CLOUD_DOWNLOAD),
    ("download", FIF.DOWNLOAD),
    ("notification", FIF.RINGER),
    ("faster", FIF.SPEED_HIGH),
    ("look", FIF.PALETTE),
    ("update", FIF.UPDATE),
    ("fix", FIF.DEVELOPER_TOOLS),
)


def target_for(title):
    """(tab key, button text) for a feature title, or None."""
    low = (title or "").lower()
    for words, key, label in TARGETS:
        if words in low:
            return key, label
    return None


def icon_for(title):
    low = (title or "").lower()
    return next((icon for words, icon in ICONS if words in low), FIF.INFO)


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


def _label(text, size, color="#ffffff", weight=400, rich=False):
    label = QLabel(text)
    label.setWordWrap(True)
    if rich:
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
    label.setStyleSheet(f"color: {color}; background: transparent; font-size: {size}px; "
                        f"font-weight: {weight};")
    return label


class FeaturePage(QWidget):
    show_me = pyqtSignal(str)            # tab key

    DETAILS_HEIGHT = 230                 # px the notes open into, then scroll

    def __init__(self, version, date, feature, several_versions, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(16)

        head = QHBoxLayout()
        head.setSpacing(18)
        tile = QLabel()
        tile.setFixedSize(64, 64)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setPixmap(icon_for(feature.get("title")).icon(color=QColor(ACCENT)).pixmap(30, 30))
        tile.setStyleSheet("background: rgba(76, 194, 255, 0.14); border-radius: 14px;")
        head.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)

        names = QVBoxLayout()
        names.setSpacing(6)
        title = feature.get("title") or "Other changes"
        top = QHBoxLayout()
        top.setSpacing(10)
        if title.lower().startswith("new:"):
            title = title[4:].strip() or "New feature"
            # The QLabel.BadgeSuccess look, inline: a dialog's own stylesheet keeps
            # the app-wide class rule from reaching labels inside it.
            badge = QLabel("New")
            badge.setStyleSheet("background: rgba(81, 207, 102, 0.15); color: #51cf66; "
                                "border: 1px solid rgba(81, 207, 102, 0.3); border-radius: 11px; "
                                "padding: 3px 10px; font-size: 12px; font-weight: bold;")
            top.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = _label(title[:1].upper() + title[1:], 22, weight=600)
        top.addWidget(self.title, 1)
        names.addLayout(top)
        if several_versions:
            when = " · ".join(p for p in (f"Version {version}", _long_date(date)) if p)
            names.addWidget(_label(when, 13, "#999999"))
        head.addLayout(names, 1)
        root.addLayout(head)

        # The short version, for everyone; the full notes on request.
        self.summary = _label(feature.get("summary") or "", 17, "#e8e8e8", rich=True)
        root.addWidget(self.summary)

        # Under it: the tab it's about, and the full notes on request.
        actions = QHBoxLayout()
        actions.setSpacing(12)
        target = target_for(feature.get("title"))
        self.button = None
        if target:
            key, label = target
            self.button = PushButton(FIF.RIGHT_ARROW, label)
            self.button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.button.setMinimumHeight(40)
            self.button.clicked.connect(lambda: self.show_me.emit(key))
            actions.addWidget(self.button)
        self.details_button = None
        self.details = None
        self.points = []
        if feature.get("points"):
            self.details_button = TransparentPushButton(FIF.CHEVRON_RIGHT_MED, "More details")
            self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.details_button.setMinimumHeight(40)
            self.details_button.clicked.connect(self.toggle_details)
            actions.addWidget(self.details_button)
        actions.addStretch(1)
        root.addLayout(actions)
        if feature.get("points"):
            self.details = SmoothScrollArea()
            self.details.setMaximumHeight(self.DETAILS_HEIGHT)
            self.details.setWidgetResizable(True)
            self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.details.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            host = QWidget()
            host.setStyleSheet("background: transparent;")
            col = QVBoxLayout(host)
            col.setContentsMargins(4, 0, 10, 0)
            col.setSpacing(10)
            col.setAlignment(Qt.AlignmentFlag.AlignTop)
            for point in feature["points"]:
                row = QHBoxLayout()
                row.setSpacing(10)
                row.addWidget(_label("•", 15, ACCENT), 0, Qt.AlignmentFlag.AlignTop)
                text = _label(point, 15, "#c8c8c8", rich=True)
                row.addWidget(text, 1)
                col.addLayout(row)
                self.points.append(text)
            self.details.setWidget(host)
            self.details.hide()
            root.addWidget(self.details)
        root.addStretch(1)

    def toggle_details(self):
        show = not self.details.isVisibleTo(self)
        self.details.setVisible(show)
        self.details_button.setText("Fewer details" if show else "More details")
        self.details_button.setIcon(FIF.CHEVRON_DOWN_MED if show else FIF.CHEVRON_RIGHT_MED)


class WhatsNewDialog(MessageBoxBase):
    """Pages of features. `opened` (a tab key) is set when "Show me" closed it."""

    MIN_WIDTH, MAX_WIDTH = 760, 980       # px; within these, 72% of the window

    def __init__(self, versions, parent=None, start=0):
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
        head.addWidget(_label(heading, 26, weight=600), 1)
        self.counter = _label("", 14, "#999999")
        self.counter.setWordWrap(False)
        head.addWidget(self.counter, 0, Qt.AlignmentFlag.AlignVCenter)
        self.viewLayout.addLayout(head)
        if not several and versions[0][1]:
            self.viewLayout.addWidget(_label(f"Released {_long_date(versions[0][1])}", 14, "#999999"))

        self.stack = QStackedWidget()
        self.stack.setObjectName("whatsNewPages")
        self.stack.setStyleSheet("#whatsNewPages, #whatsNewPages > QWidget { background: transparent; }")
        # Tall enough for a page with its details open: opening them doesn't
        # resize the window.
        self.stack.setMinimumHeight(FeaturePage.DETAILS_HEIGHT + 220)
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
        width = int(parent.width() * 0.72) if parent is not None else self.MIN_WIDTH
        self.widget.setFixedWidth(max(self.MIN_WIDTH, min(self.MAX_WIDTH, width)))
        self.go(start)

    @property
    def current(self):
        """The page showing (0-based)."""
        return self._index

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
        if self._index < len(self.pages) - 1:
            self.go(self._index + 1)
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


def show(window, versions, open_tab, start=0):
    """Show the window for `versions` over `window`, from page `start`; afterwards
    it counts as seen. A page's tab button opens its tab through open_tab(key), and
    when pages are left, a notice there offers to continue from the next one."""
    if not versions:
        return None
    dlg = WhatsNewDialog(versions, window, start)
    dlg.exec()
    mark_seen()
    if dlg.opened:
        open_tab(dlg.opened)
        if dlg.current < len(dlg.pages) - 1:
            offer_to_continue(window, versions, open_tab, dlg.current + 1, len(dlg.pages))
    return dlg


def offer_to_continue(window, versions, open_tab, start, total):
    """A notice at the bottom of the tab just opened: the tour isn't over. It stays
    until used or closed."""
    from qfluentwidgets import InfoBar, InfoBarPosition
    left = total - start
    bar = InfoBar.info("What's new", f"{left} more to see.", isClosable=True,
                       position=InfoBarPosition.BOTTOM, duration=-1, parent=window)
    button = PushButton(FIF.RIGHT_ARROW, "Continue")
    button.setCursor(Qt.CursorShape.PointingHandCursor)

    def resume():
        bar.close()
        show(window, versions, open_tab, start)
    button.clicked.connect(resume)
    bar.addWidget(button)
    return bar
