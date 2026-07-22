import os
import sys
import shutil
import urllib.request
import ssl
from utils.config import UNRAR_PATH, ARIA2C_PATH

def _bundle_dir():
    """Directory holding the bundled tools: the frozen app's extract dir, or the repo when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "tools")
    # Running from source: <repo>/tools (this file is <repo>/utils/tools_manager.py)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "tools")

def restore_from_bundle(filename, dest_path, min_size=100000):
    """Copy a tool shipped inside the installer to its runtime location. Returns True on success."""
    src = os.path.join(_bundle_dir(), filename)
    if not os.path.exists(src) or os.path.getsize(src) < min_size:
        return False
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(src, dest_path)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) >= min_size
    except Exception:
        return False

def download_file_safely(url, path, headers=None, timeout=15):
    """Downloads a file safely, falling back to unverified SSL only if verification fails."""
    if headers is None:
        headers = {'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)
    try:
        # Tier 1: Try secure standard system SSL/TLS verification first
        with urllib.request.urlopen(req, timeout=timeout) as response, open(path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return True
    except Exception as first_err:
        first_err_str = str(first_err).lower()
        is_ssl_issue = any(word in first_err_str for word in ["ssl", "cert", "handshake", "verification", "untrusted"])
        
        if is_ssl_issue:
            # Tier 2: Targeted SSL Bypass (only trigger if verified check failed due to TLS validation error)
            try:
                context = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=context) as response, open(path, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)
                return True
            except Exception:
                try: os.remove(path)
                except: pass
        else:
            try: os.remove(path)
            except: pass
    return False

def ensure_unrar():
    if not os.path.exists(UNRAR_PATH) or os.path.getsize(UNRAR_PATH) < 100000:
        # Prefer the copy bundled with the installer; only download if the bundle is unavailable.
        if restore_from_bundle("unrar.exe", UNRAR_PATH):
            return
        url = "https://github.com/ud7-a/unrar/raw/refs/heads/main/UnRAR.exe"
        if download_file_safely(url, UNRAR_PATH):
            if os.path.exists(UNRAR_PATH) and os.path.getsize(UNRAR_PATH) < 100000:
                try: os.remove(UNRAR_PATH)
                except: pass

def ensure_aria2c():
    if not os.path.exists(ARIA2C_PATH) or os.path.getsize(ARIA2C_PATH) < 100000:
        # Prefer the copy bundled with the installer; only download if the bundle is unavailable.
        if restore_from_bundle("aria2c.exe", ARIA2C_PATH):
            return
        url = "https://github.com/ud7-a/files/raw/refs/heads/main/aria2c.exe"
        if download_file_safely(url, ARIA2C_PATH):
            if os.path.exists(ARIA2C_PATH) and os.path.getsize(ARIA2C_PATH) < 100000:
                try: os.remove(ARIA2C_PATH)
                except: pass