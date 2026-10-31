"""Anime news: what's next for each anime in the Library.

One card per Library anime with its state from AniList (utils/anime_news.py):
airing now, a new season coming, waiting for news, or finished. A filter strip
above narrows the list; "Not the right anime?" fixes a wrong match; an airing
season has "Find in Search", which searches the anime sites for it. The app checks
once a day in the background and posts a Windows notification when a season is
announced, gets a date or starts airing (AppWindow._on_news_changes).
"""
import datetime
import time

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from qfluentwidgets import (Action, BodyLabel, FluentIcon as FIF, IndeterminateProgressRing,
                            InfoBar, InfoBarPosition, MessageBoxBase, PushButton, RadioButton,
                            RoundMenu, SearchLineEdit, SegmentedWidget, SimpleCardWidget,
                            SmoothScrollArea, SubtitleLabel, SwitchButton, ToolButton)

from utils import anime_news as news
from utils import watch_later as wl
from utils.config import app_settings, config_lock, save_config
from ui.watch_later_tab import _fixed, _muted, _poster, _title

# Filter strip: key, label, the states it shows.
FILTERS = (("all", "All", news.KINDS),
           ("airing", "Airing", (news.AIRING,)),
           ("coming", "Coming", (news.COMING,)),
           ("waiting", "Waiting", (news.WAITING, news.PAUSED)),
           ("finished", "Finished", (news.FINISHED,)))
# The app's badge styles (ui/styles.py, QLabel.Badge*).
BADGES = {news.AIRING: "BadgeInfo", news.COMING: "BadgeSuccess", news.WAITING: "BadgeWarning",
          news.PAUSED: "BadgeWarning", news.FINISHED: "BadgeNeutral", news.UNKNOWN: "BadgeNeutral"}
ORDER = {k: i for i, k in enumerate((news.AIRING, news.COMING, news.WAITING, news.PAUSED,
                                     news.FINISHED, news.UNKNOWN))}


def checked_text(ts, now=None):
    """'checked today at 09:12', 'checked 30 Oct at 09:12', or 'not checked yet'."""
    if not ts:
        return "not checked yet"
    when = datetime.datetime.fromtimestamp(ts)
    today = datetime.datetime.fromtimestamp(now or time.time()).date()
    day = "today" if when.date() == today else f"{when.day} {when.strftime('%b')}"
    return f"checked {day} at {when.strftime('%H:%M')}"


def library_shows():
    """The Library anime the page covers: every entry with a title and a link."""
    return [e for e in wl.entries() if e.get("url") and e.get("title")]


class CheckThread(QThread):
    done = pyqtSignal(list, list)          # [(title, notice)], [(title, error)]

    def __init__(self, entries, force):
        super().__init__()
        self.entries, self.force = entries, force

    def run(self):
        try:
            _shows, changes, errors = news.check(self.entries, force=self.force,
                                                 stop=self.isInterruptionRequested)
        except Exception as e:                     # never let a thread take the app down
            changes, errors = [], [("AniList", f"{type(e).__name__}: {e}")]
        self.done.emit(changes, errors)


class SearchThread(QThread):
    results = pyqtSignal(list, str)        # media list, error ("" when fine)

    def __init__(self, query):
        super().__init__()
        self.query = query

    def run(self):
        try:
            self.results.emit(news.search(self.query), "")
        except Exception as e:
            self.results.emit([], str(e))


