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
                            IndeterminateProgressRing, MessageBoxBase, SubtitleLabel,
                            BodyLabel, CheckBox, SwitchButton, LineEdit)

from utils.config import (get_watchlist, remove_watch, update_watch, app_settings, APP_DIR,
                          cloud_register_and_sync, cloud_unsubscribe, save_config)
from ui.styles import apply_danger_style, apply_tinted_style, rounded_pixmap


def _today_key():
    """Today as one of the schedule's canonical day keys."""
    from core.schedule import DAY_ORDER
    # DAY_ORDER starts on Saturday; Python's weekday() has Monday as 0.
    return DAY_ORDER[(time.localtime().tm_wday + 2) % 7]


def entries_airing_today(entries, today):
    """The watchlist entries the automatic check should look at.

    Anime with no release_day are included, not skipped. The day is filled in
    asynchronously by the schedule scrape, and on a first run no entry has one at all,
    so matching strictly on the day would quietly check nothing -- a failure that looks
    exactly like "no new episodes".
    """
    return [w for w in (entries or [])
            if not w.get("release_day") or w.get("release_day") == today]


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
            if self.isInterruptionRequested():
                return
            url = w.get("url", "")
            self.entry_status.emit(url, "Checking…")
            drv = pool.get()
            try:
                found = AnimeDetailsThread("").detect_entries(
                    url, want_covers=False, driver=drv,
                    should_cancel=self.isInterruptionRequested)
                reached = True
            except Exception:
                found, reached = [], False
            finally:
                pool.put(drv)
            if self.isInterruptionRequested():
                return
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


class ScheduleThread(QThread):
    """Scrape both sites' weekly schedules and work out each followed anime's day."""
    done = pyqtSignal(dict)      # {watch url -> day key}
    failed = pyqtSignal(str)

    def __init__(self, entries):
        super().__init__()
        self.entries = list(entries)

    def run(self):
        from core.schedule import fetch_schedule, find_day, SCHEDULE_URLS
        from ui.search_tab import _make_headless_driver
        driver = None
        try:
            driver = _make_headless_driver()
            items = []
            for domain in SCHEDULE_URLS:
                if self.isInterruptionRequested():
                    return
                try:
                    items += fetch_schedule(driver, domain)
                except Exception:
                    continue     # one site being down shouldn't lose the other
            if not items:
                self.failed.emit("Couldn't read the release schedules.")
                return
            self.done.emit({w.get("url"): find_day(w, items) for w in self.entries})
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if driver is not None:
                try: driver.quit()
                except Exception: pass


