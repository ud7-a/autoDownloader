import os
import time
import json
import sys
import shutil
import py7zr
import tempfile
import random
import threading
import rarfile
import traceback
import ctypes
import re
import tempfile
import subprocess
import sqlite3
from datetime import datetime
from urllib.parse import quote, unquote
from subprocess import CREATE_NO_WINDOW 

# --- EXPLICIT IMPORTS TO FIX PYINSTALLER LAZY LOADING BUG ---
import urllib.request
import selenium.webdriver.chrome.options
import selenium.webdriver.chrome.service
# ------------------------------------------------------------

from PyQt6.QtCore import Qt, pyqtSignal, QObject , QRectF , QTimer
from PyQt6.QtGui import QIntValidator, QDoubleValidator, QIcon, QCursor, QPixmap, QPainter, QPen, QColor, QAction
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QFileDialog, QPushButton, QLineEdit, 
                             QComboBox, QProgressBar, QLabel, QCheckBox, 
                             QScrollArea, QFrame, QTabWidget, QMessageBox, 
                             QInputDialog, QDialog, QMenu, QTabBar, QListView, QSlider, QTableWidget, QTableWidgetItem, QHeaderView,
                             QSystemTrayIcon, QStyle , QStackedWidget)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service 

# ==========================================
#        GLOBAL VARIABLES & CONFIG
# ==========================================
APP_DIR = r"C:\Auto Episodes Downloader"
CONFIG_FILE = os.path.join(APP_DIR, "sites_config.json")
PROFILE_DIR = os.path.join(APP_DIR, "SeleniumProfile")
UBLOCK_CRX_PATH = os.path.join(APP_DIR, "ublock_lite.crx")
DB_FILE = os.path.join(APP_DIR, "download_history.db")
UNRAR_PATH = os.path.join(APP_DIR, "unrar.exe")
ARIA2C_PATH = os.path.join(APP_DIR, "aria2c.exe")
rarfile.UNRAR_TOOL = UNRAR_PATH
APP_VERSION = "3.4.2"

# FIXED: RLock prevents main thread from freezing
config_lock = threading.RLock()
db_lock = threading.RLock()
progress_lock = threading.RLock()

sites_data = {}
app_settings = {
    "download_dir": os.path.join(os.environ.get('USERPROFILE', ''), "Downloads"),
    "headless": True,
    "discord_webhook": "",
    "last_profile": "",
    "custom_sounds": [],    
    "selected_sound": "",   
    "volume": 100
}

finish_event = threading.Event()
cancel_event = threading.Event()
pause_event = threading.Event()
manual_driver = None  

