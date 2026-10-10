"""Library: anime you plan to watch, are watching, and have finished.

Replaces the History tab. Each anime keeps its own download history and the
episodes you've watched; nothing here creates a profile until you press Download.

Layout: a Continue watching strip on top (the next episode of everything you're
watching, one click to play), then Watching / Watch later / Completed.
"""
import os

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from qfluentwidgets import (PushButton, PrimaryPushButton, SimpleCardWidget, SmoothScrollArea,
                            ToolButton, PrimaryToolButton, FluentIcon as FIF, InfoBar,
                            InfoBarPosition, MessageBoxBase, SubtitleLabel, BodyLabel,
                            ComboBox, ProgressBar, SegmentedWidget, RoundMenu,
                            Action, FlowLayout, IndeterminateProgressRing)

from core.signals import signals
from utils import watch_later as wl
from utils.library_scan import is_local
from utils.config import app_settings, sites_data, config_lock, save_config
from ui.episode_grid import EpisodeGrid
from ui.styles import rounded_pixmap, show_undo

CONTINUE_LIMIT = 6        # cards in the Continue watching strip
# An anime found only as a folder on disk: no profile or Watchlist entry says
# which site page its episodes come from.
NO_LINK_MESSAGE = ("This anime was found in your download folder, but the app doesn't "
                   "know which website it came from. Find it in Search and add it to "
                   "your Library -- its watched episodes carry over.")
LOG_POLL_MS = 5000        # how often mpv.net's progress log is checked while open

STATUS_COLORS = {"Success": "#2ecc71", "Failed": "#e74c3c",
                 "Partial": "#f39c12", "Cancelled": "#aaaaaa"}


def _download_dir():
    return app_settings.get("download_dir", "")


def _poster(cover, w, h, radius=6):
    label = QLabel()
    label.setFixedSize(w, h)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    from ui.watchlist_tab import _saved_covers
    held = _saved_covers.get(cover) if cover else None
    if held is not None:
        from ui.styles import rounded_from_image
        pix = rounded_from_image(held, w, h, radius)
    else:
        pix = rounded_pixmap(cover, w, h, radius) if cover and os.path.exists(cover) else None
    if pix is not None:
        label.setPixmap(pix)
        label.setStyleSheet("background: transparent;")
    else:
        label.setText("🎞️")
        label.setStyleSheet(f"border-radius: {radius}px; background-color: #1e1e1e; "
                            "color: #555555; font-size: 20px;")
    return label


def _title(text, size, max_width=None):
    label = QLabel(text)
    label.setFont(QFont("Segoe UI Variable", size, QFont.Weight.Bold))
    label.setStyleSheet("color: #ffffff; background: transparent;")
    label.setToolTip(text)
    if max_width:
        label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max_width))
    return label


def _bar(value, maximum, width):
    bar = ProgressBar()
    bar.setRange(0, max(1, maximum))
    bar.setValue(max(0, min(int(value), max(1, maximum))))
    bar.setTextVisible(False)
    bar.setFixedWidth(width)
    return bar


def _fixed(button):
    """Buttons keep their natural width instead of splitting the row's spare room."""
    from PyQt6.QtWidgets import QSizePolicy
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _muted(text, size=12, wrap=False):
    label = QLabel(text)
    label.setStyleSheet(f"color: #999999; background: transparent; font-size: {size}px;")
    label.setWordWrap(wrap)
    return label


class PosterThread(QThread):
    """Find posters for Library entries that have none (anime imported from the
    download folder or from saved profiles), one at a time, in the background.

    Uses the Search tab's own search, run inside this thread: its results and
    covers arrive through direct connections, so no browser or network object is
    shared with the GUI thread.
    """
    updated = pyqtSignal()
    # A folder-only anime got its page: look its episodes up, so it can download.
    linked = pyqtSignal(str)

    def __init__(self, entries):
        super().__init__()
        self.entries = entries

    def _search(self, query, search_url, title, template):
        """(the result that is this anime or None, its cover or None). Only that
        result's cover is fetched -- not the 60 a search page would load."""
        from ui.search_tab import AnimeSearchThread
        from utils.library_scan import match_result
        found, covers = [], {}
        th = AnimeSearchThread(query, search_url)
        # run() below happens on this thread, so th's own interruption flag is
        # never set; closing the app has to reach its cover loop through ours.
        th.isInterruptionRequested = self.isInterruptionRequested

        def on_results(results):
            match = match_result(title, template, results)
            found.append(match)
            th.cover_links = {match["link"]} if match else set()

        direct = Qt.ConnectionType.DirectConnection
        th.finished.connect(on_results, direct)
        th.cover_loaded.connect(lambda link, img: covers.__setitem__(link, img), direct)
        th.error.connect(lambda _m: None, direct)
        th.run()                       # synchronously, on this thread
        match = found[0] if found else None
        return match, (covers.get(match["link"]) if match else None)

    def run(self):
        from ui.search_tab import SUPPORTED_SITES, site_of, extract_domain
        from ui.watchlist_tab import _persist_cover
        from utils.library_scan import query_variants
        for entry in self.entries:
            if self.isInterruptionRequested():
                return
            template = next((p.get("template") for p in entry.get("parts", [])
                             if p.get("template")), "")
            known = site_of(extract_domain(template)) if template else ""
            sites = [d for d in SUPPORTED_SITES if site_of(d) == known] or list(SUPPORTED_SITES)
            found = cover = None
            domain = ""
            try:
                for domain in sites:
                    for query in query_variants(entry.get("title")):
                        if self.isInterruptionRequested():
                            return
                        found, cover = self._search(query, SUPPORTED_SITES[domain],
                                                    entry.get("title"), template)
                        if found:
                            break
                    if found:
                        break
            except Exception:
                found = None
            if self.isInterruptionRequested():
                return
            path = _persist_cover(found["link"], cover) if (found and cover is not None) else ""
            new_url = wl.set_poster(entry["url"], path, found["link"] if found else "",
                                    domain if found else "")
            if found:                  # nothing on screen changes for a miss
                self.updated.emit()
            if new_url != entry["url"] and not template:
                self.linked.emit(new_url)


class WatchedGrid(EpisodeGrid):
    STRIKE_OFF = False
    CELL_H = 42           # room for the site line under each episode number


class PartPickDialog(MessageBoxBase):
    """Which season to download, for an anime with several."""

    def __init__(self, title, parts, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(f"Download — {title}"))
        self.viewLayout.addWidget(BodyLabel("This anime has several seasons. Pick one:"))
        self.combo = ComboBox()
        for i, p in enumerate(parts):
            label = p.get("label") or f"Season {i + 1}"
            eps = p.get("max_ep") or "?"
            self.combo.addItem(f"{label}  ·  {eps} eps")
        self.viewLayout.addWidget(self.combo)
        self.widget.setMinimumWidth(380)
        self.yesButton.setText("Download")
        self.cancelButton.setText("Cancel")

    def index(self):
        return self.combo.currentIndex()


class EntryDialog(MessageBoxBase):
    """One anime: status, watched episodes per season, and its download history."""
    download_part = pyqtSignal(int)    # part index
    refresh_parts = pyqtSignal()

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.url = entry.get("url", "")
        self._grids = []
        self._clear_history = False

        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(_poster(entry.get("cover", ""), 64, 96))
        head = QVBoxLayout()
        title = SubtitleLabel(entry.get("title", "Anime"))
        title.setWordWrap(True)
        head.addWidget(title)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("Status"))
        self.combo_status = ComboBox()
        for key in wl.STATUSES:
            self.combo_status.addItem(wl.STATUS_LABELS[key], userData=key)
        self._status = entry.get("status")
        if self._status not in wl.STATUSES:
            self._status = wl.LATER
        self.combo_status.setCurrentIndex(list(wl.STATUSES).index(self._status))
        row.addWidget(self.combo_status)
        row.addStretch(1)
        head.addLayout(row)
        head.addStretch(1)
        top.addLayout(head, 1)
        self.viewLayout.addLayout(top)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setMinimumHeight(320)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(8)
        col.setAlignment(Qt.AlignmentFlag.AlignTop)

        parts = entry.get("parts") or []
        self.part_idents = [wl.part_ident(p) for p in parts]
        col.addWidget(self._section("Episodes"))
        if not parts:
            col.addWidget(_muted("Episodes aren't known yet. Use Refresh episodes "
                                 "to look them up.", wrap=True))
        from ui.downloader_tab import compact_episode_spec
        for i, part in enumerate(parts):
            files = wl.episode_files(wl.part_folder(part, _download_dir()))
            eps = sorted(set(wl._total_eps(part)) | set(files))
            head = QHBoxLayout()
            name = part.get("label") or ("" if len(parts) == 1 else f"Season {i + 1}")
            if name:
                lbl = BodyLabel(name)
                lbl.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Bold))
                head.addWidget(lbl)
            if files:
                head.addWidget(_muted(f"Downloaded: {compact_episode_spec(sorted(files))}", 11))
            elif not part.get("profile"):
                head.addWidget(_muted("Not downloaded yet — no profile until you download.", 11))
            head.addStretch(1)
            btn = PushButton(FIF.DOWNLOAD, "Download")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c, idx=i: (self.apply(), self.download_part.emit(idx)))
            head.addWidget(btn)
            col.addLayout(head)
            if eps:
                grid = WatchedGrid()
                grid.set_episodes(eps, part.get("watched", []))
                # Under each number: the site that episode was downloaded from.
                from ui.search_tab import site_display_name
                sources = wl.part_sources(part, _download_dir())
                grid.set_captions({ep: site_display_name(src.get("site", ""))
                                   for ep, src in sources.items() if src.get("site")})
                col.addWidget(grid)
                # Saved on Done (see apply), not on every click.
                self._grids.append((i, grid, set(eps), set(part.get("watched", [])),
                                    wl.part_ident(part)))
        if parts:
            col.addWidget(_muted("Click an episode to mark it watched, then press Done. Episodes you play "
                                 "in mpv.net are ticked automatically. Under each number is "
                                 "the website it was downloaded from.", 11, wrap=True))

        col.addSpacing(6)
        history = [h for h in entry.get("history") or [] if isinstance(h, dict)]
        hist_head = QHBoxLayout()
        hist_head.addWidget(self._section("History"))
        hist_head.addStretch(1)
        self.btn_clear_history = PushButton(FIF.DELETE, "Clear history")
        self.btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_history.setToolTip("Forget this anime's download history. "
                                          "Watched episodes are kept.")
        self.btn_clear_history.setVisible(bool(history))
        self.btn_clear_history.clicked.connect(self._on_clear_history)
        hist_head.addWidget(self.btn_clear_history)
        col.addLayout(hist_head)
        self.history_rows = QWidget()
        self.history_rows.setStyleSheet("background: transparent;")
        hist_col = QVBoxLayout(self.history_rows)
        hist_col.setContentsMargins(0, 0, 0, 0)
        hist_col.setSpacing(8)
        col.addWidget(self.history_rows)
        self.lbl_no_history = _muted("No downloads yet.")
        self.lbl_no_history.setVisible(not history)
        col.addWidget(self.lbl_no_history)
        for h in history:
            line = QHBoxLayout()
            line.setSpacing(10)
            line.addWidget(_muted(h.get("date", ""), 11))
            line.addWidget(BodyLabel(f"Ep {h.get('episodes', '')}"))
            st = QLabel(h.get("status", ""))
            st.setStyleSheet(f"color: {STATUS_COLORS.get(h.get('status'), '#cccccc')}; "
                             "background: transparent;")
            line.addWidget(st)
            notes = _muted(h.get("notes", ""), 11)
            notes.setToolTip(h.get("notes", ""))
            line.addWidget(notes, 1)
            hist_col.addLayout(line)

        scroll.setWidget(host)
        self.viewLayout.addWidget(scroll, 1)

        self.btn_refresh = PushButton(FIF.SYNC, "Refresh episodes")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self._on_refresh)
        self.buttonLayout.insertWidget(0, self.btn_refresh)
        # Found only as a folder on disk: there is no anime page to look up.
        self.btn_refresh.setVisible(not is_local(self.url))
        self.widget.setMinimumWidth(620)
        # Done saves the ticks and status; Close leaves without saving them.
        self.yesButton.setText("Done")
        self.yesButton.clicked.connect(self.apply)
        self.cancelButton.setText("Close")

    @staticmethod
    def _section(text):
        label = QLabel(text)
        label.setStyleSheet("color: #ffffff; background: transparent; font-size: 15px; "
                            "font-weight: bold; padding-top: 4px;")
        return label

    def apply(self):
        """Save what changed: the episodes ticked or unticked here, then the status.
        Only those clicks are written -- episodes the grid doesn't show (past the
        known count, file since deleted) and anything mpv.net ticked while the
        dialog was open stay as they are."""
        for idx, grid, shown, before, ident in self._grids:
            selected = set(grid.selected()) & shown
            was = before & shown
            added, removed = selected - was, was - selected
            if added or removed:
                wl.change_watched(self.url, idx, added, removed, ident)
        status = self.combo_status.currentData()
        if status != self._status:
            wl.set_status(self.url, status)
            self._status = status
        if self._clear_history:
            wl.clear_history(self.url)
            self._clear_history = False
        # Applied once: a second call (Download, then Done) changes nothing.
        self._grids = [(i, g, shown, (set(g.selected()) & shown) | (before - shown), ident)
                       for i, g, shown, before, ident in self._grids]

    def _on_clear_history(self):
        """Cleared on Done, like every other change here (Close keeps it)."""
        self._clear_history = True
        self.history_rows.hide()
        self.btn_clear_history.hide()
        self.lbl_no_history.show()

    def _on_refresh(self):
        self.apply()
        self.refresh_parts.emit()
        self.accept()


class ContinueCard(SimpleCardWidget):
    # url, part index, episode: plays that episode, or downloads it (and the
    # rest of its season) when it isn't on disk -- exactly what the card says.
    play = pyqtSignal(str, int, int)

    def __init__(self, entry, part_index, ep, file, percent, parent=None):
        super().__init__(parent)
        self.url = entry.get("url", "")
        self.part_index, self.ep = part_index, ep
        self.setFixedSize(300, 88)
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 12, 8)
        root.setSpacing(10)
        root.addWidget(_poster(entry.get("cover", ""), 48, 72, 5))

        info = QVBoxLayout()
        info.setSpacing(4)
        info.addStretch(1)
        info.addWidget(_title(entry.get("title", "Anime"), 10, max_width=160))
        parts = entry.get("parts") or []
        season = parts[part_index].get("label") if len(parts) > 1 else ""
        where = f"{season} · " if season else ""
        if file:
            info.addWidget(_muted(f"{where}Next: Ep {ep}" + (f" · {int(percent)}%" if percent else ""), 11))
        else:
            info.addWidget(_muted(f"{where}Ep {ep} isn't downloaded", 11))
        info.addWidget(_bar(percent, 100, 150), 0, Qt.AlignmentFlag.AlignLeft)
        info.addStretch(1)
        root.addLayout(info, 1)

        btn = _fixed(PrimaryToolButton(FIF.PLAY if file else FIF.DOWNLOAD))
        btn.setFixedSize(38, 38)
        btn.setToolTip(f"Play Ep {ep}" if file else f"Download Ep {ep}")
        btn.clicked.connect(lambda: self.play.emit(self.url, self.part_index, self.ep))
        root.addWidget(btn)


class EntryRow(SimpleCardWidget):
    primary = pyqtSignal(str)            # url -- watch / rewatch
    download_missing = pyqtSignal(str)   # url -- download what isn't on disk yet
    details = pyqtSignal(str)            # url
    move = pyqtSignal(str, str)          # url, status
    remove = pyqtSignal(str)             # url

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.url = entry.get("url", "")
        self.setFixedHeight(104)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 14, 10)
        root.setSpacing(14)
        root.addWidget(_poster(entry.get("cover", ""), 56, 84))

        info = QVBoxLayout()
        info.setSpacing(3)
        root.addLayout(info, 1)          # attached first: what goes in gets this row as parent
        info.addWidget(_title(entry.get("title", "Anime"), 11))

        watched, total, downloaded = wl.summary(entry, _download_dir())
        if not entry.get("parts"):
            text = "Episodes not looked up yet"
        elif not any(p.get("profile") for p in entry["parts"]):
            text = f"{total} episodes · not started, no profile yet" if total else "Not started"
        else:
            text = f"{watched} / {total or '?'} watched · {downloaded} downloaded"
        info.addWidget(_muted(text))
        bar = _bar(watched, total, 220)
        # Aligned, so the fixed-width bar doesn't cap the column's width (a
        # vertical layout takes its narrowest child's maximum otherwise).
        info.addWidget(bar, 0, Qt.AlignmentFlag.AlignLeft)
        # Hide only, never show: setVisible(True) on a widget that isn't inside a
        # window yet opens it as its own window -- a tiny "Python" window flashed
        # once per row on every list switch.
        if not total:
            bar.hide()
        from ui.search_tab import site_display_name, extract_domain
        domain = entry.get("domain") or ("" if is_local(self.url) else extract_domain(self.url))
        info.addWidget(_muted(site_display_name(domain) if domain else "In your download folder", 11))

        # Up to two actions: watch the next downloaded episode, and download the
        # ones the site has that aren't on disk yet. Whichever exists alone (or
        # watching, when both do) is the primary button.
        status = entry.get("status")
        nxt = wl.next_episode(entry, _download_dir())
        missing = wl.missing_episodes(entry, _download_dir())
        watch_label = None
        if status == wl.COMPLETED:
            if downloaded:
                watch_label = "Rewatch"
        elif nxt and nxt[2]:
            watch_label = (f"Continue watching · Ep {nxt[1]}" if status == wl.WATCHING
                           else f"Start watching · Ep {nxt[1]}")
        download_label = None
        if not entry.get("parts"):
            download_label = "Download"            # looks the episodes up first
        elif missing and status != wl.COMPLETED:
            download_label = "Continue downloading" if downloaded else "Download"

        if watch_label:
            btn = _fixed(PrimaryPushButton(FIF.PLAY, watch_label))
            btn.clicked.connect(lambda: self.primary.emit(self.url))
            root.addWidget(btn)
        if download_label:
            cls = PushButton if watch_label else PrimaryPushButton
            btn_dl = _fixed(cls(FIF.DOWNLOAD, download_label))
            if missing:
                from ui.downloader_tab import compact_episode_spec
                btn_dl.setToolTip(f"Not downloaded yet: Ep {compact_episode_spec(missing[1])}")
            btn_dl.clicked.connect(lambda: self.download_missing.emit(self.url))
            root.addWidget(btn_dl)

        btn_details = _fixed(PushButton(FIF.HISTORY, "Episodes and history"))
        btn_details.clicked.connect(lambda: self.details.emit(self.url))
        root.addWidget(btn_details)

        more = _fixed(ToolButton(FIF.MORE))
        more.setFixedSize(36, 36)
        more.setToolTip("Move or remove")
        more.clicked.connect(lambda: self._menu(more, status))
        root.addWidget(more)

    def _menu(self, anchor, status):
        menu = RoundMenu(parent=self)
        for key in wl.STATUSES:
            if key != status:
                act = Action(FIF.RIGHT_ARROW, f"Move to {wl.STATUS_LABELS[key]}")
                act.triggered.connect(lambda _c=False, k=key: self.move.emit(self.url, k))
                menu.addAction(act)
        menu.addSeparator()
        rm = Action(FIF.DELETE, "Remove from Library")
        rm.triggered.connect(lambda: self.remove.emit(self.url))
        menu.addAction(rm)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))


class WatchLaterWidget(QWidget):
    profile_ready = pyqtSignal(str)      # profile to open in the Downloader
    search_requested = pyqtSignal()
    player_requested = pyqtSignal()      # open the Video Player tab (to install mpv.net)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads = []
        self._status = wl.WATCHING
        self._rows = []
        self._continue_cards = []
        self._loaded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Library")
        title.setFont(QFont("Segoe UI Variable", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        header.addWidget(title)
        header.addStretch(1)
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(24, 24)
        self.spinner.hide()
        header.addWidget(self.spinner)
        btn_scan = PushButton(FIF.FOLDER, "Scan anime folder")
        btn_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_scan.setToolTip("Add the anime already in your download folder, with what "
                            "you've watched in mpv.net")
        btn_scan.clicked.connect(lambda: self.scan_folder(announce=True))
        header.addWidget(btn_scan)
        btn_add = PushButton(FIF.SEARCH, "Add from Search")
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.setToolTip("Press the Library button on any Search result to add it here")
        btn_add.clicked.connect(self.search_requested.emit)
        header.addWidget(btn_add)
        root.addLayout(header)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, 0, 6, 0)
        self.col.setSpacing(10)
        self.col.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.lbl_continue = QLabel("Continue watching")
        self.lbl_continue.setStyleSheet("color: #ffffff; background: transparent; "
                                        "font-size: 16px; font-weight: bold;")
        self.col.addWidget(self.lbl_continue)
        self.continue_host = QWidget()
        self.continue_host.setStyleSheet("background: transparent;")
        self.continue_flow = FlowLayout(self.continue_host)
        self.continue_flow.setContentsMargins(0, 0, 0, 0)
        self.continue_flow.setHorizontalSpacing(10)
        self.continue_flow.setVerticalSpacing(10)
        self.col.addWidget(self.continue_host)

        self.seg = SegmentedWidget()
        for key in wl.STATUSES:
            # onClick is wired to clicked(bool): without the first parameter
            # PyQt hands `checked` to k and the status becomes True.
            self.seg.addItem(key, wl.STATUS_LABELS[key],
                             onClick=lambda _checked=False, k=key: self._switch(k))
        self.seg.setCurrentItem(self._status)
        seg_row = QHBoxLayout()
        seg_row.addWidget(self.seg)
        seg_row.addStretch(1)
        self.col.addSpacing(6)
        self.col.addLayout(seg_row)

        self.list_col = QVBoxLayout()
        self.list_col.setSpacing(10)
        self.col.addLayout(self.list_col)
        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #888888; background: transparent; font-size: 14px; "
                                 "padding: 40px 0;")
        self.col.addWidget(self.empty)

        self.scroll.setWidget(host)
        root.addWidget(self.scroll, 1)

        # Pinned under the list: tracking what was watched needs mpv.net.
        self.mpv_notice = SimpleCardWidget()
        notice = QHBoxLayout(self.mpv_notice)
        notice.setContentsMargins(14, 10, 14, 10)
        notice.setSpacing(12)
        notice_icon = QLabel()
        notice_icon.setPixmap(FIF.INFO.icon().pixmap(18, 18))
        notice_icon.setStyleSheet("background: transparent;")
        notice.addWidget(notice_icon)
        notice_text = QLabel("Tracking which episodes you've watched needs mpv.net. "
                             "Install it from the Video Player tab and the Library "
                             "ticks episodes off as you watch.")
        notice_text.setWordWrap(True)
        notice_text.setStyleSheet("color: #cccccc; background: transparent; font-size: 12px;")
        notice.addWidget(notice_text, 1)
        btn_player = _fixed(PushButton(FIF.VIDEO, "Open Video Player"))
        btn_player.clicked.connect(self.player_requested.emit)
        notice.addWidget(btn_player)
        self.mpv_notice.hide()
        root.addWidget(self.mpv_notice)

        self._mpv_dir = ""
        self._log_path = ""
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self.refresh)
        signals.history_updated.connect(self._refresh_if_loaded)
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._poll_progress)

    # ---- lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        first = not self._loaded
        if first:
            self._loaded = True
        with config_lock:
            wl.resolve_profiles(sites_data)
        wl.fill_earlier_watched()      # one-time catch-up for older tracking
        self._poll_progress(refresh=False)
        self.refresh()
        self._log_timer.start(LOG_POLL_MS)
        # Finding mpv.net reads the registry: off the first paint, and once per
        # visit while it is missing -- never on the 5-second timer.
        QTimer.singleShot(0, self._check_mpv)
        if first and not wl.imported_from_disk():
            # First open after the update: bring in what is already downloaded
            # (scan_folder then looks up the posters).
            QTimer.singleShot(0, lambda: self.scan_folder(announce=False))
        else:
            QTimer.singleShot(0, self.fetch_posters)

    def fetch_posters(self):
        """Look up posters for entries without one, in the background. An entry
        whose lookup found nothing is retried after a day, not on every visit."""
        th = getattr(self, "_poster_thread", None)
        if th is not None and th.isRunning():
            return
        todo = [e for e in wl.entries() if wl.needs_poster(e)]
        if not todo:
            return
        th = PosterThread(todo)
        # Posters arrive one by one: rebuild the list once they pause, not per poster.
        th.updated.connect(self._schedule_refresh)
        th.linked.connect(lambda url: self._detect(url))
        self._poster_thread = th
        th.start()

    def _schedule_refresh(self):
        self._refresh_timer.start()

    def _mpv_config_dir(self):
        """mpv.net's config folder, or "" when it isn't installed. Called on a
        visit to the tab and on a scan, never from the 5-second timer, since a
        lookup walks every installed program in the registry. The moment it is
        found the progress script goes in, so tracking starts without a restart."""
        if not self._mpv_dir:
            from utils.mpvnet import find_mpvnet, config_dir, PROGRESS_LOG
            exe = find_mpvnet()[0]
            if exe:
                self._mpv_dir = config_dir(exe)
                self._log_path = os.path.join(self._mpv_dir, PROGRESS_LOG)
                self._install_progress_script()
        return self._mpv_dir

    def _check_mpv(self):
        try:
            installed = bool(self._mpv_config_dir())
        except Exception:
            installed = True        # never nag on a lookup error
        self.mpv_notice.setVisible(not installed)
        self._poll_progress()

    def scan_folder(self, announce=True):
        """Add every anime in the download folder (and every saved profile) that
        isn't in the Library yet, with what mpv.net says was watched."""
        from utils.library_scan import import_library, read_mpv_history
        from utils.config import get_watchlist
        try:
            history = read_mpv_history(self._mpv_config_dir())
        except Exception:
            history = {}
        with config_lock:
            profiles = dict(sites_data)
        try:
            added = import_library(_download_dir(), profiles, get_watchlist(), history)
        except Exception as e:
            if announce:
                InfoBar.error("Scan failed", str(e), position=InfoBarPosition.TOP,
                              duration=5000, parent=self.window())
            return 0
        if added:
            self._status = wl.WATCHING
            self.seg.setCurrentItem(wl.WATCHING)
        self.refresh()
        self.fetch_posters()
        if added or announce:
            InfoBar.success("Library updated" if added else "Nothing new",
                            f"Added {added} anime from your download folder." if added
                            else "Everything in your download folder is already here.",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
        return added

    def hideEvent(self, event):
        super().hideEvent(event)
        self._log_timer.stop()

    def _install_progress_script(self):
        try:
            from utils.mpvnet import ensure_progress_script
            ensure_progress_script()
        except Exception:
            pass

    def _refresh_if_loaded(self):
        if self._loaded:
            self.refresh()

    def _poll_progress(self, refresh=True):
        """Fold in what mpv.net logged since last time. Only once mpv.net has been
        found (see _mpv_config_dir): this runs every 5 seconds."""
        if not self._log_path:
            return
        try:
            if wl.ingest_log(self._log_path) and refresh:
                self.refresh()
        except Exception:
            pass

    # ---- building
    def refresh(self):
        self._refresh_timer.stop()
        items = wl.entries()
        counts = {k: sum(1 for e in items if e.get("status") == k) for k in wl.STATUSES}
        for key in wl.STATUSES:
            self.seg.setItemText(key, f"{wl.STATUS_LABELS[key]}  {counts[key]}")
        # Each row and card lists its folders several times; once per refresh is enough.
        with wl.cached_listing():
            self._build_continue([e for e in items if e.get("status") == wl.WATCHING])
            self._build_rows([e for e in items if e.get("status") == self._status])

    def _clear(self, widgets):
        # Hidden and deleted in place. setParent(None) turned each old row into a
        # parentless top-level widget until deleteLater ran.
        for w in widgets:
            w.hide()
            w.deleteLater()
        widgets.clear()

    def _build_continue(self, watching):
        # Out of the layout BEFORE they go: qfluentwidgets' FlowLayout.takeAt hands
        # back a widget, not a QLayoutItem, so letting Qt remove a card itself
        # (setParent(None)) raises inside Qt -- which aborts the installed app.
        self.continue_flow.takeAllWidgets()
        self._continue_cards = []
        cards = []
        for e in sorted(watching, key=lambda x: -((x.get("last_played") or {}).get("ts") or 0)):
            nxt = wl.next_episode(e, _download_dir())
            if nxt is None:
                continue
            idx, ep, file, percent = nxt
            card = ContinueCard(e, idx, ep, file, percent)
            card.play.connect(self.on_continue)
            cards.append(card)
            if len(cards) >= CONTINUE_LIMIT:
                break
        for card in cards:
            self.continue_flow.addWidget(card)
        self._continue_cards = cards
        self.lbl_continue.setVisible(bool(cards))
        self.continue_host.setVisible(bool(cards))

    def _build_rows(self, items):
        self._clear(self._rows)
        for e in items:
            row = EntryRow(e)
            row.primary.connect(self.on_primary)
            row.download_missing.connect(self.on_download_missing)
            row.details.connect(self.open_details)
            row.move.connect(self.on_move)
            row.remove.connect(self.on_remove)
            self.list_col.addWidget(row)
            self._rows.append(row)
        if items:
            self.empty.hide()
            return
        self.empty.setText({
            wl.WATCHING: "Nothing in progress.\nPlay or download an anime from your Watch later list "
                         "and it moves here.",
            wl.LATER: "Your Watch later list is empty.\nOpen Search and press the Library "
                      "button on an anime to save it for later.",
            wl.COMPLETED: "Nothing finished yet.\nAnime move here once every episode is watched.",
        }[self._status])
        self.empty.show()

    def _switch(self, status):
        self._status = status
        self.refresh()

    # ---- adding
    def add(self, title, url, domain, cover=""):
        """Called from Search. Never creates a profile; looks the episodes up in the
        background so the episode list is ready when the anime is opened."""
        from ui.watchlist_tab import _persist_cover, display_title
        title = display_title(title)
        if not wl.add(title, url, domain, _persist_cover(url, cover)):
            InfoBar.info("Already saved", f"'{title}' is already in your Library.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            return
        self._status = wl.LATER
        self.seg.setCurrentItem(wl.LATER)
        self.refresh()
        InfoBar.success("Added to Library", f"'{title}' is saved for later.",
                        position=InfoBarPosition.TOP, duration=3000, parent=self.window())
        self._detect(url)

    def _detect(self, url, then=None):
        """Look up the anime's seasons/episodes; `then(entry)` runs on success."""
        from ui.search_tab import AnimeDetailsThread
        th = AnimeDetailsThread(url)
        self.spinner.show()

        def done(found):
            wl.set_parts(url, found)
            self._thread_done(th)
            self.refresh()
            entry = wl.find(url)
            if then and entry:
                then(entry)

        def failed(msg):
            self._thread_done(th)
            if then:
                from ui.search_tab import friendly_browser_error
                InfoBar.error("Couldn't find episodes", friendly_browser_error(msg, url),
                              position=InfoBarPosition.TOP, duration=5000, parent=self.window())

        th.finished.connect(done)
        th.error.connect(failed)
        self._threads.append(th)
        th.start()

    def _thread_done(self, th):
        try:
            th.wait(100)
        except Exception:
            pass
        if th in self._threads:
            self._threads.remove(th)
        if not self._threads:
            self.spinner.hide()

    def stop_threads(self):
        threads = list(self._threads)
        if getattr(self, "_poster_thread", None) is not None:
            threads.append(self._poster_thread)
        for t in threads:
            try:
                t.requestInterruption()
            except Exception:
                pass
        return [t for t in threads if t.isRunning()]

    # ---- actions
    def on_primary(self, url):
        entry = wl.find(url)
        if not entry:
            return
        if entry.get("status") == wl.COMPLETED:
            self._rewatch(entry)
            return
        nxt = wl.next_episode(entry, _download_dir())
        if nxt:
            self.on_continue(url, nxt[0], nxt[1])
        else:
            self.on_download_missing(url)

    def on_continue(self, url, part_index, ep):
        """A Continue watching card: play its episode, or -- when it isn't on disk
        -- download it and the rest of that season, not some other season."""
        entry = wl.find(url)
        if not entry or not 0 <= part_index < len(entry.get("parts") or []):
            return
        part = entry["parts"][part_index]
        if ep in wl.episode_files(wl.part_folder(part, _download_dir())):
            self._play(entry, part_index, ep)
        else:
            self.download(entry, part_index,
                          episodes=wl.missing_from(entry, part_index, ep, _download_dir()))

    def on_download_missing(self, url):
        """Continue downloading: the episodes the site has that aren't on disk."""
        entry = wl.find(url)
        if not entry:
            return
        missing = wl.missing_episodes(entry, _download_dir())
        if missing:
            self.download(entry, missing[0], episodes=missing[1])
        else:
            self.download(entry)

    def _play(self, entry, part_index, ep):
        files = wl.playlist_from(entry, part_index, ep, _download_dir())
        if not files:
            return
        try:
            from utils.mpvnet import open_videos
            open_videos(files)
        except Exception as e:
            InfoBar.warning("Playback Error", f"Couldn't play it: {e}",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
            return
        if entry.get("status") == wl.LATER:
            wl.set_status(entry["url"], wl.WATCHING)
            self.refresh()

    def _rewatch(self, entry):
        for i, part in enumerate(entry.get("parts") or []):
            files = wl.episode_files(wl.part_folder(part, _download_dir()))
            if files:
                self._play(entry, i, min(files))
                return
        InfoBar.info("Nothing downloaded", "None of its episodes are on disk anymore.",
                     position=InfoBarPosition.TOP, duration=4000, parent=self.window())

    def download(self, entry, part_index=None, episodes=None):
        """Open this anime in the Downloader, creating its profile now if needed.
        `episodes` (when given) is what the Downloader's episode box is set to."""
        parts = entry.get("parts") or []
        if not parts and is_local(entry.get("url")):
            InfoBar.warning("Can't download this one", NO_LINK_MESSAGE,
                            position=InfoBarPosition.TOP, duration=6000, parent=self.window())
            return
        if not parts:
            InfoBar.info("Finding episodes", f"Looking up '{entry.get('title')}'…",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            self._detect(entry["url"], then=lambda e: e.get("parts") and self.download(e))
            return
        if part_index is None:
            if len(parts) == 1:
                part_index = 0
            else:
                dlg = PartPickDialog(entry.get("title", "Anime"), parts, self.window())
                if not dlg.exec():
                    return
                part_index = dlg.index()
        part = parts[part_index]
        if not part.get("template"):
            InfoBar.warning("Can't download this one", NO_LINK_MESSAGE,
                            position=InfoBarPosition.TOP, duration=6000, parent=self.window())
            return

        from ui.search_tab import open_existing_profile, create_profile
        linked = part.get("profile")
        name = linked
        with config_lock:
            if name and (name not in sites_data or sites_data[name].get("_transient")):
                name = None
        max_ep, first_ep = part.get("max_ep") or 1, part.get("first_ep") or 1
        if not name:
            name, _range = open_existing_profile(part["template"], max_ep, first_ep)
        if not name:
            # A part still linked to a deleted profile keeps that name: it names
            # the folder its episodes are in, so new ones land beside them.
            name = create_profile(linked or wl.part_profile_name(entry, part), part["template"],
                                  max_ep, entry.get("domain", ""), first_ep)
        wl.link_profile(entry["url"], part_index, name)
        with config_lock:
            if episodes:
                from ui.downloader_tab import compact_episode_spec
                sites_data[name]["last_episodes"] = compact_episode_spec(sorted(episodes))
            app_settings["last_profile"] = name
        save_config()
        self.refresh()
        self.profile_ready.emit(name)

    def open_details(self, url):
        entry = wl.find(url)
        if not entry:
            return
        dlg = EntryDialog(entry, self.window())
        # Acted on once the dialog has closed, so the Downloader (or a reopened
        # dialog) never opens underneath a dialog that is still on screen.
        after = []
        dlg.download_part.connect(lambda idx: (after.append(("download", idx)), dlg.accept()))
        dlg.refresh_parts.connect(lambda: after.append(("refresh", None)))
        dlg.exec()
        self.refresh()
        for action, idx in after[:1]:
            # Looked up again: the entry may have been folded into another, or
            # removed, while the dialog was open (its old address still finds it).
            entry = wl.find(url)
            if entry is None:
                return
            if action == "download":
                parts = entry.get("parts") or []
                want = dlg.part_idents[idx] if idx < len(dlg.part_idents) else None
                idx = next((i for i, p in enumerate(parts) if wl.part_ident(p) == want), idx)
                if 0 <= idx < len(parts):
                    self.download(entry, idx)
            else:
                self._detect(entry["url"], then=lambda e: self.open_details(e["url"]))

    def on_move(self, url, status):
        wl.set_status(url, status)
        self.refresh()

    def on_remove(self, url):
        entry, index = wl.remove(url)
        if entry is None:
            return
        self.refresh()
        show_undo(self.window(), f"Removed '{entry.get('title', 'anime')}' from your Library.",
                  lambda: wl.restore(entry, index) and self.refresh())
