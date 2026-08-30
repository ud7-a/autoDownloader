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
DEFAULT_CLOUD_SERVICE_URL = "https://aed-notification-service.onrender.com"

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
    # Cloud notification service (notifies via Discord when PC is off)
    "cloud_notify_enabled": False,
    "cloud_service_url": DEFAULT_CLOUD_SERVICE_URL,
    "cloud_subscriber_id": "",
    "cloud_token": "",
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
                        
                # Decrypt webhook and cloud token back to cleartext in-memory
                if app_settings.get("discord_webhook"):
                    app_settings["discord_webhook"] = decrypt_webhook(app_settings["discord_webhook"])
                if app_settings.get("cloud_token"):
                    app_settings["cloud_token"] = decrypt_webhook(app_settings["cloud_token"])
                        
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
    _trigger_bg_cloud_sync()
    return True

def remove_watch(url):
    with config_lock:
        wl = app_settings.get("watchlist", [])
        app_settings["watchlist"] = [w for w in wl if w.get("url") != url]
    save_config()
    _trigger_bg_cloud_sync()

def update_watch(url, **fields):
    with config_lock:
        for w in app_settings.get("watchlist", []):
            if w.get("url") == url:
                w.update(fields)
                break
    save_config()


# --- Cloud Notification Client ---
def _trigger_bg_cloud_sync():
    with config_lock:
        if not app_settings.get("cloud_notify_enabled") or not app_settings.get("cloud_subscriber_id"):
            return
    threading.Thread(target=cloud_sync_watchlist, daemon=True).start()

def cloud_register_and_sync(service_url: str = None, webhook_url: str = None) -> tuple[bool, str]:
    """Registers subscriber on the cloud backend and syncs current watchlist."""
    import urllib.request
    import urllib.error

    with config_lock:
        s_url = (service_url or app_settings.get("cloud_service_url") or "").rstrip("/")
        wh_url = webhook_url or app_settings.get("discord_webhook") or ""

    if not s_url:
        return False, "Cloud service URL is required"
    if not wh_url:
        return False, "Discord Webhook URL is required"

    # 1. Register subscriber
    reg_endpoint = f"{s_url}/v1/subscribers"
    payload = json.dumps({"webhook": wh_url}).encode("utf-8")
    req = urllib.request.Request(reg_endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            sub_id = data.get("id")
            token = data.get("token")
    except Exception as e:
        return False, f"Failed to register on cloud service: {e}"

    if not sub_id or not token:
        return False, "Invalid response from cloud service"

    with config_lock:
        app_settings["cloud_service_url"] = s_url
        app_settings["cloud_subscriber_id"] = sub_id
        app_settings["cloud_token"] = token
        app_settings["cloud_notify_enabled"] = True
    save_config()

    # 2. Sync watchlist immediately
    ok, msg = cloud_sync_watchlist()
    if not ok:
        return True, f"Registered on cloud, but initial sync had an issue: {msg}"
    return True, f"Successfully registered and synced {len(get_watchlist())} anime with cloud service!"

def cloud_sync_watchlist() -> tuple[bool, str]:
    """Pushes the current watchlist to the registered cloud service."""
    import urllib.request
    import urllib.error

    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
        sub_id = app_settings.get("cloud_subscriber_id")
        token = app_settings.get("cloud_token")
        watchlist = list(app_settings.get("watchlist", []))

    if not s_url or not sub_id or not token:
        return False, "Cloud sync is not registered or configured"

    items = [{"url": w.get("url", ""), "title": w.get("title", "")} for w in watchlist if w.get("url")]
    sync_endpoint = f"{s_url}/v1/subscribers/{sub_id}/watchlist"
    payload = json.dumps({"items": items}).encode("utf-8")
    req = urllib.request.Request(
        sync_endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        },
        method="PUT"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            count = data.get("following", len(items))
            return True, f"Synced {count} anime to cloud service"
    except Exception as e:
        return False, f"Cloud sync failed: {e}"

def cloud_unsubscribe() -> tuple[bool, str]:
    """Deletes subscriber registration from the cloud backend."""
    import urllib.request
    import urllib.error

    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
        sub_id = app_settings.get("cloud_subscriber_id")
        token = app_settings.get("cloud_token")

    if s_url and sub_id and token:
        del_endpoint = f"{s_url}/v1/subscribers/{sub_id}"
        req = urllib.request.Request(
            del_endpoint,
            headers={"Authorization": f"Bearer {token}"},
            method="DELETE"
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass  # Ignore network errors during deletion

    with config_lock:
        app_settings["cloud_notify_enabled"] = False
        app_settings["cloud_subscriber_id"] = ""
        app_settings["cloud_token"] = ""
    save_config()
    return True, "Cloud notifications disabled and removed from server"


def save_config():
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with config_lock:
            # Create a safe deep copy to obfuscate webhook and tokens on-disk only
            settings_to_save = dict(app_settings)
            if settings_to_save.get("discord_webhook"):
                settings_to_save["discord_webhook"] = encrypt_webhook(settings_to_save["discord_webhook"])
            if settings_to_save.get("cloud_token"):
                settings_to_save["cloud_token"] = encrypt_webhook(settings_to_save["cloud_token"])

            # Transient profiles (e.g. one-off Watchlist downloads) live in memory
            # only -- never persist them to disk.
            sites_to_save = {k: v for k, v in sites_data.items() if not v.get("_transient")}

            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"settings": settings_to_save, "sites": sites_to_save}, f, indent=4, ensure_ascii=False)
    except Exception as e:
        # A disk/permission failure must never crash a UI slot that called save.
        print(f"Error saving config: {e}")