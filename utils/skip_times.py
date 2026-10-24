"""Openings and endings to skip: where they're saved, the player settings, and
the background queue that finds them.

utils/intro_detect.py does the finding (numpy, libmpv); this module stays light
so the Library and the Video Player tab can read counts and settings without
loading any of that.

  <anime folder>/.aed-skip.json   hidden, written by the detector:
      {"version": 1, "episodes": {"<file name>": {"size", "mtime",
                                  "op": [start, end] | null, "ed": ... }}}
  <mpv.net config>/script-opts/aed-skip.conf   what the player does with them,
      read by scripts/aed-skip.lua: opening=ask, ending=off (off / ask / auto).
"""

import json
import os
import threading
import traceback

SKIP_FILE = ".aed-skip.json"
SETTINGS_FILE = os.path.join("script-opts", "aed-skip.conf")
MODES = ("off", "ask", "auto")
DEFAULTS = {"opening": "ask", "ending": "off"}


# ----------------------------------------------------------------- skip file

def read_skip_file(folder):
    try:
        with open(os.path.join(folder, SKIP_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def stamp(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def is_current(entry, path):
    """Was this entry made for this exact file (same size and modification time)?"""
    try:
        now = stamp(path)
    except OSError:
        return False
    return (isinstance(entry, dict) and entry.get("size") == now["size"]
            and entry.get("mtime") == now["mtime"])


def skip_counts(folder):
    """(episodes whose opening is known, video episodes in the folder)."""
    from utils.watch_later import episode_files
    files = episode_files(folder) if folder else {}
    eps = read_skip_file(folder).get("episodes") or {}
    found = 0
    for path in files.values():
        entry = eps.get(os.path.basename(path))
        if entry and entry.get("op") and is_current(entry, path):
            found += 1
    return found, len(files)


# ------------------------------------------------------------ player settings

def read_settings(cfg_dir):
    """{"opening": mode, "ending": mode} from the player's script-opts file."""
    out = dict(DEFAULTS)
    try:
        with open(os.path.join(cfg_dir, SETTINGS_FILE), encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key in out and value.strip() in MODES:
                    out[key] = value.strip()
    except OSError:
        pass
    return out


def write_settings(cfg_dir, opening, ending):
    if opening not in MODES or ending not in MODES or not cfg_dir:
        return False
    path = os.path.join(cfg_dir, SETTINGS_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Written by Auto Episodes Downloader (Video Player tab).\n"
                    "# off / ask (show \"Skip\" with the K key) / auto\n"
                    f"opening={opening}\nending={ending}\n")
    except OSError:
        return False
    return True


# ----------------------------------------------------------- background queue

_queue = []
_queue_lock = threading.Lock()
_worker = None
_stop = threading.Event()


def queue_folder(folder, force=False):
    """Find openings/endings in `folder` in the background, one folder at a time
    (a download finishing, the Library opening, or its "Find skip times")."""
    global _worker
    if not folder or not os.path.isdir(folder) or _stop.is_set():
        return False
    with _queue_lock:
        for i, (queued, queued_force) in enumerate(_queue):
            if queued == folder:
                _queue[i] = (folder, queued_force or force)
                return True
        _queue.append((folder, force))
        if _worker is None:
            _worker = threading.Thread(target=_run, name="skip-times", daemon=True)
            _worker.start()
    return True


def queue_library(download_dir):
    """Every anime folder in the download folder. Only new or replaced episodes
    get decoded, so doing this on every visit is cheap."""
    try:
        names = sorted(os.listdir(download_dir)) if download_dir else []
    except OSError:
        return 0
    queued = 0
    for name in names:
        path = os.path.join(download_dir, name)
        if os.path.isdir(path) and queue_folder(path):
            queued += 1
    return queued


def stop(wait=0.0):
    """App closing: finish the episode being decoded, start no other."""
    _stop.set()
    with _queue_lock:
        _queue.clear()
        worker = _worker
    if worker is not None and wait > 0:
        worker.join(wait)


def _run():
    global _worker
    from utils import intro_detect
    while True:
        with _queue_lock:
            # Cleared under the lock, so a folder queued right now starts a new
            # worker instead of landing behind one that is about to return.
            if _stop.is_set() or not _queue:
                _worker = None
                return
            folder, force = _queue.pop(0)
        try:
            intro_detect.detect_folder(folder, force=force, should_stop=_stop.is_set)
        except Exception:
            traceback.print_exc()
