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
import psutil

APP_DIR = os.environ.get("AED_APP_DIR") or r"C:\Auto Episodes Downloader"
CONFIG_FILE = os.path.join(APP_DIR, "sites_config.json")
LOG_FILE = os.path.join(APP_DIR, "watcher.log")


def log(msg: str):
    """Appends status to watcher.log for instant debugging."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def is_main_app_running() -> bool:
    """Checks if the main AED UI process is currently running."""
    curr_pid = os.getpid()
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if p.info['pid'] == curr_pid:
                continue
            name = (p.info.get('name') or '').lower()
            if 'python' not in name and 'py' not in name and 'autodownloader' not in name:
                continue
            cmdline = ' '.join(p.info.get('cmdline') or [])
            if 'main.py' in cmdline and '--watcher' not in cmdline:
                return True
            if getattr(sys, 'frozen', False) and 'AutoDownloader' in name and '--watcher' not in cmdline:
                return True
        except Exception:
            pass
    return False


def is_other_watcher_running() -> bool:
    """Checks if another python instance of the watcher is running."""
    curr_pid = os.getpid()
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if p.info['pid'] == curr_pid:
                continue
            name = (p.info.get('name') or '').lower()
            if 'python' not in name and 'py' not in name:
                continue
            args = [str(a).lower() for a in (p.info.get('cmdline') or [])]
            if "-c" in args:
                continue  # Ignore inline python commands
            if any(a.endswith("aed_watcher.pyw") or a.endswith("aed_watcher") for a in args):
                return True
        except Exception:
            pass
    return False


def _decrypt_token(obfuscated: str) -> str:
    if not obfuscated:
        return ""
    import base64
    try:
        reversed_back = obfuscated[::-1]
        return base64.b64decode(reversed_back.encode("utf-8")).decode("utf-8")
    except Exception:
        return obfuscated


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
        raw_token = settings.get("cloud_token", "")
        token = _decrypt_token(raw_token)
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
    except Exception as e:
        log(f"Heartbeat failed: {e}")
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
    except Exception as e:
        log(f"Fetch commands failed: {e}")
    return []


def launch_main_app():
    """Launches the main AED UI to process commands and display Downloader."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log(f"Launching main app from {base_dir}...")
    if getattr(sys, "frozen", False):
        exe = sys.executable
        try:
            os.startfile(exe)
        except Exception:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
    else:
        main_py = os.path.join(base_dir, "main.py")
        python_exe = sys.executable
        if "pythonw.exe" in python_exe.lower():
            python_reg = os.path.join(os.path.dirname(python_exe), "python.exe")
            if os.path.exists(python_reg):
                python_exe = python_reg
        try:
            # os.startfile invokes ShellExecute directly on the interactive Windows desktop
            os.startfile(main_py)
        except Exception:
            subprocess.Popen([python_exe, main_py], cwd=base_dir)
    log("Main app subprocess spawned!")


def run_watcher(single_pass: bool = False):
    """Main watcher loop."""
    log(f"Watcher started (PID: {os.getpid()}, single_pass={single_pass})")
    if not single_pass and is_other_watcher_running():
        log("Another watcher is already active -> exiting cleanly.")
        return

    loop_count = 0
    while True:
        try:
            s_url, sub_id, token, enabled = load_cloud_credentials()
            if not (enabled and s_url and sub_id and token):
                log("Cloud notify not fully configured or disabled.")
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
            if send_heartbeat(s_url, sub_id, token):
                if loop_count % 4 == 0:
                    log(f"Heartbeat sent successfully (Loop #{loop_count}). PC is ONLINE 🟢")
            loop_count += 1

            # Check for remote download commands
            commands = fetch_commands(s_url, sub_id, token)
            if commands:
                log(f"Received {len(commands)} remote download command(s)! Triggering UI...")
                launch_main_app()
                if single_pass:
                    break
                time.sleep(15)

        except Exception as e:
            log(f"Unexpected loop exception: {e}")

        if single_pass:
            break
        time.sleep(15)


if __name__ == "__main__":
    run_watcher()
