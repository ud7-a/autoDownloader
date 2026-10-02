import os
import subprocess

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel
from qfluentwidgets import (CardWidget, IconWidget, SubtitleLabel, StrongBodyLabel,
                            BodyLabel, CaptionLabel, PrimaryPushButton, PushButton,
                            IndeterminateProgressBar, InfoBar, InfoBarPosition,
                            ScrollArea, FluentIcon as FIF)

from utils import mpvnet
from utils.config import app_settings

# Shown per shader in the profile panels: (name, what it does). Unknown shaders
# fall back to their file name, so editing the bundled mpv.conf never breaks the tab.
SHADER_INFO = {
    "Anime4K_Clamp_Highlights": ("Clamp highlights", "stops halos around edges"),
    "Anime4K_Restore_CNN_L": ("Restore", "removes blur and compression"),
    "Anime4K_Upscale_CNN_x2_L": ("Upscale 2×", "sharper line art"),
    "Anime4K_Thin_HQ": ("Thin lines", "crisper outlines"),
    "FSRCNNX_x2_16-0-4-1": ("FSRCNNX 2×", "detail-preserving upscale"),
    "KrigBilateral": ("KrigBilateral", "sharper colour edges"),
    "SSimDownscaler-PK": ("SSim downscaler", "clean fit to your screen"),
    "adaptive-sharpen": ("Adaptive sharpen", "brings out fine detail"),
}
PROFILE_TITLES = {"anime": "Anime", "series": "Series & movies"}
SHORTCUTS = (("F1", "Anime profile"), ("F2", "Series profile"),
             ("Ctrl+1", "Shaders off / on"), ("P", "Show active shaders"))

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
    return label


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
    line = pyqtSignal(str)
    done = pyqtSignal(int)

    def run(self):
        try:
            code = mpvnet.run_winget_install(self.line.emit)
        except Exception as e:
            self.line.emit(f"winget could not start: {e}")
            code = -1
        self.done.emit(code)


class PlayerWidget(QWidget):
    """Video Player tab: install mpv.net through winget and apply the bundled
    quality-shader config (Anime4K for anime, FSRCNNX for series)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exe = None
        self._install_thread = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
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
        self.btn_install.clicked.connect(self.install_mpvnet)
        self.btn_open_player.clicked.connect(self.open_player)
        self.btn_recheck.clicked.connect(self.refresh_status)
        body.addLayout(self._button_row(self.btn_recheck, self.btn_open_player, self.btn_install))
        return card

    def _build_shader_card(self):
        card, body, self.shader_pill, _ = self._card(
            FIF.PALETTE, "Quality shaders",
            "mpv.net picks the right profile for each file automatically")

        # Profiles: what runs, and on which files.
        body.addWidget(section_label("Profiles"))
        panels = QHBoxLayout()
        panels.setSpacing(12)
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
            keys.addWidget(cap, row, col * 2, Qt.AlignmentFlag.AlignLeft)
            keys.addWidget(BodyLabel(action), row, col * 2 + 1)
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
            "Needs a dedicated GPU. Your current config is backed up first.")), 1)

        self.btn_open_config = PushButton(FIF.FOLDER, "Open config folder")
        self.btn_apply = PrimaryPushButton(FIF.BRUSH, "Apply shaders")
        self.btn_open_config.clicked.connect(self.open_config_folder)
        self.btn_apply.clicked.connect(self.apply_shaders)
        body.addLayout(self._button_row(self.btn_open_config, self.btn_apply, leading=note))
        return card

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
            row.addWidget(num)
            label = BodyLabel(title)
            label.setToolTip(f"{stem}.glsl")
            row.addWidget(label)
            row.addStretch(1)
            row.addWidget(muted(CaptionLabel(purpose)))
            box.addLayout(row)
        box.addStretch(1)
        return panel

    def showEvent(self, event):
        super().showEvent(event)
        if self._install_thread is None:
            self.refresh_status()

    # ------------------------------------------------------------ status

    def refresh_status(self):
        exe, version = mpvnet.find_mpvnet()
        self._exe = exe
        installing = self._install_thread is not None
        has_winget = bool(mpvnet.winget_path())

        if exe:
            self.player_pill.set_state("ok", f"Installed {version}" if version else "Installed")
            self.lbl_player_path.setText(exe)
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
        self.btn_open_player.setVisible(bool(exe))

        folder = mpvnet.anime_folder_name(app_settings.get("download_dir"))
        for name, label in self._profile_when.items():
            label.setText(f'Files inside a "{folder}" folder' if name == "anime"
                          else "Everything else")

        cfg = mpvnet.config_dir(exe)
        applied = mpvnet.shaders_applied(cfg)
        self.shader_pill.set_state("ok" if applied else "off",
                                   "Applied" if applied else "Not applied")
        self.btn_apply.setText("Re-apply shaders" if applied else "Apply shaders")
        self.btn_apply.setEnabled(bool(exe))
        self.btn_apply.setToolTip("" if exe else "Install mpv.net first")
        self.btn_open_config.setEnabled(os.path.isdir(cfg))

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
