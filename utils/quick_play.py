"""The "Play next episode" shortcut: start the next episode of what you're
watching in mpv.net, without opening the app window.

Runs from the very top of main.py (--play-next), before Qt is imported -- only
the Library's JSON, the settings file and mpv.net are touched, so the episode
starts about as fast as Windows can launch a program. It picks what the
Library's Continue watching strip would show first: the anime you played most
recently that has its next episode on disk.

Standard library only.
"""
import json
import os
import subprocess

from utils import watch_later as wl


def _settings():
    from utils.config import CONFIG_FILE
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    settings = data.get("settings") if isinstance(data, dict) else None
    return settings if isinstance(settings, dict) else {}


def pick_next(entries, download_dir):
    """(entry, part index, episode, [files from it on]) for the anime to continue,
    or None. Most recently played first, then the newest added; only anime on
    the Watching list whose next episode is downloaded."""
    def recency(e):
        played = (e.get("last_played") or {}).get("ts") or 0
        return (played, e.get("added") or 0)

    for entry in sorted((e for e in entries if e.get("status") == wl.WATCHING),
                        key=recency, reverse=True):
        nxt = wl.next_episode(entry, download_dir)
        if not nxt or not nxt[2]:
            continue
        part_index, ep, _file, _percent = nxt
        files = wl.playlist_from(entry, part_index, ep, download_dir)
        if files:
            return entry, part_index, ep, files
    return None


def play_next():
    """Start the next episode. Returns (played, message)."""
    settings = _settings()
    found = pick_next(wl.entries(), settings.get("download_dir", ""))
    if found is None:
        return False, "Nothing to continue: no anime you're watching has its next episode downloaded."
    entry, _part, ep, files = found
    exe = None
    if settings.get("play_in_mpvnet", True):
        from utils.mpvnet import find_mpvnet
        exe = find_mpvnet()[0]
    if exe:
        # The rest of the season follows as mpv.net's playlist, as "Continue
        # watching" in the Library does.
        subprocess.Popen([exe, *files], close_fds=True)
    else:
        os.startfile(files[0])
    return True, f"Playing {entry.get('title', 'Anime')} episode {ep}."
