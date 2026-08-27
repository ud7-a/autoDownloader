import os
import json
import threading

# --- GLOBAL CONSTANTS ---
# Everything the app persists lives under APP_DIR. Setting AED_APP_DIR before import
# redirects ALL of it (config, history DB, browser profile, tools) somewhere else --
# tests point it at a temp folder so they can never read or overwrite real user data.
DEFAULT_APP_DIR = r"C:\Auto Episodes Downloader"
APP_DIR = os.environ.get("AED_APP_DIR") or DEFAULT_APP_DIR
IS_ISOLATED = APP_DIR != DEFAULT_APP_DIR
CONFIG_FILE = os.path.join(APP_DIR, "sites_config.json")
PROFILE_DIR = os.path.join(APP_DIR, "SeleniumProfile")
DB_FILE = os.path.join(APP_DIR, "download_history.db")
UNRAR_PATH = os.path.join(APP_DIR, "unrar.exe")
ARIA2C_PATH = os.path.join(APP_DIR, "aria2c.exe")
APP_VERSION = "4.3.3"

# --- GLOBAL LOCKS ---
config_lock = threading.RLock()
progress_lock = threading.RLock()

# --- GLOBAL STATE ---
sites_data = {}
app_settings = {
    "download_dir": os.path.join(os.environ.get('USERPROFILE', ''), "Downloads"),
    "headless": True,
    "discord_webhook": "",
    "last_profile": "",
    "custom_sounds": [],    
    "selected_sound": "",   
    "volume": 100,
    "window_width": 1100,
    "window_height": 800,
    "window_x": -1,
    "window_y": -1,
    "window_maximized": False,
    "unfinished_session": None,
    "captcha_provider": "Disabled",
    "captcha_api_key": "",
    # Auto-tune how many episodes download at once from measured speed; the
    # "concurrency" value above stays as the manual setting and the starting point.
    "concurrency_auto": True,
    # Followed anime for the new-episode watcher. Each entry:
    # {"title", "url", "domain", "seen_max", "latest_template", "latest_max", "checked"}
    "watchlist": [],
}

def encrypt_webhook(url):
    if not url: return ""
    import base64
    try:
        # Base64 encode the string, then reverse it for safe obfuscation
        encoded = base64.b64encode(url.encode("utf-8")).decode("utf-8")
        return encoded[::-1]
    except Exception:
        return url

def decrypt_webhook(obfuscated):
    if not obfuscated: return ""
    import base64
    try:
        # Reverse back, then base64 decode it
        reversed_back = obfuscated[::-1]
        return base64.b64decode(reversed_back.encode("utf-8")).decode("utf-8")
    except Exception:
        # Fallback if cleartext is entered directly
        return obfuscated

def load_config():
    if not os.path.exists(APP_DIR):
        os.makedirs(APP_DIR, exist_ok=True)
        
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                sites_data.update(data.get("sites", {}))

                needs_save = False
                for _, site_config in sites_data.items():
                    if "steps" in site_config and "step_paths" not in site_config:
                        site_config["step_paths"] = {"Path 1": site_config["steps"]}
                        del site_config["steps"]
                        needs_save = True
                if needs_save: save_config()

                saved_settings = data.get("settings", {})
                for k in app_settings.keys():
                    if k in saved_settings:
                        app_settings[k] = saved_settings[k]
                        
                # Decrypt webhook back to cleartext in-memory
                if app_settings.get("discord_webhook"):
                    app_settings["discord_webhook"] = decrypt_webhook(app_settings["discord_webhook"])
                        
                if "custom_sound_path" in saved_settings and saved_settings["custom_sound_path"]:
                    old_path = saved_settings["custom_sound_path"]
                    if old_path not in app_settings["custom_sounds"]:
                        app_settings["custom_sounds"].append(old_path)
                    if not app_settings.get("selected_sound"):
                        app_settings["selected_sound"] = old_path
                        
        except Exception as e: 
            print(f"Error loading config: {e}")
    else: 
        save_config()

# --- Watchlist helpers (followed anime for the new-episode watcher) ---
def get_watchlist():
    with config_lock:
        return list(app_settings.get("watchlist", []))

def find_watch(url):
    with config_lock:
        for w in app_settings.get("watchlist", []):
            if w.get("url") == url:
                return w
    return None

def add_watch(entry):
    """Add a followed anime (keyed by url). Returns False if already followed."""
    with config_lock:
        wl = app_settings.setdefault("watchlist", [])
        if any(w.get("url") == entry.get("url") for w in wl):
            return False
        wl.append(entry)
    save_config()
    return True

def remove_watch(url):
    with config_lock:
        wl = app_settings.get("watchlist", [])
        app_settings["watchlist"] = [w for w in wl if w.get("url") != url]
    save_config()

def update_watch(url, **fields):
    with config_lock:
        for w in app_settings.get("watchlist", []):
            if w.get("url") == url:
                w.update(fields)
                break
    save_config()


def save_config():
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with config_lock:
            # Create a safe deep copy to obfuscate webhook on-disk only
            settings_to_save = dict(app_settings)
            if settings_to_save.get("discord_webhook"):
                settings_to_save["discord_webhook"] = encrypt_webhook(settings_to_save["discord_webhook"])

            # Transient profiles (e.g. one-off Watchlist downloads) live in memory
            # only -- never persist them to disk.
            sites_to_save = {k: v for k, v in sites_data.items() if not v.get("_transient")}

            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"settings": settings_to_save, "sites": sites_to_save}, f, indent=4, ensure_ascii=False)
    except Exception as e:
        # A disk/permission failure must never crash a UI slot that called save.
        print(f"Error saving config: {e}")