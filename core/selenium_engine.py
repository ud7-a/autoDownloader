import os
import time
import json
import shutil
import traceback
import ctypes
import re
import collections
import random
import tempfile
import subprocess
import threading
import urllib.request
from subprocess import CREATE_NO_WINDOW 

from core.signals import signals
from utils.config import PROFILE_DIR, ARIA2C_PATH, UNRAR_PATH, APP_DIR, sites_data, app_settings, config_lock, progress_lock
from utils.database import log_history

# --- GLOBAL THREAD EVENTS ---
CURRENT_TASK_ID = 0
finish_event = threading.Event()
cancel_event = threading.Event()
pause_event = threading.Event()
ep_pause_events = {}
ep_cancel_events = {}
ep_aria2_processes = {}
active_engine_threads = []

manual_driver = None
active_aria2_processes = []

# Per-host download throttle: never run more than PER_HOST_MAX aria2c downloads
# against the same host at once, so a single host isn't hammered into a rate-limit.
host_active = {}
host_lock = threading.Lock()
PER_HOST_MAX = 3

def _host_of(u):
    from urllib.parse import urlparse
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""

def get_download_cookies(driver, dl_url):
    """Collect the download host's cookies to hand to aria2c.

    The download is intercepted from the chrome://downloads tab, where
    driver.get_cookies() returns nothing (chrome:// pages have no cookies), so
    session-gated hosts (e.g. workupload's `token` cookie) would otherwise be
    lost and the server returns a small HTML block page instead of the file.
    Pull every cookie via CDP and keep those valid for the download URL's host,
    including parent-domain cookies (e.g. a .workupload.com cookie for
    f54.workupload.com).
    """
    from urllib.parse import urlparse
    host = (urlparse(dl_url).hostname or "").lower()
    try:
        all_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
    except Exception:
        all_cookies = driver.get_cookies()
    matched = []
    for c in all_cookies:
        cdom = (c.get("domain") or "").lstrip(".").lower()
        if cdom and (host == cdom or host.endswith("." + cdom)):
            matched.append(c)
    # Fall back to the old behaviour if nothing matched (harmless for hosts that
    # don't gate downloads on cookies).
    return matched or driver.get_cookies()

def is_block_page(path):
    """True if the downloaded file is too small to be a real video -- i.e. a block/
    error/rate-limit page saved in place of the file. Episodes are always > 1 MB."""
    try:
        return os.path.exists(path) and os.path.getsize(path) < 1_000_000
    except Exception:
        return False

def is_page_not_found(driver):
    """True if the current page is a 404 / not-found page (covers Arabic sites)."""
    try:
        t = driver.title or ""
        low = t.lower()
        return ("غير موجود" in t) or ("404" in low) or ("not found" in low)
    except Exception:
        return False

# Trailing "final episode" URL suffixes used by some Arabic sites (e.g. animerco:
# ...الحلقة-12-والاخيرة/). Tried when the plain episode URL 404s.
FINALE_SUFFIXES = ("-والاخيرة", "-والأخيرة", "-الاخيرة", "-الأخيرة")

def episode_url_variants(url):
    """Alternate episode URLs to try when the primary one is a not-found page.

    Handles sites that split a single series across two URL slug patterns -- e.g.
    animerco serves Bleach eps 1-62 as /episodes/انمي-bleach-الحلقة-N/ and eps
    63-366 as /episodes/bleach-الحلقة-N/ -- by toggling the Arabic 'anime' (انمي-)
    prefix, and sites that give the finale a special suffix, by appending it.
    """
    prefix_toggled = None
    m = re.search(r"^(.*/episodes/)(.+?)(/?)$", url)
    if m:
        head, slug, tail = m.group(1), m.group(2), m.group(3)
        if slug.startswith("انمي-"):
            prefix_toggled = head + slug[len("انمي-"):] + tail
        else:
            prefix_toggled = head + "انمي-" + slug + tail

    variants = []
    # Prefix toggle first: it fixes a whole range of episodes (e.g. Bleach 1-62),
    # whereas a finale suffix only fixes the single last episode.
    if prefix_toggled and prefix_toggled != url:
        variants.append(prefix_toggled)
    for base in [url, prefix_toggled]:
        if not base:
            continue
        stripped = base.rstrip("/")
        for suf in FINALE_SUFFIXES:
            variants.append(stripped + suf + "/")

    seen, out = set(), []
    for u in variants:
        if u != url and u not in seen:
            seen.add(u)
            out.append(u)
    return out

# Precompiled patterns + helper for parsing aria2c's progress output. These run on
# every stdout line of every active download, so they are hoisted out of the loop.
_ARIA_PROGRESS_RE = re.compile(r"([^ ]+)/([^ ]+)\((\d+)%\).*?DL:([^ \]]+)")
_ARIA_ETA_RE = re.compile(r"ETA:([^ \]]+)")
_ARIA_CONN_RE = re.compile(r"CN:(\d+)\s+DL:([^ \]]+)")
_ARIA_UNIT_RE = re.compile(r"([\d\.]+)(K|M|G)iB")


_ARIA_UNIT_BYTES = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _aria_to_bytes(val_str):
    """aria2c's "12.4MiB" -> raw bytes, for the concurrency controller's maths."""
    m = _ARIA_UNIT_RE.match(val_str or "")
    if not m:
        return 0.0
    return float(m.group(1)) * _ARIA_UNIT_BYTES.get(m.group(2), 1)


def _aria_convert_unit(val_str):
    """Turn aria2c's KiB/MiB/GiB figure into a friendly KB/MB/GB string."""
    m = _ARIA_UNIT_RE.match(val_str)
    if not m:
        return val_str
    val, unit = float(m.group(1)), m.group(2)
    if unit == 'K': return f"{val * 1.024:.1f} KB"
    if unit == 'M': return f"{val * 1.048576:.2f} MB"
    return f"{val * 1.07374:.2f} GB"


_ARIA_ETA_PARTS_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _format_eta(raw):
    """Render aria2c's ETA ("1m5s", "45s", "1h2m3s") as MM:SS.

    Minutes are never rolled up into hours, so an hour-plus wait reads 65:30 rather
    than 1:05:30. Returns the raw value unchanged if it doesn't parse.
    """
    m = _ARIA_ETA_PARTS_RE.match((raw or "").strip())
    if not m or not any(m.groups()):
        return raw
    hours, mins, secs = (int(g) if g else 0 for g in m.groups())
    total_minutes = hours * 60 + mins
    return f"{total_minutes:02d}:{secs:02d}"


