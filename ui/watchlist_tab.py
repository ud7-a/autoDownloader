import os
import sys
import time
import hashlib
import shutil

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from qfluentwidgets import (PushButton, PrimaryPushButton, SimpleCardWidget, SmoothScrollArea,
                            ToolButton, FluentIcon as FIF, InfoBar, InfoBarPosition,
                            IndeterminateProgressRing)

from utils.config import (get_watchlist, remove_watch, update_watch, app_settings, APP_DIR)
from ui.styles import apply_danger_style, rounded_pixmap


def _persist_cover(url, cover_path):
    """Copy a (temp-cached) cover into a durable Watchlist folder so it survives
    temp pruning. Returns the persistent path, or "" if there's nothing to copy."""
    if not cover_path or not os.path.exists(cover_path):
        return ""
    try:
        dest_dir = os.path.join(APP_DIR, "watchlist_covers")
        os.makedirs(dest_dir, exist_ok=True)
        key = hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
        dest = os.path.join(dest_dir, f"{key}.img")
        shutil.copy2(cover_path, dest)
        return dest
    except Exception:
        return ""


def _builtin_sound_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets", "finishingDownloadSound.wav")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "finishingDownloadSound.wav")


def _play_notify_sound():
    """Play the app's finish sound to signal new episodes (best-effort, non-blocking)."""
    try:
        import ctypes, threading
        selected = app_settings.get("selected_sound", "")
        if selected == "__none__":
            return
        path = selected if (selected and os.path.exists(selected)) else _builtin_sound_path()
        if not os.path.exists(path):
            return
        vol = int(app_settings.get("volume", 100)) * 10

        def play():
            try:
                p = path.replace("\\", "/")
                ctypes.windll.winmm.mciSendStringW('close watch_audio', None, 0, None)
                ctypes.windll.winmm.mciSendStringW(f'open "{p}" type mpegvideo alias watch_audio', None, 0, None)
                ctypes.windll.winmm.mciSendStringW(f'setaudio watch_audio volume to {vol}', None, 0, None)
                ctypes.windll.winmm.mciSendStringW('play watch_audio', None, 0, None)
            except Exception:
                pass
        threading.Thread(target=play, daemon=True).start()
    except Exception:
        pass


class WatchlistCheckThread(QThread):
    """Re-detect the current episode count for followed anime and report which have
    grown since they were last acknowledged. Reuses the search detection engine."""
    entry_status = pyqtSignal(str, str)                       # (url, live status text)
    entry_done = pyqtSignal(str, int, int, str, bool)         # (url, latest_max, new_count, template, first_time)
    all_done = pyqtSignal(int)                                # total new episodes found

    def __init__(self, entries):
        super().__init__()
        self.entries = entries

    MAX_PARALLEL = 3

    def run(self):
        import queue as _queue
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from ui.search_tab import AnimeDetailsThread, _make_headless_driver

        entries = list(self.entries)
        n = min(self.MAX_PARALLEL, len(entries)) or 1

        # A small pool of lean, short-lived headless browsers -- each worker borrows
        # one, so up to `n` anime are checked at the same time.
        drivers = []
        pool = _queue.Queue()
        for _ in range(n):
            try:
                d = _make_headless_driver()
                drivers.append(d)
                pool.put(d)
            except Exception:
                break
        if not drivers:
            self.all_done.emit(0)
            return

        total = [0]
        total_lock = threading.Lock()

        def check(w):
            url = w.get("url", "")
            self.entry_status.emit(url, "Checking…")
            drv = pool.get()
            try:
                found = AnimeDetailsThread("").detect_entries(url, want_covers=False, driver=drv)
                reached = True
            except Exception:
                found, reached = [], False
            finally:
                pool.put(drv)
            if not reached:
                # Couldn't load (network/blocked) -> leave the entry untouched.
                self.entry_status.emit(url, "Check failed")
                return
            # Page reached. Empty = the anime exists but has no episodes yet -> treat
            # as 0 so we baseline it and notify once the first episode drops.
            if found:
                current = max(found, key=lambda e: e.get("max_ep", 0))
                latest_max = int(current.get("max_ep", 0))
                template = current.get("template", "")
            else:
                latest_max, template = 0, ""
            seen = w.get("seen_max")
            first_time = seen is None
            new_count = 0 if first_time else max(0, latest_max - int(seen))
            with total_lock:
                total[0] += new_count
            self.entry_done.emit(url, latest_max, new_count, template, first_time)

        try:
            with ThreadPoolExecutor(max_workers=len(drivers)) as ex:
                list(ex.map(check, entries))
        finally:
            for d in drivers:
                try: d.quit()
                except Exception: pass

        self.all_done.emit(total[0])


