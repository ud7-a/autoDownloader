"""Fill the Library from what is already on disk.

Runs once when the Library first opens after the update (and again from the tab's
"Scan anime folder" button). Every per-anime folder in the download folder, and
every saved profile, becomes a Library entry:

  * at least one episode downloaded -> Watching (Completed if every known episode
    was watched), otherwise Watch later;
  * what was already watched comes from mpv.net's resume files (watch_later\\*,
    "start=<seconds>") and the app's own progress log, so viewing done before the
    Library existed isn't lost.

An episode counts as watched when its resume point is at least 90% in, or the
progress log saw it finish; reaching episode N counts the episodes before it as
seen, same as live tracking.

Stdlib only, no Qt.
"""
import json
import os
import re
import struct
import time

from utils import watch_later as wl
from utils.naming import safe_folder_name

LOCAL_SCHEME = "local://"       # entry key for a folder with no known anime page


# ------------------------------------------------------------ mpv.net history

def read_mpv_history(cfg_dir):
    """{normcased path: resume seconds} from mpv.net's resume files, plus
    {normcased path: None} for files the app's own progress log saw finished.

    mpv.net's recent-files list is NOT used: opening a playlist puts every file
    in it on that list, watched or not (all 12 episodes of a show appeared there
    after "Start Watching" opened them as one playlist)."""
    history = {}
    if not cfg_dir:
        return history
    from utils.mpvnet import PROGRESS_LOG
    try:
        with open(os.path.join(cfg_dir, PROGRESS_LOG), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        # Each line on its own: one odd line never costs the ones after it.
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        path = os.path.normcase(str(rec.get("path") or ""))
        try:
            percent = float(rec.get("percent") or 0)
        except (TypeError, ValueError, OverflowError):
            percent = 0.0
        if path and (rec.get("eof") or percent >= wl.WATCHED_PERCENT):
            history[path] = None
    folder = os.path.join(cfg_dir, "watch_later")
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        if not lines or not lines[0].startswith("# ") or "redirect entry" in lines[0]:
            continue
        path = lines[0][2:].strip()
        start = next((l.split("=", 1)[1] for l in lines[1:] if l.startswith("start=")), None)
        try:
            # A finish seen in the progress log outranks an older resume point.
            history.setdefault(os.path.normcase(path), float(start))
        except (TypeError, ValueError):
            continue
    return history


def mp4_duration(path):
    """Length in seconds from an MP4's movie header (moov/mvhd), or None."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos = 0
            while pos + 8 <= size:
                f.seek(pos)
                head = f.read(8)
                box_size, kind = struct.unpack(">I4s", head)
                header = 8
                if box_size == 1:
                    box_size = struct.unpack(">Q", f.read(8))[0]
                    header = 16
                elif box_size == 0:
                    box_size = size - pos
                if box_size < header:
                    return None
                if kind == b"moov":
                    end = pos + box_size
                    inner = pos + header
                    while inner + 8 <= end:
                        f.seek(inner)
                        isize, ikind = struct.unpack(">I4s", f.read(8))
                        if ikind == b"mvhd":
                            version = f.read(1)[0]
                            f.read(3)
                            if version == 1:
                                f.read(16)
                                scale, dur = struct.unpack(">IQ", f.read(12))
                            else:
                                f.read(8)
                                scale, dur = struct.unpack(">II", f.read(8))
                            return dur / scale if scale else None
                        if isize < 8:
                            return None
                        inner += isize
                    return None
                pos += box_size
    except (OSError, struct.error, IndexError):
        return None
    return None


def _ebml_vint(f, is_id):
    """One EBML variable-length integer: (value, unknown size?). An element ID
    keeps its length marker; a size drops it (all ones = unknown size)."""
    b = f.read(1)
    if not b:
        raise EOFError
    first, length, mask = b[0], 1, 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad EBML length")
    rest = f.read(length - 1)
    if len(rest) != length - 1:
        raise EOFError
    value = first if is_id else first & (mask - 1)
    for c in rest:
        value = (value << 8) | c
    unknown = not is_id and value == (1 << (7 * length)) - 1
    return value, unknown


_MKV_SEGMENT, _MKV_INFO, _MKV_SCALE, _MKV_DURATION = 0x18538067, 0x1549A966, 0x2AD7B1, 0x4489


def mkv_duration(path):
    """Length in seconds of a Matroska / WebM file (Segment > Info > Duration),
    or None."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if _ebml_vint(f, True)[0] != 0x1A45DFA3:          # EBML header
                return None
            head_size, _ = _ebml_vint(f, False)
            f.seek(head_size, 1)
            if _ebml_vint(f, True)[0] != _MKV_SEGMENT:
                return None
            seg_size, unknown = _ebml_vint(f, False)
            pos = f.tell()
            end = size if unknown else min(size, pos + seg_size)
            for _ in range(100000):                 # Info comes early; never loop forever
                if pos >= end:
                    return None
                f.seek(pos)
                eid, _ = _ebml_vint(f, True)
                esize, eunknown = _ebml_vint(f, False)
                body = f.tell()
                if eid == _MKV_INFO and not eunknown:
                    scale, dur, p = 1000000, None, body
                    while p < body + esize:
                        f.seek(p)
                        cid, _ = _ebml_vint(f, True)
                        csize, _ = _ebml_vint(f, False)
                        start = f.tell()
                        raw = f.read(min(csize, 16))
                        if cid == _MKV_SCALE and raw:
                            scale = int.from_bytes(raw, "big")
                        elif cid == _MKV_DURATION and csize in (4, 8):
                            dur = struct.unpack(">f" if csize == 4 else ">d", raw)[0]
                        p = start + csize
                    return dur * scale / 1e9 if dur and scale else None
                if eunknown:
                    return None                     # can't step over it
                pos = body + esize
    except (OSError, EOFError, ValueError, struct.error):
        return None
    return None


def avi_duration(path):
    """Length in seconds of an AVI (main header frame time x frame count; the
    OpenDML header's count when present, since avih only counts the first 1 GB)."""
    try:
        with open(path, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"AVI ":
                return None
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"LIST" or head[8:12] != b"hdrl":
                return None
            end = 12 + 8 + struct.unpack("<I", head[4:8])[0]
            us_per_frame = frames = None
            pos = f.tell()
            while pos + 8 <= end:
                f.seek(pos)
                cid, csize = struct.unpack("<4sI", f.read(8))
                if cid == b"avih":
                    data = f.read(min(csize, 56))
                    if len(data) >= 20:
                        us_per_frame = struct.unpack("<I", data[0:4])[0]
                        frames = frames or struct.unpack("<I", data[16:20])[0]
                elif cid == b"LIST" and f.read(4) == b"odml":
                    sub = f.read(12)
                    if len(sub) == 12 and sub[:4] == b"dmlh":
                        frames = struct.unpack("<I", sub[8:12])[0] or frames
                pos += 8 + csize + (csize & 1)
            if us_per_frame and frames:
                return us_per_frame * frames / 1e6
    except (OSError, struct.error):
        return None
    return None


def video_duration(path):
    """Length in seconds of a downloaded episode, by its container, or None."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in (".mp4", ".m4v", ".mov"):
        return mp4_duration(path)
    if ext in (".mkv", ".webm"):
        return mkv_duration(path)
    if ext == ".avi":
        return avi_duration(path)
    return None


def watch_state(files, history, duration=video_duration):
    """(watched episodes, {ep: percent} still in progress) for one anime folder,
    from mpv.net's history. `files` is {episode: path}."""
    watched, progress = set(), {}
    for ep, path in files.items():
        key = os.path.normcase(path)
        if key not in history:
            continue
        start = history[key]
        if start is None:
            watched.add(ep)
            continue
        length = duration(path)
        percent = (start / length * 100) if length else 0.0
        if percent >= wl.WATCHED_PERCENT:
            watched.add(ep)
        else:
            progress[ep] = round(min(percent, 99.0), 1)
    reached = set(watched) | set(progress)
    if reached:
        first = min(files) if files else 1
        watched |= set(range(first, max(reached)))
    for ep in watched:
        progress.pop(ep, None)
    return watched, progress


# ---------------------------------------------------------------- the scan

def _episode_numbers(spec):
    from ui.downloader_tab import spec_to_ranges
    return [e for a, b in spec_to_ranges(spec or "") for e in range(a, b + 1)]


def _part_from_profile(name, profile, files, watch=None, note=None):
    """A Library part for a saved profile (or a folder), with its episode range.
    `note` is the folder's source note (where its episodes were downloaded from)."""
    from ui.search_tab import episode_bounds
    bounds = episode_bounds(profile or {})
    eps = set(files) | set(_episode_numbers((profile or {}).get("last_episodes")))
    first = bounds[0] if bounds else (min(eps) if eps else 1)
    last = bounds[1] if bounds else (max(eps) if eps else 0)
    if watch and watch.get("latest_max"):
        last = max(last, int(watch["latest_max"]))
    note = note or {}
    template = ((profile or {}).get("url") or (watch or {}).get("latest_template", "")
                or note.get("template", ""))
    sources = {k: v for k, v in (note.get("episodes") or {}).items() if isinstance(v, dict)}
    return {"label": "", "template": template, "max_ep": last, "first_ep": first,
            "profile": name, "watched": [], "progress": {}, "sources": sources}


def _match_watch(folder_name, template, watchlist):
    """The Watchlist entry for the same anime (it knows the page, poster and the
    newest episode), by episode template or by the folder its downloads use."""
    from ui.search_tab import _template_key
    key = _template_key(template) if template else None
    for w in watchlist or []:
        if key and _template_key(w.get("latest_template", "")) == key:
            return w
        if safe_folder_name(w.get("title", "")).lower() == folder_name.lower():
            return w
    return None


def _make_entry(title, folder_name, profile_name, profile, path, watchlist, history,
                duration=video_duration):
    """A Library entry for one anime folder (at `path`, or None when it doesn't
    exist yet) and/or saved profile, or None for a folder that isn't an anime."""
    files = wl.episode_files(path) if path else {}
    note = wl.read_folder_source(path) if path else {}
    watch = _match_watch(folder_name, (profile or {}).get("url", "") or note.get("template", ""),
                         watchlist)
    part = _part_from_profile(profile_name, profile, files, watch, note)
    if not files and not part["template"]:
        return None                               # an unrelated folder
    watched, progress = watch_state(files, history or {}, duration)
    part["watched"] = sorted(watched)
    part["progress"] = {str(k): v for k, v in progress.items()}
    url = (watch or {}).get("url") or LOCAL_SCHEME + folder_name
    from ui.search_tab import extract_domain
    entry = {
        "url": url, "title": (watch or {}).get("title") or title,
        "domain": ((watch or {}).get("domain") or note.get("site")
                   or extract_domain(part["template"])),
        "cover": (watch or {}).get("cover", ""), "status": wl.LATER,
        "added": 0, "parts": [part], "history": [], "last_played": None,
        "imported": True,
    }
    if files:
        entry["status"] = wl.COMPLETED if wl.is_finished(entry) else wl.WATCHING
    return entry


def entry_for_profile(name, profile, download_dir, watchlist, history=None):
    """The Library entry for one saved (or running) profile -- used when a profile
    that isn't in the Library downloads something."""
    if not name or not isinstance(profile, dict):
        return None
    folder_name = safe_folder_name(name)
    path = os.path.join(download_dir, folder_name) if download_dir else ""
    entry = _make_entry(name, folder_name, name, profile,
                        path if path and os.path.isdir(path) else None, watchlist, history)
    if entry is not None:
        entry["added"] = time.time()
        entry["imported"] = False
    return entry


def build_entries(download_dir, profiles, watchlist, history, duration=video_duration):
    """Entries for every anime folder / saved profile, not yet in the Library."""
    folders = {}
    try:
        for name in sorted(os.listdir(download_dir)):
            path = os.path.join(download_dir, name)
            if os.path.isdir(path):
                folders[name.lower()] = (name, path)
    except (OSError, TypeError):
        pass

    entries, seen = [], set()

    def make(title, folder_name, profile_name, profile):
        path = folders.get(folder_name.lower(), (None, None))[1]
        return _make_entry(title, folder_name, profile_name, profile, path, watchlist,
                           history, duration)

    for name, profile in (profiles or {}).items():
        if not isinstance(profile, dict) or profile.get("_transient"):
            continue
        folder_name = safe_folder_name(name)
        entry = make(name, folder_name, name, profile)
        seen.add(folder_name.lower())
        if entry:
            entries.append(entry)
    for key, (name, _path) in folders.items():
        if key in seen:
            continue
        entry = make(name, name, name, None)
        if entry:
            entries.append(entry)
    return entries


def import_library(download_dir, profiles, watchlist, history, duration=video_duration):
    """Add every anime found on disk that the Library doesn't have yet (matched by
    page URL or by its download folder). Returns how many were added."""
    candidates = build_entries(download_dir, profiles, watchlist, history, duration)
    return wl.add_imported(candidates)


def is_local(url):
    return (url or "").startswith(LOCAL_SCHEME)


# ------------------------------------------------------------- posters
#
# Imported anime have no poster: nothing on disk stores one. The Library looks
# each one up on its site the way Search does and takes the matching result's
# poster (and its page, which an entry found only as a folder doesn't know).

def normalize_title(title):
    """Letters and digits only, lowercased: "Fate/Zero" and "FateZero" agree."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def query_variants(title):
    """Searches to try, best first. Folder names lose punctuation ("Fate/Zero" is
    saved as "FateZero", which the site's search can't find), so a spaced-out
    version and a shorter one follow the name as-is."""
    title = (title or "").strip()
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title)
    spaced = re.sub(r"[^\w\s]", " ", spaced)
    spaced = " ".join(spaced.split())
    short = " ".join(spaced.split()[:3])
    out = []
    for q in (title, spaced, short):
        if q and q not in out:
            out.append(q)
    return out


def slug_from_template(template):
    """The anime's URL name from a witanime episode template, or ""
    (/watch/<slug>/{x} and /episode/<slug>-الحلقة-{x}/)."""
    from urllib.parse import unquote, urlsplit
    path = unquote(urlsplit(template or "").path)
    m = re.match(r"^/watch/([^/]+)/\{x\}", path) or \
        re.match(r"^/episode/(.+?)-الحلقة-\{x\}", path)
    return m.group(1).lower() if m else ""


def match_result(title, template, results):
    """The search result that is this anime, or None. Same URL name as its episode
    links first, then the exact title; never just the closest -- a wrong poster
    (and a wrong page link) is worse than none."""
    slug = slug_from_template(template)
    if slug:
        for r in results:
            if (r.get("link") or "").rstrip("/").lower().endswith("/anime/" + slug):
                return r
    want = normalize_title(title)
    if not want:
        return None
    for r in results:
        if normalize_title(r.get("title")) == want:
            return r
    return None
