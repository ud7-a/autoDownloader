"""Test package bootstrap.

Redirects the app's data directory to a throwaway temp folder BEFORE any app module
is imported, so tests can never read or overwrite the real config, history database
or browser profile under C:\\Auto Episodes Downloader.

This runs first because `unittest discover` imports the package before its modules.
"""

import os
import tempfile

_ISOLATED_DIR = os.path.join(tempfile.gettempdir(), "AutoEpisodesDownloader-tests")
os.makedirs(_ISOLATED_DIR, exist_ok=True)
os.environ["AED_APP_DIR"] = _ISOLATED_DIR
# The keyboard shortcuts live in the user's Start menu: tests get their own
# folder, so they never read -- let alone write -- the real ones (utils/shortcut.py).
_SHORTCUT_DIR = os.path.join(_ISOLATED_DIR, "shortcuts")
os.makedirs(_SHORTCUT_DIR, exist_ok=True)
os.environ["AED_SHORTCUT_DIR"] = _SHORTCUT_DIR
# mpv's resume files: libmpv keeps them in %LOCALAPPDATA%\mpv\watch_later
# (utils/library_scan.py); tests get an empty folder instead of the user's.
_MPV_STATE_DIR = os.path.join(_ISOLATED_DIR, "mpv-state")
os.makedirs(_MPV_STATE_DIR, exist_ok=True)
os.environ["AED_MPV_STATE_DIR"] = _MPV_STATE_DIR
