import os
import sys
import json
import urllib.request
import subprocess
import shutil
from core.signals import signals
from utils.config import APP_VERSION

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

:: 2. Delete the previous .old file if it exists
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
        
    DETACHED_PROCESS = 0x00000008
    clean_env = os.environ.copy()
    keys_to_remove = [k for k in clean_env if k.startswith('_MEI') or k == 'PYTHONPATH']
    for k in keys_to_remove:
        clean_env.pop(k, None)
        
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path], 
        env=clean_env, 
        creationflags=DETACHED_PROCESS
    )
    os._exit(0)

def check_for_updates_silently():
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