import os
import threading
import sys
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog)

# THE UPGRADE: We are using Fluent Widgets for everything!
from qfluentwidgets import (PushButton, PrimaryPushButton, LineEdit, CheckBox,
                            ComboBox, Slider, SmoothScrollArea, SpinBox, FluentIcon as FIF, ToolButton,
                            InfoBar, InfoBarPosition)

from utils.config import app_settings, sites_data, save_config, config_lock
from core.signals import signals
from core.selenium_engine import run_selenium_task, launch_visible_browser
from ui.styles import apply_danger_style


# Sentinel stored in selected_sound to mean "no finish sound". Distinct from ""
# (unset), which falls back to the built-in default.
SOUND_NONE = "__none__"


def builtin_sound_path():
    """Absolute path to the app's bundled default finish sound (frozen or source)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "assets", "finishingDownloadSound.wav")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "finishingDownloadSound.wav")


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
            sp.setRange(0, 99999)
            sp.setValue(val)
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
        ranges = spec_to_ranges(text) or [(1, 1)]
        for e in list(self._rows):
            e["widget"].setParent(None)
            e["widget"].deleteLater()
        self._rows.clear()
        for a, b in ranges:
            self._add_row(a, b)
        self._update_remove_buttons()


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
            req = urllib.request.Request(
                self.url,
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
                try:
                    host = urllib.parse.urlsplit(self.url).netloc.split(':')[0]
                    if host:
                        socket.gethostbyname(host)
                        ok = True
                except Exception as dns_err:
                    err = f"DNS resolution failed: {dns_err}"
        except Exception as e:
            # Any unexpected failure must still report back, or Start stays disabled.
            err = str(e)
        self.result.emit(ok, err)


class DownloaderWidget(QWidget):
    goto_profiles_signal = pyqtSignal()   # asks the main window to open Profile Manager

    def __init__(self, parent=None):
        super().__init__(parent)

        self._episodes_valid = True
        self._checking = False   # True while the async connection check is in flight

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

        # Fluent Button
        self.btn_profile = PushButton(FIF.GLOBE, "Open Browser (Extensions / Login)")
        self.btn_profile.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_profile.clicked.connect(lambda: threading.Thread(target=launch_visible_browser, daemon=True).start())
        main_layout.addWidget(self.btn_profile)

        main_layout.addWidget(QLabel("Download Location:", styleSheet="font-weight: bold; margin-top: 10px;"))
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

        main_layout.addWidget(QLabel("Active Website Profile:", styleSheet="font-weight: bold; margin-top: 5px;"))
        
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
        self.lbl_url.setStyleSheet("color: #888888; font-size: 12px;")
        main_layout.addWidget(self.lbl_url)

        # Fluent CheckBox
        self.chk_headless = CheckBox("Run Invisibly (Headless)")
        self.chk_headless.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_headless.setChecked(app_settings.get("headless", True))
        self.chk_headless.toggled.connect(self.save_settings)
        main_layout.addWidget(self.chk_headless)


        main_layout.addWidget(QLabel("Concurrent Downloads (Max Active Episode Downloading):", styleSheet="font-weight: bold; margin-top: 5px; background: transparent;"))
        self.spin_concurrency = SpinBox()
        self.spin_concurrency.setRange(1, 6) # Allow 1 to 6 simultaneous downloads
        self.spin_concurrency.setValue(app_settings.get("concurrency", 3)) # Default to 3
        self.spin_concurrency.valueChanged.connect(self.save_settings)
        self.spin_concurrency.setFocusPolicy(Qt.FocusPolicy.StrongFocus) # Click and Tab focus only, no Wheel focus!
        self.spin_concurrency.wheelEvent = lambda e: e.ignore() # Pass scroll events to parent
        main_layout.addWidget(self.spin_concurrency)

        main_layout.addWidget(QLabel("Notification Sound:", styleSheet="font-weight: bold; margin-top: 5px;"))
        sound_layout = QHBoxLayout()
        
        self.combo_sound = ComboBox()
        self.combo_sound.currentTextChanged.connect(self.on_sound_change)
        self.combo_sound.setFixedHeight(40)
        sound_layout.addWidget(self.combo_sound, 1) 
        
        self.btn_play_sound = PushButton(FIF.PLAY, "Play")
        self.btn_play_sound.setFixedHeight(40)
        self.btn_play_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play_sound.clicked.connect(self.preview_sound)
        sound_layout.addWidget(self.btn_play_sound)
        
        self.btn_add_sound = PushButton(FIF.ADD, "Add Sound")
        self.btn_add_sound.setFixedHeight(40)
        self.btn_add_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_sound.clicked.connect(self.browse_custom_sound)
        sound_layout.addWidget(self.btn_add_sound)
        
        self.btn_delete_sound = ToolButton(FIF.DELETE, self)
        self.btn_delete_sound.setObjectName("Danger")
        apply_danger_style(self.btn_delete_sound)
        self.btn_delete_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_sound.setFixedSize(40, 40)
        self.btn_delete_sound.clicked.connect(self.delete_custom_sound)
        sound_layout.addWidget(self.btn_delete_sound)
        
        main_layout.addLayout(sound_layout)
        
        self.volume_container = QWidget()
        vol_layout = QHBoxLayout(self.volume_container)
        vol_layout.setContentsMargins(0, 0, 0, 0)
        
        self.unmute_volume = app_settings.get("volume", 100)
        
        self.btn_mute = ToolButton(FIF.VOLUME, self)
        self.btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mute.setFixedSize(40, 32)
        self.btn_mute.clicked.connect(self.toggle_mute)
        vol_layout.addWidget(self.btn_mute)
        
        # Fluent Slider
        self.slider_vol = Slider(Qt.Orientation.Horizontal)
        self.slider_vol.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(app_settings.get("volume", 100))
        self.slider_vol.valueChanged.connect(self.on_volume_change)
        vol_layout.addWidget(self.slider_vol, 1)
        
        self.txt_vol = LineEdit()
        self.txt_vol.setText(str(self.slider_vol.value()))
        self.txt_vol.setValidator(QIntValidator(0, 100))
        self.txt_vol.setFixedWidth(50) 
        self.txt_vol.textEdited.connect(self.on_volume_typed)
        vol_layout.addWidget(self.txt_vol)
        
        main_layout.addWidget(self.volume_container)

        self.refresh_sound_dropdown()
        self.on_volume_change(self.slider_vol.value())

        main_layout.addWidget(QLabel("Discord Webhook:", styleSheet="font-weight: bold; margin-top: 5px;"))
        self.txt_webhook = LineEdit()
        self.txt_webhook.setText(app_settings.get("discord_webhook", ""))
        self.txt_webhook.setPlaceholderText("https://discord.com/api/webhooks/...")
        self.txt_webhook.textChanged.connect(self.save_settings)
        main_layout.addWidget(self.txt_webhook)

        main_layout.addWidget(QLabel("Episodes", styleSheet="font-weight: bold; margin-top: 5px;"))
        self.ep_picker = EpisodeRangePicker()
        self.ep_picker.changed.connect(self.save_settings)
        self.ep_picker.changed.connect(self._update_episode_feedback)
        main_layout.addWidget(self.ep_picker)

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
        self.refresh_dropdown()
        self._update_episode_feedback()   # populate the live preview for the initial value
        QTimer.singleShot(1000, self.check_and_prompt_resume)

    def check_and_prompt_resume(self):
        with config_lock:
            session = app_settings.get("unfinished_session")
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
        
        if w.exec():
            # Restore UI values to match the resumed session
            if site in [self.combo_site.itemText(i) for i in range(self.combo_site.count())]:
                self.combo_site.setCurrentText(site)
            self.txt_dir.setText(target_dir)
            self.chk_headless.setChecked(headless)
            self.txt_webhook.setText(webhook)
            self.spin_concurrency.setValue(concurrency)
            self.slider_vol.setValue(volume)
            
            signals.update_buttons.emit(False, True, False)
            signals.task_started.emit()
            threading.Thread(
                target=run_selenium_task, 
                args=(site, episodes, target_dir, headless, webhook, selected_sound, volume, concurrency), 
                daemon=True
            ).start()
        else:
            with config_lock:
                app_settings.pop("unfinished_session", None)
                save_config()

    # KEEP ALL YOUR EXISTING FUNCTIONS BELOW HERE EXACTLY THE SAME!
    # (refresh_sound_dropdown, browse_folder, save_settings, start_task, etc.)
    # I have omitted them here to save space, but DO NOT delete your functions!

    def on_sound_change(self, text):
        data = self.combo_sound.currentData()   # file path, or SOUND_NONE, for None
        app_settings["selected_sound"] = data if data else ""
        save_config()
        self._update_sound_controls()

    def browse_custom_sound(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Audio File", "", "Audio Files (*.mp3 *.wav *.ogg)")
        if file_path:
            file_path = os.path.normpath(file_path)
            sounds = app_settings.get("custom_sounds", [])
            if file_path not in sounds:
                sounds.append(file_path)
                app_settings["custom_sounds"] = sounds
                app_settings["selected_sound"] = file_path
                save_config()
                self.refresh_sound_dropdown()

    def delete_custom_sound(self):
        selected = app_settings.get("selected_sound", "")
        sounds = app_settings.get("custom_sounds", [])
        if selected not in sounds:
            # The built-in default can't be deleted.
            InfoBar.info("Built-in Sound", "The default sound can't be removed.",
                         orient=Qt.Orientation.Horizontal, isClosable=True,
                         position=InfoBarPosition.TOP, duration=3000, parent=self)
            return
        sounds.remove(selected)
        app_settings["custom_sounds"] = sounds
        app_settings["selected_sound"] = sounds[0] if sounds else builtin_sound_path()
        save_config()
        self.refresh_sound_dropdown()

    def preview_sound(self):
        selected = app_settings.get("selected_sound", "")
        if selected and os.path.exists(selected):
            vol = app_settings.get("volume", 100)
            def play_preview():
                try:
                    import ctypes
                    mci_vol = int(vol * 10)
                    mci_path = selected.replace("\\", "/")
                    ctypes.windll.winmm.mciSendStringW('close custom_audio', None, 0, None)
                    # Always open via mpegvideo: the waveaudio device rejects
                    # "setaudio volume" (so the slider had no effect on .wav files).
                    ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" type mpegvideo alias custom_audio', None, 0, None)
                    ctypes.windll.winmm.mciSendStringW(f'setaudio custom_audio volume to {mci_vol}', None, 0, None)
                    ctypes.windll.winmm.mciSendStringW('play custom_audio', None, 0, None)
                except Exception:
                    pass
            import threading
            threading.Thread(target=play_preview, daemon=True).start()
    def toggle_mute(self):
        if self.slider_vol.value() > 0:
            self.unmute_volume = self.slider_vol.value()
            self.slider_vol.setValue(0)
        else:
            target = self.unmute_volume if hasattr(self, 'unmute_volume') and self.unmute_volume > 0 else 100
            self.slider_vol.setValue(target)
    def on_volume_typed(self, text):
        if text:
            try:
                val = int(text)
                self.slider_vol.setValue(val)
            except ValueError: pass
    def on_volume_change(self, value):
        if self.txt_vol.text() != str(value):
            self.txt_vol.setText(str(value))
        if value == 0: self.btn_mute.setIcon(FIF.MUTE)
        else: self.btn_mute.setIcon(FIF.VOLUME)
        self.save_settings()
    def save_settings(self):
        app_settings["headless"] = self.chk_headless.isChecked()

        if hasattr(self, 'txt_webhook'):
            app_settings["discord_webhook"] = self.txt_webhook.text().strip()

        if hasattr(self, 'slider_vol'):
            app_settings["volume"] = self.slider_vol.value()

        site = self.combo_site.currentText()
        app_settings["concurrency"] = self.spin_concurrency.value()

        with config_lock:
            if site and site != "No Profiles" and site in sites_data:
                if hasattr(self, 'ep_picker'):
                    sites_data[site]["last_episodes"] = self.ep_picker.spec()
        # Debounce the disk write: rapid edits (e.g. holding a spinbox arrow) update
        # memory instantly but coalesce into a single save ~400ms after the last change.
        self._save_timer.start(400)
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", app_settings["download_dir"])
        if folder:
            self.txt_dir.setText(os.path.normpath(folder))
            app_settings["download_dir"] = os.path.normpath(folder)
            save_config()
    def refresh_sound_dropdown(self):
        self.combo_sound.blockSignals(True)
        self.combo_sound.clear()
        builtin = builtin_sound_path()
        self.combo_sound.addItem("🔕 None", userData=SOUND_NONE)
        self.combo_sound.addItem("🔔 Default (Built-in)", userData=builtin)
        sounds = app_settings.get("custom_sounds", [])
        for s in sounds:
            self.combo_sound.addItem(os.path.basename(s), userData=s)

        all_paths = [SOUND_NONE, builtin] + sounds
        selected = app_settings.get("selected_sound", "")
        if selected in all_paths:
            self.combo_sound.setCurrentIndex(all_paths.index(selected))
        else:
            self.combo_sound.setCurrentIndex(1)   # unset -> default to built-in
            app_settings["selected_sound"] = builtin

        self._update_sound_controls()
        self.combo_sound.blockSignals(False)

    def _update_sound_controls(self):
        # Hide preview/volume when "None" is selected -- there is nothing to play.
        selected = app_settings.get("selected_sound", "")
        is_none = selected == SOUND_NONE
        if hasattr(self, 'btn_play_sound'): self.btn_play_sound.setVisible(not is_none)
        if hasattr(self, 'volume_container'): self.volume_container.setVisible(not is_none)
        # Delete only applies to user-added sounds. Disable it for None and the
        # built-in default so it can't be spam-clicked (which stacked InfoBars).
        if hasattr(self, 'btn_delete_sound'):
            deletable = selected in app_settings.get("custom_sounds", [])
            self.btn_delete_sound.setVisible(not is_none)
            self.btn_delete_sound.setEnabled(deletable)
    def _update_episode_feedback(self):
        """Live total under the picker: friendly count + normalized spec when valid,
        or a red hint when a range is backwards. Also gates the Start button while idle."""
        eps, err = self.ep_picker.episodes()
        if err:
            self._episodes_valid = False
            self.lbl_ep_feedback.setText(f"⚠  {err}")
            self.lbl_ep_feedback.setStyleSheet("color:#ff6b6b; font-size:12px; background:transparent;")
        else:
            self._episodes_valid = True
            noun = "episode" if len(eps) == 1 else "episodes"
            self.lbl_ep_feedback.setText(f"✓  {len(eps)} {noun}   →   {compact_episode_spec(eps)}")
            self.lbl_ep_feedback.setStyleSheet("color:#51cf66; font-size:12px; background:transparent;")
        # Only touch the button while idle -- during a task the picker is disabled,
        # and during a connection check we must not re-enable Start under the checker.
        if self.ep_picker.isEnabled() and not self._checking:
            site = self.combo_site.currentText()
            valid_site = site not in ("No Profiles", "No profile selected", "")
            self.btn_start.setEnabled(self._episodes_valid and valid_site)

    def set_inputs_enabled(self, enabled):
        self.btn_start.setEnabled(enabled)
        self.ep_picker.setEnabled(enabled)
        self.spin_concurrency.setEnabled(enabled)
        self.txt_webhook.setEnabled(enabled)
        self.chk_headless.setEnabled(enabled)
        self.btn_profile.setEnabled(enabled)
        if enabled:
            self._update_episode_feedback()   # re-apply the invalid-spec Start gate

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
                    self.ep_picker.blockSignals(True)
                    self.ep_picker.set_spec(spec)
                    self.ep_picker.blockSignals(False)
                    self._update_episode_feedback()
                save_config()
            else: 
                self.lbl_url.setText("No profile selected")
    def set_buttons(self, start_en, _close_en, prof_en):
        if self.combo_site.currentText() == "No Profiles":
            self.btn_start.setEnabled(False)
            self.btn_profile.setEnabled(False)
        else:
            self.btn_start.setEnabled(start_en)
            self.btn_profile.setEnabled(prof_en)

    def start_redownload(self, profile, episodes_str):
        """Re-run a past download from the History tab: select the profile, set the
        episode spec, and start."""
        names = [self.combo_site.itemText(i) for i in range(self.combo_site.count())]
        if profile not in names:
            InfoBar.warning(title="Profile Missing",
                            content=f"Profile '{profile}' no longer exists. Recreate it first.",
                            orient=Qt.Orientation.Horizontal, isClosable=True,
                            position=InfoBarPosition.TOP, duration=4000, parent=self)
            return
        self.combo_site.setCurrentText(profile)
        # The stored episodes string ("1-12", "5", or "1-5, 8-12") seeds the picker.
        self.ep_picker.set_spec((episodes_str or "").strip())
        self._update_episode_feedback()
        self.start_task()

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
        site_key = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or "Anime"

        transient = site_key not in sites_data
        if transient:
            from ui.search_tab import resolve_site_flow
            step_paths, next_btn = resolve_site_flow(domain)
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
        else:
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
            "headless": self.chk_headless.isChecked(),
            "webhook": self.txt_webhook.text().strip(),
            "selected_sound": app_settings.get("selected_sound", ""),
            "volume": app_settings.get("volume", 100),
            "concurrency": app_settings.get("concurrency", 3),
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
        episodes_list, ep_err = self.ep_picker.episodes()
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
            "headless": self.chk_headless.isChecked(),
            "webhook": self.txt_webhook.text().strip(),
            "selected_sound": app_settings.get("selected_sound", ""),
            "volume": app_settings.get("volume", 100),
            "concurrency": app_settings.get("concurrency", 3),
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
            InfoBar.warning(
                title="Connection Failed",
                content=f"Could not connect to the profile Base URL. Please check your internet connection or the URL itself.\nError: {err}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self
            )
            return
        self._begin_download(params)

    def _begin_download(self, p):
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
            }
            save_config()

        signals.update_buttons.emit(False, True, False)
        signals.task_started.emit()
        threading.Thread(
            target=run_selenium_task,
            args=(p["site"], p["episodes_list"], p["target_dir"], p["headless"],
                  p["webhook"], p["selected_sound"], p["volume"], p["concurrency"]),
            daemon=True
        ).start()