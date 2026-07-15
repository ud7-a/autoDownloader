import os
import json
import threading

# --- GLOBAL CONSTANTS ---
APP_DIR = r"C:\Auto Episodes Downloader"
CONFIG_FILE = os.path.join(APP_DIR, "sites_config.json")
PROFILE_DIR = os.path.join(APP_DIR, "SeleniumProfile")
UBLOCK_CRX_PATH = os.path.join(APP_DIR, "ublock_lite.crx")
DB_FILE = os.path.join(APP_DIR, "download_history.db")
UNRAR_PATH = os.path.join(APP_DIR, "unrar.exe")
ARIA2C_PATH = os.path.join(APP_DIR, "aria2c.exe")
APP_VERSION = "4.0.1"

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
    "transparency": True,
    "window_width": 1100,
    "window_height": 800,
    "window_x": -1,
    "window_y": -1,
    "window_maximized": False,
    "unfinished_session": None,
    "captcha_provider": "Disabled",
    "captcha_api_key": ""
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
    global sites_data, app_settings
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

def save_config():
    os.makedirs(APP_DIR, exist_ok=True)
    with config_lock:
        # Create a safe deep copy to obfuscate webhook on-disk only
        settings_to_save = dict(app_settings)
        if settings_to_save.get("discord_webhook"):
            settings_to_save["discord_webhook"] = encrypt_webhook(settings_to_save["discord_webhook"])
            
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"settings": settings_to_save, "sites": sites_data}, f, indent=4, ensure_ascii=False)

def force_windows_transparency():
    try:
        import winreg
        registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(registry_key, "EnableTransparency")
        if value == 0:
            winreg.SetValueEx(registry_key, "EnableTransparency", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(registry_key)
    except Exception:
        pass