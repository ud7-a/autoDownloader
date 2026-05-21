import os
import shutil
import urllib.request
from utils.config import UNRAR_PATH, ARIA2C_PATH, UBLOCK_CRX_PATH

def ensure_updater_exe():
    """Silently downloads the updater.exe to the C: drive."""
    UPDATER_DIR = r"C:\Auto Episodes Downloader"
    UPDATER_PATH = os.path.join(UPDATER_DIR, "updater.exe")
    
    if not os.path.exists(UPDATER_DIR):
        try: os.makedirs(UPDATER_DIR)
        except Exception: pass 
            
    if not os.path.exists(UPDATER_PATH) or os.path.getsize(UPDATER_PATH) < 10000:
        try:
            url = "" # You can fill this in later when you upload the updater to GitHub
            if url:
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
        except Exception:
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
        except Exception: pass