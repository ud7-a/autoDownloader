import os
import json
import sys
import threading
import time

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
APP_VERSION = "4.4.0"
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

    # 2. Sync watchlist immediately. Recovery is disabled for this call: we have just
    #    registered, so a 401 here is not something re-registering again would fix.
    ok, msg = cloud_sync_watchlist(_allow_recovery=False)
    if not ok:
        return True, f"Registered on cloud, but initial sync had an issue: {msg}"
    return True, f"Successfully registered and synced {len(get_watchlist())} anime with cloud service!"


_cloud_log_seen = {}
_CLOUD_LOG_REPEAT_AFTER = 300.0


def _cloud_log(message: str) -> None:
    """Record a cloud-sync problem somewhere a person can actually find it.

    These calls used to fail completely silently: cloud_fetch_commands caught every
    exception and returned [], so a rotated token looked exactly like "no commands
    waiting". A remote download would simply never arrive, with no clue anywhere on
    the machine -- which is precisely how a stale in-memory token went unnoticed.

    Repeats are collapsed per message, not just against the previous one. The poller
    heartbeats and fetches every 20 seconds, so during an outage two different
    messages alternate -- tracking only the last one suppressed nothing and still
    wrote thousands of lines a day.

    Best effort throughout: a logging failure must never break a download.
    """
    now = time.time()
    if now - _cloud_log_seen.get(message, 0.0) < _CLOUD_LOG_REPEAT_AFTER:
        return
    _cloud_log_seen[message] = now
    if len(_cloud_log_seen) > 200:
        # Unbounded only if every failure is unique; drop what is no longer suppressing.
        for key, seen_at in list(_cloud_log_seen.items()):
            if now - seen_at >= _CLOUD_LOG_REPEAT_AFTER:
                del _cloud_log_seen[key]
    try:
        path = os.path.join(APP_DIR, "cloud.log")
        # Keep at most two files so a long outage cannot fill the disk.
        try:
            if os.path.getsize(path) > 512_000:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def cloud_recover_identity() -> bool:
    """Re-register after the service has forgotten us, returning whether it worked.

    The service can lose its database. Recovery is a plain re-registration: subscriber
    ids are derived from the webhook, so registering again with the same webhook
    returns the same identity and a fresh token. Possession of the webhook is the
    proof -- the server must never simply believe an id it is handed.
    """
    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
        wh_url = app_settings.get("discord_webhook") or ""
    if not s_url or not wh_url:
        return False
    ok, _msg = cloud_register_and_sync(s_url, wh_url)
    return bool(ok)


