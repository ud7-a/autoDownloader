import os
import threading
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog)

# THE UPGRADE: We are using Fluent Widgets for everything!
from qfluentwidgets import (PushButton, PrimaryPushButton, LineEdit, CheckBox,
                            ComboBox, SmoothScrollArea, SpinBox, ToolButton, FluentIcon as FIF,
                            InfoBar, InfoBarPosition)

from utils.config import app_settings, sites_data, save_config, config_lock
from core.signals import signals
from core.selenium_engine import run_selenium_task
from utils.sounds import builtin_sound_path
from ui.styles import apply_danger_style
from utils.naming import safe_folder_name


class FillerLookupThread(QThread):
    """Read a profile's filler episode numbers off its site, once.

    Both sites list every episode of the show on any single episode page -- witanime
    badges them "فيلر", animerco writes "- فلر" into the list item -- so one page is
    enough, whichever episode it is. Runs off the GUI thread because witanime's page
    is ~900 KB.
    """
    done = pyqtSignal(list, str, str)   # (episode numbers, error message, source site)

    def __init__(self, url_template, title="", parent=None):
        super().__init__(parent)
        self.url_template = url_template or ""
        self.title = title or ""

    def run(self):
        from core.filler import parse_filler_episodes
        from ui.search_tab import site_display_name, extract_domain
        url = self.url_template.replace("{x}", "1")
        here = site_display_name(extract_domain(url))
        try:
            episodes = parse_filler_episodes(self._page(url))
        except Exception as err:
            self.done.emit([], f"{type(err).__name__}: {err}", "")
            return
        # Ask the other site as well, and merge -- filler numbering belongs to the
        # anime, so both lists describe the same episodes.
        #
        # "Only when this site found nothing" was the obvious rule and it is wrong:
        # animerco marks 2 of Bleach's 366 episodes where witanime marks 163, so a
        # non-empty answer there is not a complete one. An animerco profile
        # therefore always asks witanime (one fetch, about a second, far better
        # data), while a witanime profile only bothers when it came up empty --
        # animerco needs a browser, takes ~5 s, and rarely adds anything.
        from core.filler import wants_other_site
        want_other = wants_other_site(url, episodes)
        other, source = [], ""
        if want_other and not self.isInterruptionRequested():
            try:
                other, source = self._from_other_site(url)
            except Exception:
                other, source = [], ""      # a failed fallback is just "none found"

        merged = sorted(set(episodes) | set(other))
        if episodes and other:
            label = f"{here} + {source}"
        elif other:
            label = source
        else:
            label = here
        self.done.emit(merged, "", label if merged else here)

    def _page(self, url):
        """The page's HTML, fetched the way the app already reaches that site."""
        if "witanime" in url:
            from ui.search_tab import fetch_anime_page
            return fetch_anime_page(url)
        # animerco serves this list to a normal headless browser.
        from ui.search_tab import _make_headless_driver
        driver = _make_headless_driver()
        try:
            driver.get(url)
            return driver.page_source
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def _from_other_site(self, url):
        """(episodes, site name) for the same anime on the other supported site."""
        from urllib.parse import quote
        from core.filler import parse_filler_episodes, pick_cross_site_match
        from ui.search_tab import (SUPPORTED_SITES, fetch_anime_page,
                                   parse_witanime_results, site_display_name,
                                   page_anchors, _make_headless_driver)
        if not self.title or self.isInterruptionRequested():
            return [], ""

        if "witanime" in url:               # witanime profile -> ask animerco
            domain = "eta.animerco.org"
            search = SUPPORTED_SITES[domain].replace("{query}", quote(self.title))
            driver = _make_headless_driver()
            try:
                driver.get(search)
                rows = [{"title": (r.get("title") or r.get("text") or "").strip(),
                         "link": r.get("href", "")}
                        for r in page_anchors(driver) if "/animes/" in (r.get("href") or "")]
                hit = pick_cross_site_match(self.title, rows)
                if not hit:
                    return [], ""
                driver.get(hit["link"])
                page = driver.page_source
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
            # animerco marks filler on EPISODE pages, so follow the first episode.
            first = self._first_animerco_episode(page)
            return (parse_filler_episodes(self._page(first)) if first else []), "animerco"

        domain = "witanime.site"            # animerco profile -> ask witanime
        search = SUPPORTED_SITES[domain].replace("{query}", quote(self.title))
        hit = pick_cross_site_match(self.title, parse_witanime_results(fetch_anime_page(search)))
        if not hit:
            return [], ""
        return parse_filler_episodes(fetch_anime_page(hit["link"])), site_display_name(domain)

    @staticmethod
    def _first_animerco_episode(html):
        import re
        found = re.search(r'''href=['"]([^'"]*/episodes/[^'"]+)['"]''', html or "", re.I)
        return found.group(1) if found else ""


def compact_episode_spec(nums):
    """Collapse a list of episode numbers into a compact spec string ('1-5, 8-12')."""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def spec_to_ranges(text):
    """Parse a spec string ('1-5, 8-12', '5') into a list of (from, to) tuples.
    Tolerant -- unparseable tokens are skipped. Used to seed the range picker."""
    ranges = []
    for tok in (text or "").split(","):
        tok = tok.strip().replace("–", "-").replace("—", "-")   # normalize en/em dashes
        if not tok:
            continue
        if "-" in tok:
            parts = [p.strip() for p in tok.split("-")]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                ranges.append((int(parts[0]), int(parts[1])))
        elif tok.isdigit():
            ranges.append((int(tok), int(tok)))
    return ranges


