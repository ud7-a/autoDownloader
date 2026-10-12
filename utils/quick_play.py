"""The "Continue watching" shortcut (first called Play next): continue what
you're watching in mpv.net, without opening the app window. An episode left
part-way comes first (mpv.net resumes it where it stopped: save-position-on-quit
in the mpv.conf the app installs); otherwise the next one.

Runs from the very top of main.py (--play-next), before Qt is imported -- only
the Library's JSON, the settings file and mpv.net are touched, so the episode
starts about as fast as Windows can launch a program.

What it picks: the anime on the Watching list you played most recently (the
Library's Continue watching order). Its next episode plays, with the rest of
its season queued. If that episode isn't downloaded, nothing else plays
instead: the app opens on the Downloader with that anime's missing episodes
selected (main.py, AppWindow.download_next). An anime with nothing left to
watch is skipped for the next one.

First, mpv.net's progress log is read into the Library -- the app normally
does that only while the Library tab is open, so after watching with the app
closed, "last played" and "watched" would otherwise be out of date.

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


def catch_up(mpvnet_exe):
    """Fold what mpv.net logged since the Library last looked into it."""
    if not mpvnet_exe:
        return False
    from utils.mpvnet import config_dir, PROGRESS_LOG
    try:
        return wl.ingest_log(os.path.join(config_dir(mpvnet_exe), PROGRESS_LOG))
    except Exception:
        return False


def decide(entries, download_dir):
    """What the shortcut does, as one of:

      ("play", entry, part index, episode, [files from it on])
      ("download", entry, part index, episode)   -- its next episode isn't on disk
      None                                       -- nothing to continue

    Watching anime, most recently played first (then newest added). The first
    one with an episode left decides: played if downloaded, else downloaded --
    never quietly swapped for a different anime. One that can't be downloaded
    (found only as a folder, no site page) is passed over."""
    def recency(e):
        played = (e.get("last_played") or {}).get("ts") or 0
        return (played, e.get("added") or 0)

    for entry in sorted((e for e in entries if e.get("status") == wl.WATCHING),
                        key=recency, reverse=True):
        nxt = wl.next_episode(entry, download_dir)
        if not nxt:
            continue                                   # all watched: the next anime
        part_index, ep, file, _percent = nxt
        if file:
            files = wl.playlist_from(entry, part_index, ep, download_dir)
            if files:
                return "play", entry, part_index, ep, files
        if (entry.get("parts") or [{}])[part_index].get("template"):
            return "download", entry, part_index, ep
    return None


def pick_next(entries, download_dir):
    """(entry, part index, episode, files) when the next episode can play now."""
    found = decide(entries, download_dir)
    return found[1:] if found and found[0] == "play" else None


def play_next():
    """Do what the shortcut does. Returns (status, message, request):

      ("played", ..., None)
      ("download", ..., {"url", "part", "ep", "title"}) -- for the app to open
      ("nothing", ..., None)"""
    from utils.mpvnet import find_mpvnet
    settings = _settings()
    exe = find_mpvnet()[0]     # also where the progress log is, whatever plays
    catch_up(exe)
    found = decide(wl.entries(), settings.get("download_dir", ""))
    if found is None:
        return "nothing", ("Nothing to continue: no anime on your Watching list has an "
                           "episode left."), None
    if found[0] == "download":
        _kind, entry, part_index, ep = found
        title = entry.get("title", "Anime")
        return "download", f"{title} episode {ep} isn't downloaded yet.", {
            "url": entry.get("url", ""), "part": part_index, "ep": ep, "title": title}
    _kind, entry, _part, ep, files = found
    if settings.get("play_in_mpvnet", True) and exe:
        # The rest of the season follows as mpv.net's playlist, as "Continue
        # watching" in the Library does.
        subprocess.Popen([exe, *files], close_fds=True)
    else:
        os.startfile(files[0])
    return "played", f"Playing {entry.get('title', 'Anime')} episode {ep}.", None