def rewrite_gdrive_to_direct_download(driver, download_dir):
    """If the active tab is a Google Drive file-preview page (…/file/d/<ID>/view),
    navigate it to the direct-download URL so a large video shows the "Download
    anyway" virus-scan confirm and can be intercepted. No-op for any other page.

    animerco's download links redirect here (unlike witanime's, which land on the
    download page directly), so this bridges the gap. The redirect can take a moment
    to settle, so we poll briefly for the Drive file URL.
    """
    try:
        for _ in range(12):
            url = driver.current_url or ""
            m = re.search(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)", url)
            if m:
                driver.get(f"https://drive.google.com/uc?export=download&id={m.group(1)}")
                try:
                    driver.execute_cdp_cmd("Page.setDownloadBehavior",
                                           {"behavior": "allow", "downloadPath": download_dir})
                except Exception:
                    pass
                return
            # Already settled on a non-preview Drive/host page -> nothing to rewrite.
            if "drive.google.com" in url and "/file/d/" not in url:
                return
            time.sleep(0.5)
    except Exception:
        pass


def parse_smart_xpath(raw_input):
    raw_input = raw_input.strip()
    if not raw_input: return ""
    if raw_input.startswith("/") or raw_input.startswith("("): return raw_input
        
    if "#" in raw_input:
        parts = raw_input.split("#", 1) 
        text_part = parts[0].strip().lower()
        index_part = parts[1].strip().lower()
        base_xpath = f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text_part}')]"
        if index_part == "last": return f"({base_xpath})[last()]"
        else: return f"({base_xpath})[{index_part}]"
    else:
        text_part = raw_input.lower()
        return f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text_part}')]"

def kill_stuck_chrome_processes():
    try: 
        subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"], 
                       creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    
    # Securely list and terminate chrome.exe instances matching SeleniumProfile or --headless using wmic
    try:
        cmd = ["wmic", "process", "where", "name='chrome.exe'", "get", "processid,commandline", "/format:csv"]
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        for line in res.stdout.splitlines():
            if "," in line:
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    cmdline = parts[1]
                    pid = parts[2]
                    if pid.isdigit() and ("SeleniumProfile" in cmdline or "--headless" in cmdline):
                        subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], 
                                       creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # Safe fallback: only kill by name if parsing fails
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], 
                           creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
    time.sleep(1)
    if not os.path.exists(PROFILE_DIR): return
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try: os.remove(os.path.join(PROFILE_DIR, lock))
        except: pass

def launch_visible_browser():
    global manual_driver
    signals.update_buttons.emit(False, False, False)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        signals.update_status.emit("Status: Preparing profile browser...", "#f39c12")
        kill_stuck_chrome_processes()
            
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        # "enable-logging" is excluded so Chrome does not open its own console window
        # alongside the browser -- that black log window is alarming and useless to users.
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("prefs", {"profile.exit_type": "Normal", "profile.exited_cleanly": True})
        options.add_argument("--start-maximized")
        options.add_argument("--force-dark-mode")
        options.add_argument("--enable-features=WebContentsForceDark")
        options.add_argument("--log-level=3")   # fatal only
        from utils.browser_flags import apply_dns_flags
        apply_dns_flags(options)   # resolve via Google Public DNS, not the machine's

        # chromedriver's own log still goes to a file for diagnostics, but
        # --enable-chrome-logs is NOT used: it is what spawned the console window.
        log_path = os.path.join(APP_DIR, "chromedriver.log")
        service = Service(log_output=log_path)
        service.creation_flags = CREATE_NO_WINDOW
        manual_driver = webdriver.Chrome(options=options, service=service)
        from core import adblock
        adblock.apply(manual_driver)
        manual_driver.get("chrome://extensions/")
        signals.update_status.emit("Status: Profile browser open. Manage your sessions/logins, then close to continue.", "#f39c12")
    except Exception as e:
        signals.update_status.emit(f"Status: ❌ Error opening browser: {e}", "#e74c3c")
    finally:
        signals.update_buttons.emit(True, False, True)

def create_browser(download_dir, headless=True):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    # "enable-logging" excluded: it makes Chrome open a separate console window.
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_argument("--log-level=3")
    from utils.browser_flags import apply_dns_flags
    apply_dns_flags(options)   # resolve via Google Public DNS, not the machine's
    os.makedirs(download_dir, exist_ok=True)
    prefs = {
        "profile.exit_type": "Normal", "profile.exited_cleanly": True,
        "download.default_directory": download_dir, "download.prompt_for_download": False,
        "download.directory_upgrade": True, "safebrowsing.enabled": False, "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)

    options.add_argument("--force-dark-mode")
    options.add_argument("--enable-features=WebContentsForceDark,ParallelDownloading")

    if headless: 
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080") 
    else: 
        options.add_argument("--start-maximized") 

    # Aggressive Performance & Memory Tweaks (Keep Images Enabled)
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-features=Translate,MediaRouter,BackForwardCache,SharedArrayBuffer")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-hang-monitor")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-prompt-on-repost")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--mute-audio")

    service = Service()
    service.creation_flags = CREATE_NO_WINDOW
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(45)
    # Real ad blocker first (hides leftover ad slots and disarms popunders, which a
    # URL blocklist cannot); fall back to request blocking if it cannot be loaded.
    from core import extensions, adblock
    if extensions.load_into(driver):
        signals.update_status.emit("Status: 🛡️ Ad blocker active.", "#2ecc71")
    else:
        adblock.apply(driver)
    return driver

