"""
Lightweight Background Watcher Service for Auto Episodes Downloader.
RAM footprint: ~20 MB (< 100 MB).
CPU usage: 0.0%.

Functions:
- Runs silently in the background (zero window / console).
- Sends heartbeat to Cloud Notification Service every 15s so your PC is marked ONLINE 🟢 24/7.
- Checks for pending remote download commands triggered from Discord.
- When a command arrives, automatically launches the main AED app window to download it!
- If the main AED app is already open, steps aside and lets the main app handle it.
"""

import os
import sys
import time
import json
import subprocess
import urllib.request
import urllib.error
import ctypes

APP_DIR = os.environ.get("AED_APP_DIR") or r"C:\Auto Episodes Downloader"
CONFIG_FILE = os.path.join(APP_DIR, "sites_config.json")

MAIN_APP_MUTEX_NAME = "Local\\AED_Main_App_Running_Mutex"
WATCHER_MUTEX_NAME = "Local\\AED_Watcher_Running_Mutex"


def is_main_app_running() -> bool:
    """Checks if the main AED UI process is currently open via Windows Mutex."""
    if sys.platform != "win32":
        return False
    mutex = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, MAIN_APP_MUTEX_NAME)
    if mutex != 0:
        ctypes.windll.kernel32.CloseHandle(mutex)
        return True
    return False


def load_cloud_credentials() -> tuple[str, str, str, bool]:
    """Reads cloud config from sites_config.json."""
    if not os.path.exists(CONFIG_FILE):
        return "", "", "", False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = data.get("settings", {})
        enabled = bool(settings.get("cloud_notify_enabled", False))
        s_url = (settings.get("cloud_service_url") or "https://aed-notification-service.onrender.com").rstrip("/")
        sub_id = settings.get("cloud_subscriber_id", "")
        token = settings.get("cloud_token", "")
        return s_url, sub_id, token, enabled
    except Exception:
        return "", "", "", False


def send_heartbeat(s_url: str, sub_id: str, token: str) -> bool:
    endpoint = f"{s_url}/v1/subscribers/{sub_id}/heartbeat"
    req = urllib.request.Request(
        endpoint,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "AED-BackgroundWatcher/4.4.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def fetch_commands(s_url: str, sub_id: str, token: str) -> list[dict]:
    endpoint = f"{s_url}/v1/subscribers/{sub_id}/commands"
    req = urllib.request.Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "AED-BackgroundWatcher/4.4.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("commands", [])
    except Exception:
        pass
    return []


def launch_main_app():
    """Launches the main AED UI to process commands and display Downloader."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "frozen", False):
        exe = sys.executable
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    else:
        main_py = os.path.join(base_dir, "main.py")
        python_exe = sys.executable
        if "pythonw.exe" in python_exe.lower():
            # Use regular python or keep pythonw
            pass
        subprocess.Popen([python_exe, main_py], cwd=base_dir)


def run_watcher(single_pass: bool = False):
    """Main watcher loop."""
    watcher_mutex = None
    if sys.platform == "win32" and not single_pass:
        watcher_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, WATCHER_MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return

    while True:
        try:
            s_url, sub_id, token, enabled = load_cloud_credentials()
            if not (enabled and s_url and sub_id and token):
                if single_pass:
                    break
                time.sleep(15)
                continue

            # If main UI is active, let it handle heartbeats and commands
            if is_main_app_running():
                if single_pass:
                    break
                time.sleep(15)
                continue

            # Send heartbeat so PC is ONLINE 🟢
            send_heartbeat(s_url, sub_id, token)

            # Check for remote download commands
            commands = fetch_commands(s_url, sub_id, token)
            if commands:
                launch_main_app()
                if single_pass:
                    break
                time.sleep(15)

        except Exception:
            pass

        if single_pass:
            break
        time.sleep(15)


if __name__ == "__main__":
    run_watcher()
