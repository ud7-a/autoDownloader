import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from subprocess import CREATE_NO_WINDOW
from core.signals import signals
from utils.config import PROFILE_DIR
from core.selenium_engine import kill_stuck_chrome_processes

def launch_path_picker(tab, base_url):
    signals.update_status.emit("Status: 🎯 Smart Picker Active! Hold SHIFT and Click elements.", "#4cc2ff")
    signals.update_buttons.emit(False, False, False)
    picker_driver = None
    try:
        kill_stuck_chrome_processes()
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_argument("--start-maximized")

        service = Service()
        service.creation_flags = CREATE_NO_WINDOW
        picker_driver = webdriver.Chrome(options=options, service=service)
        
        url_to_load = base_url if base_url else "https://google.com"
        url_to_load = url_to_load.replace("{x}", "1") 
        picker_driver.get(url_to_load)

        js_payload = """
        if (typeof window.pickedXpaths === 'undefined') {
            window.pickedXpaths = [];
            window.lastOutline = null;

            document.addEventListener('mouseover', function(e) {
                if (!e.shiftKey) return;
                if (e.target.dataset.oldOutline === undefined) {
                    e.target.dataset.oldOutline = e.target.style.outline || '';
                }
                e.target.style.outline = '3px solid #4cc2ff';
                e.target.style.cursor = 'crosshair';
            }, true);

            document.addEventListener('mouseout', function(e) {
                if (e.target.dataset.oldOutline !== undefined) {
                    e.target.style.outline = e.target.dataset.oldOutline;
                    delete e.target.dataset.oldOutline; 
                }
                e.target.style.cursor = '';
            }, true);

            document.addEventListener('click', function(e) {
                if (!e.shiftKey) return;
                e.preventDefault();
                e.stopPropagation();

                function getXPath(elm) {
                    if (elm.id !== '') return '//*[@id="' + elm.id + '"]';
                    if (elm === document.body) return '/html/body';
                    let ix = 0;
                    let siblings = elm.parentNode.childNodes;
                    for (let i = 0; i < siblings.length; i++) {
                        let sibling = siblings[i];
                        if (sibling === elm) {
                            return getXPath(elm.parentNode) + '/' + elm.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                        }
                        if (sibling.nodeType === 1 && sibling.tagName === elm.tagName) ix++;
                    }
                }
                
                let xpath = getXPath(e.target);
                window.pickedXpaths.push(xpath);
                
                e.target.style.outline = '3px solid #2ecc71'; 
                setTimeout(() => { 
                    if (e.target.dataset.oldOutline !== undefined) {
                        e.target.style.outline = e.target.dataset.oldOutline;
                    } else {
                        e.target.style.outline = '';
                    }
                }, 500);

            }, true);
        }
        """
        current_tab_id = picker_driver.current_window_handle

        while len(picker_driver.window_handles) > 0:
            try:
                handles = picker_driver.window_handles
                if current_tab_id not in handles or handles[-1] != current_tab_id:
                    current_tab_id = handles[-1]
                    picker_driver.switch_to.window(current_tab_id)

                has_array = picker_driver.execute_script("return typeof window.pickedXpaths !== 'undefined';")
                if not has_array:
                    picker_driver.execute_script(js_payload)
                
                new_xpaths = picker_driver.execute_script("let res = window.pickedXpaths; window.pickedXpaths = []; return res;")
                if new_xpaths:
                    for xp in new_xpaths:
                        signals.add_picked_step.emit(tab, xp)
            except Exception:
                pass 
            time.sleep(0.5)

    except Exception:
        pass 
    finally:
        signals.update_status.emit("Status: 🎯 Smart Picker Closed.", "#ffffff")
        signals.update_buttons.emit(True, False, True)
        if picker_driver:
            try: picker_driver.quit()
            except: pass