class EpisodeSelectDialog(MessageBoxBase):
    """Tick which of an anime's new episodes to download.

    Everything starts checked, so confirming without touching anything behaves the
    same as the plain "Download new" button. `selected` holds the chosen episode
    numbers once the dialog is accepted.
    """

    def __init__(self, title, episodes, parent=None):
        super().__init__(parent)
        self.selected = []
        self._boxes = {}

        heading = SubtitleLabel(f"Select episodes — {title}")
        heading.setWordWrap(True)
        self.viewLayout.addWidget(heading)

        n = len(episodes)
        self.lbl_count = BodyLabel("")
        self.viewLayout.addWidget(self.lbl_count)

        # Scrolls, so a long backlog stays usable instead of a dialog taller than the screen.
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(260 if n > 6 else max(80, n * 36))
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        col = QVBoxLayout(host)
        col.setContentsMargins(4, 4, 4, 4)
        col.setSpacing(6)
        col.setAlignment(Qt.AlignmentFlag.AlignTop)
        for ep in episodes:
            cb = CheckBox(f"Episode {ep}")
            cb.setChecked(True)
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.stateChanged.connect(self._refresh_count)
            self._boxes[ep] = cb
            col.addWidget(cb)
        scroll.setWidget(host)
        self.viewLayout.addWidget(scroll)

        # Bulk actions sit under the list, aligned left, with solid backgrounds so
        # they read as buttons rather than plain text.
        tools = QHBoxLayout()
        tools.setSpacing(8)
        btn_all = PushButton("Select all")
        btn_none = PushButton("Clear")
        for b in (btn_all, btn_none):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(32)
        # WinUI 3 dark theme: the primary action uses AccentFillColorDefault
        # (SystemAccentColorLight2 = #4CC2FF, the Windows default blue) with black
        # text, dimming on hover/press. The secondary action uses the neutral
        # ControlFillColor ramp with white text.
        apply_tinted_style(btn_all, "#4CC2FF", "#48B2E9", "#43A2D2", text="#000000")
        apply_tinted_style(btn_none, "#2D2D2D", "#353535", "#272727", text="#FFFFFF")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        tools.addWidget(btn_all)
        tools.addWidget(btn_none)
        tools.addStretch()
        self.viewLayout.addLayout(tools)

        self.widget.setMinimumWidth(420)
        self.yesButton.setText("Download selected")
        self.cancelButton.setText("Cancel")
        self.yesButton.clicked.connect(self._collect)
        self._refresh_count()

    def _set_all(self, state):
        for cb in self._boxes.values():
            cb.setChecked(state)

    def _refresh_count(self):
        chosen = sum(1 for cb in self._boxes.values() if cb.isChecked())
        self.lbl_count.setText(f"{chosen} of {len(self._boxes)} selected")
        self.yesButton.setEnabled(chosen > 0)

    def _collect(self):
        self.selected = [ep for ep, cb in self._boxes.items() if cb.isChecked()]