class EpisodeRangePicker(QWidget):
    """Pick episodes as one or more inclusive From/To ranges.

    One range is always shown; 'Add another range' reveals more so gaps are
    possible (e.g. 1-5 and 8-12). No syntax for the user to get wrong -- the
    number boxes can't hold letters or negatives. Emits `changed` on any edit,
    and serializes to/from the compact spec string used elsewhere.
    """
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._limits = None        # (first, last) episode the anime has, if known

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(6)
        root.addLayout(self._rows_box)

        self.btn_add = PushButton(FIF.ADD, "Add another range")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.setMinimumHeight(36)
        self.btn_add.clicked.connect(self._on_add_clicked)
        root.addWidget(self.btn_add)   # full-width button

        self._add_row()   # always start with one range

    def _on_add_clicked(self):
        self._add_row()
        self.changed.emit()

    def _add_row(self, a=1, b=1):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        lbl_from = QLabel("From")
        lbl_from.setStyleSheet("background: transparent;")
        sp_from = SpinBox()
        sp_to = SpinBox()
        for sp, val in ((sp_from, a), (sp_to, b)):
            self._apply_limits(sp)
            sp.setValue(val)          # clamped into the anime's episodes when known
            sp.setMinimumWidth(80)   # keep the up/down arrows off the digits
            sp.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            sp.wheelEvent = lambda e: e.ignore()   # don't change value on scroll
        lbl_to = QLabel("to")
        lbl_to.setStyleSheet("background: transparent;")
        sp_from.valueChanged.connect(self.changed.emit)
        sp_to.valueChanged.connect(self.changed.emit)

        btn_rm = ToolButton(FIF.DELETE, self)
        btn_rm.setObjectName("Danger")
        apply_danger_style(btn_rm)
        btn_rm.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rm.setFixedSize(40, 40)
        btn_rm.setToolTip("Remove this range")

        # From/To each take an equal (~50%) share of the row width.
        h.addWidget(lbl_from)
        h.addWidget(sp_from, 1)
        h.addWidget(lbl_to)
        h.addWidget(sp_to, 1)
        h.addWidget(btn_rm)

        entry = {"widget": row, "from": sp_from, "to": sp_to, "rm": btn_rm}
        btn_rm.clicked.connect(lambda: self._remove_row(entry))
        self._rows.append(entry)
        self._rows_box.addWidget(row)
        self._update_remove_buttons()
        return entry

    def _remove_row(self, entry):
        if len(self._rows) <= 1:
            return
        self._rows.remove(entry)
        entry["widget"].setParent(None)
        entry["widget"].deleteLater()
        self._update_remove_buttons()
        self.changed.emit()

    def _update_remove_buttons(self):
        # No point removing the only range -- hide its ✕.
        only = len(self._rows) <= 1
        for e in self._rows:
            e["rm"].setVisible(not only)

    def _apply_limits(self, sp):
        if self._limits:
            first, last = self._limits
            sp.setRange(first, last)
            sp.setToolTip(f"This anime has episodes {first}–{last}.")
        else:
            sp.setRange(0, 99999)
            sp.setToolTip("")

    def set_limits(self, limits):
        """Cap every box to (first, last) -- the anime's real episodes -- or lift
        the cap with None. Values already outside are pulled inside."""
        self._limits = tuple(limits) if limits else None
        for e in self._rows:
            for sp in (e["from"], e["to"]):
                self._apply_limits(sp)

    def limits(self):
        return self._limits

    def ranges(self):
        return [(e["from"].value(), e["to"].value()) for e in self._rows]

    def episodes(self):
        """Return (sorted_unique_episode_list, None) or ([], error_message)."""
        eps = set()
        for a, b in self.ranges():
            if a > b:
                return [], f"Range {a}–{b} is backwards — 'From' must be ≤ 'To'."
            eps.update(range(a, b + 1))
        if not eps:
            return [], "Pick at least one episode."
        return sorted(eps), None

    def spec(self):
        """Compact spec string for saving/history; falls back to raw ranges if invalid."""
        eps, err = self.episodes()
        if not err:
            return compact_episode_spec(eps)
        return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in self.ranges())

    def set_spec(self, text):
        """Fill the boxes with a spec ("1-5, 8-12").

        A spec handed in -- a saved range, a History re-download -- names episodes
        that do exist (they were picked or downloaded before), so it widens the
        limits rather than being bent to fit them: replacing it once turned a "25-26"
        re-download into all 24 episodes. The limits only stop typing past them.
        """
        ranges = spec_to_ranges(text) or [(1, 1)]
        if self._limits:
            first, last = self._limits
            lo = min([first] + [a for a, _b in ranges])
            hi = max([last] + [b for _a, b in ranges])
            if (lo, hi) != (first, last):
                self.set_limits((lo, hi))
        for e in list(self._rows):
            e["widget"].setParent(None)
            e["widget"].deleteLater()
        self._rows.clear()
        for a, b in ranges:
            self._add_row(a, b)
        self._update_remove_buttons()


