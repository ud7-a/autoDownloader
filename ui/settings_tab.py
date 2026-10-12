"""Persistent app preferences, separate from an individual download."""

import os
import threading
from urllib.parse import urlsplit

from PyQt6.QtCore import Qt, QTimer, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFileDialog
from qfluentwidgets import (
    CardWidget, SubtitleLabel, BodyLabel, CaptionLabel, PushButton,
    PrimaryPushButton, ToolButton, CheckBox, ComboBox, LineEdit, Slider,
    SpinBox, SmoothScrollArea, FluentIcon as FIF, InfoBar, InfoBarPosition,
)

from core.signals import signals
from utils.config import (
    app_settings, config_lock, save_config, get_watchlist, DEFAULT_CLOUD_SERVICE_URL,
    cloud_register_and_sync, cloud_unsubscribe, set_windows_autostart,
    start_background_watcher,
)
from utils.sounds import SOUND_NONE, builtin_sound_path
from ui.secret_field import SecretLineEdit
from ui.styles import apply_danger_style


class SettingsWidget(QWidget):
    cloud_changed = pyqtSignal()
    _cloud_finished = pyqtSignal(bool, bool, str)  # enabling, success, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = True
        self._cloud_busy = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(save_config)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(30, 20, 30, 24)
        layout.setSpacing(18)
        title = SubtitleLabel("Settings")
        title.setStyleSheet("font-size: 22px; font-weight: bold; background: transparent;")
        layout.addWidget(title)
        intro = BodyLabel("Download and sound preferences save automatically. Defaults apply "
                          "when you start the next download.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        defaults = self._section(layout, "Download defaults")
        self.chk_headless = CheckBox("Keep the download browser hidden")
        self.chk_headless.setToolTip("Hide the automation browser. You can change browser "
                                    "visibility for a running download while it is paused.")
        self.chk_headless.toggled.connect(self.save_settings)
        defaults.addWidget(self.chk_headless)
        self.chk_auto_concurrency = CheckBox("Adjust simultaneous downloads automatically")
        self.chk_auto_concurrency.toggled.connect(self.on_auto_concurrency_toggled)
        defaults.addWidget(self.chk_auto_concurrency)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("Simultaneous downloads"))
        self.spin_concurrency = SpinBox()
        self.spin_concurrency.setRange(1, 6)
        self.spin_concurrency.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_concurrency.wheelEvent = lambda event: event.ignore()
        self.spin_concurrency.valueChanged.connect(self.save_settings)
        row.addWidget(self.spin_concurrency)
        row.addStretch()
        defaults.addLayout(row)
        self.lbl_concurrency_hint = CaptionLabel("")
        self.lbl_concurrency_hint.setWordWrap(True)
        defaults.addWidget(self.lbl_concurrency_hint)
        self.btn_profile = PushButton(FIF.GLOBE, "Open browser for sign-in or extensions")
        self.btn_profile.clicked.connect(self.open_browser)
        defaults.addWidget(self.btn_profile, alignment=Qt.AlignmentFlag.AlignLeft)

        notifications = self._section(layout, "Notifications and sound")
        self.chk_win_notify = CheckBox("Show Windows notifications")
        self.chk_win_notify.setToolTip("Notify when downloads finish or new episodes are "
                                      "found while you are using another window.")
        self.chk_win_notify.toggled.connect(self.save_settings)
        notifications.addWidget(self.chk_win_notify)
        notifications.addWidget(BodyLabel("Notification sound"))
        sound_row = QHBoxLayout()
        self.combo_sound = ComboBox()
        self.combo_sound.currentTextChanged.connect(self.on_sound_change)
        sound_row.addWidget(self.combo_sound, 1)
        self.btn_play_sound = PushButton(FIF.PLAY, "Preview")
        self.btn_play_sound.clicked.connect(self.preview_sound)
        sound_row.addWidget(self.btn_play_sound)
        self.btn_add_sound = PushButton(FIF.ADD, "Add")
        self.btn_add_sound.clicked.connect(self.browse_custom_sound)
        sound_row.addWidget(self.btn_add_sound)
        self.btn_delete_sound = ToolButton(FIF.DELETE)
        self.btn_delete_sound.setToolTip("Remove this custom sound")
        self.btn_delete_sound.clicked.connect(self.delete_custom_sound)
        apply_danger_style(self.btn_delete_sound)
        sound_row.addWidget(self.btn_delete_sound)
        notifications.addLayout(sound_row)
        self.volume_container = QWidget()
        volume = QHBoxLayout(self.volume_container)
        volume.setContentsMargins(0, 0, 0, 0)
        volume.addWidget(BodyLabel("Volume"))
        self.btn_mute = ToolButton(FIF.VOLUME)
        self.btn_mute.setToolTip("Mute or restore notification volume")
        self.btn_mute.clicked.connect(self.toggle_mute)
        volume.addWidget(self.btn_mute)
        self.slider_vol = Slider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.valueChanged.connect(self.on_volume_change)
        volume.addWidget(self.slider_vol, 1)
        self.txt_vol = LineEdit()
        self.txt_vol.setValidator(QIntValidator(0, 100))
        self.txt_vol.setFixedWidth(54)
        self.txt_vol.textEdited.connect(self.on_volume_typed)
        volume.addWidget(self.txt_vol)
        notifications.addWidget(self.volume_container)

        discord = self._section(layout, "Discord")
        description = BodyLabel("Use a webhook for download and episode alerts. Enable "
                                "cloud notifications to receive release alerts while your PC is off.")
        description.setWordWrap(True)
        discord.addWidget(description)
        discord.addWidget(BodyLabel("Webhook URL"))
        self.txt_webhook = SecretLineEdit()
        self.txt_webhook.setPlaceholderText("https://discord.com/api/webhooks/...")
        discord.addWidget(self.txt_webhook)
        hint = CaptionLabel("In Discord: Channel settings → Integrations → Webhooks → Copy webhook URL.")
        hint.setWordWrap(True)
        discord.addWidget(hint)
        self.chk_cloud = CheckBox("Receive episode alerts through the cloud service")
        self.chk_cloud.toggled.connect(self._on_cloud_toggle)
        discord.addWidget(self.chk_cloud)
        discord.addWidget(BodyLabel("Cloud service URL"))
        self.txt_cloud_url = LineEdit()
        self.txt_cloud_url.setPlaceholderText(DEFAULT_CLOUD_SERVICE_URL)
        discord.addWidget(self.txt_cloud_url)
        self.btn_save_discord = PrimaryPushButton(FIF.SAVE, "Save Discord settings")
        self.btn_save_discord.clicked.connect(self.save_discord)
        discord.addWidget(self.btn_save_discord, alignment=Qt.AlignmentFlag.AlignLeft)
        self.lbl_cloud_status = CaptionLabel("")
        self.lbl_cloud_status.setWordWrap(True)
        discord.addWidget(self.lbl_cloud_status)
        layout.addStretch()
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll)

        self._cloud_finished.connect(self._on_cloud_finished, Qt.ConnectionType.QueuedConnection)
        signals.paused_settings_changed.connect(self.load_defaults)
        signals.update_buttons.connect(self._on_task_buttons)
        self._loading = False
        self.load_defaults()

    @staticmethod
    def _section(layout, title):
        card = CardWidget()
        section = QVBoxLayout(card)
        section.setContentsMargins(20, 16, 20, 18)
        section.setSpacing(12)
        section.addWidget(SubtitleLabel(title))
        layout.addWidget(card)
        return section

    def showEvent(self, event):
        self.load_defaults()
        super().showEvent(event)

    def load_defaults(self, _values=None):
        self._loading = True
        controls = (self.chk_headless, self.chk_auto_concurrency, self.spin_concurrency,
                    self.slider_vol, self.chk_win_notify, self.chk_cloud)
        blockers = [QSignalBlocker(control) for control in controls]
        with config_lock:
            self.chk_headless.setChecked(bool(app_settings.get("headless", True)))
            self.chk_auto_concurrency.setChecked(bool(app_settings.get("concurrency_auto", True)))
            self.spin_concurrency.setValue(int(app_settings.get("concurrency", 3)))
            self.slider_vol.setValue(int(app_settings.get("volume", 100)))
            self.chk_win_notify.setChecked(bool(app_settings.get("windows_notifications", True)))
            self.txt_webhook.setText(app_settings.get("discord_webhook", ""))
            self.txt_cloud_url.setText(app_settings.get("cloud_service_url") or DEFAULT_CLOUD_SERVICE_URL)
            self.refresh_sound_dropdown()
        self.unmute_volume = self.slider_vol.value() or 100
        self.on_volume_change(self.slider_vol.value())
        self._update_concurrency_hint()
        self._update_cloud_ui()
        del blockers
        self._loading = False

    def save_settings(self, _value=None):
        if self._loading:
            return
        with config_lock:
            app_settings.update({
                "headless": self.chk_headless.isChecked(),
                "concurrency_auto": self.chk_auto_concurrency.isChecked(),
                "concurrency": self.spin_concurrency.value(),
                "volume": self.slider_vol.value(),
                "windows_notifications": self.chk_win_notify.isChecked(),
            })
        self._save_timer.start(400)

    def on_auto_concurrency_toggled(self, _checked=None):
        self._update_concurrency_hint()
        self.save_settings()

    def _update_concurrency_hint(self):
        auto = self.chk_auto_concurrency.isChecked()
        self.spin_concurrency.setEnabled(not auto)
        self.lbl_concurrency_hint.setText(
            "Adjusts the number of active episodes to suit your connection."
            if auto else "Uses this fixed number for each new download.")

    def open_browser(self):
        from core.selenium_engine import launch_visible_browser
        threading.Thread(target=launch_visible_browser, daemon=True).start()

    def _on_task_buttons(self, _start, _close, browser_enabled):
        self.btn_profile.setEnabled(browser_enabled)

    def _update_cloud_ui(self):
        with config_lock:
            enabled = bool(app_settings.get("cloud_notify_enabled", False))
            registered = bool(app_settings.get("cloud_subscriber_id"))
        blocker = QSignalBlocker(self.chk_cloud)
        self.chk_cloud.setChecked(enabled)
        del blocker
        if self._cloud_busy:
            return
        if enabled and registered:
            self.lbl_cloud_status.setText(
                f"Active · {len(get_watchlist())} followed shows. Remote downloads require "
                "your PC to be on, with the app or background watcher connected.")
        elif enabled:
            self.lbl_cloud_status.setText("Cloud notifications need a connection. Save Discord settings to connect.")
        else:
            self.lbl_cloud_status.setText("Cloud notifications are off. Desktop alerts require the app to be running.")

    def _discord_values(self, require_webhook=False):
        webhook = self.txt_webhook.text().strip()
        service_url = self.txt_cloud_url.text().strip().rstrip("/") or DEFAULT_CLOUD_SERVICE_URL
        if require_webhook and not webhook:
            self._message("warning", "Webhook required", "Enter a Discord webhook URL before enabling cloud notifications.")
            return None
        if webhook and not webhook.startswith(("https://discord.com/api/webhooks/",
                                               "https://discordapp.com/api/webhooks/")):
            self._message("warning", "Invalid webhook", "Enter a Discord webhook URL, or leave it empty to disable desktop Discord alerts.")
            return None
        try:
            parsed = urlsplit(service_url)
            valid_service = parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except ValueError:
            valid_service = False
        if not valid_service:
            self._message("warning", "Invalid service URL", "Enter an HTTP or HTTPS URL for the cloud service.")
            return None
        with config_lock:
            app_settings["discord_webhook"] = webhook
            app_settings["cloud_service_url"] = service_url
        self.txt_webhook.setText(webhook)
        self.txt_cloud_url.setText(service_url)
        save_config()
        return service_url, webhook

    def save_discord(self):
        if self._cloud_busy:
            return
        enabled = bool(app_settings.get("cloud_notify_enabled"))
        values = self._discord_values(require_webhook=enabled)
        if values is None:
            return
        if enabled:
            self._run_cloud(True, *values)
        else:
            self._message("success", "Discord settings saved", "Your preferences will be used for new downloads and episode alerts.")

    def _on_cloud_toggle(self, enabled):
        if self._loading or self._cloud_busy:
            return
        if enabled:
            values = self._discord_values(require_webhook=True)
            if values is None:
                self._update_cloud_ui()
                return
            self._run_cloud(True, *values)
        else:
            self._run_cloud(False)

    def _run_cloud(self, enabling, service_url="", webhook=""):
        self._cloud_busy = True
        for control in (self.chk_cloud, self.txt_cloud_url, self.txt_webhook, self.btn_save_discord):
            control.setEnabled(False)
        self.lbl_cloud_status.setText("Connecting to cloud service…" if enabling else "Disabling cloud notifications…")

        def run():
            try:
                if enabling:
                    ok, message = cloud_register_and_sync(service_url, webhook)
                else:
                    cloud_unsubscribe()
                    set_windows_autostart(False)
                    ok, message = True, "Cloud notifications disabled."
            except Exception:
                # Do not show an exception that could contain a credential URL.
                ok, message = False, "Could not update cloud settings. Check the connection and try again."
            try:
                self._cloud_finished.emit(enabling, ok, message)
            except RuntimeError:
                pass  # The window closed while this daemon worker was finishing.

        threading.Thread(target=run, daemon=True).start()

    def _on_cloud_finished(self, enabling, ok, message):
        self._cloud_busy = False
        for control in (self.chk_cloud, self.txt_cloud_url, self.txt_webhook, self.btn_save_discord):
            control.setEnabled(True)
        if ok and enabling:
            start_background_watcher()
        self._update_cloud_ui()
        self.cloud_changed.emit()
        self._message("success" if ok else "error",
                      "Cloud notifications" if ok else "Cloud connection failed", message)

    def _message(self, level, title, content):
        getattr(InfoBar, level)(title, content, position=InfoBarPosition.TOP,
                                duration=5000, parent=self.window())

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
    def refresh_sound_dropdown(self):
        self.combo_sound.blockSignals(True)
        self.combo_sound.clear()
        builtin = builtin_sound_path()
        self.combo_sound.addItem("None", userData=SOUND_NONE)
        self.combo_sound.addItem("Default (built-in)", userData=builtin)
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
