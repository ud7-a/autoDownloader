"""Updates from the GitHub releases.

At startup (and from the sidebar's "Check for updates") the latest release is
read; one strictly newer than APP_VERSION is offered (AppWindow.prompt_update).
Accepting downloads its Setup .exe into a fresh temp folder -- with a timeout,
a Cancel button, and checked against the size and SHA-256 GitHub lists for the
asset -- then starts it with --silent and closes the app normally (settings
saved, the search browser shut down). The setup (installer.py) swaps the app
folder and starts the new version.

Only the installed app updates itself: a run from source, or one pointed at
another data folder (AED_APP_DIR), never offers to install over the real one.

Everything goes to updater.log in the app folder: the installed app has no
console, so print() was lost and a broken update left no trace.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from PyQt6.QtCore import QThread, pyqtSignal

from core.signals import signals
from utils.config import APP_DIR, APP_VERSION, IS_ISOLATED

API_URL = "https://api.github.com/repos/ud7-a/autoDownloader/releases/latest"
# The end-to-end update test (Windows Sandbox, a release served from localhost)
# builds with an extra module, aed_update_test, that names its own release URL.
# It isn't in the repository and tools/build_release.py never bundles it, so a
# shipped build has no way to point updates anywhere but GitHub -- no
# environment variable, file or setting.
try:
    from aed_update_test import API_URL  # noqa: F811
except ImportError:
    pass
SETUP_NAME = "AutoDownloader_Setup.exe"
CHECK_TIMEOUT = 10        # seconds, the release lookup
READ_TIMEOUT = 30         # seconds without a byte before the download gives up
CHUNK = 256 * 1024
LOG_LIMIT = 256_000


def log(message):
    """Best effort: a logging problem never breaks an update."""
    try:
        path = os.path.join(APP_DIR, "updater.log")
        try:
            if os.path.getsize(path) > LOG_LIMIT:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _user_agent():
    return f"AutoEpisodesDownloader/{APP_VERSION}"


def parse_version(text):
    """"v5.0.1" -> (5, 0, 1). Trailing zeros are dropped, so 4.9 and 4.9.0 are
    the same version. () when there is no number in it."""
    m = re.search(r"\d+(?:\.\d+)*", text or "")
    if not m:
        return ()
    parts = [int(p) for p in m.group().split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(remote, local=None):
    """Strictly newer only: a different but older release is never an "update"."""
    r = parse_version(remote)
    return bool(r) and r > parse_version(APP_VERSION if local is None else local)


def pick_asset(assets):
    """The release's Setup .exe (what the Release workflow publishes), or None."""
    for asset in assets or []:
        name = str(asset.get("name") or "")
        if "Setup" in name and name.lower().endswith(".exe") and asset.get("browser_download_url"):
            return asset
    return None


def update_info(release, current=None):
    """What to offer from a release's JSON, or None (with the reason logged)."""
    current = APP_VERSION if current is None else current
    tag = str(release.get("tag_name") or "")
    if not is_newer(tag, current):
        log(f"No update: latest release is {tag or '(none)'}, this is {current}.")
        return None
    asset = pick_asset(release.get("assets"))
    if not asset:
        log(f"Release {tag} has no Setup .exe asset; nothing to install.")
        return None
    digest = str(asset.get("digest") or "")
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "version": re.sub(r"^[vV]", "", tag),
        "url": asset["browser_download_url"],
        "size": size,
        "sha256": digest[7:].lower() if digest.lower().startswith("sha256:") else "",
    }


def updates_enabled():
    """Only the installed (frozen) app with the real data folder installs updates."""
    return bool(getattr(sys, "frozen", False)) and not IS_ISOLATED


def fetch_latest(timeout=CHECK_TIMEOUT):
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": _user_agent(), "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_for_updates(manual=False):
    """Look for a newer release. Emits update_available with what to install;
    a manual check also reports "up to date" or the error (update_check_result).
    Returns the info dict, or None."""
    if not updates_enabled():
        log("Update check skipped: not the installed app "
            f"(frozen={bool(getattr(sys, 'frozen', False))}, isolated={IS_ISOLATED}).")
        if manual:
            signals.update_check_result.emit(
                "info", "Updates install only into the installed app, not a copy run from source.")
        return None
    try:
        info = update_info(fetch_latest())
    except Exception as e:
        log(f"Update check failed: {type(e).__name__}: {e}")
        if manual:
            signals.update_check_result.emit("error", f"Couldn't check for updates: {e}")
        return None
    if info is None:
        if manual:
            signals.update_check_result.emit("success", f"You're up to date (version {APP_VERSION}).")
        return None
    info["manual"] = manual
    log(f"Update available: {info['version']} ({info['size']} bytes, "
        f"sha256 {'listed' if info['sha256'] else 'not listed'}).")
    signals.update_available.emit(info)
    return info


