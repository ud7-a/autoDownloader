"""Free up space: delete watched episodes that are N behind (utils/watch_later.py
decides which, `cleanup_candidates`); this module removes the files and records it.

Two settings in sites_config.json's "settings":
    cleanup_keep        0 = off (default), else how many watched episodes to keep
                        (3 or 5; a 1 or 2 saved by 5.1 reads as 3)
    cleanup_permanent   False = Recycle Bin (default), True = delete for good

Runs after mpv.net's progress log is read -- in the Library tab and in the Continue
shortcut (utils/quick_play.py), which has no Qt -- so standard library only.
"""
import ctypes
import os
import sys

from utils import watch_later as wl

KEEP_SETTING = "cleanup_keep"
PERMANENT_SETTING = "cleanup_permanent"
# No 1 or 2: a folder never goes below 3 episodes (wl.MIN_EPISODES_KEPT, for skip
# times), so once caught up they kept the same 3 files as "keep 3".
KEEP_CHOICES = (0, 3, 5)

# SHFileOperationW: FO_DELETE and its flags.
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
# Asks before deleting for good a file too big for the Recycle Bin. Without it,
# FOF_NOCONFIRMATION lets Windows delete it permanently without a word -- the
# opposite of what choosing the Recycle Bin promised.
_FOF_WANTNUKEWARNING = 0x4000


def keep_setting(settings):
    """How many watched episodes to keep, or 0 when clean-up is off."""
    try:
        keep = int((settings or {}).get(KEEP_SETTING) or 0)
    except (TypeError, ValueError):
        return 0
    return max(keep, KEEP_CHOICES[1]) if keep > 0 else 0


def _recycle(path):
    """Move one file to the Recycle Bin. True when it is gone from its folder."""
    if sys.platform != "win32":
        return False

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    op = SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"          # the list ends with a second NUL
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
                 | _FOF_WANTNUKEWARNING)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


def remove_files(paths, permanent=False, recycle=None):
    """Delete `paths`. Returns (removed, failed, bytes freed). A file in use (open
    in mpv.net) fails and is simply tried again next time; no error is shown.
    "Freed" counts what left its folder -- in the Recycle Bin it is reclaimed
    once the bin is emptied."""
    recycle = recycle or _recycle
    removed, failed, freed = [], [], 0
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue                                  # already gone
        try:
            if permanent:
                os.remove(path)
                ok = True
            else:
                ok = recycle(path)
        except OSError:
            ok = False
        if ok:
            removed.append(path)
            freed += size
        else:
            failed.append(path)
    return removed, failed, freed


def clean(download_dir, keep, permanent=False, url=None, finished_only=True, recycle=None):
    """Delete what `wl.cleanup_candidates` picks, in every anime (or only `url`),
    and record it so "Download missing" leaves those episodes alone.

    Returns [{"title", "label", "episodes", "freed", "failed"}], one per season
    that lost files or couldn't."""
    if url is not None:
        one = wl.find(url)
        targets = [one] if one else []
    else:
        targets = wl.entries()
    report = []
    for entry in targets:
        for part_index, items in wl.cleanup_candidates(entry, download_dir, keep,
                                                       finished_only=finished_only).items():
            eps_of = {path: ep for ep, path in items}
            removed, failed, freed = remove_files(list(eps_of), permanent, recycle)
            if not removed and not failed:
                continue
            eps = sorted(eps_of[p] for p in removed)
            if eps:
                wl.mark_cleaned(entry.get("url"), part_index, eps,
                                wl.part_ident(entry["parts"][part_index]))
            report.append({"title": entry.get("title", "Anime"),
                           "label": entry["parts"][part_index].get("label") or "",
                           "episodes": eps, "freed": freed, "failed": len(failed)})
    return report


def auto_clean(settings, download_dir, recycle=None):
    """The automatic pass after playback was read: only episodes finished in
    mpv.net, and only when the user turned it on."""
    keep = keep_setting(settings)
    if not keep or not download_dir:
        return []
    return clean(download_dir, keep, bool((settings or {}).get(PERMANENT_SETTING)),
                 recycle=recycle)


def episode_spec(eps):
    """[1, 2, 3, 7] -> "1-3, 7"."""
    out, run = [], []
    for ep in sorted(set(eps)):
        if run and ep != run[-1] + 1:
            out.append(run)
            run = []
        run.append(ep)
    if run:
        out.append(run)
    return ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in out)


def size_text(n):
    gb = n / 1024 ** 3
    return f"{gb:.1f} GB" if gb >= 1 else f"{max(1, round(n / 1024 ** 2))} MB"


def describe(report):
    """("Freed 1.2 GB", "Sousou no Frieren Ep 15-16") for a notification, or None
    when nothing was deleted."""
    done = [r for r in report if r["episodes"]]
    if not done:
        return None
    total = sum(r["freed"] for r in done)
    lines = []
    for r in done:
        season = f" {r['label']}" if r["label"] else ""
        lines.append(f"{r['title']}{season} Ep {episode_spec(r['episodes'])}")
    return f"Freed {size_text(total)}", "; ".join(lines)