class MatchDialog(MessageBoxBase):
    """Pick the AniList entry a Library anime really is."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.picked = None
        self._threads = []
        self._found = []
        self.viewLayout.addWidget(SubtitleLabel("Which anime is this?"))
        lead = BodyLabel(f"Pick the AniList entry that “{title}” is. Its news follows that "
                         "entry from now on.")
        lead.setWordWrap(True)
        self.viewLayout.addWidget(lead)
        self.search_box = SearchLineEdit()
        self.search_box.setText(news._norm(title) or title)
        self.search_box.searchSignal.connect(self._search)       # Enter or the icon
        self.viewLayout.addWidget(self.search_box)
        self.status = _muted("Searching…")
        self.viewLayout.addWidget(self.status)
        self.list_host = QWidget()
        self.list_col = QVBoxLayout(self.list_host)
        self.list_col.setContentsMargins(0, 0, 0, 0)
        self.list_col.setSpacing(2)
        self.viewLayout.addWidget(self.list_host)
        self.group = QButtonGroup(self)
        self.group.idClicked.connect(self._on_pick)
        self.yesButton.setText("Use this")
        self.yesButton.setEnabled(False)
        self.cancelButton.setText("Cancel")
        self.widget.setMinimumWidth(560)
        self._search(self.search_box.text())

    def _search(self, query):
        query = (query or "").strip()
        if not query:
            return
        self.status.setText("Searching…")
        self.status.show()
        t = SearchThread(query)
        t.results.connect(self._show)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _show(self, found, error):
        for b in self.group.buttons():
            self.group.removeButton(b)
            b.deleteLater()
        self.picked = None
        self.yesButton.setEnabled(False)
        self._found = [m for m in found if m.get("format") in news.SERIES_FORMATS] or found
        if error:
            self.status.setText(error)
        elif not self._found:
            self.status.setText("Nothing found. Try another name.")
        else:
            self.status.hide()
        for i, m in enumerate(self._found[:8]):
            btn = RadioButton(f"{news.display_title(m)}   ·   {news.candidate_line(m)}")
            self.group.addButton(btn, i)
            self.list_col.addWidget(btn)

    def _on_pick(self, index):
        self.picked = self._found[index]
        self.yesButton.setEnabled(True)

    def done(self, result):
        for t in self._threads:
            t.wait(3000)
        super().done(result)


class NewsRow(SimpleCardWidget):
    pick = pyqtSignal(str)                 # url -- choose the AniList entry
    recheck = pyqtSignal(str)              # url -- check this show now
    find = pyqtSignal(str)                 # url -- look the airing season up in Search

    def __init__(self, entry, record, parent=None):
        super().__init__(parent)
        self.url = entry.get("url", "")
        state = (record or {}).get("state") or {}
        kind = state.get("kind")
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 14, 10)
        root.setSpacing(14)
        root.addWidget(_poster(entry.get("cover", ""), 56, 84))

        info = QVBoxLayout()
        info.setSpacing(4)
        root.addLayout(info, 1)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(_title(entry.get("title", "Anime"), 11))
        badge = QLabel(news.KIND_LABELS.get(kind, "Not checked yet"))
        badge.setProperty("class", BADGES.get(kind, "BadgeNeutral"))
        top.addWidget(badge)
        if news.is_fresh(record):
            new = QLabel("New since last check")
            new.setStyleSheet("color: #4cc2ff; background: transparent; font-size: 12px; "
                              "font-weight: 600;")
            top.addWidget(new)
        top.addStretch(1)
        info.addLayout(top)
        headline = QLabel(state.get("headline") or "Waiting for the first check.")
        headline.setWordWrap(True)
        headline.setStyleSheet("color: #e6e6e6; background: transparent; font-size: 14px;")
        info.addWidget(headline)
        detail = state.get("detail") or ""
        match = (record or {}).get("match_title")
        if match:
            detail += (" · " if detail else "") + f"matched to {match}"
        if detail:
            info.addWidget(_muted(detail, 12, wrap=True))

        if state.get("when"):
            when = QLabel(state["when"])
            when.setStyleSheet("color: #e6e6e6; background: transparent; font-size: 13px;")
            root.addWidget(when)
        if kind == news.AIRING:
            # Airing means the sites have its episodes: one click to the season's
            # page in Search, to follow it in the Watchlist or download it.
            self.btn_find = _fixed(PushButton(FIF.SEARCH, "Find in Search"))
            self.btn_find.setToolTip("Search the anime sites for this season")
            self.btn_find.clicked.connect(lambda: self.find.emit(self.url))
            root.addWidget(self.btn_find)
        else:
            self.btn_find = None
        btn_site = _fixed(PushButton(FIF.LINK, "AniList"))
        btn_site.setToolTip("Open this season on AniList")
        site = state.get("site_url") or ""
        btn_site.setEnabled(bool(site))
        btn_site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(site)))
        root.addWidget(btn_site)
        more = _fixed(ToolButton(FIF.MORE))
        more.setFixedSize(36, 36)
        more.setToolTip("Fix the match, or check this show now")
        more.clicked.connect(lambda: self._menu(more))
        root.addWidget(more)

    def _menu(self, anchor):
        menu = RoundMenu(parent=self)
        pick = Action(FIF.EDIT, "Not the right anime?")
        pick.triggered.connect(lambda: self.pick.emit(self.url))
        menu.addAction(pick)
        again = Action(FIF.SYNC, "Check this show")
        again.triggered.connect(lambda: self.recheck.emit(self.url))
        menu.addAction(again)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))


class NewsWidget(QWidget):
    # [(title, notice)] from a background check, for the Windows notification.
    changes_found = pyqtSignal(list)
    # query, site domain ("" for the one picked in Search): Find in Search.
    search_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads = []
        self._filter = "all"
        self._rows = []
        self._manual = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel("Anime news")
        title.setFont(QFont("Segoe UI Variable", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        heading.addWidget(title)
        self.lead = _muted("", 13)
        heading.addWidget(self.lead)
        header.addLayout(heading)
        header.addStretch(1)
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(24, 24)
        self.spinner.hide()
        header.addWidget(self.spinner)
        self.btn_check = _fixed(PushButton(FIF.SYNC, "Check now"))
        self.btn_check.setToolTip("Ask AniList about every show now (it is checked once "
                                  "a day on its own)")
        self.btn_check.clicked.connect(lambda: self.check(force=True, manual=True))
        header.addWidget(self.btn_check)
        root.addLayout(header)

        self.seg = SegmentedWidget()
        for key, label, _kinds in FILTERS:
            self.seg.addItem(key, label, onClick=lambda _checked=False, k=key: self._switch(k))
        self.seg.setCurrentItem(self._filter)
        seg_row = QHBoxLayout()
        seg_row.addWidget(self.seg)
        seg_row.addStretch(1)
        root.addLayout(seg_row)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self.list_col = QVBoxLayout(host)
        self.list_col.setContentsMargins(0, 0, 6, 0)
        self.list_col.setSpacing(10)
        self.list_col.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #888888; background: transparent; font-size: 14px; "
                                 "padding: 40px 0;")
        self.list_col.addWidget(self.empty)
        self.scroll.setWidget(host)
        root.addWidget(self.scroll, 1)

        card = SimpleCardWidget()
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(14)
        icon = QLabel()
        icon.setPixmap(FIF.RINGER.icon().pixmap(18, 18))
        icon.setStyleSheet("background: transparent;")
        row.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(2)
        label = QLabel("Notify me when something changes")
        label.setStyleSheet("color: #ffffff; background: transparent; font-size: 14px;")
        text.addWidget(label)
        text.addWidget(_muted("A Windows notification when a new season is announced, gets a "
                              "date, or starts airing.", 12, wrap=True))
        row.addLayout(text, 1)
        self.switch = SwitchButton()
        self.switch.setChecked(bool(app_settings.get("news_notify", True)))
        self.switch.checkedChanged.connect(self._on_notify_toggled)
        row.addWidget(self.switch)
        root.addWidget(card)

    # ---- lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def stop_threads(self):
        for t in self._threads:
            t.requestInterruption()
        return [t for t in self._threads if t.isRunning()]

    # ---- list
    def _switch(self, key):
        self._filter = key
        self.refresh()

    def refresh(self):
        entries = library_shows()
        data = news.load()
        shows = data.get("shows", {})
        kind_of = lambda e: ((shows.get(e["url"]) or {}).get("state") or {}).get("kind")
        for key, label, kinds in FILTERS:
            n = sum(1 for e in entries if kind_of(e) in kinds or key == "all")
            self.seg.widget(key).setText(f"{label}  {n}")
        n = len(entries)
        when = checked_text(max((float((shows.get(e["url"]) or {}).get("checked_at") or 0)
                                 for e in entries), default=0))
        self.lead.setText(f"{n} show{'s' if n != 1 else ''} from your Library · {when}"
                          if n else "Nothing in your Library yet")

        for r in self._rows:
            self.list_col.removeWidget(r)
            r.deleteLater()
        self._rows = []
        kinds = dict((k, ks) for k, _l, ks in FILTERS)[self._filter]
        shown = [e for e in entries if self._filter == "all" or kind_of(e) in kinds]
        shown.sort(key=lambda e: (ORDER.get(kind_of(e), len(ORDER)), e["title"].lower()))
        for e in shown:
            row = NewsRow(e, shows.get(e["url"]))
            row.pick.connect(self.pick_match)
            row.recheck.connect(self.recheck)
            row.find.connect(self.find_in_search)
            self.list_col.addWidget(row)
            self._rows.append(row)
        if not entries:
            self.empty.setText("Add anime to your Library and what's next for each one shows "
                               "up here.")
        elif not shown:
            self.empty.setText("No shows here right now.")
        self.empty.setVisible(not shown)

    # ---- checking
    def check(self, force=False, manual=False, entries=None):
        """Ask AniList. In the background (`manual` False) only shows not checked
        today are asked, and nothing is said unless something changed."""
        if any(isinstance(t, CheckThread) and t.isRunning() for t in self._threads):
            return False
        entries = library_shows() if entries is None else entries
        if not entries:
            return False
        self._manual = manual
        self.btn_check.setEnabled(False)
        self.spinner.show()
        t = CheckThread(entries, force)
        t.done.connect(self._on_checked)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()
        return True

    def recheck(self, url):
        self.check(force=True, manual=True, entries=[e for e in library_shows() if e["url"] == url])

    def _on_checked(self, changes, errors):
        self.btn_check.setEnabled(True)
        self.spinner.hide()
        self.refresh()
        if changes:
            self.changes_found.emit(changes)
        if not self._manual:
            return
        if errors:
            InfoBar.warning("Couldn't check every show",
                            f"{len(errors)} couldn't be checked: {errors[0][1]}",
                            position=InfoBarPosition.TOP, duration=8000, parent=self)
        elif changes:
            InfoBar.success("Anime news", " ".join(msg for _t, msg in changes[:3]),
                            position=InfoBarPosition.TOP, duration=8000, parent=self)
        else:
            InfoBar.info("Anime news", "Checked. Nothing new since last time.",
                         position=InfoBarPosition.TOP, duration=4000, parent=self)

    def find_in_search(self, url):
        """Search the show's own site for it, without season words: the site lists
        each season as its own result, and the new one is among them."""
        entry = next((e for e in library_shows() if e["url"] == url), None)
        if entry is None:
            return
        title = entry.get("title", "")
        self.search_requested.emit(news._norm(title) or title, entry.get("domain") or "")

    def pick_match(self, url):
        entry = next((e for e in library_shows() if e["url"] == url), None)
        if entry is None:
            return
        dlg = MatchDialog(entry.get("title", "Anime"), self.window())
        if dlg.exec() and dlg.picked:
            news.set_match(url, dlg.picked["id"], news.display_title(dlg.picked))
            self.refresh()
            self.check(force=True, manual=True, entries=[entry])

    def _on_notify_toggled(self, on):
        with config_lock:
            app_settings["news_notify"] = bool(on)
        save_config()