class WatchCard(SimpleCardWidget):
    check_one = pyqtSignal(str)                  # url
    remove_one = pyqtSignal(str)                 # url
    download_new = pyqtSignal(str)               # url

    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.url = entry.get("url", "")
        self.setFixedHeight(104)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 16, 10)
        root.setSpacing(14)

        # Poster (rounded corners), or a placeholder glyph if none stored.
        poster = QLabel()
        poster.setFixedSize(56, 84)
        poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover = entry.get("cover", "")
        pix = rounded_pixmap(cover, 56, 84, 6) if cover else None
        if pix is not None:
            poster.setPixmap(pix)
        else:
            poster.setText("🎞️")
            poster.setStyleSheet("border-radius: 6px; background-color: #1e1e1e; "
                                 "color: #555555; font-size: 24px;")
        root.addWidget(poster)

        info = QVBoxLayout()
        info.setSpacing(2)
        title = QLabel(entry.get("title", "Anime"))
        title.setFont(QFont("Segoe UI Variable", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        info.addWidget(title)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("background: transparent; font-size: 12px;")
        info.addWidget(self.lbl_status)

        sub = QLabel(entry.get("domain", ""))
        sub.setStyleSheet("color: #777777; background: transparent; font-size: 11px;")
        info.addWidget(sub)

        root.addLayout(info, 1)

        self.btn_download = PrimaryPushButton(FIF.DOWNLOAD, "Download new")
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.clicked.connect(lambda: self.download_new.emit(self.url))
        root.addWidget(self.btn_download)

        self.btn_check = PushButton(FIF.SYNC, "Check")
        self.btn_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check.clicked.connect(lambda: self.check_one.emit(self.url))
        root.addWidget(self.btn_check)

        btn_remove = ToolButton(FIF.DELETE, self)
        btn_remove.setObjectName("Danger")
        apply_danger_style(btn_remove)
        btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_remove.setFixedSize(40, 40)
        btn_remove.setToolTip("Stop following")
        btn_remove.clicked.connect(lambda: self.remove_one.emit(self.url))
        root.addWidget(btn_remove)

        self.apply_entry(entry)

    def apply_entry(self, entry):
        seen = entry.get("seen_max")
        latest = entry.get("latest_max")
        new_count = int(entry.get("new_count", 0) or 0)
        if new_count > 0:
            self.lbl_status.setText(f"🔔 {new_count} new episode{'s' if new_count != 1 else ''}!  "
                                    f"(up to ep {latest})")
            self.lbl_status.setStyleSheet("color: #51cf66; background: transparent; "
                                          "font-size: 12px; font-weight: bold;")
            self.btn_download.setEnabled(True)
        else:
            self.lbl_status.setStyleSheet("color: #999999; background: transparent; font-size: 12px;")
            if seen is None:
                self.lbl_status.setText("Not checked yet")
            elif not seen:
                self.lbl_status.setText("✓ No episodes yet — watching for the first one")
            else:
                self.lbl_status.setText(f"✓ Up to date  (ep {seen})")
            self.btn_download.setEnabled(False)

    def set_status_text(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #f39c12; background: transparent; font-size: 12px;")


class WatchlistWidget(QWidget):
    # (title, template, domain, max_ep, episodes_str) -> create/refresh profile + download
    download_new_signal = pyqtSignal(str, str, str, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = {}          # url -> WatchCard
        self._check_thread = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Watchlist")
        title.setFont(QFont("Segoe UI Variable", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        header.addWidget(title)
        header.addStretch()

        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(24, 24)
        self.spinner.hide()
        header.addWidget(self.spinner)

        self.btn_check_all = PushButton(FIF.SYNC, "Check all now")
        self.btn_check_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check_all.clicked.connect(self.check_all)
        header.addWidget(self.btn_check_all)
        root.addLayout(header)

        sub = QLabel("Follow anime from the Search tab; the app checks for new episodes "
                     "on launch and whenever you press Check.")
        sub.setStyleSheet("color: #999999; background: transparent;")
        root.addWidget(sub)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.host = QWidget()
        self.host.setStyleSheet("background: transparent;")
        self.col = QVBoxLayout(self.host)
        self.col.setSpacing(10)
        self.col.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)

        self.empty = QLabel("📺  You're not following any anime yet.\n\n"
                            "Open Search, load an anime, and press ♥ to follow it.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: #888888; background: transparent; font-size: 14px;")
        self.col.addWidget(self.empty)

        self.refresh_cards()

    # ---- data <-> cards ----
    def refresh_cards(self):
        # Drop old cards.
        for c in self._cards.values():
            c.setParent(None)
            c.deleteLater()
        self._cards.clear()

        entries = get_watchlist()
        self.empty.setVisible(not entries)
        for e in entries:
            card = WatchCard(e)
            card.check_one.connect(self.check_one)
            card.remove_one.connect(self.remove_one)
            card.download_new.connect(self.on_download_new)
            self.col.insertWidget(self.col.count() - 1, card)   # keep empty label last
            self._cards[e.get("url", "")] = card

    def follow(self, title, url, domain, cover=""):
        """Add an anime to the watchlist (called from Search) and check it once."""
        from utils.config import add_watch
        added = add_watch({"title": title, "url": url, "domain": domain,
                           "cover": _persist_cover(url, cover),
                           "seen_max": None, "latest_max": None,
                           "latest_template": "", "new_count": 0, "checked": 0})
        self.refresh_cards()
        if added:
            InfoBar.success("Now Watching", f"'{title}' added to your Watchlist.",
                            position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            self._start_check([w for w in get_watchlist() if w.get("url") == url])
        else:
            InfoBar.info("Already Watching", f"'{title}' is already in your Watchlist.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())

    def remove_one(self, url):
        remove_watch(url)
        self.refresh_cards()

    # ---- checking ----
    def check_all(self):
        self._start_check(get_watchlist())

    def check_one(self, url):
        self._start_check([w for w in get_watchlist() if w.get("url") == url])

    def _start_check(self, entries):
        if not entries or (self._check_thread and self._check_thread.isRunning()):
            return
        self.spinner.show()
        self.btn_check_all.setEnabled(False)
        for w in entries:
            card = self._cards.get(w.get("url"))
            if card:
                card.set_status_text("Checking…")
        th = WatchlistCheckThread(entries)
        th.entry_status.connect(self._on_entry_status)
        th.entry_done.connect(self._on_entry_done)
        th.all_done.connect(self._on_all_done)
        self._check_thread = th
        th.start()

    def _on_entry_status(self, url, text):
        card = self._cards.get(url)
        if card:
            card.set_status_text(text)

    def _on_entry_done(self, url, latest_max, new_count, template, first_time):
        fields = {"latest_max": latest_max, "latest_template": template,
                  "new_count": new_count, "checked": time.time()}
        if first_time:
            fields["seen_max"] = latest_max
        update_watch(url, **fields)
        card = self._cards.get(url)
        if card:
            card.apply_entry(next((w for w in get_watchlist() if w.get("url") == url), {}))

    def _on_all_done(self, total_new):
        self.spinner.hide()
        self.btn_check_all.setEnabled(True)
        if total_new > 0:
            _play_notify_sound()
            InfoBar.success(
                "New Episodes Available",
                f"{total_new} new episode{'s' if total_new != 1 else ''} across your Watchlist.",
                position=InfoBarPosition.TOP, duration=6000, parent=self.window())

    # ---- download new ----
    def on_download_new(self, url):
        w = next((x for x in get_watchlist() if x.get("url") == url), None)
        if not w:
            return
        seen = w.get("seen_max") or 0
        latest = w.get("latest_max") or 0
        template = w.get("latest_template", "")
        if not template or latest <= seen:
            InfoBar.info("Nothing New", "No new episodes to download right now.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            return
        episodes_str = f"{seen + 1}-{latest}" if latest > seen + 1 else str(latest)
        # Acknowledge: from now on these count as seen.
        update_watch(url, seen_max=latest, new_count=0)
        self.refresh_cards()
        self.download_new_signal.emit(w.get("title", "Anime"), template, w.get("domain", ""),
                                      latest, episodes_str)
