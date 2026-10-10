"""Watch Later: the anime the user plans to watch, is watching, or has finished.

Kept in its own file (APP_DIR/watch_later.json), not in sites_config.json: an entry
exists before any profile does -- adding an anime never creates one. A profile is
linked to an entry's part only when the user starts downloading it.

Entry:
    {"url", "title", "domain", "cover", "status", "added",
     "parts": [{"label", "template", "max_ep", "first_ep", "profile", "watched": [eps],
                "progress": {"<ep>": percent}}],
     "history": [{"date", "profile", "episodes", "status", "notes"}],
     "last_played": {"profile", "ep", "ts"}}

One entry per anime. A show with several seasons keeps them as parts; a flat show
has a single part.

Status moves on its own only when something happens (a download finishes, an
episode is played, the last episode is watched), never on a recompute -- so a
status the user picked by hand stays put until the next such event.

Pure logic and file I/O only (no Qt), so it is unit-testable.
"""
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from utils.config import APP_DIR
from utils.naming import safe_folder_name, VIDEO_EXTENSIONS

FILE = os.path.join(APP_DIR, "watch_later.json")

LATER, WATCHING, COMPLETED = "later", "watching", "completed"
STATUSES = (WATCHING, LATER, COMPLETED)
STATUS_LABELS = {WATCHING: "Watching", LATER: "Watch later", COMPLETED: "Completed"}

# An episode counts as watched once playback got this far (credits are skipped).
WATCHED_PERCENT = 90.0

_lock = threading.RLock()
# "Ep<n>" not glued to a letter, so "Sleep2 Ep5" is episode 5, not 2. The app
# names files "<name> Ep<n>.<ext>", so the last match in the name is the one.
_EP_RE = re.compile(r"(?<![A-Za-z])Ep\s*(\d+)(?!\d)", re.IGNORECASE)


def episode_number(filename):
    """The episode in a file name ("Show Ep7.mp4" -> 7), or None."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    found = _EP_RE.findall(stem)
    return int(found[-1]) if found else None


# ------------------------------------------------------------------ storage

def _empty():
    return {"entries": [], "log_offset": 0}


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return _empty()
    data.setdefault("log_offset", 0)
    # A hand-edited or damaged file: drop what isn't an entry, and a status the
    # app doesn't know becomes Watch later (it would otherwise be on no list).
    data["entries"] = [e for e in data["entries"] if isinstance(e, dict)]
    for e in data["entries"]:
        if e.get("status") not in STATUSES:
            e["status"] = LATER
        if not isinstance(e.get("parts"), list):
            e["parts"] = []
    return data


def _save(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


def _norm_url(url):
    return re.sub(r"^https?://(www\.)?", "", (url or "").strip()).rstrip("/").lower()


def _find(data, url):
    """The entry for `url` -- its own, or one it had before (a folder-only entry
    keeps its local:// address as an alias once its page is found, so a dialog
    or button still holding the old one reaches it)."""
    key = _norm_url(url)
    if not key:
        return None
    for e in data["entries"]:
        if _norm_url(e.get("url")) == key:
            return e
    for e in data["entries"]:
        if any(_norm_url(a) == key for a in e.get("aliases") or []):
            return e
    return None


def entries():
    """A snapshot of every entry (copies, safe to read off the lock)."""
    with _lock:
        return json.loads(json.dumps(_load()["entries"]))


def find(url):
    with _lock:
        e = _find(_load(), url)
        return json.loads(json.dumps(e)) if e else None


def add(title, url, domain, cover=""):
    """Add an anime. Returns False when it is already on the list."""
    with _lock:
        data = _load()
        if not url or _find(data, url):
            return False
        data["entries"].append({
            "url": url, "title": title or "Anime", "domain": domain or "",
            "cover": cover or "", "status": LATER, "added": time.time(),
            "parts": [], "history": [], "last_played": None,
        })
        _save(data)
        return True


def _folders_of(entry):
    return {safe_folder_name(p["profile"]).lower()
            for p in entry.get("parts", []) if p.get("profile")}


def add_imported(candidates):
    """Add entries found on disk (utils/library_scan). One already in the Library
    -- same page URL, or a part downloading into the same folder -- is skipped.
    Returns how many were added."""
    added = 0
    with _lock:
        data = _load()
        for cand in candidates or []:
            folders = _folders_of(cand)
            if _find(data, cand.get("url")) or any(
                    folders & _folders_of(e) for e in data["entries"]):
                continue
            data["entries"].append(cand)
            added += 1
        data["imported_from_disk"] = True
        _save(data)
    return added


def imported_from_disk():
    """Whether the one-time import of existing anime folders has run."""
    with _lock:
        return bool(_load().get("imported_from_disk"))


POSTER_RETRY_SECONDS = 24 * 3600      # a failed poster lookup is retried after a day


def needs_poster(entry, now=None):
    cover = entry.get("cover") or ""
    if cover and os.path.exists(cover):
        return False
    tried = entry.get("poster_tried") or 0
    return (now or time.time()) - tried >= POSTER_RETRY_SECONDS


def set_poster(url, cover="", page_url="", domain=""):
    """Record a poster lookup: the poster (if found), and the anime's page when
    the entry was only known as a folder. When another entry already has that
    page, the folder entry is folded into it (one anime, one entry). Returns the
    entry's URL afterwards."""
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None:
            return url
        e["poster_tried"] = time.time()
        if cover:
            e["cover"] = cover
        if page_url and (e.get("url") or "").startswith("local://"):
            owner = _find(data, page_url)
            if owner is None:
                e.setdefault("aliases", []).append(e["url"])
                e["url"] = page_url
                if domain:
                    e["domain"] = domain
            elif owner is not e:
                matched = [(_counterpart(owner, p), p) for p in e.get("parts", [])]
                _fold(data, owner, e, [(m, t) for m, t in matched if m is not None])
                e = owner
        _save(data)
        return e["url"]