def solve_captcha_if_present(driver, url):
    """
    Detects and solves hCaptcha, reCAPTCHA, and Cloudflare Turnstile using 2Captcha or Anti-Captcha API.
    """
    provider = app_settings.get("captcha_provider", "Disabled")
    api_key = app_settings.get("captcha_api_key", "").strip()
    
    if provider == "Disabled" or not api_key:
        return
        
    import urllib.request
    import urllib.parse
    import time
    
    # 1. Detection of Captcha Elements & Sitekeys
    sitekey = None
    captcha_type = None # "recaptcha", "hcaptcha", "turnstile"
    
    # Check reCAPTCHA v2
    try:
        elements = driver.find_elements("xpath", "//*[contains(@class, 'g-recaptcha')] | //iframe[contains(@src, 'recaptcha/api2')]")
        for el in elements:
            sk = el.get_attribute("data-sitekey")
            if sk:
                sitekey = sk
                captcha_type = "recaptcha"
                break
            src = el.get_attribute("src")
            if src and "k=" in src:
                parsed = urllib.parse.urlparse(src)
                params = urllib.parse.parse_qs(parsed.query)
                if 'k' in params:
                    sitekey = params['k'][0]
                    captcha_type = "recaptcha"
                    break
    except Exception:
        pass
        
    # Check hCaptcha
    if not sitekey:
        try:
            elements = driver.find_elements("xpath", "//*[contains(@class, 'h-captcha')] | //iframe[contains(@src, 'hcaptcha.com')]")
            for el in elements:
                sk = el.get_attribute("data-sitekey")
                if sk:
                    sitekey = sk
                    captcha_type = "hcaptcha"
                    break
                src = el.get_attribute("src")
                if src and "sitekey=" in src:
                    parsed = urllib.parse.urlparse(src)
                    params = urllib.parse.parse_qs(parsed.query)
                    if 'sitekey' in params:
                        sitekey = params['sitekey'][0]
                        captcha_type = "hcaptcha"
                        break
        except Exception:
            pass
            
    # Check Cloudflare Turnstile
    if not sitekey:
        try:
            elements = driver.find_elements("xpath", "//*[contains(@class, 'cf-turnstile')] | //iframe[contains(@src, 'challenges.cloudflare.com')]")
            for el in elements:
                sk = el.get_attribute("data-sitekey")
                if sk:
                    sitekey = sk
                    captcha_type = "turnstile"
                    break
                src = el.get_attribute("src")
                if src and "sitekey=" in src:
                    parsed = urllib.parse.urlparse(src)
                    params = urllib.parse.parse_qs(parsed.query)
                    if 'sitekey' in params:
                        sitekey = params['sitekey'][0]
                        captcha_type = "turnstile"
                        break
        except Exception:
            pass

    if not sitekey or not captcha_type:
        return # No captcha found
        
    signals.update_status.emit(f"Status: 🛡️ Captcha Detected ({captcha_type.upper()}). Solving via {provider}...", "#ffa500")
    
    # 2. Submit Task to API
    try:
        task_id = None
        if provider == "2Captcha":
            submit_url = "https://2captcha.com/in.php"
            payload = {
                "key": api_key,
                "method": "userrecaptcha" if captcha_type == "recaptcha" else ("hcaptcha" if captcha_type == "hcaptcha" else "turnstile"),
                "sitekey": sitekey,
                "pageurl": url,
                "json": 1
            }
            req_data = urllib.parse.urlencode(payload).encode('utf-8')
            req = urllib.request.Request(submit_url, data=req_data, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode('utf-8'))
                if res.get("status") == 1:
                    task_id = res.get("request")
                else:
                    raise Exception(res.get("request"))
        
        elif provider == "Anti-Captcha":
            submit_url = "https://api.anti-captcha.com/createTask"
            task_type = "NoCaptchaTaskProxyless" if captcha_type == "recaptcha" else ("HCaptchaTaskProxyless" if captcha_type == "hcaptcha" else "TurnstileTaskProxyless")
            payload = {
                "clientKey": api_key,
                "task": {
                    "type": task_type,
                    "websiteURL": url,
                    "websiteKey": sitekey
                }
            }
            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(submit_url, data=req_data, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode('utf-8'))
                if res.get("errorId") == 0:
                    task_id = res.get("taskId")
                else:
                    raise Exception(res.get("errorDescription"))

        if not task_id:
            signals.update_status.emit(f"Status: ❌ Captcha Submission Failed.", "#ff4c4c")
            return
            
        # 3. Poll for Solution
        solution_token = None
        poll_count = 0
        max_polls = 40 # 2 minutes
        
        while poll_count < max_polls:
            time.sleep(3.0)
            poll_count += 1
            signals.update_status.emit(f"Status: 🛡️ Captcha Solving... ({poll_count * 3}s)", "#ffa500")
            
            try:
                if provider == "2Captcha":
                    res_url = f"https://2captcha.com/res.php?key={api_key}&action=get&id={task_id}&json=1"
                    req = urllib.request.Request(res_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=10) as response:
                        res = json.loads(response.read().decode('utf-8'))
                        if res.get("status") == 1:
                            solution_token = res.get("request")
                            break
                        elif res.get("request") == "CAPCHA_NOT_READY":
                            continue
                        else:
                            raise Exception(res.get("request"))
                
                elif provider == "Anti-Captcha":
                    res_url = "https://api.anti-captcha.com/getTaskResult"
                    payload = {"clientKey": api_key, "taskId": task_id}
                    req_data = json.dumps(payload).encode('utf-8')
                    req = urllib.request.Request(res_url, data=req_data, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=10) as response:
                        res = json.loads(response.read().decode('utf-8'))
                        if res.get("errorId") != 0:
                            raise Exception(res.get("errorDescription"))
                        status = res.get("status")
                        if status == "ready":
                            solution = res.get("solution", {})
                            solution_token = solution.get("gRecaptchaResponse") or solution.get("token") or solution.get("text")
                            break
                        elif status == "processing":
                            continue
            except Exception as e:
                print(f"[CAPTCHA] Poll error: {e}")
                
        if not solution_token:
            signals.update_status.emit("Status: ❌ Captcha Solve Timeout.", "#ff4c4c")
            return
            
        # 4. Inject Solution Token
        signals.update_status.emit("Status: 🛡️ Captcha Solved! Injecting token...", "#00e676")
        
        js_inject = f"""
        let token = "{solution_token}";
        let googleRes = document.getElementById("g-recaptcha-response");
        if (googleRes) googleRes.innerHTML = token;
        
        let hcapRes = document.querySelector("[name='h-captcha-response']");
        if (hcapRes) hcapRes.innerHTML = token;
        
        let cfRes = document.querySelector("[name='cf-turnstile-response']");
        if (cfRes) cfRes.innerHTML = token;
        
        try {{
            if (typeof recaptchaCallback === 'function') recaptchaCallback(token);
        }} catch(e) {{}}
        try {{
            if (typeof onSubmit === 'function') onSubmit(token);
        }} catch(e) {{}}
        
        try {{
            let event = new Event('change', {{ bubbles: true }});
            if (googleRes) googleRes.dispatchEvent(event);
            if (hcapRes) hcapRes.dispatchEvent(event);
            if (cfRes) cfRes.dispatchEvent(event);
        }} catch(e) {{}}
        """
        driver.execute_script(js_inject)
        
        try:
            submit_btn = driver.find_elements("xpath", "//button[@type='submit'] | //input[@type='submit'] | //*[contains(@class, 'captcha-submit')]")
            if submit_btn:
                submit_btn[0].click()
            else:
                driver.execute_script("if (document.forms.length > 0) document.forms[0].submit();")
        except Exception:
            pass
            
        time.sleep(2.0)
        signals.update_status.emit("Status: 🛡️ Captcha solved successfully!", "#00e676")
        
    except Exception as e:
        signals.update_status.emit(f"Status: ❌ Captcha error: {e}", "#ff4c4c")