def cloud_request_with_recovery(do_request):
    """Run a service call; on 401, re-register once and try again.

    `do_request` takes (subscriber_id, token) and raises urllib.error.HTTPError on a
    non-2xx response. A 401 means the service no longer knows us -- almost always
    because it lost its database -- so we prove who we are and retry exactly once.
    There is no second retry: if re-registration succeeded and the call is still
    rejected, the problem is not identity, and looping would hide it.
    """
    import urllib.error

    with config_lock:
        sid = app_settings.get("cloud_subscriber_id") or ""
        token = app_settings.get("cloud_token") or ""
    try:
        return do_request(sid, token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    if not cloud_recover_identity():
        raise RuntimeError("cloud service rejected our credentials and re-registration failed")
    with config_lock:
        sid = app_settings.get("cloud_subscriber_id") or ""
        token = app_settings.get("cloud_token") or ""
    return do_request(sid, token)


def cloud_sync_watchlist(_allow_recovery: bool = True) -> tuple[bool, str]:
    """Pushes the current watchlist to the registered cloud service.

    `_allow_recovery` exists to break a cycle: a 401 here triggers re-registration,
    and registration finishes by syncing the watchlist. Without the guard a service
    that keeps answering 401 would drive sync -> register -> sync -> register with no
    limit. cloud_register_and_sync passes False for exactly that reason.
    """
    import urllib.request
    import urllib.error

    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
        sub_id = app_settings.get("cloud_subscriber_id")
        token = app_settings.get("cloud_token")
        watchlist = list(app_settings.get("watchlist", []))

    if not s_url or not sub_id or not token:
        return False, "Cloud sync is not registered or configured"

    items = [{
        "url": w.get("url", ""),
        "title": w.get("title", ""),
        "release_day": w.get("release_day", ""),
        "seen_max": int(w.get("seen_max") or 0)
    } for w in watchlist if w.get("url")]
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
    except urllib.error.HTTPError as he:
        if he.code in (401, 404) and _allow_recovery:
            # The service no longer knows us -- almost always because it lost its
            # database. Prove who we are by re-registering with the webhook; that
            # call syncs the watchlist itself, so there is nothing to retry here.
            if cloud_recover_identity():
                return True, "Re-registered with the cloud service and synced"
            return False, "Cloud rejected our credentials and re-registration failed"
        return False, f"Cloud sync failed: HTTP {he.code}"
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


def is_windows_autostart_enabled() -> bool:
    """Checks if the app is registered in Windows HKCU Run key."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "AutoEpisodesDownloader")
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_windows_autostart(enabled: bool) -> bool:
    """Registers or unregisters the lightweight watcher in Windows HKCU Run key."""
    if sys.platform != "win32":
        return False
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --watcher'
                else:
                    watcher_pyw = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "aed_watcher.pyw"))
                    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                    if not os.path.exists(pythonw):
                        pythonw = sys.executable
                    cmd = f'"{pythonw}" "{watcher_pyw}"'
                winreg.SetValueEx(key, "AutoEpisodesDownloader", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "AutoEpisodesDownloader")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        print(f"Failed to set Windows autostart: {e}")
        return False


def cloud_send_heartbeat() -> bool:
    """Sends a lightweight online heartbeat to the cloud backend."""
    import urllib.request
    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
    if not s_url:
        return False

    def _send(sub_id, token):
        if not (sub_id and token):
            raise RuntimeError("not registered with the cloud service")
        req = urllib.request.Request(
            f"{s_url}/v1/subscribers/{sub_id}/heartbeat",
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5):
            return True

    try:
        return cloud_request_with_recovery(_send)
    except Exception as e:
        _cloud_log(f"heartbeat failed: {type(e).__name__}: {e}")
        return False


def cloud_fetch_commands() -> list[dict]:
    """Fetches any pending remote download commands from the cloud queue."""
    import urllib.request
    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
    if not s_url:
        return []

    def _fetch(sub_id, token):
        if not (sub_id and token):
            raise RuntimeError("not registered with the cloud service")
        req = urllib.request.Request(
            f"{s_url}/v1/subscribers/{sub_id}/commands",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("commands", [])

    try:
        return cloud_request_with_recovery(_fetch)
    except Exception as e:
        # This is the one that hid a stale token: an empty list is indistinguishable
        # from "no commands waiting", so a broken remote download looked like nothing
        # had been queued at all.
        _cloud_log(f"fetch commands failed: {type(e).__name__}: {e}")
        return []


def cloud_ack_command(command_id: int) -> bool:
    """Acknowledges execution of a remote download command to the cloud."""
    import urllib.request
    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
    if not s_url:
        return False

    def _ack(sub_id, token):
        if not (sub_id and token):
            raise RuntimeError("not registered with the cloud service")
        req = urllib.request.Request(
            f"{s_url}/v1/subscribers/{sub_id}/commands/{command_id}/ack",
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5):
            return True

    try:
        return cloud_request_with_recovery(_ack)
    except Exception as e:
        # A failed ack means the command stays pending and will be executed again on
        # the next poll, so it is worth seeing rather than guessing at duplicates.
        _cloud_log(f"ack command {command_id} failed: {type(e).__name__}: {e}")
        return False


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