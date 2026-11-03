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


MIN_EPISODES = 3          # two neighbours have to agree (utils/intro_detect.py)


def skip_status(folder):
    """One season's "Skip opening" status, in words, for the Library's details
    window; "" when the folder has no episodes. Says which episodes have it, which
    were looked at with no opening found (a first episode or a finale often has
    none), and which aren't looked at yet, and why."""
    from utils.storage import episode_spec
    from utils.watch_later import episode_files
    files = episode_files(folder) if folder else {}
    if not files:
        return ""
    if len(files) < MIN_EPISODES:
        return (f"Skip opening: needs {MIN_EPISODES} episodes in the folder to find the "
                f"opening ({len(files)} so far).")
    saved = read_skip_file(folder).get("episodes") or {}
    ready, none, todo = [], [], []
    for ep, path in sorted(files.items()):
        entry = saved.get(os.path.basename(path))
        if not is_current(entry, path):
            todo.append(ep)
        elif entry.get("op"):
            ready.append(ep)
        else:
            none.append(ep)
    if not todo and not none:
        return "Skip opening: ready for every episode."
    parts = []
    if ready:
        parts.append(f"ready for Ep {episode_spec(ready)}")
    if none:
        parts.append(f"no opening found in Ep {episode_spec(none)}")
    if todo:
        when = ("being looked at now" if is_queued(folder) else
                "Find openings to skip, in the card's ··· menu, looks now")
        parts.append(f"Ep {episode_spec(todo)} not looked at yet ({when})")
    return "Skip opening: " + "; ".join(parts) + "."


def is_queued(folder):
    """Is `folder` waiting in the queue, or being looked at now?"""
    key = os.path.normcase(os.path.abspath(folder or ""))
    with _queue_lock:
        busy = [f for f, _force in _queue] + ([_current] if _current else [])
    return any(os.path.normcase(os.path.abspath(f)) == key for f in busy)


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
_current = None           # the folder being looked at now
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
    global _worker, _current
    from utils import intro_detect
    while True:
        with _queue_lock:
            _current = None
            # Cleared under the lock, so a folder queued right now starts a new
            # worker instead of landing behind one that is about to return.
            if _stop.is_set() or not _queue:
                _worker = None
                return
            folder, force = _queue.pop(0)
            _current = folder
        try:
            intro_detect.detect_folder(folder, force=force, should_stop=_stop.is_set)
        except Exception:
            traceback.print_exc()
