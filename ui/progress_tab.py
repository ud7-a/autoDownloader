import subprocess
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedWidget, QFrame

# THE UPGRADE: Fluent Components
from qfluentwidgets import (PushButton, PrimaryPushButton, ProgressBar, SmoothScrollArea,
                            IndeterminateProgressRing, FluentIcon as FIF, SpinBox, CheckBox,
                            InfoBar, InfoBarPosition)

from core.signals import signals
from ui.episode_grid import EpisodePicker
from core.selenium_engine import (active_aria2_processes, pause_event, cancel_event, finish_event,
                                  ep_pause_events, ep_cancel_events, ep_aria2_processes,
                                  run_snapshot, request_adjustments)

def describe_changes(before, *, limit, auto, headless, skip):
    """One line for the "Settings applied" toast: only what differs from the
    paused screen's starting state, and when each change takes effect."""
    parts = []
    if bool(auto) != bool(before.get("auto")) or (not auto and limit != before.get("limit")):
        parts.append(f"auto concurrency (starting at {limit})" if auto
                     else f"{limit} download{'s' if limit != 1 else ''} at once")
    if bool(headless) != bool(before.get("headless")):
        parts.append(f"browser {'hidden' if headless else 'visible'} from the next episode")
    if set(skip) != set(before.get("pending_skip", ())):
        parts.append(f"skipping {len(skip)} episode{'s' if len(skip) != 1 else ''}" if skip
                     else "no episodes skipped")
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