def aria2c_downloader(ep, url, final_name, cookies, ua, temp_dir, cancel_event, on_episode_completed, process_callback=None, my_task_id=0, controller=None):
    if not os.path.exists(ARIA2C_PATH):
        from utils.tools_manager import ensure_aria2c
        signals.update_active_download.emit(ep, "⚡ Bootstrapping downloader...")
        ensure_aria2c()
        if not os.path.exists(ARIA2C_PATH):
            signals.update_active_download.emit(ep, "❌ Downloader core missing! Please restart the app.")
            on_episode_completed()
            return

    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    final_name = final_name if final_name else f"episode_{ep}.mp4"

    if ep not in ep_pause_events: ep_pause_events[ep] = threading.Event()
    if ep not in ep_cancel_events: ep_cancel_events[ep] = threading.Event()

    # Some hosts (e.g. workupload) limit or reject multi-connection splitting
    # ("Invalid range header", errorCode=8). On failure we step down the connection
    # count to find the largest one that actually works, instead of dropping to 1.
    conn_levels = [16, 8, 4, 2, 1]
    conn_idx = 0
    attempts = 0
    max_attempts = 6       # cap retries so a dead/blocking host isn't hammered forever
    verify_cert = True     # try with TLS verification first; drop it only on a cert error

    while True:
        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set(): break

        attempts += 1
        if attempts > max_attempts:
            signals.update_active_download.emit(ep, "❌ Download failed after several attempts.")
            break

        conns = str(conn_levels[conn_idx])
        cmd = [
            ARIA2C_PATH, "-c", "--auto-file-renaming=false",
            "-x", conns, "-s", conns, "-j", conns,
            "-k", "1M", "--min-split-size=1M", "--disk-cache=128M",
            "--optimize-concurrent-downloads=true", "--disable-ipv6=true",
            "--file-allocation=none", "--summary-interval=1", "--auto-save-interval=1",
            "--connect-timeout=5", "--timeout=10", "--max-tries=5", "--retry-wait=2",
        ]
        # TLS: verify by default; fall back to no verification only after a cert error.
        cmd.append("--check-certificate=true" if verify_cert else "--check-certificate=false")
        if ua: cmd.append(f"--user-agent={ua}")
        else: cmd.append("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        
        cmd.extend(["--header=Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "--header=Accept-Language: en-US,en;q=0.5", "--header=Sec-Fetch-Dest: document",
                    "--header=Sec-Fetch-Mode: navigate"])
        if cookie_str: cmd.append(f"--header=Cookie: {cookie_str}")
        cmd.extend([f"--dir={temp_dir}", f"--out={final_name}", url])

        # Phantom File Cleanup with lock bypass
        target_file = os.path.join(temp_dir, final_name)
        aria2_file = target_file + ".aria2"
        if os.path.exists(target_file) and not os.path.exists(aria2_file):
            for _ in range(10):
                try: 
                    os.remove(target_file)
                    break
                except Exception: 
                    time.sleep(0.5)

        process_finished_normally = False

        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW
            )
            active_aria2_processes.append(process)
            ep_aria2_processes[ep] = process
            # Bounded ring buffer: appending drops the oldest line for free, instead of
            # re-slicing a list on every single line of output.
            recent_lines = collections.deque(maxlen=30)

            for line in process.stdout:
                recent_lines.append(line.rstrip())
                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set() or pause_event.is_set() or ep_pause_events[ep].is_set():
                    break
                    
                if "%" in line and "DL:" in line:
                    try:
                        match = _ARIA_PROGRESS_RE.search(line)
                        if match:
                            pct = int(match.group(3))
                            speed = _aria_convert_unit(match.group(4)) + "/s"
                            total_size = _aria_convert_unit(match.group(2))   # full episode size

                            # Feed the auto-concurrency controller: size / speed is
                            # this episode's projected total time.
                            if controller is not None:
                                controller.record_progress(ep,
                                                           _aria_to_bytes(match.group(2)),
                                                           _aria_to_bytes(match.group(4)))

                            # Time remaining, straight from aria2c's ETA field.
                            eta_str = ""
                            eta_match = _ARIA_ETA_RE.search(line)
                            if eta_match:
                                eta_str = f"   •   Remaining: {_format_eta(eta_match.group(1))}"

                            signals.update_active_bar.emit(ep, pct)
                            signals.update_active_download.emit(ep, f"⚡ {speed}   •   {pct}% of {total_size}{eta_str}")
                    except Exception: pass
                elif "CN:" in line:
                    try:
                        match_init = _ARIA_CONN_RE.search(line)
                        if match_init:
                            active_conns = match_init.group(1)
                            speed = _aria_convert_unit(match_init.group(2)) + "/s"
                            signals.update_active_download.emit(ep, f"🔌 Connecting... (Conns: {active_conns}, Speed: {speed})")
                    except Exception: pass
            
            process.wait()
            if process in active_aria2_processes: active_aria2_processes.remove(process)
            if ep in ep_aria2_processes: del ep_aria2_processes[ep]
            if process.returncode == 0:
                process_finished_normally = True
            elif not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id or ep_cancel_events[ep].is_set() or pause_event.is_set() or ep_pause_events[ep].is_set()):
                # A genuine failure often means the host is pushing back, so ease off
                # the number of parallel downloads rather than hammering it harder.
                if controller is not None:
                    controller.record_failure(f"download error (rc={process.returncode})")
                # Failed -> step down to fewer connections (server may limit
                # splitting / ignore Range) to find the largest working count.
                if conn_idx < len(conn_levels) - 1:
                    conn_idx += 1
                    signals.update_active_download.emit(ep, f"⚙ Retrying with {conn_levels[conn_idx]} connection(s)...")
                    # Split count changed -> clear the partial + control file for a clean retry.
                    for _f in (target_file, aria2_file):
                        try: os.remove(_f)
                        except Exception: pass
                # A TLS/certificate failure -> retry once without verification.
                if verify_cert and any(w in l.lower() for l in recent_lines for w in ("ssl", "certificate", "handshake")):
                    verify_cert = False
                    signals.update_active_download.emit(ep, "⚙ Certificate error — retrying without verification...")
                # Genuine aria2c failure -- log the real output so the cause is visible.
                try:
                    with open(os.path.join(APP_DIR, "aria2c_error.log"), "a", encoding="utf-8") as _lf:
                        _lf.write(f"\n=== Ep {ep}  rc={process.returncode}  {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                        _lf.write(f"URL: {url}\n")
                        _lf.write(f"Cookies sent: {'yes' if cookie_str else 'NONE'} (len={len(cookie_str)})\n")
                        _lf.write("aria2c output:\n" + "\n".join(recent_lines) + "\n")
                except Exception:
                    pass
            
        except Exception as e:
            if process in active_aria2_processes: active_aria2_processes.remove(process)
            if ep in ep_aria2_processes: del ep_aria2_processes[ep]
            if not pause_event.is_set() and not ep_pause_events[ep].is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) and not ep_cancel_events[ep].is_set():
                signals.update_active_download.emit(ep, f"Download Error: {e}")
                break
                
        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set(): break
            
        if pause_event.is_set() or ep_pause_events[ep].is_set():
            signals.update_active_download.emit(ep, "⏸ Paused")
            while (pause_event.is_set() or ep_pause_events[ep].is_set()) and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) and not ep_cancel_events[ep].is_set():
                time.sleep(1)
            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set(): break
            continue 
            
        # A tiny "completed" file is a block/error page, not the video -> treat as failed.
        if process_finished_normally and is_block_page(target_file):
            process_finished_normally = False
            # The clearest rate-limit signal there is: back the concurrency off hard.
            if controller is not None:
                controller.record_failure("block page from the host")
            signals.update_active_download.emit(ep, "❌ Received a block/error page, not the video. Retrying...")

        if process_finished_normally:
            signals.update_active_bar.emit(ep, 100)
            signals.update_active_download.emit(ep, "Extraction & Cleanup...")
            if process_callback: process_callback(ep, temp_dir)
            
            # Smart Session Recovery: remove completed episode
            from utils.config import save_config
            with config_lock:
                session = app_settings.get("unfinished_session")
                if session and "episodes" in session:
                    if ep in session["episodes"]:
                        session["episodes"].remove(ep)
                    if not session["episodes"]:
                        app_settings.pop("unfinished_session", None)
                    save_config()

            time.sleep(1)
            signals.remove_active_download.emit(ep)
            break 
        elif not pause_event.is_set() and not ep_pause_events[ep].is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) and not ep_cancel_events[ep].is_set():
            signals.update_active_download.emit(ep, "❌ Download Failed. Retrying...")
            time.sleep(min(3 * attempts, 30))   # backoff -- don't hammer the host
            
    if ep_cancel_events[ep].is_set() and not process_finished_normally:
        signals.remove_active_download.emit(ep)

    # Release this host's throttle slot (incremented before the thread was started).
    with host_lock:
        h = _host_of(url)
        if host_active.get(h):
            host_active[h] = max(0, host_active[h] - 1)

    on_episode_completed()

