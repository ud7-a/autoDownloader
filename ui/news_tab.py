"""Anime news: what's next for each anime in the Library.

One card per Library anime with its state from AniList (utils/anime_news.py):
airing now, a new season coming, waiting for news, or finished. The search bar at
the top follows an anime that isn't in the Library: it is picked on AniList and
gets a card like the rest. A filter strip narrows the list; "Not the right anime?"
fixes a wrong match; an airing season has "Find in Search", which searches the
anime sites for it. The app checks once a day in the background and posts a
Windows notification when a season is announced, gets a date or starts airing
(AppWindow._on_news_changes).
"""
import datetime
import time

from PyQt6.QtCore import QRectF, QSize, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from qfluentwidgets import (Action, BodyLabel, CardWidget, FluentIcon as FIF,
                            IndeterminateProgressRing, InfoBar, InfoBarPosition, LineEdit,
                            MessageBoxBase, PrimaryPushButton, PushButton, RoundMenu,
                            SegmentedWidget, SimpleCardWidget, SmoothScrollArea, SubtitleLabel,
                            SwitchButton, ToolButton)

from utils import anime_news as news
from utils import watch_later as wl
from utils.config import app_settings, config_lock, save_config
from ui.watch_later_tab import _fixed, _muted, _poster, _title

# Filter strip: key, label, the states it shows ("new": any state, news from the
# last few days -- the rows marked "New since last check").
FILTERS = (("all", "All", news.KINDS),
           ("new", "New", news.KINDS),
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


def in_filter(key, rec):
    """Does a row with this record belong under filter `key`?"""
    if key == "all":
        return True
    if key == "new":
        return news.is_fresh(rec)
    kinds = dict((k, ks) for k, _l, ks in FILTERS)[key]
    return ((rec or {}).get("state") or {}).get("kind") in kinds


def row_order(entry, rec):
    """Rows by state (airing first), airing ones by their next episode, soonest
    first, then by name."""
    state = (rec or {}).get("state") or {}
    kind = state.get("kind")
    soon = state.get("next_at") if kind == news.AIRING else None
    return (ORDER.get(kind, len(ORDER)), soon if isinstance(soon, (int, float)) else float("inf"),
            entry["title"].lower())


def library_shows():
    """The Library anime the page covers: every entry with a title and a link."""
    return [e for e in wl.entries() if e.get("url") and e.get("title")]


def tracked_shows():
    """Every show the page covers: the Library's, then those followed from the
    search bar."""
    return library_shows() + news.followed()


def _season_rank(title):
    """How late a season a Library title names: its number ("2nd Season", "III",
    "Part 2"), with any "Final" season after every numbered one."""
    import re
    from core.schedule import season_number
    n = season_number(title or "") or 1
    return n + 100 if re.search(r"\bfinal\b", title or "", re.IGNORECASE) else n


def _added(entry):
    try:
        return float(entry.get("added") or 0)
    except (TypeError, ValueError):
        return 0.0


def group_record(members, shows):
    """The record a row shows for these Library entries: the one checked most
    recently (another may be a day old), marked new if any of them changed."""
    records = [shows[e["url"]] for e in members if isinstance(shows.get(e["url"]), dict)]
    if not records:
        return None
    best = max(records, key=lambda r: float(r.get("checked_at") or 0))
    changed = [r.get("changed_at") for r in records if isinstance(r.get("changed_at"), (int, float))]
    return dict(best, changed_at=max(changed)) if changed else best


def newest_per_anime(entries, shows):
    """[(entry, [other entries])]: one per anime. Library entries for different
    seasons of one anime ("Black Clover", "Black Clover 2nd Season") lead to the
    same newest season, so they'd show the same news twice; the newest season of
    them stands for all. Entries not matched or not checked yet stay on their own.
    A Library entry leads before one followed from the search bar."""
    groups, order = {}, []
    for e in entries:
        rec = shows.get(e["url"])
        state = (rec.get("state") if isinstance(rec, dict) else None) or {}
        key = ("anime", state["anilist_id"]) if state.get("anilist_id") else ("entry", e["url"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    def newest(e):
        return (not news.is_followed(e.get("url")), _season_rank(e.get("title")), _added(e))
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
    """One page of AniList results for the pick list, then their covers."""
    # generation, media list, error ("" when fine), whether AniList has a next page
    results = pyqtSignal(int, list, str, bool)
    # generation, index in AniList's whole answer (over every page), image bytes
    cover = pyqtSignal(int, int, bytes)

    COVER_WORKERS = 6             # covers fetched at once

    def __init__(self, query, generation, page=1):
        super().__init__()
        self.query, self.generation, self.page = query, generation, page

    def run(self):
        try:
            found, more = news.search_page(self.query, self.page)
        except Exception as e:
            self.results.emit(self.generation, [], str(e), False)
            return
        self.results.emit(self.generation, found, "", more)
        offset = (self.page - 1) * news.PICK_RESULTS
        # Covers after the list, so the names show at once, a few at a time, in
        # list order; one failed cover just keeps its placeholder.
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor

        def fetch(m):
            url = (m.get("coverImage") or {}).get("medium")
            if not url or self.isInterruptionRequested():
                return None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AutoEpisodesDownloader"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read()
            except Exception:
                return None
        with ThreadPoolExecutor(self.COVER_WORKERS) as pool:
            for i, data in enumerate(pool.map(fetch, found)):
                if self.isInterruptionRequested():
                    return
                if data:
                    self.cover.emit(self.generation, offset + i, data)


class ElidedLabel(QLabel):
    """One line that ends in "…" when it doesn't fit, with the whole text as its
    tooltip then: a long AniList name never pushes a card past the dialog's edge."""

    def __init__(self, text, style, parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)     # a title like "<Infinite...>"
        self.setStyleSheet(style)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        shown = self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight,
                                              self.width())
        self.setText(shown)
        self.setToolTip(self.full_text if shown != self.full_text else "")


class ResultCard(CardWidget):
    """One AniList entry in the pick list: cover, names, what it is. Click picks it,
    double-click picks it and closes the dialog."""
    chosen = pyqtSignal()

    COVER_W, COVER_H = 46, 64

    def __init__(self, m, current=False, parent=None):
        super().__init__(parent)
        self.media = m
        self.image = None                   # the cover, once loaded (kept on follow)
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
        self.title = ElidedLabel(news.display_title(m), "color: #ffffff; background: transparent; "
                                                        "font-size: 14px; font-weight: 600;")
        top.addWidget(self.title, 1)
        if current:
            # The QLabel.BadgeInfo look, inline: the dialog's own stylesheet keeps
            # the app-wide class rule from reaching labels inside it.
            tag = QLabel("Current match")
            tag.setStyleSheet("background: rgba(76, 194, 255, 0.15); color: #4cc2ff; "
                              "border: 1px solid rgba(76, 194, 255, 0.3); border-radius: 10px; "
                              "padding: 2px 8px; font-size: 11px; font-weight: bold;")
            top.addWidget(tag)
        text.addLayout(top)
        muted = "color: #999999; background: transparent; font-size: 12px;"   # as _muted
        other = news.other_title(m)
        if other:
            text.addWidget(ElidedLabel(other, muted))
        text.addWidget(ElidedLabel(news.candidate_line(m), muted))
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
        self.image = image
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
    """Pick the AniList entry a Library anime really is -- or, with `follow`, an
    anime to follow that isn't in the Library (`title` is then what was typed).

    Searches once on opening, then only on Search or Enter, never while typing.
    Each result shows its cover, both its names and what it is (format, year,
    episodes, finished or airing), since several seasons and spin-offs share a
    name; the entry the show follows now is marked. AniList answers 50 at a time:
    "Show more results" at the end of the list adds the next 50."""

    ROW_STEP = 90                 # a result card and the gap after it
    MORE_STEP = 52                # the "Show more results" button and its margin
    VISIBLE_ROWS = 6              # the list grows to this many, then scrolls...
    CHROME_H = 370                # ...or less, so the dialog fits the window: the
                                  # height of everything else in it, with margins

    def __init__(self, title, current_id=None, parent=None, follow=False):
        super().__init__(parent)
        self.picked = None
        self.picked_image = None  # the picked card's cover, when it had loaded
        self.current_id = current_id
        self._threads = []
        self._cards = []
        self._order = []          # card position -> index in AniList's answer
        self._generation = 0
        self._query = ""          # what the shown results were searched for
        self._page = 1            # AniList pages shown
        self._series_only = True  # films and specials left out (the first page had series)
        self.viewLayout.setSpacing(12)
        if follow:
            self.viewLayout.addWidget(SubtitleLabel("Follow an anime"))
            lead = BodyLabel("Pick the anime to follow. Its news shows here with your "
                             "Library's, and you're told when a new season is announced, "
                             "gets a date or starts airing.")
        else:
            self.viewLayout.addWidget(SubtitleLabel("Which anime is this?"))
            lead = BodyLabel(f"Pick the AniList entry for “{title}” from your Library. Its "
                             "news follows that entry from now on.")
        lead.setWordWrap(True)
        self.viewLayout.addWidget(lead)
        # The Search tab's search bar (ui/search_tab.py): the same field and button,
        # at the same size.
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search_box = LineEdit()
        self.search_box.setPlaceholderText("Enter anime title to search...")
        self.search_box.setFixedHeight(40)
        self.search_box.setText((title or "").strip() if follow else news.search_text(title))
        self.search_box.returnPressed.connect(lambda: self._search(self.search_box.text()))
        bar.addWidget(self.search_box, 1)
        self.btn_search = PrimaryPushButton("Search")
        self.btn_search.setFixedSize(120, 40)
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search.clicked.connect(lambda: self._search(self.search_box.text()))
        bar.addWidget(self.btn_search)
        self.viewLayout.addLayout(bar)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(16, 16)
        self.spinner.setStrokeWidth(2)
        status_row.addWidget(self.spinner)
        self.status = _muted("", 12)
        self.status.setTextFormat(Qt.TextFormat.PlainText)    # it quotes what was typed
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
        # Under the last card, while AniList has more: cards go in above it.
        self.btn_more = PushButton(FIF.DOWN, "Show more results")
        self.btn_more.setFixedHeight(40)
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.clicked.connect(self._load_more)
        self.btn_more.hide()
        self.list_col.addWidget(self.btn_more)
        self.scroll.setWidget(host)
        self.viewLayout.addWidget(self.scroll)

        self.yesButton.setText("Follow" if follow else "Use this anime")
        self.yesButton.setEnabled(False)
        self.cancelButton.setText("Cancel")
        self.widget.setMinimumWidth(600)
        self._search(self.search_box.text())

    def _search(self, query):
        query = (query or "").strip()
        if not query:
            return
        self._generation += 1
        self._query, self._page = query, 1
        self.btn_more.hide()
        self.status.setText(f"Searching AniList for “{query}”…")
        self._start(SearchThread(query, self._generation), self._show)

    def _start(self, t, on_results, new_search=True):
        if new_search:                  # an older search's covers aren't wanted now
            for old in self._threads:
                old.requestInterruption()
        self.spinner.show()
        t.results.connect(on_results)
        t.cover.connect(self._cover)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _load_more(self):
        """AniList's next 50, added under the cards already shown."""
        self.btn_more.setEnabled(False)
        self.btn_more.setText("Loading…")
        self._start(SearchThread(self._query, self._generation, self._page + 1), self._more,
                    new_search=False)

    def _ranked(self, found, offset):
        """[(index in AniList's whole answer, media)] to show: series only -- or
        everything, when the first page had no series at all -- closest name first,
        AniList's own order breaking ties, each entry once."""
        shown = {c.media.get("id") for c in self._cards}
        picked = []
        for i, m in enumerate(found):
            if self._series_only and m.get("format") not in news.SERIES_FORMATS:
                continue
            if m.get("id") in shown:
                continue                    # shown already, or twice in AniList's answer
            shown.add(m.get("id"))
            picked.append((offset + i, m))
        return sorted(picked, key=lambda im: (-round(news.similarity(self._query, im[1]), 1), im[0]))

    def _add(self, ranked):
        for index, m in ranked:
            card = ResultCard(m, current=bool(self.current_id) and m.get("id") == self.current_id)
            card.clicked.connect(lambda c=card: self._pick(c))
            card.chosen.connect(lambda c=card: (self._pick(c), self.accept()))
            self.list_col.insertWidget(self.list_col.count() - 1, card)    # above "Show more"
            self._cards.append(card)
            self._order.append(index)

    def _count_text(self):
        n = len(self._cards)
        return (f"{n} result{'s' if n != 1 else ''}. Click one to pick it, or double-click to "
                "use it right away.")

    def _fit(self, more):
        """The list's height for its cards (and "Show more"), up to list_height()."""
        self.btn_more.setVisible(more)
        self.btn_more.setEnabled(True)
        self.btn_more.setText("Show more results")
        content = max(len(self._cards), 1) * self.ROW_STEP + (self.MORE_STEP if more else 0)
        self.scroll.setFixedHeight(min(content, self.list_height()))
        self.scroll.setVisible(bool(self._cards) or more)

    def _show(self, generation, found, error, more=False):
        if generation != self._generation:
            return                                  # an older search, typed over
        self.spinner.hide()
        for c in self._cards:
            self.list_col.removeWidget(c)
            c.deleteLater()
        self._cards, self._order = [], []
        self.picked = self.picked_image = None
        self.yesButton.setEnabled(False)
        # Films and specials only show for a name that has no series at all.
        self._series_only = any(m.get("format") in news.SERIES_FORMATS for m in found)
        self._add(self._ranked(found, 0))
        if error:
            self.status.setText(f"{error}. Check your connection and press Enter to try again.")
        elif not self._cards:
            self.status.setText(f"Nothing on AniList is called “{self._query}”. Try a shorter "
                                "name, or its English or Japanese title.")
        else:
            self.status.setText(self._count_text())
        self._fit(more and not error)
        self.scroll.verticalScrollBar().setValue(0)

    def _more(self, generation, found, error, more=False):
        if generation != self._generation:
            return                                  # the search changed meanwhile
        self.spinner.hide()
        if error:
            self.status.setText(f"{error}. Check your connection and press “Show more results” "
                                "to try again.")
            self._fit(True)
            return
        self._page += 1
        if not found:
            # AniList said there was more, then had nothing: the end all the same.
            self.status.setText(f"{self._count_text()} AniList has nothing more for "
                                f"“{self._query}”.")
            self._fit(False)
            return
        added = self._ranked(found, (self._page - 1) * news.PICK_RESULTS)
        self._add(added)
        self.status.setText(self._count_text() if added else
                            f"{len(self._cards)} results. The next {len(found)} on AniList were "
                            "films, specials or ones shown already.")
        self._fit(more)

    def list_height(self):
        """The most the result list grows to before it scrolls: VISIBLE_ROWS rows,
        or what the window has room for, never under 2 rows. A window's room is
        used to the pixel, so a part-shown last card says there is more below."""
        most = self.VISIBLE_ROWS * self.ROW_STEP
        host = self.parentWidget()
        if host is None:
            return most
        return max(2 * self.ROW_STEP, min(most, host.height() - self.CHROME_H))

    def _cover(self, generation, index, data):
        if generation == self._generation and index in self._order:
            self._cards[self._order.index(index)].set_cover(data)

    def _pick(self, card):
        for c in self._cards:
            c.set_selected(c is card)
        self.picked = card.media
        self.yesButton.setEnabled(True)

    def accept(self):
        # The cover as it is now: it may have loaded after the card was picked.
        card = next((c for c in self._cards if c.selected), None)
        self.picked_image = card.image if card else None
        super().accept()

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
    unfollow = pyqtSignal(str)             # url -- stop following (followed rows)

    def __init__(self, entry, record, also=(), parent=None, followed=False):
        super().__init__(parent)
        self.url = entry.get("url", "")
        self.followed = followed           # this row stands for a followed anime
        self.lead_followed = news.is_followed(self.url)
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
        if self.lead_followed:
            # Not in the Library: followed from the search bar.
            top.addWidget(_muted("Following", 12))
        if news.is_fresh(record):
            new = QLabel("New since last check")
            new.setStyleSheet("color: #4cc2ff; background: transparent; font-size: 12px; "
                              "font-weight: 600;")
            top.addWidget(new)
        top.addStretch(1)
        info.addLayout(top)
        headline = QLabel(state.get("headline") or "Waiting for the first check.")
        headline.setTextFormat(Qt.TextFormat.PlainText)   # it names the season
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
            # page in Search, to follow it in the Watchlist or download it. A
            # followed anime has no episodes here yet, so that's what it offers.
            if self.lead_followed:
                self.btn_find = _fixed(PrimaryPushButton(FIF.DOWNLOAD, "Find to download"))
                self.btn_find.setToolTip("Search the anime sites for it. On its result, the "
                                         "heart button downloads each new episode for you.")
            else:
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
        more.setToolTip("Check this show now, or stop following it" if self.lead_followed
                        else "Fix the match, or check this show now")
        more.clicked.connect(lambda: self._menu(more))
        root.addWidget(more)

    def actions(self):
        """The ⋯ menu: (icon, text, signal). A followed anime was picked on
        AniList itself, so it has no match to fix."""
        out = []
        if not self.lead_followed:
            out.append((FIF.EDIT, "Not the right anime?", self.pick))
        out.append((FIF.SYNC, "Check this show", self.recheck))
        if self.followed:
            out.append((FIF.REMOVE_FROM, "Stop following", self.unfollow))
        return out

    def _menu(self, anchor):
        menu = RoundMenu(parent=self)
        for icon, text, signal in self.actions():
            action = Action(icon, text)
            action.triggered.connect(lambda _c=False, s=signal: s.emit(self.url))
            menu.addAction(action)
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
        self._group = {}          # row's url -> the entries it stands for
        self._manual = False
        self._pending = []        # just followed while a check was running

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

        # Follow an anime that isn't in the Library: the Search tab's search bar
        # (ui/search_tab.py), at the same size; the results open in the AniList
        # picker.
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search_box = LineEdit()
        self.search_box.setPlaceholderText("Search AniList to follow an anime that isn't in "
                                           "your Library...")
        self.search_box.setFixedHeight(40)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self.follow_search)
        bar.addWidget(self.search_box, 1)
        self.btn_search = PrimaryPushButton("Search")
        self.btn_search.setFixedSize(120, 40)
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search.clicked.connect(self.follow_search)
        bar.addWidget(self.btn_search)
        root.addLayout(bar)

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

    def show_filter(self, key):
        """Show one filter, as if its segment were clicked."""
        self.seg.setCurrentItem(key)
        self._switch(key)

    def refresh(self):
        entries = tracked_shows()
        data = news.load()
        shows = data.get("shows", {})
        anime = [(e, others, group_record([e] + others, shows))
                 for e, others in newest_per_anime(entries, shows)]
        # Which entries each row stands for, for "Check this show" and "Stop following".
        self._group = {e["url"]: [e] + others for e, others, _r in anime}
        for key, label, _kinds in FILTERS:
            n = sum(1 for _e, _o, rec in anime if in_filter(key, rec))
            self.seg.widget(key).setText(f"{label}  {n}")
        # When the whole list was last checked: its least recently checked row. The
        # newest check of any one show (Check this show, a show just followed) would
        # say every show was checked then.
        checked = [float((rec or {}).get("checked_at") or 0) for _e, _o, rec in anime]
        when = checked_text(min((t for t in checked if t), default=0))
        self.lead.setText(self.lead_text(anime, when))

        for r in self._rows:
            self.list_col.removeWidget(r)
            r.deleteLater()
        self._rows = []
        shown = [a for a in anime if in_filter(self._filter, a[2])]
        shown.sort(key=lambda a: row_order(a[0], a[2]))
        for e, others, rec in shown:
            # "Also in your Library" names only Library seasons; a followed copy of
            # the same anime just makes the row offer "Stop following".
            library_others = [o for o in others if not news.is_followed(o["url"])]
            followed = any(news.is_followed(x["url"]) for x in [e] + others)
            row = NewsRow(e, rec, library_others, followed=followed)
            row.pick.connect(self.pick_match)
            row.recheck.connect(self.recheck)
            row.find.connect(self.find_in_search)
            row.unfollow.connect(self.unfollow)
            self.list_col.addWidget(row)
            self._rows.append(row)
        if not entries:
            self.empty.setText("Add anime to your Library, or search above to follow one, and "
                               "what's next for each shows up here.")
        elif not shown:
            self.empty.setText("No news in the last 7 days." if self._filter == "new"
                               else "No shows here right now.")
        self.empty.setVisible(not shown)

    @staticmethod
    def lead_text(anime, when):
        """'5 shows from your Library · checked ...', with the followed ones counted
        apart: '5 shows from your Library, 2 followed · ...'."""
        if not anime:
            return "Nothing to show yet"
        followed = sum(1 for e, _o, _r in anime if news.is_followed(e["url"]))
        library = len(anime) - followed
        parts = []
        if library:
            parts.append(f"{library} show{'s' if library != 1 else ''} from your Library")
        if followed:
            parts.append(f"{followed} followed" if library
                         else f"{followed} followed show{'s' if followed != 1 else ''}")
        return ", ".join(parts) + f" · {when}"

    # ---- checking
    def check(self, force=False, manual=False, entries=None):
        """Ask AniList. In the background (`manual` False) only shows not checked
        today are asked, and nothing is said unless something changed."""
        if any(isinstance(t, CheckThread) and t.isRunning() for t in self._threads):
            return False
        entries = tracked_shows() if entries is None else entries
        if not entries:
            return False
        self._manual = manual
        self.btn_check.setEnabled(False)
        self.spinner.show()
        t = CheckThread(entries, force)
        t.done.connect(self._on_checked)
        t.finished.connect(lambda t=t: self._threads.remove(t) if t in self._threads else None)
        t.finished.connect(self._check_pending)
        self._threads.append(t)
        t.start()
        return True

    def recheck(self, url):
        """Check this show: the row's Library entry and the other seasons it
        stands for."""
        urls = {e["url"] for e in self._group.get(url, [])} or {url}
        self.check(force=True, manual=True, entries=[e for e in tracked_shows() if e["url"] in urls])

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
        entry = next((e for e in tracked_shows() if e["url"] == url), None)
        if entry is None:
            return
        # A followed anime: its romaji name, the one the sites use.
        title = entry.get("site_name") or entry.get("title", "")
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
        dlg.deleteLater()           # with any tooltip window of its own

    # ---- following anime that aren't in the Library
    def tracked_ids(self):
        """AniList ids the page already covers: each show's match and newest season."""
        ids = set()
        shows = news.load()["shows"]
        for e in tracked_shows():
            rec = shows.get(e["url"]) or {}
            ids.update(i for i in (rec.get("anilist_id"), (rec.get("state") or {}).get("anilist_id")) if i)
        return ids

    def follow_search(self):
        """Search AniList for what's typed and follow the anime picked."""
        query = self.search_box.text().strip()
        if not query:
            self.search_box.setFocus()
            return
        dlg = MatchDialog(query, None, self.window(), follow=True)
        if dlg.exec() and dlg.picked:
            self.follow(dlg.picked, dlg.picked_image)
        dlg.deleteLater()           # with any tooltip window of its own

    def follow(self, m, image=None):
        name = news.display_title(m)
        if m.get("id") in self.tracked_ids():
            InfoBar.info("Already here", f"{name} is already in Anime news.",
                         position=InfoBarPosition.TOP, duration=4000, parent=self)
            return None
        cover = ""
        if image is not None:
            from ui.watchlist_tab import _persist_cover
            cover = _persist_cover(f"{news.FOLLOW_PREFIX}{m['id']}", image)
        entry = news.follow(m, cover)
        if entry is None:
            return None
        self.search_box.clear()
        self.refresh()
        InfoBar.success("Following", f"{name}. Its news shows here in a moment.",
                        position=InfoBarPosition.TOP, duration=4000, parent=self)
        # Quietly: a first check has no news to report, only the state to show. A
        # check already running takes it up when it ends.
        if not self.check(force=True, entries=[entry]):
            self._pending.append(entry)
        return entry

    def _check_pending(self):
        pending, self._pending = self._pending, []
        if pending and not self.check(force=True, entries=pending):
            self._pending = pending + self._pending

    def unfollow(self, url):
        """Stop following the followed anime this row stands for."""
        gone = [news.unfollow(e["url"]) for e in self._group.get(url, [{"url": url}])
                if news.is_followed(e["url"])]
        gone = [g for g in gone if g]
        if not gone:
            return
        self.refresh()
        InfoBar.info("Stopped following", gone[0]["title"],
                     position=InfoBarPosition.TOP, duration=4000, parent=self)

    def _on_notify_toggled(self, on):
        with config_lock:
            app_settings["news_notify"] = bool(on)
        save_config()
