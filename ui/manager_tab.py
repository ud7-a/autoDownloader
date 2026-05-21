import os
import json
import re
import threading
from urllib.parse import unquote
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QDoubleValidator, QCursor, QIcon
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QFrame, QTabWidget, QFileDialog, QTabBar, QApplication, QPushButton)

# THE UPGRADE: We removed FluentIcon and will use standard Emojis for the menu!
from qfluentwidgets import (PushButton, PrimaryPushButton, LineEdit, ComboBox, 
                            SmoothScrollArea, MessageBoxBase, SubtitleLabel, MessageBox,
                            RoundMenu, Action, FluentIcon as FIF, ToolButton, TransparentToolButton)

from utils.config import sites_data, config_lock, save_config
from core.signals import signals
from core.smart_picker import launch_path_picker


# --- NATIVE FLUENT DIALOGS ---

class FluentInputDialog(MessageBoxBase):
    def __init__(self, title, label_text, default_text="", parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        
        self.inputEdit = LineEdit(self)
        self.inputEdit.setText(default_text)
        self.inputEdit.setClearButtonEnabled(True)
        
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(QLabel(label_text, styleSheet="margin-top: 5px; color: #aaaaaa; background: transparent;"))
        self.viewLayout.addWidget(self.inputEdit)
        
        self.widget.setMinimumWidth(350)
        self.yesButton.setText("Confirm")
        self.result_text = ""
        
    def validate(self):
        self.result_text = self.inputEdit.text().strip()
        return bool(self.result_text)

class DuplicateProfileDialog(MessageBoxBase):
    def __init__(self, parent, orig_name, orig_data):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("Duplicate Profile", self)
        
        self.orig_data = json.loads(json.dumps(orig_data)) 
        self.new_name = None
        self.new_data = None
        
        self.name_entry = LineEdit()
        self.name_entry.setText(orig_name + " (Copy)")
        
        self.url_entry = LineEdit()
        self.url_entry.setText(self.orig_data.get("url", ""))
        self.url_entry.textChanged.connect(self.auto_decode)
        
        btn_layout = QHBoxLayout()
        btn_copy = PushButton("Copy {x}")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText("{x}"))
        btn_fmt = PushButton("Auto-Format URL")
        btn_fmt.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_fmt.clicked.connect(self.format_url)
        btn_layout.addWidget(btn_copy)
        btn_layout.addWidget(btn_fmt)
        
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(QLabel("New Profile Name:", styleSheet="margin-top: 10px; font-weight: bold; background: transparent;"))
        self.viewLayout.addWidget(self.name_entry)
        self.viewLayout.addWidget(QLabel("New Base URL:", styleSheet="margin-top: 10px; font-weight: bold; background: transparent;"))
        self.viewLayout.addWidget(self.url_entry)
        self.viewLayout.addLayout(btn_layout)
        
        self.widget.setMinimumWidth(400)
        self.yesButton.setText("Save Duplicate")

    def auto_decode(self, text):
        decoded = unquote(text)
        if text != decoded: 
            pos = self.url_entry.cursorPosition()
            self.url_entry.setText(decoded)
            self.url_entry.setCursorPosition(max(0, pos - (len(text) - len(decoded))))
        
    def format_url(self):
        current = self.url_entry.text().strip()
        if current.endswith('/'): current = current[:-1]
        self.url_entry.setText(re.sub(r'\d+$', '{x}', current))
        
    def validate(self):
        n = self.name_entry.text().strip()
        if not n: return False
        self.new_name = n
        self.orig_data["url"] = self.url_entry.text().strip()
        self.new_data = self.orig_data
        return True


# --- MAIN WIDGET ---

class PathTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.step_widgets = [] 
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        
        self.btn_start_picker = PushButton(FIF.GLOBE, "Start Smart Browser Picker")
        self.btn_start_picker.setFixedHeight(40)
        self.btn_start_picker.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.btn_start_picker)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        self.s_layout = QVBoxLayout(self.content)
        self.s_layout.setContentsMargins(0, 0, 10, 0)
        self.s_layout.addStretch()
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)