def run_selenium_task(site_key, episodes_list, download_dir, headless, webhook_url, selected_sound, volume , concurrency):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = None
    global CURRENT_TASK_ID
    CURRENT_TASK_ID += 1
    my_task_id = CURRENT_TASK_ID

    cancel_event.clear()
    pause_event.clear()
    ep_pause_events.clear()
    ep_cancel_events.clear()
    ep_aria2_processes.clear()
    failed_eps = []
    task_started = False
    episode_temp_dirs = {} 
    
    # Auto mode tunes the number of parallel downloads from measured speed; manual
    # mode pins it to the user's value. Either way the throttle reads controller.limit.
    from core.concurrency import ConcurrencyController
    with config_lock:
        auto_concurrency = bool(app_settings.get("concurrency_auto", True))
    controller = ConcurrencyController(start=concurrency, enabled=auto_concurrency)
    signals.concurrency_changed.emit(controller.describe())
    active_engine_threads = []
    episodes_completed_count = 0

    def on_episode_completed():
        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): return 
        
        nonlocal episodes_completed_count
        with progress_lock:
            episodes_completed_count += 1
            signals.update_progress.emit(episodes_completed_count, len(episodes_list))

    try:
        if site_key not in sites_data:
            signals.update_status.emit("Status: ❌ Invalid Profile.", "#e74c3c")
            return
            
        task_started = True

        with config_lock:
            config = sites_data.get(site_key, {})
            
        url_template = config.get("url", "")
        step_paths = config.get("step_paths", {"Path 1": config.get("steps", [])})
        safe_site_name = "".join(c for c in site_key if c not in r'\/:*?"<>|').strip()

        profile_folder_path = os.path.join(download_dir, safe_site_name)
        os.makedirs(profile_folder_path, exist_ok=True)
        VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts')

        def process_downloaded_episode(x, temp_dir):
            if not os.path.exists(temp_dir): return
            import py7zr
            import rarfile
            # Point rarfile at the bundled unrar.exe (it is not on the system PATH).
            if os.path.exists(UNRAR_PATH):
                rarfile.UNRAR_TOOL = UNRAR_PATH
            try:
                current_timestamp = time.time()
                for item in os.listdir(temp_dir):
                    src_item = os.path.join(temp_dir, item)
                    
                    if os.path.isfile(src_item) and src_item.lower().endswith(('.zip', '.tar', '.gz', '.bz2', '.rar', '.7z')):
                        extract_temp = os.path.join(temp_dir, "extracted_junk")
                        os.makedirs(extract_temp, exist_ok=True)
                        try:
                            extract_success = False
                            for attempt in range(4):
                                try:
                                    if src_item.lower().endswith('.rar'):
                                        with rarfile.RarFile(src_item) as rf:
                                            rf.extractall(path=extract_temp)
                                    elif src_item.lower().endswith('.7z'):
                                        with py7zr.SevenZipFile(src_item, mode='r') as z:
                                            z.extractall(path=extract_temp)
                                    else:
                                        shutil.unpack_archive(src_item, extract_temp)
                                    extract_success = True
                                    break 
                                except Exception as zip_err:
                                    print(f"Extraction locked. Retrying in 2s... ({zip_err})")
                                    time.sleep(2)
                                    
                            if not extract_success:
                                raise Exception("Episode File is corrupted or incomplete.")
                            
                            found_videos = []
                            for root, _, files in os.walk(extract_temp):
                                for f in files:
                                    if f.lower().endswith(VIDEO_EXTENSIONS):
                                        found_videos.append(os.path.join(root, f))
                            
                            if found_videos:
                                found_videos.sort(key=os.path.getsize, reverse=True)
                                main_video = found_videos[0]
                                _, ext = os.path.splitext(main_video)
                                new_name = f"{safe_site_name} Ep{x}{ext}"
                                dst_item = os.path.join(profile_folder_path, new_name)
                                
                                counter = 1
                                while os.path.exists(dst_item):
                                    dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                                    counter += 1
                                    
                                shutil.move(main_video, dst_item)
                                try: os.utime(dst_item, (current_timestamp, current_timestamp))
                                except Exception as e: print(f"Error setting timestamp for Ep {x}: {e}")
                        except Exception as e:
                            print(f"Extraction failed for Ep {x}: {e}")
                            
                    elif os.path.isdir(src_item):
                        found_videos = []
                        for root, _, files in os.walk(src_item):
                            for f in files:
                                if f.lower().endswith(VIDEO_EXTENSIONS):
                                    found_videos.append(os.path.join(root, f))
                        if found_videos:
                            found_videos.sort(key=os.path.getsize, reverse=True)
                            main_video = found_videos[0]
                            _, ext = os.path.splitext(main_video)
                            new_name = f"{safe_site_name} Ep{x}{ext}"
                            dst_item = os.path.join(profile_folder_path, new_name)
                            counter = 1
                            while os.path.exists(dst_item):
                                dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                                counter += 1
                            shutil.move(main_video, dst_item)
                            try: os.utime(dst_item, (current_timestamp, current_timestamp))
                            except Exception as e: print(f"Error setting timestamp for Ep {x}: {e}")

                    elif os.path.isfile(src_item) and src_item.lower().endswith(VIDEO_EXTENSIONS):
                        _, ext = os.path.splitext(item)
                        new_name = f"{safe_site_name} Ep{x}{ext}"
                        dst_item = os.path.join(profile_folder_path, new_name)
                        counter = 1
                        while os.path.exists(dst_item):
                            dst_item = os.path.join(profile_folder_path, f"{safe_site_name} Ep{x} ({counter}){ext}")
                            counter += 1
                        shutil.move(src_item, dst_item)
                        try: os.utime(dst_item, (current_timestamp, current_timestamp))
                        except Exception as e: print(f"Error setting timestamp for Ep {x}: {e}")
                        
                shutil.rmtree(temp_dir)
            except Exception as e:
                print(f"Error processing Ep {x}: {e}")

        signals.update_status.emit("Status: Cleaning up...", "#f39c12")
        kill_stuck_chrome_processes()

        # Ads are suppressed by blocking their requests (see core.adblock) rather than
        # by installing an extension, which current Chrome no longer permits an app to
        # do unattended. Nothing to set up here, so downloads start straight away.
        driver = create_browser(download_dir, headless)
        wait = WebDriverWait(driver, 10)

        total_episodes = len(episodes_list)
        signals.update_progress.emit(0, total_episodes)

        for x in episodes_list:
            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break
            
            while pause_event.is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                time.sleep(1)
            
            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break
            
            if len([t for t in active_engine_threads if t.is_alive()]) > 0:
                time.sleep(random.uniform(2.0, 4.0))

            path_success = False
            ep_temp_dir = os.path.join(tempfile.gettempdir(), f"AnimeDL_{safe_site_name}_Ep_{x}")
            os.makedirs(ep_temp_dir, exist_ok=True)
            episode_temp_dirs[x] = ep_temp_dir
            
            url = url_template.replace("{x}", str(x))
            print(f"\nProcessing Ep {x}")
            
            for attempt in range(3):
                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or path_success: break
                try:
                    signals.update_status.emit(f"Status: Loading {site_key} - Ep {x} (Attempt {attempt+1}/3)...", "#ffffff")
                    
                    driver.execute_script("window.open('');")
                    driver.switch_to.window(driver.window_handles[-1])
                    driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": ep_temp_dir})
                    
                    driver.get(url)
                    time.sleep(3)

                    # A site may serve this episode under a different URL slug (an
                    # انمي- prefix, or a "final episode" suffix). If the primary URL
                    # is a not-found page, retry the known alternate forms.
                    if is_page_not_found(driver):
                        for alt_url in episode_url_variants(url):
                            driver.get(alt_url)
                            time.sleep(2)
                            if not is_page_not_found(driver):
                                break

                    # Dynamic Captcha Solver Integration
                    try:
                        solve_captcha_if_present(driver, driver.current_url)
                    except Exception as captcha_err:
                        print(f"Captcha solving failed: {captcha_err}")

                    for path_name, steps in step_paths.items():
                        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or path_success: break
                        if not steps: continue
                        
                        signals.update_status.emit(f"Status: [{path_name}] Executing...", "#ffffff")
                        path_failed = False
                        current_tabs = len(driver.window_handles)

                        for step_idx, step in enumerate(steps):
                            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break

                            while pause_event.is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                time.sleep(1)
                            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break

                            raw_xpath = step.get("xpath", "").strip()
                            delay = float(step.get("delay", 0.0))
                            if not raw_xpath: continue
                            
                            xpath = parse_smart_xpath(raw_xpath)
                            signals.update_status.emit(f"Status: [{path_name}] Clicking Step {step_idx + 1}...", "#ffffff")
                            
                            xpaths_to_try = [xpath, xpath.replace("text()", "@value")]
                            if step_idx == 0 and 'google drive' in raw_xpath.lower():
                                xpaths_to_try.append("//ul[contains(@class, 'download-links')]//a")

                            btn = None
                            for xp in xpaths_to_try:
                                try: 
                                    btn = wait.until(EC.presence_of_element_located((By.XPATH, xp)))
                                    break
                                except Exception: pass
                            
                            if not btn: 
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Could not find button for Step {step_idx + 1}", "#e74c3c")
                                break 

                            try: 
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                                time.sleep(0.5)
                                # ALWAYS use JS click first to bypass invisible ad overlays!
                                driver.execute_script("arguments[0].click();", btn)
                            except Exception:
                                try:
                                    driver.execute_script("arguments[0].click();", btn)
                                except Exception:
                                    path_failed = True
                                    signals.update_status.emit(f"Status: ❌ Failed to click Step {step_idx + 1}", "#e74c3c")
                                    break
                            
                            slept = 0
                            while slept < delay:
                                while pause_event.is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): 
                                    time.sleep(1)
                                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break
                                time.sleep(0.5)
                                slept += 0.5

                            new_tabs = len(driver.window_handles)
                            if new_tabs > current_tabs:
                                driver.switch_to.window(driver.window_handles[-1])
                                driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": ep_temp_dir})
                                current_tabs = new_tabs

                                # A new tab that lands on a Google Drive file-preview
                                # page (e.g. animerco's /links redirect) can't be
                                # downloaded from /view -- rewrite it to the direct
                                # download URL so the large-file "Download anyway"
                                # confirm shows and the file can be intercepted.
                                rewrite_gdrive_to_direct_download(driver, ep_temp_dir)

                                # Dynamic Captcha Solver Integration for tab switch
                                try:
                                    solve_captcha_if_present(driver, driver.current_url)
                                except Exception as captcha_err:
                                    print(f"Captcha solving failed on tab switch: {captcha_err}")
                        
                        if not path_failed:
                            signals.update_status.emit(f"Status: Intercepting Ep {x} (Waiting up to 35s)...", "#f39c12")
                            
                            driver.execute_script("window.open('');")
                            driver.switch_to.window(driver.window_handles[-1])
                            driver.get('chrome://downloads')
                            
                            found_data = None
                            wait_timer = 0
                            
                            while wait_timer < 35 and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                while pause_event.is_set() and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): 
                                    time.sleep(1)
                                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break

                                js_intercept = """
                                    const manager = document.querySelector('downloads-manager');
                                    if (!manager) return null;

                                    let targetData = null;
                                    let cancelBtn = null;

                                    try {
                                        const list = manager.shadowRoot.querySelector('#downloadsList');
                                        if (list) {
                                            const items = list.querySelectorAll('downloads-item');
                                            for (let item of items) {
                                                const shadow = item.shadowRoot;
                                                if (shadow) {
                                                    const btn = shadow.querySelector('#cancel');
                                                    if (btn && !btn.hidden && item.data) {
                                                        targetData = item.data;
                                                        cancelBtn = btn;
                                                        break;
                                                    }
                                                }
                                            }
                                        }
                                    } catch(e) {}

                                    if (!targetData && manager.items) {
                                        for (let item of manager.items) {
                                            if (item.state === 'IN_PROGRESS') {
                                                targetData = item;
                                                break;
                                            }
                                        }
                                    }

                                    if (targetData) {
                                        let dl_url = targetData.url || targetData.finalUrl || targetData.originalUrl;
                                        if (!dl_url) return null; 
                                        if (dl_url.startsWith('blob:')) return "BLOB";
                                        
                                        let fname = targetData.fileName || targetData.filePath || 'episode.mp4';
                                        fname = fname.split('\\\\').pop().split('/').pop();
                                        
                                        try {
                                            if (cancelBtn) {
                                                cancelBtn.click(); 
                                            } else if (manager.shadowRoot) {
                                                manager.shadowRoot.querySelector('#downloadsList').cancelDownload(targetData.id);
                                            }
                                        } catch(e) {}
                                        
                                        return JSON.stringify({url: dl_url, filename: fname});
                                    }
                                    return null;
                                """

                                dl_buttons = driver.find_elements(By.XPATH, "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download')]")
                                for btn in dl_buttons:
                                    try:
                                        if btn.is_displayed():
                                            driver.execute_script("arguments[0].click();", btn)
                                            time.sleep(1)
                                    except: pass

                                res = driver.execute_script(js_intercept)
                                if res:
                                    found_data = res
                                    break
                                time.sleep(1)
                                wait_timer += 1
                            
                            if found_data and found_data != "BLOB":
                                signals.update_status.emit(f"Status: ✅ Locked onto Ep {x}! Pre-fetched download details.", "#2ecc71")                                
                                signals.add_active_download.emit(x)
                                data_obj = json.loads(found_data)
                                dl_url = data_obj['url']
                                dl_fname = data_obj['filename']
                                cookies = get_download_cookies(driver, dl_url)
                                ua = driver.execute_script("return navigator.userAgent;")
                                
                                # Close the successfully intercepted tab immediately to free up system memory
                                if len(driver.window_handles) > 1:
                                    try:
                                        driver.close()
                                        driver.switch_to.window(driver.window_handles[0])
                                    except: pass
                                
                                # Throttle: wait until an overall slot is free AND the
                                # download's host is under its per-host cap.
                                dl_host = _host_of(dl_url)
                                while not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                    previous_limit = controller.limit
                                    limit = controller.evaluate()   # acts once per window
                                    if limit != previous_limit:
                                        signals.concurrency_changed.emit(controller.describe())
                                    alive = len([t for t in active_engine_threads if t.is_alive()])
                                    with host_lock:
                                        host_n = host_active.get(dl_host, 0)
                                    if alive < limit and host_n < PER_HOST_MAX:
                                        break
                                    time.sleep(1)

                                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                    break

                                with host_lock:
                                    host_active[dl_host] = host_active.get(dl_host, 0) + 1

                                signals.update_status.emit(f"Status: ▶ Starting download for Ep {x}...", "#2ecc71")
                                t = threading.Thread(target=aria2c_downloader,
                                                     args=(x, dl_url, dl_fname, cookies, ua, ep_temp_dir, cancel_event, on_episode_completed, process_downloaded_episode, my_task_id, controller))
                                t.start()
                                active_engine_threads.append(t)
                                path_success = True
                                break
                            elif found_data == "BLOB":
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Video is streaming, not a direct file.", "#e74c3c")
                                break
                            else:
                                path_failed = True
                                signals.update_status.emit(f"Status: ❌ Download never started on the webpage.", "#e74c3c")
                                
                    if not path_success: raise Exception("Interception failed")
                    
                except Exception:
                    signals.update_status.emit("Status: ⚠️ Attempt failed, retrying...", "#e74c3c")
                    if len(driver.window_handles) > 1:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    time.sleep(2)

            if not path_success:
                failed_eps.append(x)
                signals.update_status.emit(f"Status: ❌ Failed to grab Episode {x} after 3 retries.", "#e74c3c")

            # Close every tab this episode opened (episode page, ad/redirect tabs,
            # chrome://downloads) and keep only the base tab, so Chrome tabs don't
            # accumulate across episodes. aria2c downloads run independently of these.
            try:
                handles = driver.window_handles
                base = handles[0]
                for h in handles[1:]:
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(base)
            except Exception:
                pass

        if not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
            signals.update_status.emit("Status: All downloads triggered! Waiting for files to finish...", "#f39c12")
            signals.update_buttons.emit(False, True, False)
            
            while len([t for t in active_engine_threads if t.is_alive()]) > 0 and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                time.sleep(1)
                
            if not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                cancelled_count = sum(1 for ep in episodes_list if ep_cancel_events.get(ep) and ep_cancel_events[ep].is_set())
                
                if len(failed_eps) + cancelled_count == len(episodes_list):
                    signals.update_status.emit("Status: ❌ All episodes failed or were cancelled.", "#e74c3c")
                else:
                    signals.update_status.emit("Status: 🎉 All files downloaded and extracted successfully!", "#2ecc71")
                    
                    def send_webhook_alert():
                        if webhook_url:
                            msg_text = f"🎉Successfully finished downloading episodes for {site_key}🎉"
                            if len(failed_eps) > 1: msg_text += f"\n⚠️ Note: {len(failed_eps)} episodes failed to download."
                            elif len(failed_eps) == 1: msg_text += f"\n⚠️ Note: {len(failed_eps)} episode failed to download."
                            data = {"content": msg_text}
                            headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                            req = urllib.request.Request(webhook_url, data=json.dumps(data).encode(), headers=headers)
                            try: urllib.request.urlopen(req, timeout=5)
                            except Exception: pass
                    
                    threading.Thread(target=send_webhook_alert, daemon=True).start()
                    
                    try:
                        if selected_sound and os.path.exists(selected_sound):
                            vol = volume * 10 
                            mci_path = selected_sound.replace("\\", "/")
                            ctypes.windll.winmm.mciSendStringW('close custom_audio', None, 0, None)
                            # Always open via mpegvideo so "setaudio volume" works;
                            # the waveaudio device rejects it, ignoring the volume for .wav.
                            ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" type mpegvideo alias custom_audio', None, 0, None)
                            ctypes.windll.winmm.mciSendStringW(f'setaudio custom_audio volume to {vol}', None, 0, None)
                            ctypes.windll.winmm.mciSendStringW('play custom_audio', None, 0, None)
                    except Exception: pass
            
    except Exception:
        signals.update_status.emit("Status: ❌ Critical Error Occurred. Check console.", "#e74c3c")
        traceback.print_exc()

    finally:
        if driver:
            try: driver.quit()
            except: pass
            
        if task_started:
            cancelled_count = sum(1 for ep in episodes_list if ep_cancel_events.get(ep) and ep_cancel_events[ep].is_set())
            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                status = "Cancelled"
                notes = "Stopped by user."
            elif len(failed_eps) + cancelled_count == len(episodes_list):
                status = "Failed"
                notes = f"Failed/Cancelled: {len(failed_eps)}/{cancelled_count} out of {len(episodes_list)}."
            elif failed_eps or cancelled_count > 0:
                status = "Partial"
                notes = f"Failed: {len(failed_eps)}, Cancelled: {cancelled_count}"
            else:
                status = "Success"
                notes = "Completed successfully."
            # Compact the episode list back into a spec ("1-5, 8-12") so History is
            # accurate and Re-download reproduces gapped selections exactly.
            def _compact_spec(nums):
                nums = sorted(set(nums))
                out, i = [], 0
                while i < len(nums):
                    j = i
                    while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
                        j += 1
                    out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
                    i = j + 1
                return ", ".join(out)
            eps_str = _compact_spec(episodes_list) if episodes_list else ""
            log_history(site_key, eps_str, status, notes)

        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
            signals.update_status.emit("Status: ❌ Download Cancelled.", "#e74c3c")
            
        signals.update_buttons.emit(True, False, True)
        finish_event.clear()
        
        # Only emit task_finished (which triggers the Success Screen) if at least one episode actually succeeded!
        cancelled_count = sum(1 for ep in episodes_list if ep_cancel_events.get(ep) and ep_cancel_events[ep].is_set())
        if not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) and (len(failed_eps) + cancelled_count < len(episodes_list)):
            signals.task_finished.emit(failed_eps)
        else:
            # If all were cancelled or failed, wait 2 seconds so the user can read the error/cancellation message,
            # then auto-hide the tab safely (only if this task wasn't interrupted by a new one).
            time.sleep(2)
            if CURRENT_TASK_ID == my_task_id:
                signals.task_cancelled.emit()