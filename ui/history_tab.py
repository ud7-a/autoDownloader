import sqlite3
import os
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHeaderView, QTableWidgetItem
from PyQt6.QtGui import QColor

# THE UPGRADE: Fluent Components
from qfluentwidgets import TableWidget, PushButton, SubtitleLabel, MessageBox, FluentIcon as FIF, InfoBar, InfoBarPosition

from utils.config import DB_FILE
from utils.database import db_lock
from core.signals import signals

class HistoryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)

        # Fluent Subtitle Label
        header_lbl = SubtitleLabel("Download History")
        layout.addWidget(header_lbl)

        # Fluent TableWidget (Natively supports Dark Mode and rounded corners!)
        self.table = TableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Date", "Profile", "Episodes", "Status", "Notes", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        
        # Enable double click to play/preview
        self.table.cellDoubleClicked.connect(lambda row, col: self.play_download(row))

        # Fluent PushButton with styled danger design
        self.btn_clear_history = PushButton(FIF.DELETE, "Clear History")
        self.btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_history.setObjectName("Danger")
        self.btn_clear_history.setMinimumHeight(40)
        self.btn_clear_history.clicked.connect(self.clear_history)
        
        layout.addWidget(self.btn_clear_history)
        layout.addWidget(self.table)
        
        signals.history_updated.connect(self.refresh_data)
        self._initial_load_done = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_load_done:
            self._initial_load_done = True
            self.refresh_data()
    def clear_history(self):
        # Fluent Native MessageBox (Gorgeous animated popup)
        msg = MessageBox("Clear History", "Are you sure you want to permanently delete your download history?", self)
        
        if msg.exec():
            conn = None
            try:
                with db_lock:
                    conn = sqlite3.connect(DB_FILE, check_same_thread=False) 
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM downloads_v2") 
                    conn.commit()
                self.refresh_data()
                
                success_msg = MessageBox("Success", "History cleared successfully!", self)
                success_msg.cancelButton.hide()
                success_msg.exec()
            except Exception as e:
                err_msg = MessageBox("Error", f"Failed to clear history: {e}", self)
                err_msg.cancelButton.hide()
                err_msg.exec()
            finally:
                if conn:
                    try: conn.close()
                    except: pass

    def refresh_data(self):
        self.table.setRowCount(0)
        conn = None
        try:
            with db_lock:
                conn = sqlite3.connect(DB_FILE, check_same_thread=False)
                c = conn.cursor()
                c.execute("SELECT date, profile, episodes, status, notes FROM downloads_v2 ORDER BY id DESC")
                rows = c.fetchall()
                for row_idx, row_data in enumerate(rows):
                    self.table.insertRow(row_idx)
                    for col_idx, item_data in enumerate(row_data):
                        item = QTableWidgetItem(str(item_data))
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        
                        if col_idx == 3: 
                            if item_data == "Success": item.setForeground(QColor("#2ecc71"))
                            elif item_data == "Failed": item.setForeground(QColor("#e74c3c"))
                            elif item_data == "Partial": item.setForeground(QColor("#f39c12"))
                            elif item_data == "Cancelled": item.setForeground(QColor("#aaaaaa"))
                            
                        self.table.setItem(row_idx, col_idx, item)
                        
                    # Action button
                    play_btn = PushButton(FIF.PLAY, "Start Watching")
                    play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    play_btn.setFixedHeight(28)
                    play_btn.clicked.connect(lambda checked, r=row_idx: self.play_download(r))
                    self.table.setCellWidget(row_idx, 5, play_btn)
        except Exception:
            pass
        finally:
            if conn:
                try: conn.close()
                except: pass

    def play_download(self, row):
        profile_item = self.table.item(row, 1)
        if not profile_item: return
        profile_name = profile_item.text()
        
        # Parse first episode in the range from Column 2 ("Episodes")
        episodes_item = self.table.item(row, 2)
        first_ep = None
        if episodes_item:
            ep_txt = episodes_item.text().strip()
            if "-" in ep_txt:
                parts = ep_txt.split("-")
                try: first_ep = int(parts[0].strip())
                except ValueError: pass
            else:
                try: first_ep = int(ep_txt)
                except ValueError: pass

        # Sanitize profile name
        safe_site_name = "".join(c for c in profile_name if c not in r'\/:*?"<>|').strip()
        
        from utils.config import app_settings
        download_dir = app_settings.get("download_dir", "")
        folder_path = os.path.join(download_dir, safe_site_name)
        
        if not os.path.exists(folder_path):
            InfoBar.warning(
                title="Folder Not Found",
                content=f"The download folder for '{profile_name}' does not exist or was deleted.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            return
            
        # Find video files inside the folder
        VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts')
        videos = []
        try:
            for f in os.listdir(folder_path):
                if f.lower().endswith(VIDEO_EXTENSIONS):
                    videos.append(os.path.join(folder_path, f))
        except Exception:
            pass
                
        if videos:
            import re
            # Natural sort by filename so that Ep2 comes before Ep10
            def natural_sort_key(s):
                return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
            
            videos.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
            
            # Attempt to find a direct match for the target first episode
            selected_video = None
            if first_ep is not None:
                pattern = rf"ep{first_ep}(\D|$)"
                for v in videos:
                    filename = os.path.basename(v).lower()
                    if re.search(pattern, filename):
                        selected_video = v
                        break
            
            # If first_ep was somehow not parsed, default to the first sorted video, else require it
            if not selected_video and first_ep is None:
                selected_video = videos[0]
                
            if selected_video:
                try:
                    os.startfile(selected_video)
                except Exception as e:
                    InfoBar.warning(
                        title="Playback Error",
                        content=f"Failed to play the video: {e}",
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=4000,
                        parent=self
                    )
            else:
                InfoBar.warning(
                    title="Episode Not Found",
                    content=f"Episode {first_ep} could not be found in the download folder.",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self
                )
        else:
            try:
                os.startfile(folder_path)
            except Exception as e:
                InfoBar.warning(
                    title="Open Folder Error",
                    content=f"Failed to open directory: {e}",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=4000,
                    parent=self
                )