def encode_check_url(url):
    """Percent-encode a Base URL's path/query so it can be requested.

    These profiles' Base URLs often hold raw Arabic (…/episode/one-piece-الحلقة-444/),
    and urllib's Request() raises UnicodeEncodeError on a non-ASCII URL before any
    request goes out. Already-encoded URLs pass through unchanged ('%' stays safe), so
    this is idempotent.
    """
    import urllib.parse
    sp = urllib.parse.urlsplit(url or "")
    return urllib.parse.urlunsplit((
        sp.scheme,
        sp.netloc,
        urllib.parse.quote(sp.path, safe="/%~"),
        urllib.parse.quote(sp.query, safe="=&%~"),
        "",
    ))


class ConnectionCheckThread(QThread):
    """Check a profile's Base URL is reachable, off the UI thread -- so clicking
    Start never freezes the window while the network connection times out."""
    result = pyqtSignal(bool, str)   # (ok, error_message)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        ok, err = False, ""
        try:
            import urllib.request, urllib.parse, ssl, socket
            from urllib.error import HTTPError
            # Encode first: a raw-Arabic Base URL would otherwise raise
            # UnicodeEncodeError here and skip both reachability tiers below.
            check_url = encode_check_url(self.url)
            req = urllib.request.Request(
                check_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            )
            if self.isInterruptionRequested():
                self.result.emit(False, "cancelled")
                return
            try:
                # Tier 1: standard TLS-verified reach.
                with urllib.request.urlopen(req, timeout=5.0):
                    ok = True
            except HTTPError:
                ok = True   # any HTTP status means the server is alive
            except Exception as first_err:
                s = str(first_err).lower()
                if any(w in s for w in ["ssl", "cert", "handshake", "verification", "untrusted"]):
                    # Tier 2: only if it failed on TLS validation, retry unverified.
                    try:
                        ctx = ssl._create_unverified_context()
                        with urllib.request.urlopen(req, timeout=5.0, context=ctx):
                            ok = True
                    except HTTPError:
                        ok = True
                    except Exception as fb:
                        err = str(fb)
                else:
                    err = str(first_err)
            # Tier 3: DNS resolves -> site is up even if Cloudflare reset the socket.
            if not ok:
                host = urllib.parse.urlsplit(self.url).netloc.split(':')[0]
                try:
                    if host:
                        socket.gethostbyname(host)
                        ok = True
                except Exception as dns_err:
                    # Tier 4: the machine's resolver is the problem, not the site. ISP
                    # resolvers routinely return NXDOMAIN for these domains, and every
                    # tier above this one goes through that same resolver, so the check
                    # failed while the download would have worked: all three browsers
                    # pass --host-resolver-rules built from Google Public DNS (see
                    # utils.browser_flags). Ask the same resolver they use before
                    # blocking Start over a name the browser can resolve fine.
                    resolved = False
                    if host:
                        try:
                            from utils.browser_flags import google_resolve
                            resolved = bool(google_resolve(host))
                        except Exception:
                            resolved = False
                    if resolved:
                        ok = True
                    else:
                        err = f"DNS resolution failed: {dns_err}"
        except Exception as e:
            # Any unexpected failure must still report back, or Start stays disabled.
            err = str(e)
        self.result.emit(ok, err)


