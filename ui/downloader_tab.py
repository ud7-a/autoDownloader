import os
import ctypes
import threading
import subprocess
import tempfile
import shutil
import sys
import urllib.request
from urllib.parse import unquote

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIntValidator, QCursor
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog, QMessageBox, QDialog)

# THE UPGRADE: We are using Fluent Widgets for everything!
from qfluentwidgets import (PushButton, PrimaryPushButton, LineEdit, CheckBox, 
                            ComboBox, Slider, SmoothScrollArea, MessageBoxBase, SubtitleLabel, SpinBox, SwitchButton, FluentIcon as FIF, ToolButton, InfoBar, InfoBarPosition)

from utils.config import app_settings, sites_data, save_config, config_lock
from core.signals import signals
from core.selenium_engine import run_selenium_task, launch_visible_browser, kill_stuck_chrome_processes
from core.updater import check_for_updates_silently



class UpdateDialog(QDialog):
    # (Keep your UpdateDialog exactly the same for now)
    pass 

class DownloaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

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
        
        # THE FIX: Native Fluent ComboBox!
        self.combo_site = ComboBox()
        self.combo_site.currentTextChanged.connect(self.on_site_select)
        main_layout.addWidget(self.combo_site)
        
        self.lbl_url = QLabel("No profile selected")
        self.lbl_url.setStyleSheet("color: #888888; font-size: 12px;")
        main_layout.addWidget(self.lbl_url)

        # Fluent CheckBox
        self.chk_headless = CheckBox("Run Invisibly (Headless)")
        self.chk_headless.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_headless.setChecked(app_settings.get("headless", True))
        self.chk_headless.toggled.connect(self.save_settings)
        main_layout.addWidget(self.chk_headless)

        # Transparency Toggle & Restart Button
        transparency_layout = QHBoxLayout()
        self.chk_transparency = SwitchButton("Enable Transparency Effects (Mica/Acrylic)")
        self.chk_transparency.setOnText("Transparency On")
        self.chk_transparency.setOffText("Transparency Off")
        self.chk_transparency.setChecked(app_settings.get("transparency", True))
        self.chk_transparency.checkedChanged.connect(self.toggle_transparency)
        
        self.btn_restart_app = PushButton(FIF.UPDATE, "Restart to Apply")
        self.btn_restart_app.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restart_app.clicked.connect(self.restart_app)
        self.btn_restart_app.hide()
        
        transparency_layout.addWidget(self.chk_transparency)
        transparency_layout.addWidget(self.btn_restart_app)
        transparency_layout.addStretch(1)
        main_layout.addLayout(transparency_layout)

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
        self.combo_sound.currentIndexChanged.connect(self.on_sound_change)
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

        ep_layout = QHBoxLayout()
        st_layout = QVBoxLayout()
        st_layout.addWidget(QLabel("Start Episode", styleSheet="font-weight: bold;"))
        self.txt_start = LineEdit()
        self.txt_start.setText("1")
        self.txt_start.setValidator(QIntValidator(1, 99999))
        self.txt_start.textEdited.connect(self.save_settings)
        st_layout.addWidget(self.txt_start)
        
        en_layout = QVBoxLayout()
        en_layout.addWidget(QLabel("End Episode", styleSheet="font-weight: bold;"))
        self.txt_end = LineEdit()
        self.txt_end.setText("1")
        self.txt_end.setValidator(QIntValidator(1, 99999))
        self.txt_end.textEdited.connect(self.save_settings)
        en_layout.addWidget(self.txt_end)
        
        ep_layout.addLayout(st_layout)
        ep_layout.addLayout(en_layout)
        main_layout.addLayout(ep_layout)
        
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

    # KEEP ALL YOUR EXISTING FUNCTIONS BELOW HERE EXACTLY THE SAME!
    # (refresh_sound_dropdown, browse_folder, save_settings, start_task, etc.)
    # I have omitted them here to save space, but DO NOT delete your functions!

    def on_sound_change(self, index):
        if index >= 0:
            sounds = app_settings.get("custom_sounds", [])
            if sounds and index < len(sounds):
                app_settings["selected_sound"] = sounds[index]
                save_config()

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
        if selected in sounds:
            sounds.remove(selected)
            app_settings["custom_sounds"] = sounds
            if sounds:
                app_settings["selected_sound"] = sounds[0]
            else:
                app_settings["selected_sound"] = ""
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
                    if mci_path.lower().endswith(".mp3"):
                        ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" type mpegvideo alias custom_audio', None, 0, None)
                    else:
                        ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" alias custom_audio', None, 0, None)
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
    def restart_app(self):
        import os
        import sys
        import subprocess
        
        # Sanitizing environment: remove all PyInstaller variables and references to the old _MEIPASS folder.
        # This prevents the restarted app from trying to load DLLs/assets from the old, deleting folder.
        env = os.environ.copy()
        
        # Explicitly remove known PyInstaller variables and all _PYI_ bootloader variables
        # so the new process knows it is a fresh parent bootloader run.
        for key in ["_MEIPASS", "_MEIPASS2", "PYTHONPATH", "PYTHONHOME"]:
            env.pop(key, None)
            
        for key in list(env.keys()):
            if key.startswith("_PYI_"):
                env.pop(key, None)
            
        # Dynamically scan and remove/clean any variable containing the old _MEIPASS directory path
        if hasattr(sys, '_MEIPASS'):
            old_mei = os.path.abspath(sys._MEIPASS).lower()
            keys_to_remove = []
            for k, v in list(env.items()):
                val_lower = os.path.abspath(v).lower() if os.path.isabs(v) else v.lower()
                if old_mei in val_lower:
                    if k.upper() == "PATH":
                        # For PATH, we just filter out the old _MEIPASS segment instead of deleting the whole PATH
                        parts = v.split(os.pathsep)
                        cleaned_parts = [p for p in parts if old_mei not in os.path.abspath(p).lower()]
                        env[k] = os.pathsep.join(cleaned_parts)
                    else:
                        keys_to_remove.append(k)
                        
            for k in keys_to_remove:
                env.pop(k, None)
                
        # Determine a safe working directory (the folder containing our executable)
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        
        # Spawn the new instance completely detached and isolated
        subprocess.Popen(
            [sys.executable] + sys.argv[1:], 
            env=env, 
            cwd=exe_dir,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def toggle_transparency(self, is_checked):
        app_settings["transparency"] = is_checked
        save_config()
        if is_checked:
            from utils.config import force_windows_transparency
            force_windows_transparency()
        self.btn_restart_app.show()

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
                if hasattr(self, 'txt_start'): 
                    sites_data[site]["last_start"] = self.txt_start.text().strip()
                if hasattr(self, 'txt_end'): 
                    sites_data[site]["last_end"] = self.txt_end.text().strip()
        save_config()
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", app_settings["download_dir"])
        if folder:
            self.txt_dir.setText(os.path.normpath(folder))
            app_settings["download_dir"] = os.path.normpath(folder)
            save_config()
    def refresh_sound_dropdown(self):
        self.combo_sound.blockSignals(True)
        self.combo_sound.clear()
        sounds = app_settings.get("custom_sounds", [])
        if not sounds:
            self.combo_sound.addItem("No sounds added...")
        else:
            for s in sounds:
                self.combo_sound.addItem(os.path.basename(s), userData=s)
            selected = app_settings.get("selected_sound", "")
            if selected in sounds:
                self.combo_sound.setCurrentIndex(sounds.index(selected))
            else:
                self.combo_sound.setCurrentIndex(0)
                app_settings["selected_sound"] = sounds[0] if sounds else ""
        self.combo_sound.blockSignals(False)
    def refresh_dropdown(self):
        self.combo_site.blockSignals(True)
        self.combo_site.clear()
        with config_lock:
            has_sites = len(sites_data) > 0
            keys = list(sites_data.keys())
        if not has_sites:
            self.combo_site.addItem("No Profiles")
            self.lbl_url.setText("No profile selected")
        else:
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
                prof_start = sites_data[text].get("last_start", "1")
                prof_end = sites_data[text].get("last_end", "1")
                if hasattr(self, 'txt_start'):
                    self.txt_start.blockSignals(True)
                    self.txt_start.setText(prof_start)
                    self.txt_start.blockSignals(False)
                if hasattr(self, 'txt_end'):
                    self.txt_end.blockSignals(True)
                    self.txt_end.setText(prof_end)
                    self.txt_end.blockSignals(False)
                save_config()
            else: 
                self.lbl_url.setText("No profile selected")
    def set_buttons(self, start_en, close_en, prof_en):
        self.btn_start.setEnabled(start_en)
        self.btn_profile.setEnabled(prof_en)
    def start_task(self):
        site = self.combo_site.currentText()
        if site == "No Profiles": return
        try:
            start_ep = int(self.txt_start.text())
            end_ep = int(self.txt_end.text())
        except ValueError: return
        episodes_list = list(range(start_ep, end_ep + 1))
        target_dir = app_settings["download_dir"]
        headless = self.chk_headless.isChecked()
        webhook = self.txt_webhook.text().strip()
        selected_sound = app_settings.get("selected_sound", "")
        volume = app_settings.get("volume", 100)
        concurrency = app_settings.get("concurrency", 3)
        signals.update_buttons.emit(False, True, False)
        signals.task_started.emit()
        threading.Thread(target=run_selenium_task, args=(site, episodes_list, target_dir, headless, webhook, selected_sound, volume , concurrency), daemon=True).start()