# ==========================================
#  MIGRATION & INITIALIZATION LOGIC
# ==========================================
def init_db():
    os.makedirs(APP_DIR, exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS downloads_v2
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      date TEXT,
                      profile TEXT,
                      episodes TEXT,
                      status TEXT,
                      notes TEXT)''')
        conn.commit()
        conn.close()

def log_history(profile, episodes_str, status, notes):
    try:
        date_str = datetime.now().strftime("%b %d, %Y • %I:%M %p")
        with db_lock:
            conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT INTO downloads_v2 (date, profile, episodes, status, notes) VALUES (?, ?, ?, ?, ?)",
                      (date_str, profile, str(episodes_str), status, str(notes)))
            conn.commit()
            conn.close()
        signals.history_updated.emit()
    except Exception:
        pass


def apply_update_and_restart(downloaded_update_path):
    current_exe = sys.executable 
    exe_dir = os.path.dirname(current_exe)
    
    if not current_exe.lower().endswith(".exe") or "python" in current_exe.lower():
        print("Update skipped: Running from raw Python script, not a compiled .exe")
        os._exit(0)

    bat_path = os.path.join(exe_dir, "updater.bat")
    old_exe = current_exe + ".old"
    abs_download_path = os.path.join(exe_dir, downloaded_update_path)
    current_pid = os.getpid()
    
    bat_content = f"""@echo off
echo Installing new version... Please wait.
timeout /t 2 /nobreak > NUL

:: 1. Force kill the Python app completely
taskkill /F /PID {current_pid} /T > NUL 2>&1
timeout /t 2 /nobreak > NUL

:: 2. Delete the previous .old file if it exists from a past update
del /f /q "{old_exe}" > NUL 2>&1

:: 3. Rename current to .old
move /y "{current_exe}" "{old_exe}" > NUL 2>&1

:: 4. Move new download to current
move /y "{abs_download_path}" "{current_exe}" > NUL 2>&1

timeout /t 2 /nobreak > NUL

:: 5. Launch the new app
cd /d "{exe_dir}"
start "" "{current_exe}"

:: 6. Delete the script
del "%~f0"
"""
    
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)
        
    # --- THE HEAVY ARTILLERY ---
    # 0x00000008 is the Windows flag for DETACHED_PROCESS. 
    # It completely severs the batch script from the Python app.
    DETACHED_PROCESS = 0x00000008
    
    # Aggressively hunt down and scrub ALL PyInstaller environment variables
    clean_env = os.environ.copy()
    keys_to_remove = [k for k in clean_env if k.startswith('_MEI') or k == 'PYTHONPATH']
    for k in keys_to_remove:
        clean_env.pop(k, None)
        
    # Launch the orphaned batch script
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path], 
        env=clean_env, 
        creationflags=DETACHED_PROCESS
    )
    # ---------------------------
    
    # Instantly kill this Python process
    os._exit(0)

def check_for_updates_silently():
    """Silently checks the GitHub API in the background on startup."""
    API_URL = "https://api.github.com/repos/ud7-a/autoDownloader/releases/latest"
    try:
        req = urllib.request.Request(API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))

        latest_version = data.get('tag_name', '').replace("v", "")
        
        if latest_version and latest_version != APP_VERSION:
            for asset in data.get('assets', []):
                if asset['name'].endswith('.exe'):
                    download_url = asset['browser_download_url']
                    signals.update_available.emit(latest_version, download_url)
                    break
    except Exception:
        pass

def trigger_download_and_restart(download_url):
    """Downloads the file and triggers the batch script."""
    try:
        temp_exe = "update_temp.exe"
        req_exe = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_exe) as response, open(temp_exe, 'wb') as out_file:
            import shutil
            shutil.copyfileobj(response, out_file)
            
        apply_update_and_restart(temp_exe) 
    except Exception as e:
        print(f"Failed to download update: {e}")
        os._exit(1)

# ==========================================
#  FOOLPROOF UI ICON GENERATOR
# ==========================================
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
QWidget { background-color: #202020; color: #ffffff; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QLabel { background: transparent; }
QSlider { min-height: 24px; }
QSlider::groove:horizontal { border: none; height: 4px; background: #333333; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4cc2ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 12px; height: 12px; margin: -4px 0px; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #4cc2ff; }
QPushButton#MuteButton { background-color: transparent; border: none; font-size: 20px; padding: 0px; color: #ffffff; }
QPushButton#MuteButton:hover { color: #4cc2ff; }
QLineEdit#VolumeText { background-color: transparent; border: none; border-radius: 4px; padding: 0px 4px; color: #aaaaaa; font-weight: bold; font-size: 14px; }
QLineEdit#VolumeText:hover { background-color: #2b2b2b; color: #ffffff; }
QLineEdit#VolumeText:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; color: #4cc2ff; }
QCheckBox { spacing: 10px; color: #ffffff; font-weight: 500; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 1px solid #888888; background-color: rgba(255, 255, 255, 0.05); }
QCheckBox::indicator:hover { border: 1px solid #aaaaaa; background-color: rgba(255, 255, 255, 0.1); }
QCheckBox::indicator:checked { background-color: #4cc2ff; border: 1px solid #4cc2ff; image: url("ICON_CHECK"); }
QTabWidget::pane { border: none; border-top: 1px solid #333333; }
QTabBar::tab { background: transparent; color: #aaaaaa; padding: 12px 24px; font-weight: bold; font-size: 16px; border: none; border-bottom: 3px solid transparent; }
QTabBar::tab:hover { color: #ffffff; background: rgba(255, 255, 255, 0.05); }
QTabBar::tab:selected { color: #ffffff; border-bottom: 3px solid #4cc2ff; }
QTableWidget { background-color: #202020; color: #ffffff; border: 1px solid #333333; gridline-color: #333333; border-radius: 6px; }
QHeaderView::section { background-color: #2b2b2b; color: #aaaaaa; padding: 8px; border: none; border-bottom: 1px solid #444444; border-right: 1px solid #333333; font-weight: bold; }
QTableWidget::item { padding: 5px; border-bottom: 1px solid #2b2b2b; }
QTabWidget#PathTabs::pane { border: none; background-color: #202020; margin-top: 5px; }
QTabBar#PathTabBar::tab { background: #333333; color: #aaaaaa; padding: 6px 10px 6px 24px; margin-right: 6px; border-radius: 12px; font-size: 13px; font-weight: 500; }
QTabBar#PathTabBar::tab:selected { background: #4cc2ff; color: #000000; }
QTabBar#PathTabBar::tab:hover:!selected { background: #444444; color: #ffffff; }
QPushButton#TabDots { background-color: transparent; color: #aaaaaa; border: none; font-size: 33px; font-weight: bold; padding: 0px; margin: 0px; margin-right: 15px; margin-bottom: 2px; }
QPushButton#TabDots:hover { color: #ffffff; background-color: transparent; }
QPushButton#TabDotsSelected { background-color: transparent; color: #000000; border: none; font-weight: bold; padding: 0px; margin: 0px; font-size: 33px; margin-right: 15px; margin-bottom: 2px; }
QFrame#Card { background-color: #272727; border: 1px solid #333333; border-radius: 8px; }
QPushButton { background-color: #333333; border: 1px solid #444444; border-radius: 6px; padding: 8px 16px; }
QPushButton:hover { background-color: #3e3e3e; }
QPushButton:pressed { background-color: #2b2b2b; color: #aaaaaa; }
QPushButton:disabled { background-color: #2a2a2a; color: #555555; border: 1px solid #333333; }
QPushButton#Primary { background-color: #4cc2ff; color: #000000; border: none; font-weight: bold; }
QPushButton#Primary:hover { background-color: #4ebaf2; }
QPushButton#Primary:disabled { background-color: #1f4a60; color: #888888; }
QPushButton#Danger { background-color: #c0392b; color: white; border: none; }
QPushButton#Danger:hover { background-color: #e74c3c; }
QPushButton#DeleteStep { background-color: transparent; color: #ff5c5c; border: none; font-size: 18px; font-weight: bold; border-radius: 6px; padding: 0px; }
QPushButton#DeleteStep:hover { background-color: rgba(255, 92, 92, 0.15); }
QLineEdit { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px; color: white; }
QLineEdit:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; border-bottom: 2px solid #4cc2ff; }
QComboBox { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px 12px; min-height: 20px; }
QComboBox:hover { background-color: #333333; }
QComboBox:on { border-bottom: 2px solid #4cc2ff; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 30px; border-left: 1px solid #3a3a3a; }
QComboBox::down-arrow { image: url("ICON_ARROW"); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background-color: #2c2c2c; border: 1px solid #444444; border-radius: 8px; outline: none; padding: 4px; }
QComboBox QAbstractItemView::item { background-color: transparent; padding: 8px 12px; border-radius: 4px; min-height: 24px; color: #ffffff; border-left: 3px solid transparent; }
QComboBox QAbstractItemView::item:hover { background-color: #3a3a3a; }
QComboBox QAbstractItemView::item:selected { background-color: #444444; border-left: 3px solid #4cc2ff; color: #ffffff; }
QProgressBar { border: 1px solid #444444; border-radius: 4px; background-color: #2b2b2b; text-align: center; color: transparent; height: 6px; }
QProgressBar::chunk { background-color: #4cc2ff; border-radius: 3px; }
QScrollArea#StepScroll, QWidget#StepScrollContent { border: none; background-color: #202020; }
QScrollBar:vertical { border: none; background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #555555; min-height: 20px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
QScrollBar:horizontal { border: none; background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle:horizontal { background: #555555; min-width: 20px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { border: none; background: none; }
QDialog { background-color: #202020; }
QMenu { background-color: #2b2b2b; color: #ffffff; border: 1px solid #444444; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 36px 6px 24px; border-radius: 4px; }
QMenu::item:selected { background-color: #444444; }
QMenu::item:disabled { color: #777777; }
QMenu::separator { height: 1px; background-color: #444444; margin: 4px 0px; }
"""

# ==========================================
#     AUTO-DOWNLOADER FOR EXTENSIONS/TOOLS
# ==========================================

def ensure_updater_exe():
    """Silently downloads the updater.exe to the C: drive."""
    UPDATER_DIR = r"C:\Auto Episodes Downloader"
    UPDATER_PATH = os.path.join(UPDATER_DIR, "updater.exe")
    
    if not os.path.exists(UPDATER_DIR):
        try:
            os.makedirs(UPDATER_DIR)
        except Exception:
            pass 
            
    # 2. Download the updater.exe
    if not os.path.exists(UPDATER_PATH) or os.path.getsize(UPDATER_PATH) < 10000:
        try:
            # WE WILL FILL THIS URL IN DURING STEP 3
            url = "" 
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response, open(UPDATER_PATH, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        except Exception as e: 
            print(f"Updater download failed: {e}")


def ensure_unrar():
    if not os.path.exists(UNRAR_PATH) or os.path.getsize(UNRAR_PATH) < 100000:
        try:
            url = "https://github.com/ud7-a/unrar/raw/refs/heads/main/UnRAR.exe" 
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response, open(UNRAR_PATH, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            if os.path.getsize(UNRAR_PATH) < 100000: os.remove(UNRAR_PATH)
        except Exception: pass

def ensure_ublock_lite():
    if not os.path.exists(UBLOCK_CRX_PATH) or os.path.getsize(UBLOCK_CRX_PATH) < 100000:
        try:
            url = "https://clients2.google.com/service/update2/crx?response=redirect&os=win&arch=x64&os_arch=x86_64&nacl_arch=x86-64&prod=chromecrx&prodchannel=&prodversion=147.0.0.0&acceptformat=crx2,crx3&x=id%3Dddkjiahejlhfcafbddmgiahcphecmpfh%26installsource%3Dondemand%26uc"
            
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Upgrade-Insecure-Requests': '1'
            }
            
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response, open(UBLOCK_CRX_PATH, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
                
            if os.path.getsize(UBLOCK_CRX_PATH) < 100000: 
                os.remove(UBLOCK_CRX_PATH)
                print("⚠️ uBlock Lite download failed: Google is still blocking the bot.")
            else:
                print("✅ uBlock Lite downloaded successfully!")
                
        except Exception as e:
            print(f"⚠️ uBlock Lite download failed: {e}")
            try: os.remove(UBLOCK_CRX_PATH) 
            except: pass

def ensure_aria2c():
    if not os.path.exists(ARIA2C_PATH) or os.path.getsize(ARIA2C_PATH) < 100000:
        try:
            url = "https://github.com/ud7-a/files/raw/refs/heads/main/aria2c.exe" 
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response, open(ARIA2C_PATH, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            if os.path.getsize(ARIA2C_PATH) < 100000: os.remove(ARIA2C_PATH)
        except Exception as e: print(f"Aria2c download failed: {e}")


def load_config():
    global sites_data, app_settings
    if not os.path.exists(APP_DIR): 
        os.makedirs(APP_DIR, exist_ok=True)
        
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                sites_data = data.get("sites", {})
                
                needs_save = False
                for site, site_config in sites_data.items():
                    if "steps" in site_config and "step_paths" not in site_config:
                        site_config["step_paths"] = {"Path 1": site_config["steps"]}
                        del site_config["steps"]
                        needs_save = True
                if needs_save: save_config()

                saved_settings = data.get("settings", {})
                for k in app_settings.keys():
                    if k in saved_settings:
                        app_settings[k] = saved_settings[k]
                        
                if "custom_sound_path" in saved_settings and saved_settings["custom_sound_path"]:
                    old_path = saved_settings["custom_sound_path"]
                    if old_path not in app_settings["custom_sounds"]:
                        app_settings["custom_sounds"].append(old_path)
                    if not app_settings.get("selected_sound"):
                        app_settings["selected_sound"] = old_path
                        
        except Exception: pass
    else: 
        save_config()

def save_config():
    os.makedirs(APP_DIR, exist_ok=True)
    with config_lock:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"settings": app_settings, "sites": sites_data}, f, indent=4, ensure_ascii=False)

class WorkerSignals(QObject):
    update_status = pyqtSignal(str, str) 
    update_progress = pyqtSignal(int, int) 
    update_buttons = pyqtSignal(bool, bool, bool) 
    task_finished = pyqtSignal(list) 
    history_updated = pyqtSignal()
    add_active_download = pyqtSignal(int)
    update_active_download = pyqtSignal(int, str)
    update_active_bar = pyqtSignal(int, int)
    remove_active_download = pyqtSignal(int)
    task_started = pyqtSignal()
    update_available = pyqtSignal(str, str)

signals = WorkerSignals()

def parse_smart_xpath(raw_input):
    raw_input = raw_input.strip()
    if not raw_input: return ""
    if raw_input.startswith("/") or raw_input.startswith("("): return raw_input
        
    if "#" in raw_input:
        parts = raw_input.split("#", 1) 
        text_part = parts[0].strip().lower()
        index_part = parts[1].strip().lower()
        base_xpath = f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text_part}')]"
        if index_part == "last": return f"({base_xpath})[last()]"
        else: return f"({base_xpath})[{index_part}]"
    else:
        text_part = raw_input.lower()
        return f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text_part}')]"

def kill_stuck_chrome_processes():
    try: subprocess.run("taskkill /F /IM chromedriver.exe /T", shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    ps_kill_profile = 'powershell -Command "Get-WmiObject Win32_Process -Filter \\"Name=\'chrome.exe\'\\" | Where-Object {$_.CommandLine -match \'SeleniumProfile\'} | ForEach-Object { $_.Terminate() }"'
    try: subprocess.run(ps_kill_profile, shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    ps_kill_headless = 'powershell -Command "Get-WmiObject Win32_Process -Filter \\"Name=\'chrome.exe\'\\" | Where-Object {$_.CommandLine -match \'--headless\'} | ForEach-Object { $_.Terminate() }"'
    try: subprocess.run(ps_kill_headless, shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    time.sleep(1)
    if not os.path.exists(PROFILE_DIR): return
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try: os.remove(os.path.join(PROFILE_DIR, lock))
        except: pass

def launch_visible_browser():
    global manual_driver
    signals.update_buttons.emit(False, False, False)
    try:
        signals.update_status.emit("Status: Preparing profile browser...", "#f39c12")
        kill_stuck_chrome_processes()
        
            
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("prefs", {"profile.exit_type": "Normal", "profile.exited_cleanly": True})
        options.add_argument("--start-maximized")

        log_path = os.path.join(APP_DIR, "chromedriver.log")
        service = Service(service_args=["--log-level=ALL", "--enable-chrome-logs"], log_output=log_path)
        service.creation_flags = CREATE_NO_WINDOW
        manual_driver = webdriver.Chrome(options=options, service=service)
        manual_driver.get("chrome://extensions/")
        signals.update_status.emit("Status: Browser open. Setup extensions/logins, then close & Start!", "#f39c12")
    except Exception as e:
        signals.update_status.emit(f"Status: ❌ Error opening browser: {e}", "#e74c3c")
    finally:
        signals.update_buttons.emit(True, False, True)

def create_browser(download_dir, headless=True):
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    os.makedirs(download_dir, exist_ok=True)
    prefs = {
        "profile.exit_type": "Normal", "profile.exited_cleanly": True,
        "download.default_directory": download_dir, "download.prompt_for_download": False,
        "download.directory_upgrade": True, "safebrowsing.enabled": False, "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)

    options.add_argument("--enable-features=ParallelDownloading")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")

    if headless: 
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080") 
    else: 
        options.add_argument("--start-maximized") 

    if os.path.exists(UBLOCK_CRX_PATH) and os.path.getsize(UBLOCK_CRX_PATH) > 100000:
        try: options.add_extension(UBLOCK_CRX_PATH)
        except: pass

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    service = Service()
    service.creation_flags = CREATE_NO_WINDOW
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(45) 
    return driver

# ========================================================
# 🚀 ARIA2C 16-THREAD ENGINE HANDLER (UTF-8 SAFE)
# ========================================================
def aria2c_downloader(ep, url, final_name, cookies, ua, temp_dir, cancel_event, on_episode_completed, process_callback=None):
    if not os.path.exists(ARIA2C_PATH):
        signals.update_active_download.emit(ep, "❌ Downloader core missing! Please restart the app.")
        on_episode_completed()
        return

    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    final_name = final_name if final_name else f"episode_{ep}.mp4"
    
    while True:
        if cancel_event.is_set(): break
        
        cmd = [
            ARIA2C_PATH,
            "-c",                         
            "--auto-file-renaming=false", 
            "-x", "8",                    
            "-s", "8",                    
            "-j", "8",                    
            "-k", "5M",                   
            "--min-split-size=5M",        
            "--disk-cache=64M",           
            "--optimize-concurrent-downloads=true",
            "--file-allocation=none",     
            "--summary-interval=1",
            "--auto-save-interval=1",
        ]
        if ua: cmd.append(f"--user-agent={ua}")
        else: cmd.append("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        
        cmd.extend([
            "--header=Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "--header=Accept-Language: en-US,en;q=0.5",
            "--header=Sec-Fetch-Dest: document",
            "--header=Sec-Fetch-Mode: navigate",
        ])

        if cookie_str: cmd.append(f"--header=Cookie: {cookie_str}")

        cmd.extend([f"--dir={temp_dir}", f"--out={final_name}", url])

        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW
            )
            
            for line in process.stdout:
                if cancel_event.is_set() or pause_event.is_set():
                    process.terminate()
                    break
                    
                if "%" in line and "DL:" in line:
                    try:
                        match = re.search(r"([^ ]+)/([^ ]+)\((\d+)%\).*?DL:([^ \]]+)", line)
                        if match:
                            def convert_unit(val_str):
                                m = re.match(r"([\d\.]+)(K|M|G)iB", val_str)
                                if m:
                                    val = float(m.group(1))
                                    unit = m.group(2)
                                    if unit == 'K': return f"{val * 1.024:.1f} KB"
                                    if unit == 'M': return f"{val * 1.048576:.2f} MB"
                                    if unit == 'G': return f"{val * 1.07374:.2f} GB"
                                return val_str
                        
                            pct = int(match.group(3))
                            speed = convert_unit(match.group(4)) + "/s"
                            txt = f"Speed: {speed}   •   Progress: {pct}%"
                            
                            signals.update_active_bar.emit(ep, pct)
                            signals.update_active_download.emit(ep, txt)
                    except Exception:
                        pass
            
            process.wait()
            
            if cancel_event.is_set():
                break
                
            if pause_event.is_set():
                signals.update_active_download.emit(ep, "⏸ Paused")
                while pause_event.is_set() and not cancel_event.is_set():
                    time.sleep(1)
                if cancel_event.is_set(): break
                continue # RESTART THE DOWNLOADER LOOP TO RESUME!
            
            if process.returncode == 0:
                signals.update_active_bar.emit(ep, 100)
                signals.update_active_download.emit(ep, "Extraction & Cleanup...")
                if process_callback:
                    process_callback(ep, temp_dir)
                time.sleep(1)
                signals.remove_active_download.emit(ep)
                break 
            else:
                signals.update_active_download.emit(ep, "❌ Download Failed. Retrying...")
                time.sleep(3)
                
        except Exception as e:
            signals.update_active_download.emit(ep, f"Download Error: {e}")
            break
            
    on_episode_completed()

# ==========================================
#     SELENIUM INTERCEPTION CORE
# ==========================================
def run_selenium_task(site_key, episodes_list, download_dir, headless, webhook_url, selected_sound, volume):
    driver = None
    cancel_event.clear()
    pause_event.clear()
    failed_eps = []
    task_started = False
    episode_temp_dirs = {} 
    
    MAX_CONCURRENT = 3
    active_engine_threads = []
    episodes_completed_count = 0

    def on_episode_completed():
        if cancel_event.is_set(): return # <-- ADD THIS: Ignores the update if cancelled!
        
        nonlocal episodes_completed_count
        with progress_lock:
            episodes_completed_count += 1
            signals.update_progress.emit(episodes_completed_count, len(episodes_list))

    try:
        if site_key not in sites_data:
            signals.update_status.emit("Status: ❌ Invalid Profile.", "#e74c3c")
            return
            
        task_started = True

        with config_lock:
            config = sites_data.get(site_key, {})
            
        url_template = config.get("url", "")
        next_btn_xpath = parse_smart_xpath(config.get("next_btn_xpath", ""))
        step_paths = config.get("step_paths", {"Path 1": config.get("steps", [])})
        safe_site_name = "".join(c for c in site_key if c not in r'\/:*?"<>|').strip()

        step_paths = config.get("step_paths", {"Path 1": config.get("steps", [])})
        safe_site_name = "".join(c for c in site_key if c not in r'\/:*?"<>|').strip()

        # ---> 1. ADD THIS ENTIRE HELPER FUNCTION HERE <---
        profile_folder_path = os.path.join(download_dir, safe_site_name)
        os.makedirs(profile_folder_path, exist_ok=True)
        VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts')

        def process_downloaded_episode(x, temp_dir):
            if not os.path.exists(temp_dir): return
            try:
                current_timestamp = time.time()
                for item in os.listdir(temp_dir):
                    src_item = os.path.join(temp_dir, item)
                    
                    if os.path.isfile(src_item) and src_item.lower().endswith(('.zip', '.tar', '.gz', '.bz2', '.rar', '.7z')):
                        extract_temp = os.path.join(temp_dir, "extracted_junk")
                        os.makedirs(extract_temp, exist_ok=True)
                        try:
                            extract_success = False
                            for attempt in range(4):
                                try:
                                    if src_item.lower().endswith('.rar'):
                                        with rarfile.RarFile(src_item) as rf:
                                            rf.extractall(path=extract_temp)
                                    elif src_item.lower().endswith('.7z'):
                                        with py7zr.SevenZipFile(src_item, mode='r') as z:
                                            z.extractall(path=extract_temp)
                                    else:
                                        shutil.unpack_archive(src_item, extract_temp)
                                    extract_success = True
                                    break 
                                except Exception as zip_err:
                                    print(f"Extraction locked. Retrying in 2s... ({zip_err})")
                                    time.sleep(2)
                                    
                            if not extract_success:
                                raise Exception("Episode File is corrupted or incomplete.")
                            
                            found_videos = []
                            for root, _, files in os.walk(extract_temp):
                                for f in files:
                                    if f.lower().endswith(VIDEO_EXTENSIONS):
                                        found_videos.append(os.path.join(root, f))
                            
                            if found_videos:
                                found_videos.sort(key=os.path.getsize, reverse=True)
                                main_video = found_videos[0]
                                _, ext = os.path.splitext(main_video)
                                new_name = f"{safe_site_name} Ep{x}{ext}"
                                dst_item = os.path.join(profile_folder_path, new_name)
                                
                                counter = 1
                                while os.path.exists(dst_item):
                                    dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                                    counter += 1
                                    
                                shutil.move(main_video, dst_item)
                                try: os.utime(dst_item, (current_timestamp, current_timestamp))
                                except: pass
                        except Exception as e:
                            print(f"Extraction failed for Ep {x}: {e}")
                            
                    elif os.path.isdir(src_item):
                        found_videos = []
                        for root, _, files in os.walk(src_item):
                            for f in files:
                                if f.lower().endswith(VIDEO_EXTENSIONS):
                                    found_videos.append(os.path.join(root, f))
                        if found_videos:
                            found_videos.sort(key=os.path.getsize, reverse=True)
                            main_video = found_videos[0]
                            _, ext = os.path.splitext(main_video)
                            new_name = f"{safe_site_name} Ep{x}{ext}"
                            dst_item = os.path.join(profile_folder_path, new_name)
                            counter = 1
                            while os.path.exists(dst_item):
                                dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                                counter += 1
                            shutil.move(main_video, dst_item)
                            try: os.utime(dst_item, (current_timestamp, current_timestamp))
                            except: pass

                    elif os.path.isfile(src_item) and src_item.lower().endswith(VIDEO_EXTENSIONS):
                        _, ext = os.path.splitext(item)
                        new_name = f"{safe_site_name} Ep{x}{ext}"
                        dst_item = os.path.join(profile_folder_path, new_name)
                        counter = 1
                        while os.path.exists(dst_item):
                            dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                            counter += 1
                        shutil.move(src_item, dst_item)
                        try: os.utime(dst_item, (current_timestamp, current_timestamp))
                        except: pass
                        
                shutil.rmtree(temp_dir)
            except Exception as e:
                print(f"Error processing Ep {x}: {e}")
        # -----------------------------------------------------

        signals.update_status.emit("Status: Cleaning up...", "#f39c12")
        kill_stuck_chrome_processes()
        driver = create_browser(download_dir, headless)
        wait = WebDriverWait(driver, 10)

        total_episodes = len(episodes_list)
        signals.update_progress.emit(0, total_episodes)

        for x in episodes_list:
            if cancel_event.is_set(): break
            
            while len([t for t in active_engine_threads if t.is_alive()]) >= MAX_CONCURRENT and not cancel_event.is_set():
                time.sleep(1)

            while pause_event.is_set() and not cancel_event.is_set():
                time.sleep(1)
            
            if cancel_event.is_set(): break
            
            if len([t for t in active_engine_threads if t.is_alive()]) > 0:
                time.sleep(random.uniform(2.0, 4.0))

            path_success = False
            ep_temp_dir = os.path.join(tempfile.gettempdir(), f"AnimeDL_{safe_site_name}_Ep_{x}")
            os.makedirs(ep_temp_dir, exist_ok=True)
            episode_temp_dirs[x] = ep_temp_dir
            
            url = url_template.replace("{x}", str(x))
            print(f"\nProcessing Ep {x}")
            
            for attempt in range(3):
                if cancel_event.is_set() or path_success: break
                try:
                    signals.update_status.emit(f"Status: Loading {site_key} - Ep {x} (Attempt {attempt+1}/3)...", "#ffffff")
                    
                    driver.execute_script("window.open('');")
                    driver.switch_to.window(driver.window_handles[-1])
                    driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": ep_temp_dir})
                    
                    driver.get(url)
                    time.sleep(3) 

                    for path_name, steps in step_paths.items():
                        if cancel_event.is_set() or path_success: break
                        if not steps: continue
                        
                        signals.update_status.emit(f"Status: [{path_name}] Executing...", "#ffffff")
                        path_failed = False
                        current_tabs = len(driver.window_handles)

                        for step_idx, step in enumerate(steps):
                            if cancel_event.is_set(): break
                            raw_xpath = step.get("xpath", "").strip()
                            delay = float(step.get("delay", 0.0))
                            if not raw_xpath: continue
                            
                            xpath = parse_smart_xpath(raw_xpath)
                            signals.update_status.emit(f"Status: [{path_name}] Clicking Step {step_idx + 1}...", "#ffffff")
                            
                            xpaths_to_try = [xpath, xpath.replace("text()", "@value")]
                            if step_idx == 0 and 'google drive' in raw_xpath.lower():
                                xpaths_to_try.append("//ul[contains(@class, 'download-links')]//a")

                            btn = None
                            for xp in xpaths_to_try:
                                try: 
                                    btn = wait.until(EC.presence_of_element_located((By.XPATH, xp)))
                                    break
                                except Exception: pass
                            
                            if not btn: 
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Could not find button for Step {step_idx + 1}", "#e74c3c")
                                break 

                            try: 
                                from selenium.webdriver.common.action_chains import ActionChains
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                                time.sleep(0.5)
                                ActionChains(driver).move_to_element(btn).click().perform()
                            except Exception:
                                try:
                                    driver.execute_script("arguments[0].click();", btn)
                                except Exception:
                                    path_failed = True
                                    signals.update_status.emit(f"Status: ❌ Failed to click Step {step_idx + 1}", "#e74c3c")
                                    break
                            
                            time.sleep(delay)
                            new_tabs = len(driver.window_handles)
                            if new_tabs > current_tabs:
                                driver.switch_to.window(driver.window_handles[-1])
                                driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": ep_temp_dir})
                                current_tabs = new_tabs
                        
                        if not path_failed:
                            # 🚀 ARIA2C INTERCEPTION PHASE 
                            signals.update_status.emit(f"Status: Intercepting Ep {x} (Waiting up to 35s)...", "#f39c12")
                            
                            driver.execute_script("window.open('');")
                            driver.switch_to.window(driver.window_handles[-1])
                            driver.get('chrome://downloads')
                            
                            found_data = None
                            wait_timer = 0
                            
                            while wait_timer < 35 and not cancel_event.is_set():
                                # This JS guarantees it finds the active download, extracts the URL, and physically clicks "Cancel" in Chrome
                                js_intercept = """
                                    const manager = document.querySelector('downloads-manager');
                                    if (!manager) return null;

                                    let targetData = null;
                                    let cancelBtn = null;

                                    // 1. Physically hunt for the UI "Cancel" button. If it exists, the file is actively downloading.
                                    try {
                                        const list = manager.shadowRoot.querySelector('#downloadsList');
                                        if (list) {
                                            const items = list.querySelectorAll('downloads-item');
                                            for (let item of items) {
                                                const shadow = item.shadowRoot;
                                                if (shadow) {
                                                    const btn = shadow.querySelector('#cancel');
                                                    // Make sure the button is actually visible/active
                                                    if (btn && !btn.hidden && item.data) {
                                                        targetData = item.data;
                                                        cancelBtn = btn;
                                                        break;
                                                    }
                                                }
                                            }
                                        }
                                    } catch(e) {}

                                    // 2. Backup plan: Ask Chrome's internal array
                                    if (!targetData && manager.items) {
                                        for (let item of manager.items) {
                                            if (item.state === 'IN_PROGRESS') {
                                                targetData = item;
                                                break;
                                            }
                                        }
                                    }

                                    // 3. Extract the URL, click Cancel, and pass to Aria2c!
                                    if (targetData) {
                                        let dl_url = targetData.url || targetData.finalUrl || targetData.originalUrl;
                                        if (!dl_url) return null; // File hasn't fully registered yet, wait 1 more second
                                        if (dl_url.startsWith('blob:')) return "BLOB";
                                        
                                        let fname = targetData.fileName || targetData.filePath || 'episode.mp4';
                                        fname = fname.split('\\\\').pop().split('/').pop();
                                        
                                        try {
                                            if (cancelBtn) {
                                                cancelBtn.click(); // Physically click the cancel button
                                            } else if (manager.shadowRoot) {
                                                manager.shadowRoot.querySelector('#downloadsList').cancelDownload(targetData.id);
                                            }
                                        } catch(e) {}
                                        
                                        return JSON.stringify({url: dl_url, filename: fname});
                                    }
                                    return null;
                                """
                                res = driver.execute_script(js_intercept)
                                if res:
                                    found_data = res
                                    break
                                time.sleep(1)
                                wait_timer += 1
                                
                            driver.close()
                            driver.switch_to.window(driver.window_handles[0])
                            
                            if found_data and found_data != "BLOB":
                                signals.update_status.emit(f"Status: ✅ Locked onto Ep {x}! Starting download...", "#2ecc71")                                
                                signals.add_active_download.emit(x)
                                data_obj = json.loads(found_data)
                                dl_url = data_obj['url']
                                dl_fname = data_obj['filename']
                                cookies = driver.get_cookies()
                                ua = driver.execute_script("return navigator.userAgent;")
                                
                                t = threading.Thread(target=aria2c_downloader, 
                                                     args=(x, dl_url, dl_fname, cookies, ua, ep_temp_dir, cancel_event, on_episode_completed, process_downloaded_episode))
                                t.start()
                                active_engine_threads.append(t)
                                path_success = True
                                break
                            elif found_data == "BLOB":
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Video is streaming, not a direct file.", "#e74c3c")
                                break
                            else:
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Download never started on the webpage.", "#e74c3c")
                                
                    if not path_success: raise Exception("Interception failed")
                    
                except Exception as e:
                    signals.update_status.emit(f"Status: ⚠️ Attempt failed, retrying...", "#e74c3c")
                    if len(driver.window_handles) > 1:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    time.sleep(2)

            if not path_success:
                failed_eps.append(x)
                signals.update_status.emit(f"Status: ❌ Failed to grab Episode {x} after 3 retries.", "#e74c3c")

        if not cancel_event.is_set():
            signals.update_status.emit("Status: All downloads triggered! Waiting for files to finish...", "#f39c12")
            signals.update_buttons.emit(False, True, False)
            
            while len([t for t in active_engine_threads if t.is_alive()]) > 0 and not cancel_event.is_set():
                time.sleep(1)
                
            if not cancel_event.is_set():
                signals.update_status.emit("Status: 🎉 All files downloaded and extracted successfully!", "#2ecc71")
                
                def send_webhook_alert():
                    if webhook_url:
                        msg_text = f"🎉Successfully finished downloading episodes for {site_key}🎉"
                        if len(failed_eps) > 1: msg_text += f"\n⚠️ Note: {len(failed_eps)} episodes failed to download."
                        elif len(failed_eps) == 1: msg_text += f"\n⚠️ Note: {len(failed_eps)} episode failed to download."
                        data = {"content": msg_text}
                        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                        req = urllib.request.Request(webhook_url, data=json.dumps(data).encode(), headers=headers)
                        try: urllib.request.urlopen(req, timeout=5)
                        except Exception: pass
                
                threading.Thread(target=send_webhook_alert, daemon=True).start()
                
                try:
                    if selected_sound and os.path.exists(selected_sound):
                        vol = volume * 10 
                        ctypes.windll.winmm.mciSendStringW('close custom_audio', None, 0, None)
                        ctypes.windll.winmm.mciSendStringW(f'open "{selected_sound}" alias custom_audio', None, 0, None)
                        ctypes.windll.winmm.mciSendStringW(f'setaudio custom_audio volume to {vol}', None, 0, None)
                        ctypes.windll.winmm.mciSendStringW('play custom_audio', None, 0, None)
                except Exception: pass
            
    except Exception as e:
        signals.update_status.emit("Status: ❌ Critical Error Occurred. Check console.", "#e74c3c")
        traceback.print_exc()

    finally:
        # 1. Always clean up the browser
        if driver:
            try: driver.quit()
            except: pass
            
        # 2. Always log the history
        if task_started:
            if cancel_event.is_set():
                status = "Cancelled"
                notes = "Stopped by user."
            elif len(failed_eps) == len(episodes_list):
                status = "Failed"
                notes = "All episodes failed."
            elif failed_eps:
                status = "Partial"
                notes = f"Failed: {', '.join(map(str, failed_eps))}"
            else:
                status = "Success"
                notes = "Completed successfully."
            eps_str = f"{episodes_list[0]} - {episodes_list[-1]}" if len(episodes_list) > 1 else str(episodes_list[0])
            log_history(site_key, eps_str, status, notes)

        # 3. Always unlock the UI
        if cancel_event.is_set():
            signals.update_status.emit("Status: ❌ Download Cancelled.", "#e74c3c")
            
        signals.update_buttons.emit(True, False, True)
        finish_event.clear()
        signals.task_finished.emit(failed_eps)

# ==========================================
#     UI CLASSES (DIALOGS & TABS)
# ==========================================

class DownwardComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())
        self.setMaxVisibleItems(6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view().setCursor(Qt.CursorShape.PointingHandCursor)
        self._popup_initialized = False 

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        if popup:
            if not self._popup_initialized:
                popup.setWindowFlags(popup.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
                popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                self._popup_initialized = True
                popup.show() 
            pos = self.mapToGlobal(self.rect().bottomLeft())
            pos.setY(pos.y() + 2)
            popup.move(pos)

class JumpSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            val = self.minimum() + (self.maximum() - self.minimum()) * event.pos().x() / self.width()
            self.setValue(int(val))
        super().mousePressEvent(event)

class DuplicateProfileDialog(QDialog):
    def __init__(self, parent, orig_name, orig_data):
        super().__init__(parent)
        self.setWindowTitle("Duplicate Profile")
        self.setFixedSize(450, 250)
        self.new_name = None
        self.new_data = None
        self.orig_data = json.loads(json.dumps(orig_data)) 
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("New Profile Name:"))
        self.name_entry = QLineEdit(orig_name + " (Copy)")
        layout.addWidget(self.name_entry)
        
        layout.addWidget(QLabel("New Base URL:"))
        self.url_entry = QLineEdit(self.orig_data.get("url", ""))
        self.url_entry.textChanged.connect(self.auto_decode)
        layout.addWidget(self.url_entry)
        
        btn_layout = QHBoxLayout()
        btn_copy = QPushButton("Copy {x}")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText("{x}"))
        btn_fmt = QPushButton("Auto-Format URL")
        btn_fmt.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_fmt.clicked.connect(self.format_url)
        btn_layout.addWidget(btn_copy)
        btn_layout.addWidget(btn_fmt)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        action_layout = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        
        btn_save = QPushButton("Save Duplicate")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setObjectName("Primary")
        btn_save.clicked.connect(self.save)
        
        action_layout.addStretch()
        action_layout.addWidget(btn_cancel)
        action_layout.addWidget(btn_save)
        layout.addLayout(action_layout)

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
        
    def save(self):
        n = self.name_entry.text().strip()
        if not n: return
        self.new_name = n
        self.orig_data["url"] = self.url_entry.text().strip()
        self.new_data = self.orig_data
        self.accept()

class PathTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.step_widgets = [] 
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("StepScroll")
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content.setObjectName("StepScrollContent")
        self.s_layout = QVBoxLayout(self.content)
        self.s_layout.setContentsMargins(0, 0, 10, 0)
        self.s_layout.addStretch()
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)

class SiteManagerWidget(QWidget):
    profile_saved_signal = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.original_profile_name = None 
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        
        self.scroll = QScrollArea()
        self.scroll.setObjectName("StepScroll")
        self.scroll.setWidgetResizable(True)
        
        self.content = QWidget()
        self.content.setObjectName("StepScrollContent")
        
        layout = QVBoxLayout(self.content)
        layout.setSpacing(15)
        layout.setContentsMargins(30, 30, 30, 30)

        profile_sel_layout = QHBoxLayout()
        profile_sel_layout.addWidget(QLabel("Load Profile:"))
        self.profile_combo = DownwardComboBox()
        with config_lock:
            self.profile_combo.addItems(list(sites_data.keys()))
        self.profile_combo.currentTextChanged.connect(self.load_profile)
        profile_sel_layout.addWidget(self.profile_combo, 1)
        
        btn_new_profile = QPushButton("➕ New Profile")
        btn_new_profile.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_new_profile.clicked.connect(lambda: self.load_profile("New Profile"))
        profile_sel_layout.addWidget(btn_new_profile)
        
        self.btn_dup = QPushButton("📑 Duplicate")
        self.btn_dup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dup.clicked.connect(self.duplicate_profile)
        profile_sel_layout.addWidget(self.btn_dup)
        
        btn_import = QPushButton("📥 Import")
        btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_import.clicked.connect(self.import_profile)
        profile_sel_layout.addWidget(btn_import)
        
        btn_export = QPushButton("📤 Export")
        btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_export.clicked.connect(self.export_profile)
        profile_sel_layout.addWidget(btn_export)
        
        layout.addLayout(profile_sel_layout)

        layout.addWidget(QLabel("Profile Name:"))
        self.name_entry = QLineEdit()
        layout.addWidget(self.name_entry)

        layout.addWidget(QLabel("Base URL:"))
        self.url_entry = QLineEdit()
        self.url_entry.textChanged.connect(self.auto_decode_url) 
        layout.addWidget(self.url_entry)

        tool_layout = QHBoxLayout()
        btn_copy = QPushButton("Copy {x}")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(self.copy_x)
        
        btn_fmt = QPushButton("Auto-Format URL")
        btn_fmt.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_fmt.clicked.connect(self.format_url)
        
        tool_layout.addWidget(btn_copy)
        tool_layout.addWidget(btn_fmt)
        tool_layout.addStretch(1)
        layout.addLayout(tool_layout)

        layout.addWidget(QLabel("Next Episode Button Text (or XPath):"))
        self.next_entry = QLineEdit()
        layout.addWidget(self.next_entry)

        step_header = QHBoxLayout()
        step_header.addWidget(QLabel("Automation Paths:", styleSheet="font-weight: bold;"))
        
        btn_add_path = QPushButton("+ New Path")
        btn_add_path.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add_path.clicked.connect(lambda: self.add_new_path())
        
        btn_add_step = QPushButton("+ Add Step")
        btn_add_step.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add_step.clicked.connect(self.add_step_to_current)
        
        step_header.addStretch()
        step_header.addWidget(btn_add_path)
        step_header.addWidget(btn_add_step)
        layout.addLayout(step_header)

        self.path_tabs = QTabWidget()
        self.path_tabs.setObjectName("PathTabs")
        self.path_tabs.tabBar().setObjectName("PathTabBar")
        self.path_tabs.setTabsClosable(True)
        self.path_tabs.tabCloseRequested.connect(self.show_tab_menu)
        self.path_tabs.currentChanged.connect(self.update_tab_dots)
        layout.addWidget(self.path_tabs, 1)

        btn_layout = QHBoxLayout()
        self.btn_del = QPushButton("Delete Profile")
        self.btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self.delete_profile)
        
        btn_save = QPushButton("Save Profile")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setObjectName("Primary")
        btn_save.clicked.connect(self.save_profile)
        
        btn_layout.addWidget(self.btn_del)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

        self.scroll.setWidget(self.content)
        outer_layout.addWidget(self.scroll)

        with config_lock:
            has_sites = len(sites_data) > 0
            first_site = list(sites_data.keys())[0] if has_sites else "New Profile"
            
        if has_sites: 
            self.load_profile(first_site)
        else: 
            self.load_profile("New Profile")

    def import_profile(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Profile", "", "JSON Files (*.json)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f: data = json.load(f)
                default_name = os.path.splitext(os.path.basename(file_path))[0]
                new_name, ok = QInputDialog.getText(self, "Import Profile", "Enter a name for this profile:", text=default_name)
                if ok and new_name.strip():
                    with config_lock:
                        sites_data[new_name.strip()] = data
                    save_config()
                    self.refresh_combo(new_name.strip())
                    self.profile_saved_signal.emit()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to import profile:\n{e}")

    def export_profile(self):
        current_profile = self.profile_combo.currentText()
        with config_lock:
            if current_profile and current_profile in sites_data:
                file_path, _ = QFileDialog.getSaveFileName(self, "Export Profile", f"{current_profile}.json", "JSON Files (*.json)")
                if file_path:
                    try:
                        with open(file_path, "w", encoding="utf-8") as f:
                            json.dump(sites_data[current_profile], f, indent=4, ensure_ascii=False)
                        QMessageBox.information(self, "Success", "Profile exported successfully!")
                    except Exception as e:
                        QMessageBox.critical(self, "Error", f"Failed to export profile:\n{e}")

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
        
        # Only switch tabs if we explicitly want to (prevents flickering on save)
        if switch_to:
            self.path_tabs.setCurrentWidget(tab)
            
        btn = QPushButton("⋮")
        is_selected = (self.path_tabs.currentIndex() == self.path_tabs.indexOf(tab))
        btn.setObjectName("TabDotsSelected" if is_selected else "TabDots")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(20, 24)
        
        # Safely bind the specific tab to the lambda
        btn.clicked.connect(lambda checked=False, t=tab: self.show_tab_menu(self.path_tabs.indexOf(t)))
        self.path_tabs.tabBar().setTabButton(self.path_tabs.indexOf(tab), QTabBar.ButtonPosition.RightSide, btn)
        
        if switch_to:
            self.update_tab_dots(self.path_tabs.currentIndex())
            
        if steps:
            for step in steps:
                self.add_step_to_tab(tab, step.get("xpath", ""), str(step.get("delay", 5.0)))
        elif not name:
            self.add_step_to_tab(tab, "", "5.0")

    def update_tab_dots(self, current_idx):
        for i in range(self.path_tabs.count()):
            btn = self.path_tabs.tabBar().tabButton(i, QTabBar.ButtonPosition.RightSide)
            if btn:
                btn.setObjectName("TabDotsSelected" if i == current_idx else "TabDots")
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def add_step_to_current(self):
        tab = self.path_tabs.currentWidget()
        if tab: self.add_step_to_tab(tab, "", "5.0")

    def add_step_to_tab(self, tab, xp, dl):
        card = QFrame()
        card.setObjectName("Card")
        c_layout = QHBoxLayout(card)
        xp_in = QLineEdit(xp)
        xp_in.setPlaceholderText("Button Text (or XPath)")
        dl_in = QLineEdit(str(dl))
        dl_in.setValidator(QDoubleValidator())
        dl_in.setFixedWidth(70) 
        
        btn_del = QPushButton("🗑️")
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setObjectName("DeleteStep")
        btn_del.setFixedSize(36, 36)
        
        c_layout.addWidget(xp_in, 1)
        c_layout.addWidget(QLabel("Cooldown (sec):"))
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
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #2b2b2b; color: white; border: 1px solid #444; border-radius: 15px; padding: 4px; } QMenu::item { padding: 8px 24px; border-radius: 8px; } QMenu::item:selected { background-color: #4cc2ff; color: #000; }")
        rename_action = menu.addAction("✏️ Rename")
        delete_action = menu.addAction("🗑️ Delete")
        action = menu.exec(QCursor.pos())
        if action == rename_action:
            new_name, ok = QInputDialog.getText(self, "Rename Path", "Enter new path name:", text=self.path_tabs.tabText(index))
            if ok and new_name.strip():
                self.path_tabs.setTabText(index, new_name.strip())
        elif action == delete_action:
            widget = self.path_tabs.widget(index)
            self.path_tabs.removeTab(index)
            widget.deleteLater()

    def load_profile(self, choice):
        if not choice: return 
        
        # 1. Remember the currently active tab so we don't lose our place
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
                # Pass switch_to=False so it stops flickering through every tab
                self.add_new_path(path_name, steps, switch_to=False)
                
            if not step_paths:
                self.add_new_path("Path 1", switch_to=False)
                
            # 2. Restore the user's active tab silently
            target_idx = 0
            if current_path_name:
                for i in range(self.path_tabs.count()):
                    if self.path_tabs.tabText(i) == current_path_name:
                        target_idx = i
                        break
            
            self.path_tabs.setCurrentIndex(target_idx)
            self.update_tab_dots(target_idx)

    def duplicate_profile(self):
        if self.profile_combo.currentText() == "New Profile" and not self.name_entry.text().strip(): return
        self.save_profile() 
        saved_name = self.name_entry.text().strip()
        with config_lock:
            if not saved_name or saved_name not in sites_data: return
            orig_data = sites_data[saved_name]
            
        dlg = DuplicateProfileDialog(self, saved_name, orig_data)
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

# ==========================================
#     NEW HISTORY TAB CLASS
# ==========================================
class HistoryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)

        header_lbl = QLabel("📖 Download History")
        header_lbl.setStyleSheet("font-size: 18px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(header_lbl)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Profile", "Episodes", "Status", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        # --- CLEAR HISTORY BUTTON ---
        self.btn_clear_history = QPushButton("🗑️ Clear History")
        self.btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_history.setStyleSheet("""
            QPushButton {
                background-color: #2d2d2d;
                border: 1px solid #e74c3c;
                color: #e74c3c;
            }
            QPushButton:hover {
                background-color: #e74c3c;
                color: white;
            }
        """)
        self.btn_clear_history.clicked.connect(self.clear_history)
        
        layout.addWidget(self.btn_clear_history)
        layout.addWidget(self.table)
        
        signals.history_updated.connect(self.refresh_data)
        self.refresh_data()

    def clear_history(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Clear History")
        msg.setText("Are you sure you want to permanently delete your download history?")
        msg.setIcon(QMessageBox.Icon.NoIcon)
        
        btn_yes = msg.addButton("Yes, clear it", QMessageBox.ButtonRole.DestructiveRole)
        btn_yes.setCursor(Qt.CursorShape.PointingHandCursor)
        
        btn_no = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        btn_no.setCursor(Qt.CursorShape.PointingHandCursor)
        
        msg.exec()
        
        # 2. If they click Yes, clear the database
        if msg.clickedButton() == btn_yes:
            try:
                conn = sqlite3.connect(DB_FILE , check_same_thread=False) 
                cursor = conn.cursor()
                
                # IMPORTANT: Change "downloads" to whatever your table is actually named!
                cursor.execute("DELETE FROM downloads_v2") 
                conn.commit()
                conn.close()    
                self.refresh_data()
                success_msg = QMessageBox(self)
                success_msg.setWindowTitle("Success")
                success_msg.setText("History cleared successfully!")
                success_msg.exec()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clear history: {e}")

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


class UpdateDialog(QDialog):
    def __init__(self, new_version, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Ready")
        self.setFixedSize(400, 160)
        
        # Apply your app's dark theme via CSS
        self.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                color: #ffffff;
            }
            QLabel {
                font-size: 13px;
                color: #dddddd;
            }
            QPushButton {
                background-color: #3b3b3b;
                color: white;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #505050;
            }
            /* Style the Download button to match your blue Start Download button */
            QPushButton#downloadBtn {
                background-color: #5bc0de; 
                color: #222222;
                border: none;
            }
            QPushButton#downloadBtn:hover {
                background-color: #46b8da;
            }
        """)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Title & Message
        title = QLabel("<b>Update Ready</b>")
        title.setStyleSheet("font-size: 16px; color: white;")
        
        message = QLabel(f"A new version of Anime Episodes Downloader ({new_version}) is ready to install. Download and restart now to continue.")
        message.setWordWrap(True)

        layout.addWidget(title)
        layout.addWidget(message)

        # Buttons
        btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("Download")
        self.btn_download.setObjectName("downloadBtn") # Connects to the blue CSS above
        self.btn_cancel = QPushButton("Cancel")

        btn_layout.addWidget(self.btn_download)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # Connect the buttons
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_download.clicked.connect(self.accept)

class DownloaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        
        self.scroll = QScrollArea()
        self.scroll.setObjectName("StepScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.content = QWidget()
        self.content.setObjectName("StepScrollContent")
        
        main_layout = QVBoxLayout(self.content)
        main_layout.setSpacing(15) 
        main_layout.setContentsMargins(30, 20, 30, 20)

        self.btn_profile = QPushButton("🌐 Open Browser (Extensions / Login)")
        self.btn_profile.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_profile.clicked.connect(lambda: threading.Thread(target=launch_visible_browser, daemon=True).start())
        main_layout.addWidget(self.btn_profile)

        main_layout.addWidget(QLabel("Download Location:", styleSheet="font-weight: bold; margin-top: 10px;"))
        dir_layout = QHBoxLayout()
        self.txt_dir = QLineEdit(app_settings["download_dir"])
        self.txt_dir.setReadOnly(True)
        
        btn_browse_dir = QPushButton("📂 Browse")
        btn_browse_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse_dir.setMinimumWidth(105)
        btn_browse_dir.clicked.connect(self.browse_folder)
        
        dir_layout.addWidget(self.txt_dir, 1) 
        dir_layout.addWidget(btn_browse_dir)
        main_layout.addLayout(dir_layout)

        main_layout.addWidget(QLabel("Active Website Profile:", styleSheet="font-weight: bold; margin-top: 5px;"))
        self.combo_site = DownwardComboBox()
        self.combo_site.currentTextChanged.connect(self.on_site_select)
        main_layout.addWidget(self.combo_site)
        
        self.lbl_url = QLabel("No profile selected")
        self.lbl_url.setStyleSheet("color: #888888; font-size: 12px;")
        main_layout.addWidget(self.lbl_url)

        self.chk_headless = QCheckBox("Run Invisibly (Headless)")
        self.chk_headless.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_headless.setChecked(app_settings["headless"])
        self.chk_headless.toggled.connect(self.save_settings)
        main_layout.addWidget(self.chk_headless)

        main_layout.addWidget(QLabel("Notification Sound:", styleSheet="font-weight: bold; margin-top: 5px;"))
        sound_layout = QHBoxLayout()
        
        self.combo_sound = DownwardComboBox()
        self.combo_sound.currentIndexChanged.connect(self.on_sound_change)
        sound_layout.addWidget(self.combo_sound, 1) 
        
        self.btn_play_sound = QPushButton("▶ Play")
        self.btn_play_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play_sound.setMinimumWidth(75)
        self.btn_play_sound.clicked.connect(self.preview_sound)
        sound_layout.addWidget(self.btn_play_sound)
        
        self.btn_add_sound = QPushButton("➕ Add Sound")
        self.btn_add_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_sound.setMinimumWidth(105)
        self.btn_add_sound.clicked.connect(self.browse_custom_sound)
        sound_layout.addWidget(self.btn_add_sound)
        
        self.btn_delete_sound = QPushButton("🗑️")
        self.btn_delete_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_sound.setFixedWidth(50)
        self.btn_delete_sound.setToolTip("Delete selected sound")
        self.btn_delete_sound.clicked.connect(self.delete_custom_sound)
        sound_layout.addWidget(self.btn_delete_sound)
        
        main_layout.addLayout(sound_layout)
        
        self.volume_container = QWidget()
        vol_layout = QHBoxLayout(self.volume_container)
        vol_layout.setContentsMargins(0, 0, 0, 0)
        vol_layout.setSpacing(12) 
        
        self.unmute_volume = app_settings.get("volume", 100)
        
        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mute.setFixedSize(30, 30)
        self.btn_mute.setObjectName("MuteButton")
        self.btn_mute.clicked.connect(self.toggle_mute)
        vol_layout.addWidget(self.btn_mute)
        
        self.slider_vol = JumpSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(app_settings.get("volume", 100))
        self.slider_vol.valueChanged.connect(self.on_volume_change)
        vol_layout.addWidget(self.slider_vol, 1)
        
        self.txt_vol = QLineEdit(str(self.slider_vol.value()))
        self.txt_vol.setValidator(QIntValidator(0, 100))
        self.txt_vol.setFixedWidth(40) 
        self.txt_vol.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.txt_vol.setObjectName("VolumeText")
        self.txt_vol.textEdited.connect(self.on_volume_typed)
        vol_layout.addWidget(self.txt_vol)
        
        main_layout.addWidget(self.volume_container)

        self.refresh_sound_dropdown()
        self.on_volume_change(self.slider_vol.value())

        main_layout.addWidget(QLabel("Discord Webhook:", styleSheet="font-weight: bold; margin-top: 5px;"))
        self.txt_webhook = QLineEdit(app_settings.get("discord_webhook", ""))
        self.txt_webhook.setPlaceholderText("https://discord.com/api/webhooks/...")
        self.txt_webhook.textChanged.connect(self.save_settings)
        main_layout.addWidget(self.txt_webhook)

        signals.update_available.connect(self.prompt_user_for_update)
        threading.Thread(target=check_for_updates_silently, daemon=True).start()

        ep_layout = QHBoxLayout()
        st_layout = QVBoxLayout()
        st_layout.addWidget(QLabel("Start Episode", styleSheet="font-weight: bold;"))
        self.txt_start = QLineEdit("1")
        self.txt_start.setValidator(QIntValidator(1, 99999))
        self.txt_start.textEdited.connect(self.save_settings)
        st_layout.addWidget(self.txt_start)
        
        en_layout = QVBoxLayout()
        en_layout.addWidget(QLabel("End Episode", styleSheet="font-weight: bold;"))
        self.txt_end = QLineEdit("1")
        self.txt_end.setValidator(QIntValidator(1, 99999))
        self.txt_end.textEdited.connect(self.save_settings)
        en_layout.addWidget(self.txt_end)
        
        ep_layout.addLayout(st_layout)
        ep_layout.addLayout(en_layout)
        main_layout.addLayout(ep_layout)
        
        main_layout.addStretch()

        self.btn_start = QPushButton("⬇ Start Download")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setObjectName("Primary")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self.start_task)
        main_layout.addWidget(self.btn_start)

        self.scroll.setWidget(self.content)
        outer_layout.addWidget(self.scroll)

        signals.update_buttons.connect(self.set_buttons)
        self.refresh_dropdown()

    def refresh_sound_dropdown(self):
        self.combo_sound.blockSignals(True)
        self.combo_sound.clear()
        sounds = app_settings.get("custom_sounds", [])
        if not sounds:
            self.combo_sound.addItem("No sounds added...")
            self.btn_play_sound.hide()
            self.btn_delete_sound.hide()
            self.volume_container.hide()
        else:
            self.btn_play_sound.show()
            self.btn_delete_sound.show()
            self.volume_container.show()
            for s in sounds:
                self.combo_sound.addItem(os.path.basename(s), s)
            
            selected = app_settings.get("selected_sound", "")
            if selected in sounds:
                self.combo_sound.setCurrentIndex(sounds.index(selected))
            else:
                self.combo_sound.setCurrentIndex(0)
                app_settings["selected_sound"] = sounds[0] if sounds else ""
        self.combo_sound.blockSignals(False)

    def on_sound_change(self, index):
        sounds = app_settings.get("custom_sounds", [])
        if sounds and index >= 0 and index < len(sounds):
            app_settings["selected_sound"] = self.combo_sound.itemData(index)
            self.save_settings()

    def browse_custom_sound(self):
        file_paths, _ = QFileDialog.getOpenFileNames(self, "Select Custom Sound(s)", "", "Audio Files (*.wav *.mp3 *.ogg *.m4a *.wma)")
        if file_paths:
            for file_path in file_paths:
                norm_path = os.path.normpath(file_path)
                if norm_path not in app_settings["custom_sounds"]:
                    app_settings["custom_sounds"].append(norm_path)
            
            app_settings["selected_sound"] = os.path.normpath(file_paths[-1])
            self.refresh_sound_dropdown()
            self.save_settings()
            
    def delete_custom_sound(self):
        sounds = app_settings.get("custom_sounds", [])
        selected = app_settings.get("selected_sound", "")
        
        if not sounds or not selected: return
            
        reply = QMessageBox.question(self, "Remove Sound", 
                                     f"Are you sure you want to remove '{os.path.basename(selected)}' from your sound list?", 
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            if selected in sounds:
                sounds.remove(selected)
            app_settings["custom_sounds"] = sounds
            
            if sounds:
                app_settings["selected_sound"] = sounds[-1]
            else:
                app_settings["selected_sound"] = ""
                
            self.refresh_sound_dropdown()
            self.save_settings()

    def preview_sound(self):
        path = app_settings.get("selected_sound", "")
        if path and os.path.exists(path):
            try:
                vol = app_settings.get("volume", 100) * 10 
                ctypes.windll.winmm.mciSendStringW('close custom_audio', None, 0, None)
                ctypes.windll.winmm.mciSendStringW(f'open "{path}" alias custom_audio', None, 0, None)
                ctypes.windll.winmm.mciSendStringW(f'setaudio custom_audio volume to {vol}', None, 0, None)
                ctypes.windll.winmm.mciSendStringW('play custom_audio', None, 0, None)
            except Exception as e:
                QMessageBox.warning(self, "Playback Error", f"Could not play sound: {e}")
        else:
            QMessageBox.warning(self, "No Sound", "Please select or add a valid custom audio file first.")

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
        if value == 0: self.btn_mute.setText("🔇")
        elif value < 50: self.btn_mute.setText("🔉")
        else: self.btn_mute.setText("🔊")
        self.save_settings()

    def save_settings(self):
        app_settings["headless"] = self.chk_headless.isChecked()
        if hasattr(self, 'txt_webhook'):
            app_settings["discord_webhook"] = self.txt_webhook.text().strip()
        if hasattr(self, 'slider_vol'):
            app_settings["volume"] = self.slider_vol.value()
            
        site = self.combo_site.currentText()
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
        if site == "No Profiles":
            QMessageBox.warning(self, "Error", "Please set up a website profile first.")
            return
        try:
            start_ep = int(self.txt_start.text())
            end_ep = int(self.txt_end.text())
        except ValueError: 
            QMessageBox.warning(self, "Error", "Please enter valid numbers for the episodes.")
            return
            
        if start_ep > end_ep: 
            QMessageBox.warning(self, "Error", "Start episode must be less than End.")
            return
            
        episodes_list = list(range(start_ep, end_ep + 1))
        
        target_dir = app_settings["download_dir"]
        headless = self.chk_headless.isChecked()
        webhook = self.txt_webhook.text().strip()
        selected_sound = app_settings.get("selected_sound", "")
        volume = app_settings.get("volume", 100)
        
        signals.update_buttons.emit(False, True, False)
        signals.task_started.emit()
        threading.Thread(target=run_selenium_task, args=(site, episodes_list, target_dir, headless, webhook, selected_sound, volume), daemon=True).start()

    def prompt_user_for_update(self, new_version, download_url):
        # Create and show our beautiful dark-themed popup
        dialog = UpdateDialog(new_version, self)
        
        # If the user clicks "Download", it returns QDialog.DialogCode.Accepted
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_update_process(download_url)

    def start_update_process(self, download_url):
        signals.update_status.emit("Status: Downloading new version... Please wait.", "#f39c12")
        
        import sys
        import os
        import tempfile
        import urllib.request
        import subprocess
        import shutil
        
        try:
            # 1. Download the new version
            temp_dir = tempfile.gettempdir()
            temp_download_path = os.path.join(temp_dir, "anime_update_new.exe")
            req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as response, open(temp_download_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
                
            current_exe = sys.executable
            old_exe = current_exe + ".old"
            
            # 2. THE PROCESS NUKE: Destroy all child processes so Windows unlocks the .old file!
            kill_stuck_chrome_processes() # Kills any lingering visible/hidden browsers
            for zombie in ["chromedriver.exe", "selenium-manager.exe", "aria2c.exe", "unrar.exe"]:
                subprocess.run(f"taskkill /F /IM {zombie} /T", shell=True, creationflags=0x08000000, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            
            # 3. Create the Batch Script
            bat_path = os.path.join(temp_dir, "install_update.bat")
            bat_content = f"""@echo off
:: Give the app a moment to start closing
timeout /t 3 /nobreak > NUL

:RETRY
:: Try to delete any leftover .old backup
if exist "{old_exe}" del /f /q "{old_exe}" > NUL 2>&1

:: Try to rename the running app to .old
if exist "{current_exe}" (
    move /y "{current_exe}" "{old_exe}" > NUL 2>&1
)

:: If the current app still exists, Python hasn't released the lock yet! Wait 1 sec and retry.
if exist "{current_exe}" (
    timeout /t 1 /nobreak > NUL
    goto RETRY
)

:: Move the newly downloaded file into place
move /y "{temp_download_path}" "{current_exe}" > NUL 2>&1

:: Launch using Windows Explorer (Destroys PyInstaller ghost variables)
explorer.exe "{current_exe}"

:: Self-destruct this script cleanly
(goto) 2>nul & del "%~f0"
"""
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat_content)
                
            # 4. Launch the Batch script
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
            
            # 5. Tell the app to shut down cleanly
            os._exit(0)
            
        except Exception as e:
            QMessageBox.critical(self, "Update Error", f"Failed to apply update: {e}")
            signals.update_status.emit("Status: ❌ Update Failed.", "#e74c3c")

# ==========================================
#     NEW PROGRESS WIDGET (LIVE UI ONLY)
# ==========================================

class WinUISpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(70, 70)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_spin)
        self.timer.start(12) # ~80 FPS for buttery smooth spinning

    def update_spin(self):
        self.angle = (self.angle + 5) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(5, 5, self.width() - 10, self.height() - 10)
        
        # Background track
        pen_bg = QPen(QColor(255, 255, 255, 15))
        pen_bg.setWidth(5)
        painter.setPen(pen_bg)
        painter.drawEllipse(rect)
        
        # Spinning arc
        pen_fg = QPen(QColor("#4cc2ff"))
        pen_fg.setWidth(5)
        pen_fg.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        painter.drawArc(rect, int(-self.angle * 16), int(100 * 16)) 
        painter.end()

class WinUICheckmark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(90, 90)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Green Circle
        painter.setBrush(QColor("#2ecc71"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())
        
        # White Checkmark
        pen = QPen(QColor("white"))
        pen.setWidth(7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        
        painter.drawLine(25, 45, 40, 60)
        painter.drawLine(40, 60, 65, 30)
        painter.end()

# ==========================================
#     NEW PROGRESS WIDGET (LIVE UI ONLY)
# ==========================================
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
        self.spinner = WinUISpinner()
        l_layout.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        lbl_wait = QLabel("Downloading the episodes...", styleSheet="color: #aaaaaa; margin-top: 15px; font-size: 16px;")
        l_layout.addWidget(lbl_wait, alignment=Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page_loading)

        # --- PAGE 1: Active Downloads ---
        self.page_active = QWidget()
        a_layout = QVBoxLayout(self.page_active)
        a_layout.setContentsMargins(0,0,0,0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("StepScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("StepScrollContent")
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

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()

        self.lbl_prog = QLabel("")
        self.lbl_prog.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_prog.hide()

        layout.addWidget(self.progress)
        layout.addWidget(self.lbl_prog)

        self.btn_layout = QHBoxLayout()
        
        self.btn_pause = QPushButton("⏸ Stop")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setMinimumHeight(40)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.pause_task)
        
        self.btn_resume = QPushButton("▶ Resume")
        self.btn_resume.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_resume.setMinimumHeight(40)
        self.btn_resume.setObjectName("Primary") 
        self.btn_resume.hide() 
        self.btn_resume.clicked.connect(self.resume_task)

        self.btn_cancel = QPushButton("✖ Cancel downloading")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setObjectName("Danger") 
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

    def reset_ui(self):
        for ep_num in list(self.active_cards.keys()):
            self.remove_active_card(ep_num)
        self.stack.setCurrentIndex(0) # Show Loading Spinner
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
            self.stack.setCurrentIndex(1) # Switch to Active list once data flows
            
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 10, 15, 10)
        
        header_layout = QHBoxLayout()
        title = QLabel(f"Downloading Episode {ep_num}...")
        title.setStyleSheet("font-weight: bold;")
        stats = QLabel("Initiating...")
        stats.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        
        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(stats)
        
        pbar = QProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(0)
        pbar.setTextVisible(False)
        
        layout.addLayout(header_layout)
        layout.addWidget(pbar)
        
        self.active_tasks_layout.addWidget(card)
        self.active_cards[ep_num] = {"widget": card, "stats": stats, "pbar": pbar}

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
        if start_en: 
            self.btn_resume.hide()
            self.btn_pause.show()

    def show_success(self):
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
        # Aggressively kill Aria2c to force it to release the thread and pause immediately
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
        pause_event.clear() # Unblock it if it was paused!
        finish_event.set()
        # Kill aria2c immediately to prevent UI freezing
        subprocess.run("taskkill /F /IM aria2c.exe /T", shell=True, creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ==========================================
#     MAIN APP WINDOW (TAB CONTROLLER)
# ==========================================
class AppWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Anime Episodes Downloader | version: {APP_VERSION}")
        
        if os.path.exists("icon.png"):
            app_icon = QIcon("icon.png")
        else:
            app_icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            
        self.setWindowIcon(app_icon)
        self.resize(850, 800)
        self.setMinimumSize(800, 800)
        
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(app_icon)
        
        tray_menu = QMenu()
        options_action = QAction("Show", self)
        options_action.triggered.connect(self.showNormal)
        tray_menu.addAction(options_action)
        tray_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.force_quit)
        tray_menu.addAction(exit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        self.tray_icon.activated.connect(self.tray_icon_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        self.downloader_tab = DownloaderWidget()
        self.manager_tab = SiteManagerWidget()
        self.history_tab = HistoryWidget() 
        self.progress_tab = ProgressTab()
        
        self.tabs.addTab(self.downloader_tab, "⬇ Downloader")
        self.tabs.addTab(self.manager_tab, "⚙ Profile manager")
        self.tabs.addTab(self.history_tab, "📖 History")
        self.tabs.addTab(self.progress_tab, "🚀 Active Downloads")
        
        self.tabs.setTabVisible(3, False) # Hidden until download starts!
        self.is_downloading = False
        
        self.manager_tab.profile_saved_signal.connect(self.downloader_tab.refresh_dropdown)
        self.tabs.currentChanged.connect(self.sync_tabs)
        
        signals.task_started.connect(self.show_progress_tab)
        signals.task_finished.connect(self.handle_task_finished)
        
        threading.Thread(target=ensure_ublock_lite, daemon=True).start()
        threading.Thread(target=ensure_unrar, daemon=True).start()
        threading.Thread(target=ensure_aria2c, daemon=True).start()
    
    def show_progress_tab(self):
        self.is_downloading = True
        self.tabs.setTabVisible(3, True)
        self.tabs.setCurrentIndex(3)

    def hide_progress_tab(self):
        if not self.is_downloading:
            self.tabs.setTabVisible(3, False)
            if self.tabs.currentIndex() == 3:
                self.tabs.setCurrentIndex(0)

    def handle_task_finished(self, failed_eps):
        self.is_downloading = False
        if failed_eps and not cancel_event.is_set():
            msg = QMessageBox(self)
            msg.setWindowTitle("Unfinished episodes")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setStyleSheet("QLabel { color: #ffffff; font-size: 14px; }")
            
            if len(failed_eps) == 1:
                msg.setText(f"{len(failed_eps)} episode failed or was cancelled.")
                msg.setInformativeText(f"Episode: {failed_eps[0]}\n\nPartial progress has been saved! Would you like to resume downloading it now?")
            else:
                msg.setText(f"{len(failed_eps)} episodes failed or were cancelled.")
                msg.setInformativeText(f"Episodes: [{', '.join(map(str, failed_eps))}]\n\nPartial progress has been saved! Would you like to resume downloading them now?")

            btn_yes = msg.addButton("Resume Now", QMessageBox.ButtonRole.AcceptRole) # Changed text
            btn_yes.setCursor(Qt.CursorShape.PointingHandCursor)
            
            btn_no = msg.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            btn_no.setCursor(Qt.CursorShape.PointingHandCursor)
            
            msg.exec()
            
            if msg.clickedButton() == btn_yes:
                site = self.downloader_tab.combo_site.currentText()
                target_dir = app_settings["download_dir"]
                headless = self.downloader_tab.chk_headless.isChecked()
                webhook = self.downloader_tab.txt_webhook.text().strip()
                selected_sound = app_settings.get("selected_sound", "")
                volume = app_settings.get("volume", 100)
                
                signals.update_buttons.emit(False, True, False)
                signals.task_started.emit()
                threading.Thread(target=run_selenium_task, args=(site, failed_eps, target_dir, headless, webhook, selected_sound, volume), daemon=True).start()            
            else:
                if not cancel_event.is_set():
                    self.progress_tab.show_success()
                QTimer.singleShot(3000, self.hide_progress_tab)
        else:
            if not cancel_event.is_set():
                self.progress_tab.show_success()
            QTimer.singleShot(3000, self.hide_progress_tab)

    def sync_tabs(self, index):
        if index == 1: 
            selected_site = self.downloader_tab.combo_site.currentText()
            if selected_site and selected_site != "No Profiles":
                self.manager_tab.profile_combo.setCurrentText(selected_site)
        elif index == 2:
            signals.history_updated.emit()

    def tray_icon_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger or reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()
            self.activateWindow()
            self.raise_()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        
    def force_quit(self):
        self.tray_icon.hide()
        cancel_event.set()
        finish_event.set()
        threading.Thread(target=kill_stuck_chrome_processes, daemon=True).start()
        QApplication.instance().quit()


if __name__ == "__main__":
    import sys
    import os
    import time
    import threading
    
    def cleanup_old_exe():
        if getattr(sys, 'frozen', False):
            old_exe_path = sys.executable + ".old"
            
            for attempt in range(30):
                try:
                    os.chmod(old_exe_path, 0o777) # Strip Read-Only
                    os.remove(old_exe_path)
                    break
                except Exception:
                    time.sleep(2)

    import threading
    threading.Thread(target=cleanup_old_exe, daemon=True).start()
    # -------------------------------------

    init_db()
    load_config()
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    
    check_icon, arrow_icon = generate_ui_icons()
    final_qss = WIN11_QSS.replace("ICON_CHECK", check_icon).replace("ICON_ARROW", arrow_icon)
    app.setStyleSheet(final_qss)
    
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)
        
    window = AppWindow()
    window.show()
    sys.exit(app.exec())