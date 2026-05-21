import subprocess
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedWidget, QFrame

# THE UPGRADE: Fluent Components
from qfluentwidgets import PushButton, PrimaryPushButton, ProgressBar, SmoothScrollArea, IndeterminateProgressRing, FluentIcon as FIF

from core.signals import signals
from core.selenium_engine import active_aria2_processes, pause_event, cancel_event, finish_event, ep_pause_events, ep_cancel_events, ep_aria2_processes

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
        self.stack.addWidget(self.page_success)

        layout.addWidget(self.stack, 1)

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
        layout.addLayout(self.btn_layout)

        self.lbl_status = QLabel("Status: Waiting to start...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)

        signals.update_status.connect(self.set_status)
        signals.update_progress.connect(self.set_progress)
        signals.update_buttons.connect(self.set_buttons)
        signals.add_active_download.connect(self.add_active_card)
        signals.update_active_download.connect(self.update_active_card)
        signals.update_active_bar.connect(self.update_active_bar_ui)
        signals.remove_active_download.connect(self.remove_active_card)
        signals.task_started.connect(self.reset_ui)
        signals.task_finished.connect(self.show_success)

    def reset_ui(self):
        if hasattr(self, 'cancel_timer') and self.cancel_timer:
            self.cancel_timer.stop()
            
        for ep_num in list(self.active_cards.keys()):
            self.remove_active_card(ep_num)
        self.stack.setCurrentIndex(0) 
        self.progress.hide()
        self.lbl_prog.hide()
        self.progress.setValue(0)
        self.lbl_prog.setText("")
        stats = QLabel("Initiating...")
        stats.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        stats.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        stats.setWordWrap(False)
        stats.setMinimumWidth(200)

    def add_active_card(self, ep_num):
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
        
        self.active_tasks_layout.addWidget(card)
        self.active_cards[ep_num] = {"widget": card, "stats": stats, "pbar": pbar, "pause_btn": btn_card_pause}

    def update_active_card(self, ep_num, status_text):
        if ep_num in self.active_cards:
            self.active_cards[ep_num]["stats"].setText(status_text)
            
    def update_active_bar_ui(self, ep_num, percent):
        if ep_num in self.active_cards:
            self.active_cards[ep_num]["pbar"].setValue(percent)

    def remove_active_card(self, ep_num):
        if ep_num in self.active_cards:
            card_info = self.active_cards.pop(ep_num)
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

    def set_buttons(self, start_en, close_en, prof_en):
        self.btn_pause.setEnabled(close_en)
        self.btn_cancel.setEnabled(close_en)
        self.btn_resume.setEnabled(True)
        if start_en: 
            self.btn_resume.hide()
            self.btn_pause.show()

    def show_success(self, results=None):
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
        
        global active_aria2_processes
        for p in active_aria2_processes:
            try: p.kill() 
            except: pass
            
        subprocess.run("taskkill /F /IM aria2c.exe /T", shell=True, creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def resume_task(self):
        self.set_status("Status: ▶ Resuming downloads...", "#2ecc71")
        pause_event.clear()
        self.btn_resume.hide()
        self.btn_pause.show()

    def cancel_task(self):
        self.set_status("Status: Cancelling... Please wait.", "#e74c3c")
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        cancel_event.set()
        pause_event.clear()
        finish_event.set()
        
        global active_aria2_processes
        for p in active_aria2_processes:
            try: p.kill()
            except: pass
            
        subprocess.run("taskkill /F /IM aria2c.exe /T", shell=True, creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        from PyQt6.QtCore import QTimer
        
        if hasattr(self, 'cancel_timer') and self.cancel_timer:
            self.cancel_timer.stop()
            
        self.cancel_timer = QTimer()
        self.cancel_timer.setSingleShot(True)
        self.cancel_timer.timeout.connect(lambda: signals.task_cancelled.emit())
        self.cancel_timer.start(2000)