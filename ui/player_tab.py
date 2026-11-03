import os
import subprocess

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QFrame, QLabel)
from qfluentwidgets import (CardWidget, IconWidget, SubtitleLabel, StrongBodyLabel,
                            BodyLabel, CaptionLabel, PrimaryPushButton, PushButton,
                            HyperlinkButton, IndeterminateProgressBar, InfoBar, InfoBarPosition,
                            ScrollArea, SwitchButton, ComboBox, FluentIcon as FIF)

from utils import mpvnet
from utils.config import app_settings

# Shown per shader in the profile panels: (name, what it does). Unknown shaders
# fall back to their file name, so editing the bundled mpv.conf never breaks the tab.
SHADER_INFO = {
    "Anime4K_Clamp_Highlights": ("Clamp highlights", "stops halos around edges"),
    "Anime4K_Restore_CNN_Soft_UL": ("Restore Soft UL",
                                    "removes blur and compression, no over-sharpening"),
    "Anime4K_Upscale_CNN_x2_UL": ("Upscale 2× UL", "largest model, crispest lines"),
    "FSRCNNX_x2_8-0-4-1": ("FSRCNNX 2×", "detail-preserving upscale"),
    "adaptive-sharpen": ("Adaptive sharpen", "light, restores the original's detail"),
}
PROFILE_TITLES = {"anime": "Anime", "series": "Series & movies"}
SHORTCUTS = (("F1", "Anime profile"), ("F2", "Series profile"),
             ("Ctrl+1", "Shaders off / on"), ("P", "Show active shaders"),
             ("K", "Skip opening / ending (when offered)"))
SKIP_MODES = (("off", "Off"), ("ask", "Ask (press K)"), ("auto", "Skip automatically"))

MUTED = (QColor(96, 96, 96), QColor(160, 160, 160))   # (light, dark) theme text
PILL_COLORS = {
    "ok": ("#6ccb5f", "rgba(108,203,95,0.14)"),
    "off": ("#c5c5c5", "rgba(255,255,255,0.08)"),
    "busy": ("#4cc2ff", "rgba(76,194,255,0.14)"),
    "bad": ("#ff99a4", "rgba(255,153,164,0.14)"),
}
PANEL_STYLE = ("#ProfilePanel{background:rgba(255,255,255,0.035);"
               "border:1px solid rgba(255,255,255,0.07);border-radius:8px;}")
STEP_STYLE = ("background:rgba(76,194,255,0.16);color:#4cc2ff;border-radius:10px;"
              "font:600 11px 'Segoe UI';")
KEYCAP_STYLE = ("background:rgba(255,255,255,0.06);color:#e8e8e8;"
                "border:1px solid rgba(255,255,255,0.12);"
                "border-bottom:2px solid rgba(255,255,255,0.20);"
                "border-radius:4px;padding:1px 7px;font:12px 'Segoe UI';")


def muted(label):
    label.setTextColor(*MUTED)
    # Secondary text wraps on narrow windows instead of forcing the page wider.
    label.setWordWrap(True)
    return label


def wrappable_path(path):
    """A path that word wrap can break after each folder: one long run of
    characters set the page's minimum width and pushed it past the tab."""
    return path.replace("\\", "\\​")


def section_label(text):
    label = muted(CaptionLabel(text.upper()))
    font = label.font()
    font.setWeight(600)
    font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 0.6)
    label.setFont(font)
    return label


