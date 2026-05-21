import os
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
from utils.config import APP_DIR

def generate_ui_icons():
    os.makedirs(APP_DIR, exist_ok=True)
    check_path = os.path.join(APP_DIR, "ui_check.png")
    arrow_path = os.path.join(APP_DIR, "ui_arrow.png")

    if not os.path.exists(check_path):
        pix = QPixmap(16, 16)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("black"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(3, 8, 7, 12)
        painter.drawLine(7, 12, 13, 4)
        painter.end()
        pix.save(check_path, "PNG")

    if not os.path.exists(arrow_path):
        pix = QPixmap(16, 16)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#aaaaaa"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(4, 6, 8, 10)
        painter.drawLine(8, 10, 12, 6)
        painter.end()
        pix.save(arrow_path, "PNG")

    return check_path.replace("\\", "/"), arrow_path.replace("\\", "/")

WIN11_QSS = """
/* 1. Safely style text without poisoning the ComboBox containers */
QLabel { background: transparent; color: #ffffff; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }


/* 3. Your custom volume controls */
QPushButton#MuteButton { background-color: transparent; border: none; font-size: 20px; padding: 0px; color: #ffffff; }
QPushButton#MuteButton:hover { color: #4cc2ff; }
QLineEdit#VolumeText { background-color: transparent; border: none; border-radius: 4px; padding: 0px 4px; color: #aaaaaa; font-weight: bold; font-size: 14px; }
QLineEdit#VolumeText:hover { background-color: #2b2b2b; color: #ffffff; }
QLineEdit#VolumeText:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; color: #4cc2ff; }

/* 4. Your Checkboxes */
QCheckBox { spacing: 10px; color: #ffffff; font-weight: 500; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 1px solid #888888; background-color: rgba(255, 255, 255, 0.05); }
QCheckBox::indicator:hover { border: 1px solid #aaaaaa; background-color: rgba(255, 255, 255, 0.1); }
QCheckBox::indicator:checked { background-color: #4cc2ff; border: 1px solid #4cc2ff; image: url("ICON_CHECK"); }

/* 5. Your History Tab Tables */
QTableWidget { background-color: #202020; color: #ffffff; border: 1px solid #333333; gridline-color: #333333; border-radius: 6px; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QHeaderView::section { background-color: #2b2b2b; color: #aaaaaa; padding: 8px; border: none; border-bottom: 1px solid #444444; border-right: 1px solid #333333; font-weight: bold; }
QTableWidget::item { padding: 5px; border-bottom: 1px solid #2b2b2b; }

/* 6. Profile Manager Path Tabs */
QTabWidget#PathTabs::pane { border: none; background-color: transparent; margin-top: 5px; }
QTabBar#PathTabBar::tab { background: rgba(255, 255, 255, 0.05); color: #aaaaaa; padding: 6px 12px; margin-right: 6px; border-radius: 6px; font-size: 13px; font-weight: bold; border: 1px solid rgba(255, 255, 255, 0.05); }
QTabBar#PathTabBar::tab:selected { background: rgba(255, 255, 255, 0.15); color: #ffffff; border: 1px solid rgba(255, 255, 255, 0.2); }
QTabBar#PathTabBar::tab:hover:!selected { background: rgba(255, 255, 255, 0.1); color: #ffffff; }

QPushButton#TabDots { background-color: transparent; color: #aaaaaa; border: none; font-size: 16px; font-weight: bold; padding: 0px; margin: 0px; margin-left: 5px; }
QPushButton#TabDots:hover { color: #ffffff; background-color: rgba(255, 255, 255, 0.1); border-radius: 4px; }
QPushButton#TabDotsSelected { background-color: transparent; color: #ffffff; border: none; font-weight: bold; padding: 0px; margin: 0px; font-size: 16px; margin-left: 5px; }

QFrame#Card { background-color: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; }


/* WinUI 3 Premium Critical Destructive Buttons */
#Danger {
    background-color: #ff4d4d; /* Premium Vibrant WinUI 3 Destructive Red */
    color: #ffffff;
    border: 1px solid #ff4d4d;
    border-radius: 6px;
    font-weight: bold;
}
#Danger:hover {
    background-color: #ff6666; /* Vibrant hover red */
    border: 1px solid #ff6666;
    color: #ffffff;
}
#Danger:pressed {
    background-color: #d93838; /* Pressed deep red */
    border: 1px solid #d93838;
    color: rgba(255, 255, 255, 0.9);
}
#Danger:disabled {
    background-color: rgba(255, 77, 77, 0.2);
    color: rgba(255, 255, 255, 0.3);
    border: 1px solid rgba(255, 77, 77, 0.2);
}

#DeleteStep { background-color: transparent; color: #ff5c5c; border: none; font-size: 18px; font-weight: bold; border-radius: 6px; padding: 0px; }
#DeleteStep:hover { background-color: rgba(255, 92, 92, 0.15); }

/* 8. Text Input Fields */
QLineEdit { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px; color: white; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QLineEdit:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; border-bottom: 2px solid #4cc2ff; }

/* 9. Progress Bars */
QProgressBar { border: 1px solid #444444; border-radius: 4px; background-color: #2b2b2b; text-align: center; color: transparent; height: 6px; }
QProgressBar::chunk { background-color: #4cc2ff; border-radius: 3px; }

/* 10. Scrollbars - Set to transparent so they don't break Fluent's Dark Mode background */
QScrollArea#StepScroll, QWidget#StepScrollContent { border: none; background-color: transparent; }
QScrollBar:vertical { border: none; background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #555555; min-height: 20px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
QScrollBar:horizontal { border: none; background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle:horizontal { background: #555555; min-width: 20px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { border: none; background: none; }

/* 11. Custom Standard ComboBox */
QComboBox { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px 12px; min-height: 20px; color: #ffffff; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QComboBox:hover { background-color: #333333; }
QComboBox:on { border-bottom: 2px solid #4cc2ff; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 30px; border-left: 1px solid #3a3a3a; }
QComboBox::down-arrow { image: url("ICON_ARROW"); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background-color: #2c2c2c; border: 1px solid #444444; border-radius: 8px; outline: none; padding: 4px; }
QComboBox QAbstractItemView::item { background-color: transparent; padding: 8px 12px; border-radius: 4px; min-height: 24px; color: #ffffff; border-left: 3px solid transparent; }
QComboBox QAbstractItemView::item:hover { background-color: #3a3a3a; }
QComboBox QAbstractItemView::item:selected { background-color: #444444; border-left: 3px solid #4cc2ff; color: #ffffff; }
"""