def remove(url):
    """Remove an entry; returns (entry, index) for an undo, or (None, None)."""
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None:
            return None, None
        index = data["entries"].index(e)
        data["entries"].pop(index)
        _save(data)
        return e, index


def restore(entry, index):
    with _lock:
        data = _load()
        if _find(data, entry.get("url")):
            return False
        data["entries"].insert(max(0, min(index, len(data["entries"]))), entry)
        _save(data)
        return True


def update(url, **fields):
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None:
            return False
        e.update(fields)
        _save(data)
        return True


def set_status(url, status):
    if status not in STATUSES:
        raise ValueError(status)
    return update(url, status=status)


# -------------------------------------------------------------------- parts

def _part_key(template):
    from ui.search_tab import _template_key
    return _template_key(template) or (template or "").rstrip("/")


def merge_parts(old_parts, detected):
    """Detected seasons -> parts, keeping what the user already has on each
    (linked profile, watched episodes, progress), matched by episode template."""
    by_key = {_part_key(p.get("template")): p for p in (old_parts or []) if p.get("template")}
    # Found only as a folder: no episode link to match on. With one season either
    # way it is that season; otherwise it is kept on its own below.
    linkless = [p for p in (old_parts or []) if not p.get("template")]
    adopt = linkless[0] if len(linkless) == 1 and len(detected or []) == 1 else None
    adopted = False
    parts = []
    for i, d in enumerate(detected or []):
        old = by_key.get(_part_key(d.get("template")))
        if old is None and adopt is not None:
            old, adopted = adopt, True
        old = old or {}
        parts.append({
            "label": d.get("label") or (f"Season {i + 1}" if len(detected) > 1 else ""),
            "template": d.get("template", ""),
            "max_ep": int(d.get("max_ep") or old.get("max_ep") or 0),
            "first_ep": int(d.get("first_ep") or old.get("first_ep") or 1),
            "profile": old.get("profile"),
            "watched": list(old.get("watched", [])),
            "progress": dict(old.get("progress", {})),
            "sources": dict(old.get("sources", {})),
        })
    # A part the site no longer lists but that has a profile or watch state is kept.
    seen = {_part_key(p["template"]) for p in parts}
    # (The folder-only part counts as taken only when a season actually took it.)
    for p in old_parts or []:
        if p is adopt and adopted:
            continue
        if not p.get("template") or _part_key(p.get("template")) not in seen:
            if _has_state(p):
                parts.append(p)
    return parts