class StatusPill(QLabel):
    """Small rounded status tag shown in a card's header."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state("off", "")

    def set_state(self, state, text):
        fg, bg = PILL_COLORS[state]
        self.setText(text)
        self.setStyleSheet(f"background:{bg};color:{fg};border-radius:11px;"
                           f"padding:3px 11px;font:600 12px 'Segoe UI';")


class WingetInstallThread(QThread):
    """winget install (or, with upgrade=True, winget upgrade) of mpv.net."""
    line = pyqtSignal(str)
    done = pyqtSignal(int)

    def __init__(self, parent=None, upgrade=False):
        super().__init__(parent)
        self.upgrade = upgrade

    def run(self):
        try:
            run = mpvnet.run_winget_upgrade if self.upgrade else mpvnet.run_winget_install
            code = run(self.line.emit)
        except Exception as e:
            self.line.emit(f"winget could not start: {e}")
            code = -1
        self.done.emit(code)


class UpdateCheckThread(QThread):
    """Asks GitHub for mpv.net's newest release (network: off the UI thread)."""
    done = pyqtSignal(object)        # mpvnet.check_update's dict, or {"error": text}

    def __init__(self, installed, parent=None):
        super().__init__(parent)
        self.installed = installed

    def run(self):
        try:
            result = mpvnet.check_update(self.installed)
        except Exception as e:
            result = {"error": str(e) or type(e).__name__}
        self.done.emit(result)


class ElevatedThread(QThread):
    """Runs an elevated step (UAC prompt + wait) off the UI thread."""
    done = pyqtSignal(int)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self._fn, self._args = fn, args

    def run(self):
        try:
            code = self._fn(*self._args)
        except Exception:
            code = -1
        self.done.emit(code)


def describe_default(status):
    """("ok"|"partial"|"no", text) for the default-player line."""
    on = [ext for ext, yes in status.items() if yes]
    if on and len(on) == len(status):
        return "ok", f"Yes. {' and '.join(status)} files open in mpv.net."
    if on:
        off = [ext for ext in status if ext not in on]
        return "partial", (f"Only {', '.join(on)} files open in mpv.net; "
                           f"{', '.join(off)} still open in another player.")
    return "no", "No. Your downloads open in another player, without the shaders."


