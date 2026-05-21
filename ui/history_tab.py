import sqlite3
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHeaderView, QTableWidgetItem
from PyQt6.QtGui import QColor

# THE UPGRADE: Fluent Components
from qfluentwidgets import TableWidget, PushButton, SubtitleLabel, MessageBox, FluentIcon as FIF

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
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Date", "Profile", "Episodes", "Status", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)

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
            try:
                conn = sqlite3.connect(DB_FILE, check_same_thread=False) 
                cursor = conn.cursor()
                cursor.execute("DELETE FROM downloads_v2") 
                conn.commit()
                conn.close()    
                self.refresh_data()
                
                success_msg = MessageBox("Success", "History cleared successfully!", self)
                success_msg.cancelButton.hide()
                success_msg.exec()
            except Exception as e:
                err_msg = MessageBox("Error", f"Failed to clear history: {e}", self)
                err_msg.cancelButton.hide()
                err_msg.exec()

    def refresh_data(self):
        self.table.setRowCount(0)
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
                conn.close()
        except Exception:
            pass