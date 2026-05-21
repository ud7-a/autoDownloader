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
import concurrent.futures
from subprocess import CREATE_NO_WINDOW 

from core.signals import signals
from utils.config import PROFILE_DIR, ARIA2C_PATH, UBLOCK_CRX_PATH, APP_DIR, sites_data, app_settings, config_lock, progress_lock
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
    try: subprocess.run("taskkill /F /IM chromedriver.exe /T", shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    ps_kill_profile = 'powershell -Command "Get-WmiObject Win32_Process -Filter \\"Name=\'chrome.exe\'\\" | Where-Object {$_.CommandLine -match \'SeleniumProfile\'} | ForEach-Object { $_.Terminate() }"'
    try: subprocess.run(ps_kill_profile, shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    ps_kill_headless = 'powershell -Command "Get-WmiObject Win32_Process -Filter \\"Name=\'chrome.exe\'\\" | Where-Object {$_.CommandLine -match \'--headless\'} | ForEach-Object { $_.Terminate() }"'
    try: subprocess.run(ps_kill_headless, shell=True, creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("prefs", {"profile.exit_type": "Normal", "profile.exited_cleanly": True})
        options.add_argument("--start-maximized")

        log_path = os.path.join(APP_DIR, "chromedriver.log")
        service = Service(service_args=["--log-level=ALL", "--enable-chrome-logs"], log_output=log_path)
        service.creation_flags = CREATE_NO_WINDOW
        manual_driver = webdriver.Chrome(options=options, service=service)
        manual_driver.get("chrome://extensions/")
        signals.update_status.emit("Status: Browser open. Setup extensions/logins, then close & Start!", "#f39c12")
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

    options.add_argument("--enable-features=ParallelDownloading")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")

    if headless: 
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080") 
    else: 
        options.add_argument("--start-maximized") 

    if os.path.exists(UBLOCK_CRX_PATH) and os.path.getsize(UBLOCK_CRX_PATH) > 100000:
        try: options.add_extension(UBLOCK_CRX_PATH)
        except: pass

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    service = Service()
    service.creation_flags = CREATE_NO_WINDOW
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(45) 
    return driver

def aria2c_downloader(ep, url, final_name, cookies, ua, temp_dir, cancel_event, on_episode_completed, process_callback=None, my_task_id=0):
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
            "--connect-timeout=5", "--timeout=10", "--max-tries=5", "--retry-wait=2"
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
                            signals.update_active_bar.emit(ep, pct)
                            signals.update_active_download.emit(ep, f"Speed: {speed}   •   Progress: {pct}%")
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
        next_btn_xpath = parse_smart_xpath(config.get("next_btn_xpath", ""))
        step_paths = config.get("step_paths", {"Path 1": config.get("steps", [])})
        safe_site_name = "".join(c for c in site_key if c not in r'\/:*?"<>|').strip()

        profile_folder_path = os.path.join(download_dir, safe_site_name)
        os.makedirs(profile_folder_path, exist_ok=True)
        VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts')

        def process_downloaded_episode(x, temp_dir):
            if not os.path.exists(temp_dir): return
            import py7zr
            import rarfile
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
        driver = create_browser(download_dir, headless)
        wait = WebDriverWait(driver, 10)

        total_episodes = len(episodes_list)
        signals.update_progress.emit(0, total_episodes)

        for x in episodes_list:
            if (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id): break
            
            while len([t for t in active_engine_threads if t.is_alive()]) >= MAX_CONCURRENT and not (cancel_event.is_set() or CURRENT_TASK_ID != my_task_id):
                time.sleep(1)

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
                                from selenium.webdriver.common.action_chains import ActionChains
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                                time.sleep(0.5)
                                ActionChains(driver).move_to_element(btn).click().perform()
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

                                res = driver.execute_script(js_intercept)
                                if res:
                                    found_data = res
                                    break
                                time.sleep(1)
                                wait_timer += 1
                            
                            if found_data and found_data != "BLOB":
                                signals.update_status.emit(f"Status: ✅ Locked onto Ep {x}! Starting download...", "#2ecc71")                                
                                signals.add_active_download.emit(x)
                                data_obj = json.loads(found_data)
                                dl_url = data_obj['url']
                                dl_fname = data_obj['filename']
                                cookies = driver.get_cookies()
                                ua = driver.execute_script("return navigator.userAgent;")
                                
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
                            ctypes.windll.winmm.mciSendStringW('close custom_audio', None, 0, None)
                            ctypes.windll.winmm.mciSendStringW(f'open "{selected_sound}" alias custom_audio', None, 0, None)
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