class CloudSettingsDialog(MessageBoxBase):
    """Modal dialog for configuring Discord Webhook for cloud notifications."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("Cloud Discord Notifications", self)
        self.descLabel = BodyLabel(
            "Enter your Discord Webhook URL to receive automatic release alerts\n"
            "on Discord even when your PC is turned off.", self)
        self.descLabel.setStyleSheet("color: #888888; font-size: 12px;")

        self.wh_label = BodyLabel("Discord Webhook URL:", self)
        self.wh_input = LineEdit(self)
        self.wh_input.setPlaceholderText("https://discord.com/api/webhooks/...")
        self.wh_input.setText(app_settings.get("discord_webhook", ""))

        self.hintLabel = BodyLabel(
            "💡 How to get one: Discord Channel Settings ➔ Integrations ➔ Webhooks ➔ Copy Webhook URL", self)
        self.hintLabel.setStyleSheet("color: #666666; font-size: 11px;")

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descLabel)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(self.wh_label)
        self.viewLayout.addWidget(self.wh_input)
        self.viewLayout.addSpacing(4)
        self.viewLayout.addWidget(self.hintLabel)

        self.widget.setMinimumWidth(480)
        self.yesButton.setText("Save & Connect")
        self.cancelButton.setText("Cancel")


class WatchCard(SimpleCardWidget):
    check_one = pyqtSignal(str)                  # url
    remove_one = pyqtSignal(str)                 # url
    download_new = pyqtSignal(str)               # url
    select_episodes = pyqtSignal(str)            # url -- pick a subset to download

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

        # Only worth showing when there is an actual choice to make (2+ new episodes).
        self.btn_select = PushButton(FIF.CHECKBOX, "Select episodes")
        self.btn_select.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select.setToolTip("Pick which of the new episodes to download")
        self.btn_select.clicked.connect(lambda: self.select_episodes.emit(self.url))
        root.addWidget(self.btn_select)

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
        # With a single new episode there is nothing to choose between.
        self.btn_select.setVisible(new_count > 1)

    def set_status_text(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #f39c12; background: transparent; font-size: 12px;")


class WatchlistWidget(QWidget):
    # (title, template, domain, max_ep, episodes_str) -> create/refresh profile + download
    download_new_signal = pyqtSignal(str, str, str, int, str)
    download_all_signal = pyqtSignal(list)  # [(title, template, domain, max_ep, episodes_str), ...]
    new_episodes_found = pyqtSignal(int)   # total new episodes -> main window opens this tab
    webhook_changed = pyqtSignal(str)     # notifies when Discord webhook is edited

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = {}          # url -> WatchCard
        self._day_headers = []    # release-day group headings, rebuilt with the cards
        self._check_thread = None
        self._schedule_thread = None

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

        self.btn_download_all = PrimaryPushButton(FIF.DOWNLOAD, "Download all new")
        self.btn_download_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download_all.setToolTip("Download the new episodes of every followed anime, one after another")
        self.btn_download_all.clicked.connect(self.download_all_new)
        header.addWidget(self.btn_download_all)

        self.btn_check_all = PushButton(FIF.SYNC, "Check all now")
        self.btn_check_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check_all.setToolTip("Check every followed anime, not just the ones airing today")
        self.btn_check_all.clicked.connect(self.check_all)
        header.addWidget(self.btn_check_all)
        root.addLayout(header)

        sub = QLabel("Follow anime from the Search tab. On launch the app checks the ones "
                     "airing today; use Check all now for the rest.")
        sub.setStyleSheet("color: #999999; background: transparent;")
        root.addWidget(sub)

        # Cloud Notification Settings Card (syncs with cloud service for alerts when PC is off)
        self.cloud_card = SimpleCardWidget()
        self.cloud_card.setFixedHeight(54)
        c_layout = QHBoxLayout(self.cloud_card)
        c_layout.setContentsMargins(14, 6, 14, 6)
        c_layout.setSpacing(12)

        lbl_cloud_ico = QLabel("☁️")
        lbl_cloud_ico.setStyleSheet("font-size: 18px; background: transparent;")
        c_layout.addWidget(lbl_cloud_ico)

        c_info = QVBoxLayout()
        c_info.setSpacing(1)
        lbl_cloud_t = QLabel("Cloud Discord Notifications (When PC is off)")
        lbl_cloud_t.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Bold))
        lbl_cloud_t.setStyleSheet("color: #ffffff; background: transparent;")
        c_info.addWidget(lbl_cloud_t)

        self.lbl_cloud_status = QLabel("Disabled")
        self.lbl_cloud_status.setStyleSheet("color: #888888; font-size: 11px; background: transparent;")
        c_info.addWidget(self.lbl_cloud_status)
        c_layout.addLayout(c_info, 1)

        self.btn_cloud_cfg = ToolButton(FIF.SETTING, self.cloud_card)
        self.btn_cloud_cfg.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cloud_cfg.setToolTip("Configure Cloud Service URL & Discord Webhook")
        self.btn_cloud_cfg.clicked.connect(self._open_cloud_config)
        c_layout.addWidget(self.btn_cloud_cfg)

        self.switch_cloud = SwitchButton(self.cloud_card)
        self.switch_cloud.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch_cloud.setChecked(bool(app_settings.get("cloud_notify_enabled", False)))
        self.switch_cloud.checkedChanged.connect(self._on_cloud_toggle)
        c_layout.addWidget(self.switch_cloud)

        root.addWidget(self.cloud_card)

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
        self._update_cloud_ui()

    # ---- data <-> cards ----
    def refresh_cards(self):
        self._update_cloud_ui()
        # Drop old cards.
        for c in self._cards.values():
            c.setParent(None)
            c.deleteLater()
        self._cards.clear()

        entries = get_watchlist()
        self.empty.setVisible(not entries)
        pending = sum(1 for e in entries if self._pending_download(e) is not None)
        self.btn_download_all.setEnabled(pending > 0)
        self.btn_download_all.setText(
            f"Download all new ({pending})" if pending else "Download all new")
        # Drop old day headers too -- they are rebuilt with the cards below.
        for h in self._day_headers:
            h.setParent(None)
            h.deleteLater()
        self._day_headers.clear()

        # Group by release day, in week order, with unscheduled titles last.
        from core.schedule import DAY_ORDER, DAY_LABELS
        buckets = {}
        for e in entries:
            buckets.setdefault(e.get("release_day") or "", []).append(e)
        ordered = [(d, buckets[d]) for d in DAY_ORDER if d in buckets]
        if "" in buckets:
            ordered.append(("", buckets[""]))

        today = _today_key()
        for day, group in ordered:
            label = DAY_LABELS.get(day, "Day not known yet")
            if day and day == today:
                label += "  ·  today"
            header = QLabel(f"{label}   ({len(group)})")
            header.setStyleSheet(
                "color: #FFFFFF; background: transparent; font-size: 17px; "
                "font-weight: bold; padding: 10px 2px 2px 2px;")
            self.col.insertWidget(self.col.count() - 1, header)
            self._day_headers.append(header)
            for e in group:
                card = WatchCard(e)
                card.check_one.connect(self.check_one)
                card.remove_one.connect(self.remove_one)
                card.download_new.connect(self.on_download_new)
                card.select_episodes.connect(self.on_select_episodes)
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
            self.refresh_schedule()   # a new follow has no release day yet
        else:
            InfoBar.info("Already Watching", f"'{title}' is already in your Watchlist.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())

    def remove_one(self, url):
        remove_watch(url)
        self.refresh_cards()

    # ---- checking ----
    def check_all(self):
        self._start_check(get_watchlist())

    def check_today(self):
        """Check the anime airing today, plus any whose day isn't known yet.

        This is the automatic check on launch. Sweeping the whole watchlist there was
        slow and mostly re-read anime that cannot have a new episode today; "Check all
        now" remains the way to force a full pass.

        See entries_airing_today() for why anime with no known day are included.
        """
        self._start_check(entries_airing_today(get_watchlist(), _today_key()))

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
        entry = next((w for w in get_watchlist() if w.get("url") == url), {})
        if card:
            card.apply_entry(entry)

        # Dispatch Discord release notification if webhook is configured
        if new_count > 0 and not first_time:
            webhook = app_settings.get("discord_webhook", "").strip()
            if webhook:
                anime_title = entry.get("title") or url
                seen = entry.get("seen_max") or 0
                import threading
                from service.checker import create_discord_embed, send_discord_notification
                def _bg_notify(title, a_url, s_max, l_max, wh):
                    for ep in range(s_max + 1, l_max + 1):
                        payload = create_discord_embed(title, a_url, ep)
                        send_discord_notification(wh, payload)
                        time.sleep(0.2)
                threading.Thread(target=_bg_notify, args=(anime_title, url, seen, latest_max, webhook), daemon=True).start()

    def refresh_schedule(self):
        """Look up each followed anime's release day from the sites' schedule pages."""
        entries = get_watchlist()
        if not entries or (self._schedule_thread and self._schedule_thread.isRunning()):
            return
        th = ScheduleThread(entries)
        th.done.connect(self._on_schedule_done)
        th.failed.connect(lambda _msg: None)   # a missing schedule is not worth nagging about
        self._schedule_thread = th
        th.start()

    def _on_schedule_done(self, days):
        changed = False
        for url, day in days.items():
            existing = next((w for w in get_watchlist() if w.get("url") == url), None)
            if existing is not None and existing.get("release_day") != day:
                update_watch(url, release_day=day)
                changed = True
        if changed:
            self.refresh_cards()

    def _on_all_done(self, total_new):
        self.spinner.hide()
        self.btn_check_all.setEnabled(True)
        self.refresh_schedule()   # days can change between seasons, so re-read them
        if total_new > 0:
            _play_notify_sound()
            # Bring the user straight here so the new episodes are actually seen.
            self.new_episodes_found.emit(total_new)
            InfoBar.success(
                "New Episodes Available",
                f"{total_new} new episode{'s' if total_new != 1 else ''} across your Watchlist.",
                position=InfoBarPosition.TOP, duration=6000, parent=self.window())

    # ---- download new ----
    @staticmethod
    def _pending_download(w):
        """Return (title, template, domain, latest, episodes_str) for an entry with
        unwatched episodes, or None if it has nothing new / no usable template."""
        seen = w.get("seen_max") or 0
        latest = w.get("latest_max") or 0
        template = w.get("latest_template", "")
        if not template or latest <= seen:
            return None
        episodes_str = f"{seen + 1}-{latest}" if latest > seen + 1 else str(latest)
        return (w.get("title", "Anime"), template, w.get("domain", ""), latest, episodes_str)

    def on_download_new(self, url):
        w = next((x for x in get_watchlist() if x.get("url") == url), None)
        if not w:
            return
        item = self._pending_download(w)
        if item is None:
            InfoBar.info("Nothing New", "No new episodes to download right now.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            return
        # Acknowledge: from now on these count as seen.
        update_watch(url, seen_max=item[3], new_count=0)
        self.refresh_cards()
        self.download_new_signal.emit(*item)

    def on_select_episodes(self, url):
        """Let the user pick a subset of one anime's new episodes, then download those."""
        from ui.downloader_tab import compact_episode_spec
        w = next((x for x in get_watchlist() if x.get("url") == url), None)
        if not w:
            return
        item = self._pending_download(w)
        if item is None:
            InfoBar.info("Nothing New", "No new episodes to download right now.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            return
        title, template, domain, latest, _spec = item
        seen = w.get("seen_max") or 0
        new_eps = list(range(seen + 1, latest + 1))

        dlg = EpisodeSelectDialog(title, new_eps, self.window())
        if not dlg.exec() or not dlg.selected:
            return
        chosen = sorted(dlg.selected)

        # Only advance "seen" across the unbroken run the user actually took. Anything
        # after a skipped episode stays flagged as new, so nothing is silently lost.
        new_seen = seen
        for ep in new_eps:
            if ep in chosen:
                new_seen = ep
            else:
                break
        remaining = max(0, latest - new_seen)
        update_watch(url, seen_max=new_seen, new_count=remaining)
        self.refresh_cards()
        self.download_new_signal.emit(title, template, domain, latest,
                                      compact_episode_spec(chosen))

    def download_all_new(self):
        """Queue every followed anime that has new episodes. They download one after
        another (the engine runs a single task at a time)."""
        items = []
        for w in get_watchlist():
            item = self._pending_download(w)
            if item is not None:
                items.append((w["url"], item))
        if not items:
            InfoBar.info("Nothing New", "No new episodes across your Watchlist.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())
            return
        # Acknowledge everything up front so a later check doesn't re-flag them.
        for url, item in items:
            update_watch(url, seen_max=item[3], new_count=0)
        self.refresh_cards()
        InfoBar.success("Queued", f"Downloading new episodes for {len(items)} anime.",
                        position=InfoBarPosition.TOP, duration=4000, parent=self.window())
        self.download_all_signal.emit([i for _, i in items])

    # ---- Cloud sync handlers ----
    def _update_cloud_ui(self):
        enabled = bool(app_settings.get("cloud_notify_enabled", False))
        sub_id = app_settings.get("cloud_subscriber_id", "")
        self.switch_cloud.blockSignals(True)
        self.switch_cloud.setChecked(enabled)
        self.switch_cloud.blockSignals(False)

        if enabled and sub_id:
            n = len(get_watchlist())
            self.lbl_cloud_status.setText(f"✓ Active — {n} anime synced to cloud (checks even when PC is off)")
            self.lbl_cloud_status.setStyleSheet("color: #51cf66; font-size: 11px; background: transparent;")
        elif enabled:
            self.lbl_cloud_status.setText("Connecting / Registering with cloud...")
            self.lbl_cloud_status.setStyleSheet("color: #f39c12; font-size: 11px; background: transparent;")
        else:
            self.lbl_cloud_status.setText("Disabled (PC must be on to check)")
            self.lbl_cloud_status.setStyleSheet("color: #888888; font-size: 11px; background: transparent;")

    def _open_cloud_config(self):
        from utils.config import DEFAULT_CLOUD_SERVICE_URL
        dlg = CloudSettingsDialog(self.window())
        if not dlg.exec():
            # If user cancelled and cloud is not actively registered, turn switch back off
            if not app_settings.get("cloud_subscriber_id"):
                self.switch_cloud.blockSignals(True)
                self.switch_cloud.setChecked(False)
                self.switch_cloud.blockSignals(False)
            self._update_cloud_ui()
            return

        new_wh = dlg.wh_input.text().strip()
        if not new_wh:
            # User submitted an empty webhook
            if not app_settings.get("cloud_subscriber_id"):
                self.switch_cloud.blockSignals(True)
                self.switch_cloud.setChecked(False)
                self.switch_cloud.blockSignals(False)
            self._update_cloud_ui()
            InfoBar.warning("Webhook Required", "A Discord Webhook URL is required to enable cloud notifications.",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
            return

        s_url = app_settings.get("cloud_service_url") or DEFAULT_CLOUD_SERVICE_URL
        app_settings["cloud_service_url"] = s_url
        app_settings["discord_webhook"] = new_wh
        save_config()
        self.webhook_changed.emit(new_wh)

        self._connect_cloud(s_url, new_wh)

    def on_webhook_updated(self, webhook_url: str):
        self._update_cloud_ui()

    def _on_cloud_toggle(self, checked):
        from utils.config import DEFAULT_CLOUD_SERVICE_URL
        if checked:
            s_url = app_settings.get("cloud_service_url") or DEFAULT_CLOUD_SERVICE_URL
            wh_url = app_settings.get("discord_webhook", "")
            if not wh_url:
                self._open_cloud_config()
                return
            self._connect_cloud(s_url, wh_url)
        else:
            import threading
            def _bg_unsub():
                from utils.config import set_windows_autostart
                cloud_unsubscribe()
                set_windows_autostart(False)
            threading.Thread(target=_bg_unsub, daemon=True).start()
            self._update_cloud_ui()
            InfoBar.info("Cloud Sync Disabled", "Removed registration from cloud service.",
                         position=InfoBarPosition.TOP, duration=3000, parent=self.window())

    def _connect_cloud(self, service_url, webhook_url):
        import threading
        self.lbl_cloud_status.setText("Registering with cloud service...")
        self.lbl_cloud_status.setStyleSheet("color: #f39c12; font-size: 11px; background: transparent;")

        def _bg_reg():
            ok, msg = cloud_register_and_sync(service_url, webhook_url)
            from PyQt6.QtCore import QTimer
            if ok:
                QTimer.singleShot(0, lambda: self._on_reg_success(msg))
            else:
                QTimer.singleShot(0, lambda: self._on_reg_failed(msg))

        threading.Thread(target=_bg_reg, daemon=True).start()

    def _on_reg_success(self, msg):
        self._update_cloud_ui()
        from utils.config import set_windows_autostart
        set_windows_autostart(True)
        InfoBar.success("Cloud Notifications & Remote Queue Active", msg,
                        position=InfoBarPosition.TOP, duration=4000, parent=self.window())

    def _on_reg_failed(self, msg):
        self.switch_cloud.blockSignals(True)
        self.switch_cloud.setChecked(False)
        self.switch_cloud.blockSignals(False)
        self._update_cloud_ui()
        InfoBar.error("Cloud Connection Failed", msg,
                      position=InfoBarPosition.TOP, duration=5000, parent=self.window())