class PlayerWidget(QWidget):
    """Video Player tab: install mpv.net through winget and apply the bundled
    quality-shader config (Anime4K for anime, FSRCNNX for series)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exe = None
        self._version = None
        self._install_thread = None
        self._upgrading = False
        self._check_thread = None
        self._update = None          # check_update's result while a newer mpv.net exists

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        # Text wraps instead; a sideways scrollbar only ever hid the right column.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        scroll.setWidget(page)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("Video Player"))
        layout.addWidget(muted(BodyLabel(
            "Watch your downloads in mpv.net with upscaling shaders for noticeably "
            "sharper anime.")))
        layout.addSpacing(4)
        layout.addWidget(self._build_player_card())
        layout.addWidget(self._build_shader_card())
        layout.addWidget(self._build_skip_card())
        layout.addStretch(1)
        # Detection reads the registry, so it waits for the tab to be opened
        # instead of slowing down app start (see showEvent).

    # ------------------------------------------------------------ layout

    def _card(self, icon, title, subtitle):
        """Card with a header row (icon, title, subtitle, status pill on the right)."""
        card = CardWidget(self)
        body = QVBoxLayout(card)
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        ico = IconWidget(icon, card)
        ico.setFixedSize(28, 28)
        header.addWidget(ico)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(StrongBodyLabel(title))
        sub = muted(CaptionLabel(subtitle))
        titles.addWidget(sub)
        header.addLayout(titles, 1)
        pill = StatusPill(card)
        header.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(header)
        return card, body, pill, sub

    @staticmethod
    def _button_row(*buttons, leading=None):
        row = QHBoxLayout()
        row.setSpacing(8)
        if leading is not None:
            row.addLayout(leading, 1)
        else:
            row.addStretch(1)
        for btn in buttons:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(btn)
        return row

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:rgba(255,255,255,0.07);border:none;")
        return line

    def _build_player_card(self):
        card, body, self.player_pill, self.lbl_player_path = self._card(
            FIF.VIDEO, "mpv.net", "")
        self.lbl_player_path.setWordWrap(True)

        self.install_bar = IndeterminateProgressBar(card)
        self.install_bar.hide()
        body.addWidget(self.install_bar)
        self.lbl_install_line = muted(CaptionLabel())
        self.lbl_install_line.setWordWrap(True)
        self.lbl_install_line.hide()
        body.addWidget(self.lbl_install_line)

        self.btn_recheck = PushButton(FIF.SYNC, "Check again")
        self.btn_open_player = PushButton(FIF.PLAY, "Open mpv.net")
        self.btn_install = PrimaryPushButton(FIF.DOWNLOAD, "Install with winget")
        self.btn_update = PrimaryPushButton(FIF.UPDATE, "Update")
        self.btn_update.hide()
        self.btn_install.clicked.connect(self.install_mpvnet)
        self.btn_update.clicked.connect(self.update_mpvnet)
        self.btn_open_player.clicked.connect(self.open_player)
        self.btn_recheck.clicked.connect(self.check_now)
        body.addLayout(self._button_row(self.btn_recheck, self.btn_open_player,
                                        self.btn_update, self.btn_install))

        # Play the app's own downloads in mpv.net -- needs nothing from Windows.
        self.default_divider = self._divider()
        body.addWidget(self.default_divider)
        play_info = QVBoxLayout()
        play_info.setSpacing(0)
        play_info.addWidget(BodyLabel("Play downloads in mpv.net"))
        play_info.addWidget(muted(CaptionLabel(
            "\"Start Watching\" and History open mpv.net directly, with the whole "
            "session queued, whatever your Windows default is.")))
        self.switch_play = SwitchButton()
        self.switch_play.setOnText("On")
        self.switch_play.setOffText("Off")
        self.switch_play.setChecked(mpvnet.play_in_mpvnet_enabled())
        self.switch_play.checkedChanged.connect(self._on_play_switch)
        self.play_row = QWidget()
        self.play_row.setStyleSheet("background:transparent;")
        prow = self._button_row(self.switch_play, leading=play_info)
        prow.setContentsMargins(0, 0, 0, 0)
        self.play_row.setLayout(prow)
        body.addWidget(self.play_row)

        # Default player for Explorer: register with Windows, then the user
        # confirms in Settings (Windows allows no other way).
        info = QVBoxLayout()
        info.setSpacing(0)
        info.addWidget(BodyLabel("Default player in Windows"))
        self.lbl_default = muted(CaptionLabel())
        self.lbl_default.setWordWrap(True)
        info.addWidget(self.lbl_default)
        self.btn_make_default = PushButton(FIF.PIN, "Make default player")
        self.btn_make_default.setToolTip("One permission prompt; Windows switches .mp4 and .mkv "
                                         "to mpv.net at your next sign-in.")
        self.btn_make_default.clicked.connect(self.make_default_player)
        self.btn_default_now = PushButton(FIF.SETTING, "Set it now in Settings")
        self.btn_default_now.setToolTip("Don't want to wait for the next sign-in? Click .mp4 and "
                                        ".mkv on mpv.net's page and choose \"Set default\".")
        self.btn_default_now.clicked.connect(self._open_default_settings)
        self.btn_stop_default = PushButton(FIF.CANCEL, "Stop managing")
        self.btn_stop_default.setToolTip("Remove the setting that re-applies mpv.net at every "
                                         "sign-in.")
        self.btn_stop_default.clicked.connect(self.stop_managing_default)
        self.default_row = QWidget()
        self.default_row.setStyleSheet("background:transparent;")
        row = self._button_row(self.btn_default_now, self.btn_stop_default,
                               self.btn_make_default, leading=info)
        row.setContentsMargins(0, 0, 0, 0)
        self.default_row.setLayout(row)
        body.addWidget(self.default_row)

        self._register_thread = None
        # Coming back from Settings: show the new state straight away.
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state)
        return card

    def _on_app_state(self, state):
        if (state == Qt.ApplicationState.ApplicationActive and self.isVisible()
                and self._exe and self._register_thread is None):
            self._refresh_default()

    def _on_play_switch(self, on):
        from utils.config import app_settings, config_lock, save_config
        with config_lock:
            app_settings[mpvnet.PLAY_SETTING] = bool(on)
        save_config()

    def _build_shader_card(self):
        card, body, self.shader_pill, _ = self._card(
            FIF.PALETTE, "Quality shaders",
            "mpv.net picks the right profile for each file automatically")

        # Profiles: what runs, and on which files.
        body.addWidget(section_label("Profiles"))
        panels = QHBoxLayout()
        panels.setSpacing(12)
        self._panels = panels          # side by side; stacked on narrow windows
        self._profile_when = {}
        for name, stems in mpvnet.bundled_profiles():
            panels.addWidget(self._profile_panel(name, stems), 1)
        body.addLayout(panels)

        # Shortcuts inside the player.
        body.addSpacing(2)
        body.addWidget(section_label("Shortcuts in mpv.net"))
        keys = QGridLayout()
        keys.setHorizontalSpacing(10)
        keys.setVerticalSpacing(8)
        for i, (key, action) in enumerate(SHORTCUTS):
            cap = QLabel(key)
            cap.setStyleSheet(KEYCAP_STYLE)
            row, col = divmod(i, 2)
            keys.addWidget(cap, row, col * 2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            action_label = BodyLabel(action)
            action_label.setWordWrap(True)
            keys.addWidget(action_label, row, col * 2 + 1)
        keys.setColumnStretch(1, 1)
        keys.setColumnStretch(3, 1)
        body.addLayout(keys)

        # Footer: the one caveat, then the actions.
        body.addSpacing(2)
        body.addWidget(self._divider())
        note = QHBoxLayout()
        note.setSpacing(8)
        info = IconWidget(FIF.INFO, card)
        info.setFixedSize(16, 16)
        note.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
        note.addWidget(muted(CaptionLabel(
            "The anime profile needs a high-end GPU. Your current config is backed up "
            "first.")), 1)

        self.btn_open_config = PushButton(FIF.FOLDER, "Open config folder")
        self.btn_apply = PrimaryPushButton(FIF.BRUSH, "Apply shaders")
        self.btn_open_config.clicked.connect(self.open_config_folder)
        self.btn_apply.clicked.connect(self.apply_shaders)
        body.addLayout(self._button_row(self.btn_open_config, self.btn_apply, leading=note))
        return card

    def _build_skip_card(self):
        card, body, self.skip_pill, _ = self._card(
            FIF.SKIP_FORWARD, "Skip openings and endings",
            "Found from the audio of your downloaded episodes (3 or more of an anime), "
            "in the background after each download")
        self.skip_boxes = {}
        for key, title, caption in (
                ("opening", "Openings", "Ask shows \"Skip opening\" for 10 seconds; "
                                        "K skips until the opening ends."),
                ("ending", "Endings", "Skips to the end of the song, so a scene after "
                                      "the credits still plays.")):
            info = QVBoxLayout()
            info.setSpacing(0)
            info.addWidget(BodyLabel(title))
            info.addWidget(muted(CaptionLabel(caption)))
            box = ComboBox()
            for _mode, label in SKIP_MODES:
                box.addItem(label)
            box.setMinimumWidth(190)
            box.currentIndexChanged.connect(self._on_skip_mode)
            self.skip_boxes[key] = box
            row = self._button_row(box, leading=info)
            body.addLayout(row)
        return card

    def _skip_modes(self):
        return {key: SKIP_MODES[box.currentIndex()][0] for key, box in self.skip_boxes.items()}

    def _refresh_skip(self, exe):
        from utils import skip_times
        cfg = mpvnet.config_dir(exe)
        current = skip_times.read_settings(cfg)
        modes = [m for m, _label in SKIP_MODES]
        for key, box in self.skip_boxes.items():
            box.blockSignals(True)
            box.setCurrentIndex(modes.index(current[key]))
            box.blockSignals(False)
            box.setEnabled(bool(exe))
            box.setToolTip("" if exe else "Install mpv.net first")
        script = os.path.join(cfg, "scripts", mpvnet.SKIP_SCRIPT)
        if exe and not os.path.isfile(script):
            mpvnet.ensure_progress_script()       # the app's own scripts, not the config
        if not exe:
            self.skip_pill.set_state("off", "Needs mpv.net")
        elif not os.path.isfile(script):
            self.skip_pill.set_state("bad", "Script missing")
        elif current["opening"] == "off" and current["ending"] == "off":
            self.skip_pill.set_state("off", "Off")
        else:
            self.skip_pill.set_state("ok", "On")

    def _on_skip_mode(self, _index):
        from utils import skip_times
        if not self._exe:
            return
        modes = self._skip_modes()
        if skip_times.write_settings(mpvnet.config_dir(self._exe), modes["opening"],
                                     modes["ending"]):
            mpvnet.ensure_progress_script()       # installs the skip script too
            self._refresh_skip(self._exe)
            InfoBar.success("Saved", "Takes effect the next time an episode opens in mpv.net.",
                            position=InfoBarPosition.TOP, duration=2500, parent=self.window())

    def _profile_panel(self, name, stems):
        panel = QFrame()
        panel.setObjectName("ProfilePanel")
        panel.setStyleSheet(PANEL_STYLE)
        box = QVBoxLayout(panel)
        box.setContentsMargins(14, 12, 14, 14)
        box.setSpacing(8)

        box.addWidget(StrongBodyLabel(PROFILE_TITLES.get(name, name.title())))
        when = muted(CaptionLabel())
        when.setWordWrap(True)
        self._profile_when[name] = when
        box.addWidget(when)
        box.addSpacing(2)

        for i, stem in enumerate(stems, 1):
            title, purpose = SHADER_INFO.get(stem, (stem, ""))
            row = QHBoxLayout()
            row.setSpacing(10)
            num = QLabel(str(i))
            num.setFixedSize(20, 20)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(STEP_STYLE)
            row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            # Name over purpose, the purpose wrapping: side by side and unwrapped,
            # three panels needed a page wider than the tab and cut its right
            # edge off (the page never scrolls sideways).
            text = QVBoxLayout()
            text.setSpacing(0)
            label = BodyLabel(title)
            label.setToolTip(f"{stem}.glsl")
            text.addWidget(label)
            if purpose:
                text.addWidget(muted(CaptionLabel(purpose)))
            row.addLayout(text, 1)
            box.addLayout(row)
        box.addStretch(1)
        return panel

    STACK_PANELS_BELOW = 1100  # px of tab width: under this the three profiles stack

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = (QHBoxLayout.Direction.TopToBottom if self.width() < self.STACK_PANELS_BELOW
                     else QHBoxLayout.Direction.LeftToRight)
        if self._panels.direction() != direction:
            self._panels.setDirection(direction)

    def showEvent(self, event):
        super().showEvent(event)
        if self._install_thread is None:
            self.refresh_status()

    # ------------------------------------------------------------ status

    def refresh_status(self):
        exe, version = mpvnet.find_mpvnet()
        self._exe = exe
        self._version = version or mpvnet.file_version(exe)
        version = self._version
        installing = self._install_thread is not None
        has_winget = bool(mpvnet.winget_path())

        if exe and self._upgrading:
            self.player_pill.set_state("busy", "Updating…")
            self.lbl_player_path.setText(wrappable_path(exe))
        elif exe and self._update:
            self.player_pill.set_state("busy", f"{self._update['latest']} available")
            self.lbl_player_path.setText(wrappable_path(exe))
        elif exe:
            self.player_pill.set_state("ok", f"Installed {version}" if version else "Installed")
            self.lbl_player_path.setText(wrappable_path(exe))
        elif installing:
            self.player_pill.set_state("busy", "Installing…")
            self.lbl_player_path.setText("Windows may ask for permission.")
        elif has_winget:
            self.player_pill.set_state("off", "Not installed")
            self.lbl_player_path.setText("Free, open-source player built on mpv.")
        else:
            self.player_pill.set_state("bad", "Not installed")
            self.lbl_player_path.setText(
                "winget isn't available on this PC. Install \"App Installer\" from the "
                "Microsoft Store, then check again.")
        self.btn_install.setVisible(not exe)
        self.btn_install.setEnabled(not installing and has_winget)
        self.btn_update.setVisible(bool(exe and self._update))
        self.btn_update.setEnabled(not installing)
        if self._update:
            self.btn_update.setText(f"Update to {self._update['latest']}")
        self.btn_recheck.setText("Check for updates" if exe else "Check again")
        self.btn_recheck.setToolTip("Looks at mpv.net here again, and on GitHub for a newer "
                                    "version" if exe else "Looks for mpv.net again")
        self.btn_open_player.setVisible(bool(exe))
        self._refresh_default()

        folder = mpvnet.anime_folder_name(app_settings.get("download_dir"))
        when = {"anime": f'Files inside a "{folder}" folder', "series": "Everything else"}
        for name, label in self._profile_when.items():
            # A profile with no rule of its own (a hand-edited mpv.conf) is only
            # ever picked by hand.
            label.setText(when.get(name, "Only when picked by hand"))

        cfg = mpvnet.config_dir(exe)
        applied = mpvnet.shaders_applied(cfg)
        self.shader_pill.set_state("ok" if applied else "off",
                                   "Applied" if applied else "Not applied")
        self.btn_apply.setText("Re-apply shaders" if applied else "Apply shaders")
        self.btn_apply.setEnabled(bool(exe))
        self.btn_apply.setToolTip("" if exe else "Install mpv.net first")
        self.btn_open_config.setEnabled(os.path.isdir(cfg))
        self._refresh_skip(exe)

    # ---------------------------------------------------- default player

    def _refresh_default(self):
        """Returns True when mpv.net opens every file type the app downloads."""
        for w in (self.default_divider, self.play_row, self.default_row):
            w.setVisible(bool(self._exe))
        if not self._exe:
            return False
        state, text = describe_default(mpvnet.default_player_status())
        busy = self._register_thread is not None
        managed = mpvnet.policy_active()
        if busy:
            text = getattr(self, "_busy_text", "") or "Waiting for Windows…"
        elif managed and state == "ok":
            text += " Kept that way at every sign-in."
        elif managed:
            text = ("Policy active. Awaiting sign-in or restart to apply fully.")
        self.lbl_default.setText(text)
        self.btn_make_default.setVisible(state != "ok" and not managed)
        self.btn_make_default.setEnabled(not busy)
        self.btn_default_now.setVisible(state != "ok" and managed and not busy)
        self.btn_stop_default.setVisible(managed)
        self.btn_stop_default.setEnabled(not busy)
        return state == "ok"

    def _run_elevated(self, fn, *args, then, busy_text="Waiting for Windows to grant permission…"):
        self._busy_text = busy_text
        if self._register_thread is not None:
            return
        self._register_thread = ElevatedThread(fn, *args, parent=self)
        self._register_thread.done.connect(then)
        self._register_thread.start()
        self._refresh_default()

    def _finish_elevated(self, code):
        """Common end of an elevated step; False when it was declined."""
        thread, self._register_thread = self._register_thread, None
        if thread is not None:
            thread.deleteLater()
        self._refresh_default()
        if code == mpvnet.ERROR_CANCELLED:
            InfoBar.warning("Not changed", "Windows' permission prompt was declined.",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
            return False
        return True

    def make_default_player(self):
        """One admin prompt: repair mpv.net's registration if it is missing or from
        an older install, and set Windows' default-associations policy, which
        Windows applies itself at sign-in. (Windows 11 blocks every way for an app
        to switch the default immediately -- see utils/mpvnet.py.)"""
        if self._exe:
            self._run_elevated(mpvnet.enable_default_policy, self._exe,
                               then=self._on_policy_enabled)

    def _on_policy_enabled(self, code):
        if not self._finish_elevated(code):
            return
        if not (mpvnet.policy_active() and mpvnet.is_registered(self._exe)):
            InfoBar.error("Couldn't set mpv.net as the default",
                          f"The setup step exited with code {code}. \"Play downloads in "
                          "mpv.net\" works without this.",
                          duration=-1, position=InfoBarPosition.TOP, parent=self)
            return
        if all(mpvnet.default_player_status().values()):
            InfoBar.success("mpv.net is now your default player",
                            "All .mp4 and .mkv files will open in mpv.net automatically. "
                            "This setting is protected and applied instantly.",
                            duration=5000, position=InfoBarPosition.TOP, parent=self)
        else:
            InfoBar.warning("Partially applied",
                            "The Group Policy is active but the instant application failed. "
                            "It will take effect at your next sign-in.",
                            duration=8000, position=InfoBarPosition.TOP, parent=self)

    def _open_default_settings(self):
        """Immediate alternative: mpv.net's page in Settings, where the user clicks
        each type and "Set default"."""
        try:
            os.startfile(mpvnet.default_apps_uri())
        except OSError:
            os.startfile("ms-settings:defaultapps")
        InfoBar.info("In Settings", "Click .mp4, choose mpv.net, \"Set default\"; then the same "
                     "for .mkv. This tab updates when you come back.",
                     duration=10000, position=InfoBarPosition.TOP, parent=self)

    def stop_managing_default(self):
        self._run_elevated(mpvnet.disable_default_policy, then=self._on_policy_disabled)

    def _on_policy_disabled(self, code):
        if not self._finish_elevated(code):
            return
        if mpvnet.policy_active():
            InfoBar.error("Couldn't remove the setting", f"Exited with code {code}.",
                          duration=-1, position=InfoBarPosition.TOP, parent=self)
            return
        InfoBar.success("No longer managed",
                        "mpv.net stays the default until you pick another player.",
                        duration=5000, position=InfoBarPosition.TOP, parent=self)

    # ----------------------------------------------------------- install

    def install_mpvnet(self):
        if self._install_thread is not None or not mpvnet.winget_path():
            return
        self.btn_recheck.setEnabled(False)
        self.install_bar.show()
        self.install_bar.start()
        self.lbl_install_line.setText("Starting winget…")
        self.lbl_install_line.show()

        self._install_thread = WingetInstallThread(self)
        self._install_thread.line.connect(self.lbl_install_line.setText)
        self._install_thread.done.connect(self._on_install_done)
        self._install_thread.start()
        self.refresh_status()

    def _on_install_done(self, code):
        thread, self._install_thread = self._install_thread, None
        if thread is not None:
            thread.deleteLater()
        self.install_bar.stop()
        self.install_bar.hide()
        self.btn_recheck.setEnabled(True)
        self.refresh_status()

        if self._exe:
            self.lbl_install_line.hide()
            InfoBar.success("mpv.net installed", "Now apply the quality shaders below.",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        else:
            last = self.lbl_install_line.text()
            hint = f"winget exited with 0x{code:08X}" if code > 0xFFFF else f"winget exited with {code}"
            InfoBar.error("Install failed", f"{hint}. {last}", duration=-1,
                          position=InfoBarPosition.TOP, parent=self)

    # ----------------------------------------------------------- updates

    def check_now(self):
        """The Check button: look at mpv.net here again and, when it's installed,
        ask GitHub whether there's a newer version."""
        self.refresh_status()
        if not self._exe or self._check_thread is not None or self._install_thread is not None:
            return
        self.btn_recheck.setEnabled(False)
        self.lbl_install_line.setText("Checking for a newer mpv.net…")
        self.lbl_install_line.show()
        self._check_thread = UpdateCheckThread(self._version, self)
        self._check_thread.done.connect(self._on_update_checked)
        self._check_thread.start()

    def _on_update_checked(self, result):
        thread, self._check_thread = self._check_thread, None
        if thread is not None:
            thread.deleteLater()
        self.btn_recheck.setEnabled(True)
        self.lbl_install_line.hide()
        if "error" in result:
            InfoBar.warning("Couldn't check for mpv.net updates", result["error"],
                            duration=6000, position=InfoBarPosition.TOP, parent=self)
            return
        latest = result["latest"]
        if result["status"] == "current":
            self._update = None
            self.refresh_status()
            InfoBar.success("mpv.net is up to date",
                            f"{result['installed']} is the newest version.",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
            return
        self._update = result
        self.refresh_status()
        if result["status"] == "newer":
            self.lbl_install_line.setText(
                f"mpv.net {latest} is out — you have {result['installed']}.")
        else:
            self.lbl_install_line.setText(
                f"The newest mpv.net is {latest}. Yours doesn't say which version it is.")
        self.lbl_install_line.show()

    def update_mpvnet(self):
        if self._install_thread is not None or not self._update:
            return
        if not mpvnet.winget_path():
            self._update_elsewhere("winget isn't available on this PC, so the app can't "
                                   "update mpv.net.")
            return
        if mpvnet.is_running():
            InfoBar.warning("Close mpv.net first",
                            "Its installer can't replace mpv.net while it's open.",
                            duration=5000, position=InfoBarPosition.TOP, parent=self)
            return
        self._upgrading = True
        self.btn_recheck.setEnabled(False)
        self.install_bar.show()
        self.install_bar.start()
        self.lbl_install_line.setText("Starting winget…")
        self.lbl_install_line.show()
        self._install_thread = WingetInstallThread(self, upgrade=True)
        self._install_thread.line.connect(self.lbl_install_line.setText)
        self._install_thread.done.connect(self._on_update_done)
        self._install_thread.start()
        self.refresh_status()

    def _on_update_done(self, code):
        thread, self._install_thread = self._install_thread, None
        if thread is not None:
            thread.deleteLater()
        self._upgrading = False
        self.install_bar.stop()
        self.install_bar.hide()
        self.btn_recheck.setEnabled(True)
        target = (self._update or {}).get("latest", "")
        self.refresh_status()                       # reads the version again
        if mpvnet.version_tuple(self._version) >= mpvnet.version_tuple(target) > ():
            self._update = None
            self.refresh_status()
            self.lbl_install_line.hide()
            InfoBar.success("mpv.net updated", f"Now on {self._version}.",
                            duration=5000, position=InfoBarPosition.TOP, parent=self)
            return
        last = self.lbl_install_line.text()
        self.lbl_install_line.hide()
        if code == mpvnet.WINGET_NO_UPGRADE:
            self._update_elsewhere(f"winget doesn't offer {target} yet; it usually follows "
                                   "GitHub within a few days.")
        elif code == mpvnet.WINGET_NOT_FOUND:
            self._update_elsewhere("This mpv.net wasn't installed with winget, so winget "
                                   "can't update it.")
        else:
            hint = (f"winget exited with 0x{code:08X}" if code > 0xFFFF
                    else f"winget exited with {code}")
            InfoBar.error("Update failed", f"{hint}. {last}", duration=-1,
                          position=InfoBarPosition.TOP, parent=self)

    def _update_elsewhere(self, why):
        """The update can't go through winget: say why, and link the download."""
        url = (self._update or {}).get("url") or mpvnet.RELEASES_PAGE
        bar = InfoBar.warning("Update mpv.net from GitHub", why, duration=-1,
                              position=InfoBarPosition.TOP, parent=self)
        bar.addWidget(HyperlinkButton(url, "Download page"))

    def open_player(self):
        if self._exe and os.path.isfile(self._exe):
            subprocess.Popen([self._exe], close_fds=True)

    # ----------------------------------------------------------- shaders

    def apply_shaders(self):
        cfg = mpvnet.config_dir(self._exe)
        try:
            backup = mpvnet.apply_config(cfg, app_settings.get("download_dir"))
        except Exception as e:
            InfoBar.error("Couldn't apply the shaders", str(e), duration=-1,
                          position=InfoBarPosition.TOP, parent=self)
            return
        self.refresh_status()
        folder = mpvnet.anime_folder_name(app_settings.get("download_dir"))
        detail = f'Anime profile is used for files inside a "{folder}" folder.'
        if backup:
            detail += f"\nYour old config was saved to {backup}"
        InfoBar.success("Quality shaders applied", detail, duration=7000,
                        position=InfoBarPosition.TOP, parent=self)

    def open_config_folder(self):
        cfg = mpvnet.config_dir(self._exe)
        if os.path.isdir(cfg):
            os.startfile(cfg)