class SiteManagerWidget(QWidget):
    from PyQt6.QtCore import pyqtSignal
    profile_saved_signal = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.original_profile_name = None 
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        
        self.content = QWidget()
        self.content.setStyleSheet("QWidget { background: transparent; }")
        
        layout = QVBoxLayout(self.content)
        layout.setSpacing(15)
        layout.setContentsMargins(30, 30, 30, 30)

        profile_sel_layout = QHBoxLayout()
        profile_sel_layout.addWidget(QLabel("Load Profile:", styleSheet="font-weight: bold; background: transparent;"))
        
        # 1. REVERTED FIRST DROPDOWN DESIGN
        self.profile_combo = ComboBox()
        # (Removed custom glass styling so it defaults back)
        
        with config_lock:
            self.profile_combo.addItems(list(sites_data.keys()))
        self.profile_combo.currentTextChanged.connect(self.load_profile)
        profile_sel_layout.addWidget(self.profile_combo, 1)
        
        btn_new_profile = PushButton(FIF.ADD, "New Profile")
        btn_new_profile.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new_profile.clicked.connect(lambda: self.load_profile("New Profile"))
        profile_sel_layout.addWidget(btn_new_profile)
        
        self.btn_dup = PushButton(FIF.COPY, "Duplicate")
        self.btn_dup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dup.clicked.connect(self.duplicate_profile)
        profile_sel_layout.addWidget(self.btn_dup)
        
        btn_import = PushButton(FIF.DOWNLOAD, "Import")
        btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_import.clicked.connect(self.import_profile)
        profile_sel_layout.addWidget(btn_import)
        
        btn_export = PushButton(FIF.SHARE, "Export")
        btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_export.clicked.connect(self.export_profile)
        profile_sel_layout.addWidget(btn_export)
        
        layout.addLayout(profile_sel_layout)

        layout.addWidget(QLabel("Profile Name:", styleSheet="font-weight: bold; margin-top: 5px; background: transparent;"))
        self.name_entry = LineEdit()
        layout.addWidget(self.name_entry)

        layout.addWidget(QLabel("Base URL:", styleSheet="font-weight: bold; margin-top: 5px; background: transparent;"))
        self.url_entry = LineEdit()
        self.url_entry.textChanged.connect(self.auto_decode_url) 
        layout.addWidget(self.url_entry)

        tool_layout = QHBoxLayout()
        btn_copy = PushButton("Copy {x}")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(self.copy_x)
        
        btn_fmt = PushButton("Auto-Format URL")
        btn_fmt.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_fmt.clicked.connect(self.format_url)
        
        tool_layout.addWidget(btn_copy)
        tool_layout.addWidget(btn_fmt)
        tool_layout.addStretch(1)
        layout.addLayout(tool_layout)

        layout.addWidget(QLabel("Next Episode Button Text (or XPath):", styleSheet="font-weight: bold; margin-top: 5px; background: transparent;"))
        self.next_entry = LineEdit()
        layout.addWidget(self.next_entry)

        step_header = QHBoxLayout()
        step_header.addWidget(QLabel("Automation Paths:", styleSheet="font-weight: bold; margin-top: 15px; background: transparent;"))
        
        btn_add_path = PushButton(FIF.ADD, "New Path")
        btn_add_path.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add_path.clicked.connect(lambda: self.add_new_path())
        
        btn_add_step = PushButton(FIF.ADD, "Add Step")
        btn_add_step.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add_step.clicked.connect(self.add_step_to_current)
        
        step_header.addStretch()
        step_header.addWidget(btn_add_path)
        step_header.addWidget(btn_add_step)
        layout.addLayout(step_header)

        self.path_tabs = QTabWidget()
        self.path_tabs.setTabsClosable(True)
        self.path_tabs.tabCloseRequested.connect(self.show_tab_menu)
        self.path_tabs.currentChanged.connect(self.update_tab_button_styles)
        
        self.path_tabs.setStyleSheet("""
            QTabWidget::pane { 
                border: none; 
                background: transparent; 
            }
            QTabBar::tab { 
                background: rgba(255, 255, 255, 0.05); 
                color: #aaaaaa; 
                padding: 8px 30px 8px 16px; 
                margin-top: 5px;
                margin-bottom: 5px;
                margin-right: 6px; 
                border-radius: 16px; 
                font-weight: bold; 
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif; 
                font-size: 13px; 
            }
            QTabBar::tab:selected { 
                background: #4cc2ff; 
                color: #000000; 
            }
            QTabBar::tab:hover:!selected { 
                background: rgba(255, 255, 255, 0.1); 
                color: #ffffff; 
            }
        """)
        layout.addWidget(self.path_tabs, 1)

        btn_layout = QHBoxLayout()
        
        # 2. UPGRADED DELETE BUTTON COLORING & DESIGN
        self.btn_del = PushButton(FIF.DELETE, "Delete Profile")
        self.btn_del.setObjectName("Danger")
        self.btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_del.setMinimumHeight(40)
        self.btn_del.clicked.connect(self.delete_profile)
        
        btn_save = PrimaryPushButton("Save Profile")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self.save_profile)
        
        btn_layout.addWidget(self.btn_del)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

        self.scroll.setWidget(self.content)
        outer_layout.addWidget(self.scroll)
        signals.add_picked_step.connect(lambda t, x: self.add_step_to_tab(t, x, "5.0"))

        self._initial_load_done = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_load_done:
            self._initial_load_done = True
            from utils.config import sites_data, config_lock
            with config_lock:
                has_sites = len(sites_data) > 0
                first_site = list(sites_data.keys())[0] if has_sites else "New Profile"
                
            if has_sites: self.load_profile(first_site)
            else: self.load_profile("New Profile")

    def import_profile(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Profile", "", "JSON Files (*.json)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f: data = json.load(f)
                default_name = os.path.splitext(os.path.basename(file_path))[0]
                dlg = FluentInputDialog("Import Profile", "Enter a name for this profile:", default_name, self)
                if dlg.exec():
                    new_name = dlg.result_text
                    with config_lock:
                        sites_data[new_name] = data
                    save_config()
                    self.refresh_combo(new_name)
                    self.profile_saved_signal.emit()
            except Exception as e:
                err = MessageBox("Error", f"Failed to import profile:\n{e}", self)
                err.exec()

    def export_profile(self):
        current_profile = self.profile_combo.currentText()
        with config_lock:
            if current_profile and current_profile in sites_data:
                file_path, _ = QFileDialog.getSaveFileName(self, "Export Profile", f"{current_profile}.json", "JSON Files (*.json)")
                if file_path:
                    try:
                        with open(file_path, "w", encoding="utf-8") as f:
                            json.dump(sites_data[current_profile], f, indent=4, ensure_ascii=False)
                        msg = MessageBox("Success", "Profile exported successfully!", self)
                        msg.exec()
                    except Exception as e:
                        err = MessageBox("Error", f"Failed to export profile:\n{e}", self)
                        err.exec()

    def auto_decode_url(self, text):
        decoded = unquote(text)
        if text != decoded: 
            pos = self.url_entry.cursorPosition()
            self.url_entry.setText(decoded)
            self.url_entry.setCursorPosition(max(0, pos - (len(text) - len(decoded))))
        
    def copy_x(self):
        QApplication.clipboard().setText("{x}")
        
    def format_url(self):
        current = self.url_entry.text().strip()
        if current.endswith('/'): current = current[:-1]
        self.url_entry.setText(re.sub(r'\d+$', '{x}', current))

    def add_new_path(self, name=None, steps=None, switch_to=True):
        path_name = name if name else f"Path {self.path_tabs.count() + 1}"
        tab = PathTab()
        self.path_tabs.addTab(tab, path_name)

        url = self.url_entry.text().strip()
        tab.btn_start_picker.clicked.connect(lambda checked=False, t=tab, u=url: threading.Thread(target=launch_path_picker, args=(t, u), daemon=True).start())
        if switch_to:
            self.path_tabs.setCurrentWidget(tab)
            
        # --- BRIGHTENED & ENLARGED 3-DOTS BUTTON ---
        from PyQt6.QtWidgets import QToolButton
        btn = QToolButton(self.path_tabs.tabBar())
        btn.setFixedSize(24, 24)
        btn.setIconSize(QSize(14, 14))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda checked=False, t=tab: self.show_tab_menu(self.path_tabs.indexOf(t)))
        btn.show()
        
        self.path_tabs.tabBar().setTabButton(self.path_tabs.indexOf(tab), QTabBar.ButtonPosition.RightSide, btn)
        self.update_tab_button_styles()
            
        if steps:
            for step in steps:
                self.add_step_to_tab(tab, step.get("xpath", ""), str(step.get("delay", 5.0)))
        elif not name:
            self.add_step_to_tab(tab, "", "5.0")

    def add_step_to_current(self):
        tab = self.path_tabs.currentWidget()
        if tab: self.add_step_to_tab(tab, "", "5.0")

    def add_step_to_tab(self, tab, xp, dl):
        card = QFrame()
        card.setFixedHeight(60)
        card.setMinimumWidth(440)
        card.setStyleSheet("QFrame { background-color: rgba(255, 255, 255, 0.04);font-size: 14px; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; } QLabel { background: transparent; }")
        c_layout = QHBoxLayout(card)
        c_layout.setContentsMargins(12, 8, 12, 8)
        c_layout.setSpacing(12)
        
        xp_in = LineEdit()
        xp_in.setText(xp)
        xp_in.setFixedHeight(40)
        xp_in.setPlaceholderText("Button Text (or XPath)")

        dl_in = LineEdit()
        dl_in.setText(str(dl))
        dl_in.setValidator(QDoubleValidator())
        dl_in.setFixedHeight(40)
        dl_in.setFixedWidth(60) 
        
        btn_del = ToolButton(FIF.DELETE, self)
        btn_del.setObjectName("Danger")
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setFixedSize(40, 40)
        c_layout.addWidget(xp_in, 1)
        c_layout.addWidget(QLabel("Cooldown (sec):", styleSheet="font-size: 15px; background: transparent; border: none;"))
        c_layout.addWidget(dl_in)
        c_layout.addWidget(btn_del)
        
        tab.s_layout.insertWidget(tab.s_layout.count() - 1, card)
        obj = {"card": card, "xpath": xp_in, "delay": dl_in}
        tab.step_widgets.append(obj)
        btn_del.clicked.connect(lambda: self.remove_step(tab, obj))

    def remove_step(self, tab, obj):
        obj["card"].deleteLater()
        tab.step_widgets.remove(obj)

    def show_tab_menu(self, index):
        from PyQt6.QtGui import QAction
        menu = RoundMenu(parent=self)
        
        rename_action = QAction("✏️ Rename", menu)
        delete_action = QAction("🗑️ Delete", menu)
        
        # 1. Define exactly what happens when they are clicked
        def do_rename():
            dlg = FluentInputDialog("Rename Path", "Enter new path name:", self.path_tabs.tabText(index), self.window())
            if dlg.exec():
                new_name = dlg.result_text
                self.path_tabs.setTabText(index, new_name)
                
        def do_delete():
            widget = self.path_tabs.widget(index)
            self.path_tabs.removeTab(index)
            widget.deleteLater()
            
        # 2. Wire the actions directly to the functions (Bypassing the menu bug!)
        rename_action.triggered.connect(do_rename)
        delete_action.triggered.connect(do_delete)
        
        # 3. Add them to the menu and show it
        menu.addAction(rename_action)
        menu.addAction(delete_action)
        
        menu.exec(QCursor.pos())

    def update_tab_button_styles(self, *args):
        import os
        base_dir = os.path.dirname(os.path.dirname(__file__))
        light_icon = os.path.join(base_dir, "assets", "more_light.svg")
        dark_icon = os.path.join(base_dir, "assets", "more_dark.svg")
        
        current_index = self.path_tabs.currentIndex()
        for i in range(self.path_tabs.count()):
            btn = self.path_tabs.tabBar().tabButton(i, QTabBar.ButtonPosition.RightSide)
            if btn:
                # Sync fixed size to 30x24 so background is 24x24 with 6px right margin
                btn.setFixedSize(30, 24)
                if i == current_index:
                    # Selected tab style: Black SVG icon on bright cyan background, aligned perfectly with margins
                    btn.setIcon(QIcon(dark_icon))
                    btn.setIconSize(QSize(14, 14))
                    btn.setStyleSheet("""
                        QToolButton {
                            background: transparent;
                            border: 1px solid transparent !important;
                            border-radius: 12px !important;
                            padding: 0px;
                            margin: 0px;
                            margin-right: 6px;
                        }
                        QToolButton:hover {
                            background-color: rgba(0, 0, 0, 0.06) !important;
                        }
                        QToolButton:pressed {
                            background-color: rgba(0, 0, 0, 0.12) !important;
                        }
                    """)
                else:
                    # Unselected tab style: Pure white SVG icon on dark background, aligned perfectly with margins
                    btn.setIcon(QIcon(light_icon))
                    btn.setIconSize(QSize(14, 14))
                    btn.setStyleSheet("""
                        QToolButton {
                            background: transparent;
                            border: 1px solid transparent !important;
                            border-radius: 12px !important;
                            padding: 0px;
                            margin: 0px;
                            margin-right: 6px;
                        }
                        QToolButton:hover {
                            background-color: rgba(255, 255, 255, 0.08) !important;
                        }
                        QToolButton:pressed {
                            background-color: rgba(255, 255, 255, 0.12) !important;
                        }
                    """)

    def load_profile(self, choice):
        if not choice: return 
        
        current_path_name = None
        if self.path_tabs.count() > 0:
            current_path_name = self.path_tabs.tabText(self.path_tabs.currentIndex())
            
        while self.path_tabs.count() > 0:
            widget = self.path_tabs.widget(0)
            self.path_tabs.removeTab(0)
            widget.deleteLater()
        
        if choice == "New Profile":
            self.btn_del.hide()
            self.btn_dup.hide()
            self.original_profile_name = None
            self.name_entry.clear()
            self.url_entry.clear()
            self.next_entry.clear()
            self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentIndex(-1)
            self.profile_combo.blockSignals(False)
            self.add_new_path("Path 1", switch_to=True)
        else:
            self.btn_del.show()
            self.btn_dup.show()
            self.original_profile_name = choice
            with config_lock:
                data = sites_data.get(choice, {})
            self.name_entry.setText(choice)
            self.url_entry.setText(data.get("url", ""))
            self.next_entry.setText(data.get("next_btn_xpath", ""))
            
            step_paths = data.get("step_paths", {})
            for path_name, steps in step_paths.items():
                self.add_new_path(path_name, steps, switch_to=False)
                
            if not step_paths:
                self.add_new_path("Path 1", switch_to=False)
                
            target_idx = 0
            if current_path_name:
                for i in range(self.path_tabs.count()):
                    if self.path_tabs.tabText(i) == current_path_name:
                        target_idx = i
                        break
            
            self.path_tabs.setCurrentIndex(target_idx)

    def duplicate_profile(self):
        if self.profile_combo.currentText() == "New Profile" and not self.name_entry.text().strip(): return
        self.save_profile() 
        saved_name = self.name_entry.text().strip()
        with config_lock:
            if not saved_name or saved_name not in sites_data: return
            orig_data = sites_data[saved_name]
            
        dlg = DuplicateProfileDialog(self.window(), saved_name, orig_data)
        if dlg.exec():
            with config_lock:
                sites_data[dlg.new_name] = dlg.new_data
            save_config()
            self.refresh_combo(dlg.new_name)
            self.profile_saved_signal.emit()

    def save_profile(self):
        name = self.name_entry.text().strip()
        if not name: return
        
        old_start = "1"
        old_end = "1"
        with config_lock:
            if name in sites_data:
                old_start = sites_data[name].get("last_start", "1")
                old_end = sites_data[name].get("last_end", "1")
            elif self.original_profile_name in sites_data:
                old_start = sites_data[self.original_profile_name].get("last_start", "1")
                old_end = sites_data[self.original_profile_name].get("last_end", "1")
                
            if self.original_profile_name and self.original_profile_name != name and self.original_profile_name in sites_data:
                del sites_data[self.original_profile_name]
        
        s_paths = {}
        for i in range(self.path_tabs.count()):
            tab = self.path_tabs.widget(i)
            steps_list = []
            for obj in tab.step_widgets:
                xp = obj["xpath"].text().strip()
                if xp:
                    try: val = float(obj["delay"].text().strip())
                    except ValueError: val = 5.0
                    steps_list.append({"xpath": xp, "delay": val})
            s_paths[self.path_tabs.tabText(i).strip()] = steps_list
            
        with config_lock:
            sites_data[name] = {
                "url": self.url_entry.text().strip(), 
                "next_btn_xpath": self.next_entry.text().strip(), 
                "step_paths": s_paths, 
                "last_start": old_start, 
                "last_end": old_end
            }
        save_config()
        self.refresh_combo(name)
        self.profile_saved_signal.emit()

    def delete_profile(self):
        name = self.name_entry.text().strip()
        with config_lock:
            if name in sites_data:
                del sites_data[name]
        save_config()
        self.refresh_combo()
        self.profile_saved_signal.emit()
            
    def refresh_combo(self, target_name=None):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        
        with config_lock:
            data_keys = list(sites_data.keys())
            has_data = len(sites_data) > 0
            
        if not has_data:
            self.profile_combo.addItem("No Profiles")
            self.load_profile("New Profile")
        else:
            self.profile_combo.addItems(data_keys)
            if target_name and target_name in data_keys:
                self.profile_combo.setCurrentText(target_name)
                self.load_profile(target_name)
            elif self.original_profile_name in data_keys:
                self.profile_combo.setCurrentText(self.original_profile_name)
            else:
                self.profile_combo.setCurrentIndex(0)
                self.load_profile(self.profile_combo.currentText())
        self.profile_combo.blockSignals(False)