def remove_old_downloads(temp_dir=None):
    """Delete the setup folders earlier updates left in %TEMP% (~140 MB each;
    the setup runs from its folder, so it can't be removed at the time)."""
    temp_dir = temp_dir or tempfile.gettempdir()
    try:
        names = os.listdir(temp_dir)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(temp_dir, name)
        if name.startswith("aed_update_") and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed += not os.path.exists(path)
    if removed:
        log(f"Removed {removed} old update download folder(s).")
    return removed


def check_for_updates_silently():
    """The startup check (main.py, background thread)."""
    try:
        if updates_enabled():
            remove_old_downloads()
        check_for_updates(manual=False)
    except Exception as e:
        log(f"Update check crashed: {type(e).__name__}: {e}")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class DownloadCancelled(Exception):
    pass


def download(info, path, progress=None, cancelled=lambda: False, opener=urllib.request.urlopen):
    """Download info["url"] to `path` and check it against info's size and
    SHA-256. Raises on a stall (READ_TIMEOUT), a cancel, or a mismatch -- a
    partly written file is never left behind."""
    expected = int(info.get("size") or 0)
    req = urllib.request.Request(info["url"], headers={"User-Agent": _user_agent()})
    h = hashlib.sha256()
    done = 0
    try:
        with opener(req, timeout=READ_TIMEOUT) as response, open(path, "wb") as f:
            total = int(response.headers.get("Content-Length") or 0) or expected
            last = -1
            while True:
                if cancelled():
                    raise DownloadCancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and total:
                    percent = min(100, done * 100 // total)
                    if percent != last:
                        last = percent
                        progress(percent)
        if expected and done != expected:
            raise IOError(f"the download is incomplete ({done:,} of {expected:,} bytes)")
        if info.get("sha256") and h.hexdigest() != info["sha256"]:
            raise IOError("the downloaded file doesn't match the release (SHA-256 differs)")
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return h.hexdigest()


class UpdateDownloaderThread(QThread):
    """Downloads, verifies and starts the setup -- all off the UI thread: the
    second hash pass and the launch wait on the antivirus scanning a fresh
    139 MB exe, which froze the window for ~9 s in the sandbox test."""
    progress = pyqtSignal(int)
    setup_started = pyqtSignal(str)   # the app should close now
    error = pyqtSignal(str)           # the whole message, ready to show
    cancelled = pyqtSignal()

    def __init__(self, info):
        super().__init__()
        self.info = info

    def run(self):
        # A fresh folder each time: a fixed %TEMP% name could still be open (a
        # previous setup, an antivirus scan) or swapped by something else.
        folder = tempfile.mkdtemp(prefix="aed_update_")
        path = os.path.join(folder, SETUP_NAME)
        log(f"Downloading {self.info['url']} -> {path}")
        try:
            digest = download(self.info, path, progress=self.progress.emit,
                              cancelled=self.isInterruptionRequested)
        except DownloadCancelled:
            log("Download cancelled.")
            shutil.rmtree(folder, ignore_errors=True)
            self.cancelled.emit()
            return
        except Exception as e:
            log(f"Download failed: {type(e).__name__}: {e}")
            shutil.rmtree(folder, ignore_errors=True)
            self.error.emit(f"Couldn't download the update: {e}")
            return
        log(f"Downloaded and verified (sha256 {digest}).")
        ok, why = launch_setup(path, digest)
        if not ok:
            shutil.rmtree(folder, ignore_errors=True)
            self.error.emit(f"Couldn't start the installer: {why}")
            return
        self.setup_started.emit(path)


def launch_setup(path, sha256=""):
    """Start the downloaded setup. Its SHA-256 is checked again first, right
    before it runs. Returns (True, "") or (False, why); the caller closes the
    app only on success."""
    try:
        if sha256 and sha256_of(path) != sha256:
            raise IOError("the setup file changed after it was downloaded")
        # Its own folder as the working directory: inherited from the app it was
        # the app folder, which the setup's helper then couldn't delete ("in
        # use") -- it copied over the old files instead of replacing them.
        subprocess.Popen([path, "--silent"], close_fds=True, cwd=os.path.dirname(path))
    except Exception as e:
        log(f"Could not start the installer: {type(e).__name__}: {e}")
        return False, str(e)
    log(f"Installer started: {path}")
    return True, ""