def _kill_aria2():
    """Stop every aria2c download. The known processes are killed directly; the
    taskkill sweep for strays is started but not waited on -- it used to block the
    UI thread for up to a second on every Stop/Cancel."""
    for p in list(active_aria2_processes):
        try:
            p.kill()
        except Exception:
            pass
    try:
        subprocess.Popen(["taskkill", "/F", "/IM", "aria2c.exe", "/T"],
                         creationflags=0x08000000,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# Keep your custom Checkmark for the "Success" screen!
class WinUICheckmark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(90, 90)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        painter.setBrush(QColor("#2ecc71"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())
        
        pen = QPen(QColor("white"))
        pen.setWidth(7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        
        painter.drawLine(25, 45, 40, 60)
        painter.drawLine(40, 60, 65, 30)
        painter.end()


class ProgressTab(QWidget):
    watch_requested = pyqtSignal()   # "Start Watching" pressed on the success screen

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)

        self.stack = QStackedWidget()
        
        # --- PAGE 0: Loading Spinner ---
        self.page_loading = QWidget()
        l_layout = QVBoxLayout(self.page_loading)
        l_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Fluent Progress Ring
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(60, 60)
        l_layout.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        
        lbl_wait = QLabel("Downloading the episodes...", styleSheet="color: #aaaaaa; margin-top: 15px; font-size: 16px;")
        l_layout.addWidget(lbl_wait, alignment=Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page_loading)

        # --- PAGE 1: Active Downloads ---
        self.page_active = QWidget()
        a_layout = QVBoxLayout(self.page_active)
        a_layout.setContentsMargins(0,0,0,0)
        
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # Make scroll area transparent for Mica Glass!
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        self.active_tasks_layout = QVBoxLayout(self.content)
        self.active_tasks_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.scroll.setWidget(self.content)
        a_layout.addWidget(self.scroll)
        self.stack.addWidget(self.page_active)

        # --- PAGE 2: Success Checkmark ---
        self.page_success = QWidget()
        s_layout = QVBoxLayout(self.page_success)
        s_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.checkmark = WinUICheckmark()
        s_layout.addWidget(self.checkmark, alignment=Qt.AlignmentFlag.AlignCenter)
        lbl_done = QLabel("All downloads completed!", styleSheet="color: #2ecc71; margin-top: 20px; font-size: 20px; font-weight: bold;")
        s_layout.addWidget(lbl_done, alignment=Qt.AlignmentFlag.AlignCenter)

        # Offer to play what was just downloaded. This tab stays open until the user
        # either clicks here or navigates away, so the result is never yanked away
        # before it can be acted on.
        self.btn_watch = PrimaryPushButton(FIF.PLAY, "Start Watching")
        self.btn_watch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_watch.setMinimumHeight(40)
        self.btn_watch.setFixedWidth(220)
        self.btn_watch.clicked.connect(self.watch_requested.emit)
        s_layout.addSpacing(18)
        s_layout.addWidget(self.btn_watch, alignment=Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page_success)

        layout.addWidget(self.stack, 1)
        # Same slot as the stack: while paused it is shown in the stack's place.
        self._build_paused_panel(layout)

        self.active_cards = {}

        # Fluent Progress Bar
        self.progress = ProgressBar()
        self.progress.hide()

        self.lbl_prog = QLabel("")
        self.lbl_prog.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_prog.setStyleSheet("color: #cccccc;")
        self.lbl_prog.hide()

        layout.addWidget(self.progress)
        layout.addWidget(self.lbl_prog)

        self.btn_layout = QHBoxLayout()
        
        self.btn_pause = PushButton(FIF.PAUSE, "Stop")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setMinimumHeight(40)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.pause_task)
        
        self.btn_resume = PrimaryPushButton(FIF.PLAY, "Resume")
        self.btn_resume.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_resume.setMinimumHeight(40)
        self.btn_resume.hide() 
        self.btn_resume.clicked.connect(self.resume_task)
        self.btn_cancel = PushButton(FIF.CLOSE, "Cancel downloading")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_task)

        self.btn_layout.addWidget(self.btn_pause)
        self.btn_layout.addWidget(self.btn_resume)
        self.btn_layout.addWidget(self.btn_cancel)

        # Top to bottom: downloads (swapped for the paused panel while paused),
        # progress, status, and the Stop/Resume/Cancel buttons pinned to the bottom.
        self.lbl_status = QLabel("Status: Waiting to start...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)
        layout.addSpacing(4)
        layout.addLayout(self.btn_layout)

        signals.update_status.connect(self.set_status)
        signals.update_progress.connect(self.set_progress)
        signals.update_buttons.connect(self.set_buttons)
        signals.add_active_download.connect(self.add_active_card)
        signals.update_active_download.connect(self.update_active_card)
        signals.update_active_bar.connect(self.update_active_bar_ui)
        signals.remove_active_download.connect(self.remove_active_card)
        signals.task_started.connect(self.reset_ui)
        signals.task_finished.connect(self.show_success)

    # ---- settings that can change while paused ---------------------------------
    def _build_paused_panel(self, layout):
        """Shown only while paused: concurrency, browser mode, episodes not yet begun.

        The rest of the app is locked during a download (navigation is disabled), so
        this is the one place these can be changed without cancelling. Nothing is
        applied until Resume.
        """
        panel = QFrame()
        panel.setStyleSheet("QFrame#PausedPanel { background-color: rgba(255,255,255,0.04); "
                            "border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; }"
                            "QLabel { background: transparent; border: none; }")
        panel.setObjectName("PausedPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(15, 12, 15, 12)
        v.setSpacing(8)

        head = QLabel("While paused")
        head.setStyleSheet("font-size: 15px; font-weight: bold; color: #ffffff;")
        sub = QLabel("Changes take effect when you press Resume.")
        sub.setStyleSheet("color: #999999; font-size: 12px;")
        v.addWidget(head)
        v.addWidget(sub)

        row = QHBoxLayout()
        row.addWidget(QLabel("Concurrent downloads"))
        self.spin_paused_limit = SpinBox()
        self.spin_paused_limit.setRange(1, 6)
        self.chk_paused_auto = CheckBox("Choose automatically")
        self.chk_paused_auto.toggled.connect(
            lambda on: self.spin_paused_limit.setEnabled(not on))
        row.addWidget(self.spin_paused_limit)
        row.addSpacing(10)
        row.addWidget(self.chk_paused_auto)
        row.addStretch(1)
        v.addLayout(row)

        self.chk_paused_headless = CheckBox("Run invisibly (headless)")
        self.chk_paused_headless.setToolTip(
            "The browser restarts before the next episode. Downloads already running "
            "are not affected.")
        v.addWidget(self.chk_paused_headless)

        self.lbl_paused_eps = QLabel("Episodes not started yet")
        self.lbl_paused_eps.setStyleSheet("color: #ffffff; font-weight: bold; margin-top: 4px;")
        v.addWidget(self.lbl_paused_eps)
        self.episode_picker = EpisodePicker()
        v.addWidget(self.episode_picker, 1)

        panel.hide()
        self.paused_panel = panel
        self._paused_task_id = None
        self._paused_pending = 0
        self._paused_total = 0
        layout.addWidget(panel, 1)

    def _show_paused_panel(self, on):
        """While paused the panel takes the downloads area's place (the spinner or
        the paused cards have nothing to show then); everything else comes back
        on Resume, Cancel or the next run."""
        self.paused_panel.setVisible(on)
        self.stack.setVisible(not on)

    def _fill_paused_panel(self):
        snap = run_snapshot()
        self._paused_task_id = snap.get("task_id")
        if not snap:
            self._show_paused_panel(False)
            return
        self.spin_paused_limit.setValue(int(snap.get("limit") or 1))
        self.chk_paused_auto.setChecked(bool(snap.get("auto")))
        self.spin_paused_limit.setEnabled(not snap.get("auto"))
        self.chk_paused_headless.setChecked(bool(snap.get("headless")))

        pending = snap.get("not_started", [])
        # Episodes skipped on an earlier pause that the engine hasn't reached yet
        # stay unticked -- and ticking one again un-skips it.
        already_skipped = set(snap.get("pending_skip", ()))
        self.episode_picker.set_episodes(
            pending, [e for e in pending if e not in already_skipped])
        self._paused_snap = snap
        self.lbl_paused_eps.setText(
            "Episodes not started yet" if pending
            else "Every episode has already started; there is nothing left to skip.")
        self.episode_picker.setVisible(bool(pending))
        self._paused_pending = len(pending)
        self._paused_total = snap.get("total", len(pending))
        self._show_paused_panel(True)

    def _apply_paused_panel(self):
        """Send the paused screen's choices to the engine. False keeps the task paused."""
        if self._paused_task_id is None:
            return True
        skip = self.episode_picker.skipped() if self._paused_pending else []
        if self._paused_pending and len(skip) == self._paused_pending == self._paused_total:
            InfoBar.warning("Nothing left to download",
                            "Keep at least one episode, or use \"Cancel downloading\".",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
            return False
        auto = self.chk_paused_auto.isChecked()
        limit = self.spin_paused_limit.value()
        headless = self.chk_paused_headless.isChecked()
        before = getattr(self, "_paused_snap", {}) or {}
        if not request_adjustments(self._paused_task_id, limit=limit, auto=auto,
                                   headless=headless, skip=skip):
            self._resume_note = ""          # that run already ended
            return True
        signals.paused_settings_changed.emit({"limit": limit, "auto": auto, "headless": headless})
        self._resume_note = describe_changes(before, limit=limit, auto=auto,
                                             headless=headless, skip=skip)
        return True

    def reset_ui(self):
        if hasattr(self, 'cancel_timer') and self.cancel_timer:
            self.cancel_timer.stop()
        if hasattr(self, "paused_panel"):
            self._show_paused_panel(False)
            
        for ep_num in list(self.active_cards.keys()):
            self.remove_active_card(ep_num)
        # Purge any leftover/orphan card widgets so a new download starts clean.
        while self.active_tasks_layout.count():
            item = self.active_tasks_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.active_cards.clear()
        self.stack.setCurrentIndex(0)
        self.progress.hide()
        self.lbl_prog.hide()
        self.lbl_prog.setText("")

    def add_active_card(self, ep_num):
        # Never stack a second card for the same episode -- replace the old one,
        # otherwise repeated add signals (retries/resume) leave orphan cards piling up.
        if ep_num in self.active_cards:
            self.remove_active_card(ep_num)
        if self.stack.currentIndex() != 1:
            self.stack.setCurrentIndex(1)
            
        card = QFrame()
        # Native fluent semi-transparent card look!
        card.setStyleSheet("QFrame { background-color: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; } QLabel { background: transparent; border: none; }")
        
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 10, 15, 10)
        
        header_layout = QHBoxLayout()
        title = QLabel(f"Downloading Episode {ep_num}...")
        title.setStyleSheet("font-size: 18px; font-weight: bold; background: transparent; border: none;")
        stats = QLabel("Initiating...")
        stats.setStyleSheet("color: #aaaaaa; font-size: 14px; background: transparent; border: none;")
        
        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(stats)
        
        import threading
        if ep_num not in ep_pause_events: ep_pause_events[ep_num] = threading.Event()
        if ep_num not in ep_cancel_events: ep_cancel_events[ep_num] = threading.Event()
        
        from qfluentwidgets import ToolButton, ToolTipFilter, ToolTipPosition
        btn_card_pause = ToolButton(FIF.PAUSE)
        btn_card_pause.setToolTip("Pause this episode")
        btn_card_pause.installEventFilter(ToolTipFilter(btn_card_pause, 500, ToolTipPosition.TOP))
        btn_card_pause.setFixedSize(32, 32)
        btn_card_pause.setStyleSheet("background: transparent; border: none;")
        btn_card_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        
        btn_card_cancel = ToolButton(FIF.CLOSE)
        btn_card_cancel.setToolTip("Cancel this episode")
        btn_card_cancel.installEventFilter(ToolTipFilter(btn_card_cancel, 500, ToolTipPosition.TOP))
        btn_card_cancel.setFixedSize(32, 32)
        btn_card_cancel.setStyleSheet("background: transparent; border: none;")
        btn_card_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        
        def toggle_ep_pause():
            if ep_pause_events[ep_num].is_set():
                ep_pause_events[ep_num].clear()
                btn_card_pause.setIcon(FIF.PAUSE)
                btn_card_pause.setToolTip("Pause this episode")
            else:
                ep_pause_events[ep_num].set()
                btn_card_pause.setIcon(FIF.PLAY)
                btn_card_pause.setToolTip("Resume this episode")
                # Force kill the aria2c process for this episode so it stops downloading
                # and lets the thread enter the paused state loop
                if ep_num in ep_aria2_processes:
                    try: ep_aria2_processes[ep_num].kill()
                    except: pass
                
        def cancel_ep():
            ep_cancel_events[ep_num].set()
            if ep_num in ep_aria2_processes:
                try: ep_aria2_processes[ep_num].kill()
                except: pass
            
        btn_card_pause.clicked.connect(toggle_ep_pause)
        btn_card_cancel.clicked.connect(cancel_ep)
        
        header_layout.addSpacing(10)
        header_layout.addWidget(btn_card_pause)
        header_layout.addWidget(btn_card_cancel)
        
        pbar = ProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(0)
        
        layout.addLayout(header_layout)
        layout.addWidget(pbar)
        
        # Keep the cards in episode order: after a resume the restarted episodes are
        # re-added one by one, and appending put them at the bottom in random order.
        position = sum(1 for other in self.active_cards if other < ep_num)
        self.active_tasks_layout.insertWidget(position, card)
        self.active_cards[ep_num] ={"widget": card, "stats": stats, "pbar": pbar, "pause_btn": btn_card_pause}

    def update_active_card(self, ep_num, status_text):
        if ep_num in self.active_cards:
            self.active_cards[ep_num]["stats"].setText(status_text)
            
    def update_active_bar_ui(self, ep_num, percent):
        if ep_num in self.active_cards:
            self.active_cards[ep_num]["pbar"].setValue(percent)

    def remove_active_card(self, ep_num):
        if ep_num in self.active_cards:
            card_info = self.active_cards.pop(ep_num)
            # Out of the layout now, not when deleteLater runs, so the next insert's
            # position counts only live cards.
            self.active_tasks_layout.removeWidget(card_info["widget"])
            card_info["widget"].hide()
            card_info["widget"].deleteLater()

    def set_status(self, text, color_hex):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color_hex};")

    def set_progress(self, current, total):
        self.progress.show() 
        self.lbl_prog.show() 
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self.lbl_prog.setText(f"{current} / {total} Episodes Downloaded")

    def set_buttons(self, start_en, close_en, _prof_en):
        self.btn_pause.setEnabled(close_en)
        self.btn_cancel.setEnabled(close_en)
        self.btn_resume.setEnabled(True)
        if start_en: 
            self.btn_resume.hide()
            self.btn_pause.show()

    def show_success(self, _results=None):
        self._show_paused_panel(False)
        self.stack.setCurrentIndex(2)
        self.btn_pause.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.progress.hide()
        self.lbl_prog.hide()
        self.lbl_status.setText("")

    def pause_task(self):
        self.set_status("Status: ⏸ Paused. Progress saved.", "#f39c12")
        pause_event.set()
        self.btn_pause.hide()
        self.btn_resume.show()
        _kill_aria2()
        self._fill_paused_panel()

    def resume_task(self):
        # Hand over what was changed while paused first; the engine only continues
        # once pause_event clears, so nothing can start under the old settings.
        self._resume_note = ""
        if not self._apply_paused_panel():
            return
        self._show_paused_panel(False)
        self.set_status("Status: ▶ Resuming downloads...", "#2ecc71")
        pause_event.clear()
        if self._resume_note:
            # The status line is overwritten by the engine within a second, so the
            # confirmation of what changed goes in a toast that stays a few seconds.
            InfoBar.success("Settings applied", self._resume_note,
                            position=InfoBarPosition.TOP, duration=6000, parent=self.window())
        self.btn_resume.hide()
        self.btn_pause.show()

    def cancel_task(self):
        self._show_paused_panel(False)
        self.set_status("Status: Cancelling... Please wait.", "#e74c3c")
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        cancel_event.set()
        pause_event.clear()
        finish_event.set()
        
        # Clear unfinished session recovery on explicit user cancellation
        from utils.config import app_settings, save_config, config_lock
        with config_lock:
            app_settings.pop("unfinished_session", None)
            save_config()

        _kill_aria2()

        from PyQt6.QtCore import QTimer
        
        if hasattr(self, 'cancel_timer') and self.cancel_timer:
            self.cancel_timer.stop()
            
        self.cancel_timer = QTimer()
        self.cancel_timer.setSingleShot(True)
        self.cancel_timer.timeout.connect(lambda: signals.task_cancelled.emit())
        self.cancel_timer.start(2000)