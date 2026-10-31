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

from PyQt6.QtCore import QRectF, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from qfluentwidgets import (Action, BodyLabel, CardWidget, FluentIcon as FIF,
                            IndeterminateProgressRing, InfoBar, InfoBarPosition, LineEdit,
                            MessageBoxBase, PrimaryPushButton, PushButton, RoundMenu,
                            SegmentedWidget, SimpleCardWidget, SmoothScrollArea, SubtitleLabel,
                            SwitchButton, ToolButton)

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


def newest_per_anime(entries, shows):
    """[(entry, [other entries])]: one per anime. Library entries for different
    seasons of one anime ("Black Clover", "Black Clover 2nd Season") lead to the
    same newest season, so they'd show the same news twice; the newest season of
    them stands for all. Entries not matched or not checked yet stay on their own."""
    from core.schedule import season_number
    groups, order = {}, []
    for e in entries:
        state = (shows.get(e["url"]) or {}).get("state") or {}
        key = ("anime", state["anilist_id"]) if state.get("anilist_id") else ("entry", e["url"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    def newest(e):
        return (season_number(e.get("title")) or 1, float(e.get("added") or 0))
    out = []
    for key in order:
        members = sorted(groups[key], key=newest, reverse=True)
        out.append((members[0], members[1:]))
    return out


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
    results = pyqtSignal(int, list, str)   # generation, media list, error ("" when fine)
    cover = pyqtSignal(int, int, bytes)    # generation, result index, image bytes

    def __init__(self, query, generation):
        super().__init__()
        self.query, self.generation = query, generation

    def run(self):
        try:
            found = news.search(self.query)
        except Exception as e:
            self.results.emit(self.generation, [], str(e))
            return
        self.results.emit(self.generation, found, "")
        # Covers after the list, so the names show at once; one failed cover
        # just keeps its placeholder.
        import urllib.request
        for i, m in enumerate(found):
            if self.isInterruptionRequested():
                return
            url = (m.get("coverImage") or {}).get("medium")
            if not url:
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AutoEpisodesDownloader"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.cover.emit(self.generation, i, resp.read())
            except Exception:
                continue


class ResultCard(CardWidget):
    """One AniList entry in the pick list: cover, names, what it is. Click picks it,
    double-click picks it and closes the dialog."""
    chosen = pyqtSignal()

    COVER_W, COVER_H = 46, 64

    def __init__(self, m, current=False, parent=None):
        super().__init__(parent)
        self.media = m
        self.selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(84)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 10, 14, 10)
        row.setSpacing(12)
        self.cover = QLabel("🎞️")
        self.cover.setFixedSize(self.COVER_W, self.COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet("border-radius: 5px; background-color: #1e1e1e; "
                                 "color: #555555; font-size: 16px;")
        row.addWidget(self.cover)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addStretch(1)                  # centred, with or without a second name
        top = QHBoxLayout()
        top.setSpacing(8)
        name = news.display_title(m)
        title = QLabel(name)
        title.setStyleSheet("color: #ffffff; background: transparent; font-size: 14px; "
                            "font-weight: 600;")
        title.setToolTip(name)
        top.addWidget(title, 1)
        if current:
            # The QLabel.BadgeInfo look, inline: the dialog's own stylesheet keeps
            # the app-wide class rule from reaching labels inside it.
            tag = QLabel("Current match")
            tag.setStyleSheet("background: rgba(76, 194, 255, 0.15); color: #4cc2ff; "
                              "border: 1px solid rgba(76, 194, 255, 0.3); border-radius: 10px; "
                              "padding: 2px 8px; font-size: 11px; font-weight: bold;")
            top.addWidget(tag)
        text.addLayout(top)
        other = news.other_title(m)
        if other:
            text.addWidget(_muted(other, 12))
        text.addWidget(_muted(news.candidate_line(m), 12))
        text.addStretch(1)
        row.addLayout(text, 1)
        self.tick = QLabel()
        self.tick.setPixmap(FIF.ACCEPT.icon(color=QColor("#4cc2ff")).pixmap(16, 16))
        self.tick.setStyleSheet("background: transparent;")
        self.tick.setFixedWidth(18)
        self.tick.setVisible(False)
        row.addWidget(self.tick)

    def set_cover(self, data):
        image = QImage.fromData(data)
        if image.isNull():
            return
        from ui.styles import rounded_from_image
        self.cover.setPixmap(rounded_from_image(image, self.COVER_W, self.COVER_H, 5))
        self.cover.setText("")
        self.cover.setStyleSheet("background: transparent;")

    def set_selected(self, on):
        self.selected = bool(on)
        self.tick.setVisible(self.selected)
        self.update()

    def mouseDoubleClickEvent(self, event):
        super().mouseDoubleClickEvent(event)
        self.chosen.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.selected:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#4cc2ff"))
            pen.setWidthF(1.6)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
            p.end()


class MatchDialog(MessageBoxBase):
    """Pick the AniList entry a Library anime really is.

    Searches as you type. Each result shows its cover, both its names and what it
    is (format, year, episodes, finished or airing), since several seasons and
    spin-offs share a name; the entry the show follows now is marked."""

    TYPE_DELAY_MS = 450
    ROW_STEP = 90                 # a result card and the gap after it
    VISIBLE_ROWS = 4              # the list grows to this many, then scrolls

    def __init__(self, title, current_id=None, parent=None):
        super().__init__(parent)
        self.picked = None
        self.current_id = current_id
        self._threads = []
        self._cards = []
        self._order = []          # card position -> index in AniList's answer
        self._generation = 0
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(SubtitleLabel("Which anime is this?"))
        lead = BodyLabel(f"Pick the AniList entry for “{title}” from your Library. Its news "
                         "follows that entry from now on.")
        lead.setWordWrap(True)
        self.viewLayout.addWidget(lead)
        # The Search tab's search bar (ui/search_tab.py): the same field and button,
        # at the same size.
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search_box = LineEdit()
        self.search_box.setPlaceholderText("Enter anime title to search...")
        self.search_box.setFixedHeight(40)
        self.search_box.setText(news.search_text(title))
        self.search_box.returnPressed.connect(lambda: self._search(self.search_box.text()))
        bar.addWidget(self.search_box, 1)
        self.btn_search = PrimaryPushButton("Search")
        self.btn_search.setFixedSize(120, 40)
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search.clicked.connect(lambda: self._search(self.search_box.text()))
        bar.addWidget(self.btn_search)
        self._typing = QTimer(self)
        self._typing.setSingleShot(True)
        self._typing.setInterval(self.TYPE_DELAY_MS)
        self._typing.timeout.connect(lambda: self._search(self.search_box.text()))
        self.search_box.textEdited.connect(lambda _t: self._typing.start())
        self.viewLayout.addLayout(bar)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(16, 16)
        self.spinner.setStrokeWidth(2)
        status_row.addWidget(self.spinner)
        self.status = _muted("", 12)
        status_row.addWidget(self.status, 1)
        self.viewLayout.addLayout(status_row)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scroll.setFixedHeight(self.ROW_STEP)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self.list_col = QVBoxLayout(host)
        self.list_col.setContentsMargins(0, 0, 8, 0)
        self.list_col.setSpacing(6)
        self.list_col.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(host)
        self.viewLayout.addWidget(self.scroll)

        self.yesButton.setText("Use this anime")
        self.yesButton.setEnabled(False)
        self.cancelButton.setText("Cancel")
        self.widget.setMinimumWidth(600)
        self._search(self.search_box.text())

    def _search(self, query):
        query = (query or "").strip()
        self._typing.stop()
        if not query:
            return
        self._generation += 1
        for t in self._threads:
            t.requestInterruption()
        self.spinner.show()
        self.status.setText(f"Searching AniList for “{query}”…")
        t = SearchThread(query, self._generation)
        t.results.connect(self._show)
        t.cover.connect(self._cover)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _show(self, generation, found, error):
        if generation != self._generation:
            return                                  # an older search, typed over
        self.spinner.hide()
        for c in self._cards:
            self.list_col.removeWidget(c)
            c.deleteLater()
        self._cards = []
        self.picked = None
        self.yesButton.setEnabled(False)
        query = self.search_box.text()
        series = [m for m in found if m.get("format") in news.SERIES_FORMATS] or found
        # Closest name first; AniList's own order breaks ties.
        ranked = sorted(enumerate(series), key=lambda im: (-round(news.similarity(query, im[1]), 1), im[0]))
        if error:
            self.status.setText(f"{error}. Check your connection and press Enter to try again.")
        elif not ranked:
            self.status.setText(f"Nothing on AniList is called “{query.strip()}”. Try a shorter "
                                "name, or its English or Japanese title.")
        else:
            n = len(ranked)
            self.status.setText(f"{n} result{'s' if n != 1 else ''}. Click one to pick it, or "
                                "double-click to use it right away.")
        self._order = [found.index(m) for _i, m in ranked]
        for _i, m in ranked:
            card = ResultCard(m, current=bool(self.current_id) and m.get("id") == self.current_id)
            card.clicked.connect(lambda c=card: self._pick(c))
            card.chosen.connect(lambda c=card: (self._pick(c), self.accept()))
            self.list_col.addWidget(card)
            self._cards.append(card)
        rows = min(max(len(self._cards), 1), self.VISIBLE_ROWS)
        self.scroll.setFixedHeight(rows * self.ROW_STEP)
        self.scroll.setVisible(bool(self._cards))
        self.scroll.verticalScrollBar().setValue(0)

    def _cover(self, generation, index, data):
        if generation == self._generation and index in self._order:
            self._cards[self._order.index(index)].set_cover(data)

    def _pick(self, card):
        for c in self._cards:
            c.set_selected(c is card)
        self.picked = card.media
        self.yesButton.setEnabled(True)

    def done(self, result):
        for t in self._threads:
            t.requestInterruption()
        for t in self._threads:
            t.wait(3000)
        super().done(result)


class NewsRow(SimpleCardWidget):
    pick = pyqtSignal(str)                 # url -- choose the AniList entry
    recheck = pyqtSignal(str)              # url -- check this show now
    find = pyqtSignal(str)                 # url -- look the airing season up in Search

    def __init__(self, entry, record, also=(), parent=None):
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
        if also:
            # The other seasons in the Library, which this row stands for.
            info.addWidget(_muted("Also in your Library: " + ", ".join(e.get("title", "") for e in also),
                                  12, wrap=True))

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
        self._group = {}          # row's url -> the Library entries it stands for
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
        anime = newest_per_anime(entries, shows)
        # Which Library entries each row stands for, for "Check this show".
        self._group = {e["url"]: [e] + others for e, others in anime}
        for key, label, kinds in FILTERS:
            n = sum(1 for e, _o in anime if kind_of(e) in kinds or key == "all")
            self.seg.widget(key).setText(f"{label}  {n}")
        n = len(anime)
        when = checked_text(max((float((shows.get(e["url"]) or {}).get("checked_at") or 0)
                                 for e in entries), default=0))
        self.lead.setText(f"{n} show{'s' if n != 1 else ''} from your Library · {when}"
                          if n else "Nothing in your Library yet")

        for r in self._rows:
            self.list_col.removeWidget(r)
            r.deleteLater()
        self._rows = []
        kinds = dict((k, ks) for k, _l, ks in FILTERS)[self._filter]
        shown = [(e, o) for e, o in anime if self._filter == "all" or kind_of(e) in kinds]
        shown.sort(key=lambda eo: (ORDER.get(kind_of(eo[0]), len(ORDER)), eo[0]["title"].lower()))
        for e, others in shown:
            row = NewsRow(e, shows.get(e["url"]), others)
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
        """Check this show: the row's Library entry and the other seasons it
        stands for."""
        urls = {e["url"] for e in self._group.get(url, [])} or {url}
        self.check(force=True, manual=True, entries=[e for e in library_shows() if e["url"] in urls])

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
        current = (news.load()["shows"].get(url) or {}).get("anilist_id")
        dlg = MatchDialog(entry.get("title", "Anime"), current, self.window())
        if dlg.exec() and dlg.picked:
            news.set_match(url, dlg.picked["id"], news.display_title(dlg.picked))
            self.refresh()
            self.check(force=True, manual=True, entries=[entry])

    def _on_notify_toggled(self, on):
        with config_lock:
            app_settings["news_notify"] = bool(on)
        save_config()
