"""Install uBlock Origin Lite into the automation browser, headless included.

The engine drives ad-heavy pages, where overlay ads and popunders cause mis-clicks
and junk tabs. A real ad blocker handles that far better than a URL blocklist can:
it also hides the leftover ad containers and disarms popunder scripts.

Getting one in is the hard part. What does NOT work on current Chrome (measured on
151 -- Chrome's own profile records show no extension registered in any case):

* `--load-extension=<dir>`
* `--disable-extensions-except` + `--load-extension`
* `--enable-unsafe-extension-debugging` alongside either
* Selenium's `options.add_extension(<crx>)`

What does work is the DevTools `Extensions.loadUnpacked` command -- but only on the
*browser-level* CDP session. Selenium's `execute_cdp_cmd` talks to the page session,
where the whole domain reports "Method not available", which is what made this look
impossible. Connecting to the browser WebSocket directly succeeds, and the extension
then serves its own pages (dashboard.html loads as "uBO Lite — Dashboard").

The extension has to be re-loaded on each launch, since this does not persist it into
the profile -- fine, as the app controls every launch.
"""

import io
import json
import os
import shutil
import struct
import sys
import urllib.request
import zipfile

from utils.config import APP_DIR

UBLOCK_CRX = "ublock_lite.crx"
EXTENSIONS_DIR = os.path.join(APP_DIR, "extensions")
UBLOCK_DIR = os.path.join(EXTENSIONS_DIR, "ublock_lite")


def _bundle_dir():
    """Where files shipped with the app live: the frozen bundle, or the repo."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "tools")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "tools")


def unpack_crx(crx_path, dest_dir):
    """Extract a .crx into `dest_dir`. Returns True on success.

    A .crx is a signature header followed by an ordinary zip; both the CRX2 and CRX3
    header layouts are handled so the file can come from any Chrome version.
    """
    try:
        with open(crx_path, "rb") as f:
            blob = f.read()
        if blob[:4] != b"Cr24":
            return False
        version = struct.unpack("<I", blob[4:8])[0]
        if version == 2:
            pubkey_len, sig_len = struct.unpack("<II", blob[8:16])
            zip_start = 16 + pubkey_len + sig_len
        else:                                    # CRX3
            zip_start = 12 + struct.unpack("<I", blob[8:12])[0]

        staging = dest_dir + ".part"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(blob[zip_start:])) as z:
            z.extractall(staging)
        if not os.path.exists(os.path.join(staging, "manifest.json")):
            shutil.rmtree(staging, ignore_errors=True)
            return False
        # Swap in only once it is known good, so a half-written directory is never
        # handed to Chrome.
        shutil.rmtree(dest_dir, ignore_errors=True)
        os.replace(staging, dest_dir)
        return True
    except Exception:
        shutil.rmtree(dest_dir + ".part", ignore_errors=True)
        return False


def ensure_unpacked():
    """Path to the unpacked extension, unpacking it on first use. "" if unavailable."""
    if os.path.exists(os.path.join(UBLOCK_DIR, "manifest.json")):
        return UBLOCK_DIR
    for crx in (os.path.join(_bundle_dir(), UBLOCK_CRX),
                os.path.join(APP_DIR, UBLOCK_CRX)):          # legacy location
        if os.path.exists(crx) and unpack_crx(crx, UBLOCK_DIR):
            return UBLOCK_DIR
    # A previous version left an unpacked copy behind; use it rather than nothing.
    legacy = os.path.join(APP_DIR, "ublock_lite_unpacked")
    if os.path.exists(os.path.join(legacy, "manifest.json")):
        return legacy
    return ""


def _browser_websocket_url(driver, timeout=5):
    """The browser-level DevTools socket for a running driver."""
    address = (driver.capabilities.get("goog:chromeOptions", {}) or {}).get("debuggerAddress")
    if not address:
        return ""
    with urllib.request.urlopen(f"http://{address}/json/version", timeout=timeout) as resp:
        return json.load(resp).get("webSocketDebuggerUrl", "")


def load_into(driver, timeout=20):
    """Load the ad blocker into a live driver. Returns its extension id, or "".

    Never raises: an ad blocker is an improvement, not a requirement, so a failure
    here must not stop a download.
    """
    try:
        path = ensure_unpacked()
        if not path:
            return ""
        ws_url = _browser_websocket_url(driver)
        if not ws_url:
            return ""
        import websocket        # websocket-client, bundled with the app
        # suppress_origin keeps Chrome from rejecting the handshake (403) without
        # needing --remote-allow-origins=*, which would let any local process attach.
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Extensions.loadUnpacked",
                                "params": {"path": path}}))
            import time
            deadline = time.time() + timeout
            while time.time() < deadline:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    return message.get("result", {}).get("id", "") or ""
        finally:
            try: ws.close()
            except Exception: pass
    except Exception:
        pass
    return ""
