import os
import time
import json
import shutil
import traceback
import ctypes
import re
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

def auto_install_extensions():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        import pyautogui
        
        kill_stuck_chrome_processes()
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("prefs", {"profile.exit_type": "Normal", "profile.exited_cleanly": True})
        options.add_argument("--start-maximized")
        options.add_argument("--force-dark-mode")
        options.add_argument("--enable-features=WebContentsForceDark")
        
        service = Service()
        service.creation_flags = CREATE_NO_WINDOW
        driver = webdriver.Chrome(options=options, service=service)
        
        def install_one(url, name):
            signals.update_status.emit(f"Status: ⚡ Installing {name}...", "#f39c12")
            driver.get(url)
            time.sleep(5)
            
            js_click_btn = """
            // Target the button by its stable storefront jsname attribute first
            let btn = document.querySelector('button[jsname="wQO0od"]');
            if (btn) {
                btn.click();
                return true;
            }
            // Fallback to text matching
            const buttons = document.querySelectorAll('button');
            for (let b of buttons) {
                if (b.innerText && (b.innerText.includes('Add to Chrome') || b.innerText.includes('إضافة') || b.innerText.includes('Chrome'))) {
                    b.click();
                    return true;
                }
            }
            return false;
            """
            clicked = driver.execute_script(js_click_btn)
            if clicked:
                screen_w, screen_h = pyautogui.size()
                
                # Step 1: Force active window focus on Chrome by clicking safe neutral space in the center of the browser
                time.sleep(2)
                pyautogui.click(screen_w // 2, int(screen_h * 0.5))
                time.sleep(1)
                
                # Step 2: Try multi-layered key sequences to hit the Add Extension button (handles different default button configurations)
                # Attempt A: Left Arrow + Enter (standard shift to Add Extension)
                pyautogui.press('left')
                time.sleep(0.5)
                pyautogui.press('enter')
                
                # Attempt B: Tab + Enter (alternate modal shift to Add Extension)
                time.sleep(1.5)
                pyautogui.press('tab')
                time.sleep(0.5)
                pyautogui.press('enter')
                
                # Attempt C: Shift+Tab + Enter (reverse shift to Add Extension)
                time.sleep(1.5)
                with pyautogui.hold('shift'):
                    pyautogui.press('tab')
                time.sleep(0.5)
                pyautogui.press('enter')
                
                time.sleep(8) # Wait for download and configuration setup to finalize
                    
        # Install uBlock if missing
        ublock_path = os.path.join(PROFILE_DIR, "Default", "Extensions", "ddkjiahejlhfcafbddmgiahcphecmpfh")
        if not os.path.exists(ublock_path):
            install_one("https://chromewebstore.google.com/detail/ublock-origin-lite/ddkjiahejlhfcafbddmgiahcphecmpfh", "uBlock Origin Lite")
            
        # Install Buster if missing
        buster_path = os.path.join(PROFILE_DIR, "Default", "Extensions", "mpbjkejclgfgadiemmefgebjfooflfhl")
        if not os.path.exists(buster_path):
            install_one("https://chromewebstore.google.com/detail/buster-captcha-solver-for/mpbjkejclgfgadiemmefgebjfooflfhl", "Buster Captcha Solver")
            
        driver.quit()
    except Exception as e:
        print("Auto-install extensions failed:", e)

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
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("prefs", {"profile.exit_type": "Normal", "profile.exited_cleanly": True})
        options.add_argument("--start-maximized")
        options.add_argument("--force-dark-mode")
        options.add_argument("--enable-features=WebContentsForceDark")

        log_path = os.path.join(APP_DIR, "chromedriver.log")
        service = Service(service_args=["--log-level=ALL", "--enable-chrome-logs"], log_output=log_path)
        service.creation_flags = CREATE_NO_WINDOW
        manual_driver = webdriver.Chrome(options=options, service=service)
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
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
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

def aria2c_downloader(ep, url, final_name, cookies, ua, temp_dir, cancel_event, on_episode_completed, process_callback=None, my_task_id=0):
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
    global active_aria2_processes 
    
    if ep not in ep_pause_events: ep_pause_events[ep] = threading.Event()
    if ep not in ep_cancel_events: ep_cancel_events[ep] = threading.Event()

    while True:
        if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set(): break
        
        cmd = [
            ARIA2C_PATH, "-c", "--auto-file-renaming=false", 
            "-x", "16", "-s", "16", "-j", "16", 
            "-k", "1M", "--min-split-size=1M", "--disk-cache=128M", 
            "--optimize-concurrent-downloads=true", "--disable-ipv6=true",
            "--file-allocation=none", "--summary-interval=1", "--auto-save-interval=1",
            "--connect-timeout=5", "--timeout=10", "--max-tries=5", "--retry-wait=2",
            "--check-certificate=false"
        ]
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
            
            for line in process.stdout:
                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id) or ep_cancel_events[ep].is_set() or pause_event.is_set() or ep_pause_events[ep].is_set():
                    break
                    
                if "%" in line and "DL:" in line:
                    try:
                        match = re.search(r"([^ ]+)/([^ ]+)\((\d+)%\).*?DL:([^ \]]+)", line)
                        if match:
                            def convert_unit(val_str):
                                m = re.match(r"([\d\.]+)(K|M|G)iB", val_str)
                                if m:
                                    val = float(m.group(1))
                                    unit = m.group(2)
                                    if unit == 'K': return f"{val * 1.024:.1f} KB"
                                    if unit == 'M': return f"{val * 1.048576:.2f} MB"
                                    if unit == 'G': return f"{val * 1.07374:.2f} GB"
                                return val_str
                            pct = int(match.group(3))
                            speed = convert_unit(match.group(4)) + "/s"
                            
                            # Extract ETA
                            eta_str = ""
                            eta_match = re.search(r"ETA:([^ \]]+)", line)
                            if eta_match:
                                eta_str = f"   •   ⏳ {eta_match.group(1)} left"
                                
                            signals.update_active_bar.emit(ep, pct)
                            signals.update_active_download.emit(ep, f"⚡ {speed}   •   Progress: {pct}%{eta_str}")
                    except Exception: pass
                elif "CN:" in line:
                    try:
                        def convert_unit(val_str):
                            m = re.match(r"([\d\.]+)(K|M|G)iB", val_str)
                            if m:
                                val = float(m.group(1))
                                unit = m.group(2)
                                if unit == 'K': return f"{val * 1.024:.1f} KB"
                                if unit == 'M': return f"{val * 1.048576:.2f} MB"
                                if unit == 'G': return f"{val * 1.07374:.2f} GB"
                            return val_str
                        match_init = re.search(r"CN:(\d+)\s+DL:([^ \]]+)", line)
                        if match_init:
                            conns = match_init.group(1)
                            speed = convert_unit(match_init.group(2)) + "/s"
                            signals.update_active_download.emit(ep, f"🔌 Connecting... (Conns: {conns}, Speed: {speed})")
                    except Exception: pass
            
            process.wait()
            if process in active_aria2_processes: active_aria2_processes.remove(process)
            if ep in ep_aria2_processes: del ep_aria2_processes[ep]
            if process.returncode == 0: process_finished_normally = True
            
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
            time.sleep(3)
            
    if ep_cancel_events[ep].is_set() and not process_finished_normally:
        signals.remove_active_download.emit(ep)
        
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
    
    MAX_CONCURRENT = concurrency
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

        # Check if extensions are installed in the persistent profile
        ublock_path = os.path.join(PROFILE_DIR, "Default", "Extensions", "ddkjiahejlhfcafbddmgiahcphecmpfh")
        buster_path = os.path.join(PROFILE_DIR, "Default", "Extensions", "mpbjkejclgfgadiemmefgebjfooflfhl")
        if not os.path.exists(ublock_path) or not os.path.exists(buster_path):
            signals.update_status.emit("Status: ⚡ Checking and installing browser extensions...", "#f39c12")
            auto_install_extensions()

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
                                cookies = driver.get_cookies()
                                ua = driver.execute_script("return navigator.userAgent;")
                                
                                # Close the successfully intercepted tab immediately to free up system memory
                                if len(driver.window_handles) > 1:
                                    try:
                                        driver.close()
                                        driver.switch_to.window(driver.window_handles[0])
                                    except: pass
                                
                                # Concurrency throttle: wait here if all active slots are full
                                while len([t for t in active_engine_threads if t.is_alive()]) >= MAX_CONCURRENT and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                    time.sleep(1)
                                    
                                if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                                    break
                                    
                                signals.update_status.emit(f"Status: ▶ Starting download for Ep {x}...", "#2ecc71")
                                t = threading.Thread(target=aria2c_downloader, 
                                                     args=(x, dl_url, dl_fname, cookies, ua, ep_temp_dir, cancel_event, on_episode_completed, process_downloaded_episode, my_task_id))
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
                    
                except Exception as e:
                    signals.update_status.emit(f"Status: ⚠️ Attempt failed, retrying...", "#e74c3c")
                    if len(driver.window_handles) > 1:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    time.sleep(2)

            if not path_success:
                failed_eps.append(x)
                signals.update_status.emit(f"Status: ❌ Failed to grab Episode {x} after 3 retries.", "#e74c3c")

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
                            if mci_path.lower().endswith(".mp3"):
                                ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" type mpegvideo alias custom_audio', None, 0, None)
                            else:
                                ctypes.windll.winmm.mciSendStringW(f'open "{mci_path}" alias custom_audio', None, 0, None)
                            ctypes.windll.winmm.mciSendStringW(f'setaudio custom_audio volume to {vol}', None, 0, None)
                            ctypes.windll.winmm.mciSendStringW('play custom_audio', None, 0, None)
                    except Exception: pass
            
    except Exception as e:
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
            eps_str = f"{episodes_list[0]} - {episodes_list[-1]}" if len(episodes_list) > 1 else str(episodes_list[0])
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