def _has_state(part):
    """The part carries something of the user's: a profile link, watched episodes,
    half-way progress, or where its episodes came from."""
    return bool(part.get("profile") or part.get("watched") or part.get("progress")
                or part.get("sources"))


def set_parts(url, detected):
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None:
            return False
        e["parts"] = merge_parts(e.get("parts"), detected)
        _absorb_duplicates(data, e)
        _save(data)
        return True


_STATUS_RANK = {LATER: 0, WATCHING: 1, COMPLETED: 2}


def _counterpart(e, theirs):
    """The part of `e` that is the same season as `theirs` (another entry's part),
    or None: same episode link, or -- for a part found only as a folder, which
    has no link -- the folder `e`'s part downloads into."""
    if theirs.get("template"):
        key = _part_key(theirs["template"])
        return next((p for p in e.get("parts", [])
                     if p.get("template") and _part_key(p["template"]) == key), None)
    folder = safe_folder_name(theirs.get("profile") or "").lower()
    if not folder:
        return None
    return next((p for p in e.get("parts", [])
                 if safe_folder_name(p.get("profile") or part_profile_name(e, p)).lower() == folder),
                None)


def _fold(data, e, other, matched):
    """Move `other` into `e` and remove it. `matched` pairs each of e's parts with
    the part of `other` that is the same season; those are merged (profile link,
    watched, progress, sources). Every other part of `other` that holds anything
    of the user's is kept as its own season -- a duplicate never costs a season."""
    for mine, theirs in matched:
        mine["profile"] = mine.get("profile") or theirs.get("profile")
        mine["watched"] = sorted(set(mine.get("watched", [])) | set(theirs.get("watched", [])))
        progress = dict(theirs.get("progress") or {})
        progress.update(mine.get("progress") or {})
        mine["progress"] = {k: v for k, v in progress.items()
                            if int(k) not in mine["watched"]}
        sources = dict(theirs.get("sources") or {})
        sources.update(mine.get("sources") or {})
        mine["sources"] = sources
    taken = {id(theirs) for _mine, theirs in matched}
    for p in other.get("parts", []):
        if id(p) not in taken and _has_state(p):
            e.setdefault("parts", []).append(p)
    e["history"] = (e.get("history", []) + other.get("history", []))[:200]
    if _STATUS_RANK.get(other.get("status"), 0) > _STATUS_RANK.get(e.get("status"), 0):
        e["status"] = other["status"]
    e["last_played"] = e.get("last_played") or other.get("last_played")
    if not e.get("cover") and other.get("cover"):
        e["cover"] = other["cover"]
    aliases = e.setdefault("aliases", [])
    for a in [other.get("url"), *(other.get("aliases") or [])]:
        if a and a not in aliases and _norm_url(a) != _norm_url(e.get("url")):
            aliases.append(a)
    data["entries"].remove(other)


def _absorb_duplicates(data, e):
    """Another entry for the same anime (one imported from its download folder,
    say) is folded into `e` when any of its seasons is one of e's."""
    for other in [o for o in data["entries"] if o is not e]:
        matched = [(_counterpart(e, p), p) for p in other.get("parts", [])]
        matched = [(mine, theirs) for mine, theirs in matched if mine is not None]
        if matched:
            _fold(data, e, other, matched)


def part_profile_name(entry, part):
    """The profile a new download of this part should be created as."""
    title = entry.get("title") or "Anime"
    label = part.get("label")
    many = len(entry.get("parts") or []) > 1
    return f"{title} - {label}" if (many and label) else title


def link_profile(url, part_index, profile):
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None or not (0 <= part_index < len(e.get("parts", []))):
            return False
        e["parts"][part_index]["profile"] = profile
        _save(data)
        return True


def resolve_profiles(profiles):
    """Re-point parts whose profile was renamed or deleted at the profile that now
    downloads the same template. Returns True when anything changed.

    A part whose profile is gone and has no replacement keeps the old name: it
    still names the folder its episodes were downloaded to, so they stay playable
    (Download makes a new profile when it is needed)."""
    from ui.search_tab import find_profile_for_template
    changed = False
    with _lock:
        data = _load()
        for e in data["entries"]:
            for p in e.get("parts", []):
                name = p.get("profile")
                if name and name in profiles and not profiles[name].get("_transient"):
                    continue
                found = find_profile_for_template(profiles, p.get("template"))
                if found and found != name:
                    p["profile"] = found
                    changed = True
        if changed:
            _save(data)
    return changed


def _locate_profile(data, profile):
    """(entry, part) whose part is linked to `profile`, else (None, None)."""
    if not profile:
        return None, None
    for e in data["entries"]:
        for p in e.get("parts", []):
            if p.get("profile") == profile:
                return e, p
    return None, None


def _locate_folder(data, folder_name):
    """(entry, part) whose profile's download folder is `folder_name`."""
    want = (folder_name or "").strip().lower()
    if not want:
        return None, None
    for e in data["entries"]:
        for p in e.get("parts", []):
            if p.get("profile") and safe_folder_name(p["profile"]).lower() == want:
                return e, p
    return None, None


# ------------------------------------------------------- where episodes came from
#
# Every downloaded episode records the site and page it came from, in two places:
# the Library entry (shown under each episode in its details) and a hidden
# .aed-source.json in the anime's folder. The folder copy outlives the Library
# and the profile, so a later folder scan still knows which site an anime is
# from -- and can download its missing episodes.

SOURCE_FILE = ".aed-source.json"


def _site_of_template(template):
    from urllib.parse import urlsplit
    host = (urlsplit(template or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def episode_source(template, ep, when=None):
    return {"site": _site_of_template(template),
            "page": (template or "").replace("{x}", str(ep)),
            "date": when or time.time()}


def read_folder_source(folder):
    """The note an anime folder keeps about its source: {"template", "site",
    "episodes": {"<ep>": {"site", "page", "date"}}}, or {}."""
    try:
        with open(os.path.join(folder, SOURCE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_folder_source(folder, template, episodes, when=None):
    """Add `episodes` to the folder's source note (kept hidden: it's for the app)."""
    if not folder or not os.path.isdir(folder) or not template or not episodes:
        return False
    data = read_folder_source(folder)
    data["template"] = template
    data["site"] = _site_of_template(template)
    eps = data.setdefault("episodes", {})
    for ep in episodes:
        eps[str(ep)] = episode_source(template, ep, when)
    path = os.path.join(folder, SOURCE_FILE)
    tmp = path + ".tmp"
    try:
        # Written beside and moved over: Windows refuses to overwrite a hidden
        # file in place, and the note is hidden.
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)     # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass
    return True


def part_sources(part, download_dir):
    """{episode: source} for a part: what its entry recorded, plus anything only
    its folder's note knows (episodes downloaded before it was in the Library)."""
    note = read_folder_source(part_folder(part, download_dir)) if download_dir else {}
    merged = dict(note.get("episodes") or {})
    merged.update(part.get("sources") or {})
    out = {}
    for k, v in merged.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def record_sources(profile, episodes, template, folder):
    """Downloads finished: note each episode's site on its Library entry (when the
    anime is there) and in its folder. Called from the download engine."""
    episodes = sorted({int(e) for e in episodes or []})
    if not episodes or not template:
        return
    when = time.time()
    write_folder_source(folder, template, episodes, when)
    with _lock:
        data = _load()
        _e, part = _locate_profile(data, profile)
        if part is None:
            return
        sources = part.setdefault("sources", {})
        for ep in episodes:
            sources[str(ep)] = episode_source(template, ep, when)
        _save(data)


# ------------------------------------------------------------ status events

def _total_eps(part):
    try:
        first, last = int(part.get("first_ep") or 1), int(part.get("max_ep") or 0)
    except (TypeError, ValueError):
        return []
    return list(range(first, last + 1)) if last >= first else []


def is_finished(entry):
    """Every known episode of every part is watched (and at least one is known)."""
    parts = entry.get("parts") or []
    known = [(p, _total_eps(p)) for p in parts]
    if not any(eps for _p, eps in known):
        return False
    return all(set(eps) <= set(p.get("watched", [])) for p, eps in known)


def _on_activity(entry):
    """Downloading or playing: an anime still on "watch later" is now being watched."""
    if entry.get("status") == LATER:
        entry["status"] = WATCHING


def _on_watched(entry):
    if is_finished(entry):
        entry["status"] = COMPLETED
    else:
        _on_activity(entry)


def _locate_template(data, template):
    """(entry, part) whose part downloads the same episodes as `template`."""
    if not template:
        return None, None
    key = _part_key(template)
    for e in data["entries"]:
        for p in e.get("parts", []):
            if p.get("template") and _part_key(p["template"]) == key:
                return e, p
    return None, None


def _profile_snapshot(profile):
    from utils.config import sites_data, config_lock
    with config_lock:
        data = sites_data.get(profile)
        return json.loads(json.dumps(data)) if isinstance(data, dict) else None


def _entry_for_profile(profile, snapshot):
    """A new Library entry for a downloaded profile, built like a folder scan
    builds one (utils/library_scan), or None."""
    try:
        from utils.config import app_settings, config_lock, get_watchlist
        from utils.library_scan import entry_for_profile
        with config_lock:
            download_dir = app_settings.get("download_dir", "")
        return entry_for_profile(profile, snapshot, download_dir, get_watchlist())
    except Exception:
        return None


def record_download(profile, episodes_str, status, notes):
    """A download finished/failed/was cancelled for `profile`. Adds it to that
    anime's history. A profile no entry is linked to is matched to the entry
    with the same episode link (and linked), else its anime is added to the
    Library -- the Library is where download history is shown, so no download
    goes unseen. Returns True if recorded."""
    # Everything that needs config_lock is read before _lock: the GUI thread takes
    # config_lock then _lock (resolve_profiles), so the reverse could deadlock.
    snapshot = _profile_snapshot(profile)
    template = (snapshot or {}).get("url", "")
    candidate = _entry_for_profile(profile, snapshot) if template else None
    with _lock:
        data = _load()
        e, _p = _locate_profile(data, profile)
        if e is None:
            e, p = _locate_folder(data, safe_folder_name(profile or ""))
        if e is None and template:
            e, p = _locate_template(data, template)
            if p is not None and not p.get("profile"):
                p["profile"] = profile
        if e is None and candidate is not None:
            e = _find(data, candidate["url"])      # its page is here, its season isn't
            if e is None:
                e = candidate
                data["entries"].append(e)
            else:
                e.setdefault("parts", []).extend(candidate["parts"])
        if e is None:
            return False
        e.setdefault("history", []).insert(0, {
            "date": datetime.now().strftime("%b %d, %Y • %I:%M %p"),
            "profile": profile, "episodes": str(episodes_str),
            "status": str(status), "notes": str(notes or ""),
        })
        del e["history"][200:]
        if status in ("Success", "Partial"):
            _on_activity(e)
        _save(data)
        return True


def _write_watched(e, part, after):
    before = set(part.get("watched", []))
    part["watched"] = sorted(after)
    for ep in after:
        part.get("progress", {}).pop(str(ep), None)
    if after - before:
        _on_watched(e)


def clear_history(url):
    """Forget an anime's download history (its episodes and watch state stay)."""
    return update(url, history=[])


def set_watched(url, part_index, episodes):
    """Manual ticks from the episode grid: `episodes` is the full watched set."""
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None or not (0 <= part_index < len(e.get("parts", []))):
            return False
        _write_watched(e, e["parts"][part_index], {int(x) for x in episodes})
        _save(data)
        return True


def part_ident(part):
    """What identifies a season across reloads: its episode link, or -- found
    only as a folder -- its folder."""
    t = part.get("template")
    return ("t", _part_key(t)) if t else ("f", safe_folder_name(part.get("profile") or "").lower())


def change_watched(url, part_index, added=(), removed=(), ident=None):
    """Tick `added` and untick `removed`, leaving every other episode as it is
    now -- including ones mpv.net ticked meanwhile and ones the grid didn't show.
    `ident` (part_ident) finds the season if the parts moved since it was read."""
    with _lock:
        data = _load()
        e = _find(data, url)
        if e is None:
            return False
        parts = e.get("parts") or []
        part = parts[part_index] if 0 <= part_index < len(parts) else None
        if ident is not None and (part is None or part_ident(part) != ident):
            part = next((p for p in parts if part_ident(p) == ident), None)
        if part is None:
            return False
        after = (set(part.get("watched", [])) - {int(x) for x in removed}) | {int(x) for x in added}
        _write_watched(e, part, after)
        _save(data)
        return True


# ------------------------------------------------------ mpv.net progress log
#
# assets/mpvnet/scripts/aed-progress.lua appends one JSON line per played file:
#   {"path": "...\\<Profile>\\<Profile> Ep7.mp4", "percent": 93.1, "eof": false}
# Files are matched to an entry by their folder (the profile's download folder)
# and the "Ep<n>" in the name, so anything played in mpv.net counts -- from this
# app, Explorer, or mpv.net's own recent list.

def parse_log_line(line):
    """(folder name, episode, percent, eof) from one log line, or None."""
    try:
        rec = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(rec, dict):              # "123", "[1]", "null": not a record
        return None
    path = str(rec.get("path") or "")
    if not path.lower().endswith(VIDEO_EXTENSIONS):
        return None
    ep = episode_number(path.replace("\\", "/"))
    if ep is None:
        return None
    try:
        percent = float(rec.get("percent") or 0)
    except (TypeError, ValueError, OverflowError):
        percent = 0.0
    if percent != percent:                     # NaN
        percent = 0.0
    folder = os.path.basename(os.path.dirname(path.replace("/", "\\")))
    return folder, ep, percent, bool(rec.get("eof"))


def _earlier_episodes(part, ep):
    """The part's episodes before `ep` (from its first episode)."""
    try:
        first = int(part.get("first_ep") or 1)
    except (TypeError, ValueError):
        first = 1
    return set(range(first, ep))


def _mark_watched(part, episodes):
    """Add `episodes` to the part's watched set and drop their half-way progress.
    Returns True when any of them was not watched before."""
    watched = set(part.get("watched", []))
    new = set(episodes) - watched
    progress = part.get("progress") or {}
    for ep in episodes:
        progress.pop(str(ep), None)
    if new:
        part["watched"] = sorted(watched | new)
    return bool(new)


def fill_earlier_watched():
    """One-time catch-up for entries tracked before "playing episode N means the
    earlier ones were seen": fill each part up to its furthest watched or started
    episode. Returns True when anything changed."""
    changed = False
    with _lock:
        data = _load()
        if data.get("filled_earlier"):
            return False
        for e in data["entries"]:
            added = False
            for part in e.get("parts", []):
                reached = [*part.get("watched", []), *(int(k) for k in part.get("progress") or {})]
                if reached:
                    added |= _mark_watched(part, _earlier_episodes(part, max(reached)))
            if added:
                _on_watched(e)
                changed = True
        data["filled_earlier"] = True
        _save(data)
    return changed


def _apply_line(data, line):
    parsed = parse_log_line(line)
    if not parsed:
        return False
    folder, ep, percent, eof = parsed
    e, part = _locate_folder(data, folder)
    if e is None:
        return False
    e["last_played"] = {"profile": part.get("profile"), "ep": ep, "ts": time.time()}
    progress = part.setdefault("progress", {})
    # Playing episode N means 1..N-1 were seen (watched elsewhere, or
    # before tracking existed), even if N itself was only started.
    done = _earlier_episodes(part, ep)
    if eof or percent >= WATCHED_PERCENT:
        done.add(ep)
    elif ep not in part.get("watched", []):
        progress[str(ep)] = round(max(0.0, min(percent, 100.0)), 1)
    if _mark_watched(part, done):
        _on_watched(e)
    else:
        _on_activity(e)
    return True


def _apply_lines(data, lines):
    changed = False
    for line in lines:
        try:
            changed |= _apply_line(data, line)
        except Exception:
            continue                   # one odd line never costs the others
    return changed


def apply_progress(lines):
    """Fold log lines into the entries. Returns True when anything changed."""
    with _lock:
        data = _load()
        changed = _apply_lines(data, lines)
        if changed:
            _save(data)
    return changed


def ingest_log(log_path):
    """Read what mpv.net appended since last time and apply it. The lines and the
    new read position are saved together, so nothing read is ever skipped.
    Returns True when anything changed."""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return False
    with _lock:
        data = _load()
        try:
            offset = int(data.get("log_offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        if size < offset:          # the log was cleared or replaced
            offset = 0
        if size == offset:
            return False
        try:
            with open(log_path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return False
        # Only whole lines; a line mpv is still writing is read next time.
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return False
        lines = chunk[:cut].decode("utf-8", errors="replace").splitlines()
        changed = _apply_lines(data, lines)
        data["log_offset"] = offset + cut + 1
        _save(data)
    return changed


# --------------------------------------------------------- files and continue

_listing = threading.local()


@contextmanager
def cached_listing():
    """Within this block each folder is listed once: the Library builds every
    row from summary, next_episode and missing_episodes, which each list the
    same folders. Per thread, and nestable."""
    depth = getattr(_listing, "depth", 0)
    if not depth:
        _listing.cache = {}
    _listing.depth = depth + 1
    try:
        yield
    finally:
        _listing.depth = depth
        if not depth:
            _listing.cache = None


def episode_files(folder):
    """{episode: path} for the videos in a profile's folder ("<name> Ep<n>.<ext>")."""
    cache = getattr(_listing, "cache", None)
    if cache is not None and folder in cache:
        return dict(cache[folder])
    found = {}
    try:
        names = sorted(os.listdir(folder)) if folder else []
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith(VIDEO_EXTENSIONS):
            continue
        ep = episode_number(name)
        if ep is not None:
            found.setdefault(ep, os.path.join(folder, name))
    if cache is not None:
        cache[folder] = dict(found)
    return found


def part_folder(part, download_dir):
    if not part.get("profile") or not download_dir:
        return ""
    return os.path.join(download_dir, safe_folder_name(part["profile"]))


def next_episode(entry, download_dir):
    """What "Continue" plays: (part index, episode, file or None, percent).

    An episode left part-way through comes first; otherwise the first unwatched
    episode after the last watched one, in part order. None when nothing is left.
    """
    parts = entry.get("parts") or []
    for i, part in enumerate(parts):
        progress = {int(k): v for k, v in (part.get("progress") or {}).items()
                    if str(k).isdigit()}
        if progress:
            ep = min(progress)
            files = episode_files(part_folder(part, download_dir))
            try:
                percent = float(progress[ep])
            except (TypeError, ValueError):
                percent = 0.0
            return i, ep, files.get(ep), percent
    for i, part in enumerate(parts):
        watched = set(part.get("watched", []))
        files = episode_files(part_folder(part, download_dir))
        candidates = set(_total_eps(part)) | set(files)
        start = max(watched) if watched else 0
        later = sorted(ep for ep in candidates if ep > start and ep not in watched)
        if later:
            ep = later[0]
            return i, ep, files.get(ep), 0.0
    return None


def missing_episodes(entry, download_dir):
    """(part index, [episodes]) the site lists but that aren't downloaded, for
    the first part that has any and whose episode link is known; else None."""
    for i, part in enumerate(entry.get("parts") or []):
        if not part.get("template"):
            continue
        files = episode_files(part_folder(part, download_dir))
        missing = [ep for ep in _total_eps(part) if ep not in files]
        if missing:
            return i, missing
    return None


def missing_from(entry, part_index, ep, download_dir):
    """The episodes of one part, from `ep` on, that aren't downloaded -- what a
    "Ep N isn't downloaded" card downloads. At least [ep] when ep isn't on disk."""
    parts = entry.get("parts") or []
    if not 0 <= part_index < len(parts):
        return []
    files = episode_files(part_folder(parts[part_index], download_dir))
    missing = [e for e in _total_eps(parts[part_index]) if e >= ep and e not in files]
    if ep not in files and ep not in missing:
        missing.insert(0, ep)
    return missing


def playlist_from(entry, part_index, ep, download_dir):
    """The file for `ep` and every downloaded episode after it in that part."""
    part = entry["parts"][part_index]
    files = episode_files(part_folder(part, download_dir))
    return [files[e] for e in sorted(files) if e >= ep]


def summary(entry, download_dir):
    """(watched, total, downloaded) across all parts. total is 0 when unknown."""
    watched = total = downloaded = 0
    for part in entry.get("parts") or []:
        watched += len(part.get("watched", []))
        total += len(_total_eps(part))
        downloaded += len(episode_files(part_folder(part, download_dir)))
    return watched, total, downloaded
