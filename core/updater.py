import os
import sys
import json
import urllib.request
import subprocess
from PyQt6.QtCore import QThread, pyqtSignal
from core.signals import signals
from utils.config import APP_VERSION

class UpdateDownloaderThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, download_url):
        super().__init__()
        self.download_url = download_url

    def run(self):
        try:
            temp_dir = os.environ.get("TEMP", os.environ.get("TMP", "C:\\"))
            setup_path = os.path.join(temp_dir, "AutoDownloader_Setup.exe")
            
            def report(blocknum, blocksize, totalsize):
                if totalsize > 0:
                    percent = int(blocknum * blocksize * 100 / totalsize)
                    if percent > 100: percent = 100
                    self.progress.emit(percent)
                    
            urllib.request.urlretrieve(self.download_url, setup_path, reporthook=report)
            self.finished.emit(setup_path)
        except Exception as e:
            self.error.emit(str(e))

def launch_setup_and_exit(setup_path):
    try:
        # Launch the setup file in silent mode and exit
        subprocess.Popen([setup_path, "--silent"], close_fds=True)
        os._exit(0)
    except Exception as e:
        print(f"Failed to launch installer: {e}")

def check_for_updates_silently():
    API_URL = "https://api.github.com/repos/ud7-a/autoDownloader/releases/latest"
    try:
        req = urllib.request.Request(API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))

        latest_version = data.get('tag_name', '').replace("v", "")
        
        if latest_version and latest_version != APP_VERSION:
            for asset in data.get('assets', []):
                if "Setup" in asset['name'] and asset['name'].endswith('.exe'):
                    download_url = asset['browser_download_url']
                    signals.update_available.emit(latest_version, download_url)
                    break
    except Exception:
        pass