class DownloaderWidget(QWidget):
    goto_profiles_signal = pyqtSignal()   # asks the main window to open Profile Manager
    settings_requested = pyqtSignal()     # opens the app preferences page

    def __init__(self, parent=None):
        super().__init__(parent)

        self._episodes_valid = True
        self._checking = False   # True while the async connection check is in flight
        self._conn_infobar = None   # the on-screen connection-failure bar, if any
        # True whenever the inputs are disabled -- a task is running OR there is no
        # profile to run. Toggling auto/manual must not re-enable controls in either.
        self._inputs_locked = False
        self.last_download_folder = ""    # set per task, used by "Start Watching"
        self.last_download_episodes = []  # episode numbers of the most recent task
        self.last_download_params = None  # full task args, for retrying failed episodes
        self.last_watch_meta = None       # (title, template, domain) of a Watchlist task

        # Debounced config saver -- coalesces rapid setting edits into one disk write.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(save_config)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        
        # Fluent Scroll Area
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        
        main_layout = QVBoxLayout(self.content)
        main_layout.setSpacing(15) 
        main_layout.setContentsMargins(30, 20, 30, 20)

        header = QHBoxLayout()
        title = QLabel("Download episodes")
        title.setStyleSheet("font-size: 22px; font-weight: bold; background: transparent;")
        header.addWidget(title)
        header.addStretch()
        self.btn_settings = PushButton(FIF.SETTING, "Settings")
        self.btn_settings.setToolTip("Notification preferences and download defaults")
        self.btn_settings.clicked.connect(self.settings_requested.emit)
        header.addWidget(self.btn_settings)
        main_layout.addLayout(header)

        main_layout.addWidget(QLabel("Destination folder", styleSheet="font-weight: bold; margin-top: 10px;"))
        dir_layout = QHBoxLayout()
        
        # Fluent LineEdit
        self.txt_dir = LineEdit()
        self.txt_dir.setText(app_settings.get("download_dir", r"C:\Downloads"))
        self.txt_dir.setReadOnly(True)
        
        btn_browse_dir = PushButton(FIF.FOLDER, "Browse")
        btn_browse_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse_dir.setMinimumWidth(105)
        btn_browse_dir.clicked.connect(self.browse_folder)
        
        dir_layout.addWidget(self.txt_dir, 1) 
        dir_layout.addWidget(btn_browse_dir)
        main_layout.addLayout(dir_layout)

        main_layout.addWidget(QLabel("Anime / download profile", styleSheet="font-weight: bold; margin-top: 5px;"))
        
        # Profile dropdown + a "Create Profile" button shown only when there are none.
        site_row = QHBoxLayout()
        site_row.setSpacing(8)
        self.combo_site = ComboBox()
        self.combo_site.currentTextChanged.connect(self.on_site_select)
        site_row.addWidget(self.combo_site, 1)

        self.btn_goto_profiles = PushButton(FIF.ADD, "Create Profile")
        self.btn_goto_profiles.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_goto_profiles.setToolTip("Open Profile Manager to add a website profile")
        self.btn_goto_profiles.clicked.connect(self.goto_profiles_signal.emit)
        self.btn_goto_profiles.hide()
        site_row.addWidget(self.btn_goto_profiles)
        main_layout.addLayout(site_row)
        
        self.lbl_url = QLabel("No profile selected")
        self.lbl_url.setWordWrap(True)
        self.lbl_url.setStyleSheet("color: #888888; font-size: 12px;")
        main_layout.addWidget(self.lbl_url)

        main_layout.addWidget(QLabel("Episodes", styleSheet="font-weight: bold; margin-top: 5px;"))
        self.ep_picker = EpisodeRangePicker()
        self.ep_picker.changed.connect(self.save_settings)
        self.ep_picker.changed.connect(self._update_episode_feedback)
        main_layout.addWidget(self.ep_picker)

        # Leave out the episodes the site itself marks as filler.
        self._filler_thread = None
        self.chk_skip_filler = CheckBox("Skip filler episodes")
        self.chk_skip_filler.setToolTip(
            "Leave out episodes this site marks as filler. The list comes from the "
            "site, so it is only as complete as the site's own tagging.")
        self.chk_skip_filler.toggled.connect(self.on_skip_filler_toggled)
        main_layout.addWidget(self.chk_skip_filler)

        # Live total: friendly count + normalized form, red hint if a range is invalid.
        self.lbl_ep_feedback = QLabel("")
        self.lbl_ep_feedback.setWordWrap(True)
        self.lbl_ep_feedback.setStyleSheet("font-size: 12px; background: transparent;")
        main_layout.addWidget(self.lbl_ep_feedback)

        main_layout.addStretch()

        # Fluent Primary Button (Automatically uses accent color!)
        self.btn_start = PrimaryPushButton(FIF.DOWNLOAD, "Start Download")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self.start_task)
        main_layout.addWidget(self.btn_start)

        self.scroll.setWidget(self.content)
        outer_layout.addWidget(self.scroll)

        signals.update_buttons.connect(self.set_buttons)
        signals.paused_settings_changed.connect(self.on_paused_settings_changed)
        signals.profile_limits_changed.connect(self.on_profile_limits_changed)
        self.refresh_dropdown()
        self._update_episode_feedback()   # populate the live preview for the initial value

        # The session to offer is the one left over from the LAST run, captured now,
        # before anything in this run can start a download. Reading it a second
        # later offered to "resume" the very download a Discord command had just
        # started: that command runs 600 ms after launch and records itself as the
        # unfinished session, and the prompt read that record at 1000 ms.
        import copy
        with config_lock:
            self._session_at_launch = copy.deepcopy(app_settings.get("unfinished_session"))
        self._download_started_this_launch = False
        signals.task_started.connect(self._note_download_started)
        QTimer.singleShot(1000, self.check_and_prompt_resume)

    def _note_download_started(self):
        self._download_started_this_launch = True

    def check_and_prompt_resume(self):
        # A download already under way (typically one queued from Discord) replaces
        # the old session; offering to resume would start the same episodes twice.
        if self._download_started_this_launch:
            return
        session = self._session_at_launch
        if not session or not session.get("episodes"):
            return
        site = session.get("site")
        episodes = session.get("episodes", [])
        target_dir = session.get("target_dir")
        headless = session.get("headless", True)
        webhook = session.get("webhook", "")
        selected_sound = session.get("selected_sound", "")
        volume = session.get("volume", 100)
        concurrency = session.get("concurrency", 3)

        from qfluentwidgets import MessageBox
        title = "🔄 Resume Unfinished Session?"
        content = f"The application detected an unfinished download session for '{site}'.\n\nWould you like to resume downloading the remaining {len(episodes)} episodes?"
        w = MessageBox(title, content, self.window())
        w.yesButton.setText("Resume")
        w.cancelButton.setText("Discard")

        answered_yes = w.exec()
        # The dialog is modal but other code keeps running: a Discord command can
        # start a download while it is on screen. Then neither answer applies.
        if self._download_started_this_launch:
            return
        if answered_yes:
            # Restore UI values to match the resumed session
            if site in [self.combo_site.itemText(i) for i in range(self.combo_site.count())]:
                self.combo_site.setCurrentText(site)
            self.txt_dir.setText(target_dir)
            self._begin_download({
                "site": site, "episodes_list": episodes, "target_dir": target_dir,
                "headless": headless, "webhook": webhook,
                "selected_sound": selected_sound, "volume": volume,
                "concurrency": concurrency,
                "concurrency_auto": session.get("concurrency_auto", app_settings.get("concurrency_auto", True)),
            })
        else:
            with config_lock:
                app_settings.pop("unfinished_session", None)
                save_config()

    def save_settings(self):
        """Save this anime's episode selection independently of app defaults."""
        site = self.combo_site.currentText()
        with config_lock:
            if site in sites_data:
                sites_data[site]["last_episodes"] = self.ep_picker.spec()
        self._save_timer.start(400)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Download Folder", app_settings["download_dir"])
        if folder:
            folder = os.path.normpath(folder)
            self.txt_dir.setText(folder)
            with config_lock:
                app_settings["download_dir"] = folder
            save_config()

    def _download_defaults(self):
        """Snapshot the saved preferences when a new download is requested."""
        with config_lock:
            return {
                "headless": bool(app_settings.get("headless", True)),
                "webhook": app_settings.get("discord_webhook", "").strip(),
                "selected_sound": app_settings.get("selected_sound") or builtin_sound_path(),
                "volume": app_settings.get("volume", 100),
                "concurrency": app_settings.get("concurrency", 3),
                "concurrency_auto": bool(app_settings.get("concurrency_auto", True)),
            }

    def _profile_filler(self):
        """(cached filler numbers, whether they have ever been looked up) for the
        selected profile."""
        with config_lock:
            data = sites_data.get(self.combo_site.currentText(), {}) or {}
            return list(data.get("filler_episodes") or []), "filler_episodes" in data

    def selected_episodes(self):
        """The episodes a download would actually fetch: the picker's list, minus
        filler when that box is ticked. Returns (episodes, error, skipped count)."""
        from core.filler import strip_filler
        eps, err = self.ep_picker.episodes()
        if err or not self.chk_skip_filler.isChecked():
            return eps, err, 0
        filler, _ = self._profile_filler()
        kept = strip_filler(eps, filler)
        return kept, err, len(eps) - len(kept)

    def on_skip_filler_toggled(self, checked):
        with config_lock:
            site = self.combo_site.currentText()
            if site in sites_data:
                sites_data[site]["skip_filler"] = bool(checked)
        save_config()

        _, looked_up = self._profile_filler()
        if checked and not looked_up:
            self._start_filler_lookup()
        else:
            self._update_episode_feedback()

    def _start_filler_lookup(self):
        """Fetch this anime's filler list once, then keep it on the profile."""
        if self._filler_thread is not None and self._filler_thread.isRunning():
            return
        with config_lock:
            template = (sites_data.get(self.combo_site.currentText(), {}) or {}).get("url", "")
        if not template:
            return
        self.chk_skip_filler.setEnabled(False)
        self.lbl_ep_feedback.setText("⏳  Looking up which episodes are filler…")
        self.lbl_ep_feedback.setStyleSheet("color:#aaaaaa; font-size:12px; background:transparent;")
        self._filler_thread = FillerLookupThread(template, self.combo_site.currentText(), self)
        self._filler_thread.done.connect(self._on_filler_ready)
        self._filler_thread.start()

    def _on_filler_ready(self, episodes, error, source):
        self.chk_skip_filler.setEnabled(True)
        if error:
            # Nothing is cached on failure, so ticking the box again retries.
            self.chk_skip_filler.setChecked(False)
            InfoBar.warning(
                title="Couldn't read the filler list",
                content=f"{error}. The episode list was left unchanged.",
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self)
            self._update_episode_feedback()
            return
        with config_lock:
            site = self.combo_site.currentText()
            if site in sites_data:
                sites_data[site]["filler_episodes"] = list(episodes)
                sites_data[site]["filler_source"] = source
        save_config()
        if not episodes:
            InfoBar.info(
                title="No filler episodes marked",
                content="Neither site marks any episode of this anime as filler, "
                        "so nothing will be skipped.",
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self)
        else:
            InfoBar.success(
                title=f"{len(episodes)} filler episodes found",
                content=f"Taken from {source}." if source else "",
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=4000, parent=self)
        self._update_episode_feedback()

    def _update_episode_feedback(self):
        """Live total under the picker: friendly count + normalized spec when valid,
        or a red hint when a range is backwards. Also gates the Start button while idle."""
        eps, err, skipped = self.selected_episodes()
        if err:
            self._episodes_valid = False
            self.lbl_ep_feedback.setText(f"⚠  {err}")
            self.lbl_ep_feedback.setStyleSheet("color:#ff6b6b; font-size:12px; background:transparent;")
        else:
            self._episodes_valid = bool(eps)
            noun = "episode" if len(eps) == 1 else "episodes"
            extra = f"   ·   {skipped} filler skipped" if skipped else ""
            if not eps:
                self.lbl_ep_feedback.setText("⚠  Every episode in this range is filler — nothing to download.")
                self.lbl_ep_feedback.setStyleSheet("color:#ff6b6b; font-size:12px; background:transparent;")
                if self.ep_picker.isEnabled() and not self._checking:
                    self.btn_start.setEnabled(False)
                return
            limits = self.ep_picker.limits()
            known = (f"   ·   this anime has episodes {limits[0]}–{limits[1]} (newer ones show "
                     f"up after a Watchlist check or loading it from Search again)"
                     if limits else "")
            self.lbl_ep_feedback.setText(
                f"✓  {len(eps)} {noun}   →   {compact_episode_spec(eps)}{extra}{known}")
            self.lbl_ep_feedback.setStyleSheet("color:#51cf66; font-size:12px; background:transparent;")
        # Only touch the button while idle -- during a task the picker is disabled,
        # and during a connection check we must not re-enable Start under the checker.
        if self.ep_picker.isEnabled() and not self._checking:
            site = self.combo_site.currentText()
            valid_site = site not in ("No Profiles", "No profile selected", "")
            self.btn_start.setEnabled(self._episodes_valid and valid_site)

    def on_profile_limits_changed(self, name):
        """A Watchlist check raised this profile's last episode: if it is the one
        open here, let the boxes reach the new episodes now (not after a reselect)."""
        if self.combo_site.currentText() != name:
            return
        from ui.search_tab import episode_bounds
        with config_lock:
            limits = episode_bounds(sites_data.get(name, {}))
        if limits:
            self.ep_picker.set_limits(limits)
            self._update_episode_feedback()

    def on_paused_settings_changed(self, values):
        """Keep defaults, session recovery, and retry settings in sync."""
        updates = {key: values[source] for key, source in (
            ("headless", "headless"), ("concurrency", "limit"),
            ("concurrency_auto", "auto")) if values.get(source) is not None}
        with config_lock:
            app_settings.update(updates)
            session = app_settings.get("unfinished_session")
            if session:
                session.update(updates)
        if self.last_download_params:
            self.last_download_params.update(updates)
        self._save_timer.start(400)

    def set_inputs_enabled(self, enabled):
        self._inputs_locked = not enabled
        self.btn_start.setEnabled(enabled)
        self.ep_picker.setEnabled(enabled)
        if enabled:
            self._update_episode_feedback()

    def refresh_dropdown(self):
        self.combo_site.blockSignals(True)
        self.combo_site.clear()
        with config_lock:
            has_sites = len(sites_data) > 0
            keys = list(sites_data.keys())
        if not has_sites:
            self.combo_site.addItem("No Profiles")
            self.lbl_url.setText("No profile selected")
            self.set_inputs_enabled(False)
            self.btn_goto_profiles.show()
        else:
            self.set_inputs_enabled(True)
            self.btn_goto_profiles.hide()
            self.combo_site.addItems(keys)
            last = app_settings.get("last_profile", "")
            if last and last in keys:
                self.combo_site.setCurrentText(last)
            else:
                self.combo_site.setCurrentIndex(0)
            self.on_site_select(self.combo_site.currentText())
        self.combo_site.blockSignals(False)
    def on_site_select(self, text):
        with config_lock:
            if text in sites_data: 
                self.lbl_url.setText(unquote(sites_data[text].get("url", "")))
                app_settings["last_profile"] = text
                spec = sites_data[text].get("last_episodes")
                if not spec:
                    # Migrate old profiles that stored separate start/end fields.
                    s = sites_data[text].get("last_start")
                    e = sites_data[text].get("last_end")
                    if s and e:
                        spec = s if s == e else f"{s}-{e}"
                spec = spec or "1"
                if hasattr(self, 'ep_picker'):
                    from ui.search_tab import episode_bounds
                    self.ep_picker.blockSignals(True)
                    # Profiles made from Search know the site's first/last episode;
                    # the boxes can't go outside them. Older profiles stay open.
                    self.ep_picker.set_limits(episode_bounds(sites_data[text]))
                    self.ep_picker.set_spec(spec)
                    self.ep_picker.blockSignals(False)
                # Filler is per anime, so the setting and the cached list both
                # belong to the profile, not to the app.
                if hasattr(self, 'chk_skip_filler'):
                    self.chk_skip_filler.blockSignals(True)
                    self.chk_skip_filler.setChecked(bool(sites_data[text].get("skip_filler")))
                    self.chk_skip_filler.setEnabled(True)
                    self.chk_skip_filler.blockSignals(False)
                if hasattr(self, 'ep_picker'):
                    self._update_episode_feedback()
                save_config()
            else: 
                self.lbl_url.setText("No profile selected")

    def set_buttons(self, start_en, _close_en, _prof_en):
        self.set_inputs_enabled(start_en and self.combo_site.currentText() != "No Profiles")

    def retry_episodes(self, episodes):
        """Re-download just `episodes` from the most recent task.

        A Watchlist download runs against a transient profile that is discarded when
        the task ends, so in that case the task is rebuilt from the stored template
        rather than looked up by name.
        """
        episodes = sorted(set(episodes or []))
        if not episodes:
            return False
        params = self.last_download_params
        if not params:
            self._warn("Nothing to Retry", "The previous download's details are no longer available.")
            return False

        if params["site"] not in sites_data:
            meta = self.last_watch_meta
            if not meta:
                self._warn("Nothing to Retry",
                           "That download used a temporary profile that no longer exists.")
                return False
            title, template, domain = meta
            self.start_watch_download(title, template, domain, compact_episode_spec(episodes))
            return True

        retry = dict(params)
        retry["episodes_list"] = episodes
        self._begin_download(retry)
        return True

    def _warn(self, title, content):
        InfoBar.warning(title=title, content=content, orient=Qt.Orientation.Horizontal,
                        isClosable=True, position=InfoBarPosition.TOP, duration=4000, parent=self)

    def start_watch_download(self, title, template, domain, episodes_str):
        """Download the given episodes for a watched anime WITHOUT creating a saved
        profile. Reuses an existing same-name profile if one exists, otherwise runs
        against a transient in-memory profile that is discarded when the task ends."""
        if self._checking:
            return
        ranges = spec_to_ranges(episodes_str)
        episodes_list = sorted({e for a, b in ranges for e in range(a, b + 1)})
        if not episodes_list:
            self._warn("Nothing to Download", "No new episodes to download.")
            return

        target_dir = self.txt_dir.text().strip() or app_settings.get("download_dir", "")
        if not target_dir or not os.path.exists(target_dir):
            self._warn("Invalid Path", "Set a valid download folder in the Downloader tab first.")
            return

        # Folder / lookup key = the anime name (so videos land in a sensible folder).
        site_key = safe_folder_name(title) or "Anime"
        # Kept so a retry can rebuild the transient profile after it is cleaned up.
        self.last_watch_meta = (title, template, domain)

        # Watchlist downloads always run the built-in flow on supported sites, so an
        # existing profile with the same name and hand-edited steps can't change how
        # the episode is fetched. The flow is pinned for this run only and passed to
        # the engine directly: sites_data is left alone because that profile is the
        # user's, and save_config() (which _begin_download calls) drops _transient
        # entries -- shadowing a real profile under its own key would delete it.
        from ui.search_tab import resolve_site_flow, DEFAULT_SITE_FLOWS
        from core.site_health import lookup as site_lookup
        step_paths, next_btn = resolve_site_flow(domain)
        # Match by site name, not the exact host: the flow table is keyed "witanime.life"
        # while entries now say "witanime.site", and a remote command can pass a bare
        # "witanime". An exact `in` check missed all of those and left the built-in flow
        # unpinned even though resolve_site_flow (which also matches by name) returned it.
        pinned_flow = step_paths if site_lookup(DEFAULT_SITE_FLOWS, domain) else None

        transient = site_key not in sites_data
        if transient:
            if not any(isinstance(v, list) and v for v in step_paths.values()):
                self._warn("No Download Steps",
                           f"No automation steps configured for {domain}. Set up one "
                           f"profile for this site in Profile Manager first.")
                return
            with config_lock:
                sites_data[site_key] = {
                    "url": template,
                    "next_btn_xpath": next_btn,
                    "step_paths": step_paths,
                    "last_episodes": episodes_str,
                    "_transient": True,   # in-memory only; never saved, removed on finish
                }

            def _cleanup(*_a):
                with config_lock:
                    if sites_data.get(site_key, {}).get("_transient"):
                        sites_data.pop(site_key, None)
                for sig in (signals.task_finished, signals.task_cancelled):
                    try:
                        sig.disconnect(_cleanup)
                    except Exception:
                        pass
            signals.task_finished.connect(_cleanup)
            signals.task_cancelled.connect(_cleanup)
        elif pinned_flow is None:
            # Only an unsupported site falls back to the existing profile's steps, so
            # that profile is the one that has to be configured.
            with config_lock:
                sp = sites_data[site_key].get("step_paths", {}) or {}
            if not any(isinstance(v, list) and v for v in sp.values()):
                self._warn("No Download Steps",
                           f"The profile '{site_key}' has no automation steps. Configure it first.")
                return

        self._begin_download({
            "site": site_key,
            "episodes_list": episodes_list,
            "target_dir": target_dir,
            **self._download_defaults(),
            "step_paths_override": pinned_flow,
        })

    def start_task(self):
        # Guard re-entry: a connection check already in flight (rapid clicks, or a
        # History/Watchlist re-download firing while one runs) must not spawn a second
        # ConnectionCheckThread and orphan the first (which would crash on GC).
        if self._checking:
            return
        site = self.combo_site.currentText()
        if not site or site in ["No Profiles", "No profile selected"]:
            InfoBar.warning(
                title="Profile Required",
                content="Please select a valid site profile from the dropdown before starting.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return

        with config_lock:
            profile_data = sites_data.get(site, {})
            step_paths = profile_data.get("step_paths", {})
            base_url = profile_data.get("url", "").strip()

        if not base_url:
            InfoBar.warning(
                title="URL Required",
                content="The selected profile has no Base URL. Please configure it in the Profile Manager first.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return

        # Read the episode ranges up front so we can both validate them and use the
        # first episode for the reachability check below.
        # Filtered, so "Skip filler episodes" applies to the download itself and not
        # only to the preview under the picker.
        episodes_list, ep_err, _skipped = self.selected_episodes()
        if not ep_err and not episodes_list:
            InfoBar.warning(
                title="Nothing to download",
                content="Every episode in this range is marked as filler. Untick "
                        "\"Skip filler episodes\" or pick a different range.",
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self)
            return
        if ep_err:
            InfoBar.warning(
                title="Invalid Episodes",
                content=ep_err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return

        # Fail fast on instant, local problems BEFORE the (blocking) network check,
        # so a misconfigured profile or a bad folder reports immediately instead of
        # freezing the UI for the connection timeout.
        total_steps = 0
        for _, steps in step_paths.items():
            if isinstance(steps, list):
                for step in steps:
                    if step.get("xpath", "").strip():
                        total_steps += 1
        if total_steps == 0:
            InfoBar.warning(
                title="No Automation Steps",
                content="The selected profile has no automation steps in any path. Please add steps in the Profile Manager first.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return

        target_dir = self.txt_dir.text().strip()
        if not target_dir or not os.path.exists(target_dir):
            InfoBar.warning(
                title="Invalid Path",
                content="Please specify a valid and existing download folder path.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return

        url_to_check = base_url.replace("{x}", str(episodes_list[0]))
        if not url_to_check.startswith(("http://", "https://")):
            url_to_check = "https://" + url_to_check

        import urllib.parse
        # Proper URL encoding of non-ascii characters in URL components (e.g. Arabic)
        try:
            url_parsed = urllib.parse.urlsplit(url_to_check)
            url_to_check = urllib.parse.urlunsplit((
                url_parsed.scheme,
                url_parsed.netloc,
                urllib.parse.quote(url_parsed.path),
                urllib.parse.quote(url_parsed.query),
                url_parsed.fragment
            ))
        except Exception:
            pass

        # Everything else is validated; the only remaining step is a network
        # reachability check, which we run off the UI thread so the Start click
        # never freezes the window while the connection times out.
        params = {
            "site": site,
            "episodes_list": episodes_list,
            "target_dir": target_dir,
            **self._download_defaults(),
        }

        self._checking = True
        self.btn_start.setEnabled(False)
        self.btn_start.setText("Checking connection…")
        self._conn_thread = ConnectionCheckThread(url_to_check)
        self._conn_thread.result.connect(
            lambda ok, err: self._on_connection_checked(ok, err, params))
        self._conn_thread.start()

    def _on_connection_checked(self, ok, err, params):
        self._checking = False
        self.btn_start.setText("Start Download")
        self.btn_start.setEnabled(True)
        if not ok:
            # Pressing Start again while the previous failure is still on screen used
            # to stack a second identical bar -- easy to do, since a failed check looks
            # like nothing happened. Replace the old one rather than pile up copies.
            try:
                if self._conn_infobar is not None:
                    self._conn_infobar.close()
            except Exception:
                pass      # already dismissed or destroyed by Qt
            self._conn_infobar = InfoBar.warning(
                title="Connection Failed",
                content=f"Could not connect to the profile Base URL. Please check your internet connection or the URL itself.\nError: {err}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self
            )
            return
        if not self._confirm_disk_space(params):
            return
        self._begin_download(params)

    def _confirm_disk_space(self, p):
        """Before a Start: warn when the run likely won't fit on the drive.
        True to go ahead. (Watchlist and Discord downloads aren't asked -- nobody
        may be at the PC to answer.)"""
        import tempfile
        from qfluentwidgets import MessageBox
        from utils.disk import check_space, human
        from core.concurrency import ConcurrencyController
        safe = safe_folder_name(p["site"])
        # Auto concurrency may climb to its maximum, so plan for that many at once.
        parallel = (ConcurrencyController.MAX_LIMIT if p.get("concurrency_auto", app_settings.get("concurrency_auto", True))
                    else int(p.get("concurrency") or 1))
        per_ep, short = check_space(p["target_dir"], os.path.join(p["target_dir"], safe),
                                    len(p["episodes_list"]), temp_dir=tempfile.gettempdir(),
                                    parallel=parallel)
        if not short:
            return True
        n = len(p["episodes_list"])
        lines = []
        for drive, needed, free in short:
            role = ("the download folder's drive"
                    if drive == os.path.splitdrive(os.path.abspath(p["target_dir"]))[0].upper()
                    else "the temporary folder's drive (episodes download there first)")
            lines.append(f"{drive} — {role}: needs about {human(needed)}, "
                         f"only {human(free)} free.")
        box = MessageBox(
            "Not enough disk space",
            f"{n} episode{'s' if n != 1 else ''} at ~{human(per_ep)} each:\n"
            + "\n".join(lines)
            + "\n\nFree up space, pick fewer episodes, or choose another download folder.",
            self.window())
        box.yesButton.setText("Start anyway")
        box.cancelButton.setText("Cancel")
        return bool(box.exec())

    def _begin_download(self, p):
        # Remember where these episodes land, and which ones they are, so
        # "Start Watching" opens the first episode of THIS session rather than
        # whatever happens to sort first in a folder full of older downloads.
        safe = safe_folder_name(p["site"])
        self.last_download_folder = os.path.join(p["target_dir"], safe)
        self.last_download_episodes = list(p["episodes_list"])
        self.last_download_params = dict(p)   # lets us re-run just the failed episodes
        with config_lock:
            app_settings["unfinished_session"] = {
                "site": p["site"],
                "episodes": list(p["episodes_list"]),
                "target_dir": p["target_dir"],
                "headless": p["headless"],
                "webhook": p["webhook"],
                "selected_sound": p["selected_sound"],
                "volume": p["volume"],
                "concurrency": p["concurrency"],
                "concurrency_auto": p.get("concurrency_auto", app_settings.get("concurrency_auto", True)),
            }
            save_config()

        signals.update_buttons.emit(False, True, False)
        signals.task_started.emit()
        threading.Thread(
            target=run_selenium_task,
            args=(p["site"], p["episodes_list"], p["target_dir"], p["headless"],
                  p["webhook"], p["selected_sound"], p["volume"], p["concurrency"],
                  p.get("step_paths_override")),
            kwargs={"concurrency_auto": p.get("concurrency_auto")},
            daemon=